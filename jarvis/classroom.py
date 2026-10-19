"""Google Classroom: what's assigned, when it's due, whether it's handed in, plus
automatic reminders before each unfinished assignment is due.

It has its own Google login (token_classroom.json), separate from Calendar, because
Classroom is often on a school account. Read-only permissions: Friday can see
your classes and work, and can't change or submit anything.

Setup: enable the "Google Classroom API" in the same Google Cloud project as
Calendar, add the school account as a test user if it's different, then
Settings -> CONNECT GOOGLE CLASSROOM (or python -m jarvis --classroom-login).
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone

from .config import ROOT, settings
from .store import db
from .tools import tool

log = logging.getLogger(__name__)

TOKEN = ROOT / "token_classroom.json"
SCOPES = [
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    # Your own work and its status. (Google reports the older name, coursework.me.readonly, as this one.)
    "https://www.googleapis.com/auth/classroom.student-submissions.me.readonly",
]
NOT_SET_UP = ("Google Classroom isn't connected yet. Tell the user to open Settings and press "
              "CONNECT GOOGLE CLASSROOM (or run: python -m jarvis --classroom-login).")
DONE_STATES = {"TURNED_IN", "RETURNED"}
SYNC_MINUTES = 15

_service = None
_lock = threading.Lock()


class ClassroomNotSetUp(Exception):
    pass


def _credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN.exists():
        raise ClassroomNotSetUp(NOT_SET_UP)
    creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
        else:
            raise ClassroomNotSetUp(NOT_SET_UP)
    return creds


def login():
    """One-time consent in the browser. Pick the account your Classroom is on."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    from .google_tools import CREDENTIALS

    if not CREDENTIALS.exists():
        raise SystemExit(f"Missing {CREDENTIALS}. See README.md, 'Connect Google Calendar and Gmail'.")
    # Google sometimes answers with renamed-but-equivalent permissions; by default the OAuth
    # library treats that as an error and throws the approved login away.
    os.environ["OAUTHLIB_RELAX_TOKEN_SCOPE"] = "1"
    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=True, prompt="select_account consent")
    TOKEN.write_text(creds.to_json())
    global _service
    _service = None
    print(f"Google Classroom connected. Token saved to {TOKEN.name}.")


def connected() -> bool:
    return TOKEN.exists()


def _api():
    global _service
    with _lock:
        if _service is None:
            from googleapiclient.discovery import build

            _service = build("classroom", "v1", credentials=_credentials(), cache_discovery=False)
        return _service


def _pages(request_fn, key: str) -> list:
    out, token = [], None
    while True:
        resp = request_fn(token).execute()
        out += resp.get(key, [])
        token = resp.get("nextPageToken")
        if not token:
            return out


def _due(work: dict) -> datetime | None:
    """Classroom stores due dates in UTC; no time means end of that day."""
    d = work.get("dueDate")
    if not d:
        return None
    t = work.get("dueTime") or {}
    if not t:
        return datetime(d["year"], d["month"], d["day"], 23, 59, tzinfo=timezone.utc).astimezone()
    return datetime(d["year"], d["month"], d["day"], t.get("hours", 0), t.get("minutes", 0),
                    tzinfo=timezone.utc).astimezone()


_cache: dict = {"at": 0.0, "work": None}
CACHE_SECONDS = 900  # matches the background sync, so most questions are answered from cache


def fetch_work(fresh: bool = False) -> list[dict]:
    """Every assignment in your active classes, with due time (local) and your status.
    Cached for 5 minutes: with many classes this is dozens of API calls."""
    if not fresh and _cache["work"] is not None and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["work"]
    work = _fetch_work()
    _cache.update(at=time.time(), work=work)
    return work


_local = threading.local()


def _thread_api():
    """Google's client objects aren't thread-safe, so each worker thread gets its own."""
    if getattr(_local, "api", None) is None:
        from googleapiclient.discovery import build

        _local.api = build("classroom", "v1", credentials=_credentials(), cache_discovery=False)
    return _local.api


def _course_work(course: dict):
    """(course, its work, my submissions) for one class; None if it can't be read."""
    api = _thread_api()
    try:
        works = _pages(lambda tok: api.courses().courseWork().list(courseId=course["id"], pageToken=tok,
                                                                    pageSize=100), "courseWork")
        subs = _pages(lambda tok: api.courses().courseWork().studentSubmissions().list(
            courseId=course["id"], courseWorkId="-", userId="me", pageToken=tok, pageSize=100), "studentSubmissions")
    except Exception as e:  # one class with odd permissions shouldn't hide the rest
        log.warning("classroom: couldn't read %s: %s", course.get("name"), e)
        return None
    return course, works, subs


def _fetch_work() -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor

    api = _api()
    courses = _pages(lambda tok: api.courses().list(studentId="me", courseStates=["ACTIVE"], pageToken=tok,
                                                    pageSize=50), "courses")
    items = []
    # All classes at once instead of one after another (18 classes: ~21 s -> a few seconds).
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for r in pool.map(_course_work, courses) if r]
    for c, works, subs in results:
        state = {s["courseWorkId"]: s for s in subs}
        for w in works:
            sub = state.get(w["id"], {})
            items.append({
                "id": w["id"],
                "course": c.get("name", ""),
                "title": w.get("title", "(untitled)"),
                "type": w.get("workType", "").replace("_", " ").lower(),
                "due": _due(w),
                "done": sub.get("state") in DONE_STATES,
                "late": bool(sub.get("late")),
                "grade": sub.get("assignedGrade"),
                "max_points": w.get("maxPoints"),
                "link": w.get("alternateLink", ""),
            })
    return items


