"""Auto-stop on silence: does it fire when it should, and stay quiet when it shouldn't?

A false trigger cuts the user off mid-sentence, which is worse than waiting for the key.
Feeds synthetic blocks straight into the recorder callback -- no microphone needed.

Usage:  python tests/test_vad.py
"""

from __future__ import annotations

import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from jarvis.audio import Recorder, SAMPLE_RATE  # noqa: E402

BLOCK = 1600  # 100ms at 16 kHz
failures: list[str] = []


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'✓' if ok else '✗'} {label}: {got}, ждали {want}")
    if not ok:
        failures.append(label)


def feed(rec: Recorder, loud: bool, ms: int) -> None:
    blocks = max(1, ms // 100)
    for _ in range(blocks):
        amp = 0.25 if loud else 0.0005
        data = (np.random.randn(BLOCK, 1) * amp).astype(np.float32)
        rec._callback(data, BLOCK, None, None)


def fresh() -> Recorder:
    r = Recorder()
    r._chunks = []
    r._heard_speech = False
    r._silent_frames = 0
    r.speech_ended.clear()
    return r


def main() -> None:
    print(f"порог речи RMS {Recorder().speech_rms}, тишина {Recorder().silence_ms}мс\n")

    print("речь, затем длинная пауза — должно сработать:")
    r = fresh()
    feed(r, loud=True, ms=1500)
    feed(r, loud=False, ms=800)
    check("сработал авто-стоп", r.speech_ended.is_set(), True)

    print("\nречь с короткой паузой внутри — НЕ должно сработать:")
    r = fresh()
    feed(r, loud=True, ms=1000)
    feed(r, loud=False, ms=300)   # breath, not end of sentence
    check("не оборвал на вдохе", r.speech_ended.is_set(), False)
    feed(r, loud=True, ms=1000)   # speech resumes
    check("после продолжения речи по-прежнему молчит", r.speech_ended.is_set(), False)

    print("\nодна тишина без речи — НЕ должно сработать:")
    r = fresh()
    feed(r, loud=False, ms=3000)
    check("пустое нажатие не запускает разбор", r.speech_ended.is_set(), False)

    print("\nпауза ровно на границе (600мс) — должно сработать:")
    r = fresh()
    feed(r, loud=True, ms=800)
    feed(r, loud=False, ms=600)
    check("граница включительно", r.speech_ended.is_set(), True)

    print("\nнакопленное аудио сохраняется целиком:")
    r = fresh()
    feed(r, loud=True, ms=1000)
    feed(r, loud=False, ms=700)
    total = sum(len(c) for c in r._chunks) / SAMPLE_RATE
    check("длина записи ~1.7с", round(total, 1), 1.7)

    print()
    if failures:
        print(f"ПРОВАЛЕНО: {len(failures)} — {', '.join(failures)}")
        sys.exit(1)
    print("все проверки пройдены")


if __name__ == "__main__":
    main()
