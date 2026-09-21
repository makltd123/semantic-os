"""E9: can a "Jarvis" wake word run on this machine, and does it survive Russian speech?

Presence -- being addressed by name instead of pressing a key -- is the first thing that
separates a Jarvis from a voice remote. It was deferred in iteration 1 on two worries:
false triggers, and a permanent load on two cores. Both are measurable.

Uses openWakeWord's pre-trained `hey_jarvis` model (ONNX, no GPU). Test speech is
synthetic (offline SAPI voices), so this measures compute cost and gross behaviour; a
real microphone and a real accent are a live test.

Usage:  python bench/bench_wakeword.py
"""

from __future__ import annotations

import contextlib
import os
import statistics
import sys
import time
import wave

import numpy as np
import win32com.client

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO = os.path.join(HERE, "audio", "wake")
THRESHOLD = 0.5  # openWakeWord's suggested default
SR = 16_000

# (voice hint, text, should it trigger?)  None = unknown, reported for information.
CLIPS: list[tuple[str, str, bool | None]] = [
    ("David", "Hey Jarvis", True),
    ("David", "Hey Jarvis, turn the volume down", True),
    ("Zira", "Hey Jarvis", True),
    ("David", "Jarvis", None),
    ("Irina", "Хей, Джарвис", None),
    ("Irina", "Эй, Джарвис, открой браузер", None),
    ("Irina", "Джарвис", None),
    ("Irina", "Джарвис, сделай потише", None),
    # hard negatives: near-homophones and ordinary commands
    ("David", "Hey Travis", False),
    ("David", "Hey service", False),
    ("David", "Turn the volume down please", False),
    ("Irina", "сделай потише", False),
    ("Irina", "открой браузер", False),
    ("Irina", "поставь таймер на десять минут", False),
    ("Irina", "закрой все окна и включи режим фокуса", False),
]


def synth(text: str, voice_hint: str, path: str) -> None:
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    for v in voice.GetVoices():
        if voice_hint in v.GetDescription():
            voice.Voice = v
            break
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(path, 3)
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()


def load16k(path: str) -> np.ndarray:
    """WAV -> float32 mono at 16 kHz. Linear resample: fidelity is not the point here."""
    with contextlib.closing(wave.open(path, "rb")) as w:
        rate, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        raw = w.readframes(n)
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != SR:
        idx = np.linspace(0, len(a) - 1, int(len(a) * SR / rate))
        a = np.interp(idx, np.arange(len(a)), a).astype(np.float32)
    return a


def to_int16(a: np.ndarray) -> np.ndarray:
    return (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)


def variants(a: np.ndarray) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(0)
    rms = float(np.sqrt(np.mean(a ** 2))) or 1e-4
    noise = rng.standard_normal(len(a)).astype(np.float32) * rms / (10 ** (10 / 20))
    return {"чисто": a, "тихо ×0.25": a * 0.25, "шум 10дБ": a + noise}


def peak(model, clip_i16: np.ndarray) -> float:
    if hasattr(model, "reset"):
        model.reset()
    preds = model.predict_clip(clip_i16)
    return max((max(p.values()) for p in preds if p), default=0.0)


def main() -> None:
    from openwakeword.model import Model
    from openwakeword.utils import download_models

    print("модели openWakeWord…")
    download_models(["hey_jarvis"])
    model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
    print(f"загружена: {list(model.models.keys())}\n")

    os.makedirs(AUDIO, exist_ok=True)
    print(f"{'фраза':<40}{'ждём':<7}{'чисто':>7}{'тихо':>7}{'шум':>7}")
    tp = fp = fn = tn = 0
    for i, (hint, text, want) in enumerate(CLIPS):
        path = os.path.join(AUDIO, f"clip_{i}.wav")
        synth(text, hint, path)
        vs = variants(load16k(path))
        scores = {k: peak(model, to_int16(v)) for k, v in vs.items()}
        best = max(scores.values())
        hit = scores["чисто"] >= THRESHOLD

        expect = "да" if want else ("нет" if want is False else "?")
        cells = "".join(f"{scores[k]:>7.2f}" for k in ("чисто", "тихо ×0.25", "шум 10дБ"))
        flag = ""
        if want is True:
            tp += hit
            fn += not hit
            flag = "  ✓" if hit else "  ✗ пропуск"
        elif want is False:
            fp += hit
            tn += not hit
            flag = "  ✗ ЛОЖНОЕ" if hit else "  ✓"
        print(f"{hint[:5]+': '+text:<40}{expect:<7}{cells}{flag}")

    print(f"\nпорог {THRESHOLD}: срабатываний верно {tp}, пропусков {fn}, "
          f"ложных {fp}, верных отказов {tn}")

    # Long stretch of ordinary Russian speech: how often does it false-trigger?
    long_path = os.path.join(HERE, "audio", "long.wav")
    if os.path.exists(long_path):
        a = to_int16(load16k(long_path))
        if hasattr(model, "reset"):
            model.reset()
        preds = model.predict_clip(a)
        scores = [max(p.values()) for p in preds if p]
        minutes = len(a) / SR / 60
        print(f"\nдлинная русская речь, {minutes:.1f} мин: макс {max(scores):.3f}, "
              f"кадров ≥0.3: {sum(s >= 0.3 for s in scores)}, ≥0.5: "
              f"{sum(s >= 0.5 for s in scores)}")

    # Standing cost: one 80 ms frame every 80 ms, forever.
    src = to_int16(load16k(os.path.join(AUDIO, "clip_1.wav")))
    src = np.tile(src, 4)
    frames = [src[i:i + 1280] for i in range(0, len(src) - 1280, 1280)]
    for f in frames[:5]:
        model.predict(f)  # warm up
    ms = []
    for f in frames:
        t0 = time.perf_counter()
        model.predict(f)
        ms.append((time.perf_counter() - t0) * 1000)
    med, p95 = statistics.median(ms), sorted(ms)[int(len(ms) * 0.95)]
    print(f"\nстоимость кадра 80 мс: медиана {med:.1f} мс, p95 {p95:.1f} мс "
          f"→ около {med / 80 * 100:.0f}% одного ядра постоянно")


if __name__ == "__main__":
    main()
