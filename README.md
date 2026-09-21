# Semantic OS

**English** | [Русский](README.ru.md)

Research prototypes of Windows add-ons where you drive the computer by meaning rather than by file paths and exact commands.

| Subproject | What it is |
|---|---|
| [`semos/`](semos/) | **File search by the meaning of a query.** You type "statistics assignment, April–May" instead of a path. SQLite FTS5 index with a Russian stemmer; four signals (content, file name, recency, learned names) are merged with Reciprocal Rank Fusion. A global hotkey `Ctrl+Alt+J` opens a search box on top of all windows. recall@3 = 74% on a real folder of 358 files. |
| [`jarvis/`](jarvis/) | **A voice assistant that takes system state into account.** "Turn it down" lowers whatever is playing right now, not the master volume. A phrase is resolved through a ladder: exact match → fuzzy → embeddings → LLM. The LLM's answer is remembered, so the same phrase is resolved instantly the second time. |

The subprojects are independent: each has its own README, dependencies and way to run. Both are Windows-only and built for Russian (queries, speech, stemming). Design notes and research logs inside the subprojects are in Russian.

## License

[MIT](LICENSE)
