"""The action layer: what jarvis can actually do to the machine.

Native APIs only -- no screenshots, no clicking. That is a deliberate scope choice
(RESEARCH_LOG "Развилки"): GUI-driving agents are UFO2's territory, they are slow and
brittle, and on two cores they are hopeless.

Note how several actions take no target: `volume_down` resolves *what* to quieten from
live state at call time. That is the thesis in miniature -- and it is also why the
intent cache stores the action, never its binding. A cached "потише" must quieten
whatever is playing *now*, not whatever was playing when it was first learned.
"""

from __future__ import annotations

import ctypes
import os
import subprocess
from ctypes import POINTER, cast
from dataclasses import dataclass, field
from typing import Callable

import win32con
import win32gui
from comtypes import CLSCTX_ALL
from pycaw.utils import AudioUtilities, IAudioEndpointVolume
from rapidfuzz import fuzz, process as fuzz_process

from .state import SystemState, snapshot

# --- result / spec ---------------------------------------------------------------


@dataclass
class ActionResult:
    ok: bool
    say: str              # short phrase for TTS -- long replies are an abandonment cause
    detail: str = ""      # fuller text for the HUD


@dataclass
class ActionSpec:
    name: str
    description: str
    params: dict[str, str] = field(default_factory=dict)
    fn: Callable[[dict, SystemState], ActionResult] = None  # type: ignore[assignment]


REGISTRY: dict[str, ActionSpec] = {}


def action(name: str, description: str, params: dict[str, str] | None = None):
    def deco(fn):
        REGISTRY[name] = ActionSpec(name, description, params or {}, fn)
        return fn
    return deco


# --- volume ----------------------------------------------------------------------


def _endpoint_volume():
    speakers = AudioUtilities.GetSpeakers()
    iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(iface, POINTER(IAudioEndpointVolume))


def _live_session(pid: int):
    """Re-fetch a session by pid: COM objects from an old snapshot may be stale."""
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.pid == pid:
                return s
    except Exception:
        pass
    return None


def _nudge_volume(delta: int, st: SystemState) -> ActionResult:
    """Quieten/raise whatever is actually audible, else the system.

    The binding happens here, at call time, against fresh state.
    """
    playing = st.playing
    if playing:
        target = max(playing, key=lambda a: a.peak)
        sess = _live_session(target.pid)
        if sess is not None:
            try:
                cur = sess.SimpleAudioVolume.GetMasterVolume()
                new = min(1.0, max(0.0, cur + delta / 100.0))
                sess.SimpleAudioVolume.SetMasterVolume(new, None)
                verb = "Тише" if delta < 0 else "Громче"
                return ActionResult(True, verb,
                                    f"{target.process}: {int(cur*100)}% -> {int(new*100)}%")
            except Exception:
                pass  # fall through to system volume

    try:
        vol = _endpoint_volume()
        cur = vol.GetMasterVolumeLevelScalar()
        new = min(1.0, max(0.0, cur + delta / 100.0))
        vol.SetMasterVolumeLevelScalar(new, None)
        verb = "Тише" if delta < 0 else "Громче"
        return ActionResult(True, verb, f"общая громкость: {int(cur*100)}% -> {int(new*100)}%")
    except Exception as e:
        return ActionResult(False, "Не вышло изменить громкость", str(e))


@action("volume_down",
        "Сделать тише. Сам определяет, что именно звучит сейчас, и убавляет именно это; "
        "если ничего не играет — убавляет общую громкость.",
        {"delta": "на сколько процентов убавить, по умолчанию 15"})
def volume_down(args: dict, st: SystemState) -> ActionResult:
    return _nudge_volume(-abs(int(args.get("delta", 15))), st)


@action("volume_up",
        "Сделать громче. Определяет текущий источник звука так же, как volume_down.",
        {"delta": "на сколько процентов прибавить, по умолчанию 15"})
def volume_up(args: dict, st: SystemState) -> ActionResult:
    return _nudge_volume(abs(int(args.get("delta", 15))), st)


@action("volume_set", "Выставить общую громкость в конкретное значение.",
        {"level": "уровень 0-100"})
def volume_set(args: dict, st: SystemState) -> ActionResult:
    try:
        level = max(0, min(100, int(args.get("level", 50))))
        _endpoint_volume().SetMasterVolumeLevelScalar(level / 100.0, None)
        return ActionResult(True, f"Громкость {level}", f"общая громкость -> {level}%")
    except Exception as e:
        return ActionResult(False, "Не вышло", str(e))


