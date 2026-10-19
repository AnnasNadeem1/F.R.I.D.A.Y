"""Long-term memory: facts about the user that persist across restarts.
All facts are included in the system prompt, so the assistant just knows
them; tools let it add, list and forget them."""
from .store import db
from .tools import tool

MAX_FACTS_IN_PROMPT = 100


def facts_for_prompt() -> str:
    with db() as c:
        rows = c.execute("SELECT id, text FROM facts ORDER BY id DESC LIMIT ?", (MAX_FACTS_IN_PROMPT,)).fetchall()
    if not rows:
        return ""
    lines = "\n".join(f"- [{r['id']}] {r['text']}" for r in reversed(rows))
    return f"What you remember about the user (ids in brackets):\n{lines}"


@tool(
    "Save a lasting fact about the user to long-term memory: preferences, people in their life, "
    "birthdays, where they live or work, ongoing projects. Write it as a short standalone sentence "
    "in the third person, e.g. 'Their favourite colour is blue.' Don't save trivia from one conversation.",
    {"type": "object", "properties": {"fact": {"type": "string"}}, "required": ["fact"]},
)
def remember(fact: str):
    fact = fact.strip()
    with db() as c:
        dup = c.execute("SELECT id FROM facts WHERE lower(text) = lower(?)", (fact,)).fetchone()
        if dup:
            return {"status": "already known before now, nothing new saved", "id": dup["id"]}
        cur = c.execute("INSERT INTO facts (text) VALUES (?)", (fact,))
        return {"status": "newly saved to long-term memory", "id": cur.lastrowid}


@tool(
    "Forget a remembered fact, by its id from the memory list. Use when the user says something "
    "is wrong or out of date (then remember the corrected version), or asks you to forget it.",
    {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
)
def forget(id: int):
    with db() as c:
        row = c.execute("SELECT text FROM facts WHERE id = ?", (id,)).fetchone()
        if not row:
            return {"error": f"no memory with id {id}"}
        c.execute("DELETE FROM facts WHERE id = ?", (id,))
        return {"ok": True, "forgot": row["text"]}
