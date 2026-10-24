"""Quiet hours and quiet mode: when Friday should stay silent unless you speak to her.

QUIET_HOURS (e.g. 23:00-07:00) is a daily schedule; quiet mode is a switch you flip by voice ("go quiet for
two hours", "I'm back") or with the QUIET button in the window. While quiet, reminders, Claude alerts and
focus messages appear as notifications and chat notes with no speech, and the scheduled briefing, day plan
and evening check-in wait. Quiet hours don't silence a focus session you started yourself (late-night
study), but quiet mode does."""
import logging
import threading
from datetime import datetime, timedelta

from .config import settings
from .tools import tool

log = logging.getLogger(__name__)

MANUAL_FILE = settings.data_dir / "quiet_until.txt"
MAX_MINUTES = 12 * 60

_lock = threading.Lock()
_manual_until: datetime | None = None
_loaded = False


def parse_ranges(text: str) -> list[tuple[int, int]]:
    """"23:00-07:00, 14:00-15:30" -> [(1380, 420), (840, 930)] in minutes since midnight."""
    out = []
    for part in (text or "").split(","):
        try:
            a, b = (x.strip() for x in part.split("-"))
            (ah, am), (bh, bm) = (map(int, a.split(":")), map(int, b.split(":")))
            out.append((ah * 60 + am, bh * 60 + bm))
        except ValueError:
            continue
    return out


def in_ranges(text: str, now: datetime | None = None) -> bool:
    """Is now inside one of the ranges? A range may cross midnight (23:00-07:00)."""
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    return any((a <= minute < b) if a <= b else (minute >= a or minute < b) for a, b in parse_ranges(text))


def _load():
    global _manual_until, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        _manual_until = datetime.fromisoformat(MANUAL_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        _manual_until = None


def manual_until() -> datetime | None:
    """When quiet mode ends, or None if it's off."""
    with _lock:
        _load()
        global _manual_until
        if _manual_until and _manual_until <= datetime.now().astimezone():
            _manual_until = None
        return _manual_until


def set_manual(minutes: int | None):
    """Turn quiet mode on for `minutes`, or off with None. Survives a restart."""
    global _manual_until
    with _lock:
        _load()
        _manual_until = (datetime.now().astimezone() + timedelta(minutes=max(1, min(int(minutes), MAX_MINUTES)))
                         if minutes else None)
        try:
            settings.data_dir.mkdir(parents=True, exist_ok=True)
            if _manual_until:
                MANUAL_FILE.write_text(_manual_until.isoformat(timespec="seconds"), encoding="utf-8")
            else:
                MANUAL_FILE.unlink(missing_ok=True)
        except OSError:
            log.warning("couldn't save quiet mode", exc_info=True)


def hours_active(now: datetime | None = None) -> bool:
    return in_ranges(settings.quiet_hours, now)


def active(focus_override: bool = False) -> bool:
    """Should Friday stay silent now? `focus_override`: a focus session is the reason she'd speak, so
    quiet *hours* don't apply (quiet mode still does)."""
    if manual_until():
        return True
    return hours_active() and not focus_override


def remaining_text() -> str:
    """For the window: "1h 12m" while quiet mode is on, else ""."""
    until = manual_until()
    if not until:
        return ""
    m = max(1, int((until - datetime.now().astimezone()).total_seconds() // 60))
    return f"{m // 60}h {m % 60:02d}m" if m >= 60 else f"{m}m"


@tool(
    "Quiet mode: Friday stays silent (no spoken reminders, nags or briefings; they show as notifications). Use it "
    "when the user says 'go quiet', 'do not disturb', 'I'm in a call', and with off=true when they say 'I'm back' "
    "or 'you can talk again'. 'minutes' defaults to 120 (max 720).",
    {"type": "object", "properties": {"minutes": {"type": "integer"}, "off": {"type": "boolean"}}},
)
def quiet_mode(minutes: int = 120, off: bool = False):
    if off:
        set_manual(None)
        return {"ok": True, "quiet": False}
    set_manual(minutes or 120)
    until = manual_until()
    return {"ok": True, "quiet": True, "until": until.strftime("%I:%M %p").lstrip("0") if until else None,
            "note": "Friday will only speak when spoken to until then."}
