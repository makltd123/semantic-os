"""H1 end to end: does the loop actually get faster as it learns?

  phase 1  ten fresh intents          -> all miss, all go to the LLM
  phase 2  the same phrasings again   -> should all be exact cache hits
  phase 3  paraphrases of the same    -> semantic tier, or LLM again

H1 predicts median latency collapses between phase 1 and phase 2, and that phase 3
lands in between. Phase 3 is the honest one: people rarely repeat themselves verbatim.

Intent resolution only -- actions are NOT executed, or this would lock the screen.

Usage:  python bench/bench_h1.py [--keep-cache]
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis import llm  # noqa: E402
from jarvis.actions import ANCHORS  # noqa: E402
from jarvis.cache import IntentCache  # noqa: E402
from jarvis.resolver import Resolver  # noqa: E402
from jarvis.semantic import SemanticIndex  # noqa: E402
from jarvis.state import snapshot  # noqa: E402
from jarvis.timing import Turn  # noqa: E402

TAUGHT = [
    "сделай потише",
    "открой браузер",
    "сколько времени",
    "сверни все окна",
    "включи режим фокуса",
    "не гаси экран",
    "переключись на браузер",
    "что сейчас играет",
]

PARAPHRASES = [
    "убавь звук",
    "запусти браузер",
    "который час",
    "покажи рабочий стол",
    "мне надо сосредоточиться",
    "не выключай монитор",
    "перейди в браузер",
    "что за музыка",
]


def run_phase(label: str, phrases: list[str], resolver: Resolver, st) -> tuple[float, int, int]:
    rows = []
    for phrase in phrases:
        turn = Turn(utterance=phrase)
        t0 = time.perf_counter()
        res = resolver.resolve(phrase, st, turn)
        ms = (time.perf_counter() - t0) * 1000
        rows.append((ms, res.route, res.action, phrase))
        time.sleep(1.3)  # rate limit headroom; excluded from the timings above

    print(f"\n=== {label} ===")
    for ms, route, action, phrase in rows:
        print(f"  {ms:7.0f}мс  [{route:<14}] {action or '-':<14} «{phrase}»")

    ms_all = [r[0] for r in rows]
    fast = sum(1 for r in rows if r[1].startswith("cache"))
    print(f"  медиана {statistics.median(ms_all):.0f}мс, "
          f"быстрым путём {fast}/{len(rows)}")
    return statistics.median(ms_all), fast, len(rows)


def main() -> None:
    keep = "--keep-cache" in sys.argv
    path = (os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "data", "intent_cache.json")
            if keep else os.path.join(tempfile.mkdtemp(), "cache.json"))

    print(f"бэкенд LLM: {llm.backend()} / {llm.MISTRAL_MODEL}")
    print(f"кэш: {path}")

    cache = IntentCache(path, semantic=SemanticIndex())
    cache.start_semantic(ANCHORS, block=True)
    resolver = Resolver(cache, use_llm=True)
    st = snapshot()

    m1, f1, n1 = run_phase("фаза 1 — новые фразы (кэш пуст)", TAUGHT, resolver, st)
    m2, f2, n2 = run_phase("фаза 2 — те же фразы дословно", TAUGHT, resolver, st)
    m3, f3, n3 = run_phase("фаза 3 — перефразировки", PARAPHRASES, resolver, st)

    print("\n=== итог ===")
    print(f"  фаза 1 (новое):          {m1:7.0f}мс   быстрым путём {f1}/{n1}")
    print(f"  фаза 2 (дословный повтор): {m2:7.0f}мс   быстрым путём {f2}/{n2}")
    print(f"  фаза 3 (перефразировки):  {m3:7.0f}мс   быстрым путём {f3}/{n3}")
    if m1 > 0:
        print(f"  ускорение дословного повтора: в {m1 / max(m2, 0.001):.0f} раз")
        print(f"  ускорение перефразировок:     в {m1 / max(m3, 0.001):.1f} раз")


if __name__ == "__main__":
    main()
