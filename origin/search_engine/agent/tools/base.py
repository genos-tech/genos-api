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

from origin.search_engine.agent.fence import WEB_TAG, WORKSPACE_TAG, wrap


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
    # Answer-language locale (GENOS_CAPABILITY_ROADMAP §3.4) — the asking
    # user's UI locale ("ja", "es", …), forwarded by the view from the
    # request body (falling back to `User.language`) and persisted on
    # `AgentRun` so a leg resumed after write-approval keeps the same
    # language. Server-derived, NEVER from the LLM. Empty string on any
    # run without a known locale, which leaves the prompt untouched.
    # `_drive_loop` reads it via `language_directive`.
    locale: str = ""
    # Which front door the call came through: "mcp" for the public MCP
    # server, "" for the in-app agent (MIP-798). Server-set, NEVER from the
    # LLM or the caller. A few read tools shape their output differently for
    # an external client (cap a long chunk, send one copy of a body); the
    # empty default keeps every in-app call byte-identical to before.
    surface: str = ""
    # Set ONLY by the in-app approve path (`resume_agent`) for the one approved
    # write it runs: the tool then also returns a pre-image under `__undo__`
    # (popped by the controller and stored on `AgentStep.undo_json`, never shown
    # to the model), so the person can undo the write (`agent/undo.py`). Off for
    # every other caller (MCP, tests, evals): their results carry no pre-image
    # and cost no extra reads. Server-set, NEVER from the LLM.
    capture_undo: bool = False


class ToolError(Exception):
    """Raised by a tool to signal an LLM-visible failure.

    The controller catches this and:
      * Emits an NDJSON `tool_call_error` event.
      * Appends a `function_response` message with `{"error": "..."}`
        to the LLM's context so it can decide whether to retry or
        explain to the user.
    """


class ToolForbidden(ToolError):
    """A `ToolError` for an authorization failure: the thing exists and the
    caller may not have it (wrong team, not a member, outside the ACL).

    A subclass, so every `except ToolError` keeps catching it and the
    caller sees the same message as before. What it adds is a kind: over
    MCP a refusal comes back as HTTP 200 `isError`, so the refusal-burst
    alert (MIP-725) can't see it as a 403, and a plain `ToolError` can't
    tell a refusal from a typo. MCP counts these (MIP-744).

    Raise it at the guard, for "found but not yours". A missing id, a bad
    argument or a validation failure stays a plain `ToolError`: those are
    mistakes, not probing.
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
    # Published on MCP only (MIP-1070). The in-app agent never declares, lists or
    # runs it: `register_tool` files it in `MCP_ONLY_REGISTRY` instead of
    # `REGISTRY`. See the note on `MCP_ONLY_REGISTRY` for why that is a second
    # registry and not a filter over this one.
    mcp_only: bool = False


# Populated by `tools/__init__.py` at import time.
REGISTRY: dict[str, Tool] = {}

# Tools that exist on the MCP surface only (MIP-1070): the secretary's worklog and
# briefing, which a connected client's own model drives. A second registry rather
# than a flag every reader of REGISTRY has to remember to filter on, because the
# in-app agent reads REGISTRY in two different ways and both have to stay blind to
# these:
#   * to BUILD what the model sees: the tool declarations, the registry-generated
#     cheatsheet and tool directory (and its tool COUNT), the agency/reach tiers;
#   * to RUN what the model asks for: the controller looks a call up by the name
#     the model sent, not by what was declared, so a tool that merely is not
#     declared but is in REGISTRY would still execute if a model named it.
# A tool that is not in REGISTRY can do neither, however a new reader is written.
# What MCP-only tools need is for MCP to see them: it looks tools up through
# `get_mcp_tool`.
MCP_ONLY_REGISTRY: dict[str, Tool] = {}


# In-app tools that exist only while a `SEARCH_ENGINE` flag is on (MIP-1289). They are NOT in
# `REGISTRY` in either state, for the same reason MCP-only tools are not: every reader of
# `REGISTRY` (the tier gates, the categories test, the registry-generated prompt, the fingerprint)
# keeps seeing exactly the tools it saw before the flag existed, and a flag that is off leaves no
# trace anywhere. The flag is read at CALL time, not import time, because the eval harness
# switches flags on `settings.SEARCH_ENGINE` between arms inside one process.
#
# Name -> (flag, tool). The name MAY equal an `MCP_ONLY_REGISTRY` name: `get_timeline` is one
# capability on two surfaces, with a description and a schema written for each. Nothing mixes
# them up: MCP looks tools up through `get_mcp_tool` (`REGISTRY`, then `MCP_ONLY_REGISTRY`, never
# this), and the app reaches this registry only through `get_app_tool` / `flagged_tools`. A name
# in `REGISTRY` is refused (see `register_flagged_tool`).
FLAGGED_REGISTRY: dict[str, tuple[str, Tool]] = {}


def _flag_on(flag: str) -> bool:
    from django.conf import settings  # noqa: PLC0415 — keep this module importable before Django is set up

    return bool(settings.SEARCH_ENGINE.get(flag, False))


def flagged_tools() -> list[Tool]:
    """The flag-gated in-app tools whose flag is on right now, in registration order."""
    return [tool for flag, tool in FLAGGED_REGISTRY.values() if _flag_on(flag)]


def flagged_tool(name: str) -> Tool | None:
    """The flag-gated in-app tool called `name`, when its flag is on right now; else None."""
    entry = FLAGGED_REGISTRY.get(name)
    return entry[1] if entry is not None and _flag_on(entry[0]) else None


def get_app_tool(name: str) -> Tool | None:
    """The tool the in-app agent runs for `name`: a registered one, or a flag-gated one whose
    flag is on. A lookup by the name the MODEL sent, so a tool that is not declared is not
    runnable either."""
    tool = REGISTRY.get(name)
    return tool if tool is not None else flagged_tool(name)


def get_mcp_tool(name: str) -> Tool | None:
    """The tool the MCP surface runs for `name`: a shared one, or an MCP-only one."""
    return REGISTRY.get(name) or MCP_ONLY_REGISTRY.get(name)


def wrap_workspace_content(text: str) -> str:
    """Wrap free-text workspace content with a boundary marker.

    Used by the four read-only tools to mark every piece of
    user-authored text inside their return payload. The agent system
    prompt instructs the model to treat anything inside
    `<workspace_content>` as DATA, never as instructions — a structural
    mitigation against prompt-injection attacks where a malicious chat
    message or note body tries to override the agent's behavior.

    Text inside the value that looks like a fence tag (`</workspace_content>`,
    `</ web_content >`, ...) is escaped first, so it cannot close the fence
    early (MIP-927): see `agent/fence.py` for the scheme and how it is undone.

    Returns the input unchanged for empty / None values so we don't
    inflate payloads with empty boundary blocks.
    """
    if not text:
        return text
    return wrap(WORKSPACE_TAG, text)


def wrap_web_content(text: str) -> str:
    """`wrap_workspace_content` for text a web search fetched.

    A separate tag, not the workspace one, because the two are not equally
    trusted: anyone on the internet can write a page `search_web` returns,
    so the prompt rule for this tag also forbids acting on links or images
    it names (MIP-723). Its inner text is escaped by the same function, for both
    tags: a page can try to close this fence or the workspace one.
    """
    if not text:
        return text
    return wrap(WEB_TAG, text)