@action("mute", "Выключить или включить звук полностью.",
        {"on": "true — выключить звук, false — включить"})
def mute(args: dict, st: SystemState) -> ActionResult:
    try:
        on = args.get("on", True)
        if isinstance(on, str):
            on = on.lower() not in ("false", "0", "нет")
        _endpoint_volume().SetMute(bool(on), None)
        return ActionResult(True, "Звук выключен" if on else "Звук включён")
    except Exception as e:
        return ActionResult(False, "Не вышло", str(e))


# --- windows ---------------------------------------------------------------------


def _match_window(hint: str, st: SystemState):
    """Find the window a person means by a loose description."""
    if not st.windows:
        return None
    hint_l = (hint or "").lower().strip()
    if not hint_l:
        return None

    # A category word ("браузер", "мессенджер") beats fuzzy title matching.
    cat_words = {
        "браузер": "browser", "хром": "browser", "интернет": "browser",
        "мессенджер": "messenger", "телеграм": "messenger", "чат": "messenger",
        "музыка": "music", "плеер": "player", "проводник": "files",
        "редактор": "editor", "код": "editor",
    }
    for word, cat in cat_words.items():
        if word in hint_l:
            for w in st.windows:
                if w.category == cat:
                    return w

    choices = {i: f"{w.title} {w.process}".lower() for i, w in enumerate(st.windows)}
    best = fuzz_process.extractOne(hint_l, choices, scorer=fuzz.partial_ratio)
    if best and best[1] >= 60:
        return st.windows[best[2]]
    return None


def _bring_to_front(hwnd: int) -> bool:
    """SetForegroundWindow is refused for background processes; nudge it into working."""
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        try:
            # Briefly allow foreground changes from this process.
            ctypes.windll.user32.AllowSetForegroundWindow(-1)
        except Exception:
            pass
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.BringWindowToTop(hwnd)
            return True
        except Exception:
            return False


@action("window_focus", "Переключиться на окно по смыслу: 'браузер', 'телеграм', 'тот документ'.",
        {"hint": "описание окна словами пользователя"})
def window_focus(args: dict, st: SystemState) -> ActionResult:
    w = _match_window(str(args.get("hint", "")), st)
    if not w:
        return ActionResult(False, "Не нашла такое окно", f"hint={args.get('hint')!r}")
    ok = _bring_to_front(w.hwnd)
    return ActionResult(ok, "Готово" if ok else "Окно не выходит вперёд", w.title)


@action("window_close", "Закрыть окно по смыслу.", {"hint": "описание окна"})
def window_close(args: dict, st: SystemState) -> ActionResult:
    w = _match_window(str(args.get("hint", "")), st)
    if not w:
        return ActionResult(False, "Не нашла такое окно", f"hint={args.get('hint')!r}")
    try:
        win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)
        return ActionResult(True, "Закрыла", w.title)
    except Exception as e:
        return ActionResult(False, "Не вышло закрыть", str(e))


@action("minimize_all", "Свернуть все окна, показать рабочий стол.")
def minimize_all(args: dict, st: SystemState) -> ActionResult:
    try:
        # Shell.MinimizeAll via the shell COM object -- same as Win+D.
        import win32com.client
        win32com.client.Dispatch("Shell.Application").MinimizeAll()
        return ActionResult(True, "Свернула всё")
    except Exception as e:
        return ActionResult(False, "Не вышло", str(e))


# --- apps ------------------------------------------------------------------------

_APP_ALIASES = {
    "браузер": "chrome", "интернет": "chrome", "хром": "chrome",
    "почта": "outlook", "музыка": "spotify", "терминал": "wt",
    "калькулятор": "calc", "блокнот": "notepad", "проводник": "explorer",
    "настройки": "ms-settings:", "диспетчер задач": "taskmgr",
}

_DIRECT = {"calc", "notepad", "explorer", "taskmgr", "mspaint", "wt", "cmd", "control"}


def _start_menu_index() -> dict[str, str]:
    """Map lowercase shortcut name -> .lnk path, across both Start Menu roots."""
    roots = [
        os.path.join(os.environ.get("ProgramData", ""), r"Microsoft\Windows\Start Menu\Programs"),
        os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"),
    ]
    found: dict[str, str] = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk"):
                    found.setdefault(f[:-4].lower(), os.path.join(dirpath, f))
    return found


@action("app_launch", "Открыть приложение по названию или по смыслу ('браузер', 'музыка').",
        {"name": "название приложения словами пользователя"})
