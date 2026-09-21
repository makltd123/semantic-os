"""The intent cache -- the load-bearing part of H1.

E1 measured the gap the cache has to cover: the fast path is ~1.05s end to end, the LLM
path 4-6s. So this is not an optimisation, it is the difference between a usable loop
and one that feels broken.

What gets stored is the *action*, never its binding to state. "Потише" is remembered as
volume_down, and which app that quietens is decided at execution time against fresh
state. Caching the binding would make the system confidently do the wrong thing later --
the worst failure mode for a voice interface, because it is silent.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

from rapidfuzz import fuzz, process as fuzz_process

# Politeness and filler that carry no intent. Stripping them makes "сделай потише",
# "слушай, сделай потише" and "потише пожалуйста" collapse to one cache key.
_FILLER = {
    "пожалуйста", "слушай", "давай", "ну", "это", "там", "как бы", "короче",
    "джарвис", "привет", "эй", "окей", "плиз", "будь добр", "можешь",
}

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = _PUNCT.sub(" ", t)
    words = [w for w in _SPACE.split(t) if w and w not in _FILLER]
    return " ".join(words).strip()


@dataclass
class Entry:
    utterance: str          # normalized form -- the key
    raw: str                # what was actually said the first time
    action: str
    args: dict[str, Any]
    hits: int = 0
    created: float = 0.0
    last_used: float = 0.0


class IntentCache:
    """Exact, then fuzzy, then semantic. All local.

    Exact and fuzzy are sub-millisecond; the semantic tier (semantic.py) costs ~20ms
    and is optional -- without sentence-transformers it simply stays off.
    """

    def __init__(self, path: str, fuzzy_threshold: int = 88, semantic=None) -> None:
        self.path = path
        self.fuzzy_threshold = fuzzy_threshold
        self.entries: dict[str, Entry] = {}
        self.semantic = semantic
        self._load()

    def start_semantic(self, anchors: dict[str, list[str]], block: bool = False) -> None:
        if self.semantic is None:
            return
        rows = [(k, e.action) for k, e in self.entries.items()]
        if block:
            self.semantic.load(rows, anchors)
        else:
            self.semantic.load_async(rows, anchors)

    def _load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                for key, row in json.load(f).items():
                    self.entries[key] = Entry(**row)
        except (OSError, ValueError, TypeError):
            pass  # a corrupt cache should cost speed, not the session

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({k: asdict(v) for k, v in self.entries.items()},
                          f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def lookup(self, utterance: str) -> tuple[Entry | None, str, str]:
        """Returns (entry, route, note).

        Route is cache-exact | cache-fuzzy | cache-semantic | miss. The note explains
        the decision -- including *why* a semantic candidate was refused -- so the HUD
        can show which link of the chain gave up.
        """
        key = normalize(utterance)
        if not key:
            return None, "miss", ""

        hit = self.entries.get(key)
        if hit is not None:
            self._touch(hit)
            return hit, "cache-exact", ""

        if self.entries:
            best = fuzz_process.extractOne(
                key, list(self.entries.keys()), scorer=fuzz.token_set_ratio)
            if best and best[1] >= self.fuzzy_threshold:
                hit = self.entries[best[0]]
                self._touch(hit)
                return hit, "cache-fuzzy", f"похоже на «{hit.raw}» ({best[1]:.0f}%)"

        if self.semantic is not None:
            got = self.semantic.query(key)
            if got.reason == "hit" and got.key in self.entries:
                hit = self.entries[got.key]
                self._touch(hit)
                return hit, "cache-semantic", (
                    f"по смыслу ≈ «{hit.raw}» ({got.score:.2f}, отрыв {got.margin:.2f})")
            if got.reason == "ambiguous":
                return None, "miss", (
                    f"смысл неоднозначен: {got.action} {got.score:.2f}, "
                    f"отрыв {got.margin:.2f} — отдаю модели")
            if got.reason == "below-threshold":
                return None, "miss", f"ближайшее {got.action} {got.score:.2f} — слишком далеко"

        return None, "miss", ""

    def _touch(self, e: Entry) -> None:
        e.hits += 1
        e.last_used = time.time()

    def learn(self, utterance: str, action: str, args: dict[str, Any]) -> None:
        """Promote an LLM resolution into the fast path."""
        key = normalize(utterance)
        if not key:
            return
        now = time.time()
        self.entries[key] = Entry(
            utterance=key, raw=utterance, action=action, args=args or {},
            hits=0, created=now, last_used=now,
        )
        if self.semantic is not None:
            self.semantic.add(key, action)
        self.save()

    def forget(self, utterance: str) -> bool:
        """Drop a wrong association so a correction actually sticks."""
        key = normalize(utterance)
        if key in self.entries:
            self._drop(key)
            return True

        # The phrase may have been served by a fuzzy or semantic neighbour; the thing
        # to forget is whatever actually answered, not the words just spoken.
        if self.entries:
            best = fuzz_process.extractOne(
                key, list(self.entries.keys()), scorer=fuzz.token_set_ratio)
            if best and best[1] >= self.fuzzy_threshold:
                self._drop(best[0])
                return True
        if self.semantic is not None:
            got = self.semantic.query(key)
            if got.reason == "hit" and got.key in self.entries:
                self._drop(got.key)
                return True
        return False

    def _drop(self, key: str) -> None:
        self.entries.pop(key, None)
        if self.semantic is not None:
            self.semantic.remove(key)
        self.save()

    def stats(self) -> dict[str, int]:
        return {
            "entries": len(self.entries),
            "total_hits": sum(e.hits for e in self.entries.values()),
        }
