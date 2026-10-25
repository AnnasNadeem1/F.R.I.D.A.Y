"""Basic PC control by voice: open apps and sites, volume and media keys, lock the screen, look at the
screen, the clipboard, and system status. Nothing here deletes, installs or changes settings."""
import ctypes
import io
import logging
import os
import re
import shutil
import subprocess
import time
import webbrowser
from ctypes import wintypes
from pathlib import Path

from .config import settings
from .tools import CONTEXT, tool

log = logging.getLogger(__name__)

ALIASES = {
    "calculator": "calc.exe", "notepad": "notepad.exe", "file explorer": "explorer.exe", "explorer": "explorer.exe",
    "files": "explorer.exe", "task manager": "taskmgr.exe", "paint": "mspaint.exe", "settings": "ms-settings:",
    "snipping tool": "snippingtool.exe", "terminal": "wt.exe", "command prompt": "cmd.exe",
}
_apps: dict = {"at": 0.0, "items": {}}


def _asked(pattern: str) -> bool:
    """Did the user themselves (not a web page or an email) say something like this this turn?"""
    return bool(re.search(pattern, CONTEXT.get("last_user_message", ""), re.I))


def _start_menu() -> dict[str, Path]:
    """Installed apps by lower-case name, from the Start Menu shortcuts (cached for 5 minutes)."""
    if time.time() - _apps["at"] > 300:
        found: dict[str, Path] = {}
        for base in (os.environ.get("ProgramData"), os.environ.get("APPDATA")):
            if not base:
                continue
            root = Path(base) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
            for lnk in root.rglob("*.lnk") if root.exists() else ():
                if "uninstall" not in lnk.stem.lower():
                    found.setdefault(lnk.stem.lower(), lnk)
        _apps.update(at=time.time(), items=found)
    return _apps["items"]


