"""On-screen overlay showing the whole chain: heard -> understood -> bound -> done.

This is not decoration. The literature names two abandonment causes for voice
interfaces, and this addresses the second one: a wrong mental model, where a miss gives
you no idea which link broke. Showing the route and the per-stage milliseconds makes
the system legible -- and E2 makes it necessary, because a 6-9s LLM path that looked
identical to a 1s cache hit would read as "it froze".

tkinter runs on the main thread (it must); worker threads push updates through a queue.
"""

from __future__ import annotations

import queue
import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable

BG = "#12141a"
FG = "#e8eaf0"
DIM = "#7d8596"
ACCENT_FAST = "#4ade80"   # cache hit
ACCENT_SLOW = "#fbbf24"   # LLM path
ACCENT_FAIL = "#f87171"
ACCENT_LIVE = "#60a5fa"   # recording


@dataclass
class HudState:
    status: str = ""
    utterance: str = ""
    route: str = ""
    detail: str = ""
    timing: str = ""
    accent: str = ACCENT_LIVE
    visible: bool = True


class Hud:
    def __init__(self, on_quit: Callable[[], None] | None = None) -> None:
        self._q: queue.Queue[Any] = queue.Queue()
        self.on_quit = on_quit
        self._hide_job: str | None = None

        self.root = tk.Tk()
        self.root.title("jarvis")
        self.root.overrideredirect(True)          # no title bar
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.94)
        self.root.configure(bg=BG)

        w, h = 760, 132
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh - h - 90}")

        pad = {"padx": 18, "anchor": "w"}

        self.bar = tk.Frame(self.root, bg=ACCENT_LIVE, height=3)
        self.bar.pack(fill="x", side="top")

        self.lbl_status = tk.Label(self.root, text="", bg=BG, fg=DIM,
                                   font=("Segoe UI", 9), justify="left")
        self.lbl_status.pack(**pad, pady=(10, 0))

        self.lbl_utter = tk.Label(self.root, text="", bg=BG, fg=FG,
                                  font=("Segoe UI", 15, "bold"), justify="left",
                                  wraplength=720)
        self.lbl_utter.pack(**pad, pady=(2, 0))

        self.lbl_detail = tk.Label(self.root, text="", bg=BG, fg=FG,
                                   font=("Segoe UI", 10), justify="left", wraplength=720)
        self.lbl_detail.pack(**pad, pady=(4, 0))

        self.lbl_timing = tk.Label(self.root, text="", bg=BG, fg=DIM,
                                   font=("Consolas", 8), justify="left")
        self.lbl_timing.pack(**pad, pady=(4, 0))

        self.root.bind("<Escape>", lambda _e: self.quit())
        self.root.after(40, self._pump)

    # --- called from worker threads -------------------------------------------

    def update(self, **kwargs) -> None:
        self._q.put(("update", kwargs))

    def hide_after(self, ms: int) -> None:
        self._q.put(("hide_after", ms))

    def quit(self) -> None:
        self._q.put(("quit", None))

    # --- main thread ----------------------------------------------------------

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "update":
                    self._apply(payload)
                elif kind == "hide_after":
                    self._schedule_hide(payload)
                elif kind == "quit":
                    if self.on_quit:
                        self.on_quit()
                    self.root.destroy()
                    return
        except queue.Empty:
            pass
        self.root.after(40, self._pump)

    def _apply(self, kw: dict) -> None:
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
            self._hide_job = None

        if "accent" in kw:
            self.bar.configure(bg=kw["accent"])
        if "status" in kw:
            self.lbl_status.configure(text=kw["status"])
        if "utterance" in kw:
            self.lbl_utter.configure(text=kw["utterance"])
        if "detail" in kw:
            self.lbl_detail.configure(text=kw["detail"])
        if "timing" in kw:
            self.lbl_timing.configure(text=kw["timing"])

        self.root.deiconify()
        self.root.attributes("-topmost", True)

    def _schedule_hide(self, ms: int) -> None:
        if self._hide_job is not None:
            self.root.after_cancel(self._hide_job)
        self._hide_job = self.root.after(ms, self.root.withdraw)

    def run(self) -> None:
        self.root.mainloop()
