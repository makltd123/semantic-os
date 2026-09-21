"""Резидентный процесс: держит SQLite-соединение и хоткей открытыми, крутит
Tk-мейнлуп. Один живой процесс вместо запуска скрипта на каждый вызов —
иначе загрузка (пока лёгкая на M1, тяжелее на M4 с эмбеддером) съедала бы
2–5 секунд НА КАЖДЫЙ вызов (ARCHITECTURE.md §7).
"""

from __future__ import annotations

import json
import queue
import sys
import time
from pathlib import Path

from . import db
from .actions import ActionDenied, execute
from .hotkey import MOD_ALT, MOD_CONTROL, HotkeyError, HotkeyListener
from .popup import Choice, Popup
from .rank import Candidate, rank

VK_J = 0x4A
DEFAULT_MODIFIERS = MOD_CONTROL | MOD_ALT
DEFAULT_VK = VK_J
DEFAULT_HOTKEY_LABEL = "Ctrl+Alt+J"

POLL_MS = 50


class Service:
    def __init__(self, conn, root: Path) -> None:
        self.conn = conn
        self.root = root
        self._last_query = ""
        self._last_shown: list[int] = []
        self.popup = Popup(on_query=self._on_query, on_choice=self._on_choice)
        self.hotkey = HotkeyListener(DEFAULT_MODIFIERS, DEFAULT_VK)

    # --- поиск ---

    def _on_query(self, text: str) -> list[Candidate]:
        self._last_query = text
        cands = rank(self.conn, text, now=time.time(), limit=5)
        self._last_shown = [c.file_id for c in cands]
        return cands

    # --- действие ---

    def _on_choice(self, choice: Choice) -> None:
        if choice.kind == "dismiss":
            self._log("reject" if self._last_query.strip() else "dismiss_empty")
            return

        c = choice.candidate
        rank_of_chosen = (
            self._last_shown.index(c.file_id) + 1 if c.file_id in self._last_shown else None
        )
        self._log("choose", chosen_file_id=c.file_id, rank_of_chosen=rank_of_chosen)

        try:
            execute(choice.kind, c.path, self.root)
        except ActionDenied as e:
            print(f"[semos] отклонено: {e}", file=sys.stderr)
            raise
        else:
            if choice.kind == "open":
                self.conn.execute(
                    "UPDATE files SET last_opened=?, open_count=open_count+1 WHERE id=?",
                    (time.time(), c.file_id),
                )
                self.conn.commit()

    def _log(self, kind: str, chosen_file_id: int | None = None,
              rank_of_chosen: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO events(ts, kind, query, shown, chosen_file_id, rank_of_chosen) "
            "VALUES (?,?,?,?,?,?)",
            (time.time(), kind, self._last_query, json.dumps(self._last_shown),
             chosen_file_id, rank_of_chosen),
        )
        self.conn.commit()

    # --- цикл ---

    def _poll_hotkey(self) -> None:
        try:
            while True:
                self.hotkey.events.get_nowait()
                self.popup.show()
        except queue.Empty:
            pass  # штатный путь — очередь пуста между опросами
        self.popup.schedule(POLL_MS, self._poll_hotkey)

    def run(self) -> None:
        try:
            self.hotkey.start()
        except HotkeyError as e:
            print(f"[semos] {e} ({DEFAULT_HOTKEY_LABEL})", file=sys.stderr)
            print("[semos] сервис остановлен — хоткей не зарегистрирован", file=sys.stderr)
            return
        print(f"[semos] запущен, хоткей {DEFAULT_HOTKEY_LABEL}, корень {self.root}")
        self.popup.schedule(POLL_MS, self._poll_hotkey)
        try:
            self.popup.mainloop()
        finally:
            self.hotkey.stop()


def main(root_override: str | None = None) -> int:
    from .cli import DB_PATH, load_config, get_root  # локальный импорт — без цикла

    cfg = load_config()
    if root_override:
        cfg["root"] = root_override
    root = get_root(cfg)
    if not root.exists():
        print(f"Корень не существует: {root}\nЗапусти: python -m semos init", file=sys.stderr)
        return 1

    conn = db.connect(DB_PATH)
    db.init_schema(conn)

    service = Service(conn, root)
    service.run()
    return 0
