"""Streaming RAG-answer endpoint: POST /api/v2/agent/ask.

Flow per request:

    1. Run hybrid search with `for_agent=True` to get entity-grouped
       results plus the full chunk text for each match.
    2. Stream Gemini's answer as NDJSON over a chunked HTTP response.
       Each line is a JSON object with a `type` field:

         {"type": "sources", "sources": [...entity rows...]}
         {"type": "answer_delta", "text": "..."}
         {"type": "answer_delta", "text": "..."}
         ...
         {"type": "done"}

       On error mid-stream:
         {"type": "error", "message": "..."}

NDJSON over a regular POST (rather than SSE / EventSource) is
deliberate: EventSource doesn't support POST, and we want POST so the
query payload isn't logged in URL access logs. The frontend reads the
body with a ReadableStream reader and splits on newlines.
"""

from __future__ import annotations

import json
import logging
from typing import Iterator

from django.conf import settings
from django.http import StreamingHttpResponse
from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.llm.gemini_client import generate_answer_stream
from origin.search_engine.search import search
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

        entity_types = data.get("entity_types") or None
        if entity_types is not None and not isinstance(entity_types, list):
            return Response(
                {"error": "entity_types must be a list of strings."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Run the search synchronously up front so we can ship the
        # source list to the UI as the first NDJSON line, before the
        # (slower) LLM call kicks off.
        try:
            context_chunks = int(
                data.get("context_chunks", settings.SEARCH_ENGINE["AGENT_CONTEXT_CHUNKS"])
            )
        except (TypeError, ValueError):
            return Response(
                {"error": "context_chunks must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            search_result = search(
                query=query,
                team_id=str(team_id),
                user_id=user_id,
                entity_types=entity_types,
                limit=context_chunks,
                for_agent=True,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("Search failed for agent ask")
            return Response(
                {"error": f"Search failed: {e}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        # Hand both `agent_results` (with full chunk text — for the LLM
        # prompt) and `ui_results` (snippet-only — for the frontend's
        # source citations) to the generator. We strip `chunks` out of
        # the UI payload so the user-facing JSON stays small and
        # matches the shape of /api/v2/search/.
        agent_results = search_result["results"]
        ui_results = [{k: v for k, v in r.items() if k != "chunks"} for r in agent_results]

        stream = _stream_ndjson(query, agent_results, ui_results)
        # text/event-stream-ish; NDJSON is content-type
        # `application/x-ndjson`. We keep `Cache-Control: no-cache` so
        # an intermediate proxy doesn't buffer.
        response = StreamingHttpResponse(stream, content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"  # disable nginx buffering if present
        return response


def _stream_ndjson(
    query: str, agent_results: list[dict], ui_results: list[dict]
) -> Iterator[bytes]:
    """Yield NDJSON lines: sources first, then answer deltas, then done."""

    def line(obj: dict) -> bytes:
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

    yield line({"type": "sources", "sources": ui_results})

    try:
        for delta in generate_answer_stream(query, agent_results):
            if delta:
                yield line({"type": "answer_delta", "text": delta})
        yield line({"type": "done"})
    except Exception as e:  # noqa: BLE001 — surface to client, don't 500
        log.exception("Gemini stream failed mid-flight")
        yield line({"type": "error", "message": str(e)})
