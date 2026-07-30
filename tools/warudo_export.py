"""Capture this machine's working Warudo scene into the repo.

    python -m tools.warudo_export
    python -m tools.warudo_export --check      # report drift, write nothing

The scene is where the whole avatar setup actually lives: the character, the
camera, the room, and the `narrator` blueprint that turns websocket actions
into blendshape weights. None of it is in this repo by default, which is why a
fresh clone starts with a narrator that talks to nothing. This writes the scene
out to `warudo/DefaultScene.json`, and `tools/warudo_setup.py` installs it on
the next machine.

What gets stripped, and why -- everything here is *this machine*, not *this
project*, and carrying it across would break the clone rather than help it:

  * motion-capture assets and their graphs (SteamVR here, MediaPipe or a phone
    somewhere else). Read off the characters' own TrackingAssetIds /
    TrackingGraphIds, so this generalises to whatever rig is plugged in.
  * the microphone in the disabled MFCC lip-sync graph, which is stored as a
    Windows endpoint GUID and names a sound card that does not exist elsewhere.
  * the editor's selection, which is just where somebody last clicked.

Nothing else is touched. The `narrator` graph in particular is copied verbatim,
node schemas and all, because Warudo wrote it and Warudo is the only authority
on what its own nodes look like.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "warudo" / "DefaultScene.json"

# The blueprint the narrator drives. Named, not id'd: a scene rebuilt by hand
# gets a fresh guid, and the name is what survives.
GRAPH_NAME = "narrator"

# Written with indent=1: still one key per line, so a diff shows what actually
# changed between two exports, without the width of a four-space indent on a
# file this size.
INDENT = 1


def load(path: Path) -> dict[str, Any]:
    # utf-8-sig: Warudo writes a BOM on some paths and not others.
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sanitize(scene: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Strip everything that belongs to this machine rather than this project."""
    notes: list[str] = []

    drop_assets, drop_graphs = _tracking_ids(scene)
    for asset in scene.get("assets", []):
        for key in ("TrackingAssetIds", "TrackingGraphIds"):
            field = (asset.get("dataInputs") or {}).get(key)
            if field is not None:
                field["value"] = "[]"

    if drop_assets:
        names = [a["name"] for a in scene.get("assets", []) if a.get("id") in drop_assets]
        scene["assets"] = [
            a for a in scene.get("assets", []) if a.get("id") not in drop_assets
        ]
        notes.append(f"dropped mocap asset(s): {', '.join(names) or '?'}")
    if drop_graphs:
        names = [g["name"] for g in scene.get("graphs", []) if g.get("id") in drop_graphs]
        scene["graphs"] = [
            g for g in scene.get("graphs", []) if g.get("id") not in drop_graphs
        ]
        notes.append(f"dropped mocap graph(s): {', '.join(names) or '?'}")

    cleared = _clear_microphones(scene)
    if cleared:
        notes.append(f"cleared {cleared} microphone endpoint GUID(s)")

    # Prune both trees down to keys that still name something. A hierarchy
    # entry for a deleted asset is how a scene ends up half-loaded.
    live = {a["id"] for a in scene.get("assets", [])}
    live_graphs = {g["id"] for g in scene.get("graphs", [])}
    _prune(scene.get("assetHierarchy"), live)
    _prune(scene.get("graphHierarchy"), live_graphs)

    scene["selectedAssetId"] = "00000000-0000-0000-0000-000000000000"
    scene["selectedGraphId"] = "00000000-0000-0000-0000-000000000000"

    return scene, notes


def _tracking_ids(scene: dict[str, Any]) -> tuple[set[str], set[str]]:
    """Asset and graph ids the characters name as their motion capture rig."""
    assets: set[str] = set()
    graphs: set[str] = set()
    for asset in scene.get("assets", []):
        inputs = asset.get("dataInputs") or {}
        for key, sink in (("TrackingAssetIds", assets), ("TrackingGraphIds", graphs)):
            raw = (inputs.get(key) or {}).get("value")
            if not isinstance(raw, str):
                continue
            try:
                ids = json.loads(raw)
            except ValueError:
                continue
            if isinstance(ids, list):
                sink.update(str(i) for i in ids)
    return assets, graphs


def _clear_microphones(scene: dict[str, Any]) -> int:
    """Blank every Microphone input. It stores a Windows endpoint GUID."""
    cleared = 0
    for graph in scene.get("graphs", []):
        for node in (graph.get("nodes") or {}).values():
            field = (node.get("dataInputs") or {}).get("Microphone")
            if field is not None and field.get("value") not in (None, '""'):
                field["value"] = '""'
                cleared += 1
    return cleared


def _prune(branch: Any, live: set[str]) -> None:
    """Drop hierarchy entries whose key names nothing that still exists.

    Group nodes -- "Characters", "Cinematography" -- have plain-text keys and
    children rather than an id, so anything with children is kept.
    """
    if not isinstance(branch, dict):
        return
    children = branch.get("children")
    if not isinstance(children, list):
        return
    kept = []
    for child in children:
        _prune(child, live)
        key = str(child.get("key", ""))
        has_children = bool(child.get("children"))
        if key in live or has_children or not _looks_like_guid(key):
            kept.append(child)
    branch["children"] = kept


def _looks_like_guid(key: str) -> bool:
    return len(key) == 36 and key.count("-") == 4


def summarise(scene: dict[str, Any]) -> list[str]:
    lines = [f"  appVersion {scene.get('appVersion')}"]
    for asset in scene.get("assets", []):
        source = (asset.get("dataInputs") or {}).get("Source", {}).get("value")
        tail = f"  {json.loads(source)}" if isinstance(source, str) else ""
        lines.append(f"  asset  {asset['name']:<24}{tail}")
    for graph in scene.get("graphs", []):
        state = "on " if graph.get("enabled") else "off"
        lines.append(
            f"  graph  {graph['name']:<24}  [{state}] "
            f"{len(graph.get('nodes') or {})} nodes"
        )
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--check",
        action="store_true",
        help="compare the live scene against the committed one; write nothing",
    )
    ap.add_argument("--scene", type=Path, help="read this scene instead of the live one")
    args = ap.parse_args()

    from narrator.avatar.scene import scene_path

    source = args.scene or scene_path()
    if source is None:
        print("no Warudo scene on this machine -- nothing to export", file=sys.stderr)
        return 1

    scene, notes = sanitize(load(source))
    if not any(g.get("name") == GRAPH_NAME for g in scene.get("graphs", [])):
        print(
            f"the live scene has no {GRAPH_NAME!r} blueprint -- refusing to export a "
            "scene that cannot drive the avatar",
            file=sys.stderr,
        )
        return 1

    text = json.dumps(scene, ensure_ascii=False, indent=INDENT) + "\n"

    if args.check:
        current = TEMPLATE.read_text(encoding="utf-8") if TEMPLATE.exists() else ""
        if current == text:
            print(f"up to date: {TEMPLATE.relative_to(REPO)}")
            return 0
        print(f"DRIFT: {TEMPLATE.relative_to(REPO)} differs from the live scene")
        print("  run `python -m tools.warudo_export` to update it")
        return 1

    TEMPLATE.parent.mkdir(parents=True, exist_ok=True)
    TEMPLATE.write_text(text, encoding="utf-8")

    print(f"read  {source}")
    for note in notes:
        print(f"  - {note}")
    print(f"wrote {TEMPLATE.relative_to(REPO)}  ({len(text) / 1e6:.1f} MB)")
    print("\n".join(summarise(scene)))
    print("\nInstall it on another machine with `python -m tools.warudo_setup`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
