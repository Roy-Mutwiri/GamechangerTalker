"""Telling Unreal the things that are not the face.

Live Link carries sixty-one numbers sixty times a second and that is all it
carries. It cannot say "start the talking idle", "look at the chart", or "seat
a second host", because none of those are blendshapes -- they are decisions,
and they arrive a few times a minute rather than sixty times a second.

So there are two channels to Unreal and they are deliberately different
shapes. The face (`avatar/livelink.py`) is a firehose of state with no
acknowledgement. This is five function calls, rare, each one a discrete event
that either happened or did not.

THE CONTRACT
Five public functions on the presenter actor's Blueprint, named exactly, with
their input pins named exactly:

    SetSpeaking(speaking: bool)           talking idle on / off
    SetMood(name: string, weight: float)  posture: back for bored, forward for excited
    PlayGesture(name: string)             a montage on the Gestures slot
    SetCamera(name: string)               view target, blended
    SetStage(count: int)                  how many seats are occupied

UNREAL_SETUP.md is the source of truth for what each one does inside the
editor, and it and this file have to change together. Everything the narrator
can say to Unreal goes through these five. A sixth is a change to the
contract and to the document, not a new ad-hoc endpoint.

THREE TRANSPORTS, ONE CONTRACT
`remote_control` is the Remote Control API's HTTP server. It is the better
one: it names the function, it reports whether the call landed, and
`tools/unreal_check.py` can ask the editor to list what the actor actually
has. `osc` is the fallback for an operator who would rather drop one OSC
Server node into a level Blueprint than copy an object path out of the World
Outliner -- same five calls, same argument order, no reply, no way to know.
`none` is the face without the body: Live Link still drives the MetaHuman and
nobody has to find an object path to get a blink.

NOTHING HERE MAY RAISE OR BLOCK
Same rule as the Warudo bridge, for the same reason: the narrator keeps
talking when Unreal is closed. `call` is invoked from the speaking path, so an
exception in it costs the stream a line -- which means even a misspelt
function name is counted and logged, never raised. Calls go into a bounded
queue that one worker task drains; urllib runs in a thread so a stalled editor
cannot park the narration loop; a full queue drops its oldest entry rather
than waiting for room.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

log = logging.getLogger(__name__)

TRANSPORT_REMOTE = "remote_control"
TRANSPORT_OSC = "osc"
TRANSPORT_NONE = "none"

#: The five, and the parameter names their Blueprint pins must carry. Remote
#: Control matches pin names, so a pin called `Name` where this says `name`
#: fails with a 400 whose body says only that a parameter could not be found.
FUNCTIONS: dict[str, tuple[str, ...]] = {
    "SetSpeaking": ("speaking",),
    "SetMood": ("name", "weight"),
    "PlayGesture": ("name",),
    "SetCamera": ("name",),
    "SetStage": ("count",),
}

#: OSC addresses, when the transport is `osc`. Arguments go in the same order
#: as the parameter names above.
OSC_ADDRESS: dict[str, str] = {
    "SetSpeaking": "/character/speaking",
    "SetMood": "/character/mood",
    "PlayGesture": "/character/gesture",
    "SetCamera": "/character/camera",
    "SetStage": "/character/stage",
}

#: Short on purpose, and not a config field. This is a fire-and-forget control
#: call on a machine that is also rendering at 60fps; there is no value of this
#: number for which waiting is the right trade, so it is not offered as a knob.
TIMEOUT_SECONDS = 1.5

#: Deep enough to absorb a burst -- a line's mood, gesture and speaking flag
#: arrive within a few milliseconds of each other -- and shallow enough that a
#: dead editor cannot accumulate an hour of stale instructions to replay when
#: it comes back.
QUEUE_LIMIT = 32

#: Below this, two identical calls to the same function are the same call.
#: Stops a per-line SetSpeaking(true) going out twice when a handover sound
#: runs into the line it was covering.
DEDUP_SECONDS = 0.05


@dataclass
class Call:
    function: str
    parameters: dict[str, Any] = field(default_factory=dict)
    at: float = 0.0

    def arguments(self) -> list[Any]:
        """Parameters in contract order, for a transport that is positional."""
        return [self.parameters[name] for name in FUNCTIONS[self.function]]


class UnrealTransport:
    """The surface `avatar/character.py` calls. Queueing lives here.

    Subclasses implement `_deliver` and `ping`. They do not implement the
    queue, the dedup, the counters or the never-raise guarantee, because
    those are the parts that must behave identically whichever wire is in
    use -- an operator switching from Remote Control to OSC to work around a
    firewall should be changing how the bytes travel and nothing else.
    """

    name = "none"

    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled
        self.calls_sent = 0
        self.calls_failed = 0
        self.calls_dropped = 0
        self.calls_rejected = 0
        self.last_error = ""
        self.reachable = False

        self._queue: asyncio.Queue[Call] = asyncio.Queue(maxsize=QUEUE_LIMIT)
        self._stopping = asyncio.Event()
        self._last: dict[str, tuple[float, str]] = {}
        self._logged: set[str] = set()
        # Edge-triggered state: these are only worth sending when they change.
        self._speaking: bool | None = None
        self._stage: int | None = None
        self._camera = ""

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Drain the queue until `stop()`. Runs as its own task -- do NOT await.

            asyncio.create_task(transport.start())   # yes
            await transport.start()                  # hangs the caller forever

        Awaiting this parks whatever called it for the life of the stream. The
        symptom is not an error: the character simply never starts, the frame
        pump never runs, the face never blinks, and everything upstream looks
        healthy. Worse, it hides under `transport = "none"`, where the
        `not self.enabled` branch below returns immediately -- so the only
        configuration that appears to work is the one that does nothing, and
        every real transport is broken. Test the hanging shape, not the inert
        one.
        """
        if not self.enabled:
            log.info("Unreal control disabled (transport=%s)", self.name)
            return
        while not self._stopping.is_set():
            try:
                call = await asyncio.wait_for(self._queue.get(), timeout=0.5)
            except TimeoutError:
                continue
            try:
                await self._send(call)
            except Exception as exc:  # never into the narration loop
                self.calls_failed += 1
                self.reachable = False
                self._note(f"{exc.__class__.__name__}: {exc}")

    async def stop(self) -> None:
        self._stopping.set()

    async def _send(self, call: Call) -> None:
        self._deliver(call)

    def _deliver(self, call: Call) -> None:  # pragma: no cover - base is inert
        raise NotImplementedError

    def _note(self, message: str) -> None:
        """Record an error, and say it once rather than once per call.

        An editor that is closed is closed for every call, and a stream is
        eight hours long. The first occurrence of each distinct message is
        logged; the rest are counted.
        """
        self.last_error = message
        if message not in self._logged:
            self._logged.add(message)
            log.warning("Unreal control: %s", message)

    # -- the surface --------------------------------------------------------

    def call(self, function: str, **parameters: Any) -> None:
        """Queue one call. Never raises, never blocks.

        Invoked from the speaking path. A misspelt function name, a missing
        parameter and a dead editor are all counted and logged here rather
        than propagating, because none of them is worth costing the stream the
        line that was being spoken when it happened.
        """
        if not self.enabled:
            return
        expected = FUNCTIONS.get(function)
        if expected is None:
            self.calls_rejected += 1
            self._note(f"{function!r} is not one of the five contract functions")
            return
        missing = [pin for pin in expected if pin not in parameters]
        if missing:
            self.calls_rejected += 1
            self._note(f"{function} called without {missing}")
            return

        signature = json.dumps(parameters, sort_keys=True, default=str)
        now = time.monotonic()
        previous = self._last.get(function)
        if previous is not None and previous[1] == signature:
            if now - previous[0] < DEDUP_SECONDS:
                return
        self._last[function] = (now, signature)

        entry = Call(function, dict(parameters), now)
        try:
            self._queue.put_nowait(entry)
        except asyncio.QueueFull:
            # Drop the oldest rather than the newest: the most recent
            # instruction is the one that describes the present.
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self.calls_dropped += 1
            with contextlib.suppress(asyncio.QueueFull):
                self._queue.put_nowait(entry)

    def ping(self) -> bool:
        """Is Unreal there. Blocking, and only the check tool should call it."""
        return False

    def stats(self) -> dict[str, int]:
        return {
            "sent": self.calls_sent,
            "failed": self.calls_failed,
            "dropped": self.calls_dropped,
            "rejected": self.calls_rejected,
            "queued": self._queue.qsize(),
        }

    def status(self) -> str:
        if not self.enabled:
            return "off"
        if self.last_error and not self.reachable:
            return f"down ({self.last_error})"
        if not self.calls_sent:
            return f"{self.name}, nothing sent yet"
        return f"ok ({self.calls_sent} calls, {self.name})"

    # -- the five, as named methods -----------------------------------------
    #
    # Sugar over `call`, and the place the edge-triggering lives. A caller may
    # use either; these exist because "did this change" is a question three of
    # the five need answered and nobody should answer it twice.

    def set_speaking(self, speaking: bool) -> bool:
        if self._speaking == speaking:
            return False
        self._speaking = speaking
        self.call("SetSpeaking", speaking=bool(speaking))
        return True

    def set_mood(self, name: str, weight: float = 1.0) -> bool:
        self.call("SetMood", name=str(name), weight=float(weight))
        return True

    def play_gesture(self, name: str) -> bool:
        # An Unreal montage name cannot carry a dash and neither can a Warudo
        # action; `lean-in` is `lean_in` on both sides for the same reason.
        self.call("PlayGesture", name=str(name).replace("-", "_"))
        return True

    def set_camera(self, name: str) -> bool:
        if name == self._camera:
            return False
        self._camera = name
        self.call("SetCamera", name=str(name))
        return True

    def set_stage(self, count: int) -> bool:
        if self._stage == count:
            return False
        self._stage = int(count)
        self.call("SetStage", count=int(count))
        return True

    def call_now(self, function: str, **parameters: Any) -> None:
        """Straight down the wire, no queue. For `tools/unreal_check.py`.

        This one *does* raise: a check tool that swallows the reason nothing
        happened is worse than no check tool.
        """
        self._deliver(Call(function, parameters, time.monotonic()))


