"""E4b: does the margin rule still protect a half-filled cache?

E4 tuned the margin on a cache containing all eight actions, so every phrase had a
competitor. A real cache fills up gradually: after one correction it may know only
volume_down. Then "прибавь звук" has nothing to lose to, wins by a huge margin, and
fires the opposite action.

Anchors (actions.ANCHORS) exist for exactly this: they compete but never win on their
own. This checks that the hole is real without them and closed with them.

Usage:  python tests/test_semantic.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.actions import ANCHORS  # noqa: E402
from jarvis.cache import IntentCache  # noqa: E402
from jarvis.semantic import SemanticIndex  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: получили {got!r}, ждали {want!r}")
    if not ok:
        failures.append(label)


def build(anchors: dict, taught: list[tuple[str, str]]) -> IntentCache:
    path = os.path.join(tempfile.mkdtemp(), "cache.json")
    sem = SemanticIndex()
    cache = IntentCache(path, semantic=sem)
    for phrase, action in taught:
        cache.learn(phrase, action, {})
    cache.start_semantic(anchors, block=True)
    return cache


def route_of(cache: IntentCache, phrase: str) -> tuple[str | None, str]:
    entry, route, note = cache.lookup(phrase)
    return (entry.action if entry else None), note


def main() -> None:
    print("Кэш знает только volume_down («сделай потише»).\n")

    print("БЕЗ якорей — ожидаем дыру:")
    bare = build({}, [("сделай потише", "volume_down")])
    got, note = route_of(bare, "прибавь звук")
    print(f"    примечание: {note or '-'}")
    check("«прибавь звук» без якорей уходит в volume_down", got, "volume_down")

    print("\nС якорями — дыра должна закрыться:")
    armed = build(ANCHORS, [("сделай потише", "volume_down")])

    for phrase in ("прибавь звук", "сделай громче", "погромче"):
        got, note = route_of(armed, phrase)
        print(f"    примечание: {note or '-'}")
        check(f"«{phrase}» не срабатывает как volume_down", got, None)

    print("\nПравильные срабатывания сохраняются:")
    got, note = route_of(armed, "потише")
    print(f"    примечание: {note or '-'}")
    check("«потише» -> volume_down", got, "volume_down")

    # The price of anchors: they add competitors, so some correct matches lose their
    # margin and go to the LLM instead. Slower, not wrong -- the intended trade.
    got, note = route_of(armed, "можно потише")
    print(f"    примечание: {note or '-'}")
    print(f"    (цена якорей: «можно потише» -> {got!r}; без якорей срабатывало)")

    # Found by bench_h1.py, not by bench_cache.py: "открой X" and "переключись на X"
    # share a topic but mean different actions, and the learned app_launch entry won.
    print("\nОткрыть против переключиться (та же тема, разные действия):")
    apps = build(ANCHORS, [("открой браузер", "app_launch")])
    for phrase in ("переключись на браузер", "перейди в браузер"):
        got, note = route_of(apps, phrase)
        print(f"    примечание: {note or '-'}")
        check(f"«{phrase}» не срабатывает как app_launch", got, None)

    print("\nЗабывание убирает запись из всех слоёв:")
    dropped = armed.forget("потише")
    check("forget нашёл и удалил запись", dropped, True)
    got, _ = route_of(armed, "потише")
    check("после забывания «потише» не срабатывает", got, None)
    got, _ = route_of(armed, "сделай потише")
    check("и исходная фраза тоже не срабатывает", got, None)

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("все проверки пройдены")


if __name__ == "__main__":
    main()
