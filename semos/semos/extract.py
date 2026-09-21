"""Извлечение текста и нарезка на чанки.

Качество извлечения определяет качество поиска сильнее, чем выбор модели.
На M0 — только stdlib: .txt/.md напрямую, .docx через zipfile+xml.
Всё остальное индексируется по имени (инвариант полноты: система не имеет
слепых пятен, иначе пользователь перестаёт ей доверять).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

try:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError
except ImportError:  # pypdf — единственная внешняя зависимость (см. requirements.txt)
    PdfReader = None
    PdfReadError = Exception

TEXT_EXT = {".txt", ".md", ".markdown", ".log", ".csv", ".json", ".yaml", ".yml", ".ini"}
DOCX_EXT = {".docx"}
PDF_EXT = {".pdf"}

MAX_EXTRACT_BYTES = 8 * 1024 * 1024
MIN_PDF_TEXT_CHARS = 100  # меньше — считаем сканом без текстового слоя

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_BLOCK_SPLIT = re.compile(r"\n[ \t]*\n+")
_HEADING = re.compile(r"^#{1,6}\s+\S", re.MULTILINE)


class ExtractError(Exception):
    pass


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "cp1251", "cp866"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _docx_text(path: Path) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as e:
        raise ExtractError(f"docx: {e}") from e
    root = ET.fromstring(xml)
    paras: list[str] = []
    # root.iter находит w:p в том числе внутри таблиц, так что текст таблиц
    # не теряется — теряется только их структура.
    for p in root.iter(_W + "p"):
        text = "".join(t.text or "" for t in p.iter(_W + "t")).strip()
        if text:
            paras.append(text)
    return "\n\n".join(paras)


def _pdf_text(path: Path) -> str:
    if PdfReader is None:
        raise ExtractError("pypdf не установлен (pip install -r requirements.txt)")
    try:
        reader = PdfReader(path)
        if reader.is_encrypted:
            # Пустой пароль — частый случай для PDF, "защищённых" только от
            # редактирования. Настоящий пароль означает, что читать не должны.
            try:
                reader.decrypt("")
            except Exception:
                raise ExtractError("зашифрован")
        pages = [p.extract_text() or "" for p in reader.pages]
    except PdfReadError as e:
        raise ExtractError(f"pdf повреждён: {e}") from e
    except Exception as e:
        raise ExtractError(f"pdf: {e}") from e
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if len(text) < MIN_PDF_TEXT_CHARS:
        # Меньше MIN_PDF_TEXT_CHARS символов — скорее всего скан без текстового
        # слоя. Не ошибка чтения, а честное отсутствие содержимого.
        raise ExtractError("скан без текстового слоя (или почти пустой)")
    return text


def extract(path: Path, size: int) -> str:
    """Текст файла или ExtractError. Пустая строка — валидный результат."""
    ext = path.suffix.lower()
    if size > MAX_EXTRACT_BYTES:
        raise ExtractError(f"больше {MAX_EXTRACT_BYTES // 1024 // 1024} МБ")
    try:
        if ext in TEXT_EXT:
            return _decode(path.read_bytes())
        if ext in DOCX_EXT:
            return _docx_text(path)
        if ext in PDF_EXT:
            return _pdf_text(path)
    except OSError as e:
        raise ExtractError(str(e)) from e
    raise ExtractError(f"тип {ext or '<без расширения>'} не поддержан")


def _blocks(text: str) -> list[tuple[int, int]]:
    """Спаны блоков: разбиение по пустым строкам и перед заголовками Markdown."""
    cuts = {0, len(text)}
    for m in _BLOCK_SPLIT.finditer(text):
        cuts.add(m.start())
        cuts.add(m.end())
    for m in _HEADING.finditer(text):
        cuts.add(m.start())
    ordered = sorted(cuts)
    spans = []
    for a, b in zip(ordered, ordered[1:]):
        if text[a:b].strip():
            spans.append((a, b))
    return spans


def chunk(text: str, target: int = 400, hard_max: int = 1200) -> list[tuple[int, int, str]]:
    """Нарезка на (char_start, char_end, text).

    Короткие блоки склеиваются до target символов, длинные режутся по hard_max.
    Смысловые границы (пустая строка, заголовок) имеют приоритет над длиной.
    """
    out: list[tuple[int, int, str]] = []
    buf_start: int | None = None
    buf_end = 0

    def flush() -> None:
        nonlocal buf_start, buf_end
        if buf_start is None:
            return
        body = text[buf_start:buf_end].strip()
        if body:
            out.append((buf_start, buf_end, body))
        buf_start = None

    for a, b in _blocks(text):
        if b - a > hard_max:
            flush()
            pos = a
            while pos < b:
                end = min(pos + hard_max, b)
                body = text[pos:end].strip()
                if body:
                    out.append((pos, end, body))
                pos = end
            continue
        if buf_start is None:
            buf_start, buf_end = a, b
        else:
            buf_end = b
        if buf_end - buf_start >= target:
            flush()
    flush()
    return out
