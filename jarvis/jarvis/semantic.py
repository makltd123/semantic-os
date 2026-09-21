"""Semantic tier of the intent cache: embeddings with a margin rule.

Why this shape (RESEARCH_LOG E3, E4):

  * Character fuzzy matching caught 1/25 paraphrases -- a typo cache, not a meaning
    cache.
  * Embeddings caught 12/25, but also got 6 wrong, including inversions: embeddings
    encode the *topic* (volume), not the *direction* (up/down). "Приглуши" landed on
    volume_up at cosine 0.80. High similarity does not protect against that.
  * What does protect is the margin between the best two *different* actions. Every
    wrong answer had margin <= 0.16. With threshold 0.5 and margin 0.2 the held-out set
    went from 3 errors to 0, at the price of sending more phrases to the LLM.

A miss here is cheap (the LLM answers, slower but right); a wrong hit is the worst
failure a voice interface has -- an action happens, silently, and it is the wrong
one. The thresholds are tuned for that asymmetry.

Anchors (actions.ANCHORS) take part in the competition but never win on their own:
only phrasings the user actually taught can trigger an action.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
THRESHOLD = 0.5

# 0.2 cleared the tuned and held-out paraphrase sets, but the live H1 run found a case
# those sets did not contain: "переключись на браузер" matched a learned "открой
# браузер" at 0.87 / margin 0.22. In a short phrase the object noun outweighs the verb,
# so the embedding encodes *what it is about* rather than *what to do* -- the same
# defect as the antonym case, along a different axis. Anchors did not fix it: the shared
# noun beats them too. 0.3 covers the observed failures at the cost of some coverage.
# The real fix is separating action from object, which is beyond this iteration.
MARGIN = 0.3

_ANCHOR = "__anchor__"


@dataclass
class SemanticHit:
    key: str | None        # the cache key that matched, None on a miss
    action: str | None     # best action even on a miss, for the HUD
    score: float
    margin: float
    reason: str            # hit | below-threshold | ambiguous | not-learned | not-ready


class SemanticIndex:
    def __init__(self, model_name: str = MODEL_NAME,
                 threshold: float = THRESHOLD, margin: float = MARGIN) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.margin = margin
        self._model = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._error = ""

        # Parallel lists: row i of _vecs belongs to _keys[i] / _acts[i].
        self._keys: list[str] = []
        self._acts: list[str] = []
        self._vecs = None
        self._pending: list[tuple[str, str]] = []

    # --- lifecycle ------------------------------------------------------------

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    @property
    def error(self) -> str:
        return self._error

    def load_async(self, entries: list[tuple[str, str]],
                   anchors: dict[str, list[str]]) -> None:
        """Import torch and the model off-thread; the fast path works meanwhile."""
        threading.Thread(target=self._load, args=(entries, anchors), daemon=True).start()

    def load(self, entries: list[tuple[str, str]], anchors: dict[str, list[str]]) -> None:
        self._load(entries, anchors)

    def _load(self, entries, anchors) -> None:
        try:
            from sentence_transformers import SentenceTransformer
            model = SentenceTransformer(self.model_name, device="cpu")
        except Exception as e:  # missing package or model: tier stays off
            self._error = f"{type(e).__name__}: {e}"
            return

        from .cache import normalize

        rows = [(_ANCHOR + normalize(p), a) for a, ps in anchors.items() for p in ps]
        rows += list(entries)

        with self._lock:
            self._model = model
            self._keys, self._acts, self._vecs = [], [], None
            self._append_locked(rows)
            if self._pending:
                self._append_locked(self._pending)
                self._pending.clear()
        self._ready.set()

    # --- mutation -------------------------------------------------------------

    def _encode(self, texts: list[str]):
        return self._model.encode(texts, normalize_embeddings=True,
                                  show_progress_bar=False)

    def _append_locked(self, rows: list[tuple[str, str]]) -> None:
        import numpy as np
        rows = [(k, a) for k, a in rows if k not in self._keys]
        if not rows:
            return
        texts = [k[len(_ANCHOR):] if k.startswith(_ANCHOR) else k for k, _ in rows]
        vecs = self._encode(texts)
        self._keys += [k for k, _ in rows]
        self._acts += [a for _, a in rows]
        self._vecs = vecs if self._vecs is None else np.vstack([self._vecs, vecs])

    def add(self, key: str, action: str) -> None:
        with self._lock:
            if self._model is None:
                self._pending.append((key, action))
                return
            if key in self._keys:  # re-learned with a different action
                self._remove_locked(key)
            self._append_locked([(key, action)])

    def remove(self, key: str) -> None:
        with self._lock:
            self._pending = [(k, a) for k, a in self._pending if k != key]
            if self._model is not None:
                self._remove_locked(key)

    def _remove_locked(self, key: str) -> None:
        import numpy as np
        if key not in self._keys:
            return
        i = self._keys.index(key)
        del self._keys[i]
        del self._acts[i]
        self._vecs = np.delete(self._vecs, i, axis=0) if len(self._keys) else None

    # --- query ----------------------------------------------------------------

    def query(self, key: str) -> SemanticHit:
        if not self.ready or not key:
            return SemanticHit(None, None, 0.0, 0.0, "not-ready")

        with self._lock:
            if self._vecs is None:
                return SemanticHit(None, None, 0.0, 0.0, "not-learned")
            sims = self._vecs @ self._encode([key])[0]
            keys, acts = list(self._keys), list(self._acts)

        # Best score per action over everything, and over user-taught rows only.
        best_any: dict[str, float] = {}
        best_user: dict[str, tuple[float, str]] = {}
        for k, a, s in zip(keys, acts, sims):
            s = float(s)
            if s > best_any.get(a, -1.0):
                best_any[a] = s
            if not k.startswith(_ANCHOR) and s > best_user.get(a, (-1.0, ""))[0]:
                best_user[a] = (s, k)

        ranked = sorted(best_any.items(), key=lambda kv: -kv[1])
        top_action = ranked[0][0]
        runner_up = ranked[1][1] if len(ranked) > 1 else 0.0

        if top_action not in best_user:
            # The nearest meaning is one the user has never taught.
            return SemanticHit(None, top_action, ranked[0][1],
                               ranked[0][1] - runner_up, "not-learned")

        score, hit_key = best_user[top_action]
        margin = score - runner_up
        if score < self.threshold:
            return SemanticHit(None, top_action, score, margin, "below-threshold")
        if margin < self.margin:
            return SemanticHit(None, top_action, score, margin, "ambiguous")
        return SemanticHit(hit_key, top_action, score, margin, "hit")
