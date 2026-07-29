"""Put two characters on the stage and give each its own mouth.

The single-character path in scene.py edits assets that Warudo already has.
A second host has nothing to edit: the scene ships with one Character and one
set of viseme nodes wired to it, so both the asset and its half of the
blueprint have to be built.

The blueprint is the interesting part. Warudo has no JSON-parsing node, so each
viseme is its own websocket action matched by its own node:

    ON_WEBSOCKET_ACTION("viseme_aa") --FloatData--> SET_CHARACTER_BLENDSHAPE(Character 1, "A")

Five of those pairs drive the mouth. A second character needs five more, listening
on a different action name and pointing at a different character id:

    ON_WEBSOCKET_ACTION("viseme2_aa") --FloatData--> SET_CHARACTER_BLENDSHAPE(Character 2, "A")

That is the whole trick, and it is why two hosts can share one websocket
connection without their mouths bleeding into each other: the prefix on the
action name decides whose face moves. Everything is cloned from the nodes
already in the scene rather than built from a hand-written template, so
whatever Warudo version wrote them stays consistent -- node schemas change
between releases and a literal would rot.

Nothing here is destructive. A rollback copy is written before the first edit,
and re-running is idempotent: existing Character 2 nodes are updated in place
rather than duplicated.
"""

from __future__ import annotations

import copy
import json
import logging
import shutil
import uuid
from pathlib import Path
from typing import Any

from narrator.config import AvatarEntry

log = logging.getLogger(__name__)

SECOND_NAME = "Character 2"
SECOND_PREFIX = "viseme2_"

# typeIds, read out of the shipped scene. Warudo identifies node kinds by uuid
# rather than by name, and these are stable across scenes on one install.
WEBSOCKET_ACTION = "61445859-8471-4baa-88d6-b186773bd40d"
SET_BLENDSHAPE = "979f57cc-4329-4e21-8b99-ddde1d72a0ea"

# The narrator's five vowel actions, and the VRM blendshape each drives.
VISEMES = (("aa", "A"), ("ih", "I"), ("ou", "U"), ("ee", "E"), ("oh", "O"))


def apply(
    left: AvatarEntry,
    right: AvatarEntry,
    focus_height: float,
    path: Path | None = None,
    *,
    left_focus: float = 0.0,
    right_focus: float = 0.0,
    separation: float = 0.62,
    distance: float = 1.9,
    pitch: float = 2.0,
    eye_line: float = 1.30,
) -> bool:
    """Two characters, angled toward each other, in one two-shot.

    Returns False rather than raising. A stage that will not build is a reason
    to fall back to one character, not to take the stream down.
    """
    from narrator.avatar import scene as scene_tools

    path = path or scene_tools.scene_path()
    if path is None:
        log.warning("no Warudo scene found; two-host stage skipped")
        return False

    try:
        scene = json.loads(path.read_text(encoding="utf-8-sig"))
        first = _asset(scene, "Character 1")
        camera = _asset(scene, "Camera 1")
        graph = _graph(scene, "narrator")
        if first is None or camera is None or graph is None:
            log.warning(
                "scene needs Character 1, Camera 1 and the narrator blueprint; "
                "two-host stage skipped"
            )
            return False

        shutil.copyfile(path, path.with_suffix(".json.pre-duet"))

        second = _ensure_second_character(scene, first)
        # A previous solo switch may have parked her; bring her back.
        first["active"] = True
        second["active"] = True

        # Both turned inward, as two people at one desk are, and both raised or
        # dropped so their faces land on the same line. A chibi beside an
        # adult-proportioned VRM is nearly a metre of difference otherwise, and
        # one level camera cannot frame both -- you get one face and one chest.
        lf = left_focus or focus_height
        rf = right_focus or focus_height
        _place(
            first,
            left,
            x=-separation,
            y=eye_line - lf,
            facing=abs(left.facing) or 14.0,
        )
        _place(
            second,
            right,
            x=separation,
            y=eye_line - rf,
            facing=-(abs(right.facing) or 14.0),
        )

        _two_shot(camera, eye_line, distance=distance, pitch=pitch)
        _ensure_second_mouth(graph, second["id"])

        path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        log.warning("could not build the two-host stage: %s", exc)
        return False

    log.info("two-host stage: %s (left) and %s (right)", left.name, right.name)
    return True


# ---------------------------------------------------------------------------
# The second character
# ---------------------------------------------------------------------------


