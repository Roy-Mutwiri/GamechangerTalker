"""The avatar picker's roster.

The operator lists characters in `config.toml`; this turns that list into
something the browser UI can render and the bridge can act on. The one piece
of real work is the framing height: a chibi's face sits near 0.9m and an adult
VRM's near 1.5m, so switching avatar without moving the camera leaves it
pointing at an empty room.

For a `.vrm` that height is measured off the model's head bone rather than
guessed. `.warudo` characters are opaque -- Warudo's own Mod SDK format, not
glTF -- so those carry the number in config.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from narrator.avatar.vrm import VrmError, head_height, inspect
from narrator.config import AvatarEntry, Config

log = logging.getLogger(__name__)

# The head bone sits at the base of the skull; the face is a little above it.
FACE_ABOVE_HEAD_BONE = 0.09
FALLBACK_FOCUS = 1.35


def characters_folder() -> Path | None:
    """Warudo's Characters folder, if this machine has one."""
    for base in (
        Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo"),
        Path(r"C:\Program Files\Steam\steamapps\common\Warudo"),
    ):
        folder = base / "Warudo_Data" / "StreamingAssets" / "Characters"
        if folder.is_dir():
            return folder
    return None


def build(cfg: Config) -> list[dict[str, Any]]:
    """One entry per configured avatar, ready to hand to the browser.

    Missing files are dropped rather than offered: a picker that lists a
    character Warudo cannot load is worse than a shorter picker.
    """
    folder = characters_folder()
    roster: list[dict[str, Any]] = []

    for entry in cfg.warudo.avatars:
        path = folder / entry.file if folder else None
        if path is not None and not path.exists():
            log.warning("avatar %s is in config but not in %s", entry.file, folder)
            continue
        roster.append(
            {
                "file": entry.file,
                "source": entry.source,
                "label": entry.name,
                "gender": entry.gender,
                "setting": entry.setting,
                "focus": focus_height(entry, path),
                "lipSync": lip_sync_ok(path),
            }
        )
    return roster


def focus_height(entry: AvatarEntry, path: Path | None) -> float:
    """What the framing camera should orbit for this model."""
    if entry.focus_height > 0:
        return entry.focus_height
    if path is not None and path.suffix.lower() == ".vrm":
        try:
            head = head_height(path)
            if head:
                return round(head + FACE_ABOVE_HEAD_BONE, 3)
        except (VrmError, OSError, ValueError) as exc:
            log.debug("could not measure %s: %s", path.name, exc)
    return FALLBACK_FOCUS


def lip_sync_ok(path: Path | None) -> bool | None:
    """True/False for a readable VRM, None when we cannot tell.

    Two of the CC0 models ship vowel clips that bind to nothing, so this is
    worth showing in the picker rather than discovering mid-stream.
    """
    if path is None or path.suffix.lower() != ".vrm":
        return None
    try:
        return inspect(path).lip_sync_ready
    except (VrmError, OSError, ValueError):
        return None
