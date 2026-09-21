"""Two-tier intent resolution -- the mechanism H1 proposes.

    fast path:  normalize -> exact cache -> fuzzy cache      (~1ms, local)
    slow path:  claude CLI                                    (seconds)
    then:       promote the slow result into the fast path

Meta-commands ("не то", "забудь") are checked before everything else. They exist because
the literature names a wrong mental model as a top reason people abandon voice
interfaces: a system that learns must also be correctable out loud, or its mistakes
become permanent and invisible.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import llm
from .cache import IntentCache, normalize
from .state import SystemState
from .timing import Turn

# Spoken corrections. Kept as a small literal set: these must never reach the LLM,
# both for speed and because a mis-resolved correction is especially confusing.
_FORGET_PHRASES = {
    "не то", "неправильно", "не так", "забудь", "отмена", "отмени",
    "неверно", "я не это имел в виду", "не это",
}


@dataclass
class Resolution:
    action: str | None
    args: dict[str, Any] = field(default_factory=dict)
    say: str = ""
    route: str = "none"       # cache-exact | cache-fuzzy | llm | meta | none
    note: str = ""            # shown in the HUD, not spoken


class Resolver:
    def __init__(self, cache: IntentCache, use_llm: bool = True) -> None:
        self.cache = cache
        self.use_llm = use_llm
        self.last_utterance: str = ""   # what to forget if the next thing is "не то"

    def resolve(self, utterance: str, st: SystemState, turn: Turn) -> Resolution:
        key = normalize(utterance)
        if not key:
            return Resolution(None, say="Не расслышала", route="none")

        if key in _FORGET_PHRASES:
            return self._handle_forget()

        with turn.stage("cache"):
            entry, route, note = self.cache.lookup(utterance)

        if entry is not None:
            self.last_utterance = utterance
            learned = f"выучено из «{entry.raw}», использовано {entry.hits}×"
            return Resolution(
                action=entry.action,
                args=dict(entry.args),
                route=route,
                note=f"{note} · {learned}" if note else learned,
            )

        if not self.use_llm:
            return Resolution(None, say="Не знаю такой команды", route="none",
                              note=note)

        with turn.stage("llm"):
            got = llm.resolve(utterance, st)

        if got.action is None:
            return Resolution(None, say=got.say or "Не поняла", route="llm",
                              note=" · ".join(x for x in (note, got.raw[:200]) if x))

        # Promote into the fast path so the same phrasing is instant next time.
        with turn.stage("learn"):
            self.cache.learn(utterance, got.action, got.args)

        self.last_utterance = utterance
        learned = "новое — запомнила"
        return Resolution(
            action=got.action, args=got.args, say=got.say, route="llm",
            note=f"{note} · {learned}" if note else learned,
        )

    def _handle_forget(self) -> Resolution:
        if not self.last_utterance:
            return Resolution(None, say="Нечего забывать", route="meta")
        dropped = self.cache.forget(self.last_utterance)
        phrase = self.last_utterance
        self.last_utterance = ""
        if dropped:
            return Resolution(None, say="Забыла", route="meta",
                              note=f"удалено из кэша: «{phrase}»")
        return Resolution(None, say="Этого не было в памяти", route="meta")
