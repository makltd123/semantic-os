"""E3: does the fast path generalise across paraphrases, or only across typos?

H1 bets that most utterances repeat. But people rarely repeat *verbatim*: "сделай
потише", "убавь звук", "можно потише" are one intent. The fuzzy cache compares
characters, so the suspicion is that it catches reorderings and filler but not
synonyms -- which would mean the LLM path fires far more often than H1 hopes.

Method: teach the cache the first phrase of each group, then look up the rest.
No LLM, no microphone -- deterministic and instant.

Usage:  python bench/bench_cache.py [--matcher fuzzy|embed|both]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# (action, [first phrase is taught, the rest are probes])
GROUPS: list[tuple[str, list[str]]] = [
    ("volume_down", [
        "сделай потише", "потише пожалуйста", "сделай звук потише",  # near-verbatim
        "убавь звук", "тише", "можно потише", "слишком громко",       # paraphrase
    ]),
    ("volume_up", [
        "сделай погромче", "погромче пожалуйста",
        "прибавь звук", "громче", "ничего не слышно",
    ]),
    ("app_launch", [
        "открой браузер", "браузер открой", "открой пожалуйста браузер",
        "запусти браузер", "открой хром", "мне нужен интернет",
    ]),
    ("say_state", [
        "сколько времени", "сколько сейчас времени",
        "который час", "время", "сколько на часах",
    ]),
    ("minimize_all", [
        "сверни все окна", "сверни все окна пожалуйста",
        "сверни всё", "покажи рабочий стол", "убери все окна",
    ]),
    ("lock_screen", [
        "заблокируй компьютер", "заблокируй компьютер пожалуйста",
        "заблокируй комп", "я отойду", "блокировка",
    ]),
    ("focus_mode", [
        "включи режим фокуса", "режим фокуса включи",
        "мне надо сосредоточиться", "не отвлекай меня", "убери мессенджеры",
    ]),
    ("keep_awake", [
        "не гаси экран", "не гаси пожалуйста экран",
        "сделай чтобы экран не гас", "не выключай монитор", "я читаю не засыпай",
    ]),
]

# Held-out probes: never looked at while picking thresholds. The (threshold, margin)
# pair is chosen on GROUPS and then judged here, so the headline number isn't a
# threshold fitted to its own test set.
HOLDOUT: list[tuple[str, str]] = [
    ("volume_down", "убери громкость"), ("volume_down", "потише сделай"),
    ("volume_down", "приглуши"),
    ("volume_up", "сделай громче"), ("volume_up", "добавь громкости"),
    ("volume_up", "погромче сделай"),
    ("app_launch", "запусти хром"), ("app_launch", "хочу в интернет"),
    ("say_state", "подскажи время"), ("say_state", "сколько сейчас"),
    ("minimize_all", "спрячь все окна"), ("minimize_all", "сверни окна"),
    ("lock_screen", "заблокируй экран"), ("lock_screen", "поставь компьютер на замок"),
    ("focus_mode", "включи фокус"), ("focus_mode", "хочу поработать без отвлечений"),
    ("keep_awake", "пусть экран не гаснет"), ("keep_awake", "не отключай экран"),
]

# Probes are split by kind so the report shows *where* generalisation breaks.
NEAR = {
    "потише пожалуйста", "сделай звук потише", "погромче пожалуйста",
    "браузер открой", "открой пожалуйста браузер", "сколько сейчас времени",
    "сверни все окна пожалуйста", "заблокируй компьютер пожалуйста",
    "режим фокуса включи", "не гаси пожалуйста экран",
}


def run_fuzzy(threshold: int) -> list[tuple[str, str, str, str | None, float]]:
    from jarvis.cache import IntentCache

    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    cache = IntentCache(path, fuzzy_threshold=threshold)
    for action, phrases in GROUPS:
        cache.learn(phrases[0], action, {})

    rows = []
    for action, phrases in GROUPS:
        for probe in phrases[1:]:
            t0 = time.perf_counter()
            entry, route, _ = cache.lookup(probe)
            ms = (time.perf_counter() - t0) * 1000
            got = entry.action if entry else None
            kind = "near" if probe in NEAR else "para"
            rows.append((kind, probe, action, got, ms))
    return rows


EMBEDDERS = {
    "minilm": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
    "rubert": "cointegrated/rubert-tiny2",
}


def run_embed(threshold: float, which: str) -> list[tuple[str, str, str, str | None, float]] | None:
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        print("  sentence-transformers не установлен — пропуск")
        return None
    import numpy as np

    name = EMBEDDERS[which]
    t0 = time.perf_counter()
    model = SentenceTransformer(name, device="cpu")
    print(f"\n  [{which}] загрузка эмбеддера: {time.perf_counter() - t0:.1f}s")

    from jarvis.cache import normalize
    keys = [normalize(p[0]) for _, p in GROUPS]
    acts = [a for a, _ in GROUPS]
    mat = model.encode(keys, normalize_embeddings=True)

    model.encode(["прогрев"], normalize_embeddings=True)

    rows = []
    for action, phrases in GROUPS:
        for probe in phrases[1:]:
            t0 = time.perf_counter()
            v = model.encode([normalize(probe)], normalize_embeddings=True)[0]
            sims = mat @ v
            order = np.argsort(-sims)
            i, j = int(order[0]), int(order[1])
            ms = (time.perf_counter() - t0) * 1000
            got = acts[i] if sims[i] >= threshold else None
            kind = "near" if probe in NEAR else "para"
            rows.append((kind, probe, action, got, ms,
                         f"top1 {acts[i]} {sims[i]:.2f} | top2 {acts[j]} {sims[j]:.2f}"))

    def score(pairs):
        out = []
        for want, probe in pairs:
            v = model.encode([normalize(probe)], normalize_embeddings=True)[0]
            sims = mat @ v
            o = np.argsort(-sims)
            out.append((want, acts[o[0]], float(sims[o[0]]), float(sims[o[1]])))
        return out

    tune = score([(a, p) for a, ps in GROUPS for p in ps[1:]])
    hold = score(HOLDOUT)

    def grade(scored, thr, margin):
        right = wrong = miss = 0
        for want, top, s1, s2 in scored:
            if s1 < thr or (s1 - s2) < margin:
                miss += 1
            elif top == want:
                right += 1
            else:
                wrong += 1
        return right, wrong, miss

    # Absolute similarity alone vs. similarity plus a margin over the runner-up.
    # A small margin means two *different* actions look alike -- exactly the
    # antonym case (потише/погромче) that similarity cannot separate.
    print(f"  сетка на обучающих пробах (n={len(tune)}) | отложенных (n={len(hold)})")
    print("  порог  отрыв   верно ошибка промах  |  верно ошибка промах")
    for thr in (0.5, 0.6, 0.7):
        for margin in (0.0, 0.1, 0.15, 0.2, 0.25):
            r, w, m = grade(tune, thr, margin)
            hr, hw, hm = grade(hold, thr, margin)
            flag = "  <- 0 ошибок" if w == 0 else ""
            print(f"  {thr:.2f}   {margin:.2f}   {r:>5} {w:>6} {m:>6}  |  "
                  f"{hr:>5} {hw:>6} {hm:>6}{flag}")

    print("  отложенные пробы подробно (порог 0.5, отрыв 0.2):")
    for (want, probe), (_, top, s1, s2) in zip(HOLDOUT, hold):
        ok = s1 >= 0.5 and (s1 - s2) >= 0.2
        mark = ("✓" if top == want else "✗ НЕВЕРНО") if ok else "· в LLM"
        print(f"    {mark:<10} «{probe}» -> {top} {s1:.2f} (отрыв {s1 - s2:.2f}), нужно {want}")
    return rows


def run_pipeline(use_anchors: bool) -> list[tuple[str, str, str, str | None, float]]:
    """End-to-end through the real cache: exact -> fuzzy -> semantic(+anchors).

    Anchors close the half-filled-cache hole (tests/test_semantic.py) but add
    competitors, which costs some correct matches their margin. This measures that
    price instead of guessing at it.
    """
    from jarvis.actions import ANCHORS
    from jarvis.cache import IntentCache
    from jarvis.semantic import SemanticIndex

    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    cache = IntentCache(path, semantic=SemanticIndex())
    for action, phrases in GROUPS:
        cache.learn(phrases[0], action, {})
    cache.start_semantic(ANCHORS if use_anchors else {}, block=True)

    probes = [(a, p, "near" if p in NEAR else "para")
              for a, ps in GROUPS for p in ps[1:]]
    probes += [(a, p, "hold") for a, p in HOLDOUT]

    rows = []
    for want, probe, kind in probes:
        t0 = time.perf_counter()
        entry, route, note = cache.lookup(probe)
        ms = (time.perf_counter() - t0) * 1000
        rows.append((kind, probe, want, entry.action if entry else None, ms, route))
    return rows


def report(title: str, rows) -> None:
    print(f"\n=== {title} ===")
    labels = {"near": "почти дословно", "para": "перефразировка", "hold": "отложенные"}
    for kind, label in labels.items():
        sub = [r for r in rows if r[0] == kind]
        if not sub:
            continue
        hit = sum(1 for r in sub if r[3] == r[2])
        wrong = sum(1 for r in sub if r[3] is not None and r[3] != r[2])
        miss = sum(1 for r in sub if r[3] is None)
        print(f"  {label:<16} верно {hit}/{len(sub)}   ошибка {wrong}   промах {miss}")
    for kind, probe, want, got, ms, *extra in rows:
        mark = "✓" if got == want else ("✗ НЕВЕРНО" if got else "· промах")
        tail = f"   {extra[0]}" if extra else ""
        print(f"    {mark:<10} [{kind}] «{probe}» -> {got}  ({ms:.1f}ms){tail}")
    ms_all = sorted(r[4] for r in rows)
    print(f"  задержка поиска: медиана {ms_all[len(ms_all)//2]:.1f}ms, макс {ms_all[-1]:.1f}ms")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--matcher", default="fuzzy",
                    choices=["fuzzy", "embed", "both", "pipeline"])
    ap.add_argument("--embedder", default="all", choices=["all", *EMBEDDERS])
    ap.add_argument("--fuzzy-threshold", type=int, default=88)
    ap.add_argument("--embed-threshold", type=float, default=0.6)
    a = ap.parse_args()

    if a.matcher == "pipeline":
        for use_anchors in (False, True):
            label = "конвейер с якорями" if use_anchors else "конвейер без якорей"
            report(label, run_pipeline(use_anchors))
        return

    if a.matcher in ("fuzzy", "both"):
        report(f"rapidfuzz token_set_ratio >= {a.fuzzy_threshold}", run_fuzzy(a.fuzzy_threshold))
    if a.matcher in ("embed", "both"):
        which = list(EMBEDDERS) if a.embedder == "all" else [a.embedder]
        for w in which:
            rows = run_embed(a.embed_threshold, w)
            if rows:
                report(f"{w}, cos >= {a.embed_threshold}", rows)


if __name__ == "__main__":
    main()
