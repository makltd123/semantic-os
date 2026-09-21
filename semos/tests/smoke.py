"""Сквозная проверка M0 на синтетическом корпусе.

Синтетика не заменяет замороженный набор из eval/queries.tsv — она проверяет,
что механика (извлечение, стемминг, FTS, слияние) вообще работает. Метрика
проекта измеряется только на реальных файлах и реальных формулировках.

Запуск:  python tests\\smoke.py
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from semos import db, evalrun, indexer  # noqa: E402
from semos.rank import rank  # noqa: E402

CORPUS: dict[str, str] = {
    "договор-аренды.docx": (
        "Договор найма жилого помещения\n\n"
        "Наймодатель передаёт нанимателю двухкомнатную квартиру по адресу "
        "улица Лесная, дом 14, на срок одиннадцать месяцев.\n\n"
        "Ежемесячная плата составляет сорок пять тысяч рублей. Залог — "
        "одна месячная плата, возвращается при выезде."
    ),
    "Встреча с Ильёй 13 мая.md": (
        "# Созвон по интеграции\n\n"
        "Обсуждали сроки. Илья просит перенести релиз на две недели, "
        "потому что не готов слой авторизации.\n\n"
        "## Решили\n\n"
        "Переносим. Я готовлю смету к пятнице, он — список блокеров.\n"
    ),
    "налоги-2024.txt": (
        "Налоговый вычет за обучение\n\n"
        "Подать декларацию до 30 апреля. Нужны: справка 2-НДФЛ, договор "
        "с университетом, копии платёжек.\n\n"
        "Остаток по имущественному вычету — сто двадцать тысяч, "
        "переносится на следующий год."
    ),
    "рецепт борща.md": (
        "# Борщ\n\n"
        "Свёклу натереть и тушить отдельно с томатной пастой и уксусом, "
        "иначе цвет уйдёт.\n\n"
        "Бульон варить на говяжьей грудинке два часа. Капусту закладывать "
        "в последнюю очередь, чтобы осталась хрусткой.\n"
    ),
    "Отчёт по проекту Ирис.txt": (
        "Итоги квартала по проекту Ирис\n\n"
        "Выкатили поиск и переписали индексацию. Скорость отклика упала "
        "с двух секунд до двухсот миллисекунд.\n\n"
        "Не успели: экспорт отчётов, права доступа."
    ),
    "фото-с-дачи-май.jpg": "",          # бинарник: находится только по имени
    "черновик письма в банк.md": (
        "Прошу пересмотреть ставку по действующему кредиту в связи со "
        "снижением ключевой ставки. Прикладываю справку о доходах.\n"
    ),
}

# запрос -> ожидаемый файл. Формулировки намеренно не повторяют имена файлов.
QUERIES: list[tuple[str, str]] = [
    ("та штука про съём квартиры", "договор-аренды.docx"),
    ("что решили на созвоне с Ильёй", "Встреча с Ильёй 13 мая.md"),
    ("сколько осталось вычета", "налоги-2024.txt"),
    ("как не потерять цвет у свёклы", "рецепт борща.md"),
    ("итоги квартала по ирису", "Отчёт по проекту Ирис.txt"),
    ("фотки с дачи", "фото-с-дачи-май.jpg"),
    ("письмо про снижение ставки", "черновик письма в банк.md"),
    ("переносили ли мы релиз", "Встреча с Ильёй 13 мая.md"),
]


def _write_docx(path: Path, text: str) -> None:
    paras = "".join(
        f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in text.split("\n\n") if p.strip()
    )
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{paras}</w:body></w:document>"
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Target="word/document.xml" Type="http://schemas.openxmlformats.org'
        '/officeDocument/2006/relationships/officeDocument"/></Relationships>'
    )
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)


def build_corpus(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, body in CORPUS.items():
        path = root / name
        if path.suffix == ".docx":
            _write_docx(path, body)
        elif path.suffix == ".jpg":
            path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)
        else:
            path.write_text(body, encoding="utf-8")
    # Файл в денилисте — не должен попасть в индекс.
    (root / ".env").write_text("SECRET=должно_быть_отфильтровано\n", encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="semos-smoke-"))
    root, dbp = tmp / "corpus", tmp / "semos.db"
    failures: list[str] = []
    try:
        build_corpus(root)
        conn = db.connect(dbp)
        db.init_schema(conn)
        stats = indexer.reindex(conn, root)
        print(stats, "\n")

        n_files = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
        if n_files != len(CORPUS):
            failures.append(f"файлов в индексе {n_files}, ожидалось {len(CORPUS)} (денилист?)")
        if stats.chunks == 0:
            failures.append("ни одного чанка — извлечение текста не работает")

        docx = conn.execute(
            "SELECT content_indexed c FROM files WHERE name LIKE '%.docx'"
        ).fetchone()
        if not docx or not docx["c"]:
            failures.append(".docx не разобран через zipfile+xml")

        now = 1_760_000_000.0  # фиксируем время: прогон должен быть воспроизводим
        print(f"{'запрос':42} {'поз.':>5}  выдача")
        print("-" * 96)
        for query, expected in QUERIES:
            cands = rank(conn, query, now=now, limit=5)
            pos = next((i for i, c in enumerate(cands, 1) if c.name == expected), None)
            mark = "  " if pos == 1 else ("~ " if pos and pos <= 3 else "! ")
            print(f"{mark}{query:40} {str(pos or '—'):>5}  {', '.join(c.name for c in cands[:3])}")
            if pos is None or pos > 3:
                failures.append(f"{query!r}: ждали {expected}, позиция {pos or 'нет в выдаче'}")

        print()
        cases = [evalrun.EvalCase(q, e) for q, e in QUERIES]
        print(evalrun.report(evalrun.run(conn, root, cases, now=now)))

        conn.close()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if failures:
        print(f"ПРОВАЛЕНО ({len(failures)}):")
        for f in failures:
            print("  ·", f)
        return 1
    print("Всё прошло.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
