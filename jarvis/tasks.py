"""A to-do list and a notes pad, by voice: "add buy a charger to my to-do list", "note that the lab is
on Thursday", "what's on my list?". Kept in the same database as memory and reminders.

Notes are for things to look up later; remember() is for lasting facts about the user."""
from datetime import date, datetime

from .store import db
from .tools import tool


def _spoken_due(due: str) -> str:
    try:
        d = date.fromisoformat(due)
    except ValueError:
        return due
    delta = (d - date.today()).days
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    if delta < 0:
        return f"overdue since {d.strftime('%A %d %B')}"
    return d.strftime("%A %d %B")


@tool(
    "Add an item to the user's to-do list. 'text' is the task in a few words. 'due' is an optional date as "
    "YYYY-MM-DD (work it out from today's date for 'tomorrow' or 'Friday'); leave it empty if they gave none.",
    {"type": "object", "properties": {"text": {"type": "string"}, "due": {"type": "string"}}, "required": ["text"]},
)
def todo_add(text: str, due: str = ""):
    due = (due or "").strip()
    if due:
        try:
            date.fromisoformat(due)
        except ValueError:
            return {"error": f"due must look like 2026-10-24, not {due!r}"}
    with db() as c:
        cur = c.execute("INSERT INTO todos (text, due) VALUES (?, ?)", (text.strip(), due))
    return {"ok": True, "id": cur.lastrowid, "added": text.strip(), "due": _spoken_due(due) if due else None}


@tool("List the open items on the user's to-do list, earliest due first (or recently finished ones too if asked).",
      {"type": "object", "properties": {"include_done_today": {"type": "boolean"}}})
def todo_list(include_done_today: bool = False):
    with db() as c:
        rows = c.execute("SELECT id, text, due FROM todos WHERE done_at IS NULL "
                         "ORDER BY due = '', due, id").fetchall()
        done = c.execute("SELECT id, text FROM todos WHERE date(done_at) = date('now', 'localtime')").fetchall() \
            if include_done_today else []
    out = {"open": [{"id": r["id"], "text": r["text"], "due": _spoken_due(r["due"]) if r["due"] else None} for r in rows]}
    if include_done_today:
        out["finished_today"] = [{"id": r["id"], "text": r["text"]} for r in done]
    return out


@tool(
    "Tick off a to-do item: pass its 'id' (from todo_list) or some 'match' words from its text. If several items "
    "match, the options are returned so you can ask which one.",
    {"type": "object", "properties": {"id": {"type": "integer"}, "match": {"type": "string"}}},
)
def todo_done(id: int | None = None, match: str = ""):
    with db() as c:
        if id is not None:
            rows = c.execute("SELECT id, text FROM todos WHERE id = ? AND done_at IS NULL", (id,)).fetchall()
        elif match.strip():
            rows = c.execute("SELECT id, text FROM todos WHERE done_at IS NULL AND text LIKE ? COLLATE NOCASE",
                             (f"%{match.strip()}%",)).fetchall()
        else:
            return {"error": "give an id or some words from the item"}
        if not rows:
            return {"error": "no open to-do item matches that"}
        if len(rows) > 1:
            return {"ambiguous": [{"id": r["id"], "text": r["text"]} for r in rows]}
        c.execute("UPDATE todos SET done_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), rows[0]["id"]))
    return {"ok": True, "done": rows[0]["text"]}


@tool("Remove a to-do item entirely (not finished, just dropped), by id from todo_list.",
      {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]})
def todo_remove(id: int):
    with db() as c:
        row = c.execute("SELECT text FROM todos WHERE id = ?", (id,)).fetchone()
        if not row:
            return {"error": f"no to-do item with id {id}"}
        c.execute("DELETE FROM todos WHERE id = ?", (id,))
    return {"ok": True, "removed": row["text"]}


@tool("Save a note for later ('note that…', 'jot down…'). 'text' is the note, written out clearly.",
      {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]})
def note_add(text: str):
    with db() as c:
        cur = c.execute("INSERT INTO notes (text) VALUES (?)", (text.strip(),))
    return {"ok": True, "id": cur.lastrowid}


@tool("Find saved notes. 'query' is a word or phrase to look for; empty returns the most recent notes.",
      {"type": "object", "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}}})
def note_search(query: str = "", limit: int = 8):
    limit = max(1, min(int(limit or 8), 20))
    with db() as c:
        if query.strip():
            rows = c.execute("SELECT text, created FROM notes WHERE text LIKE ? COLLATE NOCASE ORDER BY id DESC LIMIT ?",
                             (f"%{query.strip()}%", limit)).fetchall()
        else:
            rows = c.execute("SELECT text, created FROM notes ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"notes": [{"text": r["text"], "saved": r["created"]} for r in rows]}
