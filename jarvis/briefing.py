"""Daily briefing: weather, today's calendar, assignments, recent email and today's
reminders, gathered in parallel and summarised aloud.

On request ("good morning", "brief me") through the daily_briefing tool, and optionally
on a schedule (BRIEFING_TIME, e.g. 07:30): if the PC was off at that time, it's given once
when the assistant starts, until noon."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import Callable

from . import quiet
from .config import settings
from .tools import tool

log = logging.getLogger(__name__)

LAST_FILE = settings.data_dir / "briefing_last.txt"

# What the assistant is told when the briefing is triggered by the schedule or the button.
PROMPT = ("Give me my daily briefing. Call daily_briefing, then speak it as a short, natural run of at most "
          "eight sentences: the weather first, then today's calendar, then assignments (overdue ones, and what's "
          "due in the next few days), then anything important in my email, then my reminders. Skip any part that "
          "is empty or unavailable, refer to classes by short natural names without section codes or times, don't list more than three emails or assignments by name, and end with a "
          "one-line offer of help.")


def _calendar():
    from .google_tools import calendar_list_events

    r = calendar_list_events(days=1)
    return r if "error" not in r else {"unavailable": "calendar not connected"}


def _classroom():
    from .classroom import classroom_assignments

    r = classroom_assignments(days_ahead=3)
    if "error" in r:
        return {"unavailable": "Classroom not connected"}
    return {"total": r["total"],
            "overdue": [f"{a['title']} ({a['course']})" for a in r["assignments"] if a["overdue"]][:5],
            "due_soon": [f"{a['title']} ({a['course']}), due {a['due']}" for a in r["assignments"]
                         if not a["overdue"] and "no due date" not in a["due"]][:5]}


def _email():
    from .email_tools import email_list_unread

    r = email_list_unread(5)
    if "error" in r:
        return {"unavailable": "email not set up"}
    return {"recent_unread": [{"from": m["from"], "subject": m["subject"]} for m in r["unread"]]}


def _weather():
    from .web_tools import get_weather

    return get_weather(settings.briefing_location, 1)


def _reminders():
    from .reminders import list_reminders

    today = [r for r in list_reminders()["reminders"] if r["due"].startswith("today")]
    return {"today": today}


@tool("Gather everything for the user's daily briefing in one go: weather, today's calendar, Classroom "
      "assignments, recent unread email and today's reminders. Use when they say good morning, ask to be briefed, "
      "or ask what their day looks like. Parts that aren't available say so; just skip those.")
def daily_briefing():
    parts = {"weather": _weather, "calendar": _calendar, "assignments": _classroom, "email": _email,
             "reminders": _reminders}

    def run(item):
        name, fn = item
        try:
            return name, fn()
        except Exception as e:
            log.warning("briefing: %s failed: %s", name, e)
            return name, {"unavailable": "couldn't be fetched right now"}

    with ThreadPoolExecutor(max_workers=len(parts)) as pool:  # all at once: the slowest one sets the pace
        result = dict(pool.map(run, parts.items()))
    result["now"] = datetime.now().astimezone().strftime("%A %d %B, %H:%M")
    return result


# ---------------------------------------------------------------- schedule

def _parse_time(text: str):
    try:
        h, m = text.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


def _last_run() -> date | None:
    try:
        return date.fromisoformat(LAST_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


class BriefingScheduler:
    """Calls run() once a day at BRIEFING_TIME (or late the same morning if the PC was off)."""

    def __init__(self, run: Callable[[], None]):
        self.run = run
        threading.Thread(target=self._loop, daemon=True, name="briefing").start()

    def _due(self, now: datetime) -> bool:
        at = _parse_time(settings.briefing_time)
        if not at or _last_run() == now.date() or quiet.active():
            return False
        target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        return target <= now <= max(target + timedelta(hours=1),
                                    now.replace(hour=12, minute=0, second=0, microsecond=0))

    def _loop(self):
        time.sleep(45)  # let startup finish (and the first reminders speak) before anything else talks
        while True:
            try:
                if self._due(datetime.now().astimezone()):
                    settings.data_dir.mkdir(parents=True, exist_ok=True)
                    LAST_FILE.write_text(date.today().isoformat(), encoding="utf-8")  # mark first: never twice
                    self.run()
            except Exception:
                log.exception("scheduled briefing failed")
            time.sleep(30)
