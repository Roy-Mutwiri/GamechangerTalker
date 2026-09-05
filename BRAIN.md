# The brain

The narrator core has no language model and never will: it reads MetaTrader,
computes facts, and speaks sentences a person wrote. That part is deterministic
by design.

The **hosts layer** is different. `narrator/script/hosts.py` is a two-host
conversation written by a model, screened by `narrator/script/guard.py` on
every single turn before it can reach a microphone. This page is about that
model — which one, what it costs, and what the audience hears when it breaks.

---

## Choosing a backend

```toml
[hosts]
enabled = true
backend = "ollama"      # or openrouter, openai, groq, together, azure, local,
                        # anthropic, auto
model = "qwen2.5:14b-instruct-q4_K_M"
```

| backend | cost | latency | limits | notes |
|---|---|---|---|---|
| `ollama` | free | 3–5 s local | none | the default. Offline, unmetered, cannot run up a bill overnight |
| `openrouter` | metered, free tier | 1–3 s | per-day and per-minute | `publisher/model` ids. Closest replacement for GitHub Models |
| `openai` | metered | 1–3 s | per-minute | bare ids (`gpt-4o-mini`) |
| `groq` | metered, free tier | **< 1 s** | tight per-minute | fast enough that `timeout_seconds` can come right down |
| `together` | metered | 1–3 s | per-minute | `publisher/model` ids |
| `azure` | metered | 1–3 s | per-resource | Microsoft Foundry. Set `base_url` to your own endpoint |
| `local` | free | varies | none | LM Studio, vLLM, llama.cpp — anything serving `/chat/completions` |
| `anthropic` | metered | 1–3 s | per-minute | the original hosted path, unchanged |
| `github` | — | — | — | **retired 2026-07-30. See below.** |

`"auto"` resolves: ollama if installed → any hosted provider whose key is in
the environment → anthropic → ollama. Local first, so a stream left running
unattended cannot spend money while a free model sits right there.

### GitHub Models is gone

GitHub retired GitHub Models on **30 July 2026** — the playground, the model
catalog, the inference API and BYOK, for every customer including existing
ones. `backend = "github"` is still recognised so that an operator following an
older guide is told what happened instead of debugging DNS:

```
GitHub Models was retired on 2026-07-30 — the inference API, the catalog and
BYOK are all gone. Switch backend to "openrouter" (same publisher/model ids,
so nothing else changes), or "ollama" to run locally for free
```

OpenRouter is the recommended replacement because it kept the
`publisher/model` id form, so switching is changing one word in `config.toml`.

---

## The key

**Never in `config.toml`.** That file is the one most likely to be screenshotted
on stream, pasted into a chat, or committed by accident, and this project's
entire output is a public broadcast. The key comes from the provider's own
environment variable:

```powershell
$env:OPENROUTER_API_KEY = "sk-or-..."      # this session only
setx OPENROUTER_API_KEY "sk-or-..."        # permanently, new shells only
```

`token_env` under `[hosts]` overrides which variable is read, for a provider
the presets do not cover.

```powershell
python -m tools.brain_check --providers    # which keys are set on this machine
```

---

## Before every stream

```powershell
python -m tools.brain_check      # one real turn, through the real prompt
python -m tools.soak --minutes 30
```

`brain_check` resolves exactly what the narrator resolves, sends one turn using
the real system prompt against a synthetic market, and reports latency, tokens,
rate-limit headers, the budget that turn spent, and a `timeout_seconds`
recommendation measured rather than guessed. Exit code is non-zero on any
failure, with the API's own sentence: "credit balance is too low" is instantly
actionable and "BadRequestError" is not.

---

## The budget, and why pacing beats capping

A free tier gives you something like 150 requests a day. A stream speaks every
few seconds.

Capping at 150 is the obvious response and the wrong one: the hosts talk
beautifully for twenty minutes and are then silent for five and a half hours,
which is worse than never having had them.

So `narrator/llm/budget.py` **paces**. It divides the requests still available
by the stream time still to run, and generates one turn per resulting interval.

### Worked example: 150 a day, six hours

```
  requests_per_day     150
  reserve_fraction     0.15   ->  22 held back, never planned for
  spendable                       128
  stream_hours         6      ->  21600 seconds

  interval = 21600 / 128 = one turn about every 169 seconds
```

Roughly a host turn every three minutes. Sparse — but **spread**, and the
template library fills every gap between them exactly as it does when the brain
is off. The audience hears a conversation that lasts all night rather than one
that dies before New York opens.

The interval is recomputed every time, not fixed at startup. A stream that runs
long, or an hour where the library won every slot, re-spreads what is left over
what is left.

### The three rules that keep it honest

**The reserve is never planned.** A 429 storm, a restart or an unusually chatty
patch would otherwise strand the pair with nothing for the last hour.

**A 429 outranks the estimate.** Whatever the local arithmetic believes, the
service's answer wins, and `Retry-After` beats a guess. Without one, backoff
starts at 20 s and doubles to a 5-minute ceiling.

**Counters survive a restart.** They are written to `logs/llm_budget.json`,
keyed by UTC date and model, because the provider's day does not begin again
when the process does.

### Getting `stream_hours` wrong

It is the denominator. Too low and the hosts spend the day's quota early; too
high and they finish the night with quota unspent. It does not need to be
exact — the interval is recomputed continuously — but it should be roughly how
long you actually stream.

---

## What the audience hears when it breaks

Nothing. That is the whole design. Every failure below falls through to the
template library, which is always there.

| What happened | Log | Audience hears | Recovers |
|---|---|---|---|
| No key | one line at startup, naming the variable | the library, as always | on restart with a key |
| 401 / 403 | ERROR, once, with the API's own sentence | the library | no — the layer stops for the run |
| Unknown model id | ERROR, once, showing the `publisher/model` form | the library | no |
| **429 rate limited** | WARNING, **once per pause** | the library | **yes, by itself** |
| **Budget paced out** | nothing | the library | **yes, continuously** |
| Budget spent for the day | WARNING, once | the library | yes, at the UTC reset |
| Timeout | WARNING | one line lost | yes |
| 5xx | WARNING after one retry | one line lost | yes |
| Empty completion | DEBUG | one line lost | yes |
| Guard tripped | counted | one line lost | yes |
| 8 consecutive failures | ERROR, once | the library | no — a sustained outage |

The two rows in bold are the ones that matter on a free tier, and they are
deliberately **not** failures. Counting a paced-out turn against `FAILURE_LIMIT`
would disable the brain within a minute of the first pause, over something that
was working exactly as designed.

---

## Sampling

```toml
top_p = 0.92
presence_penalty = 0.6
frequency_penalty = 0.4
```

This API has no `repeat_penalty`. The echoing that the Ollama path suppresses
with `repeat_penalty` / `repeat_last_n` — observed live as both hosts opening
"Exactly," and restating each other three turns running — is suppressed by
these two instead. Presence discourages returning to a subject at all;
frequency discourages reusing the same word.

---

## Timeouts

`timeout_seconds` defaults to 25, tuned for a 7 tok/s local model. A hosted
model answers in one to four seconds, and a timeout six times longer than the
worst case just means a stalled turn takes half a minute to give up on.

`brain_check` prints a measured recommendation. Groq in particular can go a
long way below the default.

---

## Reading a stream back

```powershell
python -m tools.review            # what was said, and what grated
python -m tools.review --emotes   # how the face behaved
```

The second is the one for tuning the brain's expression: mood distribution,
beats per hundred turns, tags the model invented and had thrown away, and the
longest run of a single mood — which is the number that actually catches a
face stuck on "excited" for a quarter of an hour.
