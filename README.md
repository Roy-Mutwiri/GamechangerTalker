# Trade Fix Narrator — speech core

> **Just want to run it?** Download
> **[GamechangerTalkerSetup.exe](https://github.com/Roy-Mutwiri/GamechangerTalker/releases/latest)**
> and see [INSTALL.md](INSTALL.md). The installer fetches Python, PyTorch, the
> voice, the AI models and MetaTrader 5 on its own. The rest of this file is
> how the thing works.

A narrator for a live XAUUSD stream. It reads **the operator's own sentences**
aloud with current numbers in them, and drives a 3D avatar's mouth while it
does.

It does not decide what to say. A human author writes every sentence as a
template; the system fills in live numbers and chooses *when* each line is
appropriate. **There is no LLM anywhere in this pipeline**, by design. The
relationship is audiobook narrator to author, not analyst to market.

It never generates trade recommendations, entries, stops or targets. Those
appear only if the operator typed them into the override channel or wrote them
into a template himself. There is a test that enforces this on the shipped
library (`tests/test_library.py::test_no_shipped_template_gives_trade_instructions`).

### Where everything is

| | |
|---|---|
| `narrator/` | the application — see `ARCHITECTURE.md` for a table of every module |
| `templates/` | **the script.** JSON, hot-reloaded, and where most of the tuning happens |
| `config.toml` | every tunable, with the reasoning next to each default |
| `tools/` | 18 diagnostics and rigs — the index is further down |
| `avatars/` | 16 CC0 VRM models, committed because `config.toml` names them |
| `warudo/` | **Warudo's own scene file.** Half the avatar setup lives inside the Warudo install rather than in a repo, so a copy is kept here — without it a clone gets a narrator talking to nothing. See `WARUDO_SETUP.md` §0 |
| `tests/` | the suite, plus one deterministic M1 fixture |

The four documents: this one for *how to run and tune it*, `ARCHITECTURE.md`
for *how it is built*, `WARUDO_SETUP.md` for *the avatar, end to end*, and
`DEPLOY.md` for *the hosted browser UI*. `requirements.txt` is the whole
install list — including the ~10 GB pip cannot fetch — not just the pip half.

---

## Status

| Milestone | What | State |
|---|---|---|
| 1 | Headless dry run — MT5 → facts → templates → terminal transcript | **done** |
| 2 | Number normalization | **done** |
| 3 | Kokoro speech engine + phrase cache | **done** |
| 4 | Phonemes → visemes | **done** |
| 5 | Warudo websocket bridge + emotes | **done** (blueprint in `WARUDO_SETUP.md`) |
| 6 | Audio playback, console UI, override channel | **done** |

Milestone 1 still matters after the rest: run `--dry-run` against live data for
an hour and tune the template library off the transcript. If the transcript
does not read well, no amount of TTS quality saves it.

## Run it

```powershell
.\run.ps1 -Replay              # recorded bars, real audio, real time
.\run.ps1                      # live: MT5 + Kokoro + Warudo
.\run.ps1 -Replay -DryRun      # transcript only, no audio, fast
```

The dashboard fills the terminal: a live fact panel on the left, the rolling
transcript on the right, connection/cache/density status along the bottom, and
an input line.

**Anything you type is spoken next, at priority 5.** It pre-empts: the current
line finishes, then yours goes. Numbers you type are normalized ("3341.20"
comes out "thirty-three forty-one twenty"), and `{fact}` slots are filled from
the current facts.

| Command | Effect |
|---|---|
| `/mute` `/unmute` | silence the templates; your overrides still speak |
| `/skip` | cut the line being spoken right now |
| `/reload` | reload the template library immediately |
| `/quiet 300` | suppress all non-override speech for N seconds |
| `/quit` | stop cleanly and print the session summary |

A **browser UI** opens automatically at `http://127.0.0.1:8770`. It carries
the dashboard, the transcript, the override box, a **voice picker** for all 28
Kokoro voices, and the avatar — one view instead of three windows.
`--no-web` turns it off; `--plain` gives the scrolling transcript instead of
the terminal dashboard.

### The avatar panel

Two sources, one toggle in the corner of the stage:

* **warudo** — Warudo's render window, mirrored in live. Captured with
  `PrintWindow`, so it keeps working when the browser is on top of it, with a
  screen-grab fallback for when Unity refuses. ~13fps at 185 KB/s on
  loopback; encoding runs on its own thread so it never touches the loop
  driving the visemes.
* **face** — the built-in SVG face, driven by the same 60fps viseme stream
  that goes to Warudo. Useful before Warudo is set up, and for checking that
  the phoneme pipeline is right independently of the avatar.

The toggle is remembered per browser. If frames stop arriving the panel falls
back rather than freezing on a stale image.

### Two buttons worth knowing

**`frame`**, on the avatar panel. Turns the stage into a camera control:
**drag to orbit** the avatar, **scroll to zoom**, **double-click to reset**.
The camera always looks back at the character, so no amount of dragging loses
it off the edge. Framing a VTuber by typing transform numbers is miserable,
and doing it in Warudo's own window means alt-tabbing away from the thing you
are trying to frame. Needs the `cam_pos`/`cam_rot` pairs in the blueprint --
see `WARUDO_SETUP.md`.

**`arrange`**, top right. Every panel gets a grab handle; drag them into
whatever order suits the stream. The layout is stored per browser, so it
survives a reload without the narrator having to know about it.

Both exit on **Escape**, and neither starts a drag when the pointer is on a
control — the buttons live inside the stage, and capturing the pointer there
made them look dead.

### The avatar picker, and settings

The dropdown on the avatar panel switches character. Each one carries its own
**setting** in `config.toml`: where they stand, how far they are dropped
towards seated height, and the shot the camera takes of them.

```toml
[[warudo.avatars]]
file = "Shipilka.warudo"
setting = "seated · shoulders"
y = -0.22        # chair height rather than standing
facing = -12.0   # turned off-axis, the way anyone at a desk is
yaw = 20.0       # camera round so the desk sits over her shoulder
distance = 0.95  # head and shoulders
offset = -0.20   # her on the left third, the room filling the right
```

`offset` is the one worth knowing: it slides the camera sideways without
turning it, so the subject sits off-centre. That is the difference between a
passport photo and a stream layout.

**Switching reloads Warudo's scene**, which takes a few seconds and is the
only thing that works: setting `Source` on a live character unloads the old
model and does not load the new one.

**Nothing here makes a character physically sit** — `StreamingAssets/
CharacterAnimations` is empty on this install, and a seated pose needs a Unity
`.anim`. A shoulders-up frame never shows a lap, so dropping the character to
seated eye height reads the same. Drop `.anim` files into that folder and the
idle animation can be set per avatar the same way as the shot.

**If the avatar is framed badly** (a shoulder filling the screen), that is
Warudo's camera, not the capture. In Warudo's render window: hold **right
mouse** to look around, **WASD** to move, **Q/E** down and up.

## The tuning loop

Two commands, and between them they are the whole workflow:

```powershell
python -m narrator.main --simulate --minutes 720   # what WOULD it say?
python -m tools.review                             # what DID it say?
```

`--simulate` replays a **12-hour session in about 8 seconds**, deterministically
— no wall clock, no audio, no GPU. Same fixture and same `--seed` gives a
byte-identical transcript, which is what makes an A/B of a template change
readable: the difference came from your edit, not from how the clock landed.

`tools/review.py` reads a real stream back out of SQLite and reports filler
share by hour, sentences that repeated inside ten minutes, templates that keep
firing back-to-back about the same fact, and everything that never fired.

Note the two disagree on density by about 3×: real Kokoro utterances run
longer than the word-count estimate the simulation uses. Judge *which lines
fire* from the simulation and *pacing* from a real run.

### Every tool

All are `python -m tools.<name>`, and all take `--help`. Most exist because
something was hard to see: a mouth that will not move and a feed that is ten
minutes stale both look exactly like working software from the outside.

| Setup and the avatar | |
|---|---|
| `warudo_setup --check` | is the Warudo half installed on this machine? |
| `warudo_setup` | install it — scene, blueprint and models. See §4 below |
| `warudo_export` | write the live Warudo scene back into `warudo/`, so the next machine gets it |
| `avatar_check [model.vrm]` | will this avatar lip sync? Reads the VRM header — no Unity, no Warudo. Catches the vowel clip that binds to nothing |
| `frame_avatar --shot bust` | point the camera at the avatar's face, sized off its own head bone |
| `liven_avatar` | breathing, sway and eye contact on, so a still avatar stops looking dead |
| `lipsync_check` | send each viseme at full weight, capture the render, measure the change. Answers "is the lip sync working" with a number instead of an opinion; `--character 2` for the second host |
| `motion_check --seconds 12` | is the avatar moving at all? Mean pixel change over time, with the face box and the blink peak broken out |
| `loopback_check --tone` | can Warudo *hear* the narrator? Records each loopback device and reports the level — and the host API, which matters just as much |

| Speech and script | |
|---|---|
| `speech_check` | Kokoro → audio → phonemes → visemes, with a mouth-opening scope |
| `delivery_demo` | one line flat and then delivered, side by side, to a wav |
| `review` | read a real stream back: filler share, repeats, clustering, what never fired |
| `bench` | hot-path microbenchmarks |
| `ui_demo` | one frame of the dashboard, with sample data |

| Market | |
|---|---|
| `feed_check` | prove the price is real, current, and gold |
| `chart_check` | show what the hosts see when they look at the chart |
| `make_fixture --days 20` | regenerate the deterministic replay fixture |

---

## Setup on a clean machine

Windows 11, Python 3.11, RTX 5080.

### 1. Torch, from the CUDA 12.8 index — do this first

The 5080 is Blackwell, compute capability **sm_120**. Default PyPI torch wheels
have no sm_120 kernels: they import fine, report `cuda.is_available() == True`,
and then die on the first kernel launch with

```
CUDA error: no kernel image is available for execution on the device
```

which surfaces inside Kokoro, an hour into a stream. So:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt
```

Verify:

```powershell
python -c "import torch; print(torch.cuda.get_device_capability())"   # (12, 0)
```

`narrator/preflight.py` checks this at boot, launches one real kernel to prove
`is_available()` is not lying, and refuses to start otherwise. Milestone 1 skips
the check (`--dry-run` needs no GPU).

### 2. MetaTrader 5 terminal

* Install the terminal, log into the broker account, leave it **running**. The
  `MetaTrader5` package attaches to a running terminal; it does not launch one.
* Open the gold symbol in Market Watch so history is available.
* Tools → Options → Charts → set "Max bars in chart" high enough for 500 bars on
  every timeframe.
* Symbol naming differs per broker (`XAUUSD`, `GOLD`, `XAUUSD.m`, `XAUUSD.pro`).
  Leave `market.symbol = ""` in `config.toml` and the adapter scans
  `mt5.symbols_get()` and logs which one it picked, or set it explicitly.

### 3. Kokoro

`pip install -r requirements.txt` brings it in. The model downloads on first
use (~330 MB, cached afterwards):

```powershell
python -m tools.speech_check
```

That synthesizes three lines, reports whether real token timestamps or the
proportional fallback is driving the timing, and draws the mouth opening over
time so you can see the visemes without Warudo running.

Kokoro-82M is small, fast, and phoneme-based — which is what the viseme mapping
depends on. Do not swap it for an amplitude-driven mouth.

### 4. Warudo

Install it from Steam (app 2079120), launch it once so it creates its folders,
**close it**, then:

```powershell
python -m tools.warudo_setup --check     # what is missing; writes nothing
python -m tools.warudo_setup             # install it
```

Half of the avatar setup does not live in this repo by default — it lives inside
the Warudo install, as a scene file holding the character, the camera and the
`narrator` blueprint that turns `{"action": "viseme_aa", "data": 0.8}` into a
blendshape. That scene is committed here as `warudo/DefaultScene.json`, and the
command above installs it along with the sixteen models in `avatars/`, which
Warudo can only load out of its own Characters folder. Without it you get a
narrator sending perfectly good viseme frames at a Warudo with nothing
listening, an empty avatar picker, and no error anywhere.

Warudo must be closed while it runs: it holds the scene in memory and rewrites
the file on exit. The tool refuses rather than letting the edit vanish later.

Two things it cannot do for you: **read the WebSocket port** out of Warudo's
settings and put it in `config.toml` (`warudo.port`; `19190` here), and **turn
Warudo's own lip sync off** for the character, which otherwise fights the
narrator's visemes. `WARUDO_SETUP.md` §0 covers both, and documents the
blueprint node by node for anyone rebuilding it by hand.

Run without it any time with `--no-avatar`. A dead bridge is never fatal — the
narrator keeps speaking, because audio is the stream and the avatar is
decoration.

---

## First run

Markets closed? Use the replay adapter. It feeds recorded M1 bars through the
exact same interface as the live feed, with a virtual clock:

```powershell
python -m narrator.main --dry-run --replay --speed 120 --minutes 45
```

Live data, still no audio:

```powershell
python -m narrator.main --dry-run
```

Output:

```
14:32:07  [price.drift]      Gold's at thirty-three forty-one twenty, barely moved in twenty minutes.
14:32:41  [levels.approach_pdl]  We're four dollars off yesterday's low. Still untested.
14:33:15  [session.pre_ny]   Forty-two minutes to the New York open.
14:34:02  --- silence 38s (all candidates on cooldown - 18 waiting) ---
```

On exit it prints a tuning summary: line count, speech density against target,
average gap, which templates fired most, and **which never fired at all** —
that last list is usually the most useful thing on the screen.

### Other flags

| Flag | What |
|---|---|
| `--dry-run` | print instead of speaking. Stays available permanently. |
| `--replay [CSV]` | recorded bars instead of MT5 |
| `--speed N` | replay speed multiplier (60 = a simulated minute per second) |
| `--minutes N` | stop after N simulated minutes |
| `--seed N` | make variant selection reproducible |
| `--validate-only` | check config + templates and exit |
| `--list-facts` | every fact name and its spoken format |
| `--list-templates` | the whole library with conditions |
| `--no-avatar` | skip Warudo |
| `--skip-cuda` | skip the Blackwell preflight |
| `-v` | debug logging to the terminal as well as the file |

### Fixture data

`tests/fixtures/xauusd_m1.csv` ships with ten days of synthetic-but-shaped M1
bars (quiet Asia, busy London, busiest overlap, weekends removed, fixed seed).
Regenerate or extend:

```powershell
python -m tools.make_fixture --days 20
```

To replay real data, export M1 bars from MT5 to a CSV with the columns
`time,open,high,low,close,volume` and point `replay.csv` at it.

---

## Writing templates

`templates/*.json` is the script. It is the file you edit daily.

```json
{
  "id": "price.drift",
  "category": "price",
  "priority": 3,
  "when": "minutes_since_move > 15 and market_open",
  "cooldown": 900,
  "max_per_session": 8,
  "variants": [
    "Gold's at {price}, barely moved in {minutes_since_move}.",
    "Still sitting around {price}. Quiet stretch."
  ],
  "emote": "neutral"
}
```

| Field | Meaning |
|---|---|
| `id` | unique across all files; appears in the transcript and the log |
| `category` | defaults to the filename; `bridge` is special (see below) |
| `priority` | 1 (lowest) to 5. **5 is reserved for the operator override.** |
| `when` | condition over facts — see below. Omit or `"True"` for always. |
| `cooldown` | seconds before this template may fire again |
| `max_per_session` | hard cap, stops one line dominating a session. Counters reset when the **trading session** turns over — Tokyo, London, the overlap, New York — with `scheduler.session_reset_hours` as a backstop for long single sessions. |
| `variants` | round-robin through a shuffle; never the same one twice running |
| `emote` | optional Warudo expression |
| `enabled` | set `false` to park a template without deleting it |
| `notes` | free text, ignored by the engine |

Edits are picked up **while the stream is running**. Cooldowns and per-session
counters survive the reload, so saving the file does not unleash the whole
library at once. A broken edit is rejected and the previous good library stays
live — check the log.

### The condition language

A tiny safe expression language over the fact dict. It is parsed with
`ast.parse` and walked; `eval()` is never used. Permitted: fact names,
`< <= > >= == !=`, `and or not`, `+ - * /`, number/string/bool literals, and
`in` / `not in` against a list of literals. Anything else — calls, attributes,
indexing, comprehensions, f-strings — is rejected **at load time**.

```
minutes_since_move > 15 and market_open
session == "london_ny"
minutes_to_next_session < 60 and next_session == "newyork"
pdl_dist < 5 and not pdl_tested
atr_ratio > 1.5
session in ["london", "london_ny"]
```

A fact that cannot be computed yet (not enough history, feed down) is `None`,
and any comparison against `None` is False — the template simply does not fire
until its inputs exist.

Typos are loud, never silent:

```
templates/price.json:price.drift: condition 'mintes_since_move > 15' refers to
unknown fact(s) mintes_since_move. Did you mean minutes_since_move?
```

### Slots

`{fact}` renders the fact in its declared spoken format; `{fact:format}`
overrides it. `python -m narrator.main --list-facts` prints all 45 facts and
their formats. If a slot's fact is `None` at the moment of speaking, the render
fails and the scheduler moves to the next candidate — a sentence with a hole in
it is worse than silence.

### Rhythm, and why short lines sound synthetic

Uniform sentence length is the loudest machine tell there is. Generated text
clusters in a narrow band — typically 15–20 words — with an even cadence
throughout; people alternate a three word reaction with a fifty word ramble.
The **standard deviation of line length** is a more reliable signal than
vocabulary or word choice.

Measured on this library before any of it existed:

```
sd 4.21   mean 9.6   89% of lines between 5 and 19 words
35+ words:  0.0%     <- nothing long existed at all
```

Every line was a short observation, so the rhythm never changed. Now:

```
sd 10.88  mean 12.3  35+ words: 6.1%   longest 56
```

Two things did it. `templates/longform.json` supplies the missing end of the
range — genuine rambles built out of real facts, so length carries information
rather than padding. And the story memory exposes **`last_line_words`**, so a
template can read the rhythm and stand down: every long template requires
`last_line_words < 14`, so rambles never stack, and the two-word reactions
require `last_line_words > 18`, so a beat only ever lands against something.

`tests/test_rhythm.py` pins it. The library once flattened to sd 4.2 without
anyone noticing, because nobody had a number to check.

### Stories, and why they are not an LLM

The fact engine answers *"what is true right now"*. That is enough to describe
a market and not enough to narrate one. A person watching the same screen for
six hours does something it cannot: they remember. They say *"that level I
flagged twenty minutes ago — gone"*, or *"first real move since the open"*.
Those lines land because they carry the session's history in them.

`narrator/script/story.py` keeps two small ledgers — **what has been said
about what**, and **what has happened** — and derives facts from them:

| fact | what it answers |
|---|---|
| `callback_level` | a level *this narrator flagged* that has since broken |
| `minutes_since_pdl_mentioned` | how long since we brought that up |
| `levels_broken` | how eventful the session has been |
| `minutes_since_event` | how long since anything actually happened |
| `events_this_session`, `last_event` | the shape of the session so far |

`templates/callbacks.json` uses them. The important one is
`story.level_paid_off`: it fires **only** when a level was mentioned *and then*
broke, in that order. Every narrative fact is `None` until it is earned, and a
comparison against `None` is False, so a template with nothing to call back to
stays silent rather than firing with a hole in it — a narrator that says "that
level I mentioned" without having mentioned it is worse than one that never
says it.

This adds no LLM and no signal generation. It contributes facts; the words stay
in the library where you can read and edit them, and it still cannot invent a
price. Measured over a simulated 12-hour session: 25 story lines out of 982.

### Bridges

`templates/bridges.json` (category `bridge`) is the filler pulled only after
`scheduler.bridge_after_seconds` of silence when nothing else qualifies. Keep
several of them unconditional and short.

---

## How the pieces fit

```
MT5 adapter      polls tick 250ms / bars 5s, reconnects with backoff
      v
Fact engine      ~45 named facts, a pure function of the bar store
      v
Conditions       which templates are valid right now
      v
Scheduler        cooldowns, priority groups, recency weighting, pacing
      v
Renderer         slot filling + number normalization
      v
[Milestone 3]    Kokoro -> audio + phonemes -> visemes -> Warudo
```

Selection happens **at the moment of speaking**, never earlier, and only one
line is ever chosen at a time. A line queued forty seconds ago may quote a price
that has since moved.

### Number normalization

The highest-value deterministic component in the system. Traders do not say
"three three four one point two zero".

| Value | Format | Spoken |
|---|---|---|
| 3341.20 | price | thirty-three forty-one twenty |
| 3341.00 | price | thirty-three forty-one |
| 3341.05 | price | thirty-three forty-one oh five |
| 3400.00 | price | thirty-four hundred |
| −11.40 | change | down eleven forty |
| +2.50 | change | up two fifty |
| 0.35 | distance | thirty-five cents |
| 1.85 | distance | a dollar eighty-five |
| 47 | duration | forty-seven minutes |
| 1.83 | ratio | one point eight |
| 0.60 | percent | sixty percent |

Numbers use the British/East African convention ("three hundred and forty-one").
Changes carry a direction word and never a minus sign.

---

## Measured performance

```powershell
python -m tools.bench                                    # microbenchmarks
python -m narrator.main --dry-run --replay --profile ... # real in-loop latency
```

Measured on the target machine (Ryzen AI 9, Python 3.11), 134 templates, full
500-bar store:

| | mean | p95 | worst |
|---|---|---|---|
| **selection tick, in the real loop** | **1.27 ms** | 2.05 ms | 19 ms |
| facts.compute (46 facts) | 0.26 ms | 0.27 ms | 0.46 ms |
| conditions.evaluate ×134 | 0.10 ms | 0.10 ms | 0.16 ms |
| scheduler.select | 0.12 ms | 0.12 ms | 0.21 ms |
| render, 4 slots | 0.015 ms | 0.015 ms | 0.066 ms |
| printer.line (rich) | 0.11 ms | 0.12 ms | 0.26 ms |
| sqlite write + commit | 0.08 ms | 0.09 ms | 6.3 ms |
| library.load (hot reload) | 2.6 ms | 2.7 ms | 13.5 ms |

**1.27 ms inside a 2000 ms tick — about 1,500× headroom.** The fact engine is
flat across market states (0.24–0.27 ms over 20 hours of clock sweep); it does
not degrade in dead markets.

Three things worth carrying into later milestones:

* **Benchmark numbers in a tight loop are ~3.6× optimistic.** The same work
  called once every 150 ms costs 1.48 ms versus 0.41 ms hot. `tools/bench.py`
  measures both; trust the cold one and the `--profile` one.
* **The transcript log was the one real hazard.** Default SQLite journalling
  committed in 7 ms typical / **200 ms worst**. At 60fps that is a dozen dropped
  viseme frames. WAL + `synchronous=NORMAL` took it to 0.08 ms / 6.3 ms.
* **The 19 ms worst-case tick is a GC pause**, and 60fps is a 16.7 ms frame
  budget. The Milestone 4 viseme pump must not share this loop's thread
  uncritically.

Memory over a 12-hour simulated stream: 42.9 → 45.2 MB RSS, growth decelerating
and flat in the second half — allocator arenas, not a leak. Every unbounded
structure is capped (bar store `maxlen`, `scheduler.recent`, and
`recent_speech` pruned per density window). Transcript log: 1.4 KB per line,
~1.5 MB per 12-hour stream.

## Logging

Every spoken line goes to SQLite (`logs/narrator.sqlite`) with the timestamp,
template id, rendered text and **a full snapshot of the facts that triggered
it**. That is what you read back to work out why a line fired when it did.

```sql
SELECT market_time, template_id, text FROM lines
WHERE run_id = (SELECT run_id FROM runs ORDER BY started_at DESC LIMIT 1);
```

Diagnostics go to `logs/narrator.log`. The terminal belongs to the transcript,
so only warnings and worse are printed there.

---

## Development

```powershell
pip install -e ".[dev]"
pre-commit install

pytest                      # everything
pytest -m "not slow"        # fast unit tests only
ruff check . && ruff format --check .
mypy
```

All four gates are enforced in CI (`.github/workflows/ci.yml`) on Windows, plus
a deterministic 12-hour simulation on every push — that last one catches
anything which stops the library speaking, without needing a market, a GPU or
an avatar. Run them locally before pushing; `ruff format` in particular will
happily rewrite a file you have not touched, and finding that out from a red
CI run is a wasted round trip.

**723 tests, 79% coverage** (the suite fails below 75%). The uncovered
remainder is hardware: a live MT5 terminal, a real audio device, a running
Warudo — `ui/capture.py` at 52% and `ui/webui.py` at 55% are the floor, and
both are things you can only really test by looking at them.

What is covered, and why those things and not others:

| Area | What is pinned down |
|---|---|
| normalizer | every cents boundary, round hundreds and thousands, negatives, zero — it runs thousands of times a stream and every mistake is audible |
| condition DSL | every unsafe expression shape is rejected **at load time**: calls, attributes, indexing, comprehensions, f-strings, walrus |
| fact engine | against fixture bars; purity (same inputs, same facts); session labels across a full simulated week |
| scheduler | cooldowns, caps, priority groups, min gap, density cap, bridges, override pre-emption, per-session resets |
| template library | every validation error names file + id + the bad reference; hot reload keeps cooldowns; a broken edit keeps the old library |
| speech | phoneme timing (both paths), viseme mapping, smoothing, the phrase cache round trip |
| avatar | VRM 0.x and 1.0 parsing, lip-sync verdicts, emote preset mapping |
| Warudo scene | that the committed scene still describes a working bridge, that grafting it onto a scene with different asset ids leaves nothing pointing at this machine's, and that finding the install works from a second Steam library |
| adapters | gold symbol auto-detection across seven broker naming schemes |
| integration | the whole app end to end, including that no digit ever reaches the transcript unspoken |

Guard tests worth knowing about: the shipped template library is checked for
anything resembling a trade instruction (`buy`, `go long`, `stop loss`,
`target`), and `--simulate` runs are asserted reproducible.

---

## Design constraints worth not forgetting

* **No LLM.** If a requirement seems to need one, it has been misread.
* **No signal generation.** The system never invents entries, stops or targets.
* **The stream survives everything.** MT5 disconnect, Warudo disconnect, a
  malformed template, a TTS failure, a market gap, a weekend. Log and continue.
  A crash mid-stream is the worst outcome in this system.
* **Never queue more than one line ahead.** Facts go stale.
* **Drive the mouth from phonemes, not amplitude.** Amplitude cannot tell "ee"
  from "oh" and the result always reads as slightly wrong.
* Out of scope: trade execution, charting, OBS, virtual audio cables.
