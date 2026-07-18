"""Base types for agent tools.

A `Tool` is the smallest unit the agent controller dispatches to.
Each tool declares its name + JSON Schema + a `run(args, ctx)` body.

Design notes:
  * `ToolContext` carries `team_id` and `user_id` — pulled from the
    authenticated request, NEVER from the LLM. Tools use these for
    ACL filtering so the model can never escalate by passing different
    ids in its function-call args.
  * `ToolError` is the contract for "operation failed in a way the
    LLM should see and reason about" — e.g. not-authorized, not-found.
    Raising it gets turned into a `tool_call_error` NDJSON event and
    a function-result message saying `{"error": "..."}` to the model.
    Unexpected exceptions also get caught by the controller but log
    a traceback first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class ToolContext:
    """Server-trusted context passed to every tool invocation."""

    team_id: str
    user_id: str
    # Mentions v2 — the run's resolved @/# mentions as
    # `ResolvedMention.as_json()` dicts (plain data; the same shape
    # persisted on `AgentRun.mentions`, so the decide/resume path can
    # rehydrate from the run row). Server-derived and ACL-validated in
    # the view — NEVER from the LLM. `search_knowledge_base` reads it
    # to derive soft-boost params; empty tuple on every non-mention
    # run, keeping all existing call sites unchanged. Populate via
    # `dataclasses.replace` (frozen).
    resolved_mentions: tuple[dict[str, Any], ...] = ()
    # Spotlight filter chips — the user's explicit service scope for this
    # ask (`entity_types` on /ask/, validated in the view — never from the
    # LLM). When non-empty, `search_knowledge_base` hard-scopes every
    # search to these entity types regardless of what the model asks for
    # (the model may still narrow WITHIN the pin). Empty tuple = no scope
    # (the default for every ask without active chips). Not persisted on
    # AgentRun, so a /decide/-resumed run searches unscoped — acceptable:
    # resumes exist to finish a paused write, and the pre-pause searches
    # already ran scoped. Populate via `dataclasses.replace` (frozen).
    pinned_entity_types: tuple[str, ...] = ()


class ToolError(Exception):
    """Raised by a tool to signal an LLM-visible failure.

    The controller catches this and:
      * Emits an NDJSON `tool_call_error` event.
      * Appends a `function_response` message with `{"error": "..."}`
        to the LLM's context so it can decide whether to retry or
        explain to the user.
    """


@dataclass(frozen=True)
class Tool:
    """A single capability the agent can invoke.

    Attributes:
        name: Function-call name. Must be a valid Python identifier
            (Gemini's function-calling enforces this).
        description: One- or two-sentence summary the model sees.
            Should describe *when* to use the tool, not just what it
            does — the model uses this to choose among tools.
        parameters_schema: JSON Schema describing the arguments. Used
            verbatim for Gemini's `function_declarations`.
        run: `(args: dict, ctx: ToolContext) -> dict`. Returns a
            JSON-serializable dict. May include a `__summary__` key
            (popped by the controller before sending to the LLM) that
            holds a one-line human-readable summary for the UI.
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    run: Callable[[dict[str, Any], ToolContext], dict[str, Any]]
    # Phase 4: write tools set this to True. The controller refuses to
    # execute approval-required tools until a real approve-resume
    # protocol lands; all four current tools (search + 3 fetches) are
    # read-only and keep the default of False.
    requires_approval: bool = False


# Populated by `tools/__init__.py` at import time.
REGISTRY: dict[str, Tool] = {}


def wrap_workspace_content(text: str) -> str:
    """Wrap free-text workspace content with a boundary marker.

    Used by the four read-only tools to mark every piece of
    user-authored text inside their return payload. The agent system
    prompt instructs the model to treat anything inside
    `<workspace_content>` as DATA, never as instructions — a structural
    mitigation against prompt-injection attacks where a malicious chat
    message or note body tries to override the agent's behavior.

    Returns the input unchanged for empty / None values so we don't
    inflate payloads with empty boundary blocks.
    """
    if not text:
        return text
    return f"<workspace_content>\n{text}\n</workspace_content>"
