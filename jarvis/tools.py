"""Tool registry. The LLM sees each tool's name, description and JSON schema,
and decides when to call it. Later milestones (reminders, calendar, email,
Claude Code) register more tools here."""
import json
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class Tool:
    name: str
    description: str
    func: Callable[..., Any]
    parameters: dict = field(default_factory=lambda: {"type": "object", "properties": {}})


REGISTRY: dict[str, Tool] = {}
AFTER_TOOL: list[Callable[[str, dict, str], None]] = []  # called with (name, args, json result) after a tool runs

# What the user actually said this turn, set by the Assistant. Lets tools with
# real-world side effects (sending email) check for the user's own consent
# instead of trusting the model's claim that they agreed.
CONTEXT = {"last_user_message": "", "turn": 0}


def tool(description: str, parameters: dict | None = None):
    def wrap(func):
        REGISTRY[func.__name__] = Tool(
            name=func.__name__,
            description=description,
            func=func,
            parameters=parameters or {"type": "object", "properties": {}},
        )
        return func
    return wrap


def run_tool(name: str, args: dict) -> str:
    """Execute a tool and return a JSON string result. Errors go back to the
    model as data so it can explain or retry, instead of crashing the loop."""
    t = REGISTRY.get(name)
    if t is None:
        return json.dumps({"error": f"unknown tool {name}"})
    # Models occasionally send junk keys (e.g. {"": {}}); pass only declared params.
    allowed = t.parameters.get("properties", {})
    args = {k: v for k, v in (args or {}).items() if k in allowed}
    try:
        result = t.func(**args)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"})
    out = json.dumps(result if result is not None else {"ok": True}, default=str)
    for hook in AFTER_TOOL:
        try:
            hook(name, args, out)
        except Exception:
            pass  # a logging hook must never break a tool
    return out
