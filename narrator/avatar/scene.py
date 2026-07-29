"""Write an avatar and its setting into Warudo's scene file.

Warudo will happily accept a new `Source` on a live Character asset, and then
unload the old model without loading the new one -- its log says
`Unloaded ModHost<name>` and the room is left empty. Changing the file and
asking Warudo to reload the scene is the route that actually works, and it has
the side benefit of moving the character and the camera in the same step, so a
switch lands on a composed shot rather than wherever the last one was.

Nothing here touches the blueprints: the graph is part of the saved scene and
comes back with it.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from pathlib import Path
from typing import Any

from narrator.config import AvatarEntry

log = logging.getLogger(__name__)


def scene_path() -> Path | None:
    """Warudo's saved scene, if this machine has one."""
    for base in (
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo"),
        Path(r"C:\Program Files\Steam\steamapps\common\Warudo"),
    ):
        scene = base / "Warudo_Data" / "StreamingAssets" / "Scenes" / "DefaultScene.json"
        if scene.is_file():
            return scene
    return None


def apply(entry: AvatarEntry, focus_height: float, path: Path | None = None) -> bool:
    """Put this avatar, in its setting, into the scene file.

    Returns False rather than raising: a failed avatar switch must never take
    the stream down with it.
    """
    path = path or scene_path()
    if path is None:
        log.warning("no Warudo scene found; avatar switch skipped")
        return False

    try:
        scene = json.loads(path.read_text(encoding="utf-8-sig"))
        character = _asset(scene, "Character 1")
        camera = _asset(scene, "Camera 1")
        if character is None or camera is None:
            log.warning("scene has no Character 1 / Camera 1; avatar switch skipped")
            return False

        # Keep one rollback copy. Warudo rewrites this file itself on exit, so
        # a backup per switch would bury the useful one under noise.
        shutil.copyfile(path, path.with_suffix(".json.pre-avatar"))

        character["dataInputs"]["Source"]["value"] = json.dumps(entry.source)
        _set_transform(
            character,
            {"x": entry.x, "y": entry.y, "z": entry.z},
            {"x": 0.0, "y": entry.facing, "z": 0.0},
        )

        position, rotation = orbit(entry, focus_height)
        camera["dataInputs"]["FollowCharacter"]["value"] = "false"
        _set_transform(camera, position, rotation)

        path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        log.warning("could not write the avatar into the scene: %s", exc)
        return False

    log.info("scene set to %s (%s)", entry.name, entry.setting or "default setting")
    return True


def orbit(
    entry: AvatarEntry, focus_height: float
) -> tuple[dict[str, float], dict[str, float]]:
    """The camera transform for this avatar's shot, orbiting their face."""
    yaw_rad = math.radians(entry.yaw)
    pitch_rad = math.radians(entry.pitch)
    flat = entry.distance * math.cos(pitch_rad)
    # The face moves with the character, so a seated drop takes the shot with it.
    eye = entry.y + focus_height
    # Sliding along the camera's own right vector moves the subject across the
    # frame without turning the lens away from them.
    right_x = math.cos(yaw_rad)
    right_z = -math.sin(yaw_rad)
    position = {
        "x": round(entry.x + flat * math.sin(yaw_rad) + entry.offset * right_x, 4),
        "y": round(eye + entry.distance * math.sin(pitch_rad), 4),
        "z": round(entry.z + flat * math.cos(yaw_rad) + entry.offset * right_z, 4),
    }
    # Positive X tilts down in Unity, so pitch goes in as-is.
    rotation = {"x": round(entry.pitch, 3), "y": round(entry.yaw + 180.0, 3), "z": 0.0}
    return position, rotation


def _asset(scene: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in scene.get("assets", []) if a.get("name") == name), None)


def _set_transform(
    asset: dict[str, Any], position: dict[str, float], rotation: dict[str, float]
) -> None:
    """Transform is structured data carrying its own JSON, so it is a string
    inside a string -- decode, edit, re-encode."""
    transform = json.loads(asset["dataInputs"]["Transform"]["value"])
    inputs = transform["dataInputs"]
    inputs["Position"]["value"] = json.dumps(position)
    inputs["Rotation"]["value"] = json.dumps(rotation)
    asset["dataInputs"]["Transform"]["value"] = json.dumps(transform)
