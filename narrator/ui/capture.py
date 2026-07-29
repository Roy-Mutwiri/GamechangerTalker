"""Capture the Warudo render window and stream it into the browser UI.

Warudo draws the avatar in its own window. Rather than make the operator
watch two windows, we grab that window and push JPEG frames down the same
websocket the dashboard already uses.

Two capture paths, in order of preference:

  PrintWindow  -- asks Windows to render the window into a bitmap. Works even
                  when the window is behind another one, which matters when
                  the browser is on top of it. Unity apps sometimes refuse and
                  return a black frame, which is why there is a fallback.
  screen grab  -- reads the pixels off the desktop at the window's rectangle.
                  Always works, but only for what is actually visible, so an
                  occluded window captures whatever is covering it.

The grabber runs on its own thread: this is CPU work and must never land on
the event loop that is driving 60fps visemes.

Measured cost per frame at 853x480 -> 640 wide (see tools/bench.py):

    PrintWindow + DIB readback   16.6 ms      84% of the total
    resize (bilinear)             1.7 ms
    blank-frame check             0.3 ms
    JPEG encode, quality 62       0.3 ms
                                 -------
                                 18.9 ms      ~28% of one core at 15fps

The readback dominates, and it is Warudo re-rendering the window on demand,
so it cannot be optimised from this side. **Frame rate is the only real
lever** -- quality and width change the bandwidth (17 KB/frame at q62) but
barely touch the CPU, because the encode is already a rounding error.
"""

from __future__ import annotations

import ctypes
import logging
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

PW_RENDERFULLCONTENT = 0x00000002
SRCCOPY = 0x00CC0020


@dataclass
class Frame:
    jpeg: bytes
    width: int
    height: int


class WindowCapture:
    """Finds a window by title and produces JPEG frames from it."""

    def __init__(
        self,
        title_contains: str,
        *,
        fps: int = 15,
        width: int = 640,
        quality: int = 62,
        exclude: tuple[str, ...] = ("Editor",),
    ) -> None:
        self.title_contains = title_contains
        self.exclude = exclude
        self.fps = max(1, fps)
        self.width = width
        self.quality = quality
        self.hwnd: int | None = None
        self.method = "none"
        self.frames = 0
        self.failures = 0
        self.last_error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sct: Any = None

    # -- finding the window -------------------------------------------------

    def find_window(self) -> int | None:
        """The visible top-level window whose title matches, editor excluded."""
        user32 = ctypes.windll.user32
        matches: list[tuple[int, str]] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def each(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
            if self.title_contains.lower() in title.lower():
                if not any(word.lower() in title.lower() for word in self.exclude):
                    matches.append((hwnd, title))
            return True

        user32.EnumWindows(each, 0)
        if not matches:
            return None
        # Prefer the largest, which is the render window rather than a dialog.
        matches.sort(key=lambda pair: -self._area(pair[0]))
        self.hwnd = matches[0][0]
        log.info("capturing window %r (hwnd %s)", matches[0][1], self.hwnd)
        return self.hwnd

    def _rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """Client area in screen coordinates: (x, y, width, height)."""
        rect = wintypes.RECT()
        ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect))
        point = wintypes.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(point))
        return point.x, point.y, rect.right, rect.bottom

    def _window_rect(self, hwnd: int) -> tuple[int, int, int, int]:
        """Whole window including frame, which is what PrintWindow draws."""
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def _area(self, hwnd: int) -> int:
        _, _, w, h = self._rect(hwnd)
        return w * h

    # -- grabbing -----------------------------------------------------------

    def _grab_printwindow(self, hwnd: int) -> Any:
        """Render the window into a bitmap, occluded or not."""
        from PIL import Image

        user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        # PrintWindow draws the whole window, title bar and all, so capture at
        # window size and crop back to the client area afterwards.
        win_x, win_y, width, height = self._window_rect(hwnd)
        client_x, client_y, client_w, client_h = self._rect(hwnd)
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetDC(hwnd)
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        gdi32.SelectObject(memory_dc, bitmap)
        try:
            ok = user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)
            if not ok:
                return None
            buffer = ctypes.create_string_buffer(width * height * 4)

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", wintypes.DWORD),
                    ("biWidth", wintypes.LONG),
                    ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD),
                    ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD),
                    ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG),
                    ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD),
                ]

            header = BITMAPINFOHEADER()
            header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            header.biWidth = width
            header.biHeight = -height  # top-down
            header.biPlanes = 1
            header.biBitCount = 32
            header.biCompression = 0
            gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(header), 0)
            image = Image.frombuffer(
                "RGB", (width, height), buffer.raw, "raw", "BGRX", 0, 1
            )
            # Drop the frame and title bar: the avatar is the point, not
            # Windows chrome.
            offset_x, offset_y = client_x - win_x, client_y - win_y
            if client_w > 0 and client_h > 0:
                image = image.crop(
                    (offset_x, offset_y, offset_x + client_w, offset_y + client_h)
                )
            return image
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)

    def _grab_screen(self, hwnd: int) -> Any:
        from PIL import Image

        if self._sct is None:
            import mss

            self._sct = mss.mss()
        x, y, width, height = self._rect(hwnd)
        if width <= 0 or height <= 0:
            return None
        shot = self._sct.grab({"left": x, "top": y, "width": width, "height": height})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")

    def grab(self) -> Frame | None:
        if self.hwnd is None or not ctypes.windll.user32.IsWindow(self.hwnd):
            if self.find_window() is None:
                self.method = "none"
                return None
        hwnd = self.hwnd
        if hwnd is None:
            return None

        image = None
        try:
            image = self._grab_printwindow(hwnd)
            if image is not None and _is_blank(image):
                image = None  # Unity handed back an empty surface
            if image is not None:
                self.method = "printwindow"
        except Exception as exc:
            self.last_error = f"printwindow: {exc}"

        if image is None:
            try:
                image = self._grab_screen(hwnd)
                self.method = "screen"
            except Exception as exc:
                self.last_error = f"screen: {exc}"
                self.failures += 1
                return None

        if image is None:
            return None

        if image.width > self.width:
            from PIL import Image

            height = round(image.height * self.width / image.width)
            # BILINEAR, not the default BICUBIC: measured 1.74ms against
            # 2.58ms, and on a downscaled 3D render the difference is not
            # visible. NEAREST is cheaper again but visibly aliased.
            image = image.resize((self.width, height), Image.Resampling.BILINEAR)

        import io

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=self.quality)
        self.frames += 1
        return Frame(buffer.getvalue(), image.width, image.height)

    # -- the loop -----------------------------------------------------------

    def start(self, on_frame: Callable[[Frame], None]) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop, args=(on_frame,), daemon=True, name="warudo-capture"
        )
        self._thread.start()

    def _loop(self, on_frame: Callable[[Frame], None]) -> None:
        interval = 1.0 / self.fps
        while not self._stop.is_set():
            started = time.perf_counter()
            try:
                frame = self.grab()
                if frame is not None:
                    on_frame(frame)
            except Exception:
                log.exception("capture loop error")
                self.failures += 1
            elapsed = time.perf_counter() - started
            self._stop.wait(max(0.005, interval - elapsed))

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> str:
        if self.hwnd is None:
            return "no window"
        return f"{self.method} {self.frames}f"


def _is_blank(image: Any) -> bool:
    """PrintWindow on a Unity window sometimes returns a uniform surface."""
    extrema = image.convert("L").getextrema()
    return extrema[0] == extrema[1]
