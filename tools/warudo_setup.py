"""Put the whole Warudo setup onto a machine that has only just cloned this repo.

    python -m tools.warudo_setup --check     # what is missing, writes nothing
    python -m tools.warudo_setup             # install it
    python -m tools.warudo_setup --replace-scene
    python -m tools.warudo_setup --remove    # take the blueprint back out

A fresh clone has the narrator, the avatars and the docs, and Warudo still does
nothing -- because the half that makes the mouth move does not live in the repo
at all. It lives inside the Warudo install:

    Warudo_Data/StreamingAssets/Characters/*.vrm    the models
    Warudo_Data/StreamingAssets/Scenes/DefaultScene.json
        the character, the camera, the room, and the `narrator` blueprint
        that turns {"action": "viseme_aa", "data": 0.8} into a blendshape

This copies both halves across. `warudo/DefaultScene.json` is the scene from the
machine this was built on, exported by `tools/warudo_export.py` with the mocap
rig and the sound-card GUID stripped out; everything else in it -- all thirty
nodes of the blueprint, the camera, the framing -- is byte-for-byte what has
been on stream.

Two modes, chosen automatically:

  * **Replace.** No scene yet, or a scene with no `Character 1`: the packaged
    scene is copied in whole. Nothing to lose, and the ids inside it already
    agree with each other.
  * **Graft.** A scene that already has a character and a camera -- somebody
    ran Warudo's Onboarding, or has a room they built: only the `narrator`
    blueprint is added, with every `Character 1` / `Camera 1` reference inside
    it re-pointed at *that* scene's assets. Their room survives; the mouth
    starts working.

Warudo must be closed. It holds the scene in memory and rewrites the file on
exit, so anything written underneath a running Warudo is discarded at the exact
moment it looks like it worked.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "warudo" / "DefaultScene.json"
AVATARS = REPO / "avatars"

GRAPH_NAME = "narrator"
SCENE_FILE = "DefaultScene.json"

# Assets the blueprint's nodes point at. Matched by name, because the ids
# differ on every install -- Warudo mints a fresh guid per asset.
LINKED_ASSETS = ("Character 1", "Character 2", "Camera 1")

# Where Steam puts Warudo when nobody has moved it.
DEFAULT_ROOTS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo"),
    Path(r"C:\Program Files\Steam\steamapps\common\Warudo"),
)
STEAM_CONFIGS = (
    Path(r"C:\Program Files (x86)\Steam\steamapps\libraryfolders.vdf"),
    Path(r"C:\Program Files\Steam\steamapps\libraryfolders.vdf"),
)


# ---------------------------------------------------------------------------
# Finding Warudo
# ---------------------------------------------------------------------------


def find_warudo(explicit: Path | None = None) -> Path | None:
    """The Warudo install directory -- the one holding Warudo_Data."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    env = os.environ.get("WARUDO_ROOT")
    if env:
        candidates.append(Path(env))
    candidates.extend(DEFAULT_ROOTS)
    candidates.extend(_steam_libraries())

    for base in candidates:
        # Tolerate being handed Warudo_Data, or the exe's folder, either way.
        for root in (base, base.parent):
            if (root / "Warudo_Data" / "StreamingAssets").is_dir():
                return root
    return None


def _steam_libraries() -> list[Path]:
    """Warudo under any Steam library folder, not just the default one."""
    found: list[Path] = []
    for config in STEAM_CONFIGS:
        if not config.is_file():
            continue
        try:
            text = config.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in re.findall(r'"path"\s+"([^"]+)"', text):
            found.append(
                Path(raw.replace("\\\\", "\\")) / "steamapps" / "common" / "Warudo"
            )
    return found


