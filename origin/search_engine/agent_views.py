"""Streaming agent endpoint: POST /api/v2/agent/ask.

Phase 3: this endpoint now drives a multi-step Gemini function-calling
loop instead of a fixed RAG pipeline. The model decides when to
search and when to fetch detail. See `agent.controller.run_agent` for
the loop body.

NDJSON event types emitted (see `agent/controller.py` for full list):

    {"type": "tool_call_start", "step": N, "tool_name": "...", "arguments": {...}}
    {"type": "tool_call_result", "step": N, "tool_name": "...", "summary": "..."}
    {"type": "tool_call_error",  "step": N, "tool_name": "...", "error": "..."}
    {"type": "sources", "sources": [...]}        // after each search_knowledge_base call
    {"type": "answer_delta", "text": "..."}      // tokens of the final answer
    {"type": "done"}
    {"type": "error", "message": "..."}

Wire format is identical to Phase 2 plus the three new tool events,
so the existing frontend NDJSON parser still works (older event types
are still emitted) — only the AnswerPanel UI needs to grow a
tool-progress strip to render the new events.

POST instead of SSE so the query payload isn't logged in access logs,
and `StreamingHttpResponse` over `application/x-ndjson` so each event
hits the client incrementally.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.agent.controller import run_agent
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.models import AgentRun
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

log = logging.getLogger(__name__)


class AgentAskView(AuthenticatedAPIView):
    def post(self, request):
        data = request.data or {}

        query = (data.get("query") or "").strip()
        team_id = data.get("team_id")

        if not query:
            return Response(
                {"error": "query is required and must be non-empty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user_id = str(getattr(request.user, "id", "")) or data.get("user_id")
        if not user_id:
            return Response(
                {"error": "Could not determine user_id from the auth token."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ctx = ToolContext(team_id=str(team_id), user_id=user_id)

        # Phase 4: persist one AgentRun row per /ask/ call so we can
        # post-mortem any answer after the stream closes. Per-step rows
        # are written by the controller as events fire. Failure to
        # persist must never break the user-facing response — caught and
        # logged here; the request still streams as normal.
        run = None
        try:
            run = AgentRun.objects.create(
                team_id=str(team_id),
                user_id=user_id,
                query=query,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to create AgentRun row; continuing without persistence")

        stream = _stream_ndjson(query, ctx, run=run)
        response = StreamingHttpResponse(stream, content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # disable nginx buffering if present
        return response


def _stream_ndjson(
    query: str,
    ctx: ToolContext,
    *,
    run: AgentRun | None = None,
) -> Iterator[bytes]:
    """Adapter: bridge `run_agent`'s `emit(dict)` callback to NDJSON bytes.

    The controller wants a synchronous `emit(event)` callback. We
    can't `yield` from inside a callback, so we run the controller on
    a background thread and have it push events into a queue that the
    HTTP-response generator drains. This way each event hits the
    client as soon as the controller produces it — no batching at the
    Django layer.

    When `run` is provided, the final `AgentRun` status / final answer
    text / error message are written once the controller exits.
    """
    import queue  # noqa: PLC0415
    import threading  # noqa: PLC0415

    def line(obj: dict) -> bytes:
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

    event_q: "queue.Queue[dict | None]" = queue.Queue()

    def emit(event: dict) -> None:
        event_q.put(event)

    run_id = run.run_id if run is not None else None

    def worker():
        try:
            run_agent(query, ctx, emit, run_id=run_id)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent run crashed")
            event_q.put({"type": "error", "message": f"Agent crashed: {e}"})
        finally:
            event_q.put(None)  # sentinel: stream is done

    threading.Thread(target=worker, daemon=True).start()

    # Collect terminal state by peeking at events as they pass through.
    # The model's `answer_delta` chunks are concatenated into the final
    # answer; the outcome (done / error / step_cap) is inferred from
    # the last terminal event.
    answer_parts: list[str] = []
    final_status = "error"
    final_error = ""

    while True:
        event = event_q.get()
        if event is None:
            break
        event_type = event.get("type")
        if event_type == "answer_delta":
            text = event.get("text") or ""
            if text:
                answer_parts.append(text)
        elif event_type == "done":
            final_status = "done"
        elif event_type == "error":
            msg = event.get("message") or ""
            final_error = msg
            if "did not reach a final answer" in msg:
                final_status = "step_cap"
            else:
                final_status = "error"
        yield line(event)

    # Close the AgentRun row. Best-effort: a DB failure here is logged
    # but doesn't affect the already-finished response.
    if run is not None:
        try:
            run.status = final_status
            run.final_answer_text = "".join(answer_parts)
            run.error_message = final_error
            run.finished_at = timezone.now()
            run.save(
                update_fields=[
                    "status",
                    "final_answer_text",
                    "error_message",
                    "finished_at",
                ]
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to close AgentRun %s", run.run_id)
