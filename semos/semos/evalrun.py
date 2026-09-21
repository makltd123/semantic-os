"""Оффлайн-харнесс — единственное, что делает проект улучшаемым.

Без замороженного набора запросов любое изменение (чанкинг, веса, стеммер, модель)
нефальсифицируемо, и разработка превращается в обмен впечатлениями.

Формат eval/queries.tsv:
    запрос <TAB> ожидаемый_файл <TAB> комментарий(необязательно)

Ожидаемый файл — путь относительно корня индексации либо просто имя файла.
Строки, начинающиеся с #, и пустые игнорируются.
"""

from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .rank import rank


@dataclass
class EvalCase:
    query: str
    expected: str
    note: str = ""
    lineno: int = 0


@dataclass
class CaseResult:
    case: EvalCase
    position: int | None          # позиция ожидаемого файла в выдаче, с 1
    top: list[str]                # имена файлов в выдаче
    resolved: bool                # удалось ли сопоставить expected с индексом
    trace_pos: dict[str, int | None]


def load_cases(path: Path) -> list[EvalCase]:
    cases = []
    for i, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("\t") if p.strip()]
        if len(parts) < 2:
            raise ValueError(f"{path}:{i}: нужно минимум два поля через TAB, получено: {line!r}")
        cases.append(EvalCase(parts[0], parts[1], parts[2] if len(parts) > 2 else "", i))
    return cases


def _nfc(s: str) -> str:
    """Windows может отдавать кириллические имена в NFD (разложенной форме);
    строки, набранные вручную (в т.ч. в eval/queries.tsv), обычно NFC. Без
    нормализации сравнение молча ломается: файл есть, а совпадения — нет."""
    return unicodedata.normalize("NFC", s)


def _resolve(conn: sqlite3.Connection, root: Path, expected: str) -> int | None:
    """Ожидаемый файл -> file_id. Принимает и относительный путь, и голое имя."""
    want = _nfc(expected.replace("/", "\\").lower())
    full = _nfc(str((root / expected).resolve()).lower())
    for r in conn.execute("SELECT id, path, name FROM files"):
        p = _nfc(r["path"].lower())
        if p == full or p.endswith("\\" + want) or _nfc(r["name"].lower()) == want:
            return r["id"]
    return None


def run(conn: sqlite3.Connection, root: Path, cases: list[EvalCase],
        now: float, limit: int = 10) -> list[CaseResult]:
    results = []
    for case in cases:
        want_id = _resolve(conn, root, case.expected)
        trace: dict[str, list[int]] = {}
        cands = rank(conn, case.query, now=now, limit=limit, trace=trace)
        pos = None
        if want_id is not None:
            for i, c in enumerate(cands, start=1):
                if c.file_id == want_id:
                    pos = i
                    break
        trace_pos = {
            gen: (ids.index(want_id) + 1 if want_id in ids else None)
            for gen, ids in trace.items()
        }
        results.append(CaseResult(case, pos, [c.name for c in cands],
                                  want_id is not None, trace_pos))
    return results


def report(results: list[CaseResult]) -> str:
    total = len(results)
    if not total:
        return "Набор пуст — заполни eval/queries.tsv (см. комментарий в шапке файла)."

    unresolved = [r for r in results if not r.resolved]
    scored = [r for r in results if r.resolved]
    n = len(scored) or 1

    def recall(k: int) -> float:
        return sum(1 for r in scored if r.position and r.position <= k) / n

    mrr = sum(1.0 / r.position for r in scored if r.position) / n

    lines = ["=" * 68, f"Набор: {total} запросов, засчитано {len(scored)}", "=" * 68]

    fails = [r for r in scored if not r.position or r.position > 3]
    if fails:
        lines.append("")
        lines.append(f"Промахи мимо топ-3 ({len(fails)}):")
        for r in fails:
            where = "не найден" if r.position is None else f"позиция {r.position}"
            lines.append(f"  · {r.case.query!r}")
            lines.append(f"      ждали: {r.case.expected}  →  {where}")
            sig = ", ".join(
                f"{g}={p}" if p else f"{g}=—"
                for g, p in sorted(r.trace_pos.items())
            )
            lines.append(f"      сигнал по генераторам: {sig}")
            if r.top[:3]:
                lines.append(f"      выдали: {', '.join(r.top[:3])}")

    if unresolved:
        lines.append("")
        lines.append(f"Не сопоставлено с индексом ({len(unresolved)}) — опечатка в пути или файл не проиндексирован:")
        for r in unresolved:
            lines.append(f"  · строка {r.case.lineno}: {r.case.expected}")

    r1, r3, r5 = recall(1), recall(3), recall(5)
    lines += [
        "",
        "-" * 68,
        f"recall@1 = {r1:.0%}    recall@3 = {r3:.0%}    recall@5 = {r5:.0%}    MRR = {mrr:.3f}",
        "-" * 68,
    ]

    # Гейт M0 из ARCHITECTURE.md §8.
    if len(scored) < 25:
        lines.append(f"Гейт M0: набор мал ({len(scored)} < 25) — метрика пока не показательна.")
    elif r3 >= 0.80:
        lines.append("Гейт M0 ПРОЙДЕН (recall@3 ≥ 80%) без единого эмбеддинга.")
        lines.append("Векторный слой (M4) откладывается: сначала M1 — хоткей и эргономика.")
    else:
        lines.append(f"Гейт M0 не пройден (recall@3 = {r3:.0%} < 80%).")
        lines.append("Разбирать по блоку «промахи» выше: где именно теряется сигнал.")
    return "\n".join(lines)
