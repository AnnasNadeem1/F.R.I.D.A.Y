"""Routines: named sequences of simple steps you can run by voice ("run my study setup").

They live in routines.txt, one per line, so you can read and edit them:

    study setup = open Visual Studio Code; site classroom.google.com; volume 30; focus 45 deep learning

Steps are a fixed, safe set (open, site, focus, volume, media, quiet, todo, note, wait, say). Friday runs them
directly, with no model involved, and can save a new one when you ask ("save a routine called ...")."""
import re
import time

from . import focus, pc_tools, quiet, tasks
from .config import ROOT
from .tools import CONTEXT, tool

ROUTINES_FILE = ROOT / "routines.txt"
VERBS = ("open", "site", "focus", "volume", "media", "quiet", "todo", "note", "wait", "say")
HEADER = """# Routines: say "run my <name>" and Friday does each step in order. One per line:
#   name = step; step; step
# Steps:  open <app>   site <address>   focus <minutes> <topic> [allow <site>]   volume <0-100>
#         media <play_pause|next|previous|mute>   quiet <minutes>   todo <text>   note <text>
#         wait <seconds up to 10>   say <text>
# Example (remove the # to use it):
# study setup = open Visual Studio Code; site classroom.google.com; volume 30; focus 45 deep learning
"""


def load() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    try:
        lines = ROUTINES_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, steps = line.split("=", 1)
        parts = [s.strip() for s in steps.split(";") if s.strip()]
        if name.strip() and parts:
            out[name.strip().lower()] = parts
    return out


def check(step: str) -> str | None:
    """A problem with this step, or None if it's fine."""
    verb, _, rest = step.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb not in VERBS:
        return f"unknown step {verb!r} (use: {', '.join(VERBS)})"
    if verb in ("open", "site", "todo", "note", "say") and not rest:
        return f"'{verb}' needs something after it"
    if verb in ("volume", "quiet", "wait") and not re.fullmatch(r"\d+(\.\d+)?", rest):
        return f"'{verb}' needs a number"
    if verb == "focus" and not re.match(r"\d+\b", rest):
        return "'focus' needs minutes first, e.g. focus 45 deep learning"
    if verb == "media" and rest not in ("play_pause", "next", "previous", "mute"):
        return "'media' must be play_pause, next, previous or mute"
    return None


def _run_step(step: str) -> dict:
    verb, _, rest = step.strip().partition(" ")
    verb, rest = verb.lower(), rest.strip()
    if verb == "open":
        return pc_tools.open_app(rest)
    if verb == "site":
        return pc_tools.open_website(rest)
    if verb == "focus":
        minutes, _, topic = rest.partition(" ")
        topic, _, allow = topic.partition(" allow ")
        return focus.MANAGER.start(int(minutes), topic.strip() or "your work", [allow.strip()] if allow.strip() else [])
    if verb == "volume":
        return pc_tools.media_control("set_volume", int(float(rest)))
    if verb == "media":
        return pc_tools.media_control(rest)
    if verb == "quiet":
        quiet.set_manual(int(float(rest)))
        return {"ok": True, "quiet_minutes": int(float(rest))}
    if verb == "todo":
        return tasks.todo_add(rest)
    if verb == "note":
        return tasks.note_add(rest)
    if verb == "wait":
        time.sleep(min(float(rest), 10))
        return {"ok": True}
    return {"say": rest}


def _find(name: str, routines: dict) -> tuple[str | None, list[str]]:
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    norm = {n: re.sub(r"[^a-z0-9]", "", n) for n in routines}
    exact = [n for n, k in norm.items() if k == key]
    if exact:
        return exact[0], []
    partial = [n for n, k in norm.items() if key and (key in k or k in key)]
    return (partial[0], []) if len(partial) == 1 else (None, partial)


@tool("List the user's saved routines (named sequences of steps) and what each does.")
def list_routines():
    r = load()
    return {"routines": [{"name": n, "steps": s} for n, s in r.items()],
            "note": "Edited in routines.txt, or saved by asking you to." if r else
                    "There are no routines yet. Offer to save one."}


@tool(
    "Run one of the user's saved routines by name ('run my study setup'). It does each step in order and reports "
    "what happened to each. Use list_routines if unsure of the name.",
    {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
)
def run_routine(name: str):
    routines = load()
    found, options = _find(name, routines)
    if not found:
        return {"error": f"no routine called {name!r}" + (f"; did they mean one of: {', '.join(options)}?" if options
                                                          else ""), "available": list(routines)}
    results = []
    for step in routines[found]:
        problem = check(step)
        try:
            res = {"error": problem} if problem else _run_step(step)
        except Exception as e:
            res = {"error": f"{type(e).__name__}: {e}"}
        results.append({"step": step, "result": res})
    return {"routine": found, "results": results}


@tool(
    "Save a routine when the user asks ('save a routine called study setup that opens VS Code and starts a 45 minute "
    "focus session'). 'steps' is a list using only: 'open <app>', 'site <address>', 'focus <minutes> <topic>', "
    "'volume <0-100>', 'media <play_pause|next|previous|mute>', 'quiet <minutes>', 'todo <text>', 'note <text>', "
    "'wait <seconds>', 'say <text>'. Saving under an existing name replaces it. Read the steps back afterwards.",
    {"type": "object", "properties": {"name": {"type": "string"}, "steps": {"type": "array", "items": {"type": "string"}}},
     "required": ["name", "steps"]},
)
def save_routine(name: str, steps: list):
    if not (re.search(r"routine|macro|set-?up|workflow", CONTEXT.get("last_user_message", ""), re.I)
            and re.search(r"save|make|create|add|remember|set up", CONTEXT.get("last_user_message", ""), re.I)):
        return {"error": "the user didn't ask to save a routine"}
    name = re.sub(r"[=;\n]", " ", name).strip().lower()
    steps = [re.sub(r"[;\n]", " ", str(s)).strip() for s in steps if str(s).strip()]
    if not name or not steps:
        return {"error": "a routine needs a name and at least one step"}
    for s in steps:
        problem = check(s)
        if problem:
            return {"error": f"step {s!r}: {problem}"}
    try:
        text = ROUTINES_FILE.read_text(encoding="utf-8")
    except OSError:
        text = HEADER
    kept = [ln for ln in text.splitlines()
            if ln.lstrip().startswith("#") or "=" not in ln or ln.split("=", 1)[0].strip().lower() != name]
    kept.append(f"{name} = " + "; ".join(steps))
    ROUTINES_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return {"ok": True, "saved": name, "steps": steps}
