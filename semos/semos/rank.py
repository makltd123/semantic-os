"""Ранжировщик — ядро системы.

Детерминированная функция (запрос, снимок индекса, now) -> ранжированный список.
Без сети. `now` передаётся параметром, а не читается из системных часов, иначе
прогон харнесса перестаёт быть воспроизводимым.

Устройство: несколько независимых дешёвых генераторов кандидатов, слитых через
Reciprocal Rank Fusion. RRF не требует калибровки шкал между генераторами и не
имеет весов, которые нужно подбирать вслепую, — сравниваются только позиции.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .textnorm import content_tokens, stem

RRF_K = 60

# Веса генераторов. Подбираются ТОЛЬКО прогоном харнесса, не на глаз.
DEFAULT_WEIGHTS: dict[str, float] = {
    "content": 1.0,   # bm25 по тексту чанков
    "name": 1.0,      # bm25 по имени файла
    "recency": 0.35,  # приоритет свежести
    "alias": 2.0,     # выученное имя (наполняется с M3)
}

RECENCY_POOL = 12       # сколько свежих файлов подмешивать
LEXICAL_POOL = 50       # глубина выборки лексических генераторов
MIN_PREFIX_LEN = 4      # с какой длины стема включать префиксное совпадение

# Насколько высоко файл должен стоять в content/name/alias, чтобы считаться
# "по-настоящему" найденным, а не случайно зацепившимся за один общий стем в
# длинном документе. Ниже этой границы — сигнал есть, но он слишком слабый,
# чтобы усиливать его бонусом за свежесть (см. STRONG_ANCHOR_RANK в fuse()).
STRONG_ANCHOR_RANK = 8


@dataclass
class Candidate:
    file_id: int
    path: str
    name: str
    mtime: float
    content_indexed: bool
    score: float = 0.0
    why: dict[str, int] = field(default_factory=dict)  # генератор -> позиция (с 1)


def build_match(query: str) -> str | None:
    """Запрос -> выражение FTS5 MATCH.

    Служебные слова выброшены: в OR-запросе каждый предлог тянет за собой
    десятки нерелевантных файлов. Стеммы короче MIN_PREFIX_LEN ищутся точно —
    префиксный поиск по двухбуквенному стему матчит половину корпуса.
    """
    terms = []
    for tok in content_tokens(query):
        s = stem(tok)
        if not s:
            continue
        terms.append(f'"{s}"*' if len(s) >= MIN_PREFIX_LEN else f'"{s}"')
    if not terms:
        return None
    return " OR ".join(terms)


def gen_content(conn: sqlite3.Connection, match: str, limit: int) -> list[int]:
    """Файлы по совпадению в тексте чанков.

    Агрегация chunk -> file обязательна: без неё одна длинная заметка на 50 чанков
    займёт всю выдачу. Балл файла = лучший чанк; при равенстве выигрывает файл с
    бо́льшим числом попаданий.
    """
    rows = conn.execute(
        "SELECT c.file_id AS fid, bm25(chunks_fts) AS s "
        "FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
        "WHERE chunks_fts MATCH ? ORDER BY s ASC LIMIT ?",
        (match, limit * 8),
    ).fetchall()
    best: dict[int, float] = {}
    hits: dict[int, int] = {}
    for r in rows:
        fid = r["fid"]
        hits[fid] = hits.get(fid, 0) + 1
        if fid not in best:
            best[fid] = r["s"]
    ordered = sorted(best, key=lambda f: (best[f], -hits[f]))
    return ordered[:limit]


def gen_name(conn: sqlite3.Connection, match: str, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT rowid AS fid FROM files_fts WHERE files_fts MATCH ? "
        "ORDER BY bm25(files_fts) ASC LIMIT ?",
        (match, limit),
    ).fetchall()
    return [r["fid"] for r in rows]


def gen_recency(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT id AS fid FROM files ORDER BY mtime DESC LIMIT ?", (limit,)
    ).fetchall()
    return [r["fid"] for r in rows]


def gen_alias(conn: sqlite3.Connection, query: str, limit: int) -> list[int]:
    """Выученные имена. До M3 таблица пуста и генератор молчит."""
    toks = content_tokens(query)
    terms = {stem(t) for t in toks}
    terms.add(" ".join(stem(t) for t in toks))
    terms.discard("")
    if not terms:
        return []
    marks = ",".join("?" * len(terms))
    rows = conn.execute(
        f"SELECT file_id AS fid, SUM(weight) AS w FROM aliases WHERE term IN ({marks}) "
        f"GROUP BY file_id ORDER BY w DESC LIMIT ?",
        (*terms, limit),
    ).fetchall()
    return [r["fid"] for r in rows]


def fuse(lists: dict[str, list[int]], weights: dict[str, float]
         ) -> tuple[dict[int, float], dict[int, dict[str, int]]]:
    """Reciprocal Rank Fusion: score = Σ w_g / (K + rank_g)."""
    scores: dict[int, float] = {}
    why: dict[int, dict[str, int]] = {}
    for gen, ids in lists.items():
        w = weights.get(gen, 1.0)
        for i, fid in enumerate(ids):
            rank = i + 1
            scores[fid] = scores.get(fid, 0.0) + w / (RRF_K + rank)
            why.setdefault(fid, {})[gen] = rank
    return scores, why


def rank(conn: sqlite3.Connection, query: str, now: float, limit: int = 10,
         weights: dict[str, float] | None = None,
         trace: dict[str, list[int]] | None = None) -> list[Candidate]:
    """`trace`, если передан, заполняется сырыми списками генераторов.

    Харнессу это нужно, чтобы на провалившемся запросе показать, на каком именно
    генераторе потерялся сигнал, — иначе отладка сводится к угадыванию.
    """
    weights = weights or DEFAULT_WEIGHTS
    match = build_match(query)

    lists: dict[str, list[int]] = {}
    if match:
        lists["content"] = gen_content(conn, match, LEXICAL_POOL)
        lists["name"] = gen_name(conn, match, LEXICAL_POOL)
    lists["alias"] = gen_alias(conn, query, LEXICAL_POOL)
    lists["recency"] = gen_recency(conn, RECENCY_POOL)

    # Свежесть только переупорядочивает то, что уверенно нашли остальные
    # генераторы, и не вбрасывает свои кандидаты — иначе выдачу забьют
    # недавние, но нерелевантные файлы. Если содержательного сигнала нет
    # вовсе, отдаём свежесть как есть.
    #
    # Порог STRONG_ANCHOR_RANK — намеренно жёстче, чем просто "встретился
    # где-нибудь в списке": найдено на прогоне eval, что длинный документ
    # почти неизбежно зацепляет один из OR-склеенных стемов запроса на
    # 10–20-м месте своего же списка. Такое слабое совпадение не должно
    # получать тот же бонус за свежесть, что и уверенное попадание в топ —
    # иначе слабый сигнал + бонус за дату обгоняют единственный, но точный
    # сигнал (например top-1 по имени файла) у настоящего ответа.
    strong = {
        fid
        for g in ("content", "name", "alias")
        for rank_, fid in enumerate(lists.get(g, ()), start=1)
        if rank_ <= STRONG_ANCHOR_RANK
    }
    weak_anchored = {fid for g in ("content", "name", "alias") for fid in lists.get(g, ())}
    if strong:
        lists["recency"] = [f for f in lists["recency"] if f in strong]
    elif weak_anchored:
        # Есть только слабые совпадения — свежесть их не усиливает, но и не
        # тонет: сигнал уже есть, просто без бонуса за дату.
        lists["recency"] = []

    if trace is not None:
        trace.clear()
        trace.update(lists)

    scores, why = fuse(lists, weights)
    if not scores:
        return []

    top = sorted(scores, key=lambda f: (-scores[f], f))[:limit]
    marks = ",".join("?" * len(top))
    rows = {
        r["id"]: r
        for r in conn.execute(
            f"SELECT id, path, name, mtime, content_indexed FROM files WHERE id IN ({marks})",
            top,
        )
    }
    out = []
    for fid in top:
        r = rows.get(fid)
        if r is None:
            continue
        out.append(Candidate(
            file_id=fid, path=r["path"], name=r["name"], mtime=r["mtime"],
            content_indexed=bool(r["content_indexed"]),
            score=scores[fid], why=why.get(fid, {}),
        ))
    return out


def margin(cands: list[Candidate]) -> float:
    """Отрыв первого кандидата от второго — вход для правила арбитра (M1+)."""
    if not cands:
        return 0.0
    if len(cands) == 1:
        return cands[0].score
    return cands[0].score - cands[1].score
