"""Windows integration: start with Windows (per-user Run key), a Start Menu
shortcut, and the paths the desktop app is launched with."""
import subprocess
import winreg
from pathlib import Path

from .config import ROOT, settings

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = settings.name  # shown in Task Manager > Startup apps
LEGACY_VALUES = ("Jarvis",)  # entries from before the assistant was renamed
SCRIPT = ROOT / "jarvis.pyw"
PROGRAMS = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
SHORTCUT = PROGRAMS / f"{settings.name}.lnk"


def pythonw() -> Path:
    """The venv's windowless Python, so no console window appears."""
    return ROOT / ".venv" / "Scripts" / "pythonw.exe"


def startup_command() -> str:
    return f'"{pythonw()}" "{SCRIPT}" --minimized'


def is_startup_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, VALUE)[0] == startup_command()
    except OSError:
        return False


def migrate_renamed(icon: Path):
    """After a rename: move a startup entry made under an old name, and replace the old
    Start Menu shortcut, so there's never a second copy launching or a stale icon."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as k:
        for old in LEGACY_VALUES:
            if old == VALUE:
                continue
            try:
                winreg.QueryValueEx(k, old)
            except FileNotFoundError:
                continue
            winreg.DeleteValue(k, old)
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, startup_command())
    for old in LEGACY_VALUES:
        old_lnk = PROGRAMS / f"{old}.lnk"
        if old != settings.name and old_lnk.exists():
            old_lnk.unlink()
            create_start_menu_shortcut(icon)


def set_startup(enabled: bool):
    """Per-user only (HKCU): no admin rights needed, and it's visible in Task Manager > Startup apps."""
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(k, VALUE)
            except FileNotFoundError:
                pass


def create_start_menu_shortcut(icon: Path):
    """Adds the assistant to the Start Menu so it can be launched (and pinned) like any app."""
    ps = (
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:J_LNK);"
        "$s.TargetPath = $env:J_TARGET; $s.Arguments = $env:J_ARGS; $s.WorkingDirectory = $env:J_DIR;"
        "$s.IconLocation = $env:J_ICON; $s.Description = 'Voice assistant'; $s.Save()"
    )
    env = {
        **__import__("os").environ,
        "J_LNK": str(SHORTCUT), "J_TARGET": str(pythonw()), "J_ARGS": f'"{SCRIPT}"',
        "J_DIR": str(ROOT), "J_ICON": str(icon),
    }
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], env=env, check=True,
                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
