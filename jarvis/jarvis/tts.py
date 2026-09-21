"""Speech out via SAPI (Microsoft Irina, ru-RU) -- offline and effectively instant.

Quality is mediocre next to neural voices, but E1's latency budget has no room for a
cloud round-trip, and Irina ships with the OS. Speaking happens on a worker thread so
an action never waits on audio.
"""

from __future__ import annotations

import queue
import threading

import pythoncom
import win32com.client

SVSF_ASYNC = 1
SVSF_PURGE_BEFORE_SPEAK = 2


class Speaker:
    def __init__(self, prefer: str = "Irina") -> None:
        self._q: queue.Queue[str | None] = queue.Queue()
        self._prefer = prefer
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        # COM must be initialised on the thread that uses it.
        pythoncom.CoInitialize()
        voice = win32com.client.Dispatch("SAPI.SpVoice")
        for v in voice.GetVoices():
            desc = v.GetDescription()
            if self._prefer in desc:
                voice.Voice = v
                break
        voice.Rate = 1  # slightly faster than default; Irina is leisurely

        while True:
            text = self._q.get()
            if text is None:
                break
            try:
                # PURGE cancels whatever is mid-sentence: a new turn outranks an old reply.
                voice.Speak(text, SVSF_PURGE_BEFORE_SPEAK)
            except Exception:
                pass

    def say(self, text: str) -> None:
        if text:
            self._q.put(text)

    def stop(self) -> None:
        self._q.put(None)
