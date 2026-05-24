"""Multi-step agent loop, with Phase 7 pause/resume for write tools.

Two entry points share the same per-step loop body:

  * `run_agent(query, ctx, emit, run_id=...)` — fresh run from a user
    query. Returns `None` on a clean finish, or a dict
    `{"paused": True, "approval_token": UUID, ...}` when the loop hit
    a `requires_approval` tool. The caller (view layer) is expected
    to write the token back onto `AgentRun.pending_approval_token`
    and flip `AgentRun.status` to `"awaiting_approval"`.

  * `resume_agent(run, decision, ctx, emit)` — resume a paused run.
    `decision` is `"approve"` or `"reject"`. Reconstructs the
    `messages` list from persisted `AgentStep` rows, executes (or
    rejects) the pending tool, and continues the loop. Same return
    shape as `run_agent` (could pause again on a subsequent write
    tool, though current tools don't chain that way).

Event types emitted (full NDJSON protocol):

  tool_call_start              read-only tool dispatch
  tool_call_result             read-only tool success
  tool_call_error              tool error (incl. user-rejected writes)
  tool_call_pending_approval   write tool — paused, awaiting user
  sources                      citation chips (after search calls)
  answer_delta                 streaming text from the final answer
  done                         final answer delivered
  error                        fatal mid-stream
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Callable
from uuid import UUID

from django.conf import settings

from origin.search_engine.agent.prompts import (
    AGENT_SELF_CRITIQUE_PROMPT_TEMPLATE,
    AGENT_SELF_CRITIQUE_SYSTEM,
    AGENT_SYSTEM_PROMPT,
)
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.llm import (
    AgentMessage,
    FunctionCall,
    ToolDeclaration,
    get_model_client,
)
from origin.search_engine.models import AgentRun, AgentStep

log = logging.getLogger(__name__)

# Marker stored in AgentStep.summary while a write tool is awaiting the
# user's decision. The resume path uses it to locate the pending row.
PENDING_APPROVAL_MARKER = "awaiting_approval"

# Decision strings accepted by `resume_agent`.
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def _persist_step(run_id: UUID | None, **fields: Any) -> AgentStep | None:
    """Best-effort write of one `AgentStep` row.

    Observability must NEVER break the user-facing path — if the DB
    insert fails for any reason, log it and move on. The agent stream
    completes regardless. Returns the saved row (or None if persistence
    is disabled / failed) so the pause path can update it later.
    """
    if run_id is None:
        return None
    try:
        return AgentStep.objects.create(run_id=run_id, **fields)
    except Exception:  # noqa: BLE001 — must not fail the response stream
        log.exception("Failed to persist AgentStep for run %s", run_id)
        return None


def _build_tool_declarations(
    disabled_tools: set[str] | None = None,
) -> list[ToolDeclaration]:
    """Translate each registered Tool into a provider-neutral declaration.

    Tools whose name appears in `disabled_tools` are omitted from the
    list, so the model never even sees them as callable. Currently used
    to honour the frontend "Web search" toggle (filters out
    `search_web`).
    """
    disabled = disabled_tools or set()
    return [
        ToolDeclaration(
            name=t.name,
            description=t.description,
            parameters_schema=t.parameters_schema,
        )
        for t in REGISTRY.values()
        if t.name not in disabled
    ]


def _user_turn(query: str) -> AgentMessage:
    return AgentMessage(role="user", text=query)


def _assistant_function_call_turn(function_call: FunctionCall) -> AgentMessage:
    return AgentMessage(role="assistant", function_call=function_call)


def _function_response_turn(name: str, response: dict[str, Any]) -> AgentMessage:
    return AgentMessage(
        role="tool_response",
        function_response_name=name,
        function_response=response,
    )


_WORKSPACE_OPEN = "<workspace_content>\n"
_WORKSPACE_CLOSE = "\n</workspace_content>"


def _strip_workspace_marker(s: str | None) -> str | None:
    """Reverse of `wrap_workspace_content` for UI-bound snippet text."""
    if not s:
        return s
    if s.startswith(_WORKSPACE_OPEN) and s.endswith(_WORKSPACE_CLOSE):
        return s[len(_WORKSPACE_OPEN) : -len(_WORKSPACE_CLOSE)]
    return s


def _ui_source_for_match(match: dict[str, Any]) -> dict[str, Any]:
    """Shape a search-tool match into the UI's `sources` event payload.

    Mirrors the `SpotlightResult` shape returned by `/api/v2/search/`
    so the frontend can hand the source chip directly to the same
    `handleSpotlightSelect` router that the search-result rows use.
    Two fields are essential for routing parity:

      * `message_id` — lets a chat citation deep-link to the exact
        bubble that matched (not just the chat/thread).
      * `related_entity_ids` — fallback the frontend reads when chunks
        pre-date direct `task_id` / `chat_*` fields on note rows. Older
        chat-note / task-note chunks only carry their parent entity in
        this list, so dropping it breaks routing for unupgraded data.
    """
    return {
        "entity_type": match.get("entity_type"),
        "entity_id": match.get("entity_id"),
        "title": match.get("title"),
        "snippet": _strip_workspace_marker(match.get("snippet")),
        "chat_type": match.get("chat_type"),
        "chat_id": match.get("chat_id"),
        "thread_id": match.get("thread_id"),
        "message_id": match.get("message_id"),
        "task_id": match.get("task_id"),
        # Human-readable task ID ("<project.code>-<project_task_number>",
        # e.g. "PRJ-42"). Hydrated by `_hydrate_task_display_ids` after
        # the source list is built — the OpenSearch index doesn't carry it.
        "task_display_id": None,
        "note_id": match.get("note_id"),
        "note_type": match.get("note_type"),
        "project_id": match.get("project_id"),
        "matched_chunk_types": list(match.get("matched_chunk_types") or []),
        "matched_terms": list(match.get("matched_terms") or []),
        "related_entity_ids": list(match.get("related_entity_ids") or []),
        "updated_at": match.get("updated_at"),
        # These ranking fields are search-result-only; the agent never
        # ranks sources itself. Defaults keep the shape uniform so the
        # frontend doesn't have to branch on agent-vs-search origin.
        "score": 0.0,
        "keyword_rank": None,
        "vector_rank": None,
    }


from origin.search_engine.friendly_titles import (
    apply_friendly_titles as _resolve_chat_titles,
)


def _apply_friendly_titles(
    sources: list[dict[str, Any]], ctx: ToolContext
) -> list[dict[str, Any]]:
    """Replace placeholder chat titles ('DM 9') with viewer-friendly names.

    Thin adapter over the shared `friendly_titles.apply_friendly_titles`
    helper — kept so the in-loop call signature stays terse and so
    structured-tool sources (which don't go through `search()`) still
    get title resolution before chip emission.
    """
    return _resolve_chat_titles(sources, ctx.user_id)


def _hydrate_task_display_ids(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backfill `task_display_id` for task sources that don't already have one.

    `_task_source` (called from structured tools like list_tasks /
    fetch_task) sets display_id directly because the tool result already
    carries it. `_ui_source_for_match` (search-knowledge-base path) does
    NOT — the OpenSearch index stores only the raw task_id. We resolve
    those missing ones here with one batched DB query.
    """
    missing_ids: list[int] = []
    for src in sources:
        if src.get("entity_type") != "task" or src.get("task_display_id"):
            continue
        raw = src.get("task_id")
        if raw is None:
            continue
        try:
            missing_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    if not missing_ids:
        return sources

    from origin.models.task.task_models import TaskMaster

    by_id: dict[int, str] = {}
    for t in TaskMaster.objects.select_related("project").filter(task_id__in=missing_ids):
        by_id[t.task_id] = t.display_id

    for src in sources:
        if src.get("entity_type") != "task" or src.get("task_display_id"):
            continue
        try:
            tid = int(src.get("task_id"))
        except (TypeError, ValueError):
            continue
        if tid in by_id:
            src["task_display_id"] = by_id[tid]
    return sources


def _blank_source(entity_type: str, entity_id: str) -> dict[str, Any]:
    """Skeleton source dict; structured-tool helpers fill in the type-specific fields."""
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": None,
        "snippet": None,
        "chat_type": None,
        "chat_id": None,
        "thread_id": None,
        "message_id": None,
        "task_id": None,
        "task_display_id": None,
        "note_id": None,
        "note_type": None,
        "project_id": None,
        "matched_chunk_types": [],
        "matched_terms": [],
        "related_entity_ids": [],
        "updated_at": None,
        "score": 0.0,
        "keyword_rank": None,
        "vector_rank": None,
    }


