"""Окно ввода — минимальный borderless Tkinter поверх всех окон.

stdlib, мгновенный старт. Не задача этого модуля быть красивым — задача
не отнимать у гипотезы "по смыслу лучше, чем по путям" ни одной лишней
миллисекунды между хоткеем и результатом (ARCHITECTURE.md §7).
"""

from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass

from .actions import ActionDenied
from .rank import Candidate, margin

# Правило арбитра (ARCHITECTURE.md §6): при большом отрыве действуем сразу,
# при малом — показываем список, ниже порога любой отрыв не считается сигналом.
MARGIN_HIGH = 0.008
MARGIN_LOW = 0.0015

BG = "#1e1e1e"
FG = "#e8e8e8"
ACCENT = "#5c9eff"
MUTED = "#8a8a8a"
FONT = ("Segoe UI", 13)
FONT_SMALL = ("Segoe UI", 10)


@dataclass
class Choice:
    """Что выбрал пользователь в окне."""
    kind: str  # "open" | "reveal" | "dismiss"
    candidate: Candidate | None


class Popup:
    """Один экземпляр окна на весь процесс — пересоздавать Tk-объект дороже,
    чем прятать/показывать существующий."""

    def __init__(self, on_query, on_choice) -> None:
        """
        on_query(text: str) -> list[Candidate]   — вызывается на каждое изменение ввода
        on_choice(Choice) -> None                 — вызывается при выборе/закрытии
        """
        self.on_query = on_query
        self.on_choice = on_choice
        self._cands: list[Candidate] = []
        self._build()
        self._hide()

    def _build(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", 0.97)

        w, h = 620, 360
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - w) // 2
        y = sh // 4
        self.root.geometry(f"{w}x{h}+{x}+{y}")

        pad = 14
        frame = tk.Frame(self.root, bg=BG, padx=pad, pady=pad)
        frame.pack(fill="both", expand=True)

        self.entry_var = tk.StringVar()
        self.entry_var.trace_add("write", lambda *_: self._on_type())
        self.entry = tk.Entry(
            frame, textvariable=self.entry_var, font=FONT, bg="#2a2a2a", fg=FG,
            insertbackground=FG, relief="flat", highlightthickness=1,
            highlightbackground="#3a3a3a", highlightcolor=ACCENT,
        )
        self.entry.pack(fill="x", ipady=8)

        self.list_frame = tk.Frame(frame, bg=BG)
        self.list_frame.pack(fill="both", expand=True, pady=(10, 0))

        self.hint = tk.Label(
            frame, text="Enter — открыть   Tab — показать в папке   Esc — закрыть",
            font=FONT_SMALL, bg=BG, fg=MUTED, anchor="w",
        )
        self.hint.pack(fill="x", pady=(8, 0))

        self.entry.bind("<Return>", lambda e: self._select(0, "open"))
        self.entry.bind("<Tab>", lambda e: self._select(0, "reveal") or "break")
        self.entry.bind("<Escape>", lambda e: self._dismiss())
        for i in range(1, 6):
            self.root.bind_all(str(i), self._digit_handler(i - 1), add=True)
        self.root.bind("<FocusOut>", lambda e: self._dismiss())

        self._row_widgets: list[tk.Frame] = []

    def _digit_handler(self, idx: int):
        def handler(event):
            if self.root.state() == "withdrawn":
                return
            # Цифры в самом поле ввода должны печататься как текст — реагируем
            # только когда фокус вне Entry (список результатов пуст быть не может).
            if self.root.focus_get() is self.entry:
                return
            self._select(idx, "open")
        return handler

    def _hide(self) -> None:
        self.root.withdraw()

    def show(self) -> None:
        self.entry_var.set("")
        self._render([])
        self.root.deiconify()
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(10, self.entry.focus_force)

    def _on_type(self) -> None:
        text = self.entry_var.get()
        self._cands = self.on_query(text) if text.strip() else []
        self._render(self._cands)

    def _render(self, cands: list[Candidate]) -> None:
        for w in self._row_widgets:
            w.destroy()
        self._row_widgets.clear()

        if not cands:
            lbl = tk.Label(
                self.list_frame, text="", bg=BG, fg=MUTED, font=FONT_SMALL, anchor="w",
            )
            lbl.pack(fill="x")
            self._row_widgets.append(lbl)
            return

        m = margin(cands)
        mode = "auto" if m >= MARGIN_HIGH else ("list" if m >= MARGIN_LOW else "list")
        if mode == "auto" and len(cands) > 0:
            note = tk.Label(
                self.list_frame, text="уверенно — Enter откроет первый результат",
                bg=BG, fg=ACCENT, font=FONT_SMALL, anchor="w",
            )
            note.pack(fill="x", pady=(0, 4))
            self._row_widgets.append(note)

        for i, c in enumerate(cands[:5]):
            row = tk.Frame(self.list_frame, bg=BG)
            row.pack(fill="x", pady=1)
            num = tk.Label(row, text=f"{i + 1}", bg=BG, fg=MUTED, font=FONT_SMALL, width=2)
            num.pack(side="left")
            flag = "" if c.content_indexed else "  [по имени]"
            name = tk.Label(
                row, text=c.name + flag, bg=BG,
                fg=FG if i == 0 else MUTED, font=FONT, anchor="w",
            )
            name.pack(side="left", fill="x", expand=True)
            self._row_widgets.append(row)

    def _select(self, idx: int, verb: str):
        if not self._cands or idx >= len(self._cands):
            return
        c = self._cands[idx]
        self._hide()
        try:
            self.on_choice(Choice(kind=verb, candidate=c))
        except ActionDenied:
            pass  # обработчик уже залогировал/сообщил — окно просто закрывается
        return "break"

    def _dismiss(self) -> None:
        if self.root.state() == "withdrawn":
            return
        self._hide()
        self.on_choice(Choice(kind="dismiss", candidate=None))

    def mainloop(self) -> None:
        self.root.mainloop()

    def schedule(self, delay_ms: int, fn) -> None:
        self.root.after(delay_ms, fn)
