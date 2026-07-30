# Warudo setup

The narrator talks to Warudo over a WebSocket. Warudo receives the messages
in a Blueprint and applies them to the avatar's blendshapes.

Warudo's WebSocket server accepts exactly one envelope:

```json
{ "action": "<name>", "data": <value> }
```

Anything else is discarded — its log says `Received data but action is null`,
and an unrecognised name gets `Unknown action: {0}`. **Warudo has no
JSON-parsing node**, so a single message carrying all five mouth weights
could not be unpacked inside a blueprint. Each channel is its own action:

```json
{ "action": "viseme_aa", "data": 0.00 }
{ "action": "viseme_ih", "data": 0.35 }
{ "action": "viseme_ou", "data": 0.00 }
{ "action": "viseme_ee", "data": 0.80 }
{ "action": "viseme_oh", "data": 0.00 }
{ "action": "emote",     "data": "Fun" }
```

Viseme actions arrive at up to 60fps while a line is being spoken, and zeros
are sent when it ends so the mouth never sticks open. A channel whose weight
has not moved is not sent at all, so a closed mouth is silent on the wire.
Emotes arrive on market events, at most one per minute.

---

## 0. On a new machine, in one command

**Read this first if you have just cloned the repo and Warudo does nothing.**

The setup lives in two places, and only one of them is this repo:

| Half | Where | In git? |
|---|---|---|
| The narrator, `config.toml`, the avatar files | this repo | **yes** |
| The Warudo **scene** — character, camera, room, and the `narrator` blueprint that turns a websocket message into a blendshape | `Warudo_Data/StreamingAssets/Scenes/DefaultScene.json`, inside the Warudo install | **it is now**, as `warudo/DefaultScene.json` |
| The avatar files Warudo can actually load | `Warudo_Data/StreamingAssets/Characters/` | the repo has them in `avatars/`, but Warudo does not look there |

That middle row is the whole problem. Cloning the repo gets you a narrator that
sends perfectly good viseme frames into a Warudo with no blueprint listening
for them, and an avatar picker that is empty because `roster.py` drops every
character it cannot find in Warudo's own Characters folder. Nothing errors.
The mouth simply never moves.

So, on the new machine:

```powershell
git clone <this repo>
# install Warudo from Steam (app 2079120) and launch it once, then close it
python -m tools.warudo_setup --check     # what is missing; writes nothing
python -m tools.warudo_setup             # install it
```

**Close Warudo before the second command.** Warudo holds the scene in memory
and rewrites the file on exit, so an edit made underneath a running Warudo is
thrown away at the exact moment it looks like it worked. The tool refuses to
run while `Warudo.exe` is up, rather than letting you discover this later.

What it does, in order:

1. **Finds Warudo.** Both default Steam paths, every library in
   `libraryfolders.vdf`, `$env:WARUDO_ROOT`, or `--warudo-root "D:\...\Warudo"`.
2. **Copies `avatars/*.vrm` into `StreamingAssets/Characters`.** Sixteen models,
   ~68 MB, already in the repo. This is what fills the avatar picker.
3. **Installs the scene**, one of two ways, chosen for you:
   * **Replace** — no scene yet, or a scene with no `Character 1`: the packaged
     scene from `warudo/DefaultScene.json` is copied in whole. That is this
     machine's scene: the same camera, the same framing, the same thirty-node
     blueprint that has been on stream.
   * **Graft** — you already ran Warudo's Onboarding, or built a room you want
     to keep: only the `narrator` blueprint is added, and every
     `Character 1` / `Camera 1` reference inside it is re-pointed at *your*
     scene's assets. Warudo mints a fresh guid per asset on every install, so
     copying the blueprint without this step wires it to ids that do not exist
     here. Node pairs whose target is missing — the `viseme2_` half, if you
     have no second character — are dropped rather than left dangling, because
     a blendshape node aimed at nothing throws sixty times a second.
4. **Prints the contract** — every websocket action and what it drives — and
   checks whether anything is listening on `warudo.port`.

A rollback copy is written before anything is overwritten
(`DefaultScene.json.pre-narrator`). To undo:

```powershell
python -m tools.warudo_setup --remove    # takes the blueprint back out
```

### If Warudo is not in the default Steam library

Steam offers a second library the first time a big game will not fit, so games
on `D:` is the ordinary case rather than the exotic one. The search covers
every library listed in `libraryfolders.vdf`, and past that:

```powershell
$env:WARUDO_ROOT = "D:\SteamLibrary\steamapps\common\Warudo"
python -m tools.warudo_setup
```

