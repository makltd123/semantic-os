"""E10b: Mistral Voxtral TTS (cloud) -- first find out how the API wants to be called.

The docs give the model id and the SDK call but not the raw REST shape, and the official
SDK does not install on this machine (see RESEARCH_LOG E8). So probe the endpoint,
learn the schema from its own error messages, then time it on the same lines as the
local voices. Voxtral TTS advertises nine languages; whether Russian is one of them is
part of what this finds out.

Usage:  python bench/bench_tts_cloud.py
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import statistics
import sys
import time
import wave

import httpx

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from jarvis.llm import _mistral_key  # noqa: E402

OUT = os.path.join(HERE, "audio", "voices")
BASE = "https://api.mistral.ai/v1"
MODEL = "voxtral-mini-tts-2603"

LINES = [
    "Слушаю, сэр.",
    "Громкость уменьшена.",
    "Режим фокуса включён. Мессенджеры приглушены.",
    "Боюсь, сэр, вы не уточнили: заблокировать компьютер или просто не гасить экран?",
    "Сэр, заряд батареи — пятнадцать процентов.",
]

HEADERS = {"Authorization": f"Bearer {_mistral_key()}"}


def wav_seconds(data: bytes) -> float:
    try:
        with contextlib.closing(wave.open(io.BytesIO(data), "rb")) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0


def speak(client: httpx.Client, text: str, **extra) -> tuple[int, float, bytes, str]:
    body = {"model": MODEL, "input": text, "response_format": "wav", **extra}
    t0 = time.perf_counter()
    r = client.post(f"{BASE}/audio/speech", json=body, headers=HEADERS)
    dt = time.perf_counter() - t0
    if r.status_code != 200:
        return r.status_code, dt, b"", r.text[:300]
    ctype = r.headers.get("content-type", "")
    if "json" in ctype:
        data = r.json()
        b64 = data.get("audio_data") or data.get("audio") or ""
        return 200, dt, base64.b64decode(b64) if b64 else b"", f"json keys: {list(data)}"
    return 200, dt, r.content, f"raw {ctype}"


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    with httpx.Client(timeout=60) as client:
        print("=== список голосов ===")
        for path in ("/audio/voices", "/audio/speech/voices"):
            r = client.get(f"{BASE}{path}", headers=HEADERS)
            print(f"  GET {path}: {r.status_code}  {r.text[:400]}")
            if r.status_code == 200:
                break
        time.sleep(1.5)

        print("\n=== как вызывать ===")
        variants = [
            ("без голоса", {}),
            ("voice=casual_male", {"voice": "casual_male"}),
            ("voice_id=casual_male", {"voice_id": "casual_male"}),
        ]
        working: dict | None = None
        for label, extra in variants:
            status, dt, data, note = speak(client, LINES[0], **extra)
            print(f"  {label:<24} {status}  {dt:.2f}s  {len(data)} байт  {note[:200]}")
            time.sleep(1.5)
            if status == 200 and data:
                working = extra
                break

        if working is None:
            print("\nни один вариант не сработал — см. тексты ошибок выше")
            return

        print(f"\n=== задержка на 5 строках (параметры: {working or 'по умолчанию'}) ===")
        print(f"  {'строка':<8}{'ответ':>9}{'аудио':>8}")
        times = []
        for i, line in enumerate(LINES):
            status, dt, data, note = speak(client, line, **working)
            secs = wav_seconds(data)
            if data:
                with open(os.path.join(OUT, f"mistral-tts-{i + 1}.wav"), "wb") as f:
                    f.write(data)
            print(f"  {i + 1:<8}{dt * 1000:>7.0f}мс{secs:>7.1f}с   {status}")
            if i > 0:
                times.append(dt)
            time.sleep(1.5)
        if times:
            print(f"  медиана (без первого): {statistics.median(times) * 1000:.0f}мс")
    print(f"\nфайлы для прослушивания: {OUT}")


if __name__ == "__main__":
    main()
