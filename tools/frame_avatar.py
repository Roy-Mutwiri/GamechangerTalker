"""Point Warudo's camera at the avatar's face.

    python -m tools.frame_avatar                    # head and shoulders
    python -m tools.frame_avatar --shot bust        # head to chest
    python -m tools.frame_avatar --shot full        # whole body
    python -m tools.frame_avatar --yaw 20 --pitch 5

The camera height is derived from the model's own head bone rather than
guessed, because VRM avatars are not a standard size -- a height that frames
one model points at another one's knees.

Warudo must be closed when this runs: it rewrites the scene on exit and would
overwrite the change.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
from pathlib import Path

from narrator.avatar.vrm import VrmError, head_height

WARUDO = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo\Warudo_Data")
SCENE = WARUDO / "StreamingAssets" / "Scenes" / "DefaultScene.json"
CHARACTERS = WARUDO / "StreamingAssets" / "Characters"
CAMERA_TYPE = "6a05ecf3-1501-4cab-b9d7-84131b881a29"
CHARACTER_TYPE = "726ab674-a550-474e-8b92-66526a5ad55e"

# How much vertical subject to fit in frame, in metres.
SHOTS = {"face": 0.34, "head": 0.55, "bust": 0.9, "half": 1.3, "full": 2.0}
# Where to aim, as a fraction of head-bone height.
#
# The head *bone* sits at the base of the skull, so the visible head centre is
# always above it -- and by a lot on stylised models with oversized heads.
# Tight shots therefore aim above the bone, wide ones below it to keep the
# body in frame.
AIM = {"face": 1.06, "head": 1.04, "bust": 0.97, "half": 0.80, "full": 0.58}


def current_model(scene: dict) -> str | None:
    for asset in scene["assets"]:
        if asset.get("typeId") != CHARACTER_TYPE:
            continue
        raw = asset["dataInputs"].get("Source", {}).get("value")
        if isinstance(raw, str):
            return json.loads(raw).rsplit("/", 1)[-1]
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--shot", choices=sorted(SHOTS), default="head")
    ap.add_argument("--yaw", type=float, default=0.0, help="degrees around the model")
    ap.add_argument("--pitch", type=float, default=3.0, help="degrees above eye line")
    ap.add_argument("--fov", type=float, default=None, help="override field of view")
    ap.add_argument("--head", type=float, default=None, help="override head height (m)")
    ap.add_argument("--flip", action="store_true", help="look from the other side")
    ap.add_argument(
        "--shift",
        type=float,
        default=0.0,
        help="slide the camera sideways, in metres. Warudo's preview window "
        "renders the camera with a constant horizontal offset, and this "
        "cancels it: positive moves the subject left in frame.",
    )
    ap.add_argument(
        "--rise", type=float, default=0.0, help="slide the camera up, in metres"
    )
    args = ap.parse_args()

    if not SCENE.exists():
        raise SystemExit(f"scene not found: {SCENE}")
    scene = json.loads(SCENE.read_text(encoding="utf-8"))

    camera = next((a for a in scene["assets"] if a.get("typeId") == CAMERA_TYPE), None)
    if camera is None:
        raise SystemExit("no Camera asset in the scene")

    # --- how tall is this avatar, really ---------------------------------
    height = args.head
    model = current_model(scene)
    if height is None and model:
        path = CHARACTERS / model
        if path.suffix.lower() == ".vrm" and path.exists():
            try:
                height = head_height(path)
            except VrmError as exc:
                print(f"could not read {model}: {exc}")
    if height is None:
        height = 1.35
        print(f"using a default head height of {height} m")
    else:
        print(f"{model}: head bone at {height:.3f} m")

    fov = (
        args.fov
        if args.fov is not None
        else float(camera["dataInputs"]["FieldOfView"]["value"])
    )
    subject = SHOTS[args.shot]
    aim_y = height * AIM[args.shot]
    # Distance that makes `subject` metres fill the vertical field of view.
    distance = (subject / 2) / math.tan(math.radians(fov / 2))

    yaw = args.yaw + (180.0 if args.flip else 0.0)
    pitch = args.pitch
    # VRM models face -Z, so the camera sits on -Z and looks back along +Z.
    rad_yaw, rad_pitch = math.radians(yaw), math.radians(pitch)
    horizontal = distance * math.cos(rad_pitch)
    # The camera's own right vector, so --shift slides it sideways in view
    # space rather than in world space (which would break at other yaws).
    right_x, right_z = math.cos(rad_yaw), -math.sin(rad_yaw)
    position = {
        "x": round(-horizontal * math.sin(rad_yaw) + args.shift * right_x, 4),
        "y": round(aim_y + distance * math.sin(rad_pitch) + args.rise, 4),
        "z": round(-horizontal * math.cos(rad_yaw) + args.shift * right_z, 4),
    }
    rotation = {"x": round(pitch, 4), "y": round(yaw, 4), "z": 0.0}

    backup = SCENE.with_suffix(".json.bak")
    if not backup.exists():
        shutil.copy2(SCENE, backup)
        print(f"backup -> {backup.name}")

    inputs = camera["dataInputs"]
    # None: hold the camera exactly where we put it. Orbit recomputes the
    # position from its own saved rotation and would undo this.
    inputs["ControlMode"]["value"] = json.dumps(
        {"label": "None", "value": 0, "description": None, "icon": None}
    )
    transform = json.loads(inputs["Transform"]["value"])
    transform["dataInputs"]["Position"]["value"] = json.dumps(position)
    transform["dataInputs"]["Rotation"]["value"] = json.dumps(rotation)
    inputs["Transform"]["value"] = json.dumps(transform)
    if args.fov is not None:
        inputs["FieldOfView"]["value"] = str(args.fov)

    SCENE.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    print(f"shot      {args.shot} ({subject} m of subject at {fov:.0f} deg fov)")
    print(f"distance  {distance:.2f} m")
    print(f"position  {position}")
    print(f"rotation  {rotation}")
    print("\nscene written. Start Warudo to see it.")


if __name__ == "__main__":
    main()
