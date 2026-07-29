"""Write the narrator's Warudo blueprint straight into the scene file.

    python -m tools.build_blueprint
    python -m tools.build_blueprint --remove

Warudo's node editor is a GUI, and wiring twelve nodes by hand is the one
step of this setup that cannot be scripted from outside... except that scenes
are JSON, and a blueprint is just a `graphs` entry. This writes it.

Node type GUIDs were read out of Warudo.Plugins.Core.dll; port keys follow the
convention visible in Warudo's own shipped graphs, where a port labelled
IS_TRACKED is keyed "IsTracked".

Warudo must be closed: it rewrites the scene on exit.
"""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from pathlib import Path

WARUDO = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Warudo\Warudo_Data")
SCENE = WARUDO / "StreamingAssets" / "Scenes" / "DefaultScene.json"

CHARACTER_TYPE = "726ab674-a550-474e-8b92-66526a5ad55e"
ON_WEBSOCKET_ACTION = "61445859-8471-4baa-88d6-b186773bd40d"
SET_CHARACTER_BLENDSHAPE = "979f57cc-4329-4e21-8b99-ddde1d72a0ea"

GRAPH_NAME = "Trade Fix Narrator"
# Must be a real GUID: Warudo parses it with System.Guid and refuses to open
# the scene at all if it is not valid hex. Fixed, so --remove can find it.
GRAPH_ID = "9f2c1a70-3b4d-4a21-9c8e-7d5f1a2b6c04"

# narrator viseme -> the VRM clip Warudo reports for this model.
VISEMES = {"aa": "A", "ih": "I", "ou": "U", "ee": "E", "oh": "O"}


def node(type_id: str, name: str, x: float, y: float, inputs: dict) -> dict:
    return {
        "typeId": type_id,
        "name": name,
        "id": str(uuid.uuid4()),
        "version": 0,
        "x": x,
        "y": y,
        "dataInputs": {k: {"value": v} for k, v in inputs.items()},
        "dataOutputs": {},
        "flowInputs": {},
        "flowOutputs": {},
    }


def build(character_id: str, character_name: str) -> dict:
    nodes: dict[str, dict] = {}
    data_connections: list[dict] = []
    flow_connections: list[dict] = []

    for index, (viseme, clip) in enumerate(VISEMES.items()):
        y = index * 220.0
        source = node(
            ON_WEBSOCKET_ACTION,
            "ON_WEBSOCKET_ACTION",
            0.0,
            y,
            {"Action": json.dumps(f"viseme_{viseme}")},
        )
        sink = node(
            SET_CHARACTER_BLENDSHAPE,
            "SET_CHARACTER_BLENDSHAPE",
            420.0,
            y,
            {
                "Character": json.dumps({"id": character_id, "name": character_name}),
                "BlendShape": json.dumps(clip),
            },
        )
        nodes[source["id"]] = source
        nodes[sink["id"]] = sink
        # The received number drives the blend shape weight...
        data_connections.append(
            {
                "outputNode": source["id"],
                "inputNode": sink["id"],
                "outputPort": "Data",
                "inputPort": "Value",
            }
        )
        # ...and the event fires the setter.
        flow_connections.append(
            {
                "outputNode": source["id"],
                "inputNode": sink["id"],
                "outputPort": "Exit",
                "inputPort": "Enter",
            }
        )

    return {
        "id": GRAPH_ID,
        "enabled": True,
        "name": GRAPH_NAME,
        "order": 0,
        "group": None,
        "panningX": None,
        "panningY": None,
        "scaling": None,
        "nodes": nodes,
        "dataConnections": data_connections,
        "flowConnections": flow_connections,
        "properties": None,
    }


# Candidate port names for the probe. Warudo rejects a bad data connection
# with "Could not deserialize data connection ... Skipping" and keeps going,
# so every combination can be tested in a single load: whichever pair is
# absent from the log is the correct one.
OUTPUT_CANDIDATES = ["Data", "Value", "Output", "Payload", "Result", "Number", "Float"]
INPUT_CANDIDATES = ["Value", "Weight", "Amount", "Strength", "Target", "BlendShapeValue"]


def build_probe(character_id: str, character_name: str) -> dict:
    """One node pair, wired every plausible way at once."""
    source = node(
        ON_WEBSOCKET_ACTION,
        "ON_WEBSOCKET_ACTION",
        0.0,
        0.0,
        {"Action": json.dumps("probe_action")},
    )
    sink = node(
        SET_CHARACTER_BLENDSHAPE,
        "SET_CHARACTER_BLENDSHAPE",
        420.0,
        0.0,
        {
            "Character": json.dumps({"id": character_id, "name": character_name}),
            "BlendShape": json.dumps("A"),
        },
    )
    connections = [
        {
            "outputNode": source["id"],
            "inputNode": sink["id"],
            "outputPort": out,
            "inputPort": inp,
        }
        for out in OUTPUT_CANDIDATES
        for inp in INPUT_CANDIDATES
    ]
    return {
        "id": GRAPH_ID,
        "enabled": True,
        "name": GRAPH_NAME,
        "order": 0,
        "group": None,
        "panningX": None,
        "panningY": None,
        "scaling": None,
        "nodes": {source["id"]: source, sink["id"]: sink},
        "dataConnections": connections,
        "flowConnections": [
            {
                "outputNode": source["id"],
                "inputNode": sink["id"],
                "outputPort": "Exit",
                "inputPort": "Enter",
            }
        ],
        "properties": None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--remove", action="store_true", help="delete the blueprint again")
    ap.add_argument(
        "--probe",
        action="store_true",
        help="wire one node pair every plausible way, to discover the real "
        "port names from which connections Warudo rejects",
    )
    args = ap.parse_args()

    if not SCENE.exists():
        raise SystemExit(f"scene not found: {SCENE}")
    scene = json.loads(SCENE.read_text(encoding="utf-8"))

    backup = SCENE.with_suffix(".json.bak")
    if not backup.exists():
        shutil.copy2(SCENE, backup)
        print(f"backup -> {backup.name}")

    scene["graphs"] = [g for g in scene["graphs"] if g.get("name") != GRAPH_NAME]
    if args.remove:
        SCENE.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        print(f"removed {GRAPH_NAME!r}")
        return

    character = next(
        (a for a in scene["assets"] if a.get("typeId") == CHARACTER_TYPE), None
    )
    if character is None:
        raise SystemExit("no Character asset -- load a model first")

    graph = (
        build_probe(character["id"], character["name"])
        if args.probe
        else build(character["id"], character["name"])
    )
    scene["graphs"].append(graph)

    hierarchy = scene.get("graphHierarchy") or {
        "collapsed": False,
        "key": "",
        "children": [],
    }
    children = hierarchy.setdefault("children", [])
    children.append({"collapsed": False, "key": GRAPH_ID, "children": None})
    scene["graphHierarchy"] = hierarchy

    SCENE.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
    print(
        f"wrote {GRAPH_NAME!r}: {len(graph['nodes'])} nodes, "
        f"{len(graph['dataConnections'])} data + "
        f"{len(graph['flowConnections'])} flow connections"
    )
    for viseme, clip in VISEMES.items():
        print(f"    viseme_{viseme:<3} -> blend shape {clip}")
    print("\nStart Warudo, then check Player.log for load errors.")


if __name__ == "__main__":
    main()
