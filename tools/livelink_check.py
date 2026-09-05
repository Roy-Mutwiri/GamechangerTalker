"""First light for the Unreal MetaHuman: does the face receive anything at all.

    python -m tools.livelink_check --blink        # idle only -- start here
    python -m tools.livelink_check --pulse        # for measuring A/V offset
    python -m tools.livelink_check --nod
    python -m tools.livelink_check --mood happy
    python -m tools.livelink_check --laugh

`--blink` is the one to run first, before anything else in this work package
is wired up. If the MetaHuman blinks and drifts, the whole chain is proven:
the encoder, the socket, the firewall, the Live Link source, the subject name
and the ARKit mapping on the face. If it does not, exactly one of those is
wrong and the others need not be investigated.

`--pulse` is for the number nobody can guess: how far the face is from the
sound. It opens the jaw fully once a second and prints a line at the same
instant, so an OBS recording of the screen and the console gives the offset in
frames. See UNREAL_SETUP.md.

Nothing here can tell you Unreal received a frame. UDP has no acknowledgement,
so a successful run against a machine with Unreal closed looks identical to a
successful run against one with Unreal open. The counts are what was *sent*.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

from narrator.avatar import livelink
from narrator.config import load_config, project_root


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="python -m tools.livelink_check", description=__doc__
    )
    p.add_argument("--blink", action="store_true", help="idle only: the first-light test")
    p.add_argument("--pulse", action="store_true", help="JawOpen 0-1-0 once a second")
    p.add_argument("--nod", action="store_true", help="a nod every two seconds")
    p.add_argument("--laugh", action="store_true", help="a laugh every three seconds")
    p.add_argument("--mood", help="hold one mood (happy, serious, thinking...)")
    p.add_argument("--subject", help="override the Live Link subject name")
    p.add_argument("--host", help="override the receiver host")
    p.add_argument("--port", type=int, help="override the receiver port")
    p.add_argument("--fps", type=int, help="override the send rate")
    p.add_argument("--seconds", type=float, default=0.0, help="stop after this long")
    p.add_argument("--config", default=str(project_root() / "config.toml"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    character = cfg.character

    subject = args.subject or character.livelink.subject
    host = args.host or character.livelink.host
    port = args.port or character.livelink.port
    fps = args.fps or character.fps

    try:
        offsets = livelink.compile_offsets(character.moods)
    except ValueError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.mood and args.mood not in offsets:
        print(
            f"unknown mood {args.mood!r}; known: {', '.join(sorted(offsets))}",
            file=sys.stderr,
        )
        return 2

    sender = livelink.LiveLinkFaceSender(host, port, (subject,), fps)
    if not sender.open():
        print(f"cannot open a UDP socket: {sender.last_error}", file=sys.stderr)
        return 1

    compositor = livelink.Compositor(seed=character.seed, offsets=offsets)
    print(f"sending to {host}:{port} as subject {subject!r} at {fps} fps")
    print("  UDP has no acknowledgement: these are frames SENT, not received.")
    if args.mood:
        print(f"  holding mood {args.mood!r}")
    print("  Ctrl-C to stop.\n")

    started = time.perf_counter()
    next_beat = started + 2.0
    next_report = started + 1.0
    interval = 1.0 / max(1, fps)
    deadline = started

    try:
        while True:
            now = time.perf_counter()
            elapsed = now - started
            if args.seconds and elapsed >= args.seconds:
                break

            if args.mood:
                # Re-set every tick so it never decays; this is a held pose to
                # look at, not a line being delivered.
                compositor.mood.set(args.mood, now, hold=10.0)
            if (args.nod or args.laugh) and now >= next_beat:
                compositor.beats.trigger("laugh" if args.laugh else "nod", now)
                next_beat = now + (3.0 if args.laugh else 2.0)

            frame = compositor.compose(now)
            if args.pulse:
                # Overwritten after composition on purpose: the pulse is a
                # measurement signal, not an expression, and it has to be
                # exactly a square-edged ramp for the frame count to mean
                # anything.
                phase = elapsed % 1.0
                frame[livelink.INDEX["JawOpen"]] = (
                    math.sin(math.pi * phase / 0.4) if phase < 0.4 else 0.0
                )
                if phase < interval:
                    print(f"  pulse at {elapsed:8.3f}s")

            # send_all, not send: the frame index is the timeline Unreal
            # interpolates along, and it advances once per tick.
            sender.send_all({subject: frame})

            if now >= next_report and not args.pulse:
                stats = sender.stats()
                print(
                    f"  {elapsed:6.1f}s  sent {stats['sent']:>6}  "
                    f"dropped {stats['dropped']}  errors {stats['errors']}"
                    + (f"  [{sender.last_error}]" if sender.last_error else "")
                )
                next_report = now + 1.0

            deadline = max(deadline + interval, now)
            wait = deadline - time.perf_counter()
            if wait > 0:
                time.sleep(wait)
    except KeyboardInterrupt:
        print()
    finally:
        sender.close()

    stats = sender.stats()
    print(
        f"\n{stats['sent']} frames sent, {stats['dropped']} dropped, {stats['errors']} errors"
    )
    if stats["errors"]:
        print(f"last error: {sender.last_error}", file=sys.stderr)
        return 1
    if not stats["sent"]:
        print("nothing was sent at all", file=sys.stderr)
        return 1
    print("\nIf the MetaHuman did not move, the problem is downstream of here:")
    print("  * Apple ARKit Face Support enabled, and Unreal restarted after")
    print(f"  * a Live Link subject named exactly {subject!r}")
    print(f"  * UDP {port} inbound allowed for the Unreal Editor (and, separately,")
    print("    for a packaged build -- Windows firewall treats them as two apps)")
    print("  See UNREAL_SETUP.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