def _task_source(
    task_id: Any, title: Any, project_id: Any, display_id: Any = None
) -> dict[str, Any]:
    s = _blank_source("task", f"task:{task_id}")
    s["title"] = title or ""
    s["task_id"] = str(task_id) if task_id is not None else None
    s["task_display_id"] = display_id or None
    s["project_id"] = str(project_id) if project_id is not None else None
    return s


def _project_source(project_id: Any, project_name: Any) -> dict[str, Any]:
    s = _blank_source("project", f"project:{project_id}")
    s["title"] = project_name or ""
    s["project_id"] = str(project_id) if project_id is not None else None
    return s


def _chat_source(
    chat_type: Any,
    chat_id: Any,
    thread_id: Any = None,
    title: Any = None,
) -> dict[str, Any]:
    # Chunker convention: entity_id has no leading "chat:" prefix.
    base = f"{chat_type}:{chat_id}"
    eid = f"{base}:thread:{thread_id}" if thread_id else base
    s = _blank_source("chat", eid)
    s["title"] = title or ""
    s["chat_type"] = chat_type
    s["chat_id"] = str(chat_id) if chat_id is not None else None
    s["thread_id"] = str(thread_id) if thread_id else None
    return s


def _note_source(
    note_type: Any,
    note_id: Any,
    title: Any = None,
    parent_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    s = _blank_source("note", f"note:{note_type}:{note_id}")
    s["title"] = title or ""
    s["note_id"] = str(note_id) if note_id is not None else None
    s["note_type"] = note_type
    pc = parent_context or {}
    s["project_id"] = pc.get("project_id")
    s["task_id"] = pc.get("task_id")
    s["chat_type"] = pc.get("chat_type")
    s["chat_id"] = pc.get("chat_id")
    s["thread_id"] = pc.get("thread_id")
    return s


def _ui_sources_from_tool_result(call_name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build UI source dicts from a non-search read tool's result.

    Returns [] for tools whose results don't map to a clickable entity
    (e.g. analytics aggregations without per-row ids, get_current_user,
    get_team_members — no user-detail view exists to link to).

    Sources are deduped upstream by (entity_type, entity_id), so emitting
    the same task from both `list_tasks` and `search_knowledge_base` in
    one run only produces a single chip.
    """
    if not isinstance(result, dict):
        return []

    if call_name in ("list_tasks", "get_stale_tasks"):
        return [
            _task_source(
                t.get("task_id"),
                t.get("title"),
                t.get("project_id"),
                display_id=t.get("display_id"),
            )
            for t in (result.get("tasks") or [])
            if t.get("task_id")
        ]

    if call_name == "fetch_task":
        tid = result.get("task_id")
        if not tid:
            return []
        return [
            _task_source(
                tid,
                result.get("title"),
                result.get("project_id"),
                display_id=result.get("display_id"),
            )
        ]

    if call_name == "list_projects":
        return [
            _project_source(p.get("project_id"), p.get("project_name"))
            for p in (result.get("projects") or [])
            if p.get("project_id")
        ]

    if call_name == "get_project_summary":
        pid = result.get("project_id")
        if not pid:
            return []
        return [_project_source(pid, result.get("project_name"))]

    if call_name == "fetch_chat_thread":
        chat_type = result.get("chat_type")
        chat_id = result.get("chat_id")
        if not chat_type or not chat_id:
            return []
        return [_chat_source(chat_type, chat_id, result.get("thread_id"))]

    if call_name == "fetch_note":
        nid = result.get("note_id")
        ntype = result.get("note_type")
        if not nid or not ntype:
            return []
        return [_note_source(ntype, nid, result.get("title"), result.get("parent_context"))]

    return []


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


def run_agent(
    query: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    *,
    run_id: UUID | None = None,
    prior_turns: list[tuple[str, str]] | None = None,
    prior_summary: str | None = None,
    disabled_tools: set[str] | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Drive the agent loop from a fresh user query.

    `prior_turns` is an ordered list of (user_query, assistant_answer)
    pairs from earlier turns in the same session (Phase 8). When
    present they are prepended to the messages list so the model can
    resolve references like "that task" or "the note you mentioned".
    Each answer is already truncated to ~400 chars by the view layer
    to keep the context budget bounded.

    Returns:
        None on clean completion (text answer, error, or step cap).
        A pause descriptor when the loop hits a write tool:
            {
                "paused": True,
                "approval_token": UUID,
                "step": int,
                "tool_name": str,
                "arguments": dict,
            }
        The view layer reflects the pause back onto the `AgentRun` row.
    """
    messages: list[AgentMessage] = []
    # Phase 3.5 — rolling summary of earlier turns prepended as an
    # assistant "note to self" so the model can reference topics that
    # have fallen out of the verbatim prior_turns window. Cheap, opt-in
    # context recovery for long sessions. See `multi_turn.py`.
    if prior_summary:
        messages.append(
            AgentMessage(
                role="assistant",
                text=f"[Context recap from earlier in this conversation: {prior_summary}]",
            )
        )
    for prior_query, prior_answer in prior_turns or []:
        messages.append(_user_turn(prior_query))
        messages.append(AgentMessage(role="assistant", text=prior_answer))
    messages.append(_user_turn(query))

    # Phase 3.2 — optional self-critique pass. Dispatched here so the
    # resume_agent path (write-tool approval flow) is NOT critiqued;
    # critique only makes sense on a complete, un-paused turn.
    if settings.SEARCH_ENGINE.get("RAG_AGENT_SELF_CRITIQUE", False):
        return _drive_loop_with_critique(
            user_query=query,
            messages=messages,
            ctx=ctx,
            emit=emit,
            run_id=run_id,
            starting_step=0,
            seen_sources_by_id={},
            disabled_tools=disabled_tools,
            trace_hook=trace_hook,
        )
    return _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=emit,
        run_id=run_id,
        starting_step=0,
        seen_sources_by_id={},
        disabled_tools=disabled_tools,
        trace_hook=trace_hook,
    )


def resume_agent(
    run: AgentRun,
    decision: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any] | None:
    """Resume a paused agent run after the user has approved or rejected.

    Reconstructs the conversation up to the pending tool call from
    `AgentStep` rows, executes (approve) or synthesizes a rejection
    (reject) for that one tool, then continues the loop. Returns
    `None` on completion or another pause descriptor if the resumed
    run hits a second write tool.
    """
    if decision not in (DECISION_APPROVE, DECISION_REJECT):
        emit(
            {
                "type": "error",
                "message": f"Invalid decision {decision!r} (expected 'approve' or 'reject').",
            }
        )
        return None

    messages, pending_step = _rebuild_messages(run)
    if pending_step is None:
        emit(
            {
                "type": "error",
                "message": "No pending tool call found on this run.",
            }
        )
        return None

    step_index = pending_step.step_index
    call_name = pending_step.tool_name
    call_args = dict(pending_step.arguments_json or {})
    function_call = FunctionCall(name=call_name, args=call_args)

    # Emit the start event the original run skipped. Same step index so
    # the frontend can correlate the approve/reject card with the row
    # that's now actually executing.
    emit(
        {
            "type": "tool_call_start",
            "step": step_index,
            "tool_name": call_name,
            "arguments": call_args,
        }
    )

    if decision == DECISION_REJECT:
        err = "User rejected this action."
        emit(
            {
                "type": "tool_call_error",
                "step": step_index,
                "tool_name": call_name,
                "error": err,
            }
        )
        try:
            pending_step.error = "user_rejected"
            pending_step.summary = ""
            pending_step.save(update_fields=["error", "summary"])
        except Exception:  # noqa: BLE001
            log.exception("Failed to update pending step %s on reject", pending_step.step_id)
        messages.append(_assistant_function_call_turn(function_call))
        messages.append(_function_response_turn(call_name, {"error": "user_rejected"}))
    else:
        # APPROVE — actually run the tool now.
        tool = REGISTRY.get(call_name)
        if tool is None:
            err = f"Unknown tool: {call_name}"
            emit(
                {
                    "type": "tool_call_error",
                    "step": step_index,
                    "tool_name": call_name,
                    "error": err,
                }
            )
            try:
                pending_step.error = err
                pending_step.summary = ""
                pending_step.save(update_fields=["error", "summary"])
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to update pending step %s on unknown tool", pending_step.step_id
                )
            messages.append(_assistant_function_call_turn(function_call))
            messages.append(_function_response_turn(call_name, {"error": err}))
        else:
            try:
                result = tool.run(call_args, ctx)
            except ToolError as e:
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step_index,
                        "tool_name": call_name,
                        "error": str(e),
                    }
                )
                try:
                    pending_step.error = str(e)
                    pending_step.summary = ""
                    pending_step.save(update_fields=["error", "summary"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after ToolError", pending_step.step_id
                    )
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, {"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                log.exception("Tool %s crashed on args %r", call_name, call_args)
                err = f"Internal error in tool '{call_name}'."
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step_index,
                        "tool_name": call_name,
                        "error": err,
                    }
                )
                try:
                    pending_step.error = err
                    pending_step.summary = ""
                    pending_step.save(update_fields=["error", "summary"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after exception", pending_step.step_id
                    )
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, {"error": err}))
            else:
                summary = result.pop("__summary__", "ok")
                emit(
                    {
                        "type": "tool_call_result",
                        "step": step_index,
                        "tool_name": call_name,
                        "summary": summary,
                    }
                )
                try:
                    pending_step.summary = summary
                    pending_step.result_json = result
                    pending_step.save(update_fields=["summary", "result_json"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after approve",
                        pending_step.step_id,
                    )
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, result))

    # Continue the loop from the next step. The original run wrote
    # steps 0..step_index inclusive, so we resume at step_index + 1.
    return _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=emit,
        run_id=run.run_id,
        starting_step=step_index + 1,
        seen_sources_by_id={},  # `sources` events were sent in the original stream; don't double-emit.
    )