`WARUDO_ROOT` accepts the install folder or `Warudo_Data` inside it. It is
read by the **narrator itself** as well as by the tools —
`narrator/avatar/install.py` is the one search all of them use, deliberately,
because a setup tool that installs into `D:` while the running narrator reads
`C:` fails in the way that is hardest to see: everything reports success and
the avatar picker is still empty. Set it in the shell you launch the narrator
from, not just the one you install from.

### Checking it moved

Four tools, in the order worth running them. Each answers with a number rather
than an impression, which matters here because "the mouth is not moving" has
about six different causes that look identical:

```powershell
python -m tools.avatar_check              # can this model's mouth be driven at all?
python -m tools.lipsync_check             # is the blueprint really wired? --character 2 for the second host
python -m tools.motion_check --seconds 12 # is anything moving, including breathing and blinks?
python -m tools.frame_avatar --shot bust  # and point the camera at it
```

`avatar_check` reads the VRM header directly — no Unity, no Warudo, no import
— so it is the one to run *before* building anything. `lipsync_check` sends
each viseme at full weight and measures how much the render changed against a
closed mouth; identical frames mean the blueprint is not driving anything.
`liven_avatar` turns on breathing, sway and eye contact, which is the
difference between an avatar and a mannequin.

### Keeping the packaged scene current

The committed scene is a snapshot. Change the blueprint, move the camera, add a
node — none of it reaches the next machine until you export it again:

```powershell
python -m tools.warudo_export            # writes warudo/DefaultScene.json
python -m tools.warudo_export --check    # has the live scene drifted from it?
```

The export strips what belongs to the machine rather than the project: the
motion-capture rig and its graph (read off the characters' own
`TrackingAssetIds`/`TrackingGraphIds`, so it works whether that is SteamVR here
or MediaPipe there), and the microphone in the disabled MFCC graph, which is
stored as a Windows sound-endpoint GUID and names a card that does not exist
anywhere else. Everything else — every node, every port, every framing number —
goes across verbatim, because Warudo wrote it and Warudo is the only authority
on what its own nodes look like.

### Still to do by hand, on the new machine

Two things the scene file cannot carry:

* **The WebSocket port.** It is a Warudo setting, not part of the scene. §1
  below is how to read it without guessing. `19190` is the value here.
* **Warudo's own lip sync must be off** for the character (§2, last paragraph).
  Two things writing the same five blendshapes fight, and the mouth jitters.

---

## 1. The port

**Verified on this install: `19190`, which is what `config.toml` already
says.** Warudo listens on `0.0.0.0:19190`, it speaks WebSocket, and it accepts
the viseme frame format below without dropping the connection. A live run
pushed 2,746 frames with 0 dropped and 0 reconnects.

If it ever moves, find it without guessing — ask the OS which port Warudo
itself is listening on:

```powershell
$wp = (Get-Process Warudo).Id
Get-NetTCPConnection -State Listen | Where-Object { $_.OwningProcess -in $wp } |
  Select-Object LocalAddress, LocalPort
```

Warudo also opens several loopback-only ports (19052, 19053, 19097) for its
own Electron UI; those are not the WebSocket server. The one bound to
`0.0.0.0` is.

Or check it in the app:

1. Open Warudo.
2. **Settings → Plugins → WebSocket** (in some builds: **Settings → General →
   Network**).
3. Note the port the WebSocket server is listening on, and make sure the
   server is **enabled**.
4. Put it in `config.toml`:

```toml
[warudo]
enabled = true
host = "127.0.0.1"
port = 19190          # <- the number you just read, not this one
path = "/"
```

Check it from the narrator side before building anything:

```powershell
python -m narrator.main --dry-run --replay --validate-only
```

Preflight reports `warudo: websocket reachable at 127.0.0.1:<port>` when it is
right, and tells you nothing is listening when it is not. A dead bridge is a
warning, never fatal — the narrator keeps speaking without the avatar, because
audio is the stream and the avatar is decoration.

---

## 2. Get an avatar

**The roster is in the repo, under `avatars/`, and `tools.warudo_setup` copies
it into Warudo for you.** Warudo only ever looks in its own Characters folder —
`Steam\steamapps\common\Warudo\Warudo_Data\StreamingAssets\Characters` — so a
model sitting in the clone is a model the picker will not offer. That copy is
step 2 of §0; nothing below needs doing by hand on a new machine.

Four are verified end to end:

