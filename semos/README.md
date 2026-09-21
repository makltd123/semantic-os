# Semantic OS

**English** | [Русский](README.ru.md)

A Windows add-on that addresses files by the meaning of a query rather than by path.

* `SPEC.md` — the original vision (sections 1–2 still hold, 3–8 are superseded).
* `ARCHITECTURE.md` — architecture and stage-by-stage plan. **Read this one.**

Both documents are in Russian.

One external dependency — `pypdf` (`pip install -r requirements.txt`).

## Running it

```
python -m semos init          # create the database and the indexing root
python -m semos index         # scan the root
python -m semos find "query"
python -m semos eval          # run the frozen query set
python -m semos stats
python -m semos serve         # resident process: Ctrl+Alt+J hotkey + input box
python tests\smoke.py         # check the mechanics on synthetic data
```

The database and config live in `%LOCALAPPDATA%\SemanticOS\`, not in the project folder (the project sat in OneDrive, and SQLite in WAL mode under file sync means constant re-uploads).
Set the indexing root with `python -m semos init --root <path>`.

## What it does now

It finds a file from a natural-language description using four signal sources merged with Reciprocal Rank Fusion:

| generator | signal |
|---|---|
| `content` | bm25 over chunk text (FTS5 + Russian stemmer) |
| `name` | bm25 over the file name |
| `recency` | recency priority, only among files already found |
| `alias` | learned names (empty until M3) |

**Every** file under the root is indexed: name and metadata always, content where it can be extracted (`.txt`, `.md`, `.docx`, `.pdf` with a text layer, and plain-text formats). Secrets (`.env`, keys, `*.kdbx`) are excluded, and OneDrive cloud placeholders are indexed by name only, so as not to trigger downloads.

`python -m semos serve` starts a resident process: the global hotkey `Ctrl+Alt+J` opens an input box on top of all windows, Enter opens the first result (or the one picked by number), and Tab shows the file in Explorer without opening it. Opening a file goes through `semos/actions.py` — a (verb, object class) matrix that checks the path does not escape the indexing root (including via symlinks/junctions).

## Status

**M0.** Run on a real Downloads folder (358 files, 27 query pairs collected by interviewing the user). The set itself is not in the repository because it contains real names of personal files. The format and collection method are in `eval/queries.example.tsv`; to run `eval` on your own folder, copy it to `eval/queries.tsv` and add your own pairs.
Result: **recall@3 = 74%**. The 80% gate was formally missed, but the user accepted it as an honest baseline: the remaining misses are not ranking bugs but the limits of purely lexical search (cross-language file names, semantic gaps between content and the query's meaning, a couple of unsupported formats). The vector layer (M4) is postponed.

**M1 — in progress.** The resident process, hotkey, input box, event log and safe file opening are implemented and tested as far as possible without a live key press (hotkey registration, conflicts, window rendering, show/hide and event logging are all checked programmatically). **A real `Ctrl+Alt+J` press by a live user has not been tested** — that is the first thing to do by hand.

### Autostart (after checking it by hand)

Deliberately not automated, because it changes system behavior at login:

1. `Win+R` → `shell:startup`
2. A shortcut to `pythonw.exe -m semos serve` with the project folder as the working directory (`pythonw`, not `python`, so there is no console window).

## Next step

Run `python -m semos serve` and press `Ctrl+Alt+J`. If the window does not appear or crashes, that is the first thing to fix in M1.
