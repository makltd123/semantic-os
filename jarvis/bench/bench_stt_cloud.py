"""E8: cloud transcription (Voxtral) against local Whisper.

E7 showed the local cost is a near-constant ~1.1s regardless of utterance length --
Whisper pads everything to a 30s window -- so transcribing while the user speaks saves
nothing locally. The remaining lever is not to spend that second on this CPU at all.

The network round trip to Mistral is already measured at 0.42s (E5), so the question is
what Voxtral adds on top, and whether Russian accuracy holds up.

Uses the WAVs E1 generated in bench/audio/.

Usage:  python bench/bench_stt_cloud.py [model]
"""

from __future__ import annotations

import glob
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from jarvis.llm import _mistral_key  # noqa: E402

URL = "https://api.mistral.ai/v1/audio/transcriptions"
DEFAULT_MODEL = "voxtral-mini-latest"

EXPECTED = {
    "ru_0": "сделай потише",
    "ru_1": "открой браузер",
    "ru_2": "поставь таймер на десять минут",
    "ru_3": "что у меня сейчас на экране",
    "ru_4": "закрой все окна и включи режим фокуса",
}


def transcribe(path: str, model: str, client: httpx.Client) -> tuple[str, float, str]:
    with open(path, "rb") as f:
        files = {"file": (os.path.basename(path), f, "audio/wav")}
        data = {"model": model, "language": "ru"}
        t0 = time.perf_counter()
        try:
            r = client.post(URL, files=files, data=data,
                            headers={"Authorization": f"Bearer {_mistral_key()}"})
        except Exception as e:
            return "", time.perf_counter() - t0, f"{type(e).__name__}"
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        return "", dt, f"http {r.status_code}: {r.text[:120]}"
    try:
        return r.json().get("text", "").strip(), dt, ""
    except ValueError:
        return "", dt, "bad json"


def main() -> None:
    model = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODEL
    audio_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio")
    paths = sorted(glob.glob(os.path.join(audio_dir, "ru_*.wav")))
    if not paths:
        print("нет файлов bench/audio/ru_*.wav — сначала запусти bench_stt.py")
        return

    print(f"=== {model} ===")
    times, exact = [], 0
    with httpx.Client(timeout=30) as client:
        for n, path in enumerate(paths):
            key = os.path.basename(path)[:-4]
            want = EXPECTED.get(key, "")
            text, dt, err = transcribe(path, model, client)
            ok = text.lower().strip(" .,!?") == want
            exact += ok
            if n > 0:
                times.append(dt)
            mark = "✓" if ok else ("✗" if text else "—")
            print(f"  {mark} {dt:5.2f}s  {text!r}{'  ' + err if err else ''}"
                  f"{'   (первый, с TLS)' if n == 0 else ''}")
            time.sleep(1.3)  # rate-limit headroom, excluded from timings

    if times:
        print(f"  тёплая медиана {statistics.median(times):.2f}s, "
              f"дословно {exact}/{len(paths)}")
    print("\n  для сравнения, локально (E1/E7): base ~1.0-1.2s, tiny ~0.55s")


if __name__ == "__main__":
    main()
