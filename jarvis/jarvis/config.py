"""Paths and defaults."""

from __future__ import annotations

import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

CACHE_PATH = os.path.join(DATA_DIR, "intent_cache.json")
TRACE_PATH = os.path.join(DATA_DIR, "turns.jsonl")

STT_MODEL = "base"      # per E1: tiny mangles intent-bearing words in Russian
HOTKEY = "ctrl_r"       # hold to talk
TTS_VOICE = "Irina"
SEMANTIC = True         # embedding tier of the cache (E4); off => exact+fuzzy only

HUD_LINGER_MS = 6000    # how long a result stays on screen
