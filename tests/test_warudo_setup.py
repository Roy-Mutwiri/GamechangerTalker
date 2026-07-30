"""Grafting the narrator blueprint into somebody else's Warudo scene.

The packaged scene in `warudo/` is the real one, so these tests run against it
rather than a fixture: what they are actually checking is that the file we ship
still describes a working bridge, and that installing it onto a scene whose
assets carry different ids does not leave a single node pointing at the ids
from this machine.
"""

from __future__ import annotations

import json

import pytest

from narrator.avatar import install
from tools import warudo_export, warudo_setup

VISEME_ACTIONS = {"viseme_aa", "viseme_ih", "viseme_ou", "viseme_ee", "viseme_oh"}


@pytest.fixture(scope="module")
def template() -> dict:
    if not warudo_setup.TEMPLATE.is_file():
        pytest.skip("warudo/DefaultScene.json is not committed")
    return warudo_setup._load(warudo_setup.TEMPLATE)


@pytest.fixture
def host_scene() -> dict:
    """A scene like Warudo's Onboarding leaves behind: one character, a camera,
    no blueprint, and ids that have nothing to do with ours."""
    return {
        "assets": [
            {"id": "aaaa-1", "name": "Character 1", "dataInputs": {}},
            {"id": "aaaa-2", "name": "Camera 1", "dataInputs": {}},
        ],
        "graphs": [],
        "assetHierarchy": {"collapsed": False, "key": "", "children": []},
        "graphHierarchy": {"collapsed": False, "key": "", "children": []},
    }


# ---------------------------------------------------------------------------
# What we ship
# ---------------------------------------------------------------------------


def test_packaged_scene_can_drive_a_mouth(template):
    """Five vowels, an emote and both camera channels, or the stream is mute."""
    graph = warudo_setup._graph(template, warudo_setup.GRAPH_NAME)
    assert graph is not None

    actions = {
        warudo_setup._string(node, "Action")
        for node in graph["nodes"].values()
        if node.get("name") == "ON_WEBSOCKET_ACTION"
    }
    assert actions >= VISEME_ACTIONS
    assert {"emote", "cam_pos", "cam_rot", "avatar", "reload"} <= actions


def test_packaged_scene_carries_no_machine_state(template):
    """The export strips the mocap rig and the sound card. If either comes
    back, the next clone inherits hardware it does not have."""
    for asset in template["assets"]:
        for key in ("TrackingAssetIds", "TrackingGraphIds"):
            field = (asset.get("dataInputs") or {}).get(key)
            if field is not None:
                assert json.loads(field["value"]) == []

    for graph in template["graphs"]:
        for node in (graph.get("nodes") or {}).values():
            mic = (node.get("dataInputs") or {}).get("Microphone")
            if mic is not None:
                assert json.loads(mic["value"]) == ""


def test_every_node_id_matches_its_key(template):
    """Warudo builds the graph from each node's own `id`, not from the key it
    is filed under. A disagreement is the silent failure duet.py documents."""
    for graph in template["graphs"]:
        for key, node in (graph.get("nodes") or {}).items():
            assert node.get("id") in (None, key)


def test_hierarchies_name_only_live_things(template):
    """An entry for something that no longer exists half-loads the scene."""
    assets = {a["id"] for a in template["assets"]}
    graphs = {g["id"] for g in template["graphs"]}
    for tree, live in (
        (template["assetHierarchy"], assets),
        (template["graphHierarchy"], graphs),
    ):
        for key in warudo_setup._keys(tree):
            if warudo_export._looks_like_guid(key):
                assert key in live


# ---------------------------------------------------------------------------
# Grafting it onto a stranger's scene
# ---------------------------------------------------------------------------


def test_graft_repoints_every_asset_reference(host_scene, template):
    scene, _ = warudo_setup.graft(host_scene, template)
    graph = warudo_setup._graph(scene, warudo_setup.GRAPH_NAME)
    here = {a["name"]: a["id"] for a in scene["assets"]}

    for node in graph["nodes"].values():
        for field in (node.get("dataInputs") or {}).values():
            value = field.get("value")
            if not isinstance(value, str) or '"name"' not in value:
                continue
            try:
                reference = json.loads(value)
            except ValueError:
                continue
            if isinstance(reference, dict) and reference.get("name") in here:
                assert reference["id"] == here[reference["name"]]


def test_graft_drops_pairs_whose_character_is_missing(host_scene, template):
    """The host scene has no Character 2. Its viseme2_ nodes must not survive:
    a blendshape node aimed at a character that is not there throws on every
    frame the narrator sends."""
    scene, notes = warudo_setup.graft(host_scene, template)
    graph = warudo_setup._graph(scene, warudo_setup.GRAPH_NAME)

    actions = {
        warudo_setup._string(node, "Action")
        for node in graph["nodes"].values()
        if node.get("name") == "ON_WEBSOCKET_ACTION"
    }
    assert actions >= VISEME_ACTIONS
    assert not any(action.startswith("viseme2_") for action in actions)
    assert any("Character 2" in note for note in notes)


