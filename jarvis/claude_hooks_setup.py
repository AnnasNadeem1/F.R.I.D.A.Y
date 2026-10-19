"""Add or remove Jarvis's hooks in Claude Code's user settings
(~/.claude/settings.json), so every Claude Code session reports to Jarvis.
Backs the file up first and only touches entries that run our hook script."""
import json
import shutil
import sys
from pathlib import Path

from .config import ROOT

SETTINGS = Path.home() / ".claude" / "settings.json"
EVENTS = ("UserPromptSubmit", "Stop", "Notification")


def hook_command() -> str:
    # Claude waits for hooks, so start fast: the base interpreter (the venv's python.exe
    # is a launcher that spawns a second process) with -I -S, as the hook is stdlib-only.
    # Forward slashes and quotes work whether Claude runs hooks through bash or cmd.
    python = Path(getattr(sys, "_base_executable", sys.executable)).as_posix()
    script = (ROOT / "hooks" / "notify_jarvis.py").as_posix()
    return f'"{python}" -I -S "{script}"'


def _is_ours(entry: dict) -> bool:
    return any("notify_jarvis.py" in h.get("command", "") for h in entry.get("hooks", []))


def _load() -> dict:
    if not SETTINGS.exists():
        return {}
    return json.loads(SETTINGS.read_text(encoding="utf-8") or "{}")


def _save(settings: dict):
    if SETTINGS.exists():
        backup = SETTINGS.with_name("settings.json.bak-jarvis")
        shutil.copy2(SETTINGS, backup)
        print(f"Backed up {SETTINGS} -> {backup.name}")
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")


def install():
    settings = _load()
    hooks = settings.setdefault("hooks", {})
    cmd = hook_command()
    for event in EVENTS:
        entries = [e for e in hooks.get(event, []) if not _is_ours(e)]  # replace an older Jarvis entry
        entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
        hooks[event] = entries
    _save(settings)
    print(f"Installed Jarvis hooks for {', '.join(EVENTS)}.")
    print("New Claude Code sessions will report to Jarvis (already-open sessions may need a restart).")


def uninstall():
    settings = _load()
    hooks = settings.get("hooks", {})
    for event in list(hooks):
        hooks[event] = [e for e in hooks[event] if not _is_ours(e)]
        if not hooks[event]:
            del hooks[event]
    if not hooks:
        settings.pop("hooks", None)
    _save(settings)
    print("Removed Jarvis hooks from Claude Code settings.")
