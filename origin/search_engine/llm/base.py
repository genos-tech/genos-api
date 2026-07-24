"""ModelClient — the interface the agent controller depends on.

A `ModelClient` is the only thing `agent/controller.py` talks to
when it needs the LLM. The controller has no knowledge of which
provider is in use; the active adapter is selected at the boundary
by `get_model_client()` in `__init__.py`.

Phase 5 ships two adapters: `GeminiClient` and `ClaudeClient`. The
interface is intentionally minimal — `generate_step` is the only
method the controller needs — so adding a third adapter (OpenAI,
local model, etc.) is one new file + one branch in the factory.
"""

from __future__ import annotations

from typing import Iterator, Protocol

from origin.search_engine.llm.types import (
    AgentMessage,
    CallUsage,
    FunctionCall,
    ToolDeclaration,
)


class ModelClient(Protocol):
    """Minimal provider interface for the agent loop.

    `generate_step(messages, tools, system_instruction)` runs ONE
    model turn against the given message history. It yields a
    stream of `(text_chunk, function_call)` pairs as the model
    produces them. Per yield, exactly one of the two is non-None:

      * `(text, None)` — incremental text from the model.
      * `(None, FunctionCall)` — the model wants to invoke a tool.

    The controller drives the agent loop on top of this contract —
    accumulating function calls per step, executing them, appending
    the responses to `messages`, and looping until the model
    produces a text-only step (which is the final answer).
    """

    def generate_step(
        self,
        messages: list[AgentMessage],
        tools: list[ToolDeclaration],
        system_instruction: str,
        *,
        model_override: str | None = None,
        usage_sink: CallUsage | None = None,
    ) -> Iterator[tuple[str | None, FunctionCall | None]]:
        """Run one model turn.

        `model_override` lets short-lived callers (e.g. the reranker)
        point the same client at a faster / cheaper model for that one
        call without mutating shared settings. None = use the
        provider's default (the configured `*_MODEL` setting).

        `usage_sink`, when provided, is populated with this call's
        token/model telemetry once the stream is drained (see
        `CallUsage`). Optional and observational — callers that don't
        care about metrics pass nothing, and a provider that can't read
        usage simply leaves the sink at its zero defaults.
        """
        ...