def test_graft_leaves_no_dangling_connections(host_scene, template):
    scene, _ = warudo_setup.graft(host_scene, template)
    graph = warudo_setup._graph(scene, warudo_setup.GRAPH_NAME)
    ids = set(graph["nodes"])
    for connection in graph["dataConnections"] + graph["flowConnections"]:
        assert connection["outputNode"] in ids
        assert connection["inputNode"] in ids


def test_graft_puts_the_blueprint_in_the_tree(host_scene, template):
    """A graph missing from graphHierarchy runs but never appears in the
    editor, which reads exactly like it was never installed."""
    scene, _ = warudo_setup.graft(host_scene, template)
    graph = warudo_setup._graph(scene, warudo_setup.GRAPH_NAME)
    assert graph["id"] in warudo_setup._keys(scene["graphHierarchy"])


def test_graft_twice_is_the_same_as_once(host_scene, template):
    once, _ = warudo_setup.graft(host_scene, template)
    first = json.dumps(once, sort_keys=True)
    twice, _ = warudo_setup.graft(once, template)
    assert json.dumps(twice, sort_keys=True) == first
    assert sum(g["name"] == warudo_setup.GRAPH_NAME for g in twice["graphs"]) == 1


def test_graft_keeps_the_hosts_own_blueprints(host_scene, template):
    host_scene["graphs"].append({"id": "theirs", "name": "Expression Key Bindings"})
    scene, _ = warudo_setup.graft(host_scene, template)
    assert {g["name"] for g in scene["graphs"]} == {
        "Expression Key Bindings",
        warudo_setup.GRAPH_NAME,
    }


# ---------------------------------------------------------------------------
# Finding the install
#
# One search, shared by the setup tool, the avatar picker and the avatar
# switch. Anything that only knows the default Steam path works on the machine
# it was written on and quietly does nothing on a machine with games on D:.
# ---------------------------------------------------------------------------


@pytest.fixture
def nowhere(monkeypatch):
    """No Warudo anywhere: no env var, no default install, no Steam library."""
    monkeypatch.delenv("WARUDO_ROOT", raising=False)
    monkeypatch.setattr(install, "DEFAULT_ROOTS", ())
    monkeypatch.setattr(install, "_steam_libraries", list)
    return


def fake_install(base) -> object:
    (base / "Warudo_Data" / "StreamingAssets" / "Characters").mkdir(parents=True)
    (base / "Warudo_Data" / "StreamingAssets" / "Scenes").mkdir(parents=True)
    return base


def test_find_warudo_accepts_the_data_folder(tmp_path, nowhere):
    """Handed Warudo_Data instead of the folder above it, still find the root."""
    root = fake_install(tmp_path / "Warudo")
    assert warudo_setup.find_warudo(root) == root
    assert warudo_setup.find_warudo(root / "Warudo_Data") == root


def test_find_warudo_returns_none_when_absent(tmp_path, nowhere):
    assert warudo_setup.find_warudo(tmp_path / "nowhere") is None
    assert install.characters_folder() is None
    assert install.scene_path() is None


def test_env_var_beats_the_default_path(tmp_path, nowhere, monkeypatch):
    """WARUDO_ROOT is the escape hatch for a non-Steam or relocated install,
    so it has to win against a default install that also exists."""
    default = fake_install(tmp_path / "Default")
    chosen = fake_install(tmp_path / "Elsewhere")
    monkeypatch.setattr(install, "DEFAULT_ROOTS", (default,))
    monkeypatch.setenv("WARUDO_ROOT", str(chosen))
    assert install.root() == chosen
    assert (
        install.characters_folder()
        == chosen / "Warudo_Data" / "StreamingAssets" / "Characters"
    )


def test_a_second_steam_library_is_searched(tmp_path, nowhere, monkeypatch):
    """Steam offers a second library on the first big install, so games on D:
    is the normal case. Only knowing Program Files misses it entirely."""
    library = tmp_path / "SteamLibrary"
    root = fake_install(library / "steamapps" / "common" / "Warudo")
    monkeypatch.setattr(install, "_steam_libraries", lambda: [root])
    assert install.root() == root


def test_scene_path_wants_a_file_not_a_folder(tmp_path, nowhere, monkeypatch):
    """An install with no saved scene yet reports None rather than a path that
    is not there -- that is what tells warudo_setup to install ours whole."""
    root = fake_install(tmp_path / "Warudo")
    monkeypatch.setenv("WARUDO_ROOT", str(root))
    assert install.scene_path() is None
    scene = root / "Warudo_Data/StreamingAssets/Scenes" / install.SCENE_FILE
    scene.write_text("{}", encoding="utf-8")
    assert install.scene_path() == scene


def test_install_avatars_copies_the_roster(tmp_path):
    characters = tmp_path / "Characters"
    copied = warudo_setup.install_avatars(characters, dry_run=False)
    on_disk = warudo_setup._models(characters)
    assert copied and set(copied) <= set(on_disk)
    # Second run has nothing left to do: same size, already there.
    assert warudo_setup.install_avatars(characters, dry_run=False) == []
