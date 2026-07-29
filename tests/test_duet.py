"""Building the two-character stage inside Warudo's scene file."""

from __future__ import annotations

import json

import pytest

from narrator.avatar import duet
from narrator.config import AvatarEntry

CHAR_ID = "29399e49-1c26-41bb-b40f-294dc0590f68"
CAM_ID = "bc77a8de-5252-4bb9-bbfa-ec65adb66c66"

TRANSFORM = json.dumps(
    {
        "dataInputs": {
            "Position": {"value": json.dumps({"x": 0.0, "y": 0.0, "z": 0.0})},
            "Rotation": {"value": json.dumps({"x": 0.0, "y": 0.0, "z": 0.0})},
        }
    }
)


def ws_node(action: str) -> dict:
    return {
        "typeId": duet.WEBSOCKET_ACTION,
        "name": "ON_WEBSOCKET_ACTION",
        "positionY": 0.0,
        "dataInputs": {
            "Action": {"value": json.dumps(action)},
            "DataType": {"value": '{"label":"Float","value":3}'},
        },
    }


def bs_node(blendshape: str) -> dict:
    return {
        "typeId": duet.SET_BLENDSHAPE,
        "name": "SET_CHARACTER_BLENDSHAPE",
        "positionY": 0.0,
        "dataInputs": {
            "Character": {"value": json.dumps({"id": CHAR_ID, "name": "Character 1"})},
            "BlendShape": {"value": json.dumps(blendshape)},
            "Value": {"value": "0.0"},
        },
    }


@pytest.fixture
def scene_file(tmp_path):
    """A scene shaped like the real one: five viseme pairs, one character."""
    nodes: dict = {}
    data_connections = []
    flow_connections = []
    for i, (vowel, shape) in enumerate(duet.VISEMES):
        wid, bid = f"ws-{i}", f"bs-{i}"
        # Each node repeats its own id inside itself, exactly as Warudo writes
        # them. That field is what the graph is actually built from, so a
        # fixture without it cannot catch a clone that kept the original's id.
        nodes[wid] = ws_node(f"viseme_{vowel}") | {"id": wid}
        nodes[bid] = bs_node(shape) | {"id": bid}
        data_connections.append(
            {
                "outputNode": wid,
                "inputNode": bid,
                "outputPort": "FloatData",
                "inputPort": "Value",
            }
        )
        flow_connections.append(
            {
                "outputNode": wid,
                "inputNode": bid,
                "outputPort": "Exit",
                "inputPort": "Enter",
            }
        )

    scene = {
        "assets": [
            {
                "id": CHAR_ID,
                "name": "Character 1",
                "dataInputs": {
                    "Source": {"value": '"character://data/Characters/a.vrm"'},
                    "Transform": {"value": TRANSFORM},
                },
            },
            {
                "id": CAM_ID,
                "name": "Camera 1",
                "dataInputs": {
                    "FollowCharacter": {"value": "true"},
                    "Transform": {"value": TRANSFORM},
                },
            },
        ],
        # The real shape: a nested {collapsed, key, children} tree, NOT the
        # flat list it looks like at a glance. An asset missing from this is
        # loaded and then never instantiated.
        "assetHierarchy": {
            "collapsed": False,
            "key": "",
            "children": [
                {
                    "collapsed": False,
                    "key": "Cinematography",
                    "children": [
                        {"collapsed": False, "key": CAM_ID, "children": None}
                    ],
                },
                {
                    "collapsed": False,
                    "key": "Characters",
                    "children": [
                        {"collapsed": False, "key": CHAR_ID, "children": None}
                    ],
                },
            ],
        },
        "graphs": [
            {
                "name": "narrator",
                "nodes": nodes,
                "dataConnections": data_connections,
                "flowConnections": flow_connections,
            }
        ],
    }
    path = tmp_path / "DefaultScene.json"
    path.write_text(json.dumps(scene), encoding="utf-8")
    return path


def entry(file: str, name: str) -> AvatarEntry:
    return AvatarEntry(file=file, label=name)


LEFT = entry("Shipilka.warudo", "Shipilka")
RIGHT = entry("100ava_Jenny.vrm", "Jenny")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def graph_of(scene):
    return next(g for g in scene["graphs"] if g["name"] == "narrator")


def action_of(node):
    return json.loads(node["dataInputs"]["Action"]["value"])


# ---------------------------------------------------------------------------


