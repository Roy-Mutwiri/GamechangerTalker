"""The browser UI.

Serves a single self-contained page on localhost and pushes live state to it
over a websocket. The page shows the dashboard *and* an avatar whose mouth is
driven by the same 60fps viseme stream that goes to Warudo -- so the phoneme
pipeline is visible without Warudo running at all.

This does not replace Warudo. Warudo drives the real VRM that goes on stream;
this is the operator's own window, and a way to see that the visemes are
right. It is also capturable directly if you ever want a browser source
instead of a VRM.

Two ports, both loopback only:
    port      HTTP, serves the page
    port + 1  websocket, state out and operator commands in

Viseme frames are not streamed one at a time. The whole frame list for an
utterance is sent once, with its duration, and the page animates it on its own
requestAnimationFrame clock. Sixty messages a second would be jittery and
pointless when the browser can interpolate locally.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import threading
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from narrator.avatar import roster
from narrator.config import Config

log = logging.getLogger(__name__)

WEB_ROOT = Path(__file__).parent / "web"


class _Handler(SimpleHTTPRequestHandler):
    """Serves narrator/ui/web, quietly."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, fmt: str, *args: Any) -> None:
        log.debug("http %s", fmt % args)

    def end_headers(self) -> None:
        # The page is regenerated constantly during tuning; never cache it.
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class WebUI:
    def __init__(self, cfg: Config, *, run_id: str, symbol: str, mode: str) -> None:
        self.cfg = cfg
        self.run_id = run_id
        self.symbol = symbol
        self.mode = mode
        self.enabled = cfg.webui.enabled
        self.host = cfg.webui.host
        self.port = cfg.webui.port
        self.ws_port = cfg.webui.port + 1
        self.url = f"http://{self.host}:{self.port}/?ws={self.ws_port}"

        self.commands: queue.Queue[str] = queue.Queue()
        # Camera moves from the framing control. Depth 1 on purpose: see
        # _queue_camera -- only the newest framing is worth acting on.
        self.cameras: queue.Queue[dict[str, float]] = queue.Queue()
        self.avatar_picks: queue.Queue[dict[str, Any]] = queue.Queue()
        # Built once at startup: it reads every VRM to measure its face
        # height, which is not something to redo on each page load.
        self.avatars: list[dict[str, Any]] = roster.build(cfg)
        # Filled in by the runner once the conversation layer is built; left
        # empty when it is off or has no key.
        self.hosts: list[dict[str, Any]] = []
        self.hosts_off: str = ""
        # Whether two characters are on stage right now. Sent with every state
        # update so the toggle reflects the truth even if it was flipped from
        # the terminal, or refused because the layer is unavailable.
        self.podcast: bool = False
        # Whether the toggle can be used at all. False with no key, or after
        # the layer has given up -- the button then explains rather than
        # silently doing nothing when clicked.
        self.podcast_usable: bool = False
        self.clients: set[Any] = set()
        self.history: list[dict[str, Any]] = []
        self._http: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pending: set[asyncio.Task] = set()
        self._stop = asyncio.Event()
        self._last_state: dict[str, Any] = {}

    # -- lifecycle ----------------------------------------------------------

    def start_http(self) -> bool:
        if not self.enabled:
            return False
        if not (WEB_ROOT / "index.html").exists():
            log.error("web assets missing at %s", WEB_ROOT)
            self.enabled = False
            return False
        try:
            self._http = ThreadingHTTPServer((self.host, self.port), _Handler)
        except OSError as exc:
            log.error(
                "could not bind the web UI to %s:%s (%s)", self.host, self.port, exc
            )
            self.enabled = False
            return False
        self._thread = threading.Thread(
            target=self._http.serve_forever, daemon=True, name="webui-http"
        )
        self._thread.start()
        log.info("web UI at %s", self.url)
        return True

    async def start_ws(self) -> None:
        if not self.enabled:
            return
        try:
            from websockets.asyncio.server import serve
        except ImportError:  # pragma: no cover - older websockets
            from websockets import serve

        try:
            async with serve(self._client, self.host, self.ws_port):
                await self._stop.wait()
        except OSError as exc:
            log.error("web UI websocket could not bind %s: %s", self.ws_port, exc)
            self.enabled = False

    async def stop(self) -> None:
        self._stop.set()
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None

    def open_browser(self) -> None:
        if not self.enabled or not self.cfg.webui.open_browser:
            return
        import webbrowser

        try:
            webbrowser.open(self.url)
        except Exception as exc:  # pragma: no cover
            log.warning("could not open a browser: %s", exc)

    # -- clients ------------------------------------------------------------

    async def _client(self, connection: Any, path: str | None = None) -> None:
        self.clients.add(connection)
        log.info("web UI client connected (%d total)", len(self.clients))
        try:
            from narrator.speech.engine import VOICES

            await connection.send(
                json.dumps(
                    {
                        "type": "hello",
                        "symbol": self.symbol,
                        "mode": self.mode,
                        "run": self.run_id,
                        "voice": self.cfg.speech.voice,
                        "voices": VOICES,
                        "avatars": self.avatars,
                        # Empty when the conversation layer is off; hostsOff
                        # then carries the reason, so the browser can say what
                        # is missing instead of quietly showing nothing.
                        "hosts": self.hosts,
                        "hostsOff": self.hosts_off,
                        "podcast": self.podcast,
                        "podcastUsable": self.podcast_usable,
                    }
                )
            )
            # A page opened mid-stream should not look empty.
            for message in self.history[-40:]:
                await connection.send(json.dumps(message))
            if self._last_state:
                await connection.send(json.dumps(self._last_state))

            async for raw in connection:
                try:
                    message = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if message.get("type") == "camera":
                    self._queue_camera(message)
                    continue
                if message.get("type") == "avatar":
                    self._queue_avatar(message)
                    continue
                text = str(message.get("text", "")).strip()
                if text:
                    self.commands.put(text)
        except Exception as exc:
            log.debug("web UI client dropped: %s", exc)
        finally:
            self.clients.discard(connection)

    def pop_command(self) -> str | None:
        try:
            return self.commands.get_nowait()
        except queue.Empty:
            return None

    def _queue_camera(self, message: dict[str, Any]) -> None:
        """Latest framing wins.

        A drag produces a move per pointer event, and only the newest one is
        worth anything -- queueing them all would leave the camera crawling
        through a backlog after the operator's hand stopped.
        """
        try:
            move = {
                "yaw": float(message["yaw"]),
                "pitch": float(message["pitch"]),
                "distance": float(message["distance"]),
            }
        except (KeyError, TypeError, ValueError):
            return
        with contextlib.suppress(queue.Empty):
            while True:
                self.cameras.get_nowait()
        self.cameras.put(move)

    def pop_camera(self) -> dict[str, float] | None:
        try:
            return self.cameras.get_nowait()
        except queue.Empty:
            return None

    def _queue_avatar(self, message: dict[str, Any]) -> None:
        """Only offer characters that are actually on the roster.

        The source string goes on to Warudo as a property value, so it is not
        somewhere to pass a browser's word for it through unchecked.
        """
        wanted = str(message.get("file", ""))
        # Which chair this is for: "a" or "b" in podcast mode, empty for the
        # single character. Validated against the personas by the runner --
        # anything else is ignored rather than trusted.
        slot = str(message.get("slot", ""))[:1].lower()
        for entry in self.avatars:
            if entry["file"] == wanted:
                self.avatar_picks.put({**entry, "slot": slot})
                return
        log.warning("avatar %r is not on the roster; ignoring", wanted)

    def pop_avatar(self) -> dict[str, Any] | None:
        try:
            return self.avatar_picks.get_nowait()
        except queue.Empty:
            return None

    # -- sending ------------------------------------------------------------

    def broadcast(self, message: dict[str, Any]) -> None:
        """Fire and forget. A slow or dead client never stalls the narrator.

        The task references are held until they finish: asyncio only keeps a
        weak reference to a running task, so a fire-and-forget send can be
        garbage collected mid-flight and the message silently lost.
        """
        if not self.enabled or not self.clients:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # Called off the event loop (a test, or during shutdown). Checked
            # before building any coroutine, so none is left un-awaited.
            return
        payload = json.dumps(message)
        for connection in list(self.clients):
            task = asyncio.create_task(_send(connection, payload))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def send_state(
        self, facts: dict[str, Any], status: dict[str, str], clock: datetime
    ) -> None:
        message = {
            "type": "state",
            "clock": clock.strftime("%H:%M:%S"),
            "date": clock.strftime("%a %d %b"),
            "facts": _jsonable(facts),
            "status": status,
            "podcast": self.podcast,
            "podcastUsable": self.podcast_usable,
            "hostsOff": self.hosts_off,
            "hosts": self.hosts,
        }
        self._last_state = message
        self.broadcast(message)

    def send_line(
        self,
        when: datetime,
        template_id: str,
        text: str,
        *,
        source: str,
        emote: str | None,
    ) -> None:
        message = {
            "type": "line",
            "time": when.strftime("%H:%M:%S"),
            "id": template_id,
            "text": text,
            "source": source,
            "emote": emote,
        }
        self.history.append(message)
        del self.history[:-200]
        self.broadcast(message)

    def send_utterance(self, text: str, duration: float, frames: list[Any]) -> None:
        """The whole viseme track at once; the page animates it locally."""
        packed = [
            [round(f.t, 3)]
            + [round(f.weights[v], 3) for v in ("aa", "ee", "ih", "oh", "ou")]
            for f in frames
        ]
        self.broadcast(
            {
                "type": "utterance",
                "text": text,
                "duration": round(duration, 3),
                "frames": packed,
            }
        )

    def send_emote(self, name: str, hold: float, reason: str = "") -> None:
        self.broadcast({"type": "emote", "name": name, "hold": hold, "reason": reason})

    def send_note(self, text: str) -> None:
        self.broadcast({"type": "note", "text": text})

    def send_avatar_frame(self, jpeg: bytes, width: int, height: int) -> None:
        """One captured frame of Warudo's render window.

        Sent as binary, not base64: a JPEG survives the websocket intact and
        base64 would add a third to every frame for nothing.
        """
        if not self.enabled or not self.clients:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for connection in list(self.clients):
            task = asyncio.create_task(_send_bytes(connection, jpeg))
            self._pending.add(task)
            task.add_done_callback(self._pending.discard)

    def send_silence(self, reason: str, detail: str) -> None:
        self.broadcast({"type": "silence", "reason": reason, "detail": detail})


async def _send(connection: Any, payload: str) -> None:
    with contextlib.suppress(Exception):
        await connection.send(payload)


async def _send_bytes(connection: Any, payload: bytes) -> None:
    with contextlib.suppress(Exception):
        await connection.send(payload)


def _jsonable(facts: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in facts.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, dict):
            out[key] = {str(k): v for k, v in value.items()}
        else:
            out[key] = value
    return out
