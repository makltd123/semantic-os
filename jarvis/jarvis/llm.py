"""The slow path: resolve a novel phrase into an action via the `claude` CLI.

There is no API key on this machine, but the Claude Code CLI is authenticated, so we
run it headlessly. How it is launched matters a great deal (RESEARCH_LOG E2):

  * The native claude.exe is called directly. Going through the npm .cmd shim means
    cmd.exe, which truncates a multi-line prompt at the first newline and caps the
    command line at 8191 chars; and a shell=True timeout kills cmd but not the child,
    so the call overran its limit by 10s+.
  * The user part of the prompt goes over stdin for the same reasons.
  * --system-prompt replaces Claude Code's very large default prompt with ours.
  * MCP servers, tools and session persistence are all switched off -- each one is
    startup work that buys nothing for a single classification.
  * Extended thinking is disabled (MAX_THINKING_TOKENS=0). Haiku was spending 1.5-2.7s
    reasoning about a one-of-twelve pick; without it the call is ~4.5s and, more
    importantly, stops swinging between 5s and 7s.

`--bare` would be leaner still, but it never reads OAuth, and OAuth is the only
credential here.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any

from .actions import describe_registry
from .state import SystemState

TIMEOUT_SEC = 30
MODEL = "haiku"  # fastest available; the task is classification, not reasoning

_JSON = re.compile(r"\{.*\}", re.DOTALL)
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

SYSTEM = """Ты — слой разбора намерений в голосовой надстройке над Windows.
Ты не ассистент и не ведёшь диалог. Ты не используешь инструменты.