# --------------------------------------------------------------------------- #
# Shared loop body                                                            #
# --------------------------------------------------------------------------- #


def _drive_loop(
    *,
    messages: list[AgentMessage],
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    run_id: UUID | None,
    starting_step: int,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    disabled_tools: set[str] | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """The core agent loop, shared by `run_agent` and `resume_agent`.

    Returns `None` on completion, or a pause descriptor on hitting a
    write tool. See `run_agent` for the descriptor shape.
    """
    max_steps = int(settings.SEARCH_ENGINE.get("AGENT_MAX_STEPS", 5))
    client = get_model_client()
    tools = _build_tool_declarations(disabled_tools)

    for step in range(starting_step, max_steps):
        accumulated_function_calls: list[FunctionCall] = []
        accumulated_text_parts: list[str] = []

        try:
            stream = client.generate_step(
                messages=messages,
                tools=tools,
                system_instruction=AGENT_SYSTEM_PROMPT,
            )
            for text_chunk, function_call in stream:
                if function_call is not None:
                    accumulated_function_calls.append(function_call)
                elif text_chunk:
                    accumulated_text_parts.append(text_chunk)
                    emit({"type": "answer_delta", "text": text_chunk})
        except Exception as e:  # noqa: BLE001 — surface as stream error
            log.exception("Agent step %d Gemini call failed", step)
            emit({"type": "error", "message": f"Gemini call failed: {e}"})
            _persist_step(run_id, step_index=step, error=f"Gemini call failed: {e}")
            return None

        any_text_emitted = bool(accumulated_text_parts)

        if any_text_emitted:
            _persist_step(
                run_id,
                step_index=step,
                answer_text="".join(accumulated_text_parts),
            )

        if not accumulated_function_calls:
            if not any_text_emitted:
                emit(
                    {
                        "type": "error",
                        "message": "Model returned an empty response.",
                    }
                )
                _persist_step(run_id, step_index=step, error="empty_response")
                return None
            emit({"type": "done"})
            return None

        for call in accumulated_function_calls:
            call_args = dict(call.args)
            call_name = call.name

            tool = REGISTRY.get(call_name)
            if tool is None:
                emit(
                    {
                        "type": "tool_call_start",
                        "step": step,
                        "tool_name": call_name,
                        "arguments": call_args,
                    }
                )
                err = f"Unknown tool: {call_name}"
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step,
                        "tool_name": call_name,
                        "error": err,
                    }
                )
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    error=err,
                )
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

            # ---- Phase 7: write tools pause the loop ----
            if getattr(tool, "requires_approval", False):
                approval_token = uuid.uuid4()
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    summary=PENDING_APPROVAL_MARKER,
                )
                # `run_id` is included so the frontend has everything it
                # needs to POST `/decide/`. When `run_id` is None (eval
                # / test paths) we omit the field rather than serialize
                # a `null` that the wire schema doesn't expect.
                event: dict[str, Any] = {
                    "type": "tool_call_pending_approval",
                    "step": step,
                    "tool_name": call_name,
                    "arguments": call_args,
                    "approval_token": str(approval_token),
                }
                if run_id is not None:
                    event["run_id"] = str(run_id)
                emit(event)
                return {
                    "paused": True,
                    "approval_token": approval_token,
                    "step": step,
                    "tool_name": call_name,
                    "arguments": call_args,
                }

            # ---- Read-only tool: run it inline ----
            emit(
                {
                    "type": "tool_call_start",
                    "step": step,
                    "tool_name": call_name,
                    "arguments": call_args,
                }
            )

            try:
                result = tool.run(call_args, ctx)
            except ToolError as e:
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step,
                        "tool_name": call_name,
                        "error": str(e),
                    }
                )
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    error=str(e),
                )
                if trace_hook is not None:
                    try:
                        trace_hook(call_name, call_args, {"error": str(e)})
                    except Exception:  # noqa: BLE001
                        log.exception("trace_hook failed for tool %s (error path)", call_name)
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": str(e)}))
                continue
            except Exception as e:  # noqa: BLE001
                log.exception("Tool %s crashed on args %r", call_name, call_args)
                err = f"Internal error in tool '{call_name}'."
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step,
                        "tool_name": call_name,
                        "error": err,
                    }
                )
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    error=err,
                )
                if trace_hook is not None:
                    try:
                        trace_hook(call_name, call_args, {"error": err})
                    except Exception:  # noqa: BLE001
                        log.exception("trace_hook failed for tool %s (crash path)", call_name)
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

            summary = result.pop("__summary__", "ok")
            emit(
                {
                    "type": "tool_call_result",
                    "step": step,
                    "tool_name": call_name,
                    "summary": summary,
                }
            )
            _persist_step(
                run_id,
                step_index=step,
                tool_name=call_name,
                arguments_json=call_args,
                summary=summary,
                result_json=result,
            )
            if trace_hook is not None:
                try:
                    trace_hook(call_name, call_args, result)
                except Exception:  # noqa: BLE001 — trace hook must never break the loop
                    log.exception("trace_hook failed for tool %s", call_name)

            # Collect citation chips from this tool's result. Search produces
            # them via _ui_source_for_match (one per match); structured read
            # tools produce them via _ui_sources_from_tool_result. Both feed
            # the same dedup map so a task surfaced by both list_tasks and
            # search_knowledge_base in one run is still a single chip.
            new_sources: list[dict[str, Any]] = []
            if call_name == "search_knowledge_base":
                new_sources = [_ui_source_for_match(m) for m in result.get("matches", [])]
            else:
                new_sources = _ui_sources_from_tool_result(call_name, result)

            # Swap viewer-agnostic placeholders ("DM 9") for friendly
            # titles (partner / group / project name) before chips ship.
            _apply_friendly_titles(new_sources, ctx)
            # Backfill PRJ-123 display ids for search-result task sources
            # (the index stores raw task_id only).
            _hydrate_task_display_ids(new_sources)

            added = False
            for src in new_sources:
                key = (src.get("entity_type"), src.get("entity_id"))
                if not all(key) or key in seen_sources_by_id:
                    continue
                seen_sources_by_id[key] = src
                added = True

            if added:
                emit(
                    {
                        "type": "sources",
                        "sources": list(seen_sources_by_id.values()),
                    }
                )

            messages.append(_assistant_function_call_turn(call))
            messages.append(_function_response_turn(call_name, result))

    # Step cap.
    emit(
        {
            "type": "error",
            "message": f"Agent did not reach a final answer in {max_steps} steps.",
        }
    )
    _persist_step(run_id, step_index=max_steps, error="step_cap_reached")
    return None


