# The Warudo half

`DefaultScene.json` is a Warudo scene — the one that has been on stream. It is
here because it is the half of the avatar setup that does *not* otherwise live
in this repo:

* **Character 1** and **Character 2**, their placement and their expressions
* **Camera 1**, and the framing the avatar panel drags around
* the **`narrator` blueprint** — thirty nodes that turn
  `{"action": "viseme_aa", "data": 0.8}` into a blendshape weight, plus the
  emote, camera, avatar-switch and scene-reload channels
* `lip sync (MFCC, disabled)`, the fallback graph from `WARUDO_SETUP.md` §3A,
  kept disabled because two graphs writing the same five blendshapes fight

Warudo keeps this at
`Warudo_Data/StreamingAssets/Scenes/DefaultScene.json`. A clone of this repo
without it is a narrator sending perfectly good viseme frames at a Warudo with
nothing listening — no error, no movement.

```powershell
python -m tools.warudo_setup --check    # what a machine is missing
python -m tools.warudo_setup            # install this scene and avatars/
python -m tools.warudo_export           # rewrite this file from the live scene
```

Do not hand-edit it. Change the scene in Warudo, save, and export — the node
schemas are Warudo's, and it is the only authority on what they look like.

**What was stripped on the way out**, because it is a machine and not a project:
the motion-capture rig and its graph (SteamVR here; whatever is plugged in
there), and the microphone in the disabled lip-sync graph, which is stored as a
Windows sound-endpoint GUID. Everything else is verbatim.

`Shipilka.warudo` and `VR Room.warudo` are referenced but not committed — both
ship with Warudo itself. Everything else the scene names is in `avatars/`.
