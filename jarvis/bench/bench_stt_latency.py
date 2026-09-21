"""E7: where exactly does the ~1.0s of speech recognition go?

STT is now the biggest term in the budget (~1.0s of ~1.9s). Before optimising, find out
what the second is made of. The key question is whether cost scales with utterance
length: Whisper pads every input to a 30s window, so it may be near-constant -- in which
case "transcribe while the user speaks" buys nothing and the effort belongs elsewhere.

Measures:
  A. latency vs audio length      -- is it constant or proportional?
  B. latency vs cpu_threads       -- 4 threads on 2 physical cores may contend
  C. decoding options             -- without_timestamps, vad_filter, compute_type
  D. model sizes                  -- what tiny/base/small actually cost here

Usage:  python bench/bench_stt_latency.py
"""

from __future__ import annotations

import os
import statistics
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPEATS = 3
SR = 16_000


def make_speech(seconds: float) -> np.ndarray:
    """Real speech of a given length, via the offline SAPI voice.

    Synthetic audio is cleaner than a microphone, but this experiment is about
    compute cost, not accuracy, so that is fine.
    """
    import wave
    import contextlib
    import win32com.client

    text = ("сделай потише и открой браузер потом сверни все окна "
            "и включи режим фокуса чтобы никто не отвлекал меня от работы "
            "и ещё поставь таймер на десять минут пожалуйста ") * 4

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio", "long.wav")
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if not os.path.exists(path):
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for v in voice.GetVoices():
            if "ru" in v.GetAttribute("Language") or "Irina" in v.GetDescription():
                voice.Voice = v
                break
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        stream.Open(path, 3)
        voice.AudioOutputStream = stream
        voice.Speak(text)
        stream.Close()

    with contextlib.closing(wave.open(path, "rb")) as w:
        raw = w.readframes(w.getnframes())
        rate = w.getframerate()
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    if rate != SR:  # cheap linear resample; fidelity is irrelevant here
        idx = np.linspace(0, len(audio) - 1, int(len(audio) * SR / rate))
        audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)

    want = int(seconds * SR)
    if len(audio) < want:
        audio = np.tile(audio, int(np.ceil(want / len(audio))))
    return audio[:want]


def timeit(model, audio, **kw) -> tuple[float, str]:
    opts = dict(language="ru", beam_size=1, condition_on_previous_text=False)
    opts.update(kw)
    runs, text = [], ""
    for _ in range(REPEATS):
        t0 = time.perf_counter()
        segments, _ = model.transcribe(audio, **opts)
        text = " ".join(s.text for s in segments).strip()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs), text


def main() -> None:
    from faster_whisper import WhisperModel

    print("=== A. задержка от длины записи (base, int8, 4 потока) ===")
    model = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=4)
    timeit(model, make_speech(2.0))  # warm
    for secs in (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 15.0):
        ms, _ = timeit(model, make_speech(secs))
        print(f"  {secs:5.1f}s аудио -> {ms * 1000:6.0f}мс   ({ms / secs:.2f} от реального времени)")

    print("\n=== B. задержка от числа потоков (base, 3с аудио) ===")
    audio3 = make_speech(3.0)
    for threads in (1, 2, 3, 4):
        m = WhisperModel("base", device="cpu", compute_type="int8", cpu_threads=threads)
        timeit(m, audio3)
        ms, _ = timeit(m, audio3)
        print(f"  {threads} потока -> {ms * 1000:6.0f}мс")
        del m

    print("\n=== C. опции декодирования (base, 3с, 4 потока) ===")
    variants = [
        ("как сейчас (vad + timestamps)", dict(vad_filter=True)),
        ("без временных меток", dict(vad_filter=True, without_timestamps=True)),
        ("без vad", dict(vad_filter=False)),
        ("без vad и без меток", dict(vad_filter=False, without_timestamps=True)),
    ]
    for label, kw in variants:
        ms, _ = timeit(model, audio3, **kw)
        print(f"  {label:<32} {ms * 1000:6.0f}мс")

    print("\n=== C2. тип вычислений (base, 3с) ===")
    for ct in ("int8", "int8_float32", "float32"):
        try:
            m = WhisperModel("base", device="cpu", compute_type=ct, cpu_threads=4)
            timeit(m, audio3)
            ms, _ = timeit(m, audio3)
            print(f"  {ct:<14} {ms * 1000:6.0f}мс")
            del m
        except Exception as e:
            print(f"  {ct:<14} недоступен: {type(e).__name__}")

    print("\n=== D. размеры моделей (3с, int8, 4 потока) ===")
    for size in ("tiny", "base", "small"):
        try:
            t0 = time.perf_counter()
            m = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=4)
            load = time.perf_counter() - t0
            timeit(m, audio3)
            ms, text = timeit(m, audio3)
            print(f"  {size:<6} загрузка {load:5.1f}s, распознавание {ms * 1000:6.0f}мс")
            print(f"         {text[:90]!r}")
            del m
        except Exception as e:
            print(f"  {size:<6} ошибка: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
