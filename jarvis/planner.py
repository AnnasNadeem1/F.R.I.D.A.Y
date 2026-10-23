"""The daily plan and the evening check-in.

Morning: everything Friday knows about today (calendar, deadlines, to-dos, reminders, how yesterday went)
goes to the model, which speaks a short ordered plan; the plan is saved. Evening: today's plan is set
against what actually happened (focus minutes, distractions, finished to-dos) and Friday asks how it went.
Either can be asked for any time, and each can run by itself at PLAN_TIME / CHECKIN_TIME."""
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable

from .config import settings
from .store import db
from .tools import tool

log = logging.getLogger(__name__)

PLAN_PROMPT = ("Plan my day. First call plan_my_day. Then decide the plan and call save_plan with its 3 to 6 short "
               "items BEFORE you say anything. Then speak the plan, at most seven sentences: deadlines first, then "
               "the to-dos that matter, with times where my calendar leaves gaps, and focus blocks (about 45 "
               "minutes each) to reach my study goal. If I missed things yesterday, say so in one line. Refer to "
               "classes by short natural names. End by asking if the plan works for me.")
CHECKIN_PROMPT = ("Evening check-in. Call evening_checkin, then in at most five sentences tell me honestly how today "
                  "went against the plan and my study goal: a brief nod for real wins, the gaps said plainly without "
                  "a lecture, and what's due next. Then ask one question: what did I get done, and what should move "
                  "to tomorrow. When I answer, tick off finished to-dos with todo_done and add leftovers with todo_add.")


def _today() -> str:
    return date.today().isoformat()


def _classroom(days: int):
    from .classroom import classroom_assignments

    r = classroom_assignments(days_ahead=days)
    if "error" in r:
        return {"unavailable": "Classroom not connected"}
    return {"overdue": [f"{a['title']} ({a['course']})" for a in r["assignments"] if a["overdue"]][:6],
            "due": [f"{a['title']} ({a['course']}), due {a['due']}" for a in r["assignments"]
                    if not a["overdue"] and "no due date" not in a["due"]][:8]}


def _calendar(offset_days: int):
    from .google_tools import calendar_list_events

    start = (datetime.now().astimezone() + timedelta(days=offset_days)).date().isoformat() + "T00:00:00"
    r = calendar_list_events(start_date=start, days=1)
    return r if "error" not in r else {"unavailable": "calendar not connected"}


def _plan_for(day: str):
    with db() as c:
        row = c.execute("SELECT items FROM plans WHERE day = ?", (day,)).fetchone()
    return json.loads(row["items"]) if row else None


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log.warning("planner: %s failed: %s", getattr(fn, "__name__", fn), e)
        return {"unavailable": "couldn't be fetched right now"}


def _focus():
    from .focus import MANAGER

    return MANAGER.stats()


@tool("Gather what's needed to plan the user's day: calendar, Classroom deadlines, open to-dos, today's reminders, "
      "yesterday's plan and focus time, and their study goal. Use when they ask you to plan or organise their day or "
      "what to work on. Decide the plan, call save_plan, and only then speak it.")
def plan_my_day():
    from .reminders import list_reminders
    from .tasks import todo_list

    yesterday = (date.today() - timedelta(days=1)).isoformat()
    return {"now": datetime.now().astimezone().strftime("%A %d %B, %H:%M"),
            "calendar_today": _safe(_calendar, 0),
            "assignments": _safe(_classroom, 3),
            "todos": _safe(todo_list),
            "reminders_today": [r for r in list_reminders()["reminders"] if r["due"].startswith("today")],
            "yesterdays_plan": _plan_for(yesterday),
            "focus": _safe(_focus),
            "study_goal_minutes_per_day": settings.study_goal_minutes}


@tool("Save today's plan (call it before you speak the plan). 'items' are 3 to 6 short items, e.g. 'Professional Practices "
      "assignment, 10 to 11', so the evening check-in can compare against them.",
      {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "string"}}}, "required": ["items"]})
def save_plan(items: list):
    items = [str(i).strip() for i in items if str(i).strip()][:8]
    if not items:
        return {"error": "no plan items given"}
    with db() as c:
        c.execute("INSERT OR REPLACE INTO plans (day, items) VALUES (?, ?)", (_today(), json.dumps(items)))
    return {"ok": True, "saved": len(items)}


@tool("Gather what's needed for the evening check-in: today's plan, focus minutes against the goal, distractions, "
      "finished and open to-dos, what's due next, and tomorrow's calendar. Use when they ask how their day went or "
      "to wrap up the day.")
def evening_checkin():
    from .focus import MANAGER
    from .tasks import todo_list

    stats = _safe(_focus)
    return {"now": datetime.now().astimezone().strftime("%A %d %B, %H:%M"),
            "todays_plan": _plan_for(_today()),
            "focus_minutes_today": stats.get("today_minutes") if isinstance(stats, dict) else None,
            "distractions_caught_today": stats.get("distractions_today") if isinstance(stats, dict) else None,
            "streak_days": stats.get("streak_days") if isinstance(stats, dict) else None,
            "study_goal_minutes": settings.study_goal_minutes,
            "session_running": MANAGER.session is not None,
            "todos": _safe(todo_list, True),
            "assignments_due_next_two_days": _safe(_classroom, 2),
            "calendar_tomorrow": _safe(_calendar, 1)}


# ---------------------------------------------------------------- schedule

def _parse_time(text: str):
    try:
        h, m = text.strip().split(":")
        return int(h), int(m)
    except (ValueError, AttributeError):
        return None


class DailyScheduler:
    """Calls run() once a day at the time returned by get_time() ("HH:MM"), or late the same day if the PC was
    off then, until `until_hour`. `name` keeps one marker file per schedule, so each fires once a day."""

    def __init__(self, name: str, get_time: Callable[[], str], run: Callable[[], None], until_hour: int):
        self.name, self.get_time, self.run, self.until_hour = name, get_time, run, until_hour
        self.marker = settings.data_dir / f"{name}_last.txt"
        threading.Thread(target=self._loop, daemon=True, name=f"daily-{name}").start()

    def _last(self) -> date | None:
        try:
            return date.fromisoformat(self.marker.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _due(self, now: datetime) -> bool:
        at = _parse_time(self.get_time())
        if not at or self._last() == now.date():
            return False
        target = now.replace(hour=at[0], minute=at[1], second=0, microsecond=0)
        limit = now.replace(hour=min(self.until_hour, 23), minute=59 if self.until_hour >= 23 else 0, second=0, microsecond=0)
        return target <= now <= max(target + timedelta(hours=1), limit)

    def _loop(self):
        time.sleep(60)  # let startup finish (and the first reminders and any briefing speak) first
        while True:
            try:
                if self._due(datetime.now().astimezone()):
                    settings.data_dir.mkdir(parents=True, exist_ok=True)
                    self.marker.write_text(date.today().isoformat(), encoding="utf-8")  # mark first: never twice
                    self.run()
            except Exception:
                log.exception("scheduled %s failed", self.name)
            time.sleep(30)
