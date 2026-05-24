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

from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT
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


def _friendly_chat_title(
    ctx: ToolContext,
    chat_type_label: Any,
    chat_id: Any,
) -> str | None:
    """Resolve a viewer-facing chat title.

    Chat docs are indexed once (not per-viewer), so the chunker can only
    write a viewer-agnostic placeholder like "DM 9". Real titles depend
    on context: a DM's name is the OTHER participant; a PM's name is
    the project's; GM / MDM use the group/display name. Resolved here
    at read time using ctx.user_id so each viewer sees a name they
    recognise.

    Returns None on lookup failure so the caller keeps whatever was
    already on the source (typically the indexed placeholder).
    """
    if not chat_type_label or not chat_id:
        return None
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return None

    label = str(chat_type_label).lower()

    # Lazy imports — keep the controller's import block tidy and avoid
    # circular-import surprises during Django startup.
    if label == "dm":
        from origin.models.chat.dm_models import DMMaster
        from origin.models.common.user_models import CustomUser

        try:
            dm = DMMaster.objects.get(dm_id=cid)
        except DMMaster.DoesNotExist:
            return None
        partner_id = dm.user_2_id if str(dm.user_1_id) == ctx.user_id else dm.user_1_id
        if not partner_id:
            return None
        try:
            user = CustomUser.objects.get(id=partner_id)
        except CustomUser.DoesNotExist:
            return None
        return user.username or None

    if label == "gm":
        from origin.models.chat.gm_models import GMMaster

        try:
            return GMMaster.objects.get(gm_id=cid).group_name or None
        except GMMaster.DoesNotExist:
            return None

    if label == "mdm":
        from origin.models.chat.mdm_models import MDMMaster

        try:
            return MDMMaster.objects.get(mdm_id=cid).display_name or None
        except MDMMaster.DoesNotExist:
            return None

    if label == "pm":
        from origin.models.project.prj_models import ProjectMaster

        try:
            # For PM chats the chat_id is the project_id (see fetch_chat_thread).
            return ProjectMaster.objects.get(project_id=cid).project_name or None
        except ProjectMaster.DoesNotExist:
            return None

    return None


def _apply_friendly_titles(
    sources: list[dict[str, Any]], ctx: ToolContext
) -> list[dict[str, Any]]:
    """Replace placeholder chat titles ('DM 9', 'Project 5') with viewer-friendly names.

    Best-effort: lookup failures leave the original title in place so a
    missing partner / soft-deleted chat doesn't blank out the chip.
    """
    for src in sources:
        if src.get("entity_type") != "chat":
            continue
        title = _friendly_chat_title(ctx, src.get("chat_type"), src.get("chat_id"))
        if title:
            src["title"] = title
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


def _task_source(task_id: Any, title: Any, project_id: Any) -> dict[str, Any]:
    s = _blank_source("task", f"task:{task_id}")
    s["title"] = title or ""
    s["task_id"] = str(task_id) if task_id is not None else None
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
            _task_source(t.get("task_id"), t.get("title"), t.get("project_id"))
            for t in (result.get("tasks") or [])
            if t.get("task_id")
        ]

    if call_name == "fetch_task":
        tid = result.get("task_id")
        if not tid:
            return []
        return [_task_source(tid, result.get("title"), result.get("project_id"))]

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
    disabled_tools: set[str] | None = None,
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
    for prior_query, prior_answer in prior_turns or []:
        messages.append(_user_turn(prior_query))
        messages.append(AgentMessage(role="assistant", text=prior_answer))
    messages.append(_user_turn(query))
    return _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=emit,
        run_id=run_id,
        starting_step=0,
        seen_sources_by_id={},
        disabled_tools=disabled_tools,
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
