"""Check a VRM avatar against the narrator's requirements.

    python -m tools.avatar_check "C:\\path\\to\\model.vrm"
    python -m tools.avatar_check                       # every VRM in Warudo's folder

Run this on any model you download BEFORE building the Warudo blueprint. It
reads the VRM header directly -- no Unity, no Warudo, no import step -- and
tells you whether the mouth can actually be driven and which expressions the
emotes will land on.

A model that fails the lip-sync check cannot be lip-synced by anything,
Warudo's own lip sync included. That is a property of the model, not of this
narrator.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from narrator.avatar.vrm import VrmError, VrmInfo, inspect
from narrator.config import load_config, project_root

TICK = "OK  "
CROSS = "MISS"


def warudo_characters_folder() -> Path | None:
    """Warudo's data folder on Windows, if it is where it usually is."""
    candidates = [
        Path(os.environ.get("USERPROFILE", "")) / "Warudo" / "Characters",
        Path(os.environ.get("USERPROFILE", "")) / "Documents" / "Warudo" / "Characters",
        Path(os.environ.get("APPDATA", "")) / "Warudo" / "Characters",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Warudo" / "Characters",
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return None


def report(info: VrmInfo, expressions: dict[str, list[str]]) -> bool:
    print(f"\n{info.path.name}")
    print("-" * min(78, max(20, len(info.path.name))))
    print(f"  VRM version    {info.version}")
    print(f"  name           {info.name or '(none)'}")
    print(f"  author         {info.author or '(none)'}")
    if info.commercial:
        print(f"  commercial use {info.commercial}")
    if info.license_url:
        print(f"  license        {info.license_url}")
    print(
        f"  content        {info.mesh_count} meshes, {info.texture_count} textures, "
        f"{info.humanoid_bones} humanoid bones, {info.size_mb:.1f} MB"
    )
    print(f"  expressions    {len(info.expressions)}")

    print("\n  lip sync (required)")
    visemes = info.viseme_status()
    unbound = set(info.unbound_visemes())
    for viseme, clip in visemes.items():
        if clip and viseme in unbound:
            print(f"    [{CROSS}] {viseme:<3} -> {clip} (clip exists but drives nothing)")
        else:
            mark = TICK if clip else CROSS
            print(f"    [{mark}] {viseme:<3} -> {clip or 'not found'}")

    print("\n  emotes (optional, falls back to neutral)")
    emotes = info.emote_status(expressions)
    for emote, clip in emotes.items():
        mark = TICK if clip else "none"
        target = clip or f"no clip; {emote} will do nothing"
        print(f"    [{mark:<4}] {emote:<10} -> {target}")

    extra = sorted(
        e
        for e in info.expressions
        if e.lower() not in {v.lower() for v in visemes.values() if v}
        and e.lower() not in {v.lower() for v in emotes.values() if v}
    )
    if extra:
        shown = ", ".join(extra[:12])
        more = f" (+{len(extra) - 12} more)" if len(extra) > 12 else ""
        print(f"\n  other clips    {shown}{more}")

    ok = info.lip_sync_ready
    if ok:
        print("\n  VERDICT: usable. The mouth will move.")
    elif unbound:
        print(
            f"\n  VERDICT: NOT usable for lip sync -- {', '.join(sorted(unbound))} "
            "name a clip\n  that binds to nothing. The clip is a label with no morph "
            "target and no\n  material value behind it, so Warudo will list it, accept "
            "a weight of 1.0,\n  and move no part of the face. Re-export from VRoid "
            "Studio, or bind the\n  clips in Unity with UniVRM."
        )
    else:
        missing = [v for v, clip in visemes.items() if not clip]
        print(
            f"\n  VERDICT: NOT usable for lip sync -- missing {', '.join(missing)}.\n"
            "  Fix it by re-exporting from VRoid Studio (which always writes the\n"
            "  five vowel shapes), or add the clips in Unity with UniVRM."
        )
    if info.humanoid_bones == 0:
        print("  WARNING: no humanoid bones; motion capture will not work either.")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", help="a .vrm file or a folder of them")
    args = ap.parse_args()

    cfg = load_config(project_root() / "config.toml")
    expressions = cfg.warudo.expressions

    if args.path:
        target = Path(args.path)
    else:
        target = warudo_characters_folder()
        if target is None:
            print(
                "Could not find Warudo's Characters folder. Pass a path:\n"
                '    python -m tools.avatar_check "C:\\path\\to\\model.vrm"\n\n'
                "In Warudo the folder is Menu -> Open Data Folder -> Characters."
            )
            return
        print(f"scanning {target}")

    files = [target] if target.is_file() else sorted(target.glob("*.vrm"))
    if not files:
        print(f"no .vrm files found in {target}")
        return

    good = 0
    for path in files:
        try:
            if report(inspect(path), expressions):
                good += 1
        except VrmError as exc:
            print(f"\n{path.name}\n  ERROR: {exc}")

    if len(files) > 1:
        print(f"\n{good} of {len(files)} models are ready for lip sync.")


if __name__ == "__main__":
    main()
