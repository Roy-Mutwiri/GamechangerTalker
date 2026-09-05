"""Capture a gesture off the Live Link Face app and keep it as a clip.

    python -m tools.record_clip nod
    python -m tools.record_clip laugh --port 11112 --seconds 20
    python -m tools.record_clip nod --list        # what is already recorded

Every beat already has a procedural clip, so nothing here is required. It
exists because a nod performed by a person is better than a nod described by a
sine wave, and thirty seconds with a phone is a cheaper way to get one than an
afternoon tuning curves.

Point the Live Link Face app at this machine, press record here, perform the
gesture, stop. The clip is trimmed to the part where the face was actually
moving, so "press record, wait, nod, wait, stop" leaves a clean clip without
anybody editing anything.

**Close Unreal first, or pass --port.** Two processes cannot bind the same UDP
port, and Unreal will already be sitting on 11111 if it is running.

`avatar/face.py` picks up `clips/<beat>.csv` for any beat name automatically.
The file format is the Live Link Face app's own CSV export, so a clip recorded
on the phone and a clip recorded here are the same thing.
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

import numpy as np

from narrator.avatar.livelink import CHANNEL_COUNT, LIVELINK_NAMES, decode_frame
from narrator.config import load_config, project_root
from narrator.script.expression import BEATS

#: Below this, a channel counts as "not moving". Chosen from the noise floor
#: of a phone sitting still on a desk, which is around 0.005 on the eyelids.
MOVEMENT = 0.02
#: Frames of stillness kept either side of the movement, so a clip does not
#: begin mid-gesture.
PADDING_FRAMES = 6


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m tools.record_clip", description=__doc__)
    p.add_argument("name", nargs="?", help="the beat this clip is for, e.g. nod")
    p.add_argument("--port", type=int, default=11111)
    p.add_argument(
        "--host", default="0.0.0.0", help="bind address; the phone is not on loopback"
    )
    p.add_argument("--seconds", type=float, default=15.0, help="how long to listen")
    p.add_argument("--fps", type=int, default=60, help="resample the clip to this")
    p.add_argument("--no-trim", action="store_true", help="keep the still parts")
    p.add_argument("--list", action="store_true", help="list recorded clips and exit")
    p.add_argument("--config", default=str(project_root() / "config.toml"))
    return p.parse_args(argv)


def capture(host: str, port: int, seconds: float) -> tuple[np.ndarray, str, float]:
    """Listen, and return (frames, subject, measured fps)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind((host, port))
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f"cannot listen on {host}:{port} ({exc}).\n"
            "Unreal is probably already bound to it -- close Unreal, or "
            "record on another port with --port and set the Live Link Face "
            "app to match."
        ) from exc

    sock.settimeout(0.5)
    rows: list[np.ndarray] = []
    subject = ""
    started = 0.0
    last = 0.0
    deadline = time.monotonic() + seconds
    print(f"listening on {host}:{port} for {seconds:.0f}s -- perform the gesture now")
    try:
        while time.monotonic() < deadline:
            try:
                data, _ = sock.recvfrom(4096)
            except TimeoutError:
                continue
            frame = decode_frame(data)
            if frame is None:
                continue
            if not rows:
                subject = frame.subject
                started = time.monotonic()
                print(f"  receiving from subject {subject!r}")
            last = time.monotonic()
            rows.append(frame.values)
            if len(rows) % 60 == 0:
                print(f"  {len(rows)} frames", end="\r", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        sock.close()

    if not rows:
        return np.zeros((0, CHANNEL_COUNT), dtype=np.float32), "", 0.0
    span = max(1e-6, last - started)
    return np.array(rows, dtype=np.float32), subject, len(rows) / span


def trim(frames: np.ndarray) -> np.ndarray:
    """Cut down to the part where the face was actually doing something.

    Measured against the FIRST frame rather than against zero: a recording
    starts with whatever expression the performer's face was resting in, and
    that resting face is the clip's zero, not a neutral mesh.
    """
    if len(frames) < 3:
        return frames
    delta = np.abs(frames - frames[0]).max(axis=1)
    moving = np.flatnonzero(delta > MOVEMENT)
    if moving.size == 0:
        return frames
    first = max(0, int(moving[0]) - PADDING_FRAMES)
    last = min(len(frames), int(moving[-1]) + PADDING_FRAMES + 1)
    return frames[first:last]


def resample(frames: np.ndarray, source_fps: float, target_fps: int) -> np.ndarray:
    """Linear, per channel. The phone records at 60; a laptop under load does not."""
    if len(frames) < 2 or source_fps <= 0 or abs(source_fps - target_fps) < 1.0:
        return frames
    duration = len(frames) / source_fps
    count = max(2, round(duration * target_fps))
    source = np.linspace(0.0, 1.0, len(frames))
    target = np.linspace(0.0, 1.0, count)
    out = np.empty((count, frames.shape[1]), dtype=np.float32)
    for channel in range(frames.shape[1]):
        out[:, channel] = np.interp(target, source, frames[:, channel])
    return out


def write_csv(path: Path, frames: np.ndarray, fps: int) -> None:
    """The Live Link Face app's own export format, so the two are the same thing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = ["Timecode", "BlendshapeCount", *LIVELINK_NAMES]
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(",".join(header) + "\n")
        for index, row in enumerate(frames):
            seconds = index / fps
            timecode = (
                f"{int(seconds // 3600):02d}:{int(seconds // 60) % 60:02d}:"
                f"{int(seconds) % 60:02d}:{int(index % fps):02d}"
            )
            values = ",".join(f"{value:.6f}" for value in row)
            fh.write(f"{timecode},{CHANNEL_COUNT},{values}\n")


def describe(frames: np.ndarray) -> list[tuple[str, float]]:
    """The channels that moved most, so the operator can see what was caught."""
    if len(frames) < 2:
        return []
    travel = np.abs(frames - frames[0]).max(axis=0)
    order = np.argsort(travel)[::-1]
    return [
        (LIVELINK_NAMES[i], float(travel[i])) for i in order[:6] if travel[i] > MOVEMENT
    ]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    clips_dir = cfg.path(cfg.character.clips_dir)

    if args.list:
        existing = sorted(clips_dir.glob("*.csv")) if clips_dir.exists() else []
        print(f"clips in {clips_dir}:")
        for path in existing:
            rows = sum(1 for _ in path.open(encoding="utf-8")) - 1
            used = " (a beat)" if path.stem in BEATS else ""
            print(f"  {path.stem:<14} {rows:>5} frames{used}")
        if not existing:
            print("  none -- every beat is using its procedural clip")
        return 0

    if not args.name:
        print(
            "give the clip a name, e.g. `python -m tools.record_clip nod`",
            file=sys.stderr,
        )
        return 2
    if args.name not in BEATS:
        print(
            f"note: {args.name!r} is not a beat the model can write "
            f"({', '.join(sorted(BEATS))}), so nothing will play it automatically.",
        )

    frames, subject, measured = capture(args.host, args.port, args.seconds)
    print()
    if not len(frames):
        print(
            "nothing arrived.\n"
            "  * is the Live Link Face app pointed at this machine's IP?\n"
            f"  * is UDP {args.port} allowed inbound?\n"
            "  * is Unreal already bound to that port?",
            file=sys.stderr,
        )
        return 1

    print(f"{len(frames)} frames from {subject!r} at ~{measured:.1f} fps")
    kept = frames if args.no_trim else trim(frames)
    if len(kept) != len(frames):
        print(f"trimmed to {len(kept)} frames where the face was moving")
    kept = resample(kept, measured, args.fps)

    path = clips_dir / f"{args.name}.csv"
    write_csv(path, kept, args.fps)
    print(
        f"wrote {path}  ({len(kept)} frames, {len(kept) / args.fps:.2f}s at {args.fps} fps)"
    )

    moved = describe(kept)
    if moved:
        print("channels that moved most:")
        for name, amount in moved:
            print(f"  {name:<22} {amount:.3f}")
    else:
        print("nothing in that recording moved -- the clip will do nothing")
    return 0


if __name__ == "__main__":
    sys.exit(main())
