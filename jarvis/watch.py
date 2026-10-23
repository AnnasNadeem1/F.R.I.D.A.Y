"""Distraction watch: which window is in front, and is it on the distraction list?

Only the foreground window's program name and title are read, on this PC, and only while a focus
session or study hours are on. They are compared with distractions.txt and never stored or sent anywhere."""
import ctypes
import logging
import re
from ctypes import wintypes

from .config import ROOT

log = logging.getLogger(__name__)

DISTRACTIONS_FILE = ROOT / "distractions.txt"

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_user32.GetForegroundWindow.restype = wintypes.HWND
_user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
_user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                 ctypes.POINTER(wintypes.DWORD)]
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def foreground() -> tuple[str, str]:
    """(program name, window title) of the window in front, e.g. ("chrome.exe", "YouTube - Google Chrome").
    Empty strings when nothing is (a locked screen, say)."""
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    n = _user32.GetWindowTextLengthW(hwnd)
    title = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, title, n + 1)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    program = ""
    handle = _kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if handle:
        try:
            size = wintypes.DWORD(520)
            path = ctypes.create_unicode_buffer(size.value)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                program = path.value.rsplit("\\", 1)[-1]
        finally:
            _kernel32.CloseHandle(handle)
    return program, title.value


class Distractions:
    """Keywords from distractions.txt, matched against the program name and window title.
    Re-read when the file changes, like pronunciations.txt."""

    def __init__(self, path=DISTRACTIONS_FILE):
        self.path = path
        self._mtime = None
        self._words: list[str] = []

    def _load(self):
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._words, self._mtime = [], None
            return
        if mtime != self._mtime:
            lines = (ln.strip().lower() for ln in self.path.read_text(encoding="utf-8").splitlines())
            self._words = [ln for ln in lines if ln and not ln.startswith("#")]
            self._mtime = mtime

    def match(self, program: str, title: str, allowed: tuple[str, ...] = ()) -> str | None:
        """The distraction keyword that matches (for a nag like "That's YouTube"), or None."""
        self._load()
        text = f"{program} {title}".lower()
        for word in self._words:
            if word in text and not any(a in text for a in allowed if a):
                return word
        return None


def friendly(word: str) -> str:
    """A keyword as it should be spoken: "youtube" -> "YouTube"."""
    special = {"youtube": "YouTube", "tiktok": "TikTok", "netflix": "Netflix", "reddit": "Reddit",
               "instagram": "Instagram", "facebook": "Facebook", "twitch": "Twitch", "steam": "Steam"}
    return special.get(word, re.sub(r"\.exe$", "", word).title())
