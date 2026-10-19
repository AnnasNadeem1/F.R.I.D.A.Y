"""SQLite storage for long-term memory and reminders (data/jarvis.db).
One short-lived connection per call keeps it safe to use from the reminder
thread and the conversation thread at the same time."""
import sqlite3
import threading
from contextlib import contextmanager

from .config import settings

_lock = threading.Lock()
_prepared: dict = {}  # which database file has had its schema set up in this process
DB_PATH = settings.data_dir / "jarvis.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    due TEXT NOT NULL,              -- ISO 8601 local time with UTC offset
    created TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    fired INTEGER NOT NULL DEFAULT 0
);
"""


def _migrate(conn: sqlite3.Connection):
    """Add columns introduced after the database was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
    if "source" not in cols:
        # Who created a reminder: NULL = the user; "classroom:<id>:<when>" = automatic,
        # so it can be found again (e.g. removed once the work is turned in).
        conn.execute("ALTER TABLE reminders ADD COLUMN source TEXT")


@contextmanager
def db():
    with _lock:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            if _prepared.get("path") != DB_PATH:  # tables/migrations once per database, not every call
                conn.executescript(SCHEMA)
                _migrate(conn)
                _prepared["path"] = DB_PATH
            yield conn
            conn.commit()
        finally:
            conn.close()
