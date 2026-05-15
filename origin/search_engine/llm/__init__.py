"""LLM provider package — factory + neutral types.

`get_model_client()` is the only thing other modules should import
from here. It reads `SEARCH_ENGINE["LLM_PROVIDER"]` at call time
and returns the matching adapter. Importing adapters lazily inside
each branch means a deploy that only uses one provider doesn't pay
the import cost of the others (and a missing SDK for an unused
provider doesn't break the app).
"""

from __future__ import annotations

from django.conf import settings

from origin.search_engine.llm.base import ModelClient
from origin.search_engine.llm.types import (
    AgentMessage,
    FunctionCall,
    ToolDeclaration,
)


def get_model_client() -> ModelClient:
    """Return the configured `ModelClient` adapter.

    Provider is chosen by `SEARCH_ENGINE["LLM_PROVIDER"]`:
        "gemini" (default) → `GeminiClient`
        "claude"           → `ClaudeClient`

    Raises `RuntimeError` for an unknown value rather than silently
    falling back, so a typo in the env var surfaces immediately.
    """
    provider = (settings.SEARCH_ENGINE.get("LLM_PROVIDER") or "gemini").lower()
    if provider == "gemini":
        from origin.search_engine.llm.gemini_client import GeminiClient  # noqa: PLC0415

        return GeminiClient()
    if provider == "claude":
        from origin.search_engine.llm.claude_client import ClaudeClient  # noqa: PLC0415

        return ClaudeClient()
    raise RuntimeError(
        f"Unknown LLM_PROVIDER {provider!r}. "
        "Set SEARCH_ENGINE['LLM_PROVIDER'] to 'gemini' or 'claude'."
    )


__all__ = [
    "AgentMessage",
    "FunctionCall",
    "ModelClient",
    "ToolDeclaration",
    "get_model_client",
]
