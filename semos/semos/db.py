"""Схема и подключение. Одна база SQLite на всё — метаданные, чанки, FTS, журнал.

Две базы без общей транзакции рассинхронизируются при первом падении процесса
посреди переиндексации. Здесь этого класса ошибок нет по построению.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS files (
    id              INTEGER PRIMARY KEY,
    path            TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    ext             TEXT    NOT NULL,
    mtime           REAL    NOT NULL,
    size            INTEGER NOT NULL,
    content_indexed INTEGER NOT NULL DEFAULT 0,
    skip_reason     TEXT,
    indexed_at      REAL    NOT NULL,
    last_opened     REAL,
    open_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    file_id    INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    ord        INTEGER NOT NULL,
    text       TEXT    NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_id);

-- rowid == chunks.id
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts
    USING fts5(text_norm, tokenize='unicode61 remove_diacritics 2');

-- rowid == files.id
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
    USING fts5(name_norm, tokenize='unicode61 remove_diacritics 2');

-- Журнал выборов. Пишется с первого дня, используется начиная с M3.
CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY,
    ts             REAL NOT NULL,
    kind           TEXT NOT NULL,          -- query | choose | reject | open
    query          TEXT,
    shown          TEXT,                   -- JSON: список file_id в показанном порядке
    chosen_file_id INTEGER,
    rank_of_chosen INTEGER
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

-- Выученные имена. Наполняется начиная с M3.
CREATE TABLE IF NOT EXISTS aliases (
    term    TEXT    NOT NULL,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    weight  REAL    NOT NULL DEFAULT 1.0,
    PRIMARY KEY (term, file_id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
