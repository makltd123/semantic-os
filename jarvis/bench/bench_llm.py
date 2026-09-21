"""E2: how slow is the slow path, and how much of that is launch overhead?

A trivial `claude -p` took 6-9s, far above the 3-5s assumed. The suspicion is that most
of it is Claude Code booting (MCP servers, settings, a very large default system prompt)
rather than inference -- in which case it is partly recoverable.

Compares a plain launch against the lean one used by jarvis.llm, on the real intent
prompt, and checks the answers are still right.

Usage:  python bench/bench_llm.py [runs]
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis import llm  # noqa: E402
from jarvis.state import snapshot  # noqa: E402

CASES = [
    ("сколько времени", "say_state"),
    ("сделай потише", "volume_down"),
    ("мне надо сосредоточиться", "focus_mode"),
    # the paraphrases the fuzzy cache missed in E3
    ("убавь звук", "volume_down"),
    ("ничего не слышно", "volume_up"),
    ("мне нужен интернет", "app_launch"),
    ("который час", "say_state"),
    ("покажи рабочий стол", "minimize_all"),
    ("я отойду", "lock_screen"),
    ("не отвлекай меня", "focus_mode"),
    ("я читаю не засыпай", "keep_awake"),
    ("переключись на хром", "window_focus"),
]

# Held-out phrases, written AFTER the direction rule was added to the prompt and never
# looked at while editing it. Without these, "11/12" would just be the prompt fitted to
# its own test set.
HOLDOUT_CASES = [
    ("приглуши музыку", "volume_down"),
    ("звук слишком тихий", "volume_up"),
    ("сделай погромче немного", "volume_up"),
    ("убери звук совсем", "mute"),
    ("открой проводник", "app_launch"),
    ("покажи что в буфере", "say_state"),
    ("какая сейчас громкость", "say_state"),
    ("спрячь окна", "minimize_all"),
    ("закрой это окно", "window_close"),
    ("ухожу на обед", "lock_screen"),
    ("пусть не выключается экран", "keep_awake"),
    ("верни громкость на сорок", "volume_set"),
]


def main() -> None:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 1
    modes = sys.argv[2].split(",") if len(sys.argv) > 2 else ["plain", "lean"]
    print(f"claude.exe: {llm.find_claude()}")
    st = snapshot()

    for lean in [m == "lean" for m in modes]:
        label = "lean (jarvis)" if lean else "plain"
        print(f"\n=== {label} ===")
        times = []
        correct = 0
        for phrase, want in CASES:
            for _ in range(runs):
                prompt = llm.build_user_prompt(phrase, st)
                t0 = time.perf_counter()
                text, err = llm.call(prompt, lean=lean, timeout=60)
                dt = time.perf_counter() - t0
                times.append(dt)
                got = llm.parse(text).action if not err else None
                ok = got == want
                correct += ok
                mark = "✓" if ok else "✗"
                shown = err or text.replace("\n", " ")[:90]
                print(f"  {mark} {dt:5.1f}s  «{phrase}» -> {got}   {shown}")
        times.sort()
        print(f"  медиана {times[len(times)//2]:.1f}s, мин {times[0]:.1f}s, "
              f"макс {times[-1]:.1f}s, верно {correct}/{len(times)}")


if __name__ == "__main__":
    main()