def set_solo(entry: AvatarEntry, focus_height: float, path: Path | None = None) -> bool:
    """Back to one character on stage.

    The second character is deactivated rather than deleted. Rebuilding her
    means re-cloning five node pairs and reloading a model; flipping `active`
    costs nothing and makes the toggle instant in both directions. Her viseme
    nodes stay wired and simply stop being sent to.
    """
    from narrator.avatar import scene as scene_tools

    path = path or scene_tools.scene_path()
    if path is None:
        return False

    try:
        scene = json.loads(path.read_text(encoding="utf-8-sig"))
        second = _asset(scene, SECOND_NAME)
        if second is not None:
            second["active"] = False
        # Everything else -- the remaining character's placement and the shot
        # the camera takes -- is exactly the single-avatar path.
        first = _asset(scene, "Character 1")
        camera = _asset(scene, "Camera 1")
        if first is None or camera is None:
            return False

        first["active"] = True
        first["dataInputs"]["Source"]["value"] = json.dumps(entry.source)
        scene_tools._set_transform(
            first,
            {"x": entry.x, "y": entry.y, "z": entry.z},
            {"x": 0.0, "y": entry.facing, "z": 0.0},
        )
        position, rotation = scene_tools.orbit(entry, focus_height)
        camera["dataInputs"]["FollowCharacter"]["value"] = "false"
        scene_tools._set_transform(camera, position, rotation)

        path.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError, KeyError) as exc:
        log.warning("could not return to a single character: %s", exc)
        return False

    log.info("solo stage: %s", entry.name)
    return True


def _ensure_second_character(
    scene: dict[str, Any], template: dict[str, Any]
) -> dict[str, Any]:
    """Clone Character 1 into a Character 2, or return the existing one."""
    existing = _asset(scene, SECOND_NAME)
    if existing is None:
        existing = copy.deepcopy(template)
        existing["id"] = str(uuid.uuid4())
        existing["name"] = SECOND_NAME
        scene["assets"].append(existing)
        log.info("added %s (%s)", SECOND_NAME, existing["id"][:8])

    # Outside the branch above on purpose. A scene written by an earlier
    # version has the character in `assets` and nowhere in the tree, which is
    # precisely the state where she does not appear -- and skipping this for
    # an existing character would leave that scene broken forever.
    _add_to_hierarchy(scene, template["id"], existing["id"])
    return existing


def _add_to_hierarchy(scene: dict[str, Any], sibling_id: str, new_id: str) -> None:
    """Put the new character beside the existing one in the scene tree.

    This is not cosmetic. `assetHierarchy` is what Warudo builds the scene
    from, so an asset present in `assets` but absent from the tree is loaded
    from the file and then never instantiated -- the character simply does not
    appear, with nothing in the log to say why.

    The tree is a nested {collapsed, key, children} structure, NOT the flat
    list it looks like at a glance. Getting that wrong is exactly how the
    second host stayed invisible.
    """
    root = scene.get("assetHierarchy")
    if not isinstance(root, dict):
        log.warning("scene has no assetHierarchy tree; %s may not appear", SECOND_NAME)
        return

    # Whole-tree presence check BEFORE inserting anywhere. Checking as we walk
    # is not enough: the sibling comes first in the list, so we would insert
    # beside it and only then reach the copy already sitting further along.
    if new_id in _keys(root):
        return

    node = {"collapsed": False, "key": new_id, "children": None}

    def insert(branch: dict[str, Any]) -> bool:
        children = branch.get("children") or []
        for i, child in enumerate(children):
            if child.get("key") == sibling_id:
                children.insert(i + 1, node)
                branch["children"] = children
                return True
            if insert(child):
                return True
        return False

    if insert(root):
        return
    # No sibling to sit beside: fall back to a "Characters" group, then root.
    for child in root.get("children") or []:
        if child.get("key") == "Characters":
            child["children"] = (child.get("children") or []) + [node]
            return
    root["children"] = (root.get("children") or []) + [node]


def _place(
    character: dict[str, Any],
    entry: AvatarEntry,
    x: float,
    y: float,
    facing: float,
) -> None:
    from narrator.avatar import scene as scene_tools

    character["dataInputs"]["Source"]["value"] = json.dumps(entry.source)
    scene_tools._set_transform(
        character,
        {"x": round(x, 4), "y": round(y, 4), "z": entry.z},
        {"x": 0.0, "y": round(facing, 3), "z": 0.0},
    )


def _two_shot(
    camera: dict[str, Any], eye_line: float, distance: float, pitch: float
) -> None:
    """One camera holding both, centred on the gap between them.

    Level with their faces rather than above them: a two-shot looking down on
    a pair of hosts reads as surveillance footage. The characters have already
    been raised or dropped so both faces sit on `eye_line`.
    """
    from narrator.avatar import scene as scene_tools

    camera["dataInputs"]["FollowCharacter"]["value"] = "false"
    scene_tools._set_transform(
        camera,
        {"x": 0.0, "y": round(eye_line, 4), "z": round(distance, 4)},
        {"x": round(pitch, 3), "y": 180.0, "z": 0.0},
    )


# ---------------------------------------------------------------------------
# The second mouth
# ---------------------------------------------------------------------------


