"""Where Warudo is on this machine.

Four things need to find the install and they must all agree: the avatar
picker (`roster.py`) reads its Characters folder, the avatar switch
(`scene.py`, `duet.py`) rewrites its scene, and `tools/warudo_setup.py`
installs both. Anything that only knows the default Steam path works here and
silently does nothing on a machine where Steam put Warudo on another drive --
the picker comes up empty, the avatar switch logs a warning nobody reads, and
none of it looks like a wrong path.

Searched in order, first hit wins:

  1. `$env:WARUDO_ROOT` -- the escape hatch, and what to set for a portable
     install or a non-Steam copy.
  2. the two default Steam locations, 32- and 64-bit Program Files.
  3. every library in Steam's `libraryfolders.vdf`. This is the one that
     matters: Steam offers a second library on the first big install, so a
     machine with games on D: is the normal case, not the exotic one.

`WARUDO_ROOT` may point at the install folder or at `Warudo_Data` inside it;
both are accepted, because both are things a person reasonably copies out of
an explorer window.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

# Where Steam puts Warudo when nobody has moved it.
DEFAULT_ROOTS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo"),
    Path(r"C:\Program Files\Steam\steamapps\common\Warudo"),
)
LIBRARY_FOLDERS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"),
    Path(r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"),
)

SCENE_FILE = "DefaultScene.json"


def candidates(explicit: Path | None = None) -> list[Path]:
    """Every place worth looking, best first."""
    found: list[Path] = []
    if explicit is not None:
        found.append(Path(explicit))
    env = os.environ.get("WARUDO_ROOT")
    if env:
        found.append(Path(env))
    found.extend(DEFAULT_ROOTS)
    found.extend(_steam_libraries())
    return found


def root(explicit: Path | None = None) -> Path | None:
    """The Warudo install directory -- the one holding Warudo_Data."""
    for base in candidates(explicit):
        # Accept the install folder or Warudo_Data inside it, either way.
        for folder in (base, base.parent):
            if (folder / "Warudo_Data" / "StreamingAssets").is_dir():
                return folder
    return None


def streaming_assets(explicit: Path | None = None) -> Path | None:
    found = root(explicit)
    return found / "Warudo_Data" / "StreamingAssets" if found else None


def characters_folder(explicit: Path | None = None) -> Path | None:
    """The folder Warudo loads .vrm/.warudo models from.

    Warudo looks here and nowhere else, which is why the repo's `avatars/`
    has to be copied in rather than pointed at.
    """
    assets = streaming_assets(explicit)
    folder = assets / "Characters" if assets else None
    return folder if folder and folder.is_dir() else None


def scene_path(explicit: Path | None = None, name: str = SCENE_FILE) -> Path | None:
    """Warudo's saved scene, if this machine has one."""
    assets = streaming_assets(explicit)
    if assets is None:
        return None
    scene = assets / "Scenes" / name
    return scene if scene.is_file() else None


@lru_cache(maxsize=1)
def _library_paths() -> tuple[Path, ...]:
    """Steam library roots, parsed out of libraryfolders.vdf.

    Cached: this runs on every roster rebuild, and the answer cannot change
    without Steam restarting. Not worth a real VDF parser -- one regex over
    the `"path"` keys is the whole of what is needed here, and a malformed
    file degrades to "no extra libraries" rather than an exception.
    """
    paths: list[Path] = []
    for config in LIBRARY_FOLDERS:
        if not config.is_file():
            continue
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            paths.append(Path(raw.replace("\\\\", "\\")))
    return tuple(paths)


def _steam_libraries() -> list[Path]:
    return [base / "steamapps" / "common" / "Warudo" for base in _library_paths()]