def app_launch(args: dict, st: SystemState) -> ActionResult:
    raw = str(args.get("name", "")).strip()
    if not raw:
        return ActionResult(False, "Не поняла, что открыть")

    name = _APP_ALIASES.get(raw.lower(), raw)

    if name.endswith(":"):  # ms-settings: and friends
        try:
            os.startfile(name)
            return ActionResult(True, "Открыла", name)
        except Exception as e:
            return ActionResult(False, "Не вышло", str(e))

    if name.lower() in _DIRECT:
        try:
            subprocess.Popen(name, shell=True)
            return ActionResult(True, "Открыла", name)
        except Exception as e:
            return ActionResult(False, "Не вышло", str(e))

    index = _start_menu_index()
    best = fuzz_process.extractOne(name.lower(), list(index.keys()), scorer=fuzz.WRatio)
    if best and best[1] >= 70:
        try:
            os.startfile(index[best[0]])
            return ActionResult(True, "Открыла", best[0])
        except Exception as e:
            return ActionResult(False, "Не вышло", str(e))

    try:  # last resort: let the shell resolve it
        subprocess.Popen(f'start "" "{name}"', shell=True)
        return ActionResult(True, "Пробую открыть", name)
    except Exception as e:
        return ActionResult(False, f"Не нашла {raw}", str(e))


@action("open_recent", "Открыть недавний файл по описанию: 'то, что вчера редактировал'.",
        {"hint": "описание файла словами пользователя"})
def open_recent(args: dict, st: SystemState) -> ActionResult:
    hint = str(args.get("hint", "")).strip().lower()
    if not st.recent:
        return ActionResult(False, "Список недавних пуст")
    if not hint:
        target = st.recent[0]
    else:
        best = fuzz_process.extractOne(hint, st.recent, scorer=fuzz.WRatio)
        if not best or best[1] < 55:
            return ActionResult(False, "Не нашла такой файл", f"hint={hint!r}")
        target = best[0]

    folder = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Recent")
    path = os.path.join(folder, target + ".lnk")
    try:
        os.startfile(path)  # opening the shortcut opens its target
        return ActionResult(True, "Открыла", target)
    except Exception as e:
        return ActionResult(False, "Не вышло открыть", str(e))


# --- system modes ----------------------------------------------------------------

ES_CONTINUOUS = 0x80000000
ES_DISPLAY_REQUIRED = 0x00000002
ES_SYSTEM_REQUIRED = 0x00000001


@action("keep_awake", "Не давать экрану гаснуть, или вернуть обычное поведение.",
        {"on": "true — не гасить экран, false — вернуть как было"})
def keep_awake(args: dict, st: SystemState) -> ActionResult:
    on = args.get("on", True)
    if isinstance(on, str):
        on = on.lower() not in ("false", "0", "нет")
    try:
        if on:
            ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_DISPLAY_REQUIRED | ES_SYSTEM_REQUIRED)
            return ActionResult(True, "Экран не будет гаснуть",
                                "держится, пока jarvis запущен")
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        return ActionResult(True, "Вернула как было")
    except Exception as e:
        return ActionResult(False, "Не вышло", str(e))


@action("focus_mode",
        "Режим фокуса: свернуть мессенджеры и заглушить их звук, чтобы не отвлекали.",
        {"on": "true — включить фокус, false — выключить"})
def focus_mode(args: dict, st: SystemState) -> ActionResult:
    on = args.get("on", True)
    if isinstance(on, str):
        on = on.lower() not in ("false", "0", "нет")

    if not on:
        try:
            for s in AudioUtilities.GetAllSessions():
                if s.Process and s.Process.name().lower() in _MESSENGERS:
                    s.SimpleAudioVolume.SetMute(0, None)
        except Exception:
            pass
        return ActionResult(True, "Фокус выключен")

    hidden = []
    for w in st.windows:
        if w.category == "messenger" or w.process.lower() in _MESSENGERS:
            try:
                win32gui.ShowWindow(w.hwnd, win32con.SW_MINIMIZE)
                hidden.append(w.process)
            except Exception:
                pass
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() in _MESSENGERS:
                s.SimpleAudioVolume.SetMute(1, None)
    except Exception:
        pass

    if hidden:
        return ActionResult(True, "Режим фокуса", "свернула: " + ", ".join(sorted(set(hidden))))
    return ActionResult(True, "Режим фокуса", "отвлекающих окон не нашлось")


