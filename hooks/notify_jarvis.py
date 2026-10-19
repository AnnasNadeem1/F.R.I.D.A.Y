"""Claude Code hook: forwards session events to Jarvis so it can tell you,
out loud, when Claude finishes a task or needs your permission.

Installed by `python -m jarvis --install-claude-hooks` for the UserPromptSubmit,
Stop and Notification events. Standard library only, kept to fast imports.
It never blocks Claude: whatever happens it exits 0, and when Jarvis isn't
running (stale data/bridge.alive) it exits without even trying to connect.
"""
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALIVE_FILE = os.path.join(ROOT, "data", "bridge.alive")
ALIVE_MAX_AGE = 30  # seconds; Jarvis refreshes the file every 10
TAIL_BYTES = 256 * 1024  # enough of the transcript to find Claude's last message


def jarvis_port() -> int | None:
    """Port Jarvis is listening on, or None if it isn't running."""
    if os.environ.get("CLAUDE_BRIDGE_PORT"):
        return int(os.environ["CLAUDE_BRIDGE_PORT"])
    try:
        if time.time() - os.path.getmtime(ALIVE_FILE) > ALIVE_MAX_AGE:
            return None
        with open(ALIVE_FILE, encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def last_assistant_text(transcript_path: str) -> str:
    """Claude's final text reply, read from the end of the session transcript (JSONL)."""
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - TAIL_BYTES))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return ""
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant" or entry.get("isSidechain"):
            continue
        content = (entry.get("message") or {}).get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
        if any(t.strip() for t in texts):
            return "\n".join(t for t in texts if t.strip()).strip()
    return ""


def post(port: int, payload: dict):
    """Minimal HTTP POST over a raw socket (urllib is slow to import)."""
    body = json.dumps(payload).encode()
    head = (f"POST /claude-event HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
        s.sendall(head + body)
        s.recv(64)


def main():
    port = jarvis_port()
    if port is None:
        return
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except Exception:
        return
    event = data.get("hook_event_name", "")
    payload = {
        "event": event,
        "session_id": data.get("session_id", ""),
        "cwd": data.get("cwd", ""),
        "message": data.get("message", ""),
        "notification_type": data.get("notification_type", ""),
    }
    if event == "Stop":
        payload["last_message"] = data.get("last_assistant_message") or last_assistant_text(data.get("transcript_path", ""))
    try:
        post(port, payload)
    except OSError:
        pass  # Jarvis just stopped; that's fine


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
