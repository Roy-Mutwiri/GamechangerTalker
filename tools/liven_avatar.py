"""Turn on the things that make a Warudo avatar look alive.

    python -m tools.liven_avatar
    python -m tools.liven_avatar --off        # back to a mannequin
    python -m tools.liven_avatar --breathing-rate 0.3 --sway 0.35

Warudo ships breathing, swaying and look-at, and a fresh scene has all three
switched **off**. That is most of why a new character reads as a shop dummy.

  Breathing   slow chest and shoulder movement
  Swaying     weight shifting from foot to foot
  Look At     the head, eyes and body track a target. Pointed at the camera
              it becomes eye contact with the audience, which is the single
              biggest difference between "a model" and "a streamer".

Automatic blinking and idle head motion are NOT in here: in Warudo they
belong to the face-tracking plugins and need a webcam. Without one they have
to be driven externally -- see WARUDO_SETUP.md for the blink and head-turn
actions the narrator sends.

Warudo must be closed: it rewrites the scene on exit.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

WARUDO = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo\Warudo_Data")
SCENE = WARUDO / "StreamingAssets" / "Scenes" / "DefaultScene.json"
CHARACTER_TYPE = "726ab674-a550-474e-8b92-66526a5ad55e"
CAMERA_TYPE = "6a05ecf3-1501-4cab-b9d7-84131b881a29"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--off", action="store_true", help="switch it all back off")
    ap.add_argument("--breathing-rate", type=float, default=0.28)
    ap.add_argument("--breathing-exertion", type=float, default=0.22)
    ap.add_argument("--sway", type=float, default=0.3)
    ap.add_argument(
        "--look-weight",
        type=float,
        default=0.65,
        help="how strongly the character tracks the camera, 0..1",
    )
    args = ap.parse_args()

    if not SCENE.exists():
        raise SystemExit(f"scene not found: {SCENE}")
    scene = json.loads(SCENE.read_text(encoding="utf-8"))

    camera = next((a for a in scene["assets"] if a.get("typeId") == CAMERA_TYPE), None)
    character = next(
        (a for a in scene["assets"] if a.get("typeId") == CHARACTER_TYPE), None
    )
    if character is None:
        raise SystemExit("no Character asset in the scene")

    backup = SCENE.with_suffix(".json.bak")
    if not backup.exists():
        shutil.copy2(SCENE, backup)
        print(f"backup -> {backup.name}")

    on = not args.off
    inputs = character["dataInputs"]

    def put(key: str, value: object) -> None:
        if key in inputs:
            inputs[key]["value"] = (
                json.dumps(value) if isinstance(value, dict | list) else str(value)
            )
            print(f"  {key:<26} {inputs[key]['value'][:60]}")
        else:
            print(f"  {key:<26} (not on this character, skipped)")

    print("breathing")
    put("BreathingEnabled", "true" if on else "false")
    put("BreathingRate", args.breathing_rate)
    put("BreathingExertion", args.breathing_exertion)

    print("swaying")
    put("SwayingEnabled", "true" if on else "false")
    put("SwayingIntensity", args.sway)

    print("look at")
    put("LookAtEnabled", "true" if on else "false")
    if on and camera is not None:
        # Eye contact with the audience: the camera is where the viewer is.
        put("LookAtTarget", {"id": camera["id"], "name": camera["name"]})
    elif not on:
        put("LookAtTarget", None)
    put("LookAtWeight", args.look_weight)
    put("LookAtEyesWeight", 1.0)
    put("LookAtHeadWeight", 0.85 if on else 1.0)
    put("LookAtBodyWeight", 0.25 if on else 1.0)

    SCENE.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    print(f"\nscene written ({'alive' if on else 'off'}). Start Warudo to see it.")


if __name__ == "__main__":
    main()
