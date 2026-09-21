"""CLI для M0. На M1 это станет тонким клиентом к резидентному процессу."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import db, evalrun, indexer
from .rank import margin, rank

PROJECT = Path(__file__).resolve().parent.parent

# База НЕ лежит в проекте: проект находится в OneDrive, а SQLite в режиме WAL под
# файловой синхронизацией — это постоянная перезаливка и риск повреждения.
DATA_DIR = Path(
    os.environ.get("SEMOS_DATA") or os.path.expandvars(r"%LOCALAPPDATA%\SemanticOS")
)
DB_PATH = DATA_DIR / "semos.db"
CONFIG_PATH = DATA_DIR / "config.json"
EVAL_PATH = PROJECT / "eval" / "queries.tsv"

DEFAULT_ROOT = Path(os.path.expandvars(r"%USERPROFILE%\SemanticOS\test-notes"))


def _setup_console() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def load_config() -> dict:
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {"root": str(DEFAULT_ROOT)}


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def get_root(cfg: dict) -> Path:
    return Path(cfg["root"]).expanduser()


def cmd_init(args) -> int:
    cfg = load_config()
    if args.root:
        cfg["root"] = str(Path(args.root).expanduser().resolve())
    save_config(cfg)
    root = get_root(cfg)
    root.mkdir(parents=True, exist_ok=True)
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    db.set_meta(conn, "root", str(root))
    conn.commit()
    print(f"База:  {DB_PATH}")
    print(f"Корень: {root}")
    print()
    print("Дальше:")
    print(f"  1. Положи в корень 10–20 РЕАЛЬНЫХ файлов, которыми пользуешься.")
    print(f"  2. python -m semos index")
    print(f"  3. Заполни {EVAL_PATH.relative_to(PROJECT)} — 25–30 пар «запрос → файл».")
    print("     Формулируй запросы так, как спросил бы вслух, НЕ подглядывая в имена файлов.")
    print("  4. python -m semos eval")
    return 0


def cmd_index(args) -> int:
    cfg = load_config()
    root = get_root(cfg)
    if not root.exists():
        print(f"Корень не существует: {root}\nЗапусти: python -m semos init", file=sys.stderr)
        return 1
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    db.set_meta(conn, "root", str(root))
    t0 = time.perf_counter()
    stats = indexer.reindex(conn, root, full=args.full)
    dt = time.perf_counter() - t0
    print(f"Корень: {root}")
    print(stats)
    print(f"за {dt:.2f} с")
    skipped = conn.execute(
        "SELECT ext, COUNT(*) n, MIN(skip_reason) r FROM files "
        "WHERE content_indexed = 0 GROUP BY ext ORDER BY n DESC"
    ).fetchall()
    if skipped:
        print("\nБез содержимого (находятся только по имени):")
        for r in skipped:
            print(f"  {r['ext'] or '<без расширения>':12} {r['n']:4}  {r['r'] or ''}")
    return 0


def _fmt(cands, root: Path) -> str:
    if not cands:
        return "Ничего не найдено."
    lines = []
    for i, c in enumerate(cands, start=1):
        try:
            shown = Path(c.path).relative_to(root)
        except ValueError:
            shown = Path(c.path)
        flag = "" if c.content_indexed else "  [только имя]"
        why = " ".join(f"{g}:{p}" for g, p in sorted(c.why.items()))
        lines.append(f"{i}. {shown}{flag}")
        lines.append(f"     score={c.score:.4f}  {why}")
    return "\n".join(lines)


def cmd_find(args) -> int:
    cfg = load_config()
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    t0 = time.perf_counter()
    cands = rank(conn, args.query, now=time.time(), limit=args.n)
    dt = (time.perf_counter() - t0) * 1000
    print(_fmt(cands, get_root(cfg)))
    print(f"\n{dt:.1f} мс, отрыв top1 от top2: {margin(cands):.4f}")
    return 0


def cmd_eval(args) -> int:
    cfg = load_config()
    path = Path(args.file) if args.file else EVAL_PATH
    if not path.exists():
        print(f"Нет набора: {path}", file=sys.stderr)
        return 1
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    cases = evalrun.load_cases(path)
    now = args.now if args.now else time.time()
    results = evalrun.run(conn, get_root(cfg), cases, now=now, limit=args.n)
    print(evalrun.report(results))
    return 0


def cmd_stats(args) -> int:
    cfg = load_config()
    conn = db.connect(DB_PATH)
    db.init_schema(conn)
    files = conn.execute("SELECT COUNT(*) n FROM files").fetchone()["n"]
    withc = conn.execute("SELECT COUNT(*) n FROM files WHERE content_indexed=1").fetchone()["n"]
    chunks = conn.execute("SELECT COUNT(*) n FROM chunks").fetchone()["n"]
    events = conn.execute("SELECT COUNT(*) n FROM events").fetchone()["n"]
    print(f"Корень:   {get_root(cfg)}")
    print(f"База:     {DB_PATH} ({DB_PATH.stat().st_size / 1024:.0f} КБ)")
    print(f"Файлов:   {files} (с содержимым {withc}, только имя {files - withc})")
    print(f"Чанков:   {chunks}")
    print(f"Событий:  {events}")
    top = conn.execute(
        "SELECT ext, COUNT(*) n FROM files GROUP BY ext ORDER BY n DESC LIMIT 10"
    ).fetchall()
    if top:
        print("Типы:     " + ", ".join(f"{r['ext'] or '?'}×{r['n']}" for r in top))
    return 0


def cmd_serve(args) -> int:
    from . import service
    return service.main()


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    p = argparse.ArgumentParser(prog="semos", description="Semantic OS — M0")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="создать базу и корень индексации")
    s.add_argument("--root", help="папка для индексации")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("index", help="просканировать корень")
    s.add_argument("--full", action="store_true", help="перестроить индекс с нуля")
    s.set_defaults(func=cmd_index)

    s = sub.add_parser("find", help="поиск")
    s.add_argument("query")
    s.add_argument("-n", type=int, default=5)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser("eval", help="прогнать харнесс")
    s.add_argument("--file")
    s.add_argument("-n", type=int, default=10)
    s.add_argument("--now", type=float, help="зафиксировать время для воспроизводимости")
    s.set_defaults(func=cmd_eval)

    s = sub.add_parser("stats", help="состояние индекса")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("serve", help="резидентный процесс: хоткей + окно ввода")
    s.set_defaults(func=cmd_serve)

    args = p.parse_args(argv)
    return args.func(args)