# --------------------------------------------------------------------------- #
# Phase 3.2 — self-critique reflection wrapper                                #
# --------------------------------------------------------------------------- #


def _drive_loop_with_critique(
    *,
    user_query: str,
    messages: list[AgentMessage],
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    run_id: UUID | None,
    starting_step: int,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    disabled_tools: set[str] | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
) -> dict[str, Any] | None:
    """Run `_drive_loop` with captured events, then optionally rewrite
    the draft answer via a single self-critique LLM call.

    Wrapper design (intentionally NOT inside `_drive_loop`): the inner
    loop is untouched and remains the canonical control path. The
    wrapper buffers events, runs a critique pass, then replays events
    to the real `emit` with the draft answer possibly swapped for a
    revised version.

    Precision-tightening only — the critique cannot fire more tool
    calls in this MVP. If a recall gap turns out to be the bottleneck
    on a future suite, extend the critique prompt to allow emitting
    a query the loop then executes.

    Tradeoff: TTFT becomes "end of loop + critique" because all
    answer_delta events are buffered. Acceptable for an experimental
    flag (off by default). Production rollout should weigh streaming
    vs. precision wins.

    Pause path (write-tool approval) is passed through unchanged — the
    `_drive_loop` returns a pause descriptor and the wrapper flushes
    captured events as-is. Critique never fires on a paused run.
    """
    captured_events: list[dict[str, Any]] = []
    captured_tool_results: list[dict[str, Any]] = []

    def _capture_emit(event: dict[str, Any]) -> None:
        captured_events.append(event)

    def _capture_trace(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        captured_tool_results.append({"tool_name": name, "arguments": args, "result": result})
        # Also forward to the caller's trace_hook if any (e.g. the eval runner).
        if trace_hook is not None:
            try:
                trace_hook(name, args, result)
            except Exception:  # noqa: BLE001
                log.exception("Outer trace_hook failed inside critique wrapper for %s", name)

    pause_descriptor = _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=_capture_emit,
        run_id=run_id,
        starting_step=starting_step,
        seen_sources_by_id=seen_sources_by_id,
        disabled_tools=disabled_tools,
        trace_hook=_capture_trace,
    )

    if pause_descriptor is not None:
        # Loop paused on a write tool; do not critique. Flush as captured.
        for e in captured_events:
            emit(e)
        return pause_descriptor

    draft_answer = "".join(
        (e.get("text") or "") for e in captured_events if e.get("type") == "answer_delta"
    )
    if not draft_answer.strip():
        # No final answer to critique (e.g. step-cap, fatal error).
        for e in captured_events:
            emit(e)
        return None

    try:
        revised = _run_self_critique(
            user_query=user_query,
            tool_results=captured_tool_results,
            draft=draft_answer,
        )
    except Exception:  # noqa: BLE001 — never break the loop on critique failure
        log.exception("Self-critique LLM call failed; emitting draft unchanged")
        for e in captured_events:
            emit(e)
        return None

    if revised is None or _critique_says_keep(revised):
        # KEEP path — flush as captured.
        for e in captured_events:
            emit(e)
        return None

    # Revise path — replay everything except the draft answer_delta
    # events, then emit the revised answer once (just before `done`).
    revised_emitted = False
    for e in captured_events:
        etype = e.get("type")
        if etype == "answer_delta":
            # Drop the draft text.
            continue
        if etype == "done" and not revised_emitted:
            emit({"type": "answer_delta", "text": revised})
            revised_emitted = True
        emit(e)
    # Defensive: if there was no `done` event in the capture (shouldn't
    # happen for a clean termination) but we have a revision, surface it.
    if not revised_emitted:
        emit({"type": "answer_delta", "text": revised})
        emit({"type": "done"})
    return None


