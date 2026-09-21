"""E10a: local neural Russian voices against the built-in SAPI voice.

Voice is the most audible difference between a voice remote and a Jarvis, and Irina
(SAPI) sounds like a 2005 satnav. Silero's neural Russian voices run on CPU through the
torch that is already installed. The question is whether they are fast enough here.

Writes WAVs to bench/audio/voices/ so the result can be *listened to*, not just timed.
Note: Silero's models are CC BY-NC-SA -- fine for a personal project, a blocker for
anything commercial.

Usage:  python bench/bench_tts_local.py [v4_ru] [v5_ru]
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
import wave

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MODELS = os.path.join(ROOT, "data", "models")
OUT = os.path.join(HERE, "audio", "voices")
BASE = "https://models.silero.ai/models/tts/ru/{}.pt"

# Lines in the register the persona would actually use.
LINES = [
    "Слушаю, сэр.",
    "Громкость уменьшена.",
    "Режим фокуса включён. Мессенджеры приглушены.",
    "Боюсь, сэр, вы не уточнили: заблокировать компьютер или просто не гасить экран?",
    "Сэр, заряд батареи — пятнадцать процентов.",
]


def fetch(name: str) -> str:
    os.makedirs(MODELS, exist_ok=True)
    path = os.path.join(MODELS, f"{name}.pt")
    if os.path.exists(path):
        return path
    print(f"  скачиваю {name}…", flush=True)
    t0 = time.perf_counter()
    urllib.request.urlretrieve(BASE.format(name), path)
    print(f"  {os.path.getsize(path) / 1e6:.0f} МБ за {time.perf_counter() - t0:.0f}с")
    return path


def save_wav(path: str, audio: np.ndarray, rate: int) -> None:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())


def run_silero(name: str) -> None:
    import torch

    torch.set_num_threads(2)  # two physical cores; more only contends (cf. E7)
    path = fetch(name)

    t0 = time.perf_counter()
    imp = torch.package.PackageImporter(path)
    model = imp.load_pickle("tts_models", "model")
    model.to(torch.device("cpu"))
    print(f"=== silero {name}: загрузка {time.perf_counter() - t0:.1f}с, "
          f"голоса {list(model.speakers)} ===")

    rate = 24_000
    speakers = [s for s in ("aidar", "eugene") if s in model.speakers]
    model.apply_tts(text="проверка", speaker=speakers[0], sample_rate=rate,
                    put_accent=True, put_yo=True)  # warm up

    print(f"  {'голос':<8}{'строка':<8}{'синтез':>9}{'аудио':>8}{'RTF':>7}")
    for spk in speakers:
        for i, line in enumerate(LINES):
            t0 = time.perf_counter()
            audio = model.apply_tts(text=line, speaker=spk, sample_rate=rate,
                                    put_accent=True, put_yo=True)
            dt = time.perf_counter() - t0
            arr = audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio)
            secs = len(arr) / rate
            save_wav(os.path.join(OUT, f"silero-{name}-{spk}-{i + 1}.wav"), arr, rate)
            print(f"  {spk:<8}{i + 1:<8}{dt * 1000:>7.0f}мс{secs:>7.1f}с{dt / secs:>7.2f}")


def run_sapi() -> None:
    import win32com.client

    print("=== SAPI Irina (для сравнения) ===")
    for i, line in enumerate(LINES):
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for v in voice.GetVoices():
            if "Irina" in v.GetDescription():
                voice.Voice = v
        stream = win32com.client.Dispatch("SAPI.SpFileStream")
        path = os.path.join(OUT, f"sapi-irina-{i + 1}.wav")
        stream.Open(path, 3)
        voice.AudioOutputStream = stream
        t0 = time.perf_counter()
        voice.Speak(line)
        dt = time.perf_counter() - t0
        stream.Close()
        print(f"  строка {i + 1}: синтез {dt * 1000:.0f}мс")


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    names = sys.argv[1:] or ["v4_ru"]
    run_sapi()
    for name in names:
        try:
            run_silero(name)
        except Exception as e:
            print(f"  ✗ {name}: {type(e).__name__}: {e}")
    print(f"\nфайлы для прослушивания: {OUT}")


if __name__ == "__main__":
    main()
