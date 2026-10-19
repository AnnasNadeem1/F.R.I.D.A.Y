"""Claude Code bridge: a tiny local web endpoint that Claude Code hooks report
to (see hooks/notify_jarvis.py). Jarvis announces when Claude finishes a
longer task or needs permission, and can answer "what's Claude doing?".

Listens on 127.0.0.1 only, so nothing outside this PC can reach it."""
import json
import logging
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from .config import settings
from .tools import tool

log = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
ALIVE_FILE = settings.data_dir / "bridge.alive"  # holds the port; refreshed while Jarvis runs
HEARTBEAT_SECONDS = 10
SESSIONS: dict[str, dict] = {}  # session_id -> latest known state
_lock = threading.Lock()


def _first_sentence(text: str, limit: int = 160) -> str:
    """Opening of Claude's reply, cleaned up for speech (no markdown, code or paths)."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)  # [label](link) -> label
    text = re.sub(r"[*_#>|]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    s = m.group(1) if m else text
    return s if len(s) <= limit else s[:limit].rsplit(" ", 1)[0] + "..."


class Bridge:
    def __init__(self, announce: Callable[[str], None], port: int = 8765, announce_min_seconds: float = 30):
        self.announce = announce
        self.min_seconds = announce_min_seconds
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                if self.path != "/claude-event":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    self.send_error(413)
                    return
                try:
                    bridge.handle(json.loads(self.rfile.read(length) or b"{}"))
                except Exception:
                    log.exception("bad Claude event")
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):  # keep the console quiet
                pass

        try:
            self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        except OSError as e:
            log.warning("Claude bridge couldn't use port %s (%s); Claude notifications are off.", port, e)
            self.server = None
            return
        self.port = port
        threading.Thread(target=self.server.serve_forever, daemon=True, name="claude-bridge").start()
        threading.Thread(target=self._heartbeat, daemon=True, name="claude-bridge-heartbeat").start()

    def _heartbeat(self):
        """Keep data/bridge.alive fresh. The hook checks it before connecting, because on
        Windows a connection to a closed port takes ~1 s to fail and Claude waits for hooks."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                ALIVE_FILE.write_text(str(self.port))
            except OSError:
                pass
            time.sleep(HEARTBEAT_SECONDS)

    def handle(self, ev: dict):
        sid = ev.get("session_id") or "unknown"
        project = Path(ev.get("cwd") or "").name or "a project"
        now = time.time()
        with _lock:
            # Forget sessions untouched for a day, so the table doesn't grow forever.
            for stale in [k for k, v in SESSIONS.items() if now - v["since"] > 86400]:
                del SESSIONS[stale]
            s = SESSIONS.setdefault(sid, {"project": project, "state": "unknown", "since": now, "last_message": ""})
            s["project"] = project
            event = ev.get("event")
            if event == "UserPromptSubmit":
                s.update(state="working", since=now, waiting_for="")
                return
            if event == "Stop":
                took = now - s["since"] if s["state"] == "working" else None
                s.update(state="finished", since=now, last_message=ev.get("last_message", ""), waiting_for="")
                if took is not None and took < self.min_seconds:
                    return  # quick back-and-forth; not worth interrupting you
                summary = _first_sentence(s["last_message"])
                text = f"Claude finished in {project}." + (f" It says: {summary}" if summary else "")
            elif event == "Notification":
                msg = ev.get("message", "")
                kind = ev.get("notification_type", "")
                # Only permission requests matter; "waiting for your input" just repeats the Stop.
                if kind == "idle_prompt" or (not kind and "waiting for your input" in msg.lower()):
                    return
                s.update(state="needs you", waiting_for=msg)
                text = f"Claude needs your permission in {project}." if "permission" in msg.lower() else \
                    f"Claude needs your attention in {project}."
            else:
                return
        self.announce(text)


def _ago(t: float) -> str:
    mins = int((time.time() - t) // 60)
    return "just now" if mins < 1 else f"{mins} minute{'s' if mins != 1 else ''} ago"


@tool("Check on the user's Claude Code sessions: which projects Claude is working on, finished, or waiting "
      "for permission, and what it last said. Use for questions like 'what's Claude doing?'")
def claude_code_status():
    with _lock:
        items = sorted(SESSIONS.values(), key=lambda s: s["since"], reverse=True)[:8]
        sessions = [{
            "project": s["project"],
            "state": s["state"],
            "since": _ago(s["since"]),
            "waiting_for": s.get("waiting_for", ""),
            "last_message": _first_sentence(s["last_message"], 300) if s["last_message"] else "",
        } for s in items]
    if not sessions:
        return {"sessions": [], "note": f"No Claude Code activity seen since {settings.name} started."}
    return {"sessions": sessions}
