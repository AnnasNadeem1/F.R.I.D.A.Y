# Jarvis desktop app launcher. Double-click it.
#
# Windows opens .pyw files with the system Python, which doesn't have Jarvis's
# packages (they live in .venv), so this relaunches itself with .venv's
# windowless Python when needed. Startup errors are shown in a message box,
# since there is no console to print them to.
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHONW = os.path.join(ROOT, ".venv", "Scripts", "pythonw.exe")


def _error_box(message: str):
    import ctypes
    ctypes.windll.user32.MessageBoxW(None, message, "Jarvis couldn't start", 0x10)


def _in_venv() -> bool:
    return os.path.normcase(os.path.abspath(sys.prefix)) == os.path.normcase(os.path.join(ROOT, ".venv"))


if not _in_venv():
    if not os.path.exists(VENV_PYTHONW):
        _error_box(f"Jarvis's Python environment is missing:\n{VENV_PYTHONW}\n\nSee README.md for setup.")
        sys.exit(1)
    subprocess.Popen([VENV_PYTHONW, os.path.abspath(__file__), *sys.argv[1:]], cwd=ROOT,
                     creationflags=subprocess.DETACHED_PROCESS)
    sys.exit(0)

sys.path.insert(0, ROOT)
os.chdir(ROOT)
try:
    from jarvis.gui import main
    sys.exit(main())
except SystemExit:
    raise
except BaseException as e:
    import traceback
    log = os.path.join(ROOT, "data", "jarvis-crash.log")
    os.makedirs(os.path.dirname(log), exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        traceback.print_exc(file=f)
    _error_box(f"{type(e).__name__}: {e}\n\nDetails were saved to {log}")
    sys.exit(1)
