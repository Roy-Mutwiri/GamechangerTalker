"""Let the hosts see the chart the operator is actually looking at.

The narrator knows the market as numbers: a price, an ATR, a distance to a
level. That is precise and it is blind. It cannot see that the last four hours
drew a wedge, that an indicator has painted forty signals across the screen, or
that the operator just dropped a trendline on the four-hour -- and those are the
things a person watching the stream is looking at while the hosts talk.

So this reads the TradingView window and turns it into a paragraph of *shape*:
what kind of picture is on screen right now. The hosts get it as context
alongside the fact set.

WHAT THIS MAY AND MAY NOT SAY
-----------------------------
**No numbers.** Not one. The chart on this machine is an OANDA feed reading
4,017 while the MT5 account reads 4,021 -- the same metal, different brokers,
several dollars apart. A price read off an image is also a price read by an OCR
that can drop a digit. Every number the stream speaks comes from the live feed,
which is checked, timestamped and refused when stale; the picture contributes
structure and nothing else. The prompt below says so four different ways
because a vision model's instinct is to read the axis out to you.

**No signals as instructions.** The operator's chart carries an indicator that
stamps "Buy" and "Sell" across every swing. Describing those as advice is the
one thing this stream must never do, and the guard would drop the turn anyway,
so the model is told to report that an indicator marked a turn without
repeating its verdict as a recommendation.

The window is found by process rather than by title: TradingView titles itself
with the live price ("XAUUSD 4,017.055 -0.29%"), which changes on every tick
and matches nothing stable.
"""

from __future__ import annotations

import asyncio
import base64
import ctypes
import logging
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

PROCESS = "tradingview.exe"

SYSTEM = """\
You are watching a trading chart over someone's shoulder and telling them what \
kind of picture it is. Two people are about to talk about it on a live stream.

Reply with two or three short sentences of plain speech. No lists, no headings, \
no markdown.

WHAT TO SAY
- The shape of it: trending, ranging, coiling, one big move and a drift, a gap.
- Where the current price sits inside that shape: at the top of the range, \
mid-way back, pressed against the edge.
- Anything drawn on it: trendlines, boxes, levels the operator has added, an \
indicator painting markers.
- What is different about the right-hand edge -- the last few bars -- compared \
with the rest of the screen. That is the part that is happening now.

WHAT YOU MUST NOT DO
- NEVER state a number. No prices, no levels, no percentages, no dates, no \
times, not even ones printed clearly on the axis. The stream gets its numbers \
from a live broker feed and yours would contradict it -- this chart is a \
different broker and reads several dollars apart. Say "the top of the range", \
never "4,018".
- NEVER repeat a buy or sell signal as a recommendation. If an indicator has \
marked the chart, say that it has been marking turns and whether it looks early \
or late. Never "it is signalling a buy".
- NEVER guess why anything happened. You can see a chart; you cannot see news.
- If the window is not a chart -- a settings dialog, a blank screen, a browser \
tab -- say exactly that in one sentence and stop.
"""


# The words an indicator writes on a chart, and what to call them instead.
#
# This is not censorship of the description, it is keeping the hosts out of a
# trap. The operator's chart is stamped with "Buy" and "Sell" labels, and a
# vision model reports them faithfully: "multiple buy and sell indicators
# scattered across the chart" -- observed, verbatim, on the first run. That
# text becomes host context, the hosts echo the words, and the advice guard
# then drops the turn for saying "buy". The eyes would be quietly feeding the
# conversation vocabulary that gets it thrown away.
#
# So the marker keeps its meaning and loses the word: nobody is told what to
# do, and nothing downstream trips.
_SCRUB = (
    (re.compile(r"\bbuy(?:ing)?\s+(?:and\s+sell\s+)?signals?\b", re.I), "long-side markers"),
    (re.compile(r"\bsell(?:ing)?\s+signals?\b", re.I), "short-side markers"),
    (re.compile(r"\bbuy\s+and\s+sell\b", re.I), "long-side and short-side"),
    (re.compile(r"\bbuy\b", re.I), "long-side"),
    (re.compile(r"\bsell\b", re.I), "short-side"),
)


def scrub(text: str) -> str:
    """Take the trade-call vocabulary out of a description of a chart."""
    for pattern, replacement in _SCRUB:
        text = pattern.sub(replacement, text)
    return text.strip()


@dataclass(frozen=True)
class ChartView:
    """One look at the chart."""

    text: str
    at: float
    width: int = 0
    height: int = 0

    @property
    def usable(self) -> bool:
        return bool(self.text.strip())