На вход: реплика пользователя по-русски, список действий и снимок состояния компьютера.
На выход: РОВНО один JSON-объект одной строкой. Без markdown, без ```, без пояснений.

Формат:
{"action": "<имя действия>", "args": {...}, "say": "<очень короткий ответ по-русски>"}

Если ни одно действие не подходит:
{"action": null, "args": {}, "say": "<коротко, чего не хватает>"}

Правила:
- Действие выбирай ТОЛЬКО из списка.
- СНАЧАЛА определи направление, потом действие. Тише / убавь / приглуши / потише /
  слишком громко → volume_down. Громче / прибавь / погромче / плохо слышно /
  ничего не слышно → volume_up. Путать противоположные действия недопустимо.
- Неоднозначность разрешай по состоянию системы.
- Не указывай в args цель, если действие само определяет её из состояния
  (volume_down/volume_up сами находят, что звучит).
- В args для say_state поле what — одно из: time, playing, windows, clipboard, volume.
- "say" — не длиннее шести слов, это произносится вслух."""


@dataclass
class LLMIntent:
    action: str | None
    args: dict[str, Any]
    say: str
    raw: str = ""


def find_claude() -> str | None:
    """Locate the native claude.exe, avoiding the cmd.exe shim."""
    override = os.environ.get("JARVIS_CLAUDE")
    if override and os.path.exists(override):
        return override

    appdata = os.environ.get("APPDATA", "")
    native = os.path.join(appdata, "npm", "node_modules", "@anthropic-ai",
                          "claude-code", "bin", "claude.exe")
    if os.path.exists(native):
        return native

    return shutil.which("claude.exe")


def build_user_prompt(utterance: str, st: SystemState) -> str:
    return (
        f"ДЕЙСТВИЯ:\n{describe_registry()}\n\n"
        f"СОСТОЯНИЕ:\n{st.describe()}\n\n"
        f"РЕПЛИКА: {utterance}"
    )


def build_cmd(exe: str, lean: bool = True) -> list[str]:
    cmd = [exe, "-p", "--model", MODEL, "--output-format", "text"]
    if lean:
        cmd += [
            "--system-prompt", SYSTEM,
            "--tools", "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
    return cmd


def _kill_tree(pid: int) -> None:
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=5, creationflags=_NO_WINDOW)
    except Exception:
        pass


def call(user_prompt: str, lean: bool = True, timeout: int = TIMEOUT_SEC) -> tuple[str, str]:
    """Run one completion. Returns (stdout, error). Raises nothing."""
    exe = find_claude()
    if not exe:
        return "", "claude.exe not found (set JARVIS_CLAUDE)"

    env = os.environ.copy()
    if lean:
        env["MAX_THINKING_TOKENS"] = "0"
    else:
        user_prompt = f"{SYSTEM}\n\n{user_prompt}"

    try:
        proc = subprocess.Popen(
            build_cmd(exe, lean),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # A neutral cwd keeps a project's CLAUDE.md out of the context.
            cwd=tempfile.gettempdir(),
            env=env,
            creationflags=_NO_WINDOW,
        )
    except Exception as e:
        return "", f"spawn failed: {e}"

    try:
        out, err = proc.communicate(user_prompt.encode("utf-8"), timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass
        return "", "timeout"

    text = out.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0 and not text:
        return "", err.decode("utf-8", errors="replace").strip()[:300] or f"exit {proc.returncode}"
    return text, ""


def parse(text: str) -> LLMIntent:
    m = _JSON.search(text or "")
    if not m:
        return LLMIntent(None, {}, "Модель ответила не тем", (text or "")[:300])
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return LLMIntent(None, {}, "Сломанный JSON", text[:300])

    action = data.get("action")
    args = data.get("args")
    return LLMIntent(
        action=action if isinstance(action, str) and action else None,
        args=args if isinstance(args, dict) else {},
        say=str(data.get("say") or ""),
        raw=text[:500],
    )


# --- Mistral backend ---------------------------------------------------------------
#
# A plain HTTPS call skips the ~1.3s CLI boot and ~0.6s process exit measured in E2b.
# The client is kept alive so repeat calls reuse the TLS connection.

MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
# ministral-3b: 0.67s median and 10/12 on held-out phrases -- same accuracy as the
# Claude CLI path at a sixth of the latency (E5/E6). mistral-small and -medium return
# 429 on this key's tier, so the small models are the whole menu anyway.
MISTRAL_MODEL = os.environ.get("JARVIS_MISTRAL_MODEL", "ministral-3b-latest")

_http = None


def _mistral_key() -> str:
    key = os.environ.get("MISTRAL_API_KEY", "")
    if key:
        return key
    # setx writes to the user environment, which an already-running shell may not
    # have picked up yet; read it straight from the registry as a fallback.
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            return str(winreg.QueryValueEx(k, "MISTRAL_API_KEY")[0])
    except OSError:
        return ""


def call_mistral(user_prompt: str, model: str | None = None,
                 timeout: float = 15.0) -> tuple[str, str]:
    global _http
    key = _mistral_key()
    if not key:
        return "", "MISTRAL_API_KEY not set"

    import httpx
    if _http is None:
        _http = httpx.Client(timeout=timeout, http2=False)

    body = {
        "model": model or MISTRAL_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0,
        "max_tokens": 120,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {key}"}
    try:
        r = _http.post(MISTRAL_URL, json=body, headers=headers)
        if r.status_code == 429:
            # The key's tier allows ~1 req/s. Voice turns are rarer than that, so a
            # single short retry covers the only realistic collision.
            import time
            time.sleep(1.1)
            r = _http.post(MISTRAL_URL, json=body, headers=headers)
    except httpx.TimeoutException:
        return "", "timeout"
    except Exception as e:
        return "", f"http error: {type(e).__name__}"

    if r.status_code != 200:
        return "", f"http {r.status_code}: {r.text[:200]}"
    try:
        return r.json()["choices"][0]["message"]["content"], ""
    except (KeyError, IndexError, ValueError):
        return "", "bad response shape"


def backend() -> str:
    """JARVIS_LLM=mistral|claude; default: Mistral when a key exists (it is faster)."""
    choice = os.environ.get("JARVIS_LLM", "").lower()
    if choice in ("mistral", "claude"):
        return choice
    return "mistral" if _mistral_key() else "claude"


def resolve(utterance: str, st: SystemState, timeout: int = TIMEOUT_SEC) -> LLMIntent:
    prompt = build_user_prompt(utterance, st)
    if backend() == "mistral":
        text, error = call_mistral(prompt)
    else:
        text, error = call(prompt, timeout=timeout)
    if error == "timeout":
        return LLMIntent(None, {}, "Не успела подумать", "timeout")
    if error:
        return LLMIntent(None, {}, "Не дозвалась до модели", error)
    return parse(text)