class NullTransport(UnrealTransport):
    """`transport = "none"`. Accepts everything, sends nothing, costs nothing."""

    name = TRANSPORT_NONE

    def __init__(self) -> None:
        super().__init__(enabled=False)

    def _deliver(self, call: Call) -> None:
        return


class RemoteControlTransport(UnrealTransport):
    """Unreal's Remote Control API over HTTP.

    The web server is not the same thing as the plugin. Enabling the plugin
    gives you the endpoints; the server still has to be started, by ticking
    "Start Web Control Server on Startup" or running `WebControl.StartServer`
    in the console. Half the "Remote Control does not work" in this project's
    history was that distinction.
    """

    name = TRANSPORT_REMOTE

    def __init__(self, url: str, object_path: str, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        self.url = url.rstrip("/")
        self.object_path = object_path

    async def _send(self, call: Call) -> None:
        # urllib is blocking and the editor may be mid-frame. A thread keeps a
        # stalled render out of the event loop that is also pacing the mouth.
        await asyncio.to_thread(self._deliver, call)

    def _deliver(self, call: Call) -> None:
        body = json.dumps(
            {
                "objectPath": self.object_path,
                "functionName": call.function,
                "parameters": call.parameters,
                # A transaction per call fills the editor's undo stack, and an
                # eight-hour stream fills it with sixty thousand nods.
                "generateTransaction": False,
            }
        ).encode("utf-8")
        request = urlrequest.Request(
            f"{self.url}/remote/object/call",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urlrequest.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                response.read()
        except urlerror.HTTPError as exc:
            detail = ""
            with contextlib.suppress(Exception):
                detail = exc.read().decode("utf-8", "replace")[:200]
            raise RuntimeError(f"HTTP {exc.code} on {call.function}: {detail}") from exc
        self.calls_sent += 1
        self.reachable = True

    def ping(self) -> bool:
        try:
            self.info()
        except (urlerror.URLError, OSError, ValueError):
            return False
        return True

    def info(self) -> dict[str, Any]:
        """GET /remote/info -- what engine is answering."""
        with urlrequest.urlopen(f"{self.url}/remote/info", timeout=TIMEOUT_SECONDS) as r:
            return json.loads(r.read().decode("utf-8"))

    def describe(self) -> dict[str, Any]:
        """What the presenter actor actually exposes, functions and all."""
        body = json.dumps({"objectPath": self.object_path}).encode("utf-8")
        request = urlrequest.Request(
            f"{self.url}/remote/object/describe",
            data=body,
            headers={"Content-Type": "application/json"},
            method="PUT",
        )
        with urlrequest.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))


