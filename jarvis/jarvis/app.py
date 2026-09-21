"""Wiring: hold a key, speak, get an action -- with every stage timed and shown.

Threading: tkinter owns the main thread, pynput listens on its own, and each utterance
is handled on a worker so a 6-9s LLM call never freezes the overlay.
"""

from __future__ import annotations

import threading
import time

from . import actions, config
from .audio import Recorder, duration_sec, is_probably_silence
from .cache import IntentCache
from .hud import ACCENT_FAIL, ACCENT_FAST, ACCENT_LIVE, ACCENT_SLOW, Hud
from .resolver import Resolver
from .semantic import SemanticIndex
from .state import snapshot
from .stt import Transcriber
from .timing import Trace, Turn
from .tts import Speaker

_ROUTE_LABEL = {
    "cache-exact": "кэш (точно)",
    "cache-fuzzy": "кэш (похоже)",
    "cache-semantic": "кэш (по смыслу)",
    "llm": "LLM — новое",
    "meta": "команда памяти",
    "none": "не разобрано",
}

_FAST_ROUTES = {"cache-exact", "cache-fuzzy", "cache-semantic", "meta"}

# Hard stop for the silence watcher: if the key is held this long with no clear end of
# speech, give up waiting rather than leak a thread per press.
MAX_UTTERANCE_SEC = 30