def _pids(name: str) -> set[int]:
    """PIDs for a process name. tasklist rather than psutil: one less dependency
    for a lookup that runs once a minute."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout
    except Exception:
        return set()
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def find_window(process: str = PROCESS) -> int | None:
    """The largest visible top-level window belonging to that process.

    Largest, because TradingView keeps invisible helper windows and the odd
    dialog around; the chart is the big one.
    """
    wanted = _pids(process)
    if not wanted:
        return None

    user32 = ctypes.windll.user32
    found: list[tuple[int, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in wanted:
            return True
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > 10000:  # ignore tooltips and off-screen stubs
            found.append((hwnd, area))
        return True

    user32.EnumWindows(each, 0)
    if not found:
        return None
    found.sort(key=lambda pair: -pair[1])
    return found[0][0]


class ChartEyes:
    """Looks at the chart every so often and remembers what it saw.

    Deliberately slow. A look costs an image through a hosted model, and a
    chart does not change character between one minute and the next -- the
    numbers do, and those come from the feed. Looking every few seconds would
    multiply the bill without changing a word anyone says.

    Every failure is silent and leaves the previous view standing. A blind
    stream is the normal state of this project; the hosts have run without eyes
    since the day they were written.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str = "",
        backend: str = "anthropic",
        ollama_host: str = "http://127.0.0.1:11434",
        every_seconds: float = 90.0,
        width: int = 1280,
        process: str = PROCESS,
    ) -> None:
        self.model = model
        self.api_key = api_key
        # Two ways to describe a picture, and the choice is money against
        # quality. The hosted model reads a chart properly and is metered per
        # image; a local vision model is free and coarser, and on this machine
        # slow -- which is survivable only because a look happens once every
        # ninety seconds rather than once a frame.
        self.backend = backend
        self.ollama_host = ollama_host.rstrip("/")
        self.every_seconds = max(20.0, every_seconds)
        self.width = width
        self.process = process
        self.view: ChartView | None = None
        self.looks = 0
        self.failures = 0
        self.last_error = ""
        self._client: Any = None
        self._looking = False
        self._last_at = 0.0

    # -- capture -------------------------------------------------------------

    def grab(self) -> tuple[bytes, int, int] | None:
        """One JPEG of the chart window, or None if it is not there."""
        from narrator.ui.capture import WindowCapture

        hwnd = find_window(self.process)
        if hwnd is None:
            return None
        grabber = WindowCapture("", width=self.width)
        grabber.hwnd = hwnd
        frame = grabber.grab()
        if frame is None:
            return None
        return frame.jpeg, frame.width, frame.height

    # -- looking -------------------------------------------------------------

    def due(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return not self._looking and (now - self._last_at) >= self.every_seconds

    async def look(self) -> ChartView | None:
        """Capture and describe. Never raises."""
        if self._looking:
            return self.view
        self._looking = True
        self._last_at = time.monotonic()
        try:
            shot = await asyncio.to_thread(self.grab)
            if shot is None:
                self.last_error = "no TradingView window"
                return self.view
            jpeg, width, height = shot
            text = scrub(await self._describe(jpeg))
            if not text:
                return self.view
            self.looks += 1
            self.view = ChartView(text=text, at=time.time(), width=width, height=height)
            log.info("chart: %s", text[:120])
            return self.view
        except Exception as exc:
            self.failures += 1
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            log.warning("chart look failed: %s", self.last_error)
            return self.view
        finally:
            self._looking = False

    async def _describe(self, jpeg: bytes) -> str:
        if self.backend == "ollama":
            return await self._describe_locally(jpeg)
        return await self._describe_hosted(jpeg)

    async def _describe_locally(self, jpeg: bytes) -> str:
        """A vision model on this machine. Free, and slower than it sounds."""
        import httpx

        async with httpx.AsyncClient(base_url=self.ollama_host, timeout=180.0) as http:
            response = await http.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM},
                        {
                            "role": "user",
                            "content": "What kind of picture is this chart right now?",
                            "images": [base64.b64encode(jpeg).decode("ascii")],
                        },
                    ],
                    "stream": False,
                    "keep_alive": "10m",
                    "options": {"temperature": 0.3, "num_predict": 200},
                },
            )
            if response.status_code == 404:
                raise RuntimeError(
                    f"ollama has no model called {self.model!r} — "
                    f"run: ollama pull {self.model}"
                )
            response.raise_for_status()
            return str(response.json().get("message", {}).get("content", "")).strip()

    async def _describe_hosted(self, jpeg: bytes) -> str:
        if self._client is None:
            from anthropic import AsyncAnthropic

            self._client = AsyncAnthropic(api_key=self.api_key)

        message = await self._client.messages.create(
            model=self.model,
            max_tokens=220,
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": base64.b64encode(jpeg).decode("ascii"),
                            },
                        },
                        {
                            "type": "text",
                            "text": "What kind of picture is this chart right now?",
                        },
                    ],
                }
            ],
        )
        parts = [b.text for b in message.content if getattr(b, "type", "") == "text"]
        return " ".join(parts).strip()

    # -- handing it on --------------------------------------------------------

    def context(self, max_age: float = 600.0) -> str:
        """The current view as a block for the hosts, or empty if too old.

        Stale is worse than absent here. A description of a chart from ten
        minutes ago will be confidently wrong about the right-hand edge, which
        is the only part anyone is watching.
        """
        view = self.view
        if view is None or not view.usable:
            return ""
        if time.time() - view.at > max_age:
            return ""
        return (
            "ON THE CHART RIGHT NOW (what it looks like, not what it costs -- "
            "every number you use still comes from MARKET STATE)\n"
            f"  {view.text}"
        )

    def status(self) -> str:
        if self.backend != "ollama" and not self.api_key:
            return "off (no key)"
        if self.view is None:
            return f"blind ({self.last_error})" if self.last_error else "not looked yet"
        age = time.time() - self.view.at
        return f"ok ({self.looks} looks, {age:.0f}s ago)"