| File | Mouth | Emotes | Bones | Notes |
|---|---|---|---|---|
| `100ava_PetalDude.vrm` | **moves** | 5/5 | 52 | the only one verified end to end |
| `100ava_Jenny.vrm` | moves | 5/5 | 52 | real morphs, but no visible mouth on the face |
| `NeonGl_Summer_V2.vrm` | **dead** | 5/5 | 53 | vowel clips bind to nothing |
| `NeonGl_EL_BUENO.vrm` | **dead** | 5/5 | 52 | vowel clips bind to nothing |

All CC0, all VRM 0.x, all *listing* the five vowel shapes and all five emotes
mapping onto Joy/Sorrow/Fun/Neutral. Two of them cannot move their mouths at
all — see *The clip that binds to nothing* below, and run `avatar_check`
before you trust any of them. Warudo also ships with *Shipilka*.

None of these four is a good daily driver. **VRoid Studio is installed on this
machine** and always exports vowels bound to real morph targets; that is the
path to an avatar whose mouth is worth watching.

### Loading one

**The control panel is a separate window from the 3D view.** Warudo runs its
UI as an Electron client (four `warudo-client-electron` processes); if you are
looking at the avatar and scene, the controls are another window — Alt-Tab.

There is no "Add Character" menu item. Use the **Onboarding** assistant (the
default screen on a fresh install, otherwise ☰ Menu → Onboarding) and go
through **Basic Setup**. Its first step is a character picker, and the models
above are already in the folder it watches, so they appear in the list. In
Warudo's own words:

> Welcome to Warudo! To get started, select a character below. For custom
> character models, you need a model file in VRM (.vrm) or Warudo Character
> Mod (.warudo) format. Place it in the characters folder (below), and come
> back here to select it.

When it offers to **Import VRM Expressions, say yes** — those are the clips
the `emote` action drives. You can revisit them later at
*Character → Expressions*.

Save the scene when you are done (☰ Menu → Save Scene), or none of this
survives a restart.

These are stylised, not polished — fine for proving the pipeline, thin for a
daily stream. For something brandable, **VRoid Studio is already installed on
this machine**: it exports the five vowels plus Joy/Angry/Sorrow/Fun every
time, so lip sync and emotes both work with no extra wiring.

Warudo loads `.vrm` files. Anything else — MMD, VRChat, FBX — has to go
through the Mod SDK and be exported as `.warudo` first.

Where to get one, best first:

