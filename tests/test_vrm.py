"""VRM inspection tests.

The GLB files are built in-memory, so these run without downloading an avatar
and without Unity or Warudo.
"""

from __future__ import annotations

import json
import struct

import pytest

from narrator.avatar.vrm import VrmError, inspect
from narrator.config import Config

GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A


def write_glb(path, gltf: dict) -> None:
    raw = json.dumps(gltf).encode("utf-8")
    raw += b" " * ((4 - len(raw) % 4) % 4)  # chunks are 4-byte aligned
    body = struct.pack("<II", len(raw), CHUNK_JSON) + raw
    header = struct.pack("<III", GLB_MAGIC, 2, 12 + len(body))
    path.write_bytes(header + body)


def vrm0(expressions: list[str], bones: int = 52, bind: bool = True) -> dict:
    """A VRM 0.x model. `bind=False` gives clips that drive nothing -- named
    groups with no morph target and no material value, which two of the four
    avatars shipped with this project turned out to have."""
    binds = [{"mesh": 0, "index": 0, "weight": 100}] if bind else []
    return {
        "meshes": [{}],
        "textures": [{}],
        "extensions": {
            "VRM": {
                "meta": {
                    "title": "Test Model",
                    "author": "Nobody",
                    "licenseName": "CC0",
                    "commercialUssageName": "Allow",
                },
                "humanoid": {"humanBones": [{} for _ in range(bones)]},
                "blendShapeMaster": {
                    "blendShapeGroups": [
                        {"presetName": e, "binds": list(binds)} for e in expressions
                    ]
                },
            }
        },
    }


def vrm1(presets: list[str], custom: list[str] | None = None, bind: bool = True) -> dict:
    clip = {"morphTargetBinds": [{"node": 0, "index": 0, "weight": 1.0}]} if bind else {}
    return {
        "meshes": [{}, {}],
        "textures": [{}],
        "extensions": {
            "VRMC_vrm": {
                "meta": {
                    "name": "Test Model 1.0",
                    "authors": ["Nobody"],
                    "licenseUrl": "https://vrm.dev/licenses/1.0/",
                    "commercialUsage": "personalProfit",
                },
                "humanoid": {"humanBones": {f"bone{i}": {} for i in range(54)}},
                "expressions": {
                    "preset": {name: dict(clip) for name in presets},
                    "custom": {name: dict(clip) for name in (custom or [])},
                },
            }
        },
    }


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_reads_a_vrm0_model(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["A", "I", "U", "E", "O", "Blink", "Joy", "Sorrow"]))
    info = inspect(path)
    assert info.version == "0.x"
    assert info.name == "Test Model"
    assert info.author == "Nobody"
    assert info.commercial == "Allow"
    assert info.humanoid_bones == 52
    assert info.lip_sync_ready


def test_reads_a_vrm1_model(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm1(["aa", "ih", "ou", "ee", "oh", "happy", "relaxed"]))
    info = inspect(path)
    assert info.version == "1.0"
    assert info.name == "Test Model 1.0"
    assert info.lip_sync_ready


def test_viseme_names_match_the_vrm1_spec_exactly(tmp_path):
    """The viseme mapper emits aa/ih/ou/ee/oh, which IS the VRM 1.0 preset
    set. A stock VRM 1.0 needs no custom clips at all."""
    path = tmp_path / "m.vrm"
    write_glb(path, vrm1(["aa", "ih", "ou", "ee", "oh"]))
    status = inspect(path).viseme_status()
    assert status == {"aa": "aa", "ih": "ih", "ou": "ou", "ee": "ee", "oh": "oh"}


def test_vrm0_vowels_map_onto_the_narrator_visemes(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["A", "I", "U", "E", "O"]))
    status = inspect(path).viseme_status()
    assert status == {"aa": "A", "ih": "I", "ou": "U", "ee": "E", "oh": "O"}


def test_lowercase_vrm0_vowels_are_accepted(tmp_path):
    """Real CC0 models in the wild write them lowercase."""
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["a", "i", "u", "e", "o", "blink"]))
    assert inspect(path).lip_sync_ready


# ---------------------------------------------------------------------------
# The verdict the operator acts on
# ---------------------------------------------------------------------------


def test_a_model_without_mouth_shapes_is_rejected(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["Blink", "Joy"]))
    info = inspect(path)
    assert not info.lip_sync_ready
    assert {v for v in info.viseme_status().values() if v} == set()


