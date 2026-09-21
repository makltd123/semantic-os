"""Microphone capture for push-to-talk.

Push-to-talk rather than a wake word on purpose (see RESEARCH_LOG "Развилки"): no false
triggers, no always-on model burning one of two cores, and the recording boundaries are
exact -- which keeps E1's latency numbers honest.
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000  # what Whisper wants; avoids a resample step
CHANNELS = 1

# Silence long enough to mean "finished speaking" rather than "drew breath".
SILENCE_MS = 600
SPEECH_RMS = 0.012


class Recorder:
    """Records while `start()`..`stop()` is open. Returns float32 mono at 16 kHz.

    Also raises `speech_ended` once the speaker has clearly stopped. Waiting for the
    key to come up puts the user's own reaction time on the critical path -- people
    hold the key a few hundred ms after they finish the sentence. Ending on silence
    starts the work when they stop talking instead.

    A plain RMS gate, not a neural VAD: it costs nothing on two cores, and for short
    commands in a quiet room it is enough. Revisit if it misfires in noise.
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE,
                 silence_ms: int = SILENCE_MS, speech_rms: float = SPEECH_RMS) -> None:
        self.sample_rate = sample_rate
        self.silence_ms = silence_ms
        self.speech_rms = speech_rms
        self.speech_ended = threading.Event()
        self._chunks: list[np.ndarray] = []
        self._stream: sd.InputStream | None = None
        self._heard_speech = False
        self._silent_frames = 0

    def _callback(self, indata, frames, time_info, status) -> None:
        self._chunks.append(indata.copy())

        rms = float(np.sqrt(np.mean(indata ** 2))) if frames else 0.0
        if rms >= self.speech_rms:
            self._heard_speech = True
            self._silent_frames = 0
        elif self._heard_speech:
            self._silent_frames += frames
            if self._silent_frames * 1000 / self.sample_rate >= self.silence_ms:
                self.speech_ended.set()

    def start(self) -> None:
        if self._stream is not None:
            return
        self._chunks = []
        self._heard_speech = False
        self._silent_frames = 0
        self.speech_ended.clear()
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="float32",
            callback=self._callback,
            blocksize=0,          # let PortAudio pick; lowest latency it can manage
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        if self._stream is None:
            return np.zeros(0, dtype=np.float32)
        self._stream.stop()
        self._stream.close()
        self._stream = None
        if not self._chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._chunks, axis=0).flatten().astype(np.float32)

    @property
    def is_recording(self) -> bool:
        return self._stream is not None


def duration_sec(audio: np.ndarray, sample_rate: int = SAMPLE_RATE) -> float:
    return len(audio) / float(sample_rate)


def is_probably_silence(audio: np.ndarray, threshold: float = 0.005) -> bool:
    """Cheap RMS gate so an accidental key tap doesn't cost a Whisper pass."""
    if len(audio) == 0:
        return True
    return float(np.sqrt(np.mean(audio ** 2))) < threshold