| Source | Cost | Style | Notes |
|---|---|---|---|
| [VRoid Studio](https://vroid.com/en/studio) | free | anime | **Best default.** Full character creator, no 3D skill needed. Always exports the five vowel shapes plus Joy/Angry/Sorrow/Fun, so lip sync and emotes both work out of the box. Brand it however you like. |
| [Booth.pm](https://booth.pm/en/browse/3D%20Models) | ~$20–80 | anime, high quality | Where the good models are. **Check the license for commercial streaming** — it varies per model and is written per-item. |
| [VRoid Hub](https://hub.vroid.com/en) | free + paid | anime | Per-model permission flags shown bottom-right: downloadable, commercial use, modification, redistribution. Read them. |
| [Open Source Avatars](https://www.opensourceavatars.com/en/gallery) | free, CC0 | stylised / low-poly | 300+ models, CC0 so commercial use is unambiguous. Verified: they carry the five vowel shapes and `blink` — **but no emotion clips at all**, so emotes will do nothing. Good for testing, thin for a daily stream. |
| [VIVERSE Avatar Creator](https://avatar.viverse.com/) | free | stylised | Browser-based, exports VRM. |

Realistic (non-anime) VRM is thin ground. Ready Player Me and MetaHuman aim
that way but neither exports VRM directly; you would be converting through
Blender + UniVRM, and the mouth shapes are the thing that usually breaks in
that pipeline. If you want realism, budget for a commissioned model.

### 2b. Perfect Sync — the upgrade that makes the mouth human

**Five vowels is the ceiling, and it is a low one.** `A I U E O` cannot say
the thing that makes speech look real: the jaw and the lips move
independently. "Boot" is a nearly shut jaw with tightly rounded lips; "father"
is a dropped jaw with neutral ones. One channel per vowel collapses both into
a single number, and no amount of timing work fixes it.

The fix is the **ARKit 52-blendshape set**, which VTubers call *Perfect Sync*:
`jawOpen`, `mouthClose`, `mouthPucker`, `mouthFunnel`, `mouthStretchLeft/Right`,
`mouthPressLeft/Right`, `mouthRollLower`, `tongueOut` and the rest. Warudo
drives them **by name** through the same *Set Character BlendShape* node the
bridge already uses — with **Use VRM BlendShape Proxy off**, because these are
raw mesh morphs, not VRM clips.

`narrator/speech/arkit.py` already renders the narrator's phonemes onto those
channels, so the software side is waiting on the model.

#### Getting the blendshapes onto a VRoid model

VRoid exports the five vowels and nothing else. **HANA_Tool** adds the 52.

**Prerequisite, not currently installed on this machine: Unity.** HANA_Tool is
a Unity package; there is no standalone version.

1. **VRoid Studio** (installed) — design the character, then **export VRM with
   mesh optimisation and decimation turned OFF**. Reducing the mesh destroys
   the vertices the blendshapes need, and HANA_Tool will produce nothing.
2. **Unity + UniVRM** — install Unity Hub and an editor, make an empty 3D
   project, and drag in the UniVRM `.unitypackage`, then the HANA_Tool
   `.unitypackage` (sold/distributed on BOOTH).
3. Drop the `.vrm` into `Assets/`, then drag the imported model into the
   scene hierarchy.
4. **HANA_Tool → Reader**: put the model's `Face` object in the
   SkinnedMeshRenderer slot, pick your VRoid version, **Read BlendShapes**.
5. **HANA_Tool → ClipBuilder**: model into the `VRMBlendShapeProxy` slot,
   `Face` into SkinnedMeshRenderer, run it.
6. Press **Play** and confirm 50+ new shapes appear under the standard VRM
   ones. If they do not, step 1's export settings are the usual culprit.
7. **UniVRM → Export humanoid**, and drop the result in Warudo's
   `StreamingAssets/Characters`.

Then check it the same way as any other model — `avatar_check` reports whether
the vowels bind to anything, and the Perfect Sync shapes are visible in
Warudo's *Set Character BlendShape* dropdown once the character is selected.

### 2c. A better room

Warudo ships four environments in `StreamingAssets/Environments`: `VR Room`
(the current one — neon shelves and plush toys, the most streamer-bedroom of
the four), `Classroom`, `Edge` and `Ruins`.

Custom environments need the Mod SDK to build, but you do not have to build
one: **Warudo has a Steam Workshop with an Environment category**, and
subscribed items appear in the Source dropdown automatically. Nothing is
subscribed yet — `steamapps\workshop\content\2079120` is empty.

### Check it before you build anything

```powershell
python -m tools.avatar_check "C:\path\to\model.vrm"
python -m tools.avatar_check                      # scans Warudo's Characters folder
```

It reads the VRM header directly — no Unity, no Warudo, no import — and tells
you whether the mouth can be driven and which clips the emotes will land on:

```
  lip sync (required)
    [OK  ] aa  -> A
    [OK  ] ih  -> I
    ...
  VERDICT: usable. The mouth will move.
```

A model that fails this cannot be lip-synced by *anything*, Warudo's own lip
sync included. That is the model's problem, not the narrator's — re-export it
from VRoid Studio or add the clips in Unity with UniVRM.

It checks two separate things, because a clip can pass the first and fail the
second: that the vowel clip **exists**, and that it **binds to something** — a
morph target or a material value. A clip with neither is a name attached to
nothing, and it is the single most confusing failure in this whole pipeline.

### The blendshape names

The narrator's five viseme channels **are** the VRM 1.0 preset names, so a
VRM 1.0 model needs no translation at all. VRM 0.x uses the older vowels:

| narrator | VRM 1.0 | VRM 0.x | mouth shape |
|---|---|---|---|
| `aa` | `aa` | `A` | open, jaw down — "father" |
| `ih` | `ih` | `I` | narrow, relaxed — "bit", and the rest shape |
| `ou` | `ou` | `U` | pursed, small — "boot" |
| `ee` | `ee` | `E` | wide, lips spread — "feet" |
| `oh` | `oh` | `O` | rounded, medium — "boat" |

**Turn off Warudo's own lip sync** for this character (**Character → Lip Sync →
disable**, or set its input to None). If it stays on it will fight the
narrator's visemes and the mouth will jitter.

---

## 3. Two ways to move the mouth

Both are built. **Option B is the one running** — the mouth is driven by
Kokoro's own phoneme timings over the WebSocket, not by re-deriving them from
the audio. Option A's graph is still in the scene, disabled, as a fallback for
a machine where the narrator is not the audio source.

### A. Warudo's own lip sync — a three-node blueprint

Warudo ships an **MFCC-based** lip sync engine (12 mel-frequency cepstral
coefficients, 24 filter banks, per-viseme calibration in
`StreamingAssets/LipSyncProfiles/Default.json`). That is *not* amplitude
matching — it classifies the actual vowel out of the audio spectrum, so it
can tell "ee" from "oh".

It only needs to hear the narrator, and this machine has a hardware loopback
already: **Stereo Mix (Realtek HD Audio Stereo input)**. No virtual cable, no
VoiceMeeter.

1. Open the old Sound control panel on its Recording tab — `control mmsys.cpl,,1`
   — right click → **Show Disabled Devices**, then right click **Stereo Mix** →
   **Enable**. The modern Settings app does not expose this.
2. Verify before touching Warudo:

   ```powershell
   python -m tools.loopback_check --tone
   ```

   It plays a tone and reports the level *and the host API* the device came
   from. **Both halves matter.** Stereo Mix can carry a perfect signal while
   still being invisible to Warudo: if it is listed only under **WDM-KS**, that
   is the raw driver pin, and Unity — which is what Warudo is — offers only
   Windows *endpoints*. An endpoint listed nowhere but WDM-KS is disabled, and
   the Lip Sync dropdown will not show it however loud the pin is. Step 1 is
   what turns the pin into an endpoint; after it, the device appears under
   MME/DirectSound/WASAPI too.
3. Build the blueprint. **In 0.15 there is no checkbox for this** — no Lip Sync
   asset in *Add Asset*, and *Setup Motion Capture* only offers face/pose/hand
   tracking templates (it refuses to finish with all three set to None). Lip
   sync is three nodes in a blueprint, wired like this:

   ```
     [On Update] ──Exit──► Enter──[Set Character Tracking BlendShapes]
                                    Character:  Character 1
                                    BlendShapes ◄──OutputBlendShapes──┐
                                    Use VRM BlendShape Proxy: Yes     │
                                                                      │
                            [Generate Lip Sync Animation] ────────────┘
                              Microphone: Stereo Mix (Realtek(R) Audio)
                              Phonemes:   A→A  I→I  U→U  E→E  O→O
   ```

   The **Phonemes list ships empty** — every entry has a phoneme letter and a
   blank BlendShape name, and until you type the names in, the node classifies
   the audio and emits nothing. Use the capitals Warudo normalises VRM 0.x
   clips to (`A I U E O`), not the lowercase names `avatar_check` reads out of
   the file.
4. Run the narrator normally. It plays to the default output; Stereo Mix
   loops that back; Warudo classifies it.

Trade-off: Warudo re-derives from audio what this system already knows
exactly, so expect slightly softer timing than option B.

**Verified working, on this install:** the node's live meters (*Current
Volume*, and a bar per vowel) move with the narrator's voice — audio reaches
the classifier and the classifier produces visemes. **Not yet verified:** the
avatar's mouth actually moving. With the graph above saved and the scene
reloaded, `NeonGl_Summer_V2.vrm` renders no visible mouth change between
silence and speech. Unresolved — see *What is still open* below.

#### The scene JSON, for reference

Warudo writes the graph to `StreamingAssets/Scenes/DefaultScene.json`. These
are the real identifiers, read back out of a graph the editor itself produced
— which is the reliable way to get them, since the assembly's string heap does
not give up the port names:

| Node | typeId |
|---|---|
| `ON_UPDATE` | `e4140b42-efde-491a-ad88-21038f0289e2` |
| `GENERATE_LIP_SYNC_BLENDSHAPES` | `6659230d-fadb-494f-983e-c23364d51abc` |
| `SET_CHARACTER_TRACKING_BLENDSHAPES` | `7a5a570a-62fd-4124-ac02-aeff16e48789` |

Connections live in two arrays on the graph, **`dataConnections` and
`flowConnections`** — not a single `connections` key — and both entries take
the same four fields:

```json
"flowConnections": [
  { "outputNode": "<on-update-id>", "inputNode": "<set-id>",
    "outputPort": "Exit", "inputPort": "Enter" }
],
"dataConnections": [
  { "outputNode": "<lipsync-id>", "inputNode": "<set-id>",
    "outputPort": "OutputBlendShapes", "inputPort": "BlendShapes" }
]
```

Two details that will bite anyone hand-authoring this: the `Microphone` input
stores a **Windows endpoint GUID**, not a device name
(`"{0.0.1.00000000}.{5e5ff6d0-…}"`), and `Phonemes` is a list of structured
entries whose own `dataInputs` are `Phoneme`, `BlendShape` and `MaxWeight`.

### B. The phoneme bridge — exact. **This is what is installed.**

Kokoro tells us the phonemes and their timings directly, so the mouth is
driven by what is actually being said rather than by a classifier's guess.
The blueprint exists in the scene as **`narrator`**, and option A's graph is
kept alongside it as `lip sync (MFCC, disabled)` — disabled, because two
graphs writing the same five blendshapes fight each other.

Twelve nodes, six pairs, no branching:

```
  [On WebSocket Action]                  [Set Character BlendShape]
   Action:    viseme_aa    ──Exit──────►  Enter
   Data Type: Float        ──FloatData─►  Value
                                          Character:  Character 1
                                          BlendShape: A
                                          Use VRM BlendShape Proxy: Yes
```

five times over — `viseme_aa`→`A`, `viseme_ih`→`I`, `viseme_ou`→`U`,
`viseme_ee`→`E`, `viseme_oh`→`O` — plus one more pair for emotes:

```
  [On WebSocket Action]                  [Toggle Character Expression]
   Action:    emote        ──Exit───────► Enter
   Data Type: String       ──StringData─► Expression
                                          Action:    Enable
                                          Auto Exit: Yes
```

**Data Type is `Float`, not "Number".** The old note here said Number; this
build's dropdown offers None / Boolean / Integer / **Float** / String / …, and
picking Integer would round every weight to 0 or 1.

**`Action` must be `Enable`, not `Toggle`.** Toggle flips state, so the second
emote of a session switches the first one back off. `Enable` with **Auto Exit**
turns the expression on and lets Warudo release it — which also means the
`Delay` → `Exit All Character Expressions` chain the old notes called for is
unnecessary.

#### Hand-authoring it, which does work

The previous attempt concluded the graph could not be written as JSON, because
data connections came back `Could not deserialize data connection ...
outputPort: Data, inputPort: Value. Skipping`. **That was the wrong port name
and the wrong key, not a wrong approach.** The whole graph round-trips
through the scene file cleanly:

* Connections live in **two arrays on the graph**, `dataConnections` and
  `flowConnections` — there is no single `connections` key. Both take
  `{outputNode, inputNode, outputPort, inputPort}` with node **ids**, not names.
* The real ports are `Exit` → `Enter` for flow, and **`FloatData`** → **`Value`**
  for data. `ON_WEBSOCKET_ACTION` publishes one output per type —
  `BooleanData`, `IntegerData`, `FloatData`, `StringData`, `Vector3Data`,
  `QuaternionData`, the list variants, `JsonTokenData` — and hides all but the
  one matching `DataType`.
* Its inputs are `Action` (the string) and `DataType` (an enum object,
  `{"label":"Float","value":3}`), not `Value`/`Type`.
* A graph `id` must be a real GUID, and so must every node `id`.

The reliable way to get a schema you do not know: **build one pair in the GUI,
save the scene, and read what Warudo wrote.** Everything above came out of
that, and the other five pairs were then generated by cloning the pair with
fresh GUIDs. Warudo loads the result with no `Could not deserialize` lines and
the editor opens it as a normal blueprint.

**A failed graph is not harmless**: Warudo catches the exception, loads a
partial scene, and re-saves it on exit with the character stripped of every
setting. Keep the backups (`DefaultScene.json.bak`, and the `.pre-*` copies).

#### The camera pair, for the framing button

Two more pairs in the same graph let the browser UI's **`frame`** button orbit
the camera, so framing does not mean alt-tabbing into Warudo:

```
  [On WebSocket Action]              [Set Asset Position]
   Action:    cam_pos    ──Exit────►  Enter
   Data Type: Vector3    ──Vector3Data─► Position
                                        Asset: Camera 1
                                        Transition Time: 0

  [On WebSocket Action]              [Set Asset Rotation]
   Action:    cam_rot    ──Exit────►  Enter
   Data Type: Vector3    ──Vector3Data─► Rotation
                                        Asset: Camera 1
```

| Node | typeId |
|---|---|
| `SET_ASSET_POSITION` | `6fc9c96f-b9c6-4607-a059-cb270672ffdd` |
| `SET_ASSET_ROTATION` | `f0c0f757-cb1d-4c08-bfe2-3ed29537209d` |

`Data Type` is **Vector3** (enum value 5) and the payload is a JSON object:
`{"action": "cam_pos", "data": {"x": 0, "y": 0.99, "z": 0.85}}`.

Two things to get right, both of which cost a debugging round here:

- **Transition Time must be 0.** Framing is a live drag, and any easing leaves
  the camera trailing the hand.
- **Unity's positive X rotation tilts *down*.** Negating the pitch aims a
  raised camera at the ceiling and the avatar leaves the frame.

`warudo.camera_focus_height` in `config.toml` is what the orbit points at --
roughly the character's face, in metres. Shipilka is a chibi and wants ~0.92;
an adult-proportioned VRM wants ~1.5.

#### Proving it end to end

Independent of Kokoro, the scheduler and the classifier — speak the envelope
yourself and photograph the face:

```python
from websockets.sync.client import connect
import json
with connect("ws://127.0.0.1:19190") as ws:
    ws.send(json.dumps({"action": "viseme_aa", "data": 1.0}))
```

Measured on `100ava_PetalDude.vrm`, each channel moves the mouth region by a
peak of 106–119 grey levels against the all-zero pose, and the shapes are
distinct: `viseme_aa` is wide open with teeth showing, `viseme_ou` is small and
pursed.

## 3b. Rebuilding the Blueprint by hand

Already built and in the scene as `narrator`, and `python -m tools.warudo_setup`
(§0) installs it on a new machine without any of this. **This section is the
fallback**: a scene lost with no backup, a Warudo release that changes a node
schema out from under the packaged copy, or wanting to understand what the
twelve — now thirty — nodes actually do before trusting a script to write them.

**Blueprints → New Blueprint**, call it `narrator`.

The node names below were read out of your installed build, not out of the
docs — Warudo registers every node type in `Player.log` at startup.

### The mouth: five identical pairs

```
  [On WebSocket Action]              [Set Character Blend Shape]
   Action: viseme_aa      ── data ──►  Character:  <your VRM>
   Type:   Float                       Blend Shape: A
                                       Value:       ◄── data
                                       Use VRM BlendShape Proxy: Yes
```

Build that pair **five times**:

| On WebSocket Action → Action | Set Character Blend Shape → Blend Shape |
|---|---|
| `viseme_aa` | `A` |
| `viseme_ih` | `I` |
| `viseme_ou` | `U` |
| `viseme_ee` | `E` |
| `viseme_oh` | `O` |

**Use the capitals.** `avatar_check` reads the raw file and reports whatever
case is stored there, but Warudo normalises everything to the VRM preset
names when it loads. Its log says so on startup, and that is the list the
blueprint must match:

```
Available VRM blendshape clips: Neutral, A, I, U, E, O, Blink, Joy, Angry,
                                Sorrow, Fun, LookUp, LookDown, ...
```

Details that matter:

- **Data Type** on every On WebSocket Action node must be **Float**. The
  dropdown has no "Number"; Integer is the trap, and it would round every
  weight to 0 or 1 and snap the mouth open and shut.
- **Use VRM BlendShape Proxy: Yes** on every Set Character Blend Shape node.
  Without it the node addresses raw mesh morphs rather than the VRM clips
  Warudo normalises to `A I U E O`, and its BlendShape box offers nothing.
- Leave any smoothing on the Set Character Blend Shape nodes **off**. The
  narrator already applies a 40ms attack and release; smoothing twice makes
  the mouth mushy and late.
- No gating is needed. Each action drives exactly one blendshape, so an emote
  message cannot disturb the mouth.

### Emotes: one more pair

```
  [On WebSocket Action]         [Toggle Character Expression]
   Action: emote      ── data ──►  Character:  <your VRM>
   Type:   String                  Expression: ◄── data
                                   Action:    Enable
                                   Auto Exit: Yes
```

- **Data Type** is **String** here.
- **Action must be `Enable`.** The default is `Toggle`, which flips: the second
  emote of a session would switch the first one back off.
- **Auto Exit** releases the expression by itself, so the `Delay` →
  `Exit All Character Expressions` chain the earlier notes described is not
  needed. The hold duration is not sent — Warudo cannot unpack a second field
  from the same message — and with Auto Exit it does not have to be.
- The narrator sends the expression name your model actually has. Which
  spelling it sends is `warudo.expression_style` in `config.toml`: `vrm0`
  (default — `Joy`/`Sorrow`/`Fun`, what the installed avatars use), `vrm1`
  (`happy`/`relaxed`/`surprised`), or `name` for the narrator's own words.

The mapping, set in `config.toml` under `[warudo] expressions`:

| narrator emote | fires on | VRM 1.0 | VRM 0.x |
|---|---|---|---|
| `alert` | a new session opens | `surprised` | `Fun` |
| `surprised` | `atr_ratio` crosses 2.0, or a level breaks after being untested | `surprised` | `Fun` |
| `bored` | `minutes_since_move` crosses 30 | `relaxed` | `Sorrow` |
| `excited` | the range releases after 12+ tight bars | `happy` | `Joy` |
| `neutral` | resting | `neutral` | `Neutral` |

Mapping onto the standard presets is deliberate: a stock avatar works without
anyone hand-authoring expression clips. If a clip is missing the emote simply
does nothing — it will never break the mouth.

6. **Save and enable the Blueprint.**

---

## 4. Test it

With Warudo open and the Blueprint enabled:

```powershell
# Talks, moves the mouth, fires emotes on real market events.
python -m narrator.main --replay --speed 1
```

The status bar shows `warudo ok (N frames)` when frames are landing. On exit
the summary reports frames sent, frames dropped and reconnects.

To check the mouth without a market feed, watch the viseme scope:

```powershell
python -m tools.speech_check
```

### If the mouth does not move

| Symptom | Cause |
|---|---|
| Fresh clone, everything looks fine, nothing ever moves | The scene is not in the repo checkout — run `python -m tools.warudo_setup --check` (§0) |
| The avatar picker in the browser UI is empty | The models are in `avatars/` but not in Warudo's Characters folder; same command |
| `warudo down (ConnectionRefusedError)` | Wrong port, or the WebSocket server is off in Warudo settings |
| Connected, frames sent, nothing moves | Blueprint not enabled, or Set Blend Shape is pointed at the wrong character |
| Mouth twitches or fights itself | Warudo's own lip sync is still enabled on that character |
| Mouth sticks open between lines | The `type == "viseme"` gate is missing, so the closing all-zero frame is being dropped |
| Mouth moves but looks mushy and late | Smoothing enabled on the Set Blend Shape nodes; turn it off |
| Frames dropped climbing in the summary | Warudo or the machine is stalling; the narrator drops frames rather than queue them, on purpose — a late viseme is worse than none |
| Lip Sync node's meters move but the mouth does not | Its Phonemes list has no BlendShape names in it — see §3A step 3 |
| Stereo Mix missing from the Lip Sync dropdown | The endpoint is disabled in Windows, even if the pin carries audio — `control mmsys.cpl,,1` |

### The clip that binds to nothing

Working lip sync spent an afternoon looking broken because of the *model*, not
the wiring. **`NeonGl_Summer_V2.vrm` and `NeonGl_EL_BUENO.vrm` carry all five
vowel clips and zero morph targets.** The clips are names with nothing behind
them: no `binds`, no `materialValues`. Warudo loads them, lists them in every
dropdown, accepts a weight of 1.0 — and no part of the face moves.

The tell, if you ever see it again: in *Set Character BlendShape*, the
**BlendShape field offers "No results found"** for a character that clearly has
clips. That is Warudo saying there is nothing drivable on the model.

`tools/avatar_check.py` now catches this before you build anything:

```
NeonGl_Summer_V2.vrm
    [MISS] aa  -> a (clip exists but drives nothing)
    ...
  VERDICT: NOT usable for lip sync -- aa, ee, ih, oh, ou name a clip
  that binds to nothing.
```

Of the four models in the Characters folder, **only `100ava_Jenny.vrm` and
`100ava_PetalDude.vrm` have real morph targets**, and Jenny's face has no
visible mouth to move. PetalDude is the one to test against.

### Tuning the node

Defaults are conservative. What is in the scene now, and why:

| Setting | Default | Now | Why |
|---|---|---|---|
| Weight | 1.0 | **1.4** | these stylised models have small mouth morphs |
| Volume Threshold | 25 | **8** | 25 gates out quiet syllables, and the mouth stalls mid-sentence |
| Smooth Time | 0.1 | **0.04** | 0.1 lags behind the syllable rate |
| Binarize | off | **off** | snapping to one vowel looks worse than blending, on a model with real morphs |

Measured on PetalDude with the narrator speaking: mouth-region deviation ranges
2.9 (at rest) to 9.4 (open vowel) grey levels, against 0.8 for the same
measurement on a model that cannot move at all.

---

## 5. Streaming layout

TikTok LIVE Studio composites Warudo and captures system audio directly. No
OBS, no virtual audio cable, no VoiceMeeter — the narrator plays to the
Windows default output device and LIVE Studio picks it up.

If you need a specific device instead:

```powershell
python -m narrator.main --list-devices
```

then set `audio.device` in `config.toml` to the index or the name.
