"""Startup smoke test: everything imports, builds and tears down cleanly.

Covers the pieces the text modes never touch -- the tkinter overlay, the recorder and
the hotkey listener -- so `python -m jarvis` does not fail on the user's first run.
Briefly flashes the overlay on screen; that is expected.

Usage:  python tests/smoke.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

failures: list[str] = []


def step(label: str, fn) -> None:
    try:
        detail = fn()
        print(f"  ✓ {label}{f'  — {detail}' if detail else ''}")
    except Exception as e:
        print(f"  ✗ {label}: {type(e).__name__}: {e}")
        failures.append(label)


def main() -> None:
    print("импорт модулей:")

    def imports():
        import jarvis.actions, jarvis.app, jarvis.audio, jarvis.cache  # noqa: F401
        import jarvis.hud, jarvis.llm, jarvis.resolver, jarvis.semantic  # noqa: F401
        import jarvis.state, jarvis.stt, jarvis.timing, jarvis.tts  # noqa: F401
        from jarvis.actions import REGISTRY
        return f"{len(REGISTRY)} действий в реестре"

    step("все модули", imports)

    print("\nсостояние системы:")

    def state():
        from jarvis.state import snapshot
        st = snapshot()
        return f"{len(st.windows)} окон, громкость {st.master_volume}%"

    step("snapshot()", state)

    print("\nзвук:")

    def mic():
        from jarvis.audio import Recorder
        r = Recorder()
        r.start()
        audio = r.stop()
        return f"записано {len(audio)} сэмплов"

    step("микрофон открывается", mic)

    def tts():
        from jarvis.tts import Speaker
        s = Speaker()
        s.stop()
        return "голосовой поток запущен"

    step("SAPI", tts)

    print("\nинтерфейс:")

    def hud():
        from jarvis.hud import Hud
        h = Hud()
        h.update(status="проверка", utterance="jarvis", detail="ок")
        h.root.update()
        h.root.destroy()
        return "плашка создаётся и закрывается"

    step("tkinter HUD", hud)

    def hotkey():
        from pynput import keyboard
        from jarvis import config
        target = getattr(keyboard.Key, config.HOTKEY, None)
        if target is None:
            raise ValueError(f"нет такой клавиши: {config.HOTKEY}")
        listener = keyboard.Listener(on_press=lambda k: None)
        listener.start()
        listener.stop()
        return f"клавиша {config.HOTKEY}"

    step("глобальный перехват клавиши", hotkey)

    print("\nмедленный путь:")

    def backend():
        from jarvis import llm
        which = llm.backend()
        if which == "claude":
            return f"claude: {llm.find_claude() or 'НЕ НАЙДЕН'}"
        return f"mistral: {llm.MISTRAL_MODEL}"

    step("бэкенд LLM", backend)

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("всё на месте")


if __name__ == "__main__":
    main()
