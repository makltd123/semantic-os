"""Сканирование корня и инкрементальная переиндексация.

Никакого файлового вотчера: diff по (path, mtime, size) при запуске занимает
миллисекунды, а ReadDirectoryChangesW на облачной папке даёт события от
синхронизации, а не от пользователя.
"""

from __future__ import annotations

import fnmatch
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import extract
from .textnorm import norm_filename, norm_text

# Не индексируем — секреты и служебное.
DENY_FILES = (
    ".env", ".env.*", "*.key", "*.pem", "*.pfx", "*.ppk", "*.kdbx",
    "id_rsa*", "id_ed25519*", "*.crt", "desktop.ini", "Thumbs.db", "*.lnk",
)
DENY_DIRS = {
    ".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "AppData", "$RECYCLE.BIN", "System Volume Information", ".idea", ".vscode",
}

# Признаки облачного плейсхолдера. Чтение такого файла инициирует скачивание,
# поэтому его содержимое не трогаем — индексируем только имя.
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000
_CLOUD_MASK = (
    _FILE_ATTRIBUTE_OFFLINE
    | _FILE_ATTRIBUTE_RECALL_ON_OPEN
    | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)


@dataclass
class IndexStats:
    scanned: int = 0
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    content: int = 0
    name_only: int = 0
    chunks: int = 0

    def __str__(self) -> str:
        return (
            f"просмотрено {self.scanned}, добавлено {self.added}, "
            f"обновлено {self.updated}, удалено {self.removed}, "
            f"без изменений {self.unchanged}\n"
            f"с содержимым {self.content}, только имя {self.name_only}, "
            f"чанков {self.chunks}"
        )


def _denied(name: str) -> bool:
    return any(fnmatch.fnmatch(name.lower(), pat.lower()) for pat in DENY_FILES)


def _is_cloud_placeholder(st: os.stat_result) -> bool:
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & _CLOUD_MASK)


def walk(root: Path):
    """Все файлы под root, кроме денилиста. Симлинки на директории не разворачиваем."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name in DENY_DIRS or e.name.startswith("."):
                        continue
                    stack.append(Path(e.path))
                elif e.is_file(follow_symlinks=False):
                    if _denied(e.name):
                        continue
                    yield Path(e.path), e.stat()
            except OSError:
                continue


def _replace_chunks(conn: sqlite3.Connection, file_id: int, text: str) -> int:
    old = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (file_id,))]
    if old:
        conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in old])
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
    n = 0
    for ord_, (start, end, body) in enumerate(extract.chunk(text)):
        cur = conn.execute(
            "INSERT INTO chunks(file_id, ord, text, char_start, char_end) VALUES (?,?,?,?,?)",
            (file_id, ord_, body, start, end),
        )
        conn.execute(
            "INSERT INTO chunks_fts(rowid, text_norm) VALUES (?, ?)",
            (cur.lastrowid, norm_text(body)),
        )
        n += 1
    return n


def _index_one(conn: sqlite3.Connection, path: Path, st: os.stat_result,
               now: float, stats: IndexStats) -> None:
    row = conn.execute(
        "SELECT id, mtime, size FROM files WHERE path = ?", (str(path),)
    ).fetchone()
    if row and row["mtime"] == st.st_mtime and row["size"] == st.st_size:
        stats.unchanged += 1
        return

    text: str | None = None
    skip_reason: str | None = None
    if _is_cloud_placeholder(st):
        skip_reason = "облачный плейсхолдер (не гидратируем)"
    else:
        try:
            text = extract.extract(path, st.st_size)
        except extract.ExtractError as e:
            skip_reason = str(e)

    if row:
        file_id = row["id"]
        conn.execute(
            "UPDATE files SET name=?, ext=?, mtime=?, size=?, content_indexed=?, "
            "skip_reason=?, indexed_at=? WHERE id=?",
            (path.name, path.suffix.lower(), st.st_mtime, st.st_size,
             int(text is not None), skip_reason, now, file_id),
        )
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (file_id,))
        stats.updated += 1
    else:
        cur = conn.execute(
            "INSERT INTO files(path, name, ext, mtime, size, content_indexed, "
            "skip_reason, indexed_at) VALUES (?,?,?,?,?,?,?,?)",
            (str(path), path.name, path.suffix.lower(), st.st_mtime, st.st_size,
             int(text is not None), skip_reason, now),
        )
        file_id = cur.lastrowid
        stats.added += 1

    # Имя файла индексируется всегда — это инвариант полноты.
    conn.execute(
        "INSERT INTO files_fts(rowid, name_norm) VALUES (?, ?)",
        (file_id, norm_filename(path.stem)),
    )

    if text is not None:
        stats.chunks += _replace_chunks(conn, file_id, text)
        stats.content += 1
    else:
        _replace_chunks(conn, file_id, "")
        stats.name_only += 1


def reindex(conn: sqlite3.Connection, root: Path, full: bool = False) -> IndexStats:
    now = time.time()
    stats = IndexStats()
    if full:
        conn.execute("DELETE FROM chunks_fts")
        conn.execute("DELETE FROM files_fts")
        conn.execute("DELETE FROM chunks")
        conn.execute("DELETE FROM files")

    seen: set[str] = set()
    for path, st in walk(root):
        stats.scanned += 1
        seen.add(str(path))
        _index_one(conn, path, st, now, stats)

    known = {r["path"]: r["id"] for r in conn.execute("SELECT id, path FROM files")}
    gone = [fid for p, fid in known.items() if p not in seen]
    for fid in gone:
        chunk_ids = [r[0] for r in conn.execute("SELECT id FROM chunks WHERE file_id = ?", (fid,))]
        conn.executemany("DELETE FROM chunks_fts WHERE rowid = ?", [(i,) for i in chunk_ids])
        conn.execute("DELETE FROM files_fts WHERE rowid = ?", (fid,))
        conn.execute("DELETE FROM files WHERE id = ?", (fid,))
        stats.removed += 1

    conn.commit()
    return stats
