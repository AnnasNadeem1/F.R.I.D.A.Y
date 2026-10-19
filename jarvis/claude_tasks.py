"""Voice-launched Claude Code tasks.

"Friday, ask Claude to find out why the tests fail in fedcard": she prepares the task,
reads it back (project, what it will do, whether it may change files) and starts it only
after a clear spoken yes. The safeguards live in code, not in the AI's good intentions:

* only projects on the allow-list (CLAUDE_PROJECTS) can be used;
* "read" mode (the default) can read, search and run git status/diff/log; everything else
  is refused outright (permission mode dontAsk). "edit" mode adds file edits and test runs
  and needs the same explicit yes, and the read-back says so;
* never bypasses permissions, runs one task at a time, and has a time limit and a cancel;
* runs on your Claude login only: any ANTHROPIC_API_KEY is removed from its environment,
  so it can never bill an API account by accident.

Each task is a fresh Claude Code session: it can't see your other conversations.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .claude_bridge import _first_sentence
from .config import settings
from .email_tools import user_confirmed
from .tools import CONTEXT, tool

log = logging.getLogger(__name__)

SAFE_GIT = ["Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
MODES = {
    # dontAsk = anything not on the allow-list is denied without prompting (nobody is there to answer)
    "read": {"permission": "dontAsk", "tools": ["Read", "Grep", "Glob", *SAFE_GIT]},
    "edit": {"permission": "acceptEdits",
             "tools": ["Read", "Grep", "Glob", "Edit", "Write", *SAFE_GIT,
                       "Bash(python -m pytest:*)", "Bash(npm test:*)", "Bash(npm run test:*)"]},
}
# Edit mode auto-approves common file commands inside the project (including rm), so deleting
# is blocked explicitly. Deny rules win over allow rules. Tasks can create and change files,
# never delete them.
NEVER = ["Bash(rm:*)", "Bash(rmdir:*)", "Bash(del:*)", "Bash(erase:*)", "Bash(rd:*)",
         "PowerShell(Remove-Item:*)", "PowerShell(rm:*)", "PowerShell(del:*)", "PowerShell(rmdir:*)",
         "Bash(git push:*)", "Bash(git reset:*)", "Bash(git clean:*)", "Bash(git checkout:*)"]
SYSTEM_NOTE = ("You were started by a voice assistant, with nobody watching. Work on your own and don't ask "
               "questions. Your final answer must START with one plain sentence that can be read aloud as it is "
               "(no markdown, no bullets, no file paths or symbols) giving the outcome, then, if useful, details "
               "and anything the user must decide. If a tool was denied, say so; don't try to work around it.")
RESULT_LIMIT = 3000
_lock = threading.Lock()


def projects() -> dict[str, Path]:
    """Allowed projects: name -> folder, from CLAUDE_PROJECTS ("name=path;name=path")."""
    out = {}
    for part in settings.claude_projects.split(";"):
        if "=" in part:
            name, path = part.split("=", 1)
            if name.strip() and Path(path.strip()).is_dir():
                out[name.strip().lower()] = Path(path.strip())
    return out


def find_project(name: str) -> tuple[str, Path] | None:
    allowed = projects()
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for n, p in allowed.items():
        if re.sub(r"[^a-z0-9]", "", n) == key:
            return n, p
    for n, p in allowed.items():  # partial match: "the jarvis project"
        if key and (key in re.sub(r"[^a-z0-9]", "", n) or re.sub(r"[^a-z0-9]", "", n) in key):
            return n, p
    return None


def _clean_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    env["FRIDAY_TASK"] = "1"  # the Claude Code hook skips these; the assistant announces them itself
    return env


def _describe(tool_name: str, args: dict) -> str:
    target = args.get("file_path") or args.get("path") or args.get("pattern") or args.get("command") or ""
    verb = {"Read": "Reading", "Grep": "Searching", "Glob": "Looking for files", "Edit": "Editing",
            "Write": "Writing", "Bash": "Running"}.get(tool_name, tool_name)
    return f"{verb} {Path(str(target)).name if tool_name in ('Read', 'Edit', 'Write') else str(target)[:60]}".strip()


class Task:
    def __init__(self, project: str, path: Path, text: str, mode: str):
        self.id = uuid.uuid4().hex[:8]
        self.project, self.path, self.text, self.mode = project, path, text, mode
        self.state = "pending"  # pending -> running -> done | failed | cancelled | timed out
        self.created_turn = CONTEXT.get("turn", 0)
        self.started = self.ended = 0.0
        self.result = ""
        self.activity = ""
        self.proc: subprocess.Popen | None = None
        self.cancelled = False


class TaskManager:
    def __init__(self):
        self.tasks: dict[str, Task] = {}
        self.on_done: Callable[[str], None] | None = None  # set by the engine: speak/notify
        self.on_activity: Callable[[str], None] | None = None

    # ---- lifecycle

    def running(self) -> Task | None:
        return next((t for t in self.tasks.values() if t.state == "running"), None)

    def start(self, task: Task):
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("Claude Code isn't installed (the 'claude' command wasn't found).")
        mode = MODES[task.mode]
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
               "--model", settings.claude_task_model, "--permission-mode", mode["permission"],
               "--append-system-prompt", SYSTEM_NOTE, "--disallowedTools", *NEVER, "--allowedTools", *mode["tools"]]
        task.proc = subprocess.Popen(cmd, cwd=str(task.path), env=_clean_env(), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                     errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        task.state, task.started = "running", time.time()
        task.proc.stdin.write(task.text)  # via stdin: no quoting problems, nothing long on the command line
        task.proc.stdin.close()
        threading.Thread(target=self._read, args=(task,), daemon=True, name=f"claude-task-{task.id}").start()
        threading.Thread(target=self._watchdog, args=(task,), daemon=True).start()
        log.info("claude task %s started in %s (%s mode)", task.id, task.project, task.mode)

    def _read(self, task: Task):
        err = []
        threading.Thread(target=lambda: err.extend(task.proc.stderr), daemon=True).start()
        for line in task.proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "assistant":
                for block in (ev.get("message") or {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        task.activity = _describe(block.get("name", ""), block.get("input") or {})
                        if self.on_activity:
                            self.on_activity(f"Claude: {task.activity}")
            elif ev.get("type") == "result":
                task.result = (ev.get("result") or "").strip()
                if ev.get("is_error"):
                    task.state = "failed"
                elif task.state == "running":
                    task.state = "done"
        task.proc.wait()
        task.ended = time.time()
        if task.state == "running":  # exited without a result
            task.state = "failed"
            task.result = task.result or ("".join(err).strip()[-300:] or "Claude exited without an answer.")
        if "not logged in" in task.result.lower() or "oauth" in task.result.lower() or "authenticate" in task.result.lower():
            task.result = "Claude Code isn't signed in on this PC. Run: claude auth login"
        if self.on_activity:
            self.on_activity("")
        self._announce(task)

    def _watchdog(self, task: Task):
        limit = settings.claude_task_minutes * 60
        while task.state == "running":
            if time.time() - task.started > limit:
                task.state = "timed out"
                self.kill(task)
                return
            time.sleep(2)

    def kill(self, task: Task):
        if task.proc and task.proc.poll() is None:
            # The CLI is a .cmd wrapper around node, so end the whole process tree.
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(task.proc.pid)], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)

    def _announce(self, task: Task):
        minutes = round((task.ended - task.started) / 60)
        took = "less than a minute" if minutes < 1 else ("about a minute" if minutes == 1 else f"about {minutes} minutes")
        if task.state == "done":
            # Spoken aloud: drop list bullets and dashes, and read paths without their backslashes.
            spoken = re.sub(r"^[\s\-*•]+", "", task.result)
            spoken = spoken.replace("\\", " ").replace("—", ", ").replace(" - ", ", ")
            text = f"Claude finished the task in {task.project}, after {took}. " + _first_sentence(spoken, 220)
        elif task.state == "timed out":
            text = f"Claude ran out of time on the task in {task.project} and I stopped it."
        elif task.state == "cancelled":
            return
        else:
            text = f"The Claude task in {task.project} didn't work out. {_first_sentence(task.result, 160)}"
        if self.on_done:
            self.on_done(text)


MANAGER = TaskManager()


def _enabled_error() -> dict | None:
    if not settings.claude_tasks:
        return {"error": "Voice-launched Claude tasks are turned off. They can be turned on in Settings."}
    if not projects():
        return {"error": "No Claude projects are set up. Add them under CLAUDE_PROJECTS in .env."}
    return None


@tool(
    "STEP 1 of starting a Claude Code task on the user's PC: prepare it (nothing runs yet). Use when they ask to "
    "'have Claude' do something in one of their projects, such as investigate, review or fix code. Claude starts "
    "with no memory of this conversation, so write 'task' as a complete, self-contained instruction. Then you MUST "
    "read the plan back (project, what Claude will do, and, if can_edit, that it may change files and run tests) "
    "and ask 'Shall I start it?'. Only call claude_task_start after they clearly say yes. can_edit stays false "
    "unless they explicitly want files changed.",
    {"type": "object",
     "properties": {"project": {"type": "string", "description": "project name, e.g. 'fedcard' or 'jarvis'"},
                    "task": {"type": "string"},
                    "can_edit": {"type": "boolean", "description": "true only if the user wants files changed"}},
     "required": ["project", "task"]},
)
def claude_task_prepare(project: str, task: str, can_edit: bool = False):
    if (err := _enabled_error()):
        return err
    if MANAGER.running():
        return {"error": "A Claude task is already running. Ask how it's going, or cancel it, before starting another."}
    found = find_project(project)
    if not found:
        return {"error": f"'{project}' isn't on the allowed list. Allowed projects: {', '.join(projects())}."}
    name, path = found
    t = Task(name, path, task.strip(), "edit" if can_edit else "read")
    MANAGER.tasks[t.id] = t
    for old in [k for k, v in MANAGER.tasks.items() if v.state != "running" and time.time() - (v.ended or v.started or 0) > 86400 and v.state != "pending"]:
        del MANAGER.tasks[old]
    access = ("MAY CREATE AND CHANGE FILES in the project and run its tests (it can't delete files)" if can_edit
              else "READ-ONLY: it can read and search files and check git history, but can't change anything or run other commands")
    return {"task_id": t.id, "project": name, "access": access, "model": settings.claude_task_model,
            "time_limit_minutes": settings.claude_task_minutes,
            "next": "Read this back to the user and ask 'Shall I start it?'. Don't start it yet."}


@tool("STEP 2: start a prepared Claude task. Works only after the user has clearly said yes to your read-back.",
      {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]})
def claude_task_start(task_id: str):
    if (err := _enabled_error()):
        return err
    with _lock:
        t = MANAGER.tasks.get(task_id)
        if t is None or t.state != "pending":
            return {"error": "No such prepared task. Prepare it first."}
        if t.created_turn >= CONTEXT.get("turn", 0):
            return {"error": "Not started: read the plan back to the user and ask 'Shall I start it?' first."}
        if not user_confirmed(CONTEXT.get("last_user_message", "")):
            return {"error": "Not started: the user hasn't clearly said yes. Ask them first."}
        if MANAGER.running():
            return {"error": "Another Claude task is still running. Check on it or cancel it first."}
        try:
            MANAGER.start(t)
        except Exception as e:
            t.state = "failed"
            return {"error": str(e)}
    return {"status": "started", "project": t.project, "mode": t.mode,
            "tell_user": "Claude is on it; I'll tell you when it's done, or you can ask how it's going."}


@tool("Check on Claude tasks started by voice: what's running, what it's doing now, and finished results.")
def claude_task_status():
    items = []
    for t in sorted(MANAGER.tasks.values(), key=lambda t: t.started or 0, reverse=True)[:5]:
        if t.state == "pending":
            continue
        row = {"project": t.project, "mode": t.mode, "state": t.state, "task": t.text[:120]}
        if t.state == "running":
            row.update(doing_now=t.activity or "starting", running_for_minutes=round((time.time() - t.started) / 60, 1))
        else:
            row["result"] = t.result[:RESULT_LIMIT]
        items.append(row)
    return {"tasks": items} if items else {"tasks": [], "note": "No Claude tasks have been started by voice."}


@tool("Cancel the Claude task that's running now.")
def claude_task_cancel():
    t = MANAGER.running()
    if not t:
        return {"error": "No Claude task is running."}
    t.state, t.cancelled = "cancelled", True
    MANAGER.kill(t)
    return {"status": "cancelled", "project": t.project}