class Jarvis:
    def __init__(self, stt_model: str = config.STT_MODEL, use_llm: bool = True,
                 with_audio: bool = True, with_hud: bool = True,
                 use_semantic: bool = config.SEMANTIC) -> None:
        self.use_llm = use_llm
        self.with_audio = with_audio

        semantic = SemanticIndex() if use_semantic else None
        self.cache = IntentCache(config.CACHE_PATH, semantic=semantic)
        self.resolver = Resolver(self.cache, use_llm=use_llm)
        self.trace = Trace(config.TRACE_PATH)

        self.speaker = Speaker(config.TTS_VOICE) if with_audio else None
        self.hud = Hud(on_quit=self.shutdown) if with_hud else None

        self.recorder = Recorder() if with_audio else None
        self.transcriber: Transcriber | None = None
        self._stt_model = stt_model
        self._busy = threading.Lock()
        self._stop_lock = threading.Lock()
        self._running = True

        # Snapshot taken while the user is still speaking, so it costs nothing on the
        # critical path. On a cache hit the snapshot *was* the whole latency (40ms).
        self._prefetched: tuple[float, object] | None = None

    PREFETCH_MAX_AGE = 8.0  # seconds; older than this and we re-snapshot

    def _prefetch_state(self) -> None:
        def work() -> None:
            try:
                import pythoncom
                pythoncom.CoInitialize()   # snapshot touches COM (audio sessions)
            except Exception:
                pass
            try:
                self._prefetched = (time.time(), snapshot())
            except Exception:
                self._prefetched = None

        threading.Thread(target=work, daemon=True).start()

    def _take_state(self):
        """Use the prefetched snapshot if it is fresh, else take one now."""
        got = self._prefetched
        self._prefetched = None
        if got and (time.time() - got[0]) < self.PREFETCH_MAX_AGE:
            return got[1]
        return snapshot()

    # --- model loading --------------------------------------------------------

    def load_stt(self) -> None:
        """17-20s (E1). Done once at startup, off the main thread."""
        if self.transcriber is None:
            self.transcriber = Transcriber(self._stt_model)

    def start_semantic(self, block: bool = False) -> None:
        """Embedding tier; the exact/fuzzy tiers answer while it loads."""
        self.cache.start_semantic(actions.ANCHORS, block=block)

    # --- the core turn --------------------------------------------------------

    def handle_text(self, text: str, speak: bool = True) -> Turn:
        """Everything after speech-to-text. Reusable without a microphone."""
        turn = Turn(utterance=text)

        with turn.stage("state"):
            st = self._take_state()

        res = self.resolver.resolve(text, st, turn)
        turn.route = res.route

        if res.action is None:
            turn.ok = res.route == "meta"
            turn.said = res.say
            turn.error = "" if turn.ok else "unresolved"
            self._show(turn, res.say, res.note,
                       ACCENT_FAST if turn.ok else ACCENT_FAIL)
            if speak and self.speaker:
                self.speaker.say(res.say)
            self.trace.write(turn)
            return turn

        turn.action = res.action
        turn.args = res.args

        with turn.stage("act"):
            out = actions.execute(res.action, res.args, st)

        turn.ok = out.ok
        turn.said = out.say or res.say
        if not out.ok:
            turn.error = out.detail

        detail = " · ".join(x for x in (res.note, out.detail) if x)
        accent = ACCENT_FAIL if not out.ok else (
            ACCENT_FAST if res.route in _FAST_ROUTES else ACCENT_SLOW)
        self._show(turn, turn.said, detail, accent)

        if speak and self.speaker:
            self.speaker.say(turn.said)

        self.trace.write(turn)
        return turn

    def _show(self, turn: Turn, said: str, detail: str, accent: str) -> None:
        if not self.hud:
            return
        route = _ROUTE_LABEL.get(turn.route, turn.route)
        head = f"«{turn.utterance}»"
        body = said if not turn.action else f"{said}   →  {turn.action}"
        if detail:
            body += f"\n{detail}"
        self.hud.update(
            status=f"{route}  ·  {turn.total_ms:.0f} мс",
            utterance=head,
            detail=body,
            timing=turn.summary(),
            accent=accent,
        )
        self.hud.hide_after(config.HUD_LINGER_MS)

    # --- push to talk ---------------------------------------------------------

    def start_recording(self) -> None:
        if not self.recorder or self.recorder.is_recording:
            return
        self.recorder.start()
        self._prefetch_state()
        self._watch_for_end_of_speech()
        if self.hud:
            self.hud.update(status="слушаю…", utterance="", detail="", timing="",
                            accent=ACCENT_LIVE)

    def _watch_for_end_of_speech(self) -> None:
        """Start processing when the speaker stops, not when the key comes up."""
        def watch() -> None:
            if self.recorder.speech_ended.wait(timeout=MAX_UTTERANCE_SEC):
                self.stop_recording_and_handle()

        threading.Thread(target=watch, daemon=True).start()

    def stop_recording_and_handle(self) -> None:
        # Two callers race here: the released key and the silence watcher.
        with self._stop_lock:
            if not self.recorder or not self.recorder.is_recording:
                return
            audio = self.recorder.stop()

        if is_probably_silence(audio) or duration_sec(audio) < 0.25:
            if self.hud:
                self.hud.update(status="тишина", utterance="", detail="", timing="",
                                accent=ACCENT_FAIL)
                self.hud.hide_after(1200)
            return

        threading.Thread(target=self._process, args=(audio,), daemon=True).start()

    def _process(self, audio) -> None:
        # One utterance at a time: overlapping turns would interleave actions.
        if not self._busy.acquire(blocking=False):
            return
        try:
            if self.hud:
                self.hud.update(status="разбираю речь…", accent=ACCENT_LIVE)

            turn = Turn()
            with turn.stage("stt"):
                text = self.transcriber.transcribe(audio) if self.transcriber else ""

            if not text:
                if self.hud:
                    self.hud.update(status="не разобрала", utterance="",
                                    detail="", timing=turn.summary(), accent=ACCENT_FAIL)
                    self.hud.hide_after(2000)
                return

            if self.hud:
                self.hud.update(status="ищу смысл…", utterance=f"«{text}»",
                                detail="", timing=turn.summary(), accent=ACCENT_LIVE)

            inner = self.handle_text(text)
            # Fold the STT cost in so the HUD shows true end-to-end latency.
            inner.stages = {"stt": turn.stages.get("stt", 0.0), **inner.stages}
            accent = (ACCENT_FAIL if not inner.ok else
                      ACCENT_FAST if inner.route in _FAST_ROUTES else ACCENT_SLOW)
            self._show(inner, inner.said, "", accent)
        finally:
            self._busy.release()

    # --- lifecycle ------------------------------------------------------------

    def shutdown(self) -> None:
        self._running = False
        if self.speaker:
            self.speaker.stop()
        self.cache.save()

    def run(self) -> None:
        """Blocking: loads STT in the background, then runs the HUD event loop."""
        def boot() -> None:
            t0 = time.perf_counter()
            if self.hud:
                self.hud.update(status="загружаю модель распознавания…",
                                utterance="jarvis", detail="это разовые ~20 секунд",
                                accent=ACCENT_SLOW)
            self.start_semantic()   # loads in parallel with Whisper
            self.load_stt()
            secs = time.perf_counter() - t0
            stats = self.cache.stats()
            if self.hud:
                self.hud.update(
                    status="готов",
                    utterance="jarvis",
                    detail=f"удерживай {config.HOTKEY} и говори   ·   "
                           f"в памяти намерений: {stats['entries']}",
                    timing=f"загрузка модели {secs:.1f}s",
                    accent=ACCENT_FAST,
                )
                self.hud.hide_after(5000)
            self._start_hotkey()

        threading.Thread(target=boot, daemon=True).start()
        if self.hud:
            self.hud.run()

    def _start_hotkey(self) -> None:
        from pynput import keyboard

        target = getattr(keyboard.Key, config.HOTKEY, keyboard.Key.ctrl_r)

        def on_press(key) -> None:
            if key == target:
                self.start_recording()

        def on_release(key) -> None:
            if key == target:
                self.stop_recording_and_handle()

        listener = keyboard.Listener(on_press=on_press, on_release=on_release)
        listener.daemon = True
        listener.start()
