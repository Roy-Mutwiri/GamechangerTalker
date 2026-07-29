"""Warudo bridge.

Warudo takes external control through its Blueprint system: "On WebSocket
Action" nodes feeding "Set Character Blend Shape" nodes. See WARUDO_SETUP.md
for the blueprint, node by node.

Its WebSocket server accepts exactly one envelope, and discards anything
else with "Received data but action is null":

    {"action": "viseme_ee", "data": 0.8}
    {"action": "emote", "data": "Fun"}

There is no JSON-parsing node in Warudo, so a single message carrying all
five weights could not be unpacked inside a blueprint. Each channel is its
own action instead, and unchanged channels are not sent at all -- a closed
mouth costs nothing on the wire.

The narrator keeps speaking even when this bridge is dead. Audio is the
stream; the avatar is decoration. Nothing in here is allowed to raise into
the narration loop, and nothing in here is allowed to block it -- viseme
frames go into a small bounded queue and a writer task drains it. If the
socket stalls, frames are dropped rather than queued: a viseme delivered late
is worse than one never sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from typing import Any

from narrator.config import Config
from narrator.speech.visemes import VisemeFrame, rest_frame

log = logging.getLogger(__name__)

QUEUE_LIMIT = 8  # ~130ms of 60fps frames; beyond that, drop


class WarudoBridge:
    def __init__(self, cfg: Config, *, enabled: bool = True) -> None:
        self.cfg = cfg
        self.enabled = enabled and cfg.warudo.enabled
        self.url = f"ws://{cfg.warudo.host}:{cfg.warudo.port}{cfg.warudo.path}"
        self.connected = False
        self.frames_sent = 0
        self.frames_by_slot = [0, 0]
        self.frames_dropped = 0
        self.emotes_sent = 0
        self.reconnects = 0
        self.last_error = ""
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._socket: Any = None
        self._stopping = asyncio.Event()
        # Last weight sent per prefixed channel, so unchanged ones can be
        # skipped without one character's mouth silencing the other's.
        self._last_sent: dict[str, float] = {}
        # Whose mouth the viseme stream currently drives. Switched per line by
        # speak_as(); with one character on stage it never changes.
        self.viseme_prefix = cfg.warudo.action_prefix

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if not self.enabled:
            log.info("Warudo bridge disabled")
            return
        try:
            import websockets  # noqa: F401
        except ImportError:
            log.warning(
                "websockets is not installed; the avatar bridge is off "
                "(pip install websockets). The stream continues without it."
            )
            self.enabled = False
            return
        await asyncio.gather(self._connect_loop(), self._writer_loop())

    async def _connect_loop(self) -> None:
        import websockets

        delay = self.cfg.warudo.reconnect_seconds
        while not self._stopping.is_set():
            try:
                async with websockets.connect(
                    self.url, ping_interval=20, close_timeout=1
                ) as socket:
                    self._socket = socket
                    self.connected = True
                    log.info("Warudo connected at %s", self.url)
                    await self._until_closed(socket)
                    if not self._stopping.is_set():
                        self.reconnects += 1
                        log.warning("Warudo closed the connection; reconnecting")
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                if self.connected:
                    self.reconnects += 1
                    log.warning("Warudo disconnected (%s); reconnecting", self.last_error)
            self.connected = False
            self._socket = None
            if self._stopping.is_set():
                return
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=delay)
                return
            except TimeoutError:
                pass

    async def _until_closed(self, socket: Any) -> None:
        """Wait for Warudo to go away, or for us to be shutting down.

        Waiting only on the shutdown event parks this loop inside a dead
        connection: Warudo restarts, the socket closes, and nothing ever
        reconnects. That failure is silent in the worst way -- the narrator
        keeps talking, the transcript keeps scrolling, and the avatar simply
        stops listening -- so the close has to be an awaited signal here, not
        something the writer notices later and cannot act on.
        """
        closed = asyncio.ensure_future(socket.wait_closed())
        stopping = asyncio.ensure_future(self._stopping.wait())
        try:
            await asyncio.wait({closed, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (closed, stopping):
                if not task.done():
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

    async def _writer_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                message = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            socket = self._socket
            if socket is None:
                continue
            try:
                await socket.send(json.dumps(message))
                if self.is_viseme(message):
                    self.frames_sent += 1
                    # Per character, so "the second one never moves" is a
                    # number rather than an argument about what we can see.
                    action = str(message.get("action", ""))
                    slot = 1 if action.startswith(self._all_prefixes()[1]) else 0
                    self.frames_by_slot[slot] += 1
                else:
                    self.emotes_sent += 1
            except Exception as exc:
                self.last_error = f"{exc.__class__.__name__}: {exc}"
                self.connected = False
                # Drop the dead socket so the writer stops posting into it and
                # the connect loop is the only thing that hands out a new one.
                self._socket = None

    async def stop(self) -> None:
        self._stopping.set()
        socket = self._socket
        if socket is not None:
            with contextlib.suppress(Exception):
                await socket.close()

    # -- sending ------------------------------------------------------------

    def send(self, message: dict[str, Any]) -> None:
        """Non-blocking. Drops the frame if the queue is backed up."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self.frames_dropped += 1

    def send_viseme(self, frame: VisemeFrame) -> None:
        """One action per channel, and only for channels that actually moved.

        Warudo has no JSON-parsing node, so a single fat message cannot be
        unpacked in a blueprint. Each blendshape is its own action, matched
        by its own "On WebSocket Action" node. Suppressing unchanged channels
        keeps a closed mouth completely silent on the wire instead of costing
        300 messages a second.
        """
        epsilon = self.cfg.warudo.viseme_epsilon
        prefix = self.viseme_prefix
        for name, weight in frame.weights.items():
            value = self.shaped(weight)
            # The suppression cache is per prefix. Two characters share one
            # connection, and a channel the other host already sent at this
            # value must not silence this host's mouth.
            cache_key = f"{prefix}{name}"
            previous = self._last_sent.get(cache_key)
            if previous is not None and abs(previous - value) < epsilon:
                continue
            self._last_sent[cache_key] = value
            self.send({"action": cache_key, "data": round(value, 3)})

    def shaped(self, weight: float) -> float:
        """Articulation weight -> what this particular avatar needs to see it.

        The engine says how open the mouth is; this says how hard that has to
        be pushed for the morph to read on screen. Kept separate on purpose --
        the phonetics should not have to know that one model's mouth is weaker
        than another's.
        """
        gamma = self.cfg.warudo.viseme_gamma
        if gamma == 1.0 or weight <= 0.0:
            return weight
        return min(1.0, weight**gamma)

    def send_rest(self, *, all_characters: bool = False) -> None:
        """All zeros, so the mouth never sticks open between lines."""
        prefixes = self._all_prefixes() if all_characters else [self.viseme_prefix]
        for prefix in prefixes:
            for name in rest_frame().weights:
                self._last_sent[f"{prefix}{name}"] = 0.0
                self.send({"action": f"{prefix}{name}", "data": 0.0})

    # -- which character is speaking -----------------------------------------

    def _all_prefixes(self) -> list[str]:
        base = self.cfg.warudo.action_prefix
        return [base, f"{base.rstrip('_')}2_"]

    def is_viseme(self, message: dict[str, Any]) -> bool:
        """Mouth movement, as opposed to an emote or a camera move.

        Classified on the action name because that is all these messages carry
        -- {"action": "viseme_aa", "data": 0.8}. The counter used to test for a
        "type" key they have never had, so the status line read "0 frames"
        through an entire stream of working lip sync. Both characters count:
        the second speaks on viseme2_, which is not a prefix of viseme_.
        """
        action = str(message.get("action", ""))
        return any(action.startswith(prefix) for prefix in self._all_prefixes())

    def speak_as(self, index: int) -> None:
        """Route the viseme stream to character 1 or character 2.

        Called once when a line starts, not per frame: the prefix decides whose
        mouth moves, and switching it mid-utterance would leave the previous
        character's face frozen wherever it was.
        """
        base = self.cfg.warudo.action_prefix
        wanted = base if index <= 0 else f"{base.rstrip('_')}{index + 1}_"
        if wanted == self.viseme_prefix:
            return
        # Close the mouth we are leaving before handing over, or it sticks.
        self.send_rest()
        self.viseme_prefix = wanted
        log.info("viseme stream -> character %d (%s)", index + 1, wanted)

    def send_camera(self, yaw: float, pitch: float, distance: float) -> None:
        """Orbit the framing camera around the character's face.

        The operator drags on the avatar panel; this turns that into a world
        transform. Framing a VTuber by typing numbers into a transform panel
        is miserable, and doing it in Warudo's own window means alt-tabbing
        away from the thing being framed.

        Angles are degrees, distance is metres from the focus point. The
        camera always looks back at the focus, so the character cannot slide
        out of frame the way a free-flying camera lets it.
        """
        focus_height = self.cfg.warudo.camera_focus_height
        yaw_rad = math.radians(yaw)
        pitch_rad = math.radians(pitch)

        flat = distance * math.cos(pitch_rad)
        position = {
            "x": round(flat * math.sin(yaw_rad), 4),
            "y": round(focus_height + distance * math.sin(pitch_rad), 4),
            "z": round(flat * math.cos(yaw_rad), 4),
        }
        # Face back down the orbit arm: yaw flipped 180 to look inwards, and
        # pitch used as-is because Unity's positive X rotation tilts *down* --
        # so a camera raised above the face looks down at it.
        rotation = {"x": round(pitch, 3), "y": round(yaw + 180.0, 3), "z": 0.0}

        self.send({"action": self.cfg.warudo.camera_position_action, "data": position})
        self.send({"action": self.cfg.warudo.camera_rotation_action, "data": rotation})

    def send_avatar(self, source: str, focus_height: float = 0.0) -> None:
        """Swap the character, and move the camera to suit the new one.

        Models differ enormously in height -- a chibi's face sits near 0.9m,
        an adult VRM's near 1.5m -- so a swap without re-framing leaves the
        camera pointing at an empty room and looks like a crash.

        `Set Asset Property` takes the *serialized* value, so the payload is a
        quoted JSON string rather than a bare path.

        **Known limitation.** Warudo reliably *unloads* the current character
        on a Source change and does not reliably load the replacement -- its
        log says `Unloaded ModHost<name>` and the scene is left empty. The
        property change itself sticks and survives, so the pick is not lost;
        it may simply not appear until Warudo reloads the scene. This warns
        rather than pretending the swap is always instant.
        """
        self.send({"action": self.cfg.warudo.avatar_action, "data": json.dumps(source)})
        if focus_height > 0:
            self.cfg.warudo.camera_focus_height = focus_height

    def reload_scene(self) -> None:
        """Ask Warudo to reload the scene from disk.

        This is what makes an avatar switch actually land. Setting `Source` on
        a live character unloads the old model and does not load the new one;
        writing the file and reloading does both, and brings the character's
        placement and the camera shot with it.
        """
        self.send(
            {"action": self.cfg.warudo.reload_action, "data": self.cfg.warudo.scene_name}
        )

    def expression_for(self, name: str) -> str:
        """The expression clip name to send, for the configured VRM version."""
        mapping = self.cfg.warudo.expressions.get(name, [])
        style = self.cfg.warudo.expression_style
        if style == "vrm1" and len(mapping) > 0:
            return mapping[0]
        if style == "vrm0" and len(mapping) > 1:
            return mapping[1]
        return name

    def send_emote(self, name: str, hold: float = 1.5) -> None:
        """One action carrying the expression name the model actually has.

        `hold` is not sent: the blueprint releases the expression itself with
        a Delay node, because Warudo has no way to unpack a second field out
        of the same message.
        """
        self.send(
            {
                "action": self.cfg.warudo.emote_action,
                "data": self.expression_for(name),
            }
        )

    # -- the viseme pump ----------------------------------------------------

    async def play(
        self, frames: list[VisemeFrame], started_at: float | None = None
    ) -> None:
        """Push a 60fps stream, paced against the audio clock.

        `started_at` is a time.perf_counter() stamp taken when playback
        began, so the mouth tracks the sound rather than drifting from it.
        Frames whose moment has already passed are skipped, never played
        late.
        """
        if not self.enabled or not frames:
            return
        anchor = started_at if started_at is not None else time.perf_counter()
        for frame in frames:
            if self._stopping.is_set():
                break
            wait = anchor + frame.t - time.perf_counter()
            if wait < -0.02:
                continue  # behind schedule: skip rather than lag
            if wait > 0:
                await asyncio.sleep(wait)
            self.send_viseme(frame)
        self.send_rest()

    # -- status -------------------------------------------------------------

    def status(self) -> str:
        if not self.enabled:
            return "off"
        if self.connected:
            first, second = self.frames_by_slot
            if second:
                return f"ok ({first}+{second} frames)"
            return f"ok ({self.frames_sent} frames)"
        return f"down ({self.last_error or 'not connected'})"
