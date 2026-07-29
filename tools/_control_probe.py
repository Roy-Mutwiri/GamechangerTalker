"""Throwaway: does a keystroke actually move the chart, and does focus return?"""

import ctypes
import io
import time

from PIL import Image

from narrator.config import project_root
from narrator.market.chart import find_window
from narrator.market.chart_control import ChartControl
from narrator.ui.capture import WindowCapture

user32 = ctypes.windll.user32


def shot(hwnd, name):
    grabber = WindowCapture("", width=1600)
    grabber.hwnd = hwnd
    frame = grabber.grab()
    if frame is None:
        raise SystemExit("capture failed")
    image = Image.open(io.BytesIO(frame.jpeg))
    out = project_root() / "logs" / name
    image.save(out)
    # The timeframe sits in the toolbar, upper left.
    return image.crop((180, 55, 460, 95))


hwnd = find_window()
print(f"chart window: {hwnd}")
before_focus = user32.GetForegroundWindow()
print(f"foreground before: {before_focus}")

toolbar_before = shot(hwnd, "tv-before.png")
toolbar_before.save(project_root() / "logs" / "tv-toolbar-before.png")

control = ChartControl(min_gap_seconds=0.0)
action = control.do("m5")
print(f"action: {action}")
print(f"status: {control.status()}  last_error={control.last_error!r}")

time.sleep(1.5)
after_focus = user32.GetForegroundWindow()
print(f"foreground after : {after_focus}  (returned: {after_focus == before_focus})")

toolbar_after = shot(hwnd, "tv-after.png")
toolbar_after.save(project_root() / "logs" / "tv-toolbar-after.png")

same = list(toolbar_before.getdata()) == list(toolbar_after.getdata())
print(f"toolbar unchanged: {same}  (False means the timeframe moved)")
