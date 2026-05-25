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
from typing import Any, Callable, Iterator

from django.conf import settings
from django.db.models import Prefetch
from django.http import StreamingHttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from origin.search_engine.agent.controller import (
    _ui_source_for_match,
    _ui_sources_from_tool_result,
    resume_agent,
    run_agent,
)
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.choice import (
    LlmChoice,
    resolve_user_choice,
    reset_llm_choice,
    set_llm_choice,
)
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.search_engine.quota import (
    LLM_ASK_KEY,
    WEB_SEARCH_KEY,
    check_remaining,
    get_quota,
    get_used_today,
    get_user_tier,
    increment_usage,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

log = logging.getLogger(__name__)

# Answer truncation for session history — keeps the context budget bounded.
_PRIOR_ANSWER_MAX_CHARS = 400

# Phase 3.5 — upper bound on how many prior turns we'll load when
# `RAG_SESSION_ROLLING_SUMMARY` is on. The session TTL (default 30 min)
# realistically caps active sessions well below this, but we set a hard
# ceiling so a runaway session can't blow up the summary prompt.
_ROLLING_SUMMARY_LOAD_CAP = 20


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

        # --- Tier-based daily quotas. ---
        # Two pre-flight checks: total LLM asks for the day (LLM_ASK_KEY)
        # AND the user's chosen per-model count. Either failing returns
        # 429 with the existing payload shape, plus a `category` field so
        # the frontend can render the right message. Numbers come from
        # SEARCH_ENGINE["TIER_QUOTAS"][user.tier]. A None limit means
        # "no quota applies" (treated as unlimited).
        chosen = resolve_user_choice(
            request.user.preferred_llm_provider,
            request.user.preferred_llm_model,
        )

        llm_ok, llm_used, llm_limit = check_remaining(user_id, LLM_ASK_KEY)
        if not llm_ok:
            return Response(
                {
                    "error": (
                        f"You've used all {llm_limit} AI asks for today. "
                        "Upgrade your plan to keep going."
                    ),
                    "limit_reached": True,
                    "used": llm_used,
                    "limit": llm_limit,
                    "category": "llm_ask",
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        model_ok, model_used, model_limit = check_remaining(user_id, chosen.model)
        if not model_ok:
            return Response(
                {
                    "error": (
                        f"You've used all {model_limit} {chosen.model} asks for today. "
                        "Switch to another model or upgrade your plan to keep going."
                    ),
                    "limit_reached": True,
                    "used": model_used,
                    "limit": model_limit,
                    "category": "model",
                    "model": chosen.model,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Phase 8 — session memory. Non-fatal: if session machinery
        # fails for any reason we fall back to a stateless single-turn.
        # Phase 3.5 — when RAG_SESSION_ROLLING_SUMMARY is on, load up to
        # `_ROLLING_SUMMARY_LOAD_CAP` prior turns so the helper has the
        # full earlier history to summarise. Off-path keeps the original
        # tight load (just the verbatim window).
        session: AgentSession | None = None
        prior_turns_all: list[tuple[str, str]] = []
        prior_summary: str | None = None
        session_id_str = (data.get("session_id") or "").strip() or None
        max_prior_turns = int(settings.SEARCH_ENGINE.get("SESSION_MAX_PRIOR_TURNS", 3))
        rolling_summary = bool(settings.SEARCH_ENGINE.get("RAG_SESSION_ROLLING_SUMMARY", False))
        load_cap = _ROLLING_SUMMARY_LOAD_CAP if rolling_summary else max_prior_turns
        try:
            session = _get_or_create_session(session_id_str, str(team_id), user_id)
            prior_turns_all = _load_prior_turns(session, load_cap)
            from origin.search_engine.agent.multi_turn import build_prior_context  # noqa: PLC0415

            prior_turns, prior_summary = build_prior_context(prior_turns_all)
        except Exception:  # noqa: BLE001
            log.exception("Session load failed; continuing without memory")
            prior_turns = []

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

        # Per-request tool gates from the frontend Spotlight preferences.
        # `allow_web_search` defaults to True so older clients that omit
        # the field get the same behavior as before.
        disabled_tools: set[str] = set()
        if data.get("allow_web_search") is False:
            disabled_tools.add("search_web")

        # `chosen` is captured in the worker closure so the contextvar
        # is set inside the controller's threading.Thread — a bare
        # thread does NOT inherit contextvars from its parent.
        def worker(emit):
            token = set_llm_choice(chosen)
            try:
                return run_agent(
                    query,
                    ctx,
                    emit,
                    run_id=run.run_id if run else None,
                    prior_turns=prior_turns,
                    prior_summary=prior_summary,
                    disabled_tools=disabled_tools,
                )
            finally:
                reset_llm_choice(token)

        stream = _stream_ndjson(
            worker,
            run=run,
            session_id=session.session_id if session else None,
            # Increment BOTH the per-model and the LLM-ask total counter
            # on the first answer_delta of the stream. Sub-calls (query
            # rewriter, reranker) share the user's chosen model but do
            # NOT count toward quota — only the user-initiated ask does.
            user_id_for_quota=user_id,
            quota_keys=[LLM_ASK_KEY, chosen.model],
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

        # Resolve the user's LLM choice for the resumed leg. No quota
        # increment here — the original /ask/ call already counted; a
        # resume after tool approval is a continuation of the same ask.
        # Note: this re-reads the user's *current* preference, not the
        # one in effect when the original /ask/ ran. If the user opens
        # Settings and changes their model between the pause and the
        # resume, the second leg uses the new model. Approval round-
        # trips are typically seconds, so this is effectively never a
        # problem in practice; it's also the principle-of-least-surprise
        # behavior — the user's *current* preference is what counts.
        resumed_choice = resolve_user_choice(
            request.user.preferred_llm_provider,
            request.user.preferred_llm_model,
        )

        def worker(emit):
            token = set_llm_choice(resumed_choice)
            try:
                return resume_agent(run, decision, ctx, emit)
            finally:
                reset_llm_choice(token)

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
    user_id_for_quota: str | None = None,
    quota_keys: list[str] | None = None,
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
    # Quota counter — fired once on the first non-empty answer_delta.
    # Sub-tool LLM calls and empty-response failures don't charge.
    # Guarded with a flag so a stream of N tokens still counts as 1.
    # Each key in `quota_keys` is incremented atomically (LLM_ASK total
    # AND the chosen per-model counter both bump together).
    quota_charged = False

    while True:
        event = event_q.get()
        if event is None:
            break
        event_type = event.get("type")
        if event_type == "answer_delta":
            text = event.get("text") or ""
            if text:
                answer_parts.append(text)
                if not quota_charged and user_id_for_quota and quota_keys:
                    for key in quota_keys:
                        increment_usage(user_id_for_quota, key)
                    quota_charged = True
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


def _tier_limit_block(user_id: str, key: str) -> dict:
    """Helper: return `{"used": int, "limit": int|null}` for one quota
    dimension, used by AgentUsageView / AgentFeaturesView / AgentModelsView."""
    _, used, limit = check_remaining(user_id, key)
    return {"used": used, "limit": limit}


class AgentUsageView(AuthenticatedAPIView):
    """GET /api/v2/agent/usage/

    Returns today's LLM-ask count + per-tier daily limit so the
    frontend can display a "N of M asks used today" indicator without
    waiting for the next /ask/ call to fail. Tier comes from
    `CustomUser.tier`.

    Response schema:
        {
            "used":         int,          # LLM asks completed today (UTC day)
            "limit":        int | null,   # null means unlimited for this tier
            "is_unlimited": bool          # convenience flag
        }
    """

    def get(self, request):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        block = _tier_limit_block(user_id, LLM_ASK_KEY)
        return Response(
            {
                "used": block["used"],
                "limit": block["limit"],
                "is_unlimited": block["limit"] is None,
            }
        )


class AgentFeaturesView(AuthenticatedAPIView):
    """GET /api/v2/agent/features/

    Returns the calling user's tier + the two cross-cutting daily
    quotas (LLM ask + web search). The frontend uses this to surface
    "your web search quota is exhausted" warnings up front instead of
    letting the user hit a mid-stream ToolError.

    Response schema:
        {
            "tier":       "free" | "pro" | "max",
            "llm_ask":    {"used": int, "limit": int | null},
            "web_search": {"used": int, "limit": int | null}
        }
    """

    def get(self, request):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)
        return Response(
            {
                "tier": get_user_tier(user_id),
                "llm_ask": _tier_limit_block(user_id, LLM_ASK_KEY),
                "web_search": _tier_limit_block(user_id, WEB_SEARCH_KEY),
            }
        )


class AgentModelsView(AuthenticatedAPIView):
    """GET /api/v2/agent/models/

    Returns the LLM provider/model catalog tailored for the calling
    user, including:
      - The user's resolved tier ('free' / 'pro' / 'max').
      - Their currently-effective `(provider, model)` after applying
        their saved preference + stale-pref fallback.
      - Per-model daily quota (`daily_limit`) and today's count
        (`used_today`), so the Settings UI can render
        "3 / 10 used today" rows without an extra round-trip.
      - The two cross-cutting daily quotas (LLM ask + web search), so
        the Settings UI can render those rows alongside per-model.

    Response schema:
        {
          "tier": "free" | "pro" | "max",
          "current": {"provider": "gemini", "model": "gemini-2.5-flash"},
          "models": [
            {"provider": "gemini", "model": "gemini-2.5-flash",
             "label": "...", "note": "...",
             "daily_limit": int | None,   # null = unlimited
             "used_today":  int},
            ...
          ],
          "limits": {
            "llm_ask":    {"used": int, "limit": int | null},
            "web_search": {"used": int, "limit": int | null}
          }
        }
    """

    def get(self, request):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        tier = get_user_tier(user_id)
        catalog = settings.SEARCH_ENGINE.get("MODEL_CATALOG") or []

        models_payload = []
        for entry in catalog:
            provider = entry.get("provider", "")
            model_name = entry.get("model", "")
            models_payload.append(
                {
                    "provider": provider,
                    "model": model_name,
                    "label": entry.get("label", model_name),
                    "note": entry.get("note", ""),
                    "daily_limit": get_quota(user_id, model_name),
                    "used_today": get_used_today(user_id, model_name),
                }
            )

        resolved = resolve_user_choice(
            request.user.preferred_llm_provider,
            request.user.preferred_llm_model,
        )

        # Picker fallback: if the resolved model isn't in the catalog
        # (e.g. an operator left `GEMINI_MODEL` pointing at a preview
        # model not listed in `MODEL_CATALOG`), substitute the first
        # catalog entry for the resolved provider so the frontend
        # `<Select>` has a matching `<Option>`. The agent loop still
        # uses the resolved value at request time — only the picker's
        # displayed selection is normalized.
        catalog_has_resolved = any(
            m["provider"] == resolved.provider and m["model"] == resolved.model
            for m in models_payload
        )
        if not catalog_has_resolved:
            same_provider = next(
                (m for m in models_payload if m["provider"] == resolved.provider),
                None,
            )
            if same_provider is None and models_payload:
                same_provider = models_payload[0]
            if same_provider is not None:
                resolved = LlmChoice(
                    provider=same_provider["provider"],
                    model=same_provider["model"],
                )

        return Response(
            {
                "tier": tier,
                "current": {"provider": resolved.provider, "model": resolved.model},
                "models": models_payload,
                "limits": {
                    "llm_ask": _tier_limit_block(user_id, LLM_ASK_KEY),
                    "web_search": _tier_limit_block(user_id, WEB_SEARCH_KEY),
                },
            }
        )


# Cap how many recent sessions the list endpoint returns. Keeps the
# response small on workspaces with deep history; the UI exposes only
# this many today (no search / no pagination — see roadmap §11).
_HISTORY_LIST_LIMIT = 20


def _reconstruct_sources_for_run(run: AgentRun) -> list[dict[str, Any]]:
    """Rebuild the same source list the live `/ask/` flow emitted for
    this run, replaying against persisted `AgentStep.result_json`.

    Walks the run's steps in `step_index` order and dispatches each
    one through the same per-tool source builders the live controller
    uses (`_ui_source_for_match` for `search_knowledge_base` matches,
    `_ui_sources_from_tool_result` for structured reads). Dedupes by
    `entity_id` so a task touched by both `list_tasks` and a
    follow-up `fetch_task` produces a single source row — matches the
    live `seen_sources_by_id` behavior in `_drive_loop`.

    Used by the History detail endpoint so inline citation tokens
    (e.g. `[task:200]`) in archived answers can resolve back to a
    clickable preview, the same way they do in the live conversation.
    """
    seen_by_id: dict[str, dict[str, Any]] = {}
    # `.steps` is already prefetched on the run by the caller; iterating
    # `.all()` here doesn't trigger another query.
    for step in run.steps.all():
        if not step.tool_name or step.result_json is None:
            continue
        result = step.result_json
        if step.tool_name == "search_knowledge_base":
            new_sources = [_ui_source_for_match(m) for m in (result.get("matches") or [])]
        else:
            new_sources = _ui_sources_from_tool_result(step.tool_name, result)
        for s in new_sources:
            eid = s.get("entity_id")
            if eid and eid not in seen_by_id:
                seen_by_id[eid] = s
    return list(seen_by_id.values())


class AgentSessionsListView(AuthenticatedAPIView):
    """GET /api/v2/agent/sessions/?team_id=<id>

    Lists this user's recent agent conversations within `team_id` so the
    frontend can render the History panel inside Spotlight. Read-only,
    ACL-scoped to (team_id, user_id) — never returns another user's
    sessions. Ordered by `-last_active_at`, capped at
    `_HISTORY_LIST_LIMIT` rows.

    Each row carries enough metadata to render a list item (relative
    timestamp + first-query preview + turn count) without fetching the
    full conversation. Click-through hits the detail endpoint below.

    Response schema:
        {
            "sessions": [
                {
                    "session_id":      "<uuid>",
                    "created_at":      "<iso>",
                    "last_active_at":  "<iso>",
                    "first_query":     "...",  # first run's query, possibly truncated
                    "turn_count":      int     # AgentRun count for this session
                },
                ...
            ]
        }
    """

    # Truncate the first-query preview to keep the list-row payload
    # small. Long queries get an ellipsis suffix — the detail view
    # has the full text.
    _FIRST_QUERY_PREVIEW_LEN = 140

    def get(self, request):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        sessions_qs = AgentSession.objects.filter(team_id=str(team_id), user_id=user_id).order_by(
            "-last_active_at"
        )[:_HISTORY_LIST_LIMIT]
        sessions = list(sessions_qs)
        if not sessions:
            return Response({"sessions": []})

        # Hydrate per-session metadata in one extra query each. With the
        # cap above this is at most 20 round-trips; on a real workspace
        # this is dominated by AgentRun read latency, not query count.
        # If history list latency ever matters, switch to a single
        # GROUP BY query. Not worth it at this scale.
        sessions_payload = []
        for s in sessions:
            runs_qs = AgentRun.objects.filter(session=s)
            turn_count = runs_qs.count()
            first_run = runs_qs.order_by("started_at").only("query").first()
            first_query = (first_run.query if first_run else "") or ""
            if len(first_query) > self._FIRST_QUERY_PREVIEW_LEN:
                first_query = first_query[: self._FIRST_QUERY_PREVIEW_LEN].rstrip() + "…"
            sessions_payload.append(
                {
                    "session_id": str(s.session_id),
                    "created_at": s.created_at.isoformat(),
                    "last_active_at": s.last_active_at.isoformat(),
                    "first_query": first_query,
                    "turn_count": turn_count,
                }
            )

        return Response({"sessions": sessions_payload})


class AgentSessionDetailView(AuthenticatedAPIView):
    """GET /api/v2/agent/sessions/<session_id>/?team_id=<id>

    Returns the full Q&A trace for one past session so the frontend can
    render a read-only archive view inside Spotlight. ACL-scoped to
    (team_id, user_id) — a UUID guess returns 404, not someone else's
    conversation.

    Only runs with a final answer OR an error message are returned —
    in-flight runs (status="running" / "awaiting_approval") and runs
    that wrote no answer at all are filtered out. This keeps the
    read-only archive coherent: every visible row is a completed
    exchange.

    `sources` on each turn is rebuilt from the persisted
    `AgentStep.result_json` so inline `[task:N]` / `[chat:...]` /
    `[note:...]` / `[project:N]` tokens in archived answers resolve
    to clickable previews via the same `rewriteCitations` machinery
    the live view uses.

    Response schema:
        {
            "session_id":     "<uuid>",
            "created_at":     "<iso>",
            "last_active_at": "<iso>",
            "turns": [
                {
                    "run_id":     "<uuid>",
                    "query":      "...",
                    "answer":     "...",          # final_answer_text
                    "status":     "done|error|step_cap|rejected",
                    "error":      "..." | null,   # error_message when status=error
                    "started_at": "<iso>",
                    "sources":    [SpotlightResult-shaped dict, ...]
                },
                ...
            ]
        }
    """

    def get(self, request, session_id: str):
        user_id = str(getattr(request.user, "id", ""))
        if not user_id:
            return Response({"error": "Not authenticated."}, status=status.HTTP_401_UNAUTHORIZED)

        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            session = AgentSession.objects.get(
                session_id=session_id,
                team_id=str(team_id),
                user_id=user_id,
            )
        except (AgentSession.DoesNotExist, ValueError):
            # ValueError covers malformed UUIDs. Both surface as 404 so
            # we don't reveal "this id exists but you can't see it".
            return Response(
                {"error": "Session not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        # Prefetch steps so `_reconstruct_sources_for_run` resolves in
        # 2 queries total (runs + steps) instead of N+1. Steps are
        # ordered by `step_index` so the rebuilt source list matches
        # the live emission order, which the Phase 4.2 citation-density
        # ranker uses as a stable secondary sort.
        runs = (
            AgentRun.objects.filter(session=session)
            .order_by("started_at")
            .prefetch_related(
                Prefetch(
                    "steps",
                    queryset=AgentStep.objects.order_by("step_index"),
                )
            )
        )

        turns = []
        for r in runs:
            answer = r.final_answer_text or ""
            error = r.error_message or ""
            # Skip rows that produced neither — most commonly an
            # abandoned run that never completed. They'd render as
            # empty bubbles in the archive view.
            if not answer and not error:
                continue
            turns.append(
                {
                    "run_id": str(r.run_id),
                    "query": r.query or "",
                    "answer": answer,
                    "status": r.status,
                    "error": error or None,
                    "started_at": r.started_at.isoformat(),
                    "sources": _reconstruct_sources_for_run(r),
                }
            )

        return Response(
            {
                "session_id": str(session.session_id),
                "created_at": session.created_at.isoformat(),
                "last_active_at": session.last_active_at.isoformat(),
                "turns": turns,
            }
        )
