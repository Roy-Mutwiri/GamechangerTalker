# Unreal setup — the face, the body, and the five calls

This is the source of truth for the Unreal character: the five Blueprint
functions the narrator calls, the three transports it can call them over, and
what to do when nothing happens.

The character has two halves and they share no failure mode, so build them in
this order and never debug them together:

1. **The face** — sixty-one blendshapes a second down Live Link UDP. Needs no
   Blueprint, no object path and no web server. [Jump to it](#the-face); it is
   at the end of this document because everything before it depends on it, not
   the other way round.
2. **The body** — the five functions below: posture, gestures, camera, seats.

Prove the face first. If `python -m tools.livelink_check --blink` does not
make the MetaHuman blink, nothing in the rest of this document can help you.

Where this document and `narrator/avatar/unreal.py` disagree, one of them is a
bug. They are changed together or not at all.

---

## The contract

Five public functions on the presenter actor's Blueprint. The names are
case-sensitive and so are the **input pin names**, because Remote Control
matches parameters by pin name.

| Function | Pins | What it does in the editor |
|---|---|---|
| `SetSpeaking` | `speaking` (Boolean) | Talking idle on or off |
| `SetMood` | `name` (String), `weight` (Float) | Posture: back for bored, forward for excited |
| `PlayGesture` | `name` (String) | One montage on the `Gestures` slot |
| `SetCamera` | `name` (String) | View target, blended |
| `SetStage` | `count` (Integer) | How many seats are occupied |

That is the whole vocabulary. Everything the narrator can say to Unreal goes
through these five; a sixth is a change to this table, to `FUNCTIONS` in
`narrator/avatar/unreal.py`, and to `tests/test_unreal_transport.py`, in one
commit.

### The pin names are the part that goes wrong

A pin called `Name` where this table says `name` fails with HTTP 400 whose
body says only that a parameter could not be found. It does not say which
parameter, and it does not say that the case is the problem. Unreal will
happily let you name a pin `Name`, and it is in fact what you get if you type
the pin name with the capitalisation Blueprints use everywhere else.

`python -m tools.unreal_check` reads the pins back off the actor and prints
what it found against what is expected, which is the fastest way to see this.

### What must NOT go in Unreal

Nothing that decides *when* something happens. When the character blinks, how
long a mood lasts, where in a line a nod lands, how a laugh moves the face —
all of that is tested Python in `narrator/avatar/face.py`, sitting beside the
phoneme timing it has to agree with. A Blueprint copy of any of it would be a
second implementation, untestable, and wrong within a month.

Unreal's job is to render, to blend, and to own the things that are genuinely
Unreal's: montages, the state machine, the camera rig, the level.

---

## Building the five

### `SetSpeaking(speaking: bool)`

Sets a Boolean variable the body Animation Blueprint reads, driving the
`Idle ↔ Talking` state machine with a 0.4 s blend.

1. On the presenter Blueprint, add a Boolean variable `Speaking`.
2. Create a function `SetSpeaking` with one Boolean input pin named
   **`speaking`** (lower case).
3. In it: `Set Speaking` from the input pin.
4. In the body Anim BP's Event Graph, cast the pawn owner to your presenter
   Blueprint and copy `Speaking` into an Anim BP variable of the same name,
   then use it as the transition condition both ways.

The narrator sends this edge-triggered — once when a line starts and once when
it ends, never per frame. If the character sticks in the talking idle after a
line, the call to turn it off was dropped, not never sent; check
`stats()["dropped"]`.

### `SetMood(name: string, weight: float)`

The **body's** posture only. The face is already handled: by the time this
call arrives, `avatar/face.py` has the same mood on the same character's
blendshapes.

1. Function `SetMood`, input pins **`name`** (String) and **`weight`** (Float).
2. Switch on String (`name`) with pins for the moods you want to differ:
   `bored`, `excited`, `thinking`, `concerned`, and a default.
3. Each case sets a target spine/neck offset — lean back for `bored`, forward
   for `excited`, neutral otherwise — scaled by `weight`, and interpolated to
   over ~0.5 s rather than snapped.

Unknown mood names are normal and must not be an error: the narrator's mood
vocabulary is larger than any posture table needs to be. Let the default pin
go to neutral.

### `PlayGesture(name: string)`

1. Function `PlayGesture`, one String input pin **`name`**.
2. A Map of String → Anim Montage, or a Switch on String, resolving to a
   montage on the `Gestures` slot.
3. `Play Montage` on the body mesh.

The names the narrator sends, all of which have a matching face clip in
`avatar/face.py`:

```
nod  headshake  wink  eyebrow  lean_in  laugh  chuckle  sigh  shrug  hand_talk
```

**Underscores, never dashes.** The narrator's own beat is called `lean-in`;
`unreal.py` rewrites it to `lean_in` on the way out, because neither an Unreal
montage name nor a Warudo action can carry a dash. If you name the montage
`lean-in` it will never be found.

Blend each montage in and out over ~0.15 s so two arriving close together do
not pop. The narrator will not usually send two at once — `face.py` replaces a
running beat rather than blending it — but a market event and a host's line
can still land within a few hundred milliseconds of each other.

### `SetCamera(name: string)`

1. Function `SetCamera`, one String input pin **`name`**.
2. Switch on String → the matching Cine Camera Actor.
3. `Set View Target with Blend`, blend time 0.5, `VTBlend_EaseInOut`.

Names are whatever the caller uses; the build sheet's are `main`, `closeup`
and `chart`. There is no config field for these — the caller passes the name
it wants, so adding a fourth camera is a Blueprint change and a caller change,
not a config migration.

### `SetStage(count: int)`

1. Function `SetStage`, one Integer input pin **`count`**.
2. `count == 1`: hide the second seat's MetaHuman, frame `Cam_Main` on one.
3. `count == 2`: show it, and reframe to hold both.

The narrator already routes the speaking character's mouth to the right Live
Link subject; this is only what is visible.

---

## Transport 1: Remote Control (recommended)

HTTP, and it tells you when it failed. Use this one unless a firewall makes it
impossible.

### Enabling it

1. Edit → Plugins → enable **Remote Control API** → restart.
2. **Start the web server.** This is the step everyone misses: enabling the
   plugin gives you the endpoints, it does not start the server. Either tick
   Project Settings → Plugins → Remote Control → *Start Web Control Server on
   Startup*, or run `WebControl.StartServer` in the editor console each
   session.
3. The HTTP server is on port 30010 by default.

If `tools/unreal_check.py` reports `MISS ping` and Unreal is plainly running,
it is almost always step 2.

### The object path

Right-click the presenter actor in the World Outliner → **Copy Reference**.
You get something shaped like:

```
/Game/Maps/Studio.Studio:PersistentLevel.BP_Presenter_C_1
```

Put it in `config.toml`:

```toml
[character.unreal]
transport = "remote_control"
remote_control_url = "http://127.0.0.1:30010"
object_path = "/Game/Maps/Studio.Studio:PersistentLevel.BP_Presenter_C_1"
```

`python -m tools.unreal_check` prints the path it resolved, on its own line,
formatted so the whole line can be pasted into `config.toml` without editing.

**The path is per-level and per-instance.** It changes if you rename the
actor, duplicate it, or move it to another level. A path that worked last week
is not evidence that it works now.

### Play-In-Editor renames everything

This one costs an afternoon. When you press Play in the editor, Unreal
*duplicates* the world, and every object path in the running copy gains a
`UEDPIE_0_` prefix:

```
/Game/Maps/UEDPIE_0_Studio.Studio:PersistentLevel.BP_Presenter_C_1
```

The path you copied from the editor world therefore resolves to an actor that
exists but is not the one on screen. Calls return 200 and nothing moves, which
is the worst possible symptom.

Three ways out, best first:

- **Run Standalone Game** (Play dropdown → Standalone Game), or a packaged
  build. No PIE prefix, no duplication, and it is what the build sheet's Day 4
  says to stream from anyway because editor overhead costs frames.
- Copy the reference again *while PIE is running* and use that path for the
  session. It changes every time you press Play.
- Put the actor in a Remote Control preset and expose the function there
  instead, which is stable across PIE — more setup, and outside what this
  narrator needs.

If `unreal_check` says `OK ping`, `OK describe`, all five functions found, and
the nod still does not play, you are talking to the editor world while
watching the PIE world.

---

## Transport 2: OSC (the fallback)

UDP to an OSC Server node in the level Blueprint. No object path to copy, no
web server to start — that is the whole appeal.

In exchange there is **no reply**. A call that vanishes into a closed editor, a
wrong port or a blocked firewall looks exactly like one that worked. On this
transport `stats()["sent"]` means "the kernel accepted the datagram", not
"Unreal ran the function", and `unreal_check` will refuse to print a tick for
a ping it cannot actually perform.

```toml
[character.unreal]
transport = "osc"
osc_host = "127.0.0.1"
osc_port = 8000
```

### The messages

| Address | Type tags | Arguments |
|---|---|---|
| `/character/speaking` | `,i` | 1 or 0 |
| `/character/mood` | `,sf` | name, weight |
| `/character/gesture` | `,s` | name |
| `/character/camera` | `,s` | name |
| `/character/stage` | `,i` | count |

**Booleans travel as integers.** Unreal's OSC Server node reads a Boolean pin
off an integer argument, and the OSC bool type tags are not worth the
compatibility risk for one flag.

### Wiring it

1. Edit → Plugins → enable **OSC** → restart.
2. In the level Blueprint, on Begin Play, create an OSC Server bound to
   `0.0.0.0:8000` and set it to start listening.
3. Bind the message-received event, switch on the address, and call the
   matching function on the presenter actor.
4. **Firewall**: Windows blocks inbound UDP 8000 to `UnrealEditor.exe`
   silently. Add an inbound rule, and a second one for the packaged
   executable when you get there. This is the same trap as UDP 11111 for Live
   Link, and it fails the same way: nothing arrives, nothing errors.

Put a Print String on the address switch while you are wiring this up. It is
the only feedback this transport can give you.

---

## Transport 3: `none`

```toml
[character.unreal]
transport = "none"
```

The face without the body. Live Link still drives the MetaHuman, the character
still blinks, drifts and lip-syncs; nothing tries to reach the editor's
control surface. This is the right setting while you are still building the
Blueprint, and `unreal_check` will tell you plainly that there is nothing to
check.

An empty `object_path` under `remote_control` behaves the same way, on
purpose: an operator who has not been into the editor yet gets a warning in
the log telling them where the path comes from, and a face that still works.

---

## Checking it

```
python -m tools.unreal_check                # ping, describe, play a nod
python -m tools.unreal_check --no-gesture   # check only; move nothing
python -m tools.unreal_check --gesture shrug
python -m tools.unreal_check --json         # the raw describe reply
```

It exits non-zero when anything is wrong, so it belongs in a pre-stream
script. A healthy run:

```
transport      remote_control -> http://127.0.0.1:30010

  [OK  ] ping           Unreal 5.6.0
  [OK  ] describe       BP_Presenter_C

  the contract (UNREAL_SETUP.md)
    [OK  ] SetSpeaking(speaking)
    [OK  ] SetMood(name, weight)
    [OK  ] PlayGesture(name)
    [OK  ] SetCamera(name)
    [OK  ] SetStage(count)

  [OK  ] played 'nod' -- watch the character

  paste this into config.toml under [character.unreal]:

    object_path = "/Game/Maps/Studio.Studio:PersistentLevel.BP_Presenter_C_1"
```

### When it fails

| Line | What it actually means |
|---|---|
| `MISS ping` | The web server is not running. Enabling the plugin is not starting the server — see above. |
| `MISS describe` | The server answered; the object path does not resolve. Re-copy it. If you are in PIE, read the `UEDPIE_0_` section. |
| `MISS SetMood ... missing ['weight']` | The function exists; a pin is named differently. Case matters. |
| `MISS SetCamera -- not on this actor` | Function missing, misspelt, or not marked public on the Blueprint. |
| Everything OK, nothing moves | You are talking to the editor world and watching the PIE world. Run Standalone. |

---

## What the narrator sends, and when

| Moment | Call |
|---|---|
| A line starts | `SetSpeaking(true)`, then `SetMood(mood, weight)` if the line has one |
| A beat lands mid-line | `PlayGesture(name)` at the word it was written against |
| The line ends | `SetSpeaking(false)` |
| Podcast mode opens or closes | `SetStage(2)` / `SetStage(1)` |
| A camera change | `SetCamera(name)` |

`SetSpeaking`, `SetCamera` and `SetStage` are **edge-triggered** — sent only
when the value changes. `SetMood` is not: the same mood twice is two lines in
the same mood, and the posture has to be re-asserted or the second line is
played by a body that has already relaxed out of it.

Identical calls inside 50 ms are collapsed into one, which stops a handover
sound running into the line it was covering from sending `SetSpeaking(true)`
twice.

---

## Guarantees this side makes

- **Nothing here can raise into the narration loop.** `call()` is invoked from
  the speaking path. A misspelt function name, a missing parameter, a closed
  editor and a blocked port are all counted and logged, never raised, because
  none of them is worth costing the stream the line that was being spoken.
- **Nothing here can block it.** Calls go into a bounded queue that one worker
  task drains, and the HTTP request runs in a thread, so an editor stalled
  mid-frame cannot park the loop that is also pacing the mouth.
- **A full queue drops its oldest entry**, not its newest. Thirty-two calls
  deep is already a broken situation, and the most recent instruction is the
  one that describes the present.
- **An error is logged once**, not once per call. An editor that is closed is
  closed for every call, and a stream is eight hours long.

Counters are on `stats()`: `sent`, `failed`, `dropped`, `rejected`, `queued`.
`rejected` is the interesting one — it means this repo called something that
is not in the contract, which is a bug here rather than a problem in Unreal.

---

## The face

Everything above is the body. This is the other half, and it is the half to
get working first.

Subject names are matched by string and Unreal says nothing when it fails to
find one. `[character.livelink] subject` must equal the MetaHuman's Live Link
subject exactly, case included; `subject_2` is the second seat and is not
streamed at all until podcast mode seats somebody in it — a second subject
that appears from the first frame and never moves is just a source in Unreal's
panel that somebody has to explain.

**Firewall: UDP 11111 inbound.** The Unreal Editor and a packaged build are
two separate applications to Windows Firewall, so allowing one does not allow
the other — a face that works in the editor and dies in the packaged game is
almost always this and nothing else.

Live Link source interpolation smooths a frame that arrives late. It cannot do
anything sensible with a burst arriving at once, which snaps the face, so the
sender drops its backlog rather than replaying it and lets the frame index
jump. A gap in the timeline is something the source handles; eight frames all
stamped as current is not.

First light, before anything else:

```
python -m tools.livelink_check --blink
```

If the MetaHuman blinks and drifts, the whole chain is proven — encoder,
socket, firewall, Live Link source, subject name and ARKit mapping. If it does
not, exactly one of those is wrong and the rest need not be investigated.

Measuring the audio/face offset:

```
python -m tools.livelink_check --pulse
```

Opens the jaw fully once a second and prints a line at the same instant.
Record the screen and the console in OBS and count frames between the two.

Recorded gesture clips are optional — every beat has a procedural one. To
capture your own, `python -m tools.record_clip nod` listens on 11111 and
writes `clips/nod.csv` from the Live Link Face app; close Unreal first or pass
`--port`, since two processes cannot bind the same UDP port.

### Note the ordering

Prove the face with `--blink` before you build a single Blueprint function.
The face needs no object path, no web server, no Remote Control plugin and no
Blueprint — if it works, everything that follows is additive, and if it does
not, none of the rest of this document can help you.
