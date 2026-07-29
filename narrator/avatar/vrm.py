"""VRM file inspection.

A .vrm is a glTF binary (GLB) with a VRM extension block. Everything the
narrator needs to know about an avatar -- which mouth shapes it has, which
expressions it has, whether it is VRM 0.x or 1.0 -- is in the JSON chunk at
the front of the file. That is readable with the standard library alone, so
checking a model you just downloaded costs nothing and needs no Unity.

What matters for this pipeline:

  * the five lip-sync shapes. VRM 1.0 calls them aa/ih/ou/ee/oh, which is
    exactly what the viseme mapper emits; VRM 0.x calls them A/I/U/E/O.
  * the emote expressions, mapped onto standard presets so a stock avatar
    works without anyone hand-authoring clips.

A model missing the mouth shapes cannot be lip-synced by anything, Warudo's
own lip sync included. Better to find that out before building the blueprint.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GLB_MAGIC = 0x46546C67  # "glTF"
CHUNK_JSON = 0x4E4F534A  # "JSON"

# The five lip-sync presets, per VRM version.
VISEME_PRESETS_V1 = ("aa", "ih", "ou", "ee", "oh")
VISEME_PRESETS_V0 = ("a", "i", "u", "e", "o")

# narrator viseme -> the preset name in each VRM version
VISEME_MAP = {
    "aa": ("aa", "A"),
    "ih": ("ih", "I"),
    "ou": ("ou", "U"),
    "ee": ("ee", "E"),
    "oh": ("oh", "O"),
}


class VrmError(ValueError):
    pass


@dataclass
class VrmInfo:
    path: Path
    version: str = "unknown"  # "0.x" | "1.0"
    name: str = ""
    author: str = ""
    license_url: str = ""
    commercial: str = ""
    expressions: list[str] = field(default_factory=list)
    unbound: set[str] = field(default_factory=set)
    humanoid_bones: int = 0
    mesh_count: int = 0
    texture_count: int = 0
    size_mb: float = 0.0

    # -- what the narrator cares about --------------------------------------

    def viseme_status(self) -> dict[str, str | None]:
        """narrator viseme -> the clip name found on this model, or None."""
        lowered = {e.lower(): e for e in self.expressions}
        found: dict[str, str | None] = {}
        for viseme, (v1, v0) in VISEME_MAP.items():
            found[viseme] = lowered.get(v1.lower()) or lowered.get(v0.lower())
        return found

    def unbound_visemes(self) -> list[str]:
        """Visemes whose clip exists by name but drives nothing.

        A VRM clip is a *group*: it exists as a name, and separately it lists
        what it moves -- morph target binds, or material values. A group with
        neither is a label attached to nothing. The clip shows up in every
        listing, Warudo offers it in its dropdowns, and setting it to 1.0 does
        exactly nothing. Two of the four avatars shipped with this project are
        like that, which cost an afternoon before it was noticed.
        """
        lowered = {name.lower() for name in self.unbound}
        return [
            viseme
            for viseme, clip in self.viseme_status().items()
            if clip and clip.lower() in lowered
        ]

    @property
    def lip_sync_ready(self) -> bool:
        return all(self.viseme_status().values()) and not self.unbound_visemes()

    def emote_status(self, mapping: dict[str, list[str]]) -> dict[str, str | None]:
        """narrator emote -> the expression clip that will drive it."""
        lowered = {e.lower(): e for e in self.expressions}
        found: dict[str, str | None] = {}
        for emote, candidates in mapping.items():
            hit = None
            for candidate in [*candidates, emote]:
                if candidate.lower() in lowered:
                    hit = lowered[candidate.lower()]
                    break
            found[emote] = hit
        return found


def read_glb_json(path: Path) -> dict[str, Any]:
    """The glTF JSON chunk of a .vrm, without loading the binary payload."""
    with path.open("rb") as fh:
        header = fh.read(12)
        if len(header) < 12:
            raise VrmError(f"{path.name} is too small to be a VRM file")
        magic, _version, _length = struct.unpack("<III", header)
        if magic != GLB_MAGIC:
            raise VrmError(
                f"{path.name} is not a GLB/VRM file (bad magic). VRoid and most "
                "tools export .vrm as GLB; a .vrm that is really a zip or an "
                "fbx will not load in Warudo either."
            )
        chunk_header = fh.read(8)
        if len(chunk_header) < 8:
            raise VrmError(f"{path.name} has no chunks")
        chunk_length, chunk_type = struct.unpack("<II", chunk_header)
        if chunk_type != CHUNK_JSON:
            raise VrmError(f"{path.name}: first chunk is not JSON")
        raw = fh.read(chunk_length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VrmError(f"{path.name}: unreadable glTF JSON ({exc})") from exc


def head_height(path: str | Path) -> float | None:
    """World-space Y of the model's head bone, in metres.

    Walks the glTF node tree accumulating translations. This is how the
    camera gets framed on the face without anyone guessing how tall a
    downloaded avatar happens to be -- VRM models range from chibi to giant
    and a fixed camera height frames one and misses the rest.
    """
    path = Path(path)
    gltf = read_glb_json(path)
    nodes = gltf.get("nodes") or []
    extensions = gltf.get("extensions", {}) or {}

    head_index: int | None = None
    if "VRMC_vrm" in extensions:
        bones = (extensions["VRMC_vrm"].get("humanoid") or {}).get("humanBones") or {}
        entry = bones.get("head") or {}
        head_index = entry.get("node")
    elif "VRM" in extensions:
        bones = (extensions["VRM"].get("humanoid") or {}).get("humanBones") or []
        for bone in bones:
            if str(bone.get("bone", "")).lower() == "head":
                head_index = bone.get("node")
                break
    if head_index is None or head_index >= len(nodes):
        return None

    # Parent map, so we can walk from the head back up to a root.
    parent: dict[int, int] = {}
    for index, node in enumerate(nodes):
        for child in node.get("children") or []:
            parent[child] = index

    y = 0.0
    current: int | None = head_index
    seen: set[int] = set()
    while current is not None and current not in seen:
        seen.add(current)
        translation = nodes[current].get("translation") or [0.0, 0.0, 0.0]
        y += float(translation[1])
        current = parent.get(current)
    return round(y, 4)


def inspect(path: str | Path) -> VrmInfo:
    path = Path(path)
    if not path.exists():
        raise VrmError(f"no such file: {path}")
    gltf = read_glb_json(path)
    extensions = gltf.get("extensions", {}) or {}

    info = VrmInfo(path=path)
    info.size_mb = path.stat().st_size / (1024 * 1024)
    info.mesh_count = len(gltf.get("meshes", []) or [])
    info.texture_count = len(gltf.get("textures", []) or [])

    if "VRMC_vrm" in extensions:
        _read_vrm1(extensions["VRMC_vrm"], info)
    elif "VRM" in extensions:
        _read_vrm0(extensions["VRM"], info)
    else:
        raise VrmError(
            f"{path.name} is a glTF file but has no VRM extension. Warudo "
            "needs a real VRM; re-export it from VRoid Studio or UniVRM."
        )
    return info


def _read_vrm1(block: dict[str, Any], info: VrmInfo) -> None:
    info.version = "1.0"
    meta = block.get("meta", {}) or {}
    info.name = meta.get("name", "")
    authors = meta.get("authors") or []
    info.author = ", ".join(authors) if isinstance(authors, list) else str(authors)
    info.license_url = meta.get("licenseUrl", "")
    info.commercial = str(meta.get("commercialUsage", ""))

    expressions = block.get("expressions", {}) or {}
    groups: dict[str, Any] = {}
    groups.update(expressions.get("preset") or {})
    groups.update(expressions.get("custom") or {})
    info.expressions = list(groups)
    for name, group in groups.items():
        group = group or {}
        if not (
            group.get("morphTargetBinds")
            or group.get("materialColorBinds")
            or group.get("textureTransformBinds")
        ):
            info.unbound.add(name)

    humanoid = block.get("humanoid", {}) or {}
    info.humanoid_bones = len(humanoid.get("humanBones") or {})


def _read_vrm0(block: dict[str, Any], info: VrmInfo) -> None:
    info.version = "0.x"
    meta = block.get("meta", {}) or {}
    info.name = meta.get("title", "")
    info.author = meta.get("author", "")
    info.license_url = meta.get("otherPermissionUrl") or meta.get("licenseName", "")
    info.commercial = str(
        meta.get("commercialUssageName", meta.get("commercialUsageName", ""))
    )

    groups = (block.get("blendShapeMaster", {}) or {}).get("blendShapeGroups", []) or []
    names = []
    for group in groups:
        name = group.get("presetName") or group.get("name") or ""
        if name and name.lower() != "unknown":
            names.append(name)
        elif group.get("name"):
            names.append(group["name"])
        else:
            continue
        if not (group.get("binds") or group.get("materialValues")):
            info.unbound.add(names[-1])
    info.expressions = names

    humanoid = block.get("humanoid", {}) or {}
    info.humanoid_bones = len(humanoid.get("humanBones", []) or [])
