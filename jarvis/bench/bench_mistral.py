"""E5: Mistral over HTTPS as the slow path -- faster than the CLI, and still right?

Same 12 phrases as bench_llm.py so the numbers line up with E2. The first call of
each model pays the TLS handshake; it is reported separately from the warm median.

Usage:  python bench/bench_mistral.py [model,model,...]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bench_llm import CASES, HOLDOUT_CASES  # noqa: E402
from jarvis import llm  # noqa: E402
from jarvis.state import snapshot  # noqa: E402

DEFAULT_MODELS = ["ministral-3b-latest", "ministral-8b-latest", "mistral-small-latest"]
PAUSE_SEC = 1.3  # the key's tier allows ~1 req/s; the pause is not part of the timing


def main() -> None:
    global PAUSE_SEC
    models = sys.argv[1].split(",") if len(sys.argv) > 1 else DEFAULT_MODELS
    if len(sys.argv) > 2:
        PAUSE_SEC = float(sys.argv[2])
    cases = HOLDOUT_CASES if len(sys.argv) > 3 and sys.argv[3] == "hold" else CASES
    st = snapshot()

    for model in models:
        print(f"\n=== {model} ({len(cases)} фраз) ===")
        times = []
        correct = 0
        for n, (phrase, want) in enumerate(cases):
            prompt = llm.build_user_prompt(phrase, st)
            t0 = time.perf_counter()
            text, err = llm.call_mistral(prompt, model=model)
            dt = time.perf_counter() - t0
            got = llm.parse(text).action if not err else None
            ok = got == want
            correct += ok
            if n > 0:
                times.append(dt)
            mark = "✓" if ok else "✗"
            first = "  (первый, с TLS)" if n == 0 else ""
            shown = err or text.replace("\n", " ")[:80]
            print(f"  {mark} {dt:5.2f}s  «{phrase}» -> {got}   {shown}{first}")
            time.sleep(PAUSE_SEC)
        times.sort()
        if times:
            print(f"  тёплая медиана {times[len(times)//2]:.2f}s, мин {times[0]:.2f}s, "
                  f"макс {times[-1]:.2f}s, верно {correct}/{len(cases)}")


if __name__ == "__main__":
    main()
