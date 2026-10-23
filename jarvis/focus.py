"""Focus sessions and the distraction watch.

"Friday, I'm studying for 45 minutes": a timer runs, the time is logged (for totals and streaks), and
while it runs Friday keeps an eye on the window in front: if it's YouTube, a game or another entry in
distractions.txt for long enough, she tells you off. She does the same in the STUDY_HOURS you set."""
import logging
import random
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from . import watch
from .config import settings
from .store import db
from .tools import tool

log = logging.getLogger(__name__)

GRACE_SECONDS = 20      # how long a distraction has to stay in front before she says anything
NAG_EVERY = 150         # and the least time between two nags
MIN_MINUTES, MAX_MINUTES = 5, 180

# Escalating, spoken-friendly. {what} is e.g. "YouTube", {topic} what you said you'd be doing.
NAGS = (
    ("That's {what}. You said you were studying {topic}. Back to it.",
     "{what} again? {topic}. Now, please.",
     "I can see {what}. Close it and get back to {topic}."),
    ("Still {what}. This is the second time. {topic} isn't going to do itself.",
     "{what}, again. You asked me to hold you to this, so: back to {topic}.",
     "We've been here before. {what} is open. {topic}."),
    ("Third time. I'll keep saying it: close {what} and get back to {topic}.",
     "{what} again. Let me guess: just one more minute? No. {topic}.",
     "At this point I'm just reading you the window title. {what}. Close it."),
)
STUDY_HOURS_NAGS = (
    "It's study time and {what} is open. Pick the books back up.",
    "{what}, during study hours? Really. Close it.",
    "That's {what}. These are your study hours. Close it.",
)


def _parse_hours(text: str) -> list[tuple[int, int]]:
    """"20:00-22:00, 14:00-15:30" -> [(1200, 1320), (840, 930)] in minutes since midnight."""
    out = []
    for part in text.split(","):
        try:
            a, b = (x.strip() for x in part.split("-"))
            (ah, am), (bh, bm) = (map(int, a.split(":")), map(int, b.split(":")))
            out.append((ah * 60 + am, bh * 60 + bm))
        except ValueError:
            continue
    return out


def in_study_hours(now: datetime | None = None) -> bool:
    now = now or datetime.now()
    minute = now.hour * 60 + now.minute
    return any(a <= minute < b for a, b in _parse_hours(settings.study_hours))


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _spoken_minutes(m: int) -> str:
    h, mm = divmod(m, 60)
    if not h:
        return f"{mm} minutes"
    return f"{h} hour{'s' if h > 1 else ''}" + (f" {mm} minutes" if mm else "")


