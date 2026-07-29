"""Throwaway: find the TradingView window by its process, then capture it."""

import ctypes
import io
from ctypes import wintypes

from PIL import Image

from narrator.config import project_root
from narrator.ui.capture import WindowCapture

user32 = ctypes.windll.user32
PROCESS = "tradingview.exe"


def pids_named(name: str) -> set[int]:
    """PIDs of a running process, without a psutil dependency."""
    import subprocess

    out = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {name}", "/FO", "CSV", "/NH"],
        capture_output=True,
        text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = [p.strip('" ') for p in line.split('","')]
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def windows_of_process(name: str) -> list[tuple[int, str, int]]:
    """Every visible top-level window owned by a process of that name."""
    wanted = pids_named(name)
    found: list[tuple[int, str, int]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def each(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in wanted:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        rect = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        found.append((hwnd, buffer.value, area))
        return True

    user32.EnumWindows(each, 0)
    return sorted(found, key=lambda w: -w[2])


windows = windows_of_process(PROCESS)
print(f"visible windows owned by {PROCESS}: {len(windows)}")
for hwnd, title, area in windows[:5]:
    print(f"  hwnd={hwnd} area={area} title={title!r}")

if not windows:
    raise SystemExit("no visible TradingView window")

hwnd, title, _ = windows[0]
grabber = WindowCapture("", width=1600)
grabber.hwnd = hwnd
frame = grabber.grab()
if frame is None:
    raise SystemExit("capture returned nothing")

image = Image.open(io.BytesIO(frame.jpeg))
out = project_root() / "logs" / "tradingview.png"
image.save(out)
print(f"\ncaptured {image.size[0]}x{image.size[1]} -> {out}")
