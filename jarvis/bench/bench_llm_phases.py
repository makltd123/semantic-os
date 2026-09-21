"""E2b: where do the ~8s of a `claude -p` call go?

Streams events and timestamps each one, splitting the call into
    spawn -> init event (CLI boot + auth) -> first model output -> done.

If boot dominates, a long-lived CLI process would recover most of it. If inference or
the network round-trip dominates, no launch trick will help.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis import llm  # noqa: E402
from jarvis.state import snapshot  # noqa: E402


def one(prompt: str) -> None:
    exe = llm.find_claude()
    cmd = [exe, "-p", "--model", llm.MODEL,
           "--output-format", "stream-json", "--verbose",
           "--system-prompt", llm.SYSTEM, "--tools", "",
           "--strict-mcp-config", "--no-session-persistence"]

    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=tempfile.gettempdir(),
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    proc.stdin.write(prompt.encode("utf-8"))
    proc.stdin.close()

    marks: list[tuple[float, str]] = []
    result_meta = {}
    for raw in proc.stdout:
        t = time.perf_counter() - t0
        try:
            ev = json.loads(raw)
        except ValueError:
            continue
        kind = ev.get("type", "?")
        sub = ev.get("subtype", "")
        label = f"{kind}/{sub}" if sub else kind
        marks.append((t, label))
        if kind == "result":
            result_meta = {k: ev.get(k) for k in
                           ("duration_ms", "duration_api_ms", "num_turns")}
    proc.wait()
    total = time.perf_counter() - t0

    for t, label in marks:
        print(f"    {t:6.2f}s  {label}")
    print(f"    total {total:.2f}s   cli-reported: {result_meta}")


def main() -> None:
    st = snapshot()
    for phrase in ("который час", "закрой браузер"):
        print(f"\n«{phrase}»")
        one(llm.build_user_prompt(phrase, st))


if __name__ == "__main__":
    main()
