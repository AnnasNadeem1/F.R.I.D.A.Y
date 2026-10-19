import itertools
import uuid
from typing import Iterator

from google import genai
from google.genai import errors as genai_errors
from google.genai import types

from ..tools import Tool
from .base import Done, Event, ToolCall


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set in .env")
        self.client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=45_000))
        self.model = model
        self.use_thinking_config = True

    def _contents(self, history: list[dict]) -> list[types.Content]:
        out: list[types.Content] = []
        for m in history:
            if m["role"] == "user":
                out.append(types.Content(role="user", parts=[types.Part.from_text(text=m["content"])]))
            elif m["role"] == "assistant":
                raw = m.get("raw")
                if isinstance(raw, types.Content):
                    out.append(raw)
                    continue
                parts = [types.Part.from_text(text=m["content"])] if m["content"] else []
                parts += [types.Part.from_function_call(name=c.name, args=c.args) for c in m.get("tool_calls", [])]
                out.append(types.Content(role="model", parts=parts))
            elif m["role"] == "tool":
                part = types.Part.from_function_response(name=m["name"], response={"result": m["content"]})
                # Consecutive tool results belong in one user turn.
                if out and out[-1].role == "user" and out[-1].parts and out[-1].parts[0].function_response:
                    out[-1].parts.append(part)
                else:
                    out.append(types.Content(role="user", parts=[part]))
        return out

    def stream(self, system: str, history: list[dict], tools: list[Tool]) -> Iterator[Event]:
        config = types.GenerateContentConfig(
            system_instruction=system,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.parameters)
                for t in tools
            ])] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        # Thinking adds latency; a voice assistant wants fast first words.
        # Models differ in which settings they accept, so drop it if rejected.
        if self.use_thinking_config:
            if "2.5" in self.model:
                config.thinking_config = types.ThinkingConfig(thinking_budget=0)
            else:
                config.thinking_config = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)

        contents = self._contents(history)
        try:
            chunks = self.client.models.generate_content_stream(model=self.model, contents=contents, config=config)
            first = next(chunks, None)
        except genai_errors.ClientError as e:
            if config.thinking_config is None or "thinking" not in str(e).lower():
                raise
            self.use_thinking_config = False
            config.thinking_config = None
            chunks = self.client.models.generate_content_stream(model=self.model, contents=contents, config=config)
            first = next(chunks, None)

        parts: list[types.Part] = []
        for chunk in itertools.chain([first] if first else [], chunks):
            if not chunk.candidates or not chunk.candidates[0].content or not chunk.candidates[0].content.parts:
                continue
            for p in chunk.candidates[0].content.parts:
                parts.append(p)
                if p.function_call:
                    fc = p.function_call
                    yield ToolCall(id=fc.id or uuid.uuid4().hex, name=fc.name, args=dict(fc.args or {}))
                elif p.text and not p.thought:
                    yield p.text
        yield Done(raw=types.Content(role="model", parts=parts))