class FocusManager:
    def __init__(self):
        self.announce: Callable[[str, str], None] | None = None  # set by the engine: (kind, text)
        self.session: dict | None = None  # id, topic, started, minutes, allow, distractions
        self.distractions = watch.Distractions()
        self._lock = threading.Lock()
        self._bad_since: float | None = None
        self._last_nag = 0.0
        self._nags = 0
        self._started_thread = False

    # ---------------------------------------------------------------- lifecycle
    def start_thread(self):
        """Begin the background check and pick up a session that was running when Friday restarted."""
        if self._started_thread:
            return
        self._started_thread = True
        self._recover()
        threading.Thread(target=self._loop, daemon=True, name="focus").start()

    def _recover(self):
        with db() as c:
            rows = c.execute("SELECT * FROM focus_sessions WHERE status = 'active'").fetchall()
            for r in rows:
                started = datetime.fromisoformat(r["started"])
                end = started + timedelta(minutes=r["planned_minutes"])
                if end > datetime.now().astimezone() and self.session is None:
                    self.session = self._row_to_session(r)
                else:
                    c.execute("UPDATE focus_sessions SET status = 'lost' WHERE id = ?", (r["id"],))

    @staticmethod
    def _row_to_session(r) -> dict:
        return {"id": r["id"], "topic": r["topic"], "started": datetime.fromisoformat(r["started"]),
                "minutes": r["planned_minutes"], "allow": tuple(a for a in r["allow"].split(",") if a),
                "distractions": r["distractions"]}

    def start(self, minutes: int, topic: str, allow: list[str]) -> dict:
        minutes = max(MIN_MINUTES, min(MAX_MINUTES, int(minutes)))
        topic = (topic or "your work").strip()
        allow_t = tuple(a.strip().lower() for a in allow or [] if a and a.strip())
        with self._lock:
            if self.session:
                s = self.session
                if allow_t:  # "I'm allowed YouTube" said after the session began: update it, don't refuse
                    s["allow"] = tuple(dict.fromkeys(s["allow"] + allow_t))
                    with db() as c:
                        c.execute("UPDATE focus_sessions SET allow = ? WHERE id = ?", (",".join(s["allow"]), s["id"]))
                    return {"ok": True, "updated_running_session": True, "topic": s["topic"], "allowed": list(s["allow"])}
                return {"error": f"A {s['minutes']}-minute session on {s['topic']} is already running. "
                                 f"Stop it first if they want a new one."}
            now = datetime.now().astimezone()
            with db() as c:
                cur = c.execute("INSERT INTO focus_sessions (topic, started, planned_minutes, allow) VALUES (?,?,?,?)",
                                (topic, _iso(now), minutes, ",".join(allow_t)))
            self.session = {"id": cur.lastrowid, "topic": topic, "started": now, "minutes": minutes,
                            "allow": allow_t, "distractions": 0}
            self._bad_since, self._nags, self._last_nag = None, 0, 0.0
        watching = settings.distraction_watch
        return {"ok": True, "minutes": minutes, "topic": topic, "ends": (now + timedelta(minutes=minutes)).strftime("%I:%M %p").lstrip("0"),
                "distraction_watch": watching, "allowed": list(allow_t)}

    def stop(self) -> dict:
        with self._lock:
            s = self.session
            if not s:
                return {"error": "No focus session is running."}
            done = self._finish(s, "stopped")
        return {"ok": True, "topic": s["topic"], "minutes_done": done, "distractions": s["distractions"]}

    def _finish(self, s: dict, status: str) -> int:
        elapsed = int((datetime.now().astimezone() - s["started"]).total_seconds() // 60)
        done = s["minutes"] if status == "completed" else min(elapsed, s["minutes"])
        with db() as c:
            c.execute("UPDATE focus_sessions SET status = ?, minutes_done = ?, distractions = ? WHERE id = ?",
                      (status, done, s["distractions"], s["id"]))
        self.session = None
        return done

    # ---------------------------------------------------------------- stats
    def stats(self) -> dict:
        now = datetime.now().astimezone()
        today = now.date()
        with db() as c:
            rows = c.execute("SELECT started, minutes_done, distractions FROM focus_sessions "
                             "WHERE status IN ('completed','stopped') AND started >= ?",
                             (_iso(now - timedelta(days=60)),)).fetchall()
        per_day: dict = {}
        for r in rows:
            d = datetime.fromisoformat(r["started"]).astimezone().date()
            per_day[d] = per_day.get(d, 0) + r["minutes_done"]
        week = sum(m for d, m in per_day.items() if (today - d).days < 7)
        # Streak: consecutive days with enough focus. Today doesn't break it until the day is over.
        streak, day = 0, today if per_day.get(today, 0) >= settings.study_streak_minutes else today - timedelta(days=1)
        while per_day.get(day, 0) >= settings.study_streak_minutes:
            streak += 1
            day -= timedelta(days=1)
        return {"today_minutes": per_day.get(today, 0), "last_7_days_minutes": week, "streak_days": streak,
                "streak_counts_days_with_at_least_minutes": settings.study_streak_minutes,
                "distractions_today": sum(r["distractions"] for r in rows
                                          if datetime.fromisoformat(r["started"]).astimezone().date() == today)}

    def status(self) -> dict:
        out = {"stats": self.stats()}
        s = self.session
        if s:
            left = max(0, int((s["started"] + timedelta(minutes=s["minutes"]) - datetime.now().astimezone()).total_seconds() // 60))
            out["session"] = {"topic": s["topic"], "minutes_planned": s["minutes"], "minutes_left": left,
                              "times_caught_distracted": s["distractions"], "allowed": list(s["allow"])}
        else:
            out["session"] = None
        return out

    # ---------------------------------------------------------------- background check
    def _say(self, kind: str, text: str):
        if self.announce:
            self.announce(kind, text)

    def _loop(self):
        while True:
            try:
                self._tick()
            except Exception:
                log.exception("focus check failed")
            time.sleep(5)

    def _tick(self):
        s = self.session
        if s:
            end = s["started"] + timedelta(minutes=s["minutes"])
            if datetime.now().astimezone() >= end:
                with self._lock:
                    if self.session is s:
                        done = self._finish(s, "completed")
                        total = self.stats()["today_minutes"]
                        extra = f" That's {_spoken_minutes(total)} today." if total > done else ""
                        caught = (f" I caught you drifting {s['distractions']} time{'s' if s['distractions'] > 1 else ''}."
                                  if s["distractions"] else " Not a single distraction. Well done.")
                        self._say("focus", f"That's {_spoken_minutes(done)} on {s['topic']}.{caught}{extra} Take a short break.")
                return
        if not settings.distraction_watch or not (s or in_study_hours()):
            self._bad_since = None
            return
        program, title = watch.foreground()
        hit = self.distractions.match(program, title, s["allow"] if s else ())
        now = time.monotonic()
        if not hit:
            self._bad_since = None
            return
        if self._bad_since is None:
            self._bad_since = now
        if now - self._bad_since < GRACE_SECONDS or now - self._last_nag < NAG_EVERY:
            return
        self._last_nag = now
        what = watch.friendly(hit)
        if s:
            s["distractions"] += 1
            with db() as c:
                c.execute("UPDATE focus_sessions SET distractions = ? WHERE id = ?", (s["distractions"], s["id"]))
            tier = NAGS[min(self._nags, len(NAGS) - 1)]
            self._nags += 1
            self._say("focus", random.choice(tier).format(what=what, topic=s["topic"]))
        else:
            self._say("focus", random.choice(STUDY_HOURS_NAGS).format(what=what))


MANAGER = FocusManager()


@tool(
    "Start a focus (study/work) session: a timer plus distraction watch. Use when the user says they're about to "
    "study or work. 'minutes' 5-180: if they didn't say how long, ASK first ('how long, forty-five minutes?') and "
    "don't call this until they answer. 'topic' is what they'll work on, e.g. 'deep learning'. 'allow' lists "
    "sites/apps that are fine this time, e.g. ['youtube'] for a lecture; if a session is already running, calling "
    "this with 'allow' just adds to its allowed list.",
    {"type": "object", "properties": {"minutes": {"type": "integer"}, "topic": {"type": "string"},
                                      "allow": {"type": "array", "items": {"type": "string"}}},
     "required": ["minutes", "topic"]},
)
def focus_start(minutes: int, topic: str, allow: list | None = None):
    return MANAGER.start(minutes, topic, allow or [])


@tool("Stop the running focus session early. The time done so far still counts.")
def focus_stop():
    return MANAGER.stop()


@tool("The running focus session (time left, how often they were caught distracted) plus study totals: minutes today "
      "and this week, and the day streak. Use it for 'how much have I studied', and to hold them to account.")
def focus_status():
    return MANAGER.status()