def _critique_says_keep(text: str) -> bool:
    """Recognise the literal KEEP signal. Anything else is a revision.

    Strict: only `"KEEP"` (case-insensitive) plus optional surrounding
    whitespace counts. If the model writes "KEEP, but actually …" or
    "Looks good — KEEP", treat it as a revision so we don't accidentally
    suppress a corrective rewrite.
    """
    return text.strip().upper() == "KEEP"


def _run_self_critique(
    *,
    user_query: str,
    tool_results: list[dict[str, Any]],
    draft: str,
) -> str | None:
    """Run one self-critique LLM call. Returns the model's text response
    (which may be the literal `KEEP` or a revised final answer).
    """
    prompt = AGENT_SELF_CRITIQUE_PROMPT_TEMPLATE.format(
        user_query=user_query,
        tool_summary=_format_tool_results_for_critique(tool_results),
        draft=draft,
    )
    client = get_model_client()
    chunks: list[str] = []
    for text, _fcall in client.generate_step(
        messages=[AgentMessage(role="user", text=prompt)],
        tools=[],
        system_instruction=AGENT_SELF_CRITIQUE_SYSTEM,
    ):
        if text:
            chunks.append(text)
    out = "".join(chunks).strip()
    return out or None


# Limits for the tool-result blob we hand to the critique LLM. The
# critique only needs the scalar fields (status / due_date / counts) to
# verify the draft — long comment bodies don't carry weight. Mirrors
# the same per-string / per-list caps the eval judge uses, but inlined
# here to keep controller / eval coupling at zero.
_CRITIQUE_MAX_STRING_LEN = 500
_CRITIQUE_MAX_LIST_LEN = 30


