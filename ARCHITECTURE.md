# Architecture

The one-line version: **a narrator, not an analyst.** The operator writes
every sentence; this system decides which of his sentences is appropriate
right now, fills the numbers in, says it, and moves the avatar's mouth while
it does.

There is no language model in that core, by design. There is one beside it:
the two-host conversation layer, which is optional, off by default, screened
on every turn by `script/guard.py`, and which falls through to the library on
any failure. The library is what the stream is; the hosts are what it sounds
like between the things worth announcing.

```
  MetaTrader 5  ──►  MT5Adapter ─┐
                                 ├─►  FactEngine ──►  Conditions ──►  Scheduler
  recorded CSV  ──►  ReplayAdapter┘        │                              │
                                           │                              ▼
                                      ~46 facts                       Renderer
                                    (pure functions)                      │
                                                                          │
   audience ──► Audience ──┐                                              │
   (HTTP/JSONL)  filter    │                                              │
                           ▼                                              │
                    HostConversation ──► guard ──► expression.parse ──────┤
                           │                              │               │
                    BudgetedBackend                  mood + beats         │
                           │                                              │
                    OpenAICompatClient                                    │
                    / Ollama / Anthropic                                  ▼
                                                                   SpeechEngine
                                                                    (Kokoro)
                                                    ┌─────────────────┼─────────────┐
                                                    ▼                 ▼             ▼
                                                 audio out        phonemes      SQLite log
                                                 (sounddevice)        │
                                             ┌────────────────────────┴───────┐
                                             ▼                                ▼
                                          visemes                        ARKit mouth
                                    ┌────────┴────────┐                       │
                                    ▼                 ▼                       ▼
                                 Warudo          browser UI               Character
                               (WebSocket)       (WebSocket)          ┌───────┴──────┐
                                                                      ▼              ▼
                                                                  Live Link       Unreal
                                                                    (UDP)       (HTTP/OSC)
                                                                    face           body
```

Two renderers, one set of events. The VRM path and the MetaHuman path consume
the same utterances, emotes and beats; the phoneme spans are resolved once per
line and handed to both, so the two mouths agree by construction rather than
by coincidence.

## The rules that shape everything

**Selection happens at the moment of speaking, never earlier.** Facts go
stale; a line chosen forty seconds ago may quote a price that has moved. Only
one line is ever in flight.

**Nothing downstream calls `datetime.now()`.** It asks the adapter. That is
what lets the replay adapter run the same code against recorded bars on a
virtual clock, and what makes `--simulate` deterministic.

**Every failure loses at most one line.** MT5 dropping, Warudo dropping, a
malformed template, a TTS failure, a market gap, a weekend — all logged and
survived. A crash mid-stream is the worst outcome in this system.

**Missing data never speaks.** Any fact may be `None`; comparisons against
`None` are False, and a slot that renders empty aborts the line and the
scheduler moves to the next candidate. A sentence with a hole in it is worse
than silence.

## What is in the repository

```
  narrator/     the application            templates/  the script, as JSON
  tools/        18 diagnostics and rigs    tests/      the suite, plus a fixture
  avatars/      16 CC0 VRM models          warudo/     the Warudo scene
  scripts/      the one web build step     config.toml every tunable
```

Two of those are unusual and deliberate. **`avatars/`** holds ~68 MB of binary
models because `config.toml` names them and a clone without them cannot put
anybody on stage. **`warudo/DefaultScene.json`** is a copy of Warudo's own
scene file — the character, the camera and the blueprint that turns a websocket
message into a blendshape. That half of the system lives inside the Warudo
install rather than here, and a clone without it gets a narrator talking to
nothing, silently. `tools/warudo_setup.py` installs both; see `WARUDO_SETUP.md`.

## The modules

Every module in `narrator/`. Line counts are a rough guide to where the
complexity actually is: `hosts.py` and `main.py` are half the codebase.