def test_a_second_character_is_added(scene_file):
    assert duet.apply(LEFT, RIGHT, 1.4, scene_file)
    names = [a["name"] for a in load(scene_file)["assets"]]
    assert "Character 1" in names and "Character 2" in names


def keys_in_tree(node):
    """Every key in the hierarchy tree, depth first."""
    out = [node.get("key", "")]
    for child in node.get("children") or []:
        out.extend(keys_in_tree(child))
    return out


def test_the_second_character_is_added_to_the_scene_tree(scene_file):
    """The bug that made her invisible. `assets` alone is not enough --
    Warudo builds the scene from `assetHierarchy`, so an asset missing from
    the tree is loaded from the file and then never instantiated, with
    nothing in the log to say why."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    second = next(a for a in scene["assets"] if a["name"] == "Character 2")
    assert second["id"] in keys_in_tree(scene["assetHierarchy"])


def test_she_is_placed_beside_the_first_character(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    second = next(a for a in scene["assets"] if a["name"] == "Character 2")
    characters = next(
        n
        for n in scene["assetHierarchy"]["children"]
        if n["key"] == "Characters"
    )
    assert [c["key"] for c in characters["children"]] == [CHAR_ID, second["id"]]


def test_a_scene_orphaned_by_an_earlier_version_gets_repaired(scene_file):
    """Exactly the state the live scene was left in: Character 2 present in
    `assets`, absent from the tree, invisible on stage. Re-running must fix
    it rather than seeing 'she already exists' and skipping."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    second_id = next(a["id"] for a in scene["assets"] if a["name"] == "Character 2")

    # Orphan her, the way the buggy version did.
    characters = next(
        n for n in scene["assetHierarchy"]["children"] if n["key"] == "Characters"
    )
    characters["children"] = [c for c in characters["children"] if c["key"] != second_id]
    scene_file.write_text(json.dumps(scene), encoding="utf-8")
    assert second_id not in keys_in_tree(load(scene_file)["assetHierarchy"])

    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assert second_id in keys_in_tree(load(scene_file)["assetHierarchy"])