def _format_tool_results_for_critique(tool_results: list[dict[str, Any]]) -> str:
    """Compact, size-bounded JSON-ish rendering for the critique prompt."""
    import json  # local — only loaded when the critique fires

    lines: list[str] = []
    for i, tr in enumerate(tool_results, start=1):
        name = tr.get("tool_name") or "?"
        args = json.dumps(tr.get("arguments") or {}, ensure_ascii=False, default=str)
        result = json.dumps(
            _truncate_for_critique(tr.get("result") or {}), ensure_ascii=False, default=str
        )
        lines.append(f"  {i}. {name}({args})\n     result: {result}")
    return "\n".join(lines) if lines else "  (no tool calls)"


def _truncate_for_critique(value: Any) -> Any:
    """Recursively head-tail long strings and cap long lists.
    Scalars (numbers, bools, dates) pass through verbatim — those are
    where the critique's grounding checks land.
    """
    if isinstance(value, str):
        if len(value) > _CRITIQUE_MAX_STRING_LEN:
            half = _CRITIQUE_MAX_STRING_LEN // 2
            return f"{value[:half]} … [{len(value) - _CRITIQUE_MAX_STRING_LEN} chars elided] … {value[-half:]}"
        return value
    if isinstance(value, list):
        truncated = [_truncate_for_critique(v) for v in value[:_CRITIQUE_MAX_LIST_LEN]]
        if len(value) > _CRITIQUE_MAX_LIST_LEN:
            truncated.append(f"… [{len(value) - _CRITIQUE_MAX_LIST_LEN} more items elided]")
        return truncated
    if isinstance(value, dict):
        return {k: _truncate_for_critique(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Resume helpers                                                              #
# --------------------------------------------------------------------------- #


def _rebuild_messages(run: AgentRun) -> tuple[list[AgentMessage], AgentStep | None]:
    """Reconstruct the conversation up to (but not including) the pending step.

    The pending step is the one whose `summary == PENDING_APPROVAL_MARKER`
    and which has neither `result_json` nor `error` filled in yet.
    Returns (messages, pending_step). `pending_step` is None if there's
    no row matching the pending shape — the caller should treat that
    as an error.
    """
    messages: list[AgentMessage] = [_user_turn(run.query)]
    pending: AgentStep | None = None

    for step in run.steps.order_by("step_index", "step_id"):
        # Pending-write rows are skipped here — we hand them back to
        # the caller to resolve.
        if (
            step.tool_name
            and step.summary == PENDING_APPROVAL_MARKER
            and not step.result_json
            and not step.error
        ):
            pending = step
            continue

        # Text-only assistant turns.
        if step.answer_text and not step.tool_name:
            messages.append(AgentMessage(role="assistant", text=step.answer_text))
            continue

        # Completed tool calls (success OR error). Skip rows that have
        # no tool_name — they're error markers like "empty_response".
        if not step.tool_name:
            continue

        fc = FunctionCall(
            name=step.tool_name,
            args=dict(step.arguments_json or {}),
        )
        messages.append(_assistant_function_call_turn(fc))
        if step.error:
            messages.append(_function_response_turn(step.tool_name, {"error": step.error}))
        else:
            messages.append(_function_response_turn(step.tool_name, step.result_json or {}))

    return messages, pending
