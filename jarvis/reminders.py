"""Reminders: the LLM turns "in twenty minutes" or "tomorrow at five" into an
absolute time (it knows the current time from the system prompt), and a
background thread announces them when due, by voice and a Windows toast.
Reminders that came due while Jarvis was off are announced at startup."""
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from .config import settings
from .store import db
from .tools import tool

log = logging.getLogger(__name__)


def _parse_due(when: str) -> datetime:
    due = datetime.fromisoformat(when.strip().replace("Z", "+00:00"))
    if due.tzinfo is None:
        due = due.astimezone()  # naive -> local time
    return due


def _spoken(dt: datetime) -> str:
    now = datetime.now().astimezone()
    day = "today" if dt.date() == now.date() else dt.strftime("%A %d %B")
    return f"{day} at {dt.strftime('%I:%M %p').lstrip('0')}"


@tool(
    "Set a reminder. 'due' is an absolute local date-time in ISO 8601, e.g. '2026-10-19T17:00:00'; "
    "work it out from the current time for relative requests like 'in 20 minutes'. If the user gives "
    "no time at all, ask them when. 'text' is what to remind them about, e.g. 'call your brother'.",
    {
        "type": "object",
        "properties": {"text": {"type": "string"}, "due": {"type": "string"}},
        "required": ["text", "due"],
    },
)
def set_reminder(text: str, due: str):
    try:
        when = _parse_due(due)
    except ValueError:
        return {"error": f"couldn't parse time {due!r}; use ISO 8601 like 2026-10-19T17:00:00"}
    if when <= datetime.now().astimezone():
        return {"error": f"{when.isoformat(timespec='minutes')} is in the past; check the date"}
    with db() as c:
        cur = c.execute("INSERT INTO reminders (text, due) VALUES (?, ?)", (text.strip(), when.isoformat(timespec="seconds")))
    return {"ok": True, "id": cur.lastrowid, "due": _spoken(when)}


@tool("List upcoming reminders that haven't gone off yet.")
def list_reminders():
    with db() as c:
        rows = c.execute("SELECT id, text, due FROM reminders WHERE fired = 0 ORDER BY due").fetchall()
    return {"reminders": [{"id": r["id"], "text": r["text"], "due": _spoken(_parse_due(r["due"]))} for r in rows]}


@tool(
    "Cancel an upcoming reminder by id (get ids from list_reminders).",
    {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
)
def cancel_reminder(id: int):
    with db() as c:
        row = c.execute("SELECT text, source FROM reminders WHERE id = ? AND fired = 0", (id,)).fetchone()
        if not row:
            return {"error": f"no upcoming reminder with id {id}"}
        if row["source"]:
            # Automatic (e.g. Classroom): keep it as "done" so the next sync doesn't recreate it.
            c.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (id,))
        else:
            c.execute("DELETE FROM reminders WHERE id = ?", (id,))
    return {"ok": True, "cancelled": row["text"]}


def _toast(title: str, message: str):
    try:
        from winotify import Notification

        Notification(app_id=settings.name, title=title, msg=message, duration="long").show()
    except Exception as e:
        log.info("toast failed: %s", e)


class ReminderScheduler:
    """Checks every few seconds for due reminders and calls announce(text, late)."""

    def __init__(self, announce: Callable[[str, bool], None], poll_seconds: float = 5.0):
        self.announce = announce
        self.poll = poll_seconds
        threading.Thread(target=self._loop, daemon=True, name="reminders").start()

    def _due_now(self) -> list:
        now = datetime.now().astimezone()
        with db() as c:
            rows = c.execute("SELECT id, text, due FROM reminders WHERE fired = 0").fetchall()
            due = [r for r in rows if _parse_due(r["due"]) <= now]
            for r in due:
                c.execute("UPDATE reminders SET fired = 1 WHERE id = ?", (r["id"],))
        return due

    def _prune(self):
        """Fired reminders are only kept a month (Classroom ones act as 'already reminded' markers)."""
        cutoff = (datetime.now().astimezone() - timedelta(days=30)).isoformat(timespec="seconds")
        with db() as c:
            c.execute("DELETE FROM reminders WHERE fired = 1 AND due < ?", (cutoff,))

    def _loop(self):
        last_prune = 0.0
        while True:
            try:
                if time.time() - last_prune > 86400:
                    self._prune()
                    last_prune = time.time()
                now = datetime.now().astimezone()
                for r in self._due_now():
                    late = (now - _parse_due(r["due"])).total_seconds() > 120  # missed while Jarvis was off
                    _toast("Reminder", r["text"])
                    self.announce(r["text"], late)
            except Exception:
                log.exception("reminder check failed")
            time.sleep(self.poll)