def warudo_running() -> bool:
    """Warudo rewrites the scene on exit, so editing it live loses the edit."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Warudo.exe", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "Warudo.exe" in out


# ---------------------------------------------------------------------------
# The pieces
# ---------------------------------------------------------------------------


def _models(folder: Path) -> list[str]:
    """Every character file Warudo would offer from this folder."""
    if not folder.is_dir():
        return []
    return sorted(
        p.name for p in folder.iterdir() if p.suffix.lower() in (".vrm", ".warudo")
    )


def install_avatars(characters: Path, *, dry_run: bool) -> list[str]:
    """Copy the committed roster into the folder Warudo watches.

    config.toml names these files and roster.py drops any that are missing, so
    a clone without this step gets an empty avatar picker and a scene pointing
    at a model that is not there.
    """
    done: list[str] = []
    if not AVATARS.is_dir():
        return done
    if not dry_run:
        characters.mkdir(parents=True, exist_ok=True)

    for model in sorted(AVATARS.iterdir()):
        if model.suffix.lower() not in (".vrm", ".warudo"):
            continue
        target = characters / model.name
        if target.exists() and target.stat().st_size == model.stat().st_size:
            continue
        done.append(model.name)
        if not dry_run:
            shutil.copy2(model, target)
    return done


def graft(
    scene: dict[str, Any], template: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Add the packaged blueprint to somebody else's scene.

    The graph is copied whole and then re-pointed: every node input holding
    `{"id": ..., "name": "Character 1"}` gets the id *this* scene uses. A pair
    whose target does not exist here is dropped rather than left dangling --
    a Set Character BlendShape node aimed at a missing character throws on
    every frame the narrator sends, sixty times a second.
    """
    packaged = _graph(template, GRAPH_NAME)
    if packaged is None:
        raise ValueError(f"the packaged scene has no {GRAPH_NAME!r} blueprint")

    notes: list[str] = []
    graph = copy.deepcopy(packaged)
    here = {name: _asset(scene, name) for name in LINKED_ASSETS}

    missing = {name for name, asset in here.items() if asset is None}
    if missing:
        removed = _drop_nodes_for(graph, missing)
        notes.append(
            f"no {', '.join(sorted(missing))} in this scene: "
            f"dropped {removed} node(s) that pointed at them"
        )

    remapped = _remap(graph, {n: a["id"] for n, a in here.items() if a is not None})
    notes.append(f"re-pointed {remapped} asset reference(s) at this scene's ids")

    scene["graphs"] = [g for g in scene.get("graphs", []) if g.get("name") != GRAPH_NAME]
    scene["graphs"].append(graph)
    _add_graph_to_hierarchy(scene, graph["id"])
    return scene, notes


def _remap(graph: dict[str, Any], ids: dict[str, str]) -> int:
    """Rewrite asset references by name. Returns how many were changed."""
    changed = 0
    for node in (graph.get("nodes") or {}).values():
        for field in (node.get("dataInputs") or {}).values():
            raw = field.get("value")
            if not isinstance(raw, str) or '"name"' not in raw:
                continue
            try:
                value = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(value, dict) or "id" not in value:
                continue
            target = ids.get(str(value.get("name")))
            if target and target != value["id"]:
                value["id"] = target
                field["value"] = json.dumps(value)
                changed += 1
    return changed


def _drop_nodes_for(graph: dict[str, Any], names: set[str]) -> int:
    """Remove every node aimed at one of these assets, and its connections.

    Both ends go: an On WebSocket Action node whose only downstream is gone
    would sit in the graph firing into nothing.
    """
    nodes = graph.get("nodes") or {}
    doomed = {nid for nid, node in nodes.items() if _targets(node) & names}
    # Anything feeding a doomed node is now pointless too.
    for connection in graph.get("dataConnections", []) + graph.get("flowConnections", []):
        if connection["inputNode"] in doomed:
            doomed.add(connection["outputNode"])

    for nid in doomed:
        nodes.pop(nid, None)
    for key in ("dataConnections", "flowConnections"):
        graph[key] = [
            c
            for c in graph.get(key, [])
            if c["outputNode"] not in doomed and c["inputNode"] not in doomed
        ]
    return len(doomed)


def _targets(node: dict[str, Any]) -> set[str]:
    """Asset names this node names in any of its inputs."""
    found: set[str] = set()
    for field in (node.get("dataInputs") or {}).values():
        raw = field.get("value")
        if not isinstance(raw, str) or '"name"' not in raw:
            continue
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if isinstance(value, dict) and "id" in value and "name" in value:
            found.add(str(value["name"]))
    return found


def _add_graph_to_hierarchy(scene: dict[str, Any], graph_id: str) -> None:
    """A graph missing from graphHierarchy is loaded and never shown.

    Same trap as assetHierarchy in duet.py: the tree is what the editor builds
    its list from, and a blueprint absent from it is invisible even though it
    runs.
    """
    root = scene.get("graphHierarchy")
    if not isinstance(root, dict):
        scene["graphHierarchy"] = {
            "collapsed": False,
            "key": "",
            "children": [{"collapsed": False, "key": graph_id, "children": None}],
        }
        return
    if graph_id in _keys(root):
        return
    node = {"collapsed": False, "key": graph_id, "children": None}
    for child in root.get("children") or []:
        if child.get("key") == "Custom":
            child["children"] = (child.get("children") or []) + [node]
            return
    root["children"] = (root.get("children") or []) + [node]


