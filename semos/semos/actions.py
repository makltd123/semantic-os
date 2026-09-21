"""Исполнитель действий — граница между вероятностным ранжированием и
детерминированным вызовом ОС (ARCHITECTURE.md §5).

Ранжировщик выбирает ИНДЕКС в уже одобренном списке кандидатов — никогда путь,
имя или команду напрямую. Этот модуль — тот самый детерминированный слой,
который решает, что с выбранным файлом можно сделать.

Матрица (глагол, класс объекта) существует с M1, а не откладывается до
"слоя безопасности" на M3: open() впервые появляется здесь же, и открывать
произвольный файл, найденный вероятностным ранжировщиком, без этой проверки
с первого дня — значит превращать "разрешено только чтение" в фикцию.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# whitelist по классам объектов. Любое расширение, не входящее ни в один
# класс, попадает в "прочее" — только reveal.
_DOCUMENT_EXT = {
    ".txt", ".md", ".markdown", ".docx", ".pdf", ".xlsx", ".pptx", ".rtf",
    ".odt", ".csv", ".json", ".yaml", ".yml", ".log",
}
_MEDIA_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg",
    ".mp3", ".wav", ".flac", ".mp4", ".webm", ".avi", ".mov",
}
# Исполняемое — то, что os.startfile() может ЗАПУСТИТЬ, а не открыть в
# просмотрщике. .lnk и .url — ярлыки: открывают то, на что указывают, в
# обход всех проверок ниже, поэтому они здесь, а не в "прочем".
_EXECUTABLE_EXT = {
    ".exe", ".bat", ".cmd", ".com", ".lnk", ".pif", ".ps1", ".vbs", ".vbe",
    ".js", ".jse", ".wsf", ".wsh", ".msi", ".msix", ".reg", ".scr", ".hta",
    ".url", ".apk", ".application", ".gadget", ".cpl", ".jar",
}

_VERBS_BY_CLASS: dict[str, frozenset[str]] = {
    "document": frozenset({"open", "reveal", "preview"}),
    "media": frozenset({"open", "reveal"}),
    "executable": frozenset({"reveal"}),
    "other": frozenset({"reveal"}),
}


class ActionDenied(Exception):
    """Действие отклонено детерминированным слоем. Не перехватывать молча —
    это сигнал пользователю, не внутренняя деталь."""


def _object_class(ext: str) -> str:
    ext = ext.lower()
    if ext in _EXECUTABLE_EXT:
        return "executable"
    if ext in _DOCUMENT_EXT:
        return "document"
    if ext in _MEDIA_EXT:
        return "media"
    return "other"


def resolve_safe(candidate_path: str, root: Path) -> Path:
    """Путь кандидата -> реальный путь внутри root, или ActionDenied.

    Разыменовывает symlink/junction перед проверкой границы: сравнение
    исходной строки с корнем ничего не стоит, если внутри корня лежит junction
    на C:\\Windows. Регистр и разделители нормализуются — Windows
    регистронезависима.
    """
    real = Path(os.path.realpath(candidate_path))
    real_root = Path(os.path.realpath(root))
    try:
        real.relative_to(real_root)
    except ValueError:
        raise ActionDenied(f"путь вне корня индексации: {candidate_path}")
    if not real.exists():
        raise ActionDenied(f"файл не существует: {real}")
    return real


@dataclass
class ActionResult:
    verb: str
    path: Path
    object_class: str


def check(verb: str, candidate_path: str, root: Path) -> ActionResult:
    """Проверка БЕЗ побочных эффектов — можно вызвать перед подтверждением у
    пользователя, не рискуя ничего запустить."""
    real = resolve_safe(candidate_path, root)
    cls = _object_class(real.suffix)
    if verb not in _VERBS_BY_CLASS[cls]:
        raise ActionDenied(
            f"действие {verb!r} недоступно для класса {cls!r} "
            f"({real.suffix or 'без расширения'}); доступно: {sorted(_VERBS_BY_CLASS[cls])}"
        )
    return ActionResult(verb=verb, path=real, object_class=cls)


def reveal(path: Path) -> None:
    """Показать в Проводнике, ничего не запуская. Безопасно для любого файла,
    прошедшего resolve_safe — используется как дефолт для класса 'other'."""
    subprocess.run(["explorer", "/select,", str(path)], check=False)


def open_file(path: Path) -> None:
    """Открыть в ассоциированной программе. Вызывается ТОЛЬКО после check()."""
    os.startfile(str(path))  # noqa: S606 — намеренно, после явной проверки


def execute(verb: str, candidate_path: str, root: Path) -> ActionResult:
    """check() + реальный вызов ОС. Единственная функция модуля с побочным
    эффектом на файловую систему/процессы."""
    result = check(verb, candidate_path, root)
    if result.verb == "open":
        open_file(result.path)
    elif result.verb == "reveal":
        reveal(result.path)
    elif result.verb == "preview":
        # M1: превью не реализовано, откатываемся на reveal — безопасно.
        reveal(result.path)
    else:  # pragma: no cover — недостижимо: verb уже проверен в check()
        raise ActionDenied(f"неизвестное действие: {verb!r}")
    return result