class OscTransport(UnrealTransport):
    """OSC over UDP to an OSC Server node in the level Blueprint.

    No object path to copy and no web server to start, which is the whole
    appeal. In exchange there is no reply: a call that vanishes into a closed
    editor, a wrong port or a firewall looks exactly like one that worked, and
    `stats()["sent"]` here means "the kernel accepted the datagram" rather
    than "Unreal ran the function". `ping` says so by refusing to answer True.
    """

    name = TRANSPORT_OSC

    def __init__(self, host: str, port: int, *, enabled: bool = True) -> None:
        super().__init__(enabled=enabled)
        self.host = host
        self.port = int(port)
        self._socket: socket.socket | None = None

    def _open(self) -> socket.socket | None:
        if self._socket is not None:
            return self._socket
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setblocking(False)
            self._socket = sock
        except OSError as exc:
            self._note(f"{exc.__class__.__name__}: {exc}")
        return self._socket

    def _deliver(self, call: Call) -> None:
        sock = self._open()
        if sock is None:
            self.calls_dropped += 1
            return
        packet = osc_message(OSC_ADDRESS[call.function], call.arguments())
        sock.sendto(packet, (self.host, self.port))
        self.calls_sent += 1
        self.reachable = True

    def ping(self) -> bool:
        # Deliberately always False. UDP does not answer, and a check tool
        # that printed a tick here would be inventing one.
        return False

    async def stop(self) -> None:
        await super().stop()
        sock, self._socket = self._socket, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