def test_partial_mouth_shapes_still_fail(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["A", "I"]))
    info = inspect(path)
    assert not info.lip_sync_ready
    assert info.viseme_status()["aa"] == "A"
    assert info.viseme_status()["oh"] is None


def test_emotes_map_onto_standard_presets(tmp_path):
    cfg = Config()
    path = tmp_path / "m.vrm"
    write_glb(
        path,
        vrm1(["aa", "ih", "ou", "ee", "oh", "happy", "relaxed", "surprised", "neutral"]),
    )
    status = inspect(path).emote_status(cfg.warudo.expressions)
    assert status["excited"] == "happy"
    assert status["bored"] == "relaxed"
    assert status["surprised"] == "surprised"
    assert status["alert"] == "surprised"
    assert status["neutral"] == "neutral"


def test_vrm0_emotes_fall_back_to_the_old_preset_names(tmp_path):
    cfg = Config()
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["A", "I", "U", "E", "O", "Joy", "Sorrow", "Fun", "Neutral"]))
    status = inspect(path).emote_status(cfg.warudo.expressions)
    assert status["excited"] == "Joy"
    assert status["bored"] == "Sorrow"
    assert status["alert"] == "Fun"
    assert status["neutral"] == "Neutral"


def test_a_model_with_no_expression_clips_still_lip_syncs(tmp_path):
    """The CC0 100avatars models are exactly this: five vowels and blink, no
    emotions. The mouth works; the emotes simply do nothing."""
    cfg = Config()
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["a", "i", "u", "e", "o", "blink"]))
    info = inspect(path)
    assert info.lip_sync_ready
    assert all(
        clip is None for clip in info.emote_status(cfg.warudo.expressions).values()
    )


def test_vowel_clips_that_bind_to_nothing_are_not_lip_sync_ready(tmp_path):
    """A named clip is not a moving mouth.

    NeonGl_Summer_V2.vrm and NeonGl_EL_BUENO.vrm both ship all five vowels and
    zero morph targets: the clips exist, Warudo lists them, setting one to 1.0
    moves nothing. Reporting those as usable cost an afternoon of debugging a
    lip sync pipeline that was working the whole time.
    """
    path = tmp_path / "m.vrm"
    write_glb(path, vrm0(["a", "i", "u", "e", "o"], bind=False))
    info = inspect(path)

    assert info.viseme_status()["aa"] == "a"  # the name is still there
    assert sorted(info.unbound_visemes()) == ["aa", "ee", "ih", "oh", "ou"]
    assert not info.lip_sync_ready


def test_vrm1_expressions_without_binds_are_not_lip_sync_ready(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, vrm1(["aa", "ih", "ou", "ee", "oh"], bind=False))
    info = inspect(path)

    assert sorted(info.unbound_visemes()) == ["aa", "ee", "ih", "oh", "ou"]
    assert not info.lip_sync_ready


def test_a_material_only_clip_still_counts_as_bound(tmp_path):
    """VRM 0.x lets a clip drive material values instead of morph targets --
    a texture swap for the mouth. That is a real, drivable mouth."""
    path = tmp_path / "m.vrm"
    model = vrm0(["a", "i", "u", "e", "o"], bind=False)
    for group in model["extensions"]["VRM"]["blendShapeMaster"]["blendShapeGroups"]:
        group["materialValues"] = [{"materialName": "Face", "propertyName": "_MainTex"}]
    write_glb(path, model)
    info = inspect(path)

    assert info.unbound_visemes() == []
    assert info.lip_sync_ready


# ---------------------------------------------------------------------------
# Bad input
# ---------------------------------------------------------------------------


def test_not_a_glb(tmp_path):
    path = tmp_path / "m.vrm"
    path.write_bytes(b"PK\x03\x04this is a zip pretending to be a vrm")
    with pytest.raises(VrmError, match="not a GLB"):
        inspect(path)


def test_gltf_without_a_vrm_extension(tmp_path):
    path = tmp_path / "m.vrm"
    write_glb(path, {"meshes": [], "extensions": {}})
    with pytest.raises(VrmError, match="no VRM extension"):
        inspect(path)


def test_missing_file(tmp_path):
    with pytest.raises(VrmError, match="no such file"):
        inspect(tmp_path / "nope.vrm")


def test_truncated_file(tmp_path):
    path = tmp_path / "m.vrm"
    path.write_bytes(b"glTF")
    with pytest.raises(VrmError):
        inspect(path)
