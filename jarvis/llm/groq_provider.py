import json
from typing import Iterator

from groq import Groq

from ..tools import Tool
from .base import Done, Event, ToolCall


class GroqProvider:
    name = "groq"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("GROQ_API_KEY is not set in .env")
        self.client = Groq(api_key=api_key)
        self.model = model

    def _messages(self, system: str, history: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in history:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m["content"] or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.args)}}
                        for c in m["tool_calls"]
                    ]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"], "content": m["content"]})
        return out

    def stream(self, system: str, history: list[dict], tools: list[Tool]) -> Iterator[Event]:
        kwargs = {}
        if "gpt-oss" in self.model:
            # Reasoning tokens delay the first spoken word; keep them short.
            kwargs["reasoning_effort"] = "low"
        if tools:
            kwargs["tools"] = [
                {"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}}
                for t in tools
            ]
        resp = self.client.chat.completions.create(
            model=self.model, messages=self._messages(system, history), stream=True, **kwargs
        )
        pending: dict[int, dict] = {}  # tool calls arrive as fragments keyed by index
        for chunk in resp:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content
            for tc in delta.tool_calls or []:
                slot = pending.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                if tc.id:
                    slot["id"] = tc.id
                if tc.function and tc.function.name:
                    slot["name"] += tc.function.name
                if tc.function and tc.function.arguments:
                    slot["args"] += tc.function.arguments
        for slot in pending.values():
            yield ToolCall(id=slot["id"], name=slot["name"], args=json.loads(slot["args"] or "{}"))
        yield Done()