def _ensure_second_mouth(graph: dict[str, Any], character_id: str) -> None:
    """Five action/blendshape pairs on the viseme2_ prefix, bound to Character 2."""
    nodes = graph["nodes"]
    _restamp_ids(nodes)
    existing = {
        _action_name(node): nid
        for nid, node in nodes.items()
        if node.get("typeId") == WEBSOCKET_ACTION
    }

    for vowel, blendshape in VISEMES:
        action = f"{SECOND_PREFIX}{vowel}"
        if action in existing:
            # Already built. Re-point it, in case the character was rebuilt
            # with a new id, and leave the wiring alone.
            _rebind_downstream(graph, existing[action], character_id)
            continue

        source = _find_action(nodes, f"viseme_{vowel}")
        sink = _downstream(graph, nodes, source) if source else None
        if source is None or sink is None:
            log.warning("no viseme_%s pair to clone; skipping %s", vowel, action)
            continue

        ws_id, ws_node = _clone(nodes, source)
        ws_node["dataInputs"]["Action"]["value"] = json.dumps(action)
        _offset(ws_node, dy=420.0)

        bs_id, bs_node = _clone(nodes, sink)
        bs_node["dataInputs"]["Character"]["value"] = json.dumps(
            {"id": character_id, "name": SECOND_NAME}
        )
        bs_node["dataInputs"]["BlendShape"]["value"] = json.dumps(blendshape)
        bs_node["dataInputs"]["Value"]["value"] = "0.0"
        _offset(bs_node, dy=420.0)

        graph["dataConnections"].append(
            {
                "outputNode": ws_id,
                "inputNode": bs_id,
                "outputPort": "FloatData",
                "inputPort": "Value",
            }
        )
        graph["flowConnections"].append(
            {
                "outputNode": ws_id,
                "inputNode": bs_id,
                "outputPort": "Exit",
                "inputPort": "Enter",
            }
        )
        log.info("wired %s -> %s.%s", action, SECOND_NAME, blendshape)


def _clone(nodes: dict[str, Any], node: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """A copy under a fresh id -- in the map key *and* inside the node.

    A node repeats its own id in an `id` field, and Warudo builds the graph
    from that field rather than from the key the node is filed under. A deep
    copy that keeps the original's id is therefore not a second node at all:
    viseme_aa and viseme2_aa resolve to the same node, one of them wins, and
    the second character's mouth never moves -- while the scene file reads as
    perfectly wired, which is what makes this one expensive to find.
    """
    new_id = str(uuid.uuid4())
    clone = copy.deepcopy(node)
    if "id" in clone:
        clone["id"] = new_id
    nodes[new_id] = clone
    return new_id, clone


def _restamp_ids(nodes: dict[str, Any]) -> int:
    """Make every node's `id` agree with the key it is filed under.

    Repairs scenes written before _clone stamped the id, where each viseme2_
    node still claims to be the viseme_ node it was copied from. Those scenes
    stay broken forever otherwise: the nodes exist, so the build loop below
    finds them, calls them done, and never touches them again.

    Connections reference the key, and every node Warudo itself wrote already
    has id == key, so this is a no-op on anything but a bad clone.
    """
    fixed = 0
    for key, node in nodes.items():
        if node.get("id") is not None and node["id"] != key:
            node["id"] = key
            fixed += 1
    if fixed:
        log.info("repaired %d cloned node(s) that carried a duplicate id", fixed)
    return fixed


def _offset(node: dict[str, Any], dy: float) -> None:
    """Drop the clone below the original so the graph stays readable by hand."""
    for key in ("positionY", "y"):
        if isinstance(node.get(key), (int, float)):
            node[key] = node[key] + dy
            return


def _action_name(node: dict[str, Any]) -> str:
    raw = (node.get("dataInputs") or {}).get("Action", {}).get("value")
    if not isinstance(raw, str):
        return ""
    try:
        return str(json.loads(raw))
    except ValueError:
        return raw.strip('"')


def _find_action(nodes: dict[str, Any], action: str) -> dict[str, Any] | None:
    for node in nodes.values():
        if node.get("typeId") == WEBSOCKET_ACTION and _action_name(node) == action:
            return node
    return None


def _downstream(
    graph: dict[str, Any], nodes: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any] | None:
    """The blendshape node a websocket action feeds."""
    source_id = next((nid for nid, n in nodes.items() if n is source), None)
    if source_id is None:
        return None
    for connection in graph["dataConnections"]:
        if connection["outputNode"] != source_id:
            continue
        target = nodes.get(connection["inputNode"])
        if target is not None and target.get("typeId") == SET_BLENDSHAPE:
            return target
    return None


def _rebind_downstream(graph: dict[str, Any], ws_id: str, character_id: str) -> None:
    nodes = graph["nodes"]
    for connection in graph["dataConnections"]:
        if connection["outputNode"] != ws_id:
            continue
        target = nodes.get(connection["inputNode"])
        if target is not None and target.get("typeId") == SET_BLENDSHAPE:
            target["dataInputs"]["Character"]["value"] = json.dumps(
                {"id": character_id, "name": SECOND_NAME}
            )


def _keys(node: dict[str, Any]) -> set[str]:
    """Every key in a hierarchy branch, depth first."""
    found = {str(node.get("key", ""))}
    for child in node.get("children") or []:
        found |= _keys(child)
    return found


def _asset(scene: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((a for a in scene.get("assets", []) if a.get("name") == name), None)


def _graph(scene: dict[str, Any], name: str) -> dict[str, Any] | None:
    return next((g for g in scene.get("graphs", []) if g.get("name") == name), None)
