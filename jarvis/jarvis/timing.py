"""Per-stage timing.

The latency breakdown is the point of the experiment, not a debug aid: H1 stands or
falls on whether the fast path stays under the ~1.5s "conversation feels broken"
threshold while the LLM path blows past it. So every turn carries its own stage map
and gets appended to a JSONL trace we can analyse later.
"""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
from typing import Any, Iterator


@dataclass
class Turn:
    """One press-to-talk cycle, from audio in to action done."""

    started_at: float = field(default_factory=time.time)
    utterance: str = ""
    route: str = ""            # cache-exact | cache-fuzzy | llm | none
    action: str = ""
    args: dict[str, Any] = field(default_factory=dict)
    said: str = ""             # what we spoke back
    ok: bool = False
    error: str = ""
    stages: dict[str, float] = field(default_factory=dict)  # ms

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self.stages[name] = (time.perf_counter() - t0) * 1000.0

    @property
    def total_ms(self) -> float:
        return sum(self.stages.values())

    def summary(self) -> str:
        parts = " ".join(f"{k} {v:.0f}" for k, v in self.stages.items())
        return f"[{self.route or '-'}] {self.total_ms:.0f}ms = {parts}"


class Trace:
    """Append-only JSONL log of turns."""

    def __init__(self, path: str) -> None:
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def write(self, turn: Turn) -> None:
        row = asdict(turn)
        row["total_ms"] = turn.total_ms
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass  # never let logging break the loop