| Module | Responsibility | Notable |
|---|---|---|
| `config.py` | load and validate `config.toml` | pydantic; every default is documented at its definition |
| `preflight.py` | boot-time environment checks | launches a real CUDA kernel, because `is_available()` lies on sm_120 |
| `main.py` | entry point, the run loop, every flag | the one place wall-clock time is allowed |
| `simulate.py` | deterministic whole-session replay | no wall clock, no audio |
| `logbook.py` | SQLite transcript log | WAL + `synchronous=NORMAL`: the default fsync cost 200 ms worst case |
| **market** | | |
| `market/types.py` | `Bar`, `Tick`, `BarStore`, the adapter interface | `time_scale` is how replay speed reaches the loops |
| `market/mt5_adapter.py` | live MT5 polling; `ReplayAdapter` on recorded CSV | symbol auto-detect; exponential reconnect; `advance_to()` is the deterministic entry point |
| `market/web_adapter.py` | public price feed, for a machine with no MT5 | measurably ~10 min behind: `--allow-delayed` labels the whole run |
| `market/sessions.py` | session windows, market hours | overlap precedence; memoised boundary scan |
| `market/facts.py` | the ~46 facts and their spoken formats | `FACT_FORMATS` is the entire template vocabulary |
| `market/trades.py` | what the operator is actually doing, off the live terminal | read-only, and **no credentials**: it attaches to a terminal already signed into, and never logs in or places an order |
| `market/chart.py` | the hosts' eyes on the operator's chart | vision model over a window capture |
| `market/chart_control.py` | drives TradingView | borrows focus and gives it back — Chromium ignores synthetic keys sent to an unfocused window |
| **script** | | |
| `script/conditions.py` | the `when` DSL | `ast.parse` + whitelist walk, never `eval()` |
| `script/library.py` | load, validate, hot-reload templates | errors name file + id + bad reference |
| `script/scheduler.py` | cooldowns, priority, recency, pacing, bridges | where "alive vs robotic" is decided; `max_silence_seconds` is the hard floor under dead air |
| `script/render.py` | slot filling | re-capitalises sentence starts after substitution |
| `script/hosts.py` | the two-host conversation | one turn ahead of the microphone; every turn screened |
| `script/expression.py` | the tags the model writes for its own face | one vocabulary, and the prompt section is generated from it so the two cannot drift |
| `audience.py` | the chat, and what the hosts may hear of it | mostly refusal: links, handles and numbers never reach a prompt |
| **llm** | | |
| `llm/base.py` | the two-method Backend interface | lives here so the budget can wrap one without importing hosts.py |
| `llm/openai_compat.py` | one chat completion, any OpenAI-compatible endpoint | typed failures, so the conversation stops substring-matching an SDK's error text |
| `llm/budget.py` | how many turns a night, and when | paces rather than caps; the one module below main.py allowed a real clock, because a provider's day is a real UTC day |
| `script/topics.py` | what the hosts have to talk about | kernels carry their own facts, so history is retold and never invented |
| `script/briefing.py` | what the hosts know beyond this second's price | digested into sentences: a model handed 200 OHLC rows quotes one at random |
| `script/story.py` | what has happened, and what was already said about it | two ledgers, so "third time we've tested this" is possible at all |
| `script/guard.py` | what may never be spoken | trade calls, invented memories, claims about a world they cannot see |
| **speech** | | |
| `speech/normalize.py` | numbers → what a trader says | the highest-value deterministic component |
| `speech/engine.py` | Kokoro, resident; disk phrase cache | cache stores phoneme spans, not just audio |
| `speech/phonemes.py` | phoneme timing | token timestamps, else weighted proportional |
| `speech/visemes.py` | phonemes → VRM blendshapes at 60fps | never amplitude; 40ms attack/release |
| `speech/arkit.py` | phonemes → the ARKit 52 ("Perfect Sync") | what the MetaHuman's mouth is made of; the jaw and the lips move independently |
| `speech/performance.py` | how a line is delivered, not what it says | only three levers exist — voice, speed, punctuation. Kokoro ignores SSML, tags and capitals |
| `speech/fillers.py` | the noise a person makes taking the floor | covers the synthesis gap on a handover, in the incoming host's own voice |
| `speech/playback.py` | audio out | Windows default device unless `audio.device` says otherwise |
| **avatar** | | |
| `avatar/warudo.py` | WebSocket bridge | bounded queue: drops frames rather than lagging |
| `avatar/channels.py` | market vs conversation: who owns the face | one rule, shared by both renderers — two copies would be two rules within a month |
| `avatar/livelink.py` | the Live Link Face wire, and the idle layer | golden-bytes tested against a known-good encoder; the uuid field has no length prefix |
| `avatar/face.py` | idle + mouth + mood + beat, composed at 60fps | the mouth replaces, the mood adds, the beat wins; nothing allocates per frame |
| `avatar/unreal.py` | the body: five calls over Remote Control or OSC | queued and drained by one task, so a stalled editor never reaches `_speak` |
| `avatar/character.py` | the one object `main.py` talks to | holds the single arbiter; every failure degrades to a face that still blinks |
| `avatar/emotes.py` | market events → expressions | edge-triggered, debounced |
| `avatar/vrm.py` | VRM inspection | reads the GLB header; no Unity needed |
| `avatar/install.py` | where Warudo is on this machine | one search, shared — a second opinion is a silent no-op |
| `avatar/roster.py` | the avatar picker's list | measures focus height off the head bone; drops models Warudo cannot see |
| `avatar/scene.py` | writes an avatar and its shot into Warudo's scene | changing the file and reloading is what actually works |
| `avatar/duet.py` | two characters, each with its own mouth | clones the viseme pairs onto a `viseme2_` prefix |
| **ui** | | |
| `ui/webui.py` + `ui/web/` | browser dashboard and avatar | whole viseme track sent per utterance |
| `ui/dashboard.py` | terminal UI | raw keystrokes, so rich can own the screen |
| `ui/console.py` | the plain transcript | what you read back after a run |
| `ui/capture.py` | Warudo's render window into the browser UI | `PrintWindow`, with an `mss` fallback when it returns black |

