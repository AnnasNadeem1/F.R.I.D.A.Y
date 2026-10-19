from ..config import settings
from .base import Done, Provider, ToolCall


def make_provider(name: str) -> Provider:
    if name == "gemini":
        from .gemini import GeminiProvider
        return GeminiProvider(settings.gemini_api_key, settings.gemini_model)
    if name == "groq":
        from .groq_provider import GroqProvider
        return GroqProvider(settings.groq_api_key, settings.groq_model)
    if name == "groq-backup":
        from .groq_provider import GroqProvider
        p = GroqProvider(settings.groq_api_key, settings.groq_model_backup)
        p.name = "groq-backup"
        return p
    raise ValueError(f"unknown LLM provider: {name}")


def available_providers() -> list[Provider]:
    """Configured provider first, then any other provider that has a key, as fallback."""
    order = [settings.llm_provider] + [p for p in ("gemini", "groq") if p != settings.llm_provider]
    keys = {"gemini": settings.gemini_api_key, "groq": settings.groq_api_key}
    names = [p for p in order if keys.get(p)]
    if settings.groq_api_key and settings.groq_model_backup and settings.groq_model_backup != settings.groq_model:
        names.append("groq-backup")  # Groq limits tokens per model, so a second model is a second allowance
    return [make_provider(p) for p in names]


__all__ = ["Done", "Provider", "ToolCall", "make_provider", "available_providers"]
