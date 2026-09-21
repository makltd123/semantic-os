"""Benchmark: is Whisper fast enough for a Russian voice loop on this CPU?

Generates reference audio with the offline SAPI voice (Irina, ru-RU), then times
faster-whisper across model sizes. We care about latency first, WER second --
the question being answered is "does the voice premise survive on 2 cores".

Usage:  python bench/bench_stt.py [tiny base small]
"""

import os
import sys
import time
import wave
import contextlib

import win32com.client

HERE = os.path.dirname(os.path.abspath(__file__))
AUDIO_DIR = os.path.join(HERE, "audio")

# Utterances typical of the intended use: short, imperative, informal Russian.
PHRASES = [
    "сделай потише",
    "открой браузер",
    "поставь таймер на десять минут",
    "что у меня сейчас на экране",
    "закрой все окна и включи режим фокуса",
]

SSFM_CREATE_FOR_WRITE = 3


def synth(text: str, path: str) -> float:
    """Speak `text` into a WAV file with the ru-RU voice. Returns duration in sec."""
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    for v in voice.GetVoices():
        if "ru" in v.GetAttribute("Language") or "Irina" in v.GetDescription():
            voice.Voice = v
            break
    stream = win32com.client.Dispatch("SAPI.SpFileStream")
    stream.Open(path, SSFM_CREATE_FOR_WRITE)
    voice.AudioOutputStream = stream
    voice.Speak(text)
    stream.Close()

    with contextlib.closing(wave.open(path, "rb")) as w:
        return w.getnframes() / float(w.getframerate())


def main() -> None:
    sizes = sys.argv[1:] or ["tiny", "base"]
    os.makedirs(AUDIO_DIR, exist_ok=True)

    print("=== generating reference audio (SAPI ru-RU) ===")
    clips = []
    for i, phrase in enumerate(PHRASES):
        path = os.path.join(AUDIO_DIR, f"ru_{i}.wav")
        dur = synth(phrase, path)
        clips.append((path, phrase, dur))
        print(f"  [{dur:5.2f}s] {phrase}")

    from faster_whisper import WhisperModel

    for size in sizes:
        print(f"\n=== {size} / int8 / cpu ===")
        t0 = time.perf_counter()
        model = WhisperModel(size, device="cpu", compute_type="int8", cpu_threads=4)
        print(f"  load: {time.perf_counter() - t0:6.2f}s")

        # Warm the graph so the first real utterance isn't the outlier.
        model.transcribe(clips[0][0], language="ru")

        total_audio = total_wall = 0.0
        for path, expected, dur in clips:
            t0 = time.perf_counter()
            segments, _ = model.transcribe(path, language="ru", beam_size=1)
            text = " ".join(s.text for s in segments).strip()
            wall = time.perf_counter() - t0

            total_audio += dur
            total_wall += wall
            hit = "OK " if text.lower().strip(" .,!?") == expected else "DIFF"
            print(f"  {hit} {wall:5.2f}s (rtf {wall / dur:4.2f})  {text!r}")

        print(f"  TOTAL rtf: {total_wall / total_audio:.2f}  "
              f"({total_wall:.1f}s wall / {total_audio:.1f}s audio)")


if __name__ == "__main__":
    main()