@tool(
    "Open an app on the user's PC by its everyday name ('Chrome', 'Spotify', 'VS Code', 'calculator', 'file explorer'). "
    "Only when they ask.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def open_app(name: str):
    key = name.strip().lower()
    target = ALIASES.get(key)
    label = name.strip()
    if not target:
        apps = _start_menu()
        matches = [n for n in apps if key == n] or sorted((n for n in apps if key in n), key=len)
        if not matches:
            return {"error": f"couldn't find an app called {name!r} in the Start Menu"}
        target, label = str(apps[matches[0]]), matches[0].title()
    os.startfile(target)
    return {"ok": True, "opened": label}


@tool(
    "Open a website in the user's browser. 'url' can be 'youtube.com' or a full https address. Only when they ask.",
    {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
)
def open_website(url: str):
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        if re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):  # javascript:, file:, ms-settings: ...
            return {"error": "only http and https addresses can be opened"}
        url = "https://" + url
    if not re.match(r"^https?://[^/\s]+\.[a-z]{2,}|^https?://localhost", url, re.I):
        return {"error": f"{url!r} doesn't look like a web address"}
    webbrowser.open(url)
    return {"ok": True, "opened": url}


_VK = {"play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD}


def _press(vk: int, times: int = 1):
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # key up
        time.sleep(0.004)


@tool(
    "Control music/video and the PC volume. 'action': play_pause, next, previous, volume_up, volume_down, mute "
    "(toggles), or set_volume. For volume_up/down 'amount' is a percentage (default 10); for set_volume it is the "
    "level 0-100.",
    {"type": "object", "properties": {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "volume_up",
                                                                           "volume_down", "mute", "set_volume"]},
                                      "amount": {"type": "integer"}}, "required": ["action"]},
)
def media_control(action: str, amount: int | None = None):
    if action == "set_volume":
        level = max(0, min(100, int(amount if amount is not None else 50)))
        _press(_VK["volume_down"], 50)  # every key press is 2%: go to zero, then up
        _press(_VK["volume_up"], level // 2)
        return {"ok": True, "volume": level}
    if action not in _VK:
        return {"error": f"unknown action {action!r}"}
    if action in ("volume_up", "volume_down"):
        pct = max(2, min(100, int(amount or 10)))
        _press(_VK[action], pct // 2)
        return {"ok": True, "changed_by_about": pct}
    _press(_VK[action])
    return {"ok": True, "done": action}


@tool("Lock the PC's screen now. Only when the user asks to lock it.")
def lock_screen():
    if not _asked(r"\block\b"):
        return {"error": "the user didn't ask to lock the screen"}
    ctypes.windll.user32.LockWorkStation()
    return {"ok": True}


@tool(
    "Look at the user's screen and answer a question about it ('what's on my screen', 'what does this error mean'). "
    "Takes one screenshot of the main display and sends it to Google's Gemini to describe, so only when they ask.",
    {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]},
)
def look_at_screen(question: str):
    if not _asked(r"screen|display|\bsee\b|look|showing|window|error|on my"):
        return {"error": "the user didn't ask about their screen"}
    if not settings.gemini_api_key:
        return {"error": "looking at the screen needs GEMINI_API_KEY"}
    from PIL import ImageGrab
    from google import genai
    from google.genai import types

    shot = ImageGrab.grab()
    shot.thumbnail((1600, 1000))
    buf = io.BytesIO()
    shot.convert("RGB").save(buf, "JPEG", quality=75)
    client = genai.Client(api_key=settings.gemini_api_key, http_options=types.HttpOptions(timeout=30_000))
    reply = client.models.generate_content(model=settings.gemini_model, contents=[
        types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg"),
        f"{question}\nAnswer in one or two short sentences that will be spoken aloud: no markdown, no lists."])
    return {"answer": (reply.text or "").strip() or "I couldn't make anything out."}


_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_u32.OpenClipboard.argtypes = [wintypes.HWND]
_u32.GetClipboardData.restype = wintypes.HANDLE
_u32.GetClipboardData.argtypes = [wintypes.UINT]
_u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_k32.GlobalAlloc.restype = wintypes.HANDLE
_k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalLock.argtypes = [wintypes.HANDLE]
_k32.GlobalUnlock.argtypes = [wintypes.HANDLE]


def _open_clipboard():
    for _ in range(10):  # another program may have it for a moment
        if _u32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise RuntimeError("the clipboard is busy")


@tool(
    "Read the clipboard text or put text on it. 'action' is read or write; for write give 'text'. Read only when the "
    "user asks ('what did I copy'); write for 'copy that'.",
    {"type": "object", "properties": {"action": {"type": "string", "enum": ["read", "write"]}, "text": {"type": "string"}},
     "required": ["action"]},
)
def clipboard(action: str, text: str = ""):
    if not _asked(r"clipboard|copi|paste|\bcopy\b"):
        return {"error": "the user didn't ask about the clipboard"}
    _open_clipboard()
    try:
        if action == "read":
            handle = _u32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return {"text": "", "note": "the clipboard has no text"}
            ptr = _k32.GlobalLock(handle)
            try:
                value = ctypes.wstring_at(ptr)
            finally:
                _k32.GlobalUnlock(handle)
            return {"text": value[:1500], "truncated": len(value) > 1500}
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        mem = _k32.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
        ptr = _k32.GlobalLock(mem)
        ctypes.memmove(ptr, data, len(data))
        _k32.GlobalUnlock(mem)
        _u32.EmptyClipboard()
        _u32.SetClipboardData(13, mem)
        return {"ok": True, "copied_characters": len(text or "")}
    finally:
        _u32.CloseClipboard()


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("percent", ctypes.c_ubyte),
                ("saver", ctypes.c_ubyte), ("life", wintypes.DWORD), ("full", wintypes.DWORD)]


class _MemStatus(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD), ("total", ctypes.c_ulonglong),
                ("avail", ctypes.c_ulonglong), ("tpage", ctypes.c_ulonglong), ("apage", ctypes.c_ulonglong),
                ("tvirt", ctypes.c_ulonglong), ("avirt", ctypes.c_ulonglong), ("ext", ctypes.c_ulonglong)]


@tool("The PC's status: battery, memory use, free disk space and uptime.")
def system_status():
    power = _PowerStatus()
    _k32.GetSystemPowerStatus(ctypes.byref(power))
    mem = _MemStatus()
    mem.length = ctypes.sizeof(mem)
    _k32.GlobalMemoryStatusEx(ctypes.byref(mem))
    disk = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    _k32.GetTickCount64.restype = ctypes.c_ulonglong
    hours = _k32.GetTickCount64() / 3_600_000
    battery = None if power.percent == 255 else {"percent": power.percent, "plugged_in": bool(power.ac)}
    return {"battery": battery or "no battery", "memory_used_percent": mem.load,
            "memory_free_gb": round(mem.avail / 2**30, 1), "disk_free_gb": round(disk.free / 2**30), "uptime_hours": round(hours, 1)}
