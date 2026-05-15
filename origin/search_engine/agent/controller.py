"""Multi-step agent loop driven by Gemini function-calling.

Architecture (matches the plan):

    POST /api/v2/agent/ask/
      ↓
    AgentController.run(query, ctx, emit)
      ↓
      for step in 0..MAX_STEPS:
        gemini.generate_step(messages, tools)
          → may emit (text, None) or (None, function_call)
        if final answer (text only):
          stream answer_delta events → emit done; return
        else:
          for each function_call:
            REGISTRY[name].run(args, ctx)
              → emit tool_call_start / tool_call_result / tool_call_error
              → append assistant function-call + function-response turns
        loop continues
      ↓
      if step cap hit: emit error

The controller is intentionally I/O-callback-driven (the caller passes
in `emit(dict)`) so the view layer can wrap each emitted event in
NDJSON. Same interface works for tests (capture into a list).
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from django.conf import settings

from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError
from origin.search_engine.llm.gemini_client import generate_step

log = logging.getLogger(__name__)


def _build_tool_declarations():
    """Translate each registered Tool into a `genai.types.Tool`."""
    from google.genai import types  # noqa: PLC0415

    declarations = [
        types.FunctionDeclaration(
            name=t.name,
            description=t.description,
            parameters=t.parameters_schema,
        )
        for t in REGISTRY.values()
    ]
    return [types.Tool(function_declarations=declarations)]


def _user_turn(query: str):
    from google.genai import types  # noqa: PLC0415

    return types.Content(role="user", parts=[types.Part(text=query)])


def _assistant_function_call_turn(function_call):
    from google.genai import types  # noqa: PLC0415

    return types.Content(role="model", parts=[types.Part(function_call=function_call)])


def _function_response_turn(name: str, response: dict[str, Any]):
    from google.genai import types  # noqa: PLC0415

    return types.Content(
        role="user",
        parts=[
            types.Part.from_function_response(name=name, response=response),
        ],
    )


def _ui_source_for_match(match: dict[str, Any]) -> dict[str, Any]:
    """Shape a search-tool match into the UI's `sources` event payload.

    Mirrors the snippet-only entity shape `/api/v2/search/` returns so
    the frontend's existing citation-chip code works unchanged.
    """
    return {
        "entity_type": match.get("entity_type"),
        "entity_id": match.get("entity_id"),
        "title": match.get("title"),
        "snippet": match.get("snippet"),
        "chat_type": match.get("chat_type"),
        "chat_id": match.get("chat_id"),
        "thread_id": match.get("thread_id"),
        "task_id": match.get("task_id"),
        "note_id": match.get("note_id"),
        "note_type": match.get("note_type"),
        "project_id": match.get("project_id"),
        # The UI also expects these fields from the Phase 2 shape:
        "matched_chunk_types": [],
        "score": 0.0,
        "related_entity_ids": [],
        "updated_at": None,
        "keyword_rank": None,
        "vector_rank": None,
    }


def run_agent(
    query: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Drive the agent loop. `emit(event_dict)` pushes NDJSON events out.

    Event types this function emits (see the plan / docs for the full
    NDJSON protocol):
      * tool_call_start  — step, tool_name, arguments
      * tool_call_result — step, tool_name, summary
      * tool_call_error  — step, tool_name, error
      * sources          — citation chips (after each search call)
      * answer_delta     — incremental text from the final answer
      * done             — stream finished cleanly
      * error            — fatal (e.g. step cap reached)
    """
    max_steps = int(settings.SEARCH_ENGINE.get("AGENT_MAX_STEPS", 5))
    tools = _build_tool_declarations()

    messages = [_user_turn(query)]
    # entity_id (per type) → ui_source dict. De-dups across multiple
    # searches in one run so the UI only ever sees a given entity once.
    seen_sources_by_id: dict[tuple, dict[str, Any]] = {}

    for step in range(max_steps):
        accumulated_function_calls = []
        any_text_emitted = False

        try:
            stream = generate_step(
                messages=messages,
                tools=tools,
                system_instruction=AGENT_SYSTEM_PROMPT,
            )
            for text_chunk, function_call in stream:
                if function_call is not None:
                    accumulated_function_calls.append(function_call)
                elif text_chunk:
                    # Note: per the plan, we keep text deltas even on
                    # tool-calling steps. If the model emits a stray
                    # "Let me search…" preface, the user sees it. If
                    # it's annoying in practice, suppress text when a
                    # function_call accompanies it in the same step.
                    any_text_emitted = True
                    emit({"type": "answer_delta", "text": text_chunk})
        except Exception as e:  # noqa: BLE001 — surface as stream error
            log.exception("Agent step %d Gemini call failed", step)
            emit({"type": "error", "message": f"Gemini call failed: {e}"})
            return

        if not accumulated_function_calls:
            # Pure text turn → final answer was streamed above.
            if not any_text_emitted:
                # No text and no tool call: model gave us nothing.
                # Avoid hanging the UI on "thinking…" forever.
                emit(
                    {
                        "type": "error",
                        "message": "Model returned an empty response.",
                    }
                )
                return
            emit({"type": "done"})
            return

        # Execute every requested call. Gemini may batch multiple per
        # step; we run them in order and append all responses before
        # looping.
        for call in accumulated_function_calls:
            call_args = dict(getattr(call, "args", {}) or {})
            call_name = getattr(call, "name", "")
            emit(
                {
                    "type": "tool_call_start",
                    "step": step,
                    "tool_name": call_name,
                    "arguments": call_args,
                }
            )

            tool = REGISTRY.get(call_name)
            if tool is None:
                err = f"Unknown tool: {call_name}"
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step,
                        "tool_name": call_name,
                        "error": err,
                    }
                )
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

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
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": str(e)}))
                continue
            except Exception as e:  # noqa: BLE001 — unexpected, log full trace
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
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

            # Pop the human-readable summary before handing the result
            # to the LLM (the model doesn't need our UI label).
            summary = result.pop("__summary__", "ok")
            emit(
                {
                    "type": "tool_call_result",
                    "step": step,
                    "tool_name": call_name,
                    "summary": summary,
                }
            )

            # If this was a search, promote results to citation chips.
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

    # Step cap hit without a final answer.
    emit(
        {
            "type": "error",
            "message": f"Agent did not reach a final answer in {max_steps} steps.",
        }
    )
