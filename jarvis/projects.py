"""Project awareness: "where did we leave off on fedcard?" answered from the project itself.

Read-only: git status, recent commits and the README of the projects in CLAUDE_PROJECTS, plus the latest
Claude Code activity Friday announced for that project. Costs no extra model calls to gather."""
import logging
import os
import re
import subprocess
from pathlib import Path

from . import claude_tasks
from .store import db
from .tools import tool

log = logging.getLogger(__name__)


def _git(path: Path, *args: str, timeout: int = 10) -> str:
    """Run a git command in a project. Read-only commands only, no locks taken, nothing from a repo's own
    config is allowed to run a program (fsmonitor), and a failure just returns an empty string."""
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    try:
        r = subprocess.run(["git", "-c", "core.fsmonitor=false", "-C", str(path), *args], capture_output=True,
                           text=True, timeout=timeout, env=env, encoding="utf-8", errors="replace",
                           creationflags=subprocess.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return r.stdout if r.returncode == 0 else ""


def _readme(path: Path) -> str:
    for name in ("README.md", "readme.md", "README.txt", "README"):
        f = path / name
        if f.is_file():
            try:
                text = f.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                return ""
            lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith(("![", "[!["))]
            return re.sub(r"\s+", " ", " ".join(lines[:8]))[:600]
    return ""


def _recent_activity(project: str, limit: int = 3) -> list[str]:
    """What Friday last announced about Claude Code work in this project (from the history log)."""
    try:
        with db() as c:
            rows = c.execute("SELECT at, text FROM history WHERE kind = 'action' AND text LIKE ? COLLATE NOCASE "
                             "ORDER BY id DESC LIMIT ?", (f"%{project}%", limit)).fetchall()
    except Exception:
        return []
    return [f"{r['at'][:16]}: {r['text'][:220]}" for r in rows]


def summarize(name: str, path: Path, detail: bool = True) -> dict:
    status = _git(path, "status", "--porcelain=v1", "-b").splitlines()
    if not status:
        return {"project": name, "git": "not a git repository (or git isn't available)", "readme": _readme(path)}
    branch = status[0][3:].strip()
    changes = [ln for ln in status[1:] if ln.strip()]
    untracked = [ln[3:] for ln in changes if ln.startswith("??")]
    modified = [ln[3:] for ln in changes if not ln.startswith("??")]
    commits = []
    for line in _git(path, "log", "-n", "8" if detail else "1", "--pretty=format:%h%x1f%cr%x1f%an%x1f%s").splitlines():
        h, when, who, subject = (line.split("\x1f") + ["", "", "", ""])[:4]
        commits.append(f"{when}: {subject} ({who})")
    out = {"project": name, "branch": branch, "uncommitted_files": len(modified), "untracked_files": len(untracked),
           "recent_commits": commits}
    if detail:
        out["changed_files"] = [m.split(" -> ")[-1] for m in modified[:12]]
        out["new_files"] = untracked[:8]
        out["readme"] = _readme(path)
        out["recent_claude_activity"] = _recent_activity(name)
    return out


@tool(
    "Project status, read-only: git branch, uncommitted changes, recent commits, README summary and the latest "
    "Claude Code activity for one of the user's projects (the allowed list). Use for 'where did we leave off on "
    "fedcard', 'what's the state of jarvis', 'what changed recently'. With no 'project' it gives a one-line state "
    "of every project. Then suggest a sensible next step from the evidence, and say what's an inference.",
    {"type": "object", "properties": {"project": {"type": "string"}}},
)
def project_status(project: str = ""):
    allowed = claude_tasks.projects()
    if not allowed:
        return {"error": "No projects are set up. They're listed in CLAUDE_PROJECTS in .env (name=folder;name=folder)."}
    if project.strip():
        found = claude_tasks.find_project(project)
        if not found:
            return {"error": f"No project called {project!r}. Known projects: {', '.join(allowed)}."}
        return summarize(*found)
    return {"projects": [summarize(n, p, detail=False) for n, p in allowed.items()]}
