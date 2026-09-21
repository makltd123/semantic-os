# jarvis

**English** | [Русский](README.ru.md)

A voice add-on for Windows: hold a key, say an ordinary phrase, and the computer does what you meant.

It understands Russian speech; the example phrases below are in Russian with translations. The research log and architecture notes are in Russian.

A research prototype, not a product. The idea, the hypothesis and all measurements are in [RESEARCH_LOG.md](RESEARCH_LOG.md). A separate branch of work: the semantic file-system indexer ([`semos/`](../semos/)) is neither used nor touched.

## The idea in one line

A voice assistant maps **phrase → function**, which is why it needs exact commands.
This one maps **phrase × system state → action**.

"Turn it down" is unresolvable without knowing what is playing right now. So knowledge of what is happening on the machine is the actual content of the system, not language understanding on its own.

## Installation

```powershell
pip install -r requirements.txt
```

You need the Claude Code CLI (`claude`) installed and signed in — new phrasings are parsed through it. No API key is required.

The Whisper `base` model (~145 MB) is downloaded on first run.

## Running it

```powershell
# voice mode: hold right Ctrl and speak
python -m jarvis

# the same phrases as text — no microphone
python -m jarvis --text "сделай потише" "открой браузер"

# interactive text mode
python -m jarvis --repl

# what the system knows about the machine right now
python -m jarvis --state

# stats: what has been learned and the latencies
python -m jarvis --stats
```

Useful flags: `--no-llm` (fast path only), `--silent` (no voice), `--model tiny|base|small`.

## How it works

```
voice ──> STT ──> state snapshot ──> intent resolution ──> action ──> voice + HUD
          ~1.0s       ~45ms         fast ~20ms / LLM 0.7-4s   ~10ms
```

Intent resolution is a ladder of three local steps with a model at the end:

1. **Exact match** on the normalized phrase — a fraction of a millisecond.
2. **Fuzzy** (rapidfuzz) — catches reordering and filler words: «потише пожалуйста» ("quieter please").
3. **Semantic** (MiniLM embeddings) — catches different words: «который час» ≈ «сколько времени» (both "what time is it"). About 20 ms.
4. **LLM** — only when all three miss. The result is **written into the fast path**, so the same phrasing is resolved instantly the second time.

### Why the semantic layer is more complex than "take the nearest by meaning"

Embeddings encode *topic*, not *direction*: to them "quieter" and "louder" are almost the same thing. A naive nearest-neighbor search sent «приглуши» ("turn it down a bit") to `volume_up` with 0.80 similarity — that is, it confidently did the opposite.

So a match requires not only similarity (≥ 0.5) but also a **margin over the runner-up (≥ 0.3)**. A small margin means "two different commands look the same", and the phrase goes to the model. This removed all errors, at the cost of sending some phrases down the long path.

The same problem exists along a second axis: in a short phrase the noun outweighs the verb, so «переключись на браузер» ("switch to the browser") gets pulled towards the learned «открой браузер» ("open the browser"). The margin catches this too, but the real cure is separating the action from the object — which is not here yet.

Plus **anchors**: every action has canonical phrasings that take part in comparisons but never fire on their own. Without them a half-empty cache is dangerous: if only "quieter" has been learned, "louder" has nothing to compare against and drifts to `volume_down`.

### The cache stores the action, not the binding

«Потише» ("quieter") is remembered as `volume_down`; which application to turn down is decided at execution time from the fresh state. Otherwise the system would, over time, start confidently doing the wrong thing — the worst failure for voice, because it is silent.

### Which model answers new phrases

Switched with the `JARVIS_LLM` variable:

- `claude` — through the installed Claude Code CLI, no key needed. Slower, more accurate.
- `mistral` — over HTTPS, needs `MISTRAL_API_KEY`. Several times faster.

The Mistral model is set with `JARVIS_MISTRAL_MODEL`. The key is best kept in a user environment variable (`setx MISTRAL_API_KEY "..."`) so it ends up neither in git nor in a cloud-synced folder.

## What it can do

| Action | Example phrase |
|---|---|
| `volume_down` / `volume_up` | «сделай потише» ("turn it down") — lowers whatever is playing, not the master volume |
| `volume_set`, `mute` | «громкость на тридцать» ("volume to thirty"), «выключи звук» ("mute") |
| `window_focus` | «переключись на браузер» ("switch to the browser") |
| `window_close`, `minimize_all` | «закрой телеграм» ("close Telegram"), «сверни всё» ("minimize everything") |
| `app_launch` | «открой музыку» ("open music") — searches Start menu shortcuts |
| `open_recent` | «открой то, что я вчера редактировал» ("open what I was editing yesterday") |
| `focus_mode` | «мне надо сосредоточиться» ("I need to focus") — minimizes and mutes messengers |
| `keep_awake` | «сделай, чтобы экран не гас» ("keep the screen on") |
| `lock_screen` | «заблокируй компьютер» ("lock the computer") |
| `say_state` | «сколько времени» ("what time is it"), «что играет» ("what's playing"), «что в буфере» ("what's in the clipboard") |

## The on-screen HUD

It shows the whole chain: what was heard → how it was resolved → what was done, with milliseconds per stage. Bar color: green — cache, yellow — LLM, red — miss.

This is not decoration. The second most common reason people abandon voice interfaces is not understanding what the system understood and at which link it broke.

## Correcting mistakes by voice

If a phrase was resolved wrongly, say **«не то»** ("not that") or **«забудь»** ("forget it") — the association is removed from the cache, and next time the phrase goes through the LLM again. A system that learns must be correctable out loud, otherwise its mistakes become permanent and invisible.

## Data

`data/intent_cache.json` — learned intents, readable JSON, can be edited by hand.
`data/turns.jsonl` — a log of turns with per-stage timings; the basis for `--stats`.

Both are in `.gitignore`: a trace can contain a piece of the clipboard.

## Reproducing the measurements

Every number in [RESEARCH_LOG.md](RESEARCH_LOG.md) was produced by these scripts:

```powershell
python bench/bench_stt.py tiny base        # E1  — speech recognition latency
python bench/bench_llm.py 1                # E2  — regular vs lightweight CLI launch
python bench/bench_llm_phases.py           # E2b — where the time goes inside a call
python bench/bench_cache.py --matcher pipeline   # E3/E4 — fast-path coverage
python bench/bench_mistral.py              # E5  — Mistral models
python bench/bench_mistral.py ministral-3b-latest 1.3 hold   # E6 — held-out set
python bench/bench_h1.py                   # H1  — does the system get faster as it learns
python tests/test_semantic.py              # E4b — the half-empty cache hole and anchors
```

`bench_h1.py` and `test_semantic.py` only resolve intents and **do not execute actions** — otherwise the benchmark would lock the screen on the phrase «я отойду» ("I'll step away").
