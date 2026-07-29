"""Drive the operator's TradingView chart.

The hosts can ask for the chart to move -- a different timeframe, a scroll back
through the session, a zoom -- and this is what carries that out. It turns the
chart from a backdrop into something the conversation happens *to*: "pull up the
four-hour" is worth saying only if the four-hour then appears.

FOCUS, AND WHY IT IS BORROWED RATHER THAN TAKEN
------------------------------------------------
TradingView's desktop app is Chromium, and Chromium ignores synthetic key
messages posted to a window that is not focused. So a keystroke means really
focusing the window -- on a machine where the operator may be reading a chart,
typing, or in a broker terminal.

So focus is borrowed: remember what had it, activate TradingView, send one
keystroke, give focus straight back. The window is in front for a few tens of
milliseconds. That is not nothing, and it is the price of driving a real
desktop app; the alternative is a second window nobody is using, which the
operator declined.

There are three further guards, all of them because this types into a live
application:

  * **Nothing runs while the operator is typing elsewhere.** If the foreground
    window belongs to another process and has a text caret, the action is
    skipped rather than queued.
  * **One action at a time, with a floor between them.** A burst of keystrokes
    into a chart is how a layout gets mangled.
  * **Nothing destructive is in the vocabulary.** No saving, no deleting, no
    drawing, no order entry. Timeframe, scroll, zoom, reset -- all of them
    things a wrong keystroke makes untidy rather than expensive.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import time
from ctypes import wintypes
from dataclasses import dataclass

from narrator.market.chart import find_window

log = logging.getLogger(__name__)

user32 = ctypes.windll.user32

# Virtual-key codes for the few keys this needs.
VK = {
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "d": 0x44, "w": 0x57, "h": 0x48, "m": 0x4D, "r": 0x52,
    "enter": 0x0D, "left": 0x25, "right": 0x27, "up": 0x26, "down": 0x28,
    "plus": 0xBB, "minus": 0xBD, "alt": 0x12, "escape": 0x1B,
}

KEYEVENTF_KEYUP = 0x0002


@dataclass(frozen=True)
class Action:
    """One thing that can be done to the chart, and how to say it."""

    name: str
    keys: tuple[str, ...]
    says: str  # what the hosts are told just happened


# TradingView takes a timeframe by typing it and pressing enter -- "15" then
# enter is fifteen minutes, "1h" is an hour, "d" is daily. Typing into the
# chart is how the application is designed to be driven, which is why this
# needs no menus and no mouse.
ACTIONS: dict[str, Action] = {
    "m1": Action("m1", ("1", "enter"), "the one-minute"),
    "m5": Action("m5", ("5", "enter"), "the five-minute"),
    "m15": Action("m15", ("1", "5", "enter"), "the fifteen-minute"),
    "h1": Action("h1", ("1", "h", "enter"), "the hourly"),
    "h4": Action("h4", ("4", "h", "enter"), "the four-hour"),
    "d1": Action("d1", ("d", "enter"), "the daily"),
    "back": Action("back", ("left", "left", "left", "left", "left"), "scrolled back"),
    "forward": Action("forward", ("right", "right", "right"), "scrolled forward"),
    "zoom_in": Action("zoom_in", ("plus",), "zoomed in"),
    "zoom_out": Action("zoom_out", ("minus",), "zoomed out"),
    "reset": Action("reset", ("alt+r",), "reset the chart"),
}

TIMEFRAMES = ("m1", "m5", "m15", "h1", "h4", "d1")


def _caret_in_foreground(own_hwnd: int) -> bool:
    """Is someone typing into another application right now?

    GUITHREADINFO reports a caret for the foreground thread. A text cursor in a
    window that is not the chart means the operator is mid-sentence somewhere,
    and stealing focus would drop their keystrokes into the wrong place.
    """

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    foreground = user32.GetForegroundWindow()
    if not foreground or foreground == own_hwnd:
        return False
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    thread = user32.GetWindowThreadProcessId(foreground, None)
    if not user32.GetGUIThreadInfo(thread, ctypes.byref(info)):
        return False
    return bool(info.hwndCaret)


def _tap(vk: int, down: bool) -> None:
    user32.keybd_event(vk, 0, 0 if down else KEYEVENTF_KEYUP, 0)


def _send(keys: tuple[str, ...]) -> None:
    for key in keys:
        if key.startswith("alt+"):
            _tap(VK["alt"], True)
            _tap(VK[key[4:]], True)
            _tap(VK[key[4:]], False)
            _tap(VK["alt"], False)
        else:
            code = VK.get(key)
            if code is None:
                continue
            _tap(code, True)
            _tap(code, False)
        # TradingView's quick-entry box needs the characters to arrive as
        # keystrokes, not as a burst; without this the digits of "15" can be
        # swallowed and the chart jumps to the one-minute.
        time.sleep(0.04)


class ChartControl:
    """Sends one chart action at a time, and only when it is safe to."""

    def __init__(self, *, min_gap_seconds: float = 20.0, enabled: bool = True) -> None:
        self.enabled = enabled
        self.min_gap_seconds = min_gap_seconds
        self.timeframe = ""
        self.actions_sent = 0
        self.skipped = 0
        self.last_error = ""
        self._last_at = 0.0

    def ready(self, now: float | None = None) -> bool:
        now = now if now is not None else time.monotonic()
        return self.enabled and (now - self._last_at) >= self.min_gap_seconds

    def do(self, name: str) -> Action | None:
        """Perform an action. Returns what happened, or None if it did not.

        Never raises: a chart that refuses to move is a cosmetic failure and
        the conversation carries on without it.
        """
        action = ACTIONS.get(name)
        if action is None or not self.enabled or not self.ready():
            return None

        hwnd = find_window()
        if hwnd is None:
            self.last_error = "no TradingView window"
            return None

        if _caret_in_foreground(hwnd):
            # Someone is typing. Their keystrokes matter more than ours.
            self.skipped += 1
            self.last_error = "operator is typing elsewhere"
            return None

        previous = user32.GetForegroundWindow()
        try:
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.12)  # Chromium needs a beat to accept the focus
            if user32.GetForegroundWindow() != hwnd:
                self.last_error = "could not focus the chart"
                self.skipped += 1
                return None
            _send(action.keys)
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            return None
        finally:
            # Hand focus back whatever happened, including on the failure paths
            # above -- leaving the chart in front of whatever the operator was
            # doing is the rudest possible outcome.
            if previous and previous != hwnd:
                with contextlib.suppress(Exception):
                    user32.SetForegroundWindow(previous)

        self._last_at = time.monotonic()
        self.actions_sent += 1
        if name in TIMEFRAMES:
            self.timeframe = name
        log.info("chart: %s (%s)", action.name, action.says)
        return action

    def status(self) -> str:
        if not self.enabled:
            return "off"
        if self.last_error and not self.actions_sent:
            return f"idle ({self.last_error})"
        current = f" on {self.timeframe}" if self.timeframe else ""
        return f"ok ({self.actions_sent} moves{current})"
