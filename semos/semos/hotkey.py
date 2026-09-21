"""Глобальный хоткей через ctypes — без внешних зависимостей и без пакета
`keyboard`, который требует глобального перехвата клавиатуры целиком.

Слушает в ОТДЕЛЬНОМ потоке со своей собственной очередью сообщений Win32.
Это не деталь реализации: если зарегистрировать хоткей и качать сообщения в
том же потоке, где крутится Tkinter, WM_HOTKEY конкурирует с насосом
сообщений Tk за одну очередь, и Tk может молча съесть сообщение первым —
DispatchMessage вызовется, а обработчика для потокового сообщения нет.
У каждого потока Windows — своя очередь сообщений, так что отдельный поток
снимает эту гонку полностью, а не смягчает её.
"""

from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

_HOTKEY_ID = 1


class HotkeyError(Exception):
    pass


class HotkeyListener:
    """`events` заполняется по одному элементу на каждое нажатие хоткея.
    Потребитель (обычно Tk через root.after()) вычитывает очередь из своего
    потока — очередь потокобезопасна, это единственный канал между потоками."""

    def __init__(self, modifiers: int, vk: int) -> None:
        self.modifiers = modifiers | MOD_NOREPEAT
        self.vk = vk
        self.events: queue.Queue[None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._ready = threading.Event()
        self._error: Exception | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="semos-hotkey")
        self._thread.start()
        self._ready.wait(timeout=5)
        if self._error is not None:
            raise self._error

    def stop(self) -> None:
        if self._thread_id is not None:
            # PostThreadMessage будит GetMessage в _run() и завершает поток.
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, _HOTKEY_ID, self.modifiers, self.vk):
            self._error = HotkeyError(
                "не удалось зарегистрировать хоткей — возможно, занят другим приложением"
            )
            self._ready.set()
            return
        self._ready.set()
        try:
            msg = wintypes.MSG()
            while True:
                ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ret <= 0:  # 0 = WM_QUIT, -1 = ошибка
                    break
                if msg.message == WM_HOTKEY:
                    self.events.put(None)
        finally:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