## Threads and loops

Everything is one asyncio loop except three deliberate exceptions:

- **Kokoro synthesis** runs in a worker thread (`asyncio.to_thread`). It is
  the only CPU-heavy thing in the process and would otherwise stall visemes.
- **The keyboard reader** is a thread, because there is no portable async
  stdin on Windows and `input()` fights whatever is repainting the terminal.
- **The web UI's HTTP server** is a thread; only the WebSocket half is async.

Measured: the selection loop costs ~1.3 ms against a 2000 ms budget. The one
number to watch is the p99 of ~12 ms, which is a GC pause, against a 16.7 ms
frame budget at 60fps.

The MetaHuman's face is a fourth loop, at 60fps, and it is sized against that
same pause: composing a frame costs 0.43 ms worst case and encoding it 0.01
ms, so the GC is the only thing that can ever cost a frame. The mouth track
for a whole line is built once when the line starts (6 ms for a six-second
line) rather than sampled per frame, which is what keeps the per-frame cost
an array lookup. A late tick drops its backlog rather than replaying it: Live
Link smooths one late frame and snaps on eight arriving together.

## Data that outlives a run

- `logs/narrator.sqlite` — every spoken line with a full fact snapshot. WAL
  mode with `synchronous=NORMAL`, because the default fsync commit measured
  200 ms worst case and that is a dozen dropped viseme frames.
- `cache/phrases/` — synthesised audio plus its phoneme spans, keyed on
  `(rendered text, voice, speed)`.
- `templates/*.json` — the script. Hot-reloaded; cooldowns survive the reload.
- **Warudo's `DefaultScene.json`, which is not under this repo at all.** It
  lives in the Warudo install, and the narrator writes to it: an avatar switch
  is a scene edit plus a reload, because setting `Source` on a live Character
  unloads the old model without loading the new one. Two consequences worth
  keeping in mind. Warudo rewrites that file on exit, so any edit made while
  it is running is discarded — every tool here refuses, or says so. And the
  repo's copy in `warudo/` is a *snapshot*: change the scene and it does not
  reach the next machine until `python -m tools.warudo_export` is run.

## The two tuning loops

```
  what WOULD it say?            what DID it say?
  python -m narrator.main       python -m tools.review
      --simulate --minutes 720
  deterministic, ~8s for 12h    filler share, repeats, clustering
```

Use the first to A/B a template change, the second to find what grated on a
real stream. Note that simulated density is roughly a third of live density:
real Kokoro utterances run longer than the word-count estimate.

## Deliberately out of scope

Trade execution, signal generation, charting, OBS, virtual audio cables, and
any generative text component. If a requirement seems to need one of these,
it has been misread.
