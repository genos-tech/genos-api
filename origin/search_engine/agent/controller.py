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


def _build_tool_declarations() -> list[ToolDeclaration]:
    """Translate each registered Tool into a provider-neutral declaration."""
    return [
        ToolDeclaration(
            name=t.name,
            description=t.description,
            parameters_schema=t.parameters_schema,
        )
        for t in REGISTRY.values()
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
    """Shape a search-tool match into the UI's `sources` event payload."""
    return {
        "entity_type": match.get("entity_type"),
        "entity_id": match.get("entity_id"),
        "title": match.get("title"),
        "snippet": _strip_workspace_marker(match.get("snippet")),
        "chat_type": match.get("chat_type"),
        "chat_id": match.get("chat_id"),
        "thread_id": match.get("thread_id"),
        "task_id": match.get("task_id"),
        "note_id": match.get("note_id"),
        "note_type": match.get("note_type"),
        "project_id": match.get("project_id"),
        "matched_chunk_types": [],
        "score": 0.0,
        "related_entity_ids": [],
        "updated_at": None,
        "keyword_rank": None,
        "vector_rank": None,
    }


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


def run_agent(
    query: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    *,
    run_id: UUID | None = None,
) -> dict[str, Any] | None:
    """Drive the agent loop from a fresh user query.

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
    messages: list[AgentMessage] = [_user_turn(query)]
    return _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=emit,
        run_id=run_id,
        starting_step=0,
        seen_sources_by_id={},
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
) -> dict[str, Any] | None:
    """The core agent loop, shared by `run_agent` and `resume_agent`.

    Returns `None` on completion, or a pause descriptor on hitting a
    write tool. See `run_agent` for the descriptor shape.
    """
    max_steps = int(settings.SEARCH_ENGINE.get("AGENT_MAX_STEPS", 5))
    client = get_model_client()
    tools = _build_tool_declarations()

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

            if call_name == "search_knowledge_base":
                for match in result.get("matches", []):
                    key = (match.get("entity_type"), match.get("entity_id"))
                    if key in seen_sources_by_id:
                        continue
                    seen_sources_by_id[key] = _ui_source_for_match(match)
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
