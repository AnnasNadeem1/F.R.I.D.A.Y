"""Searchable history: what you and Friday said, and what she did, kept locally and searchable.

"What was I struggling with last week?" is answered from the real conversation, not from a guess.
Stored (in data/jarvis.db, on this PC): your messages, her replies, and a one-line log of actions she took
(emails sent, events added, downloads, Claude tasks). Not stored: tool results, email contents, web pages.
Kept HISTORY_DAYS days (default 90; 0 = keep nothing). You can make her forget by voice or in Settings."""
import logging
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta

from . import tools
from .config import settings
from .store import db
from .tools import CONTEXT, tool

log = logging.getLogger(__name__)

# Messages the app sends itself (scheduled briefing, plan, check-in): not something the user said.
_SELF_PROMPTS = ("Give me my daily briefing", "Plan my day. First call", "Evening check-in. Call")
# Tools whose use is worth remembering ("did I send that email?"). Arguments are summarised, never the bodies.
ACTION_TOOLS = {"email_send_draft": "sent an email", "calendar_create_event": "added a calendar event",
                "claude_task_start": "started a Claude task", "classroom_download_start": "started lecture downloads",
                "focus_start": "started a focus session", "focus_stop": "stopped a focus session",
                "set_reminder": "set a reminder", "todo_add": "added a to-do", "todo_done": "ticked off a to-do",
                "note_add": "saved a note", "run_routine": "ran a routine", "quiet_mode": "changed quiet mode",
                "save_plan": "saved today's plan"}
_last_prune = 0.0
_lock = threading.Lock()


def enabled() -> bool:
    return settings.history_days > 0


