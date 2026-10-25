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
CREATE TABLE IF NOT EXISTS focus_sessions (
    id INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    started TEXT NOT NULL,          -- ISO 8601 local time with UTC offset
    planned_minutes INTEGER NOT NULL,
    minutes_done INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',   -- active | completed | stopped | lost (Friday was off)
    distractions INTEGER NOT NULL DEFAULT 0,
    allow TEXT NOT NULL DEFAULT ''  -- comma-separated sites/apps allowed for this session
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    due TEXT NOT NULL DEFAULT '',   -- YYYY-MM-DD or empty
    created TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    done_at TEXT                    -- NULL while open
);
CREATE TABLE IF NOT EXISTS classroom_downloads (
    file_id TEXT PRIMARY KEY,       -- the Drive file id, so a file is only fetched once
    course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS classroom_folders (
    course_id TEXT PRIMARY KEY,
    folder TEXT NOT NULL            -- the short name chosen for this class's folder
);
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,               -- local time, YYYY-MM-DD HH:MM:SS
    kind TEXT NOT NULL,             -- user | assistant | action
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS history_at ON history (at);
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    created TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS plans (
    day TEXT PRIMARY KEY,           -- YYYY-MM-DD
    items TEXT NOT NULL,            -- JSON list of short items
    created TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
"""


def _migrate(conn: sqlite3.Connection):
    """Add columns introduced after the database was first created."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(reminders)")}
    try:  # full-text search over history; without FTS5 the search falls back to substring matching
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
                text, content='history', content_rowid='id', tokenize='porter unicode61');
            CREATE TRIGGER IF NOT EXISTS history_ai AFTER INSERT ON history BEGIN
                INSERT INTO history_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS history_ad AFTER DELETE ON history BEGIN
                INSERT INTO history_fts(history_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;""")
        if conn.execute("SELECT count(*) FROM history_fts").fetchone()[0] == 0 \
                and conn.execute("SELECT count(*) FROM history").fetchone()[0]:
            conn.execute("INSERT INTO history_fts(history_fts) VALUES ('rebuild')")
    except sqlite3.OperationalError:
        pass
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
