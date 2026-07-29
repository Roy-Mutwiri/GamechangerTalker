"""Prove the Warudo blueprint is really driving the mouth.

    python -m tools.lipsync_check

    python -m tools.lipsync_check --character 2

Sends each viseme to Warudo at full weight, captures the render, and measures
how much the frame changed against a closed mouth. A blueprint that is not
wired up produces identical frames -- so this answers "is the lip sync
working" with a number instead of an opinion.

On a two-host stage each half of the frame is measured separately, because
the interesting failures there are asymmetric: the second character's nodes
silently colliding with the first's (only one mouth moves), or both sets of
nodes pointing at the same character (the wrong mouth moves). One number for
the whole frame cannot tell those apart; two can.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json

from narrator.config import load_config, project_root
from narrator.ui.capture import WindowCapture

VISEMES = ("aa", "ih", "ou", "ee", "oh")


def two_hosts_on_stage() -> bool:
    """Is there a live second character to measure, or is this a solo shot?"""
    from narrator.avatar import duet
    from narrator.avatar import scene as scene_tools

    path = scene_tools.scene_path()
    if path is None:
        return False
    scene = json.loads(path.read_text(encoding="utf-8-sig"))
    second = duet._asset(scene, duet.SECOND_NAME)
    return second is not None and second.get("active") is not False


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--threshold", type=float, default=0.15)
    ap.add_argument(
        "--character",
        type=int,
        default=1,
        choices=(1, 2),
        help="which host to drive: 1 speaks on viseme_, 2 on viseme2_",
    )
    args = ap.parse_args()

    from PIL import Image, ImageChops
    from websockets.asyncio.client import connect

    cfg = load_config(project_root() / "config.toml")
    grabber = WindowCapture(cfg.webui.avatar_window, width=cfg.webui.avatar_width)
    if grabber.find_window() is None:
        raise SystemExit("no Warudo window found")

    def shot() -> Image.Image:
        frame = grabber.grab()
        if frame is None:
            raise SystemExit("capture failed")
        return Image.open(io.BytesIO(frame.jpeg)).convert("L")

    url = f"ws://{cfg.warudo.host}:{cfg.warudo.port}{cfg.warudo.path}"
    base = cfg.warudo.action_prefix
    prefix = base if args.character == 1 else f"{base.rstrip('_')}{args.character}_"
    duet = two_hosts_on_stage()
    if args.character == 2 and not duet:
        raise SystemExit(
            "no second character on stage; turn podcast mode on before checking her"
        )
    print(f"connecting to {url}")
    print(f"driving character {args.character} on {prefix}*")
    print(f"stage: {'two hosts' if duet else 'one host'}\n")

    async with connect(url) as ws:

        async def send(viseme: str, weight: float) -> None:
            await ws.send(json.dumps({"action": f"{prefix}{viseme}", "data": weight}))

        async def close_all() -> None:
            for name in VISEMES:
                await send(name, 0.0)
            await asyncio.sleep(0.6)

        def mouth_change(a, b, side: str = "centre") -> float:
            """Difference restricted to the lower half of one head.

            The avatar breathes and sways, so a whole-frame difference cannot
            tell a mouth opening from a shoulder moving. Cropping to the mouth
            raises the signal; the control below measures what is left.

            `side` picks which host. A two-shot puts one on each half of the
            frame, and the failures worth catching there are asymmetric --
            only one mouth moving, or both prefixes driving the same face --
            so each half is measured on its own.
            """
            spans = {"centre": (0.33, 0.67), "left": (0.10, 0.44), "right": (0.56, 0.90)}
            x0, x1 = spans[side]
            difference = ImageChops.difference(a, b)
            w, h = difference.size
            box = (int(w * x0), int(h * 0.42), int(w * x1), int(h * 0.80))
            data = list(difference.crop(box).getdata())
            return sum(data) / len(data)

        # Where each host stands, and which of them this prefix should move.
        driven, other = ("left", "right") if args.character == 1 else ("right", "left")
        if not duet:
            driven, other = "centre", ""

        # --- control: how much does the frame change on its own? ----------
        await close_all()
        controls = []
        closed = shot()
        for _ in range(4):
            await close_all()
            await asyncio.sleep(0.7)
            controls.append(mouth_change(closed, shot(), driven))
        baseline = sum(controls) / len(controls)
        noise = max(controls)
        print(f"control (mouth shut throughout): mean {baseline:.3f}, worst {noise:.3f}")
        print("anything at or below the worst control reading is just idle motion.\n")

        threshold = max(args.threshold, noise * 1.8)
        bystander = f"  {'the other host':>14}" if other else ""
        print(f"{'viseme':>8}  {'mouth change':>13}  {'vs control':>11}{bystander}  verdict")
        results = {}
        bleed = {}
        for viseme in VISEMES:
            await close_all()
            closed = shot()
            await send(viseme, 1.0)
            await asyncio.sleep(0.7)
            after = shot()
            change = mouth_change(closed, after, driven)
            results[viseme] = change
            ratio = change / baseline if baseline else float("inf")
            verdict = "MOVED" if change > threshold else "no change"
            column = ""
            if other:
                bleed[viseme] = mouth_change(closed, after, other)
                column = f"  {bleed[viseme]:14.3f}"
            print(f"{viseme:>8}  {change:13.3f}  {ratio:10.1f}x{column}  {verdict}")

        await close_all()

    moved = [v for v, c in results.items() if c > threshold]
    print()
    if len(moved) == len(VISEMES):
        print("  VERDICT: the blueprint is wired. Every viseme moves the mouth,")
        print(f"  well clear of the {noise:.3f} idle-motion floor.")
    elif moved:
        print(f"  VERDICT: partial -- {', '.join(moved)} move, the rest do not.")
        print("  Check the blend shape names on the nodes that did nothing.")
    else:
        print("  VERDICT: nothing moved beyond idle motion. Either the blueprint")
        print("  is not receiving, or the blend shape names do not match.")
        print("  Check Warudo's log for 'Unknown action'.")

    if bleed:
        # The nodes are supposed to be private to one character. A prefix that
        # moves both faces means the second set is still bound to the first.
        crossed = [v for v, c in bleed.items() if c > threshold]
        if crossed:
            print(
                f"  WARNING: {', '.join(crossed)} also moved the other host -- "
                "both prefixes are driving the same face."
            )
        else:
            print("  The other host stayed still throughout, as she should.")


if __name__ == "__main__":
    asyncio.run(main())