def test_re_running_does_not_add_her_to_the_tree_twice(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    keys = keys_in_tree(load(scene_file)["assetHierarchy"])
    assert len(keys) == len(set(keys))


def test_every_asset_appears_in_the_tree(scene_file):
    """A general form of the same rule: nothing in `assets` may be orphaned."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    keys = set(keys_in_tree(scene["assetHierarchy"]))
    orphans = [a["name"] for a in scene["assets"] if a["id"] not in keys]
    assert not orphans, f"assets missing from the scene tree: {orphans}"


def test_a_scene_with_no_hierarchy_still_writes_the_character(scene_file):
    scene = load(scene_file)
    del scene["assetHierarchy"]
    scene_file.write_text(json.dumps(scene), encoding="utf-8")
    assert duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assert any(a["name"] == "Character 2" for a in load(scene_file)["assets"])


def test_the_two_characters_get_different_ids(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    chars = [a for a in load(scene_file)["assets"] if a["name"].startswith("Character")]
    assert len({c["id"] for c in chars}) == 2


def test_each_character_loads_its_own_model(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assets = {a["name"]: a for a in load(scene_file)["assets"]}
    first = json.loads(assets["Character 1"]["dataInputs"]["Source"]["value"])
    second = json.loads(assets["Character 2"]["dataInputs"]["Source"]["value"])
    assert first != second
    assert "Shipilka" in first and "Jenny" in second


def placement(scene_file):
    assets = {a["name"]: a for a in load(scene_file)["assets"]}
    out = {}
    for name in ("Character 1", "Character 2", "Camera 1"):
        transform = json.loads(assets[name]["dataInputs"]["Transform"]["value"])
        out[name] = (
            json.loads(transform["dataInputs"]["Position"]["value"]),
            json.loads(transform["dataInputs"]["Rotation"]["value"]),
        )
    return out


def test_they_stand_apart_and_face_each_other(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    place = placement(scene_file)
    assert place["Character 1"][0]["x"] < 0 < place["Character 2"][0]["x"]
    # Turned inward: opposite signs on the yaw.
    assert place["Character 1"][1]["y"] * place["Character 2"][1]["y"] < 0


def test_faces_are_levelled_when_the_models_are_different_heights(scene_file):
    """A chibi beside an adult VRM is nearly a metre of difference. One level
    camera cannot frame both: you get one face and one chest."""
    duet.apply(
        LEFT, RIGHT, 1.4, scene_file, left_focus=0.92, right_focus=1.50, eye_line=1.30
    )
    place = placement(scene_file)
    left_face = place["Character 1"][0]["y"] + 0.92
    right_face = place["Character 2"][0]["y"] + 1.50
    assert left_face == pytest.approx(1.30)
    assert right_face == pytest.approx(1.30)


def test_the_camera_sits_on_the_eye_line_not_above_it(scene_file):
    """Looking down on two hosts reads as surveillance footage."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file, eye_line=1.30)
    position, rotation = placement(scene_file)["Camera 1"]
    assert position["y"] == pytest.approx(1.30)
    assert rotation["x"] < 6.0


def test_the_camera_stops_following_and_frames_both(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    camera = next(a for a in load(scene_file)["assets"] if a["name"] == "Camera 1")
    assert camera["dataInputs"]["FollowCharacter"]["value"] == "false"
    transform = json.loads(camera["dataInputs"]["Transform"]["value"])
    position = json.loads(transform["dataInputs"]["Position"]["value"])
    # Centred between them, and far enough back to hold two people.
    assert position["x"] == 0.0
    assert position["z"] >= 1.8


# ---------------------------------------------------------------------------
# The second mouth
# ---------------------------------------------------------------------------


def test_five_new_viseme_actions_appear_on_the_second_prefix(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    graph = graph_of(load(scene_file))
    actions = {
        action_of(n)
        for n in graph["nodes"].values()
        if n["typeId"] == duet.WEBSOCKET_ACTION
    }
    for vowel, _ in duet.VISEMES:
        assert f"viseme_{vowel}" in actions
        assert f"viseme2_{vowel}" in actions


def test_the_second_mouth_drives_the_second_character(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    graph = graph_of(scene)
    second_id = next(a["id"] for a in scene["assets"] if a["name"] == "Character 2")

    for node in graph["nodes"].values():
        if node["typeId"] != duet.WEBSOCKET_ACTION:
            continue
        action = action_of(node)
        if not action.startswith("viseme2_"):
            continue
        node_id = next(k for k, v in graph["nodes"].items() if v is node)
        sink_id = next(
            c["inputNode"]
            for c in graph["dataConnections"]
            if c["outputNode"] == node_id
        )
        target = json.loads(
            graph["nodes"][sink_id]["dataInputs"]["Character"]["value"]
        )
        assert target["id"] == second_id, f"{action} drives the wrong character"


def test_the_original_mouth_is_left_alone(scene_file):
    """Breaking host A's lip sync to add host B would be a bad trade."""
    before = graph_of(load(scene_file))
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    after = graph_of(load(scene_file))

    for node_id, node in before["nodes"].items():
        assert after["nodes"][node_id] == node

    for connection in before["dataConnections"]:
        assert connection in after["dataConnections"]
    for connection in before["flowConnections"]:
        assert connection in after["flowConnections"]


def test_each_new_pair_gets_both_a_data_and_a_flow_connection(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    graph = graph_of(load(scene_file))
    assert len(graph["dataConnections"]) == 10
    assert len(graph["flowConnections"]) == 10


def test_the_blendshapes_map_one_to_one(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    graph = graph_of(load(scene_file))
    mapping = {}
    for node_id, node in graph["nodes"].items():
        if node["typeId"] != duet.WEBSOCKET_ACTION:
            continue
        action = action_of(node)
        if not action.startswith("viseme2_"):
            continue
        sink_id = next(
            c["inputNode"]
            for c in graph["dataConnections"]
            if c["outputNode"] == node_id
        )
        shape = json.loads(graph["nodes"][sink_id]["dataInputs"]["BlendShape"]["value"])
        mapping[action] = shape
    assert mapping == {f"viseme2_{v}": s for v, s in duet.VISEMES}


def test_every_node_carries_the_id_it_is_filed_under(scene_file):
    """The bug that left the second mouth dead.

    Warudo builds the graph from each node's own `id` field, not from the key
    it sits under in `nodes`. A clone that kept the original's id is not a
    second node at all -- viseme2_aa resolves to the same node as viseme_aa,
    one of them wins, and Character 2 never moves while the scene file reads
    as perfectly wired.
    """
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    graph = graph_of(load(scene_file))
    wrong = {k: n["id"] for k, n in graph["nodes"].items() if n.get("id") != k}
    assert not wrong, f"nodes claiming another node's id: {wrong}"


def test_ids_cloned_by_an_earlier_version_are_repaired(scene_file):
    """The state the live scene was left in. The nodes exist, so the build
    loop calls them done and never touches them again -- without a repair
    pass that scene stays silent forever."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    scene = load(scene_file)
    graph = graph_of(scene)

    # Break them the way the buggy clone did: point each viseme2_ node's id
    # back at the viseme_ node it was copied from.
    stolen = "ws-0"
    broken = next(
        k
        for k, n in graph["nodes"].items()
        if n["typeId"] == duet.WEBSOCKET_ACTION and action_of(n) == "viseme2_aa"
    )
    graph["nodes"][broken]["id"] = stolen
    scene_file.write_text(json.dumps(scene), encoding="utf-8")

    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assert graph_of(load(scene_file))["nodes"][broken]["id"] == broken


# ---------------------------------------------------------------------------
# Safety
# ---------------------------------------------------------------------------


def test_running_twice_does_not_duplicate_anything(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    once = load(scene_file)
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    twice = load(scene_file)

    assert len(once["assets"]) == len(twice["assets"])
    assert len(graph_of(once)["nodes"]) == len(graph_of(twice)["nodes"])
    assert len(graph_of(once)["dataConnections"]) == len(
        graph_of(twice)["dataConnections"]
    )


def test_a_rollback_copy_is_written(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assert scene_file.with_suffix(".json.pre-duet").is_file()


# ---------------------------------------------------------------------------
# Toggling back to one character
# ---------------------------------------------------------------------------


def test_solo_deactivates_the_second_character(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assert duet.set_solo(LEFT, 1.4, scene_file)
    assets = {a["name"]: a for a in load(scene_file)["assets"]}
    assert assets["Character 2"]["active"] is False
    assert assets["Character 1"]["active"] is True


def test_solo_keeps_the_second_characters_wiring(scene_file):
    """Deactivating is cheap; rebuilding five node pairs on every toggle is not."""
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    wired = len(graph_of(load(scene_file))["dataConnections"])
    duet.set_solo(LEFT, 1.4, scene_file)
    assert len(graph_of(load(scene_file))["dataConnections"]) == wired


def test_solo_restores_the_single_character_framing(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    duet.set_solo(LEFT, 1.4, scene_file)
    assets = {a["name"]: a for a in load(scene_file)["assets"]}
    transform = json.loads(assets["Character 1"]["dataInputs"]["Transform"]["value"])
    position = json.loads(transform["dataInputs"]["Position"]["value"])
    # Back to centre, not parked on the left of a two-shot.
    assert position["x"] == LEFT.x


def test_toggling_back_to_podcast_reactivates_her(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    duet.set_solo(LEFT, 1.4, scene_file)
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    assets = {a["name"]: a for a in load(scene_file)["assets"]}
    assert assets["Character 2"]["active"] is True
    assert assets["Character 1"]["active"] is True


def test_toggling_repeatedly_does_not_grow_the_scene(scene_file):
    duet.apply(LEFT, RIGHT, 1.4, scene_file)
    baseline = load(scene_file)
    for _ in range(4):
        duet.set_solo(LEFT, 1.4, scene_file)
        duet.apply(LEFT, RIGHT, 1.4, scene_file)
    after = load(scene_file)
    assert len(after["assets"]) == len(baseline["assets"])
    assert len(graph_of(after)["nodes"]) == len(graph_of(baseline)["nodes"])


def test_solo_on_a_scene_that_was_never_a_duet_is_harmless(scene_file):
    assert duet.set_solo(LEFT, 1.4, scene_file)
    names = [a["name"] for a in load(scene_file)["assets"]]
    assert "Character 2" not in names


def test_a_missing_scene_is_reported_not_raised(tmp_path):
    assert duet.apply(LEFT, RIGHT, 1.4, tmp_path / "nope.json") is False


def test_a_scene_without_the_narrator_graph_is_declined(tmp_path):
    path = tmp_path / "DefaultScene.json"
    path.write_text(
        json.dumps({"assets": [], "graphs": [], "assetHierarchy": []}), encoding="utf-8"
    )
    assert duet.apply(LEFT, RIGHT, 1.4, path) is False


def test_malformed_json_is_declined(tmp_path):
    path = tmp_path / "DefaultScene.json"
    path.write_text("{not json", encoding="utf-8")
    assert duet.apply(LEFT, RIGHT, 1.4, path) is False