def _prune():
    cutoff = (datetime.now() - timedelta(days=settings.history_days)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        c.execute("DELETE FROM history WHERE at < ?", (cutoff,))


def record(kind: str, text: str):
    """Add one entry ("user", "assistant" or "action"). Never raises: history must not break a conversation."""
    global _last_prune
    text = (text or "").strip()
    if not enabled() or not text or text.startswith(_SELF_PROMPTS):
        return
    try:
        with _lock:
            if time.time() - _last_prune > 6 * 3600:  # old entries leave on their own, a few times a day
                _last_prune = time.time()
                _prune()
        with db() as c:
            c.execute("INSERT INTO history (at, kind, text) VALUES (datetime('now', 'localtime'), ?, ?)",
                      (kind, text[:2000]))
    except Exception:
        log.warning("couldn't save history", exc_info=True)


def _after_tool(name: str, args: dict, result: str):
    verb = ACTION_TOOLS.get(name)
    if not verb or '"error"' in result[:40]:
        return
    brief = ", ".join(f"{k}={str(v)[:60]}" for k, v in (args or {}).items()
                      if k in ("text", "topic", "minutes", "name", "course", "project", "title", "summary", "start"))
    record("action", f"{verb}" + (f" ({brief})" if brief else ""))


tools.AFTER_TOOL.append(_after_tool)


def _terms(query: str) -> list[str]:
    return [t for t in re.findall(r"[A-Za-z0-9_]+", query or "") if len(t) > 1]


def _fts_query(terms: list[str], any_term: bool) -> str:
    return (" OR " if any_term else " ").join(f'"{t}"*' for t in terms)


def _search(query: str, days: int, limit: int) -> list[sqlite3.Row]:
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S") if days else "0000"
    terms = _terms(query)
    with db() as c:
        if not terms:
            return c.execute("SELECT id, at, kind, text FROM history WHERE at >= ? ORDER BY id DESC LIMIT ?",
                             (since, limit)).fetchall()
        for any_term in (False, True):  # all the words first, then any of them
            try:
                rows = c.execute(
                    "SELECT h.id, h.at, h.kind, snippet(history_fts, 0, '', '', '...', 24) AS text "
                    "FROM history_fts JOIN history h ON h.id = history_fts.rowid "
                    "WHERE history_fts MATCH ? AND h.at >= ? ORDER BY bm25(history_fts) LIMIT ?",
                    (_fts_query(terms, any_term), since, limit)).fetchall()
            except sqlite3.OperationalError:  # no full-text support: plain substring search
                like = " AND ".join("text LIKE ?" for _ in terms)
                rows = c.execute(f"SELECT id, at, kind, text FROM history WHERE {like} AND at >= ? "
                                 f"ORDER BY id DESC LIMIT ?", (*[f"%{t}%" for t in terms], since, limit)).fetchall()
            if rows:
                return rows
    return []


def _when(at: str) -> str:
    try:
        return datetime.strptime(at, "%Y-%m-%d %H:%M:%S").strftime("%a %d %b, %I:%M %p").replace(" 0", " ")
    except ValueError:
        return at


@tool(
    "Search the saved history of what the user and Friday said and did. Use for 'what did I ask about X', 'what "
    "was I struggling with last week', 'did I send that email', 'when did we talk about Y'. 'query' is a few "
    "keywords (empty = the most recent entries). 'days' limits how far back (0 = all). Answer from the matches only, "
    "and say when nothing matches.",
    {"type": "object", "properties": {"query": {"type": "string"}, "days": {"type": "integer"},
                                      "limit": {"type": "integer"}}},
)
def search_history(query: str = "", days: int = 0, limit: int = 8):
    if not enabled():
        return {"error": "History is switched off (HISTORY_DAYS=0), so nothing has been saved."}
    rows = _search(query, max(0, int(days or 0)), max(1, min(int(limit or 8), 15)))
    who = {"user": "the user said", "assistant": "Friday said", "action": "Friday did"}
    return {"matches": [{"when": _when(r["at"]), "what": who.get(r["kind"], r["kind"]), "text": r["text"]} for r in rows],
            "kept_for_days": settings.history_days}


@tool(
    "Delete saved history, only when the user asks you to forget or delete it. 'query' deletes entries matching those "
    "words; 'older_than_days' deletes older entries; 'everything' wipes it all (only if they said everything/all). "
    "Tell them how many entries were removed.",
    {"type": "object", "properties": {"query": {"type": "string"}, "older_than_days": {"type": "integer"},
                                      "everything": {"type": "boolean"}}},
)
def history_forget(query: str = "", older_than_days: int = 0, everything: bool = False):
    said = CONTEXT.get("last_user_message", "")
    if not re.search(r"forget|delete|erase|clear|remove|wipe", said, re.I):
        return {"error": "the user didn't ask to delete history"}
    if not everything and not older_than_days and not _terms(query):
        return {"error": "say what to forget: some words, an age, or everything"}
    if everything and not re.search(r"everything|\ball\b|whole|entire", said, re.I):
        return {"error": "the user didn't say to delete everything"}
    found = [] if everything or older_than_days else _search(query, 0, 200)  # before db(): it isn't re-entrant
    with db() as c:
        if everything:
            n = c.execute("DELETE FROM history").rowcount
        elif older_than_days:
            cutoff = (datetime.now() - timedelta(days=int(older_than_days))).strftime("%Y-%m-%d %H:%M:%S")
            n = c.execute("DELETE FROM history WHERE at < ?", (cutoff,)).rowcount
        else:
            # A question and its answer go together: forgetting what was asked removes the reply, and the reverse.
            ids = {r["id"] for r in found}
            for r in found:
                if r["kind"] == "user":
                    nxt = c.execute("SELECT id, kind FROM history WHERE id > ? ORDER BY id LIMIT 1", (r["id"],)).fetchone()
                elif r["kind"] == "assistant":
                    nxt = c.execute("SELECT id, kind FROM history WHERE id < ? ORDER BY id DESC LIMIT 1", (r["id"],)).fetchone()
                else:
                    continue
                if nxt and nxt["kind"] == ("assistant" if r["kind"] == "user" else "user"):
                    ids.add(nxt["id"])
            n = sum(c.execute("DELETE FROM history WHERE id = ?", (i,)).rowcount for i in ids)
    return {"ok": True, "removed": n}


def clear_all() -> int:
    """For the Settings button."""
    with db() as c:
        return c.execute("DELETE FROM history").rowcount
