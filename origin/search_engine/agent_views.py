"""Streaming agent endpoints.

Two endpoints, both streaming NDJSON over POST:

    POST /api/v2/agent/ask/      — start a fresh agent run
    POST /api/v2/agent/decide/   — resume a run paused on a write tool

Phase 3 introduced the multi-step Gemini/Claude function-calling loop;
Phase 7 adds the pause/resume protocol for tools with
`requires_approval=True`. Phase 8 adds conversation memory via
`AgentSession` — the frontend sends an optional `session_id` with
each /ask/ call; the view prepends the last SESSION_MAX_PRIOR_TURNS
Q&A pairs into the model context.

NDJSON event types emitted:

    {"type": "tool_call_start",            "step": N, "tool_name": "...", "arguments": {...}}
    {"type": "tool_call_result",           "step": N, "tool_name": "...", "summary": "..."}
    {"type": "tool_call_error",            "step": N, "tool_name": "...", "error": "..."}
    {"type": "tool_call_pending_approval", "step": N, "tool_name": "...", "arguments": {...},
                                           "approval_token": "<uuid>"}   ← Phase 7
    {"type": "sources",                    "sources": [...]}
    {"type": "answer_delta",               "text": "..."}
    {"type": "done",                       "session_id": "<uuid>"}       ← Phase 8
    {"type": "error",                      "message": "..."}

POST instead of SSE so query payloads aren't logged in access logs.
`StreamingHttpResponse(application/x-ndjson)` flushes each event
incrementally; nginx buffering disabled via header.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Callable, Iterator

from django.conf import settings
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.feature_models import UserFeatureAccess
from origin.search_engine.agent.controller import resume_agent, run_agent
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.models import AgentRun, AgentSession
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

log = logging.getLogger(__name__)

# Answer truncation for session history — keeps the context budget bounded.
_PRIOR_ANSWER_MAX_CHARS = 400


# --------------------------------------------------------------------------- #
# Session helpers (Phase 8)                                                   #
# --------------------------------------------------------------------------- #


def _get_or_create_session(
    session_id_str: str | None,
    team_id: str,
    user_id: str,
) -> AgentSession:
    """Return an existing live session or create a fresh one.

    If `session_id_str` points to a valid session that still belongs to
    this user/team and hasn't expired, touch its `last_active_at` and
    return it. Otherwise (DoesNotExist, wrong owner, expired) silently
    create a new session.
    """
    ttl_minutes = int(settings.SEARCH_ENGINE.get("SESSION_TTL_MINUTES", 30))
    if session_id_str:
        try:
            session = AgentSession.objects.get(
                session_id=session_id_str,
                team_id=team_id,
                user_id=user_id,
            )
            cutoff = timezone.now() - timedelta(minutes=ttl_minutes)
            if session.last_active_at >= cutoff:
                AgentSession.objects.filter(session_id=session.session_id).update(
                    last_active_at=timezone.now()
                )
                session.last_active_at = timezone.now()
                return session
        except (AgentSession.DoesNotExist, ValueError):
            pass
    return AgentSession.objects.create(team_id=team_id, user_id=user_id)


def _load_prior_turns(session: AgentSession, max_turns: int) -> list[tuple[str, str]]:
    """Return the last `max_turns` (query, answer) pairs from the session.

    Only includes runs that have a non-empty `final_answer_text` (i.e.
    the model produced an actual answer — done, rejected, etc.). Each
    answer is truncated to `_PRIOR_ANSWER_MAX_CHARS` to keep the
    context budget predictable.
    """
    runs = (
        AgentRun.objects.filter(session=session)
        .exclude(final_answer_text="")
        .order_by("-started_at")[:max_turns]
    )
    return [(r.query, r.final_answer_text[:_PRIOR_ANSWER_MAX_CHARS]) for r in reversed(list(runs))]


# --------------------------------------------------------------------------- #
# /ask/ — start a fresh run                                                   #
# --------------------------------------------------------------------------- #


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

        # --- Daily usage limit (free tier). ---
        # Unlimited-agent subscribers bypass this check entirely.
        # Set AGENT_FREE_DAILY_LIMIT=0 in env to disable for everyone.
        daily_limit = int(settings.SEARCH_ENGINE.get("AGENT_FREE_DAILY_LIMIT", 5))
        if daily_limit > 0 and not UserFeatureAccess.user_has(
            user_id, UserFeatureAccess.FEATURE_UNLIMITED_AGENT
        ):
            today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
            used_today = AgentRun.objects.filter(
                user_id=user_id,
                started_at__gte=today_start,
            ).count()
            if used_today >= daily_limit:
                return Response(
                    {
                        "error": (
                            f"You've used all {daily_limit} AI asks for today. "
                            "Upgrade your plan to keep going."
                        ),
                        "limit_reached": True,
                        "used": used_today,
                        "limit": daily_limit,
                    },
                    status=status.HTTP_429_TOO_MANY_REQUESTS,
                )

        # Phase 8 — session memory. Non-fatal: if session machinery
        # fails for any reason we fall back to a stateless single-turn.
        session: AgentSession | None = None
        prior_turns: list[tuple[str, str]] = []
        session_id_str = (data.get("session_id") or "").strip() or None
        max_prior_turns = int(settings.SEARCH_ENGINE.get("SESSION_MAX_PRIOR_TURNS", 3))
        try:
            session = _get_or_create_session(session_id_str, str(team_id), user_id)
            prior_turns = _load_prior_turns(session, max_prior_turns)
        except Exception:  # noqa: BLE001
            log.exception("Session load failed; continuing without memory")

        # Persist one AgentRun row per /ask/ call. Failures here are
        # logged but never break the user-facing response.
        run: AgentRun | None = None
        try:
            run = AgentRun.objects.create(
                team_id=str(team_id),
                user_id=user_id,
                query=query,
                session=session,
            )
        except Exception:  # noqa: BLE001
            log.exception("Failed to create AgentRun row; continuing without persistence")

        def worker(emit):
            return run_agent(
                query,
                ctx,
                emit,
                run_id=run.run_id if run else None,
                prior_turns=prior_turns,
            )

        stream = _stream_ndjson(
            worker,
            run=run,
            session_id=session.session_id if session else None,
        )
        response = StreamingHttpResponse(stream, content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


# --------------------------------------------------------------------------- #
# /decide/ — resume a paused run                                              #
# --------------------------------------------------------------------------- #


class AgentDecideView(AuthenticatedAPIView):
    def post(self, request):
        data = request.data or {}

        run_id = (data.get("run_id") or "").strip()
        approval_token = (data.get("approval_token") or "").strip()
        decision = (data.get("decision") or "").strip().lower()

        if not run_id:
            return Response({"error": "run_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        if not approval_token:
            return Response(
                {"error": "approval_token is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if decision not in ("approve", "reject"):
            return Response(
                {"error": "decision must be 'approve' or 'reject'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            run = AgentRun.objects.get(run_id=run_id)
        except AgentRun.DoesNotExist:
            return Response({"error": "run not found."}, status=status.HTTP_404_NOT_FOUND)

        # AuthZ: the user resuming the run must be the one who started
        # it. Also enforces tenant isolation (token alone isn't enough).
        request_user_id = str(getattr(request.user, "id", "")) or data.get("user_id")
        if not request_user_id or request_user_id != run.user_id:
            return Response(
                {"error": "Not authorized to resume this run."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if run.status != "awaiting_approval":
            return Response(
                {"error": f"run is not awaiting approval (status={run.status})."},
                status=status.HTTP_409_CONFLICT,
            )
        if str(run.pending_approval_token) != approval_token:
            return Response(
                {"error": "approval_token does not match."},
                status=status.HTTP_403_FORBIDDEN,
            )

        # Consume the token immediately — single-shot. From now on this
        # run is "running" again; if we crash mid-resume the status
        # reflects that rather than leaving the row half-stuck.
        try:
            run.pending_approval_token = None
            run.status = "running"
            run.save(update_fields=["pending_approval_token", "status"])
        except Exception:  # noqa: BLE001
            log.exception("Failed to consume approval token for run %s", run.run_id)

        # Touch session last_active_at so the approval round-trip
        # doesn't count against the TTL window.
        if run.session_id:
            try:
                AgentSession.objects.filter(session_id=run.session_id).update(
                    last_active_at=timezone.now()
                )
            except Exception:  # noqa: BLE001
                pass

        ctx = ToolContext(team_id=run.team_id, user_id=run.user_id)

        def worker(emit):
            return resume_agent(run, decision, ctx, emit)

        stream = _stream_ndjson(
            worker,
            run=run,
            rejected=(decision == "reject"),
            append_to_existing_answer=True,
            session_id=run.session_id,
        )
        response = StreamingHttpResponse(stream, content_type="application/x-ndjson")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        return response


# --------------------------------------------------------------------------- #
# Shared streaming adapter                                                    #
# --------------------------------------------------------------------------- #


def _stream_ndjson(
    worker_target: Callable[[Callable[[dict], None]], dict | None],
    *,
    run: AgentRun | None = None,
    rejected: bool = False,
    append_to_existing_answer: bool = False,
    session_id=None,
) -> Iterator[bytes]:
    """Bridge a controller callback into chunked NDJSON.

    `worker_target(emit)` is the controller function to run on a
    background thread. It must call `emit(event_dict)` for each
    NDJSON line it wants to send and return either `None` (clean
    finish) or a `{"paused": True, "approval_token": UUID, ...}`
    descriptor when the loop is paused on a write tool.

    `session_id`, when present, is injected into the `done` event
    as `"session_id"`. The frontend uses this value in subsequent
    /ask/ calls to thread conversation history (Phase 8).

    `run`, when present, is closed at end-of-stream:
        * `paused=True`     → status="awaiting_approval", token stored
        * `rejected=True`   → status="rejected" (only if pause didn't fire)
        * clean text done   → status="done", final_answer_text saved
        * fatal error       → status="error"
        * step cap          → status="step_cap"

    `append_to_existing_answer=True` makes the resume path concatenate
    its `answer_delta` events onto the run's existing `final_answer_text`
    rather than overwriting (the first `/ask/` call already wrote some
    text for the paused step).
    """
    import queue  # noqa: PLC0415
    import threading  # noqa: PLC0415

    def line(obj: dict) -> bytes:
        return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

    event_q: "queue.Queue[dict | None]" = queue.Queue()
    pause_descriptor: dict | None = None

    def emit(event: dict) -> None:
        event_q.put(event)

    def worker():
        nonlocal pause_descriptor
        try:
            pause_descriptor = worker_target(emit)
        except Exception as e:  # noqa: BLE001
            log.exception("Agent worker crashed")
            event_q.put({"type": "error", "message": f"Agent crashed: {e}"})
        finally:
            event_q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    answer_parts: list[str] = []
    final_status: str | None = None
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
            # Inject session_id so the frontend can thread the next ask.
            if session_id is not None:
                event = {**event, "session_id": str(session_id)}
        elif event_type == "error":
            msg = event.get("message") or ""
            final_error = msg
            final_status = "step_cap" if "did not reach a final answer" in msg else "error"
        yield line(event)

    # Decide the row's final state. Pause beats every other outcome —
    # if the controller paused, we don't care if it also emitted some
    # text first; the run is "awaiting_approval" until /decide/ fires.
    if run is None:
        return

    try:
        if pause_descriptor and pause_descriptor.get("paused"):
            run.status = "awaiting_approval"
            run.pending_approval_token = pause_descriptor["approval_token"]
            if answer_parts:
                if append_to_existing_answer:
                    run.final_answer_text = (run.final_answer_text or "") + "".join(answer_parts)
                else:
                    run.final_answer_text = "".join(answer_parts)
            run.save(
                update_fields=[
                    "status",
                    "pending_approval_token",
                    "final_answer_text",
                ]
            )
            return

        # Terminal close.
        if final_status is None:
            final_status = "rejected" if rejected else "error"
        run.status = final_status
        new_text = "".join(answer_parts)
        if append_to_existing_answer and new_text:
            run.final_answer_text = (run.final_answer_text or "") + new_text
        elif new_text:
            run.final_answer_text = new_text
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


# --------------------------------------------------------------------------- #
# /usage/ — daily usage info for the current user                             #
# --------------------------------------------------------------------------- #


class AgentUsageView(AuthenticatedAPIView):
    """GET /api/v2/agent/usage/

    Returns today's AI-ask count and the plan limit so the frontend can
    display a "N of M asks used today" indicator without waiting for the
    next /ask/ call to fail.

    Response schema:
        {
            "used":         int,          # asks started today (UTC day)
            "limit":        int | null,   # null means unlimited
            "is_unlimited": bool
        }
    """

    def get(self, request):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        daily_limit = int(settings.SEARCH_ENGINE.get("AGENT_FREE_DAILY_LIMIT", 5))
        is_unlimited = (
            daily_limit == 0
            or UserFeatureAccess.user_has(user_id, UserFeatureAccess.FEATURE_UNLIMITED_AGENT)
        )

        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        used_today = AgentRun.objects.filter(
            user_id=user_id,
            started_at__gte=today_start,
        ).count()

        return Response(
            {
                "used": used_today,
                "limit": None if is_unlimited else daily_limit,
                "is_unlimited": is_unlimited,
            }
        )