_MESSENGERS = {
    "telegram.exe", "discord.exe", "slack.exe", "whatsapp.exe",
    "teams.exe", "ms-teams.exe", "skype.exe", "viber.exe",
}


@action("lock_screen", "Заблокировать компьютер.")
def lock_screen(args: dict, st: SystemState) -> ActionResult:
    try:
        ctypes.windll.user32.LockWorkStation()
        return ActionResult(True, "Блокирую")
    except Exception as e:
        return ActionResult(False, "Не вышло", str(e))


# --- questions about state -------------------------------------------------------


@action("say_state",
        "Ответить голосом на вопрос о текущем состоянии: сколько времени, что играет, "
        "что открыто, что в буфере, какая громкость.",
        {"what": "что именно спрашивают: time | playing | windows | clipboard | volume"})
def say_state(args: dict, st: SystemState) -> ActionResult:
    what = str(args.get("what", "")).lower().strip()

    if "time" in what or "врем" in what:
        return ActionResult(True, st.now.split(",")[0], st.now)

    if "play" in what or "звук" in what or "игра" in what or "музык" in what:
        playing = st.playing
        if playing:
            names = ", ".join(a.process for a in playing)
            return ActionResult(True, f"Играет {names}", names)
        return ActionResult(True, "Сейчас ничего не играет")

    if "clip" in what or "буфер" in what:
        if st.clipboard:
            head = st.clipboard[:150]
            return ActionResult(True, head, st.clipboard[:500])
        return ActionResult(True, "Буфер пуст")

    if "vol" in what or "громк" in what:
        return ActionResult(True, f"Громкость {st.master_volume} процентов")

    if "window" in what or "окн" in what or "откры" in what:
        if st.foreground:
            others = len(st.windows) - 1
            return ActionResult(True, f"Впереди {st.foreground.process}, всего окон {len(st.windows)}",
                                "\n".join(w.title for w in st.windows[:10]))
        return ActionResult(True, f"Открыто окон: {len(st.windows)}")

    # Unknown facet: say the most useful thing we have rather than an error.
    return ActionResult(True, f"Время {st.now.split(',')[0]}, окон {len(st.windows)}", st.describe())


# Anchor phrasings per action, for the semantic cache (see semantic.py). They never
# trigger an action themselves; they exist so that a half-filled cache still has a
# competitor for every action. Without them, a cache that has only learned
# volume_down would map "прибавь звук" onto it with a huge margin -- nothing to
# compare against. Deliberately disjoint from the phrasings in bench/bench_cache.py.
ANCHORS: dict[str, list[str]] = {
    "volume_down": ["уменьши громкость", "тише звук", "понизь звук"],
    "volume_up": ["увеличь громкость", "громче звук", "повысь звук"],
    "volume_set": ["поставь громкость на пятьдесят", "громкость на двадцать процентов"],
    "mute": ["отключи звук", "без звука"],
    # "переключись на браузер" sat closer to a learned "открой браузер" than to
    # "перейди в окно" -- same topic, different action. Anchors have to cover the
    # characteristic phrasing, not just the meaning (H1 run, RESEARCH_LOG).
    "window_focus": ["перейди в окно", "покажи мне окно",
                     "переключись на приложение", "вернись в программу"],
    "window_close": ["закрой окно", "закрой приложение"],
    "minimize_all": ["очисти экран от окон"],
    "app_launch": ["запусти программу", "открой приложение"],
    "open_recent": ["открой последний файл", "открой недавний документ"],
    "keep_awake": ["экран должен оставаться включенным", "запрети спящий режим"],
    "focus_mode": ["режим концентрации", "убери всё отвлекающее"],
    "lock_screen": ["заблокируй сеанс", "выйди на экран блокировки"],
    "say_state": ["какое сейчас время", "что сейчас звучит", "что в буфере обмена"],
}


def describe_registry() -> str:
    """Render the action list for the LLM prompt."""
    lines = []
    for spec in REGISTRY.values():
        params = ""
        if spec.params:
            params = " Параметры: " + "; ".join(f"{k} — {v}" for k, v in spec.params.items())
        lines.append(f"- {spec.name}: {spec.description}{params}")
    return "\n".join(lines)


def execute(name: str, args: dict, st: SystemState | None = None) -> ActionResult:
    spec = REGISTRY.get(name)
    if spec is None:
        return ActionResult(False, "Не знаю такого действия", name)
    if st is None:
        st = snapshot()
    try:
        return spec.fn(args or {}, st)
    except Exception as e:
        return ActionResult(False, "Действие сломалось", f"{type(e).__name__}: {e}")