def _drop_from_hierarchy(branch: Any, key: str) -> None:
    """Take a deleted graph's key back out of the tree.

    Leaving it behind is not cosmetic: Warudo walks the tree, and an entry
    naming nothing is the same half-loaded-scene failure the docs warn about.
    """
    if not isinstance(branch, dict) or not isinstance(branch.get("children"), list):
        return
    branch["children"] = [c for c in branch["children"] if c.get("key") != key]
    for child in branch["children"]:
        _drop_from_hierarchy(child, key)


def _keys(branch: dict[str, Any]) -> set[str]:
    found = {str(branch.get("key", ""))}
    for child in branch.get("children") or []:
        found |= _keys(child)
    return found


def _asset(scene: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in scene.get("assets", []) if a.get("name") == name), None)


def _graph(scene: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((g for g in scene.get("graphs", []) if g.get("name") == name), None)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


# What each sink node does, in words. Keyed by node name rather than typeId so
# the table still reads if Warudo renumbers a type between releases.
SINK_VERBS = {
    "SET_CHARACTER_BLENDSHAPE": lambda n: f"blendshape {_string(n, 'BlendShape')}",
    "TOGGLE_CHARACTER_EXPRESSION": lambda n: "expression (name comes in the message)",
    "SET_ASSET_POSITION": lambda n: "position",
    "SET_ASSET_ROTATION": lambda n: "rotation",
    "SET_ASSET_PROPERTY": lambda n: f"property {_string(n, 'DataPath')}",
    "LOAD_SCENE": lambda n: "reload the scene from disk",
}


def describe(graph: dict[str, Any]) -> list[str]:
    """What each websocket action ends up driving. The whole contract, in one table."""
    nodes = graph.get("nodes") or {}
    sinks: dict[str, str] = {}
    for connection in graph.get("flowConnections", []):
        source = nodes.get(connection["outputNode"])
        target = nodes.get(connection["inputNode"])
        if not source or not target:
            continue
        action = _string(source, "Action")
        if not action:
            continue
        name = str(target.get("name", "?"))
        who = _string(target, "Character") or _string(target, "Asset") or "scene"
        verb = SINK_VERBS.get(name, lambda n, name=name: name.lower())(target)
        sinks[action] = f"{who:<14} {verb}"
    return [f"    {action:<14} -> {sink}" for action, sink in sorted(sinks.items())]


def _string(node: dict[str, Any], key: str) -> str:
    raw = (node.get("dataInputs") or {}).get(key, {}).get("value")
    if not isinstance(raw, str):
        return ""
    try:
        value = json.loads(raw)
    except ValueError:
        return raw.strip('"')
    if isinstance(value, dict):
        return str(value.get("name", ""))
    return str(value)


def port_status() -> str:
    from narrator.config import load_config
    from narrator.preflight import check_warudo

    try:
        result = check_warudo(load_config())
    except Exception as exc:  # config is the operator's file; never crash on it
        return f"could not check the websocket: {exc}"
    return result.detail


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--warudo-root", type=Path, help="the folder holding Warudo_Data")
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument(
        "--replace-scene",
        action="store_true",
        help="overwrite the existing scene with the packaged one instead of "
        "grafting the blueprint into it",
    )
    ap.add_argument("--remove", action="store_true", help="delete the narrator blueprint")
    args = ap.parse_args()

    root = find_warudo(args.warudo_root)
    if root is None:
        print(
            "Warudo not found. Install it from Steam (app 2079120), or point at "
            "it:\n    python -m tools.warudo_setup --warudo-root "
            '"D:\\Games\\steamapps\\common\\Warudo"',
            file=sys.stderr,
        )
        return 1

    streaming = root / "Warudo_Data" / "StreamingAssets"
    characters = streaming / "Characters"
    scene_path = streaming / "Scenes" / SCENE_FILE
    print(f"Warudo: {root}")

    if not args.check and warudo_running():
        print(
            "\nWarudo is running. Close it first -- it holds the scene in memory "
            "and rewrites this file on exit, so an edit made now is thrown away "
            "the moment Warudo quits.",
            file=sys.stderr,
        )
        return 1

    if not TEMPLATE.is_file():
        print(
            f"missing {TEMPLATE.relative_to(REPO)} -- run tools.warudo_export on "
            "the machine that works",
            file=sys.stderr,
        )
        return 1
    template = _load(TEMPLATE)
    packaged = _graph(template, GRAPH_NAME)
    if packaged is None:
        print(
            f"{TEMPLATE.relative_to(REPO)} has no {GRAPH_NAME!r} blueprint -- it "
            "was exported from a Warudo that could not drive the avatar",
            file=sys.stderr,
        )
        return 1

    # --- avatars -----------------------------------------------------------
    copied = install_avatars(characters, dry_run=args.check or args.remove)
    installed = _models(characters)
    verb = "would copy" if args.check else "copied"
    if copied:
        print(f"\navatars: {verb} {len(copied)} into {characters}")
        for name in copied:
            print(f"    {name}")
    else:
        print(f"\navatars: {len(installed)} already in {characters}")

    # --- scene -------------------------------------------------------------
    have_scene = scene_path.is_file()
    scene = _load(scene_path) if have_scene else None
    have_character = scene is not None and _asset(scene, "Character 1") is not None
    replace = args.replace_scene or not have_scene or not have_character

    if args.remove:
        existing = _graph(scene, GRAPH_NAME) if scene is not None else None
        if scene is None or existing is None:
            print(f"\nscene: no {GRAPH_NAME!r} blueprint to remove")
            return 0
        _backup(scene_path, ".pre-remove")
        gone = existing["id"]
        scene["graphs"] = [g for g in scene["graphs"] if g.get("name") != GRAPH_NAME]
        _drop_from_hierarchy(scene.get("graphHierarchy"), gone)
        scene_path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        print(f"\nscene: removed the {GRAPH_NAME!r} blueprint")
        return 0

    if args.check:
        print(f"\nscene: {scene_path}")
        if not have_scene:
            print("    absent -- the packaged scene would be installed whole")
        elif not have_character:
            print("    no Character 1 -- the packaged scene would be installed whole")
        else:
            here = _graph(scene, GRAPH_NAME) if scene is not None else None
            state = (
                f"present, {len(here.get('nodes') or {})} nodes"
                if here
                else "ABSENT -- the blueprint would be grafted in"
            )
            print(f"    Character 1 present; {GRAPH_NAME!r} blueprint {state}")
        print(f"\nwebsocket: {port_status()}")
        return 0

    scene_path.parent.mkdir(parents=True, exist_ok=True)
    if replace or scene is None:
        if have_scene:
            _backup(scene_path, ".pre-narrator")
        scene_path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
        graph = packaged
        why = "no scene" if not have_scene else "no Character 1"
        print(f"\nscene: installed the packaged scene whole ({why})")
    else:
        _backup(scene_path, ".pre-narrator")
        scene, notes = graft(scene, template)
        graph = _graph(scene, GRAPH_NAME) or packaged
        scene_path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        print(f"\nscene: grafted the {GRAPH_NAME!r} blueprint into the existing scene")
        for note in notes:
            print(f"    - {note}")

    print(f"    {scene_path}")
    print(
        f"    {len(graph.get('nodes') or {})} nodes, "
        f"{len(graph.get('dataConnections') or [])} data + "
        f"{len(graph.get('flowConnections') or [])} flow connections"
    )
    print("\n".join(describe(graph)))

    print(f"\nwebsocket: {port_status()}")
    print(
        "\nNext:\n"
        "  1. Start Warudo. The scene loads with the blueprint already in it --\n"
        "     Blueprints -> narrator, and it should be enabled.\n"
        "  2. Turn Warudo's own lip sync OFF for the character, or it will fight\n"
        "     the narrator's visemes and the mouth will jitter.\n"
        "  3. Confirm the bridge:  python -m narrator.main --dry-run --replay "
        "--validate-only\n"
        "  4. Watch the mouth:     python -m narrator.main --replay --speed 1"
    )
    return 0


def _backup(path: Path, suffix: str) -> None:
    """Keep a rollback copy. A half-loaded scene is re-saved stripped on exit."""
    backup = path.with_suffix(f".json{suffix}")
    if not backup.exists():
        shutil.copy2(path, backup)
        print(f"    backup -> {backup.name}")


if __name__ == "__main__":
    raise SystemExit(main())
