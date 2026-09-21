"""CLI entry point.

    python -m jarvis                    # voice daemon: hold Right Ctrl and speak
    python -m jarvis --text "потише"    # one phrase, no microphone
    python -m jarvis --repl             # type phrases, watch the routing
    python -m jarvis --state            # dump what the machine looks like right now
    python -m jarvis --stats            # cache + latency stats from the trace

The text modes exist so the whole chain after speech-to-text can be exercised and
measured without a microphone -- which is also how the fast/slow split gets evaluated.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

from . import config


def cmd_state() -> None:
    from .state import snapshot
    print(snapshot().describe())


def cmd_stats() -> None:
    from .cache import IntentCache
    cache = IntentCache(config.CACHE_PATH)
    s = cache.stats()
    print(f"кэш намерений: {s['entries']} записей, {s['total_hits']} попаданий")
    for e in sorted(cache.entries.values(), key=lambda x: -x.hits)[:15]:
        print(f"  {e.hits:3d}×  «{e.raw}»  ->  {e.action} {e.args or ''}")

    if not os.path.exists(config.TRACE_PATH):
        print("\nтрейса пока нет")
        return

    rows = []
    with open(config.TRACE_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    if not rows:
        return

    print(f"\nходов записано: {len(rows)}")
    by_route: dict[str, list[float]] = {}
    for r in rows:
        by_route.setdefault(r.get("route", "?"), []).append(r.get("total_ms", 0.0))

    print(f"{'маршрут':<16}{'n':>5}{'медиана':>12}{'макс':>10}")
    for route, ms in sorted(by_route.items(), key=lambda kv: -len(kv[1])):
        print(f"{route:<16}{len(ms):>5}{statistics.median(ms):>11.0f}м{max(ms):>9.0f}м")

    ok = sum(1 for r in rows if r.get("ok"))
    print(f"успешных: {ok}/{len(rows)}")


def cmd_text(phrases: list[str], use_llm: bool, speak: bool, semantic: bool) -> None:
    from .app import Jarvis
    j = Jarvis(use_llm=use_llm, with_audio=speak, with_hud=False,
               use_semantic=semantic)
    if semantic:
        j.start_semantic(block=True)  # so the tier is actually exercised
    for phrase in phrases:
        turn = j.handle_text(phrase, speak=speak)
        status = "ok " if turn.ok else "FAIL"
        print(f"\n[{status}] «{phrase}»")
        print(f"  маршрут: {turn.route}")
        print(f"  действие: {turn.action or '-'} {turn.args or ''}")
        print(f"  сказано: {turn.said}")
        if turn.error:
            print(f"  ошибка: {turn.error}")
        print(f"  {turn.summary()}")
    j.shutdown()


def cmd_repl(use_llm: bool, speak: bool, semantic: bool) -> None:
    from .app import Jarvis
    j = Jarvis(use_llm=use_llm, with_audio=speak, with_hud=False,
               use_semantic=semantic)
    if semantic:
        print("загружаю семантический слой…")
        j.start_semantic(block=True)
    print("Печатай фразы. Пустая строка — выход.\n")
    try:
        while True:
            try:
                line = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not line:
                break
            turn = j.handle_text(line, speak=speak)
            mark = "✓" if turn.ok else "✗"
            print(f"  {mark} [{turn.route}] {turn.action or '-'} → {turn.said}")
            print(f"    {turn.summary()}\n")
    finally:
        j.shutdown()


def main() -> None:
    p = argparse.ArgumentParser(prog="jarvis")
    p.add_argument("--text", nargs="+", help="разобрать фразы без микрофона")
    p.add_argument("--repl", action="store_true", help="интерактивный текстовый режим")
    p.add_argument("--state", action="store_true", help="показать состояние системы")
    p.add_argument("--stats", action="store_true", help="статистика кэша и задержек")
    p.add_argument("--no-llm", action="store_true", help="только быстрый путь")
    p.add_argument("--no-semantic", action="store_true",
                   help="без семантического слоя кэша (только точное и нечёткое)")
    p.add_argument("--silent", action="store_true", help="без голосового ответа")
    p.add_argument("--model", default=config.STT_MODEL, help="размер модели whisper")
    args = p.parse_args()

    semantic = not args.no_semantic

    if args.state:
        return cmd_state()
    if args.stats:
        return cmd_stats()
    if args.text:
        return cmd_text(args.text, use_llm=not args.no_llm, speak=not args.silent,
                        semantic=semantic)
    if args.repl:
        return cmd_repl(use_llm=not args.no_llm, speak=not args.silent,
                        semantic=semantic)

    from .app import Jarvis
    j = Jarvis(stt_model=args.model, use_llm=not args.no_llm, use_semantic=semantic)
    print(f"jarvis: удерживай {config.HOTKEY} и говори. Esc на плашке — выход.")
    try:
        j.run()
    except KeyboardInterrupt:
        pass
    finally:
        j.shutdown()


if __name__ == "__main__":
    sys.exit(main())