def _spoken_due(due: datetime | None, at: datetime | None = None) -> str:
    """'tomorrow at 9:00 AM', as it will sound at time `at` (default now)."""
    if not due:
        return "no due date"
    now = at or datetime.now().astimezone()
    days = (due.date() - now.date()).days
    day = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days, due.strftime("%A %d %B"))
    return f"{day} at {due.strftime('%I:%M %p').lstrip('0')}"


def _guard(fn):
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClassroomNotSetUp as e:
            return {"error": str(e)}
        except Exception as e:
            msg = str(e)
            if "SERVICE_DISABLED" in msg or "has not been used in project" in msg:
                return {"error": "The Google Classroom API isn't enabled in the Google Cloud project yet."}
            raise
    wrapper.__name__, wrapper.__doc__ = fn.__name__, fn.__doc__
    return wrapper


@tool(
    "Google Classroom assignments: what's due and when, which class, and whether the user has turned it in. "
    "Use for 'what homework do I have', 'what's due this week', 'did I submit X'. By default shows unfinished "
    "work due in the next 14 days plus anything overdue.",
    {
        "type": "object",
        "properties": {
            "days_ahead": {"type": "integer", "description": "how far ahead to look, default 14"},
            "include_done": {"type": "boolean", "description": "also list work already turned in"},
            "course": {"type": "string", "description": "only this class (partial name is fine)"},
        },
    },
)
@_guard
def classroom_assignments(days_ahead: int = 14, include_done: bool = False, course: str = ""):
    now = datetime.now().astimezone()
    horizon = now + timedelta(days=max(1, min(int(days_ahead or 14), 120)))
    out = []
    for w in fetch_work():
        if course and course.lower() not in w["course"].lower():
            continue
        if w["done"] and not include_done:
            continue
        due = w["due"]
        if due and due > horizon:
            continue
        if due and due < now - timedelta(days=30):  # ancient history
            continue
        out.append({**w, "due": _spoken_due(due), "overdue": bool(due and due < now and not w["done"]),
                    "_sort": due or horizon})
    out.sort(key=lambda w: w.pop("_sort"))
    return {"assignments": out[:25], "total": len(out)}


@tool("List the user's active Google Classroom classes.")
@_guard
def classroom_courses():
    api = _api()
    courses = _pages(lambda tok: api.courses().list(studentId="me", courseStates=["ACTIVE"], pageToken=tok,
                                                    pageSize=50), "courses")
    return {"courses": [{"name": c.get("name"), "section": c.get("section", "")} for c in courses]}


# ---------------------------------------------------------------- automatic reminders

def remind_offsets() -> list[timedelta]:
    hours = []
    for part in settings.classroom_remind_hours.split(","):
        try:
            hours.append(float(part))
        except ValueError:
            pass
    return [timedelta(hours=h) for h in sorted(set(hours), reverse=True) if h > 0]


def sync_reminders(work: list[dict] | None = None) -> dict:
    """Make sure every unfinished assignment has its reminders (e.g. 24 h and 2 h before it's
    due), and remove pending ones for work that's been turned in or no longer exists."""
    work = fetch_work(fresh=True) if work is None else work  # the background sync always re-reads
    now = datetime.now().astimezone()
    wanted: dict[str, tuple[datetime, str]] = {}
    for w in work:
        due = w["due"]
        if w["done"] or not due or due <= now:
            continue
        for offset in remind_offsets():
            when = due - offset
            if when <= now:
                continue
            hours = offset.total_seconds() / 3600
            lead = f"{int(hours)} hours" if hours >= 2 else ("1 hour" if hours == 1 else f"{int(hours * 60)} minutes")
            # Worded for the moment the reminder is spoken, not now.
            text = (f"{w['title']} for {w['course']} is due {_spoken_due(due, at=when)}, in about {lead}, "
                    f"and it isn't turned in yet")
            wanted[f"classroom:{w['id']}:{int(hours * 60)}"] = (when, text)
    added = removed = 0
    with db() as c:
        existing = {r["source"]: r for r in c.execute(
            "SELECT id, source, due, fired FROM reminders WHERE source LIKE 'classroom:%'")}
        for source, (when, text) in wanted.items():
            row = existing.get(source)
            if row is None:
                c.execute("INSERT INTO reminders (text, due, source) VALUES (?, ?, ?)",
                          (text, when.isoformat(timespec="seconds"), source))
                added += 1
            elif not row["fired"] and row["due"] != when.isoformat(timespec="seconds"):
                # The teacher moved the due date.
                c.execute("UPDATE reminders SET due = ?, text = ? WHERE id = ?",
                          (when.isoformat(timespec="seconds"), text, row["id"]))
        for source, row in existing.items():
            if source not in wanted and not row["fired"]:
                c.execute("DELETE FROM reminders WHERE id = ?", (row["id"],))  # turned in, or removed
                removed += 1
    if added or removed:
        log.info("classroom reminders: +%d -%d", added, removed)
    return {"added": added, "removed": removed, "pending_assignments": sum(1 for w in work if not w["done"])}


class ClassroomWatcher:
    """Re-checks Classroom every 15 minutes and keeps the automatic reminders up to date."""

    def __init__(self):
        threading.Thread(target=self._loop, daemon=True, name="classroom").start()

    def _loop(self):
        time.sleep(20)  # let startup finish first
        while True:
            if connected() and settings.classroom_reminders:
                try:
                    sync_reminders()
                except ClassroomNotSetUp:
                    pass
                except Exception as e:
                    log.warning("classroom sync failed: %s", str(e).splitlines()[0][:200])
            time.sleep(SYNC_MINUTES * 60)
