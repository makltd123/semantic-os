"""Speech in via faster-whisper.

E1 settled the model choice: `base`/int8 is ~1.0s per short phrase and got 5/5 of the
test utterances right, while `tiny` is twice as fast but mangles exactly the words that
carry the intent ("Откроибраузер", "режим фокусы"). Saving 0.5s is not worth missing
the intent.

Loading costs 17-20s, which is why this object is built once and held for the process
lifetime -- the daemon shape is forced by the measurement, not chosen for tidiness.
"""

from __future__ import annotations

import numpy as np
from faster_whisper import WhisperModel

DEFAULT_MODEL = "base"


class Transcriber:
    def __init__(self, model_size: str = DEFAULT_MODEL, language: str = "ru") -> None:
        self.language = language
        self.model = WhisperModel(
            model_size,
            device="cpu",
            compute_type="int8",
            cpu_threads=4,
        )
        self._warm()

    def _warm(self) -> None:
        """Run one pass on silence so the first real utterance isn't the slow one."""
        silence = np.zeros(16_000, dtype=np.float32)
        try:
            list(self.model.transcribe(silence, language=self.language)[0])
        except Exception:
            pass

    def transcribe(self, audio: np.ndarray) -> str:
        if len(audio) == 0:
            return ""
        segments, _ = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=1,          # greedy: beam search buys accuracy we don't need here
            vad_filter=True,      # trims leading/trailing silence, so less audio to decode
            condition_on_previous_text=False,  # each utterance is independent
        )
        return " ".join(s.text for s in segments).strip()
