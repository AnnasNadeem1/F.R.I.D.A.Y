"""Provider-neutral types. History is a list of plain dicts:

    {"role": "user", "content": str}
    {"role": "assistant", "content": str, "tool_calls": [ToolCall], "raw": Any}
    {"role": "tool", "tool_call_id": str, "name": str, "content": str}

"raw" holds the provider's native message (e.g. Gemini Content with thought
signatures) so the same provider can replay it exactly; others ignore it.
"""
from dataclasses import dataclass
from typing import Any, Iterator, Protocol

from ..tools import Tool


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict


@dataclass
class Done:
    raw: Any = None


Event = str | ToolCall | Done  # str = text delta


class Provider(Protocol):
    name: str

    def stream(self, system: str, history: list[dict], tools: list[Tool]) -> Iterator[Event]:
        ...