def build_transport(cfg: Any, *, enabled: bool = True) -> UnrealTransport:
    """The transport `[character.unreal] transport` asks for.

    An empty `object_path` under `remote_control` is not an error and not a
    crash: it is an operator who has not been into the editor yet. They get a
    NullTransport, a line in the log telling them where the path comes from,
    and a face that still blinks.
    """
    unreal = cfg.character.unreal
    if not enabled or not cfg.character.enabled or unreal.transport == TRANSPORT_NONE:
        return NullTransport()
    if unreal.transport == TRANSPORT_OSC:
        return OscTransport(unreal.osc_host, unreal.osc_port)
    if not unreal.object_path:
        log.warning(
            "[character.unreal] object_path is empty, so Unreal control is off "
            "and the face will run without a body. Right-click the presenter "
            "actor -> Copy Reference, paste it into config.toml, and check it "
            "with `python -m tools.unreal_check`. See UNREAL_SETUP.md."
        )
        return NullTransport()
    return RemoteControlTransport(unreal.remote_control_url, unreal.object_path)


# ---------------------------------------------------------------------------
# OSC, in the only forty lines of it this needs
# ---------------------------------------------------------------------------
def _pad(data: bytes) -> bytes:
    """Align to the next multiple of four with nulls. The caller supplies the
    terminating null; this only pads.

    `-n % 4` and not `4 - n % 4`. The second adds a whole spurious word when
    the data is already aligned, which shifts every argument after it. That
    failure is close to invisible: Unreal's OSC Server node receives a message
    it reads as having no arguments and fires the event with an empty string,
    so a gesture silently plays nothing and no error appears anywhere. Worse,
    it depends on the length of the address, so `/character/gesture` and
    `/character/stage` round-trip perfectly while `/character/mood` and
    `/character/speaking` do not.
    """
    return data + b"\0" * (-len(data) % 4)


def osc_message(address: str, arguments: list[Any]) -> bytes:
    """One OSC message. Ints, floats, strings and bools only.

    Written out rather than pulled in: python-osc would be a dependency, an
    install step and a version to pin, and this is the entire subset the five
    calls need. Bools travel as ints because Unreal's OSC Server node reads a
    bool pin off an int argument, and the OSC bool types are not worth the
    compatibility risk for one flag.
    """
    tags = ","
    payload = b""
    for value in arguments:
        # bool before int: bool IS an int in Python, and checking int first
        # would work by accident here and break the moment a tag mattered.
        if isinstance(value, bool):
            tags += "i"
            payload += struct.pack(">i", 1 if value else 0)
        elif isinstance(value, int):
            tags += "i"
            payload += struct.pack(">i", value)
        elif isinstance(value, float):
            tags += "f"
            payload += struct.pack(">f", value)
        else:
            tags += "s"
            payload += _pad(str(value).encode("utf-8") + b"\0")
    head = _pad(address.encode("utf-8") + b"\0")
    return head + _pad(tags.encode("utf-8") + b"\0") + payload


def osc_parse(packet: bytes) -> tuple[str, list[Any]]:
    """The inverse, so the tests can read back what went on the wire.

    Kept beside the encoder on purpose. The padding bug above was found by
    round-tripping, and would not have been found by reading either function.
    """

    def take(data: bytes, at: int) -> tuple[str, int]:
        end = data.index(b"\0", at)
        text = data[at:end].decode("utf-8")
        return text, at + (len(text) // 4 + 1) * 4

    address, at = take(packet, 0)
    tags, at = take(packet, at)
    arguments: list[Any] = []
    for tag in tags[1:]:
        if tag == "i":
            arguments.append(struct.unpack_from(">i", packet, at)[0])
            at += 4
        elif tag == "f":
            arguments.append(struct.unpack_from(">f", packet, at)[0])
            at += 4
        elif tag == "s":
            text, at = take(packet, at)
            arguments.append(text)
    return address, arguments
