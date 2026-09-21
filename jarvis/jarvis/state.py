"""A snapshot of what the machine is doing right now.

This is the middle term of the project's thesis: an assistant maps *phrase -> function*,
which is why it needs exact commands; we want *phrase x state -> action*. "Сделай потише"
is unresolvable without knowing what is playing, so knowing that is the product.

Everything here must be cheap -- the snapshot sits in the hot path, before intent
resolution. Anything slow belongs in a background refresh instead.
"""

from __future__ import annotations

import os
import time
from ctypes import POINTER, cast
from dataclasses import dataclass, field

import psutil
import win32api
import win32clipboard
import win32con
import win32gui
import win32process
from comtypes import CLSCTX_ALL
from pycaw.api.endpointvolume import IAudioMeterInformation
from pycaw.utils import AudioUtilities, IAudioEndpointVolume

# Windows that exist but are not things a person would say "switch to".
_SHELL_NOISE = {
    "Program Manager",
    "Windows Input Experience",
    "Microsoft Text Input Application",
    "Setup",
}

# Rough grouping so intents can talk about kinds of things, not just process names.
_CATEGORY = {
    "msedge.exe": "browser", "chrome.exe": "browser", "firefox.exe": "browser",
    "opera.exe": "browser", "browser.exe": "browser", "yandex.exe": "browser",
    "telegram.exe": "messenger", "discord.exe": "messenger", "slack.exe": "messenger",
    "whatsapp.exe": "messenger", "teams.exe": "messenger", "skype.exe": "messenger",
    "spotify.exe": "music", "vlc.exe": "player", "wmplayer.exe": "player",
    "code.exe": "editor", "devenv.exe": "editor", "pycharm64.exe": "editor",
    "notepad.exe": "editor", "sublime_text.exe": "editor",
    "explorer.exe": "files", "winword.exe": "office", "excel.exe": "office",
    "powerpnt.exe": "office",
}


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    process: str
    category: str = ""
    foreground: bool = False


@dataclass
class AudioApp:
    pid: int
    process: str
    volume: float          # 0..1, this app's own level
    peak: float            # 0..1, instantaneous loudness -- >0 means actually audible
    category: str = ""

    @property
    def is_playing(self) -> bool:
        return self.peak > 0.0005


@dataclass
class SystemState:
    now: str = ""
    foreground: WindowInfo | None = None
    windows: list[WindowInfo] = field(default_factory=list)
    audio: list[AudioApp] = field(default_factory=list)
    master_volume: int = 0
    muted: bool = False
    clipboard: str = ""
    recent: list[str] = field(default_factory=list)

    @property
    def playing(self) -> list[AudioApp]:
        """Apps making sound this instant -- the referent of "потише"/"выключи звук"."""
        return [a for a in self.audio if a.is_playing]

    def describe(self) -> str:
        """Compact rendering for the LLM prompt.

        Kept terse on purpose: every token here is latency on the slow path.
        """
        lines = [f"Время: {self.now}"]

        if self.foreground:
            lines.append(f"Активное окно: {self.foreground.title} ({self.foreground.process})")

        if self.windows:
            others = [w for w in self.windows if not w.foreground][:10]
            if others:
                lines.append("Другие окна:")
                for w in others:
                    tag = f" [{w.category}]" if w.category else ""
                    lines.append(f"  - {w.title} ({w.process}){tag}")

        playing = self.playing
        if playing:
            lines.append("Сейчас звучит:")
            for a in playing:
                lines.append(f"  - {a.process} (громкость {int(a.volume * 100)}%)")
        elif self.audio:
            names = ", ".join(a.process for a in self.audio[:5])
            lines.append(f"Звук не играет. Аудио-сессии: {names}")

        lines.append(f"Общая громкость: {self.master_volume}%{' (выключен)' if self.muted else ''}")

        if self.clipboard:
            clip = self.clipboard[:120].replace("\n", " ")
            lines.append(f"В буфере обмена: {clip}")

        if self.recent:
            lines.append("Недавние файлы: " + ", ".join(self.recent[:8]))

        return "\n".join(lines)


# --- collection ------------------------------------------------------------------


def _process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except Exception:
        return ""


def _list_windows() -> tuple[list[WindowInfo], WindowInfo | None]:
    fg_hwnd = win32gui.GetForegroundWindow()
    out: list[WindowInfo] = []
    foreground: WindowInfo | None = None

    def cb(hwnd: int, _) -> None:
        nonlocal foreground
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title or title in _SHELL_NOISE:
            return
        # Tool windows are palettes and popups, not places you switch to.
        ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        if ex & win32con.WS_EX_TOOLWINDOW:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        proc = _process_name(pid)
        info = WindowInfo(
            hwnd=hwnd,
            title=title,
            process=proc,
            category=_CATEGORY.get(proc.lower(), ""),
            foreground=(hwnd == fg_hwnd),
        )
        if info.foreground:
            foreground = info
        out.append(info)

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return out, foreground


def _list_audio() -> list[AudioApp]:
    apps: list[AudioApp] = []
    try:
        sessions = AudioUtilities.GetAllSessions()
    except Exception:
        return apps

    for s in sessions:
        if not s.Process:
            continue
        try:
            name = s.Process.name()
            vol = s.SimpleAudioVolume.GetMasterVolume()
            try:
                meter = s._ctl.QueryInterface(IAudioMeterInformation)
                peak = meter.GetPeakValue()
            except Exception:
                peak = 0.0
            apps.append(AudioApp(
                pid=s.Process.pid,
                process=name,
                volume=float(vol),
                peak=float(peak),
                category=_CATEGORY.get(name.lower(), ""),
            ))
        except Exception:
            continue
    return apps


def get_master_volume() -> tuple[int, bool]:
    try:
        speakers = AudioUtilities.GetSpeakers()
        iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        vol = cast(iface, POINTER(IAudioEndpointVolume))
        return int(round(vol.GetMasterVolumeLevelScalar() * 100)), bool(vol.GetMute())
    except Exception:
        return 0, False


def _clipboard_text() -> str:
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                return str(win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT))
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return ""


def _recent_files(limit: int = 12) -> list[str]:
    """Names from the Windows Recent folder, newest first.

    Only the .lnk names -- resolving each shortcut to its target costs a shell call
    per file, which the hot path cannot afford. Names are enough to talk about.
    """
    folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Recent")
    if not os.path.isdir(folder):
        return []
    try:
        entries = []
        with os.scandir(folder) as it:
            for e in it:
                if e.is_file() and e.name.lower().endswith(".lnk"):
                    entries.append((e.stat().st_mtime, e.name[:-4]))
        entries.sort(reverse=True)
        return [name for _, name in entries[:limit]]
    except OSError:
        return []


def snapshot(include_recent: bool = True) -> SystemState:
    windows, foreground = _list_windows()
    master, muted = get_master_volume()
    return SystemState(
        now=time.strftime("%H:%M, %A %d.%m.%Y"),
        foreground=foreground,
        windows=windows,
        audio=_list_audio(),
        master_volume=master,
        muted=muted,
        clipboard=_clipboard_text(),
        recent=_recent_files() if include_recent else [],
    )
