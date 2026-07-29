"""Measure whether the avatar is actually moving.

    python -m tools.motion_check --seconds 12

"Looks alive" is not a matter of opinion: a still model produces identical
frames. This captures the Warudo window over time and reports how much
actually changes, so enabling breathing or look-at can be verified instead of
believed.

Reported per frame pair:
  motion    mean absolute pixel difference across the whole frame
  head      the same, restricted to the upper-middle box where the face is
  peak      the largest single-pixel change, which is what a blink looks like
"""

from __future__ import annotations

import argparse
import io
import statistics
import time

from narrator.config import load_config, project_root
from narrator.ui.capture import WindowCapture


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--fps", type=float, default=6.0)
    ap.add_argument("--window", default=None)
    args = ap.parse_args()

    from PIL import Image, ImageChops

    cfg = load_config(project_root() / "config.toml")
    grabber = WindowCapture(
        args.window or cfg.webui.avatar_window, width=cfg.webui.avatar_width
    )
    if grabber.find_window() is None:
        raise SystemExit("no Warudo window found -- is it running?")

    def shot() -> Image.Image:
        frame = grabber.grab()
        if frame is None:
            raise SystemExit("capture failed")
        return Image.open(io.BytesIO(frame.jpeg)).convert("L")

    previous = shot()
    w, h = previous.size
    # Where the face sits with the standard framing: middle horizontally,
    # upper half vertically.
    face_box = (int(w * 0.30), int(h * 0.05), int(w * 0.70), int(h * 0.60))

    whole: list[float] = []
    faces: list[float] = []
    peaks: list[int] = []
    interval = 1.0 / args.fps
    samples = int(args.seconds * args.fps)

    print(f"sampling {samples} frames over {args.seconds:g}s ...\n")
    print(f"{'t':>6}  {'motion':>8}  {'head':>8}  {'peak':>6}")
    for index in range(samples):
        time.sleep(interval)
        current = shot()
        difference = ImageChops.difference(previous, current)
        stats = difference.getbbox()
        pixels = list(difference.getdata())
        mean_all = sum(pixels) / len(pixels)
        face = list(difference.crop(face_box).getdata())
        mean_face = sum(face) / len(face)
        peak = max(pixels)
        whole.append(mean_all)
        faces.append(mean_face)
        peaks.append(peak)
        moved = "" if stats else "   (identical frame)"
        print(
            f"{index * interval:6.1f}  {mean_all:8.3f}  {mean_face:8.3f}  {peak:6d}{moved}"
        )
        previous = current

    print()
    print(f"  motion   mean {statistics.fmean(whole):.3f}   max {max(whole):.3f}")
    print(f"  head     mean {statistics.fmean(faces):.3f}   max {max(faces):.3f}")
    print(f"  peak     mean {statistics.fmean(peaks):.0f}   max {max(peaks)}")
    still = sum(1 for value in whole if value < 0.05)
    print(f"  still frames: {still} of {len(whole)}")
    print()
    if statistics.fmean(whole) < 0.05:
        print("  VERDICT: static. Nothing is moving -- run tools.liven_avatar.")
    elif statistics.fmean(faces) < 0.05:
        print("  VERDICT: the body moves but the face does not.")
    else:
        print("  VERDICT: alive. Both the body and the face are moving.")


if __name__ == "__main__":
    main()
