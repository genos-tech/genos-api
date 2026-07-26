"""One logical request, metered end to end.

`llm/spend.py` groups a request's provider calls under one `request_id`.
This module adds the two things every metered surface also needs: the
`AiRequestCost` rollup, opened at the start with its quote and closed at
the end with a result.

The ask path does this by hand (`agent_views._stream_ndjson`) because it
is a streaming generator whose terminal moment is a `finally` shared with
the `AgentRun` row. Every OTHER paid surface is an ordinary function
call — and was doing none of it. Plain `/search/`, both summary endpoints
and the judge cron bound no context at all, so their spend landed under
`surface="unattributed"`: the tripwire firing exactly as designed.

Two of those surfaces already charge the user an ask against
`LLM_ASK_KEY`, so leaving them unattributed was never merely a reporting
gap. Under the credit engine they would consume a user's quota and post a
zero charge.

**Nesting is a no-op, deliberately.** `search()` is called both by the
search endpoint and by the `search_kb` tool inside an ask. `spend_context`
is already re-entrant, so an inner bind keeps the outer `request_id` — but
opening a rollup would still write an `AiRequestCost` row under a *fresh*
`request_id` that no event ever referenced, i.e. a permanent supply of
¥0 phantom requests diluting every per-request average. So when a context
is already bound, this helper yields and does nothing else.
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from django.conf import settings

from origin.search_engine import spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.models import AiRequestCost
from origin.search_engine.quota import get_effective_tier

log = logging.getLogger(__name__)


@dataclass
class RequestOutcome:
    """Mutable result holder yielded by `metered_request`.

    Defaults to success because a clean return is the common path. The
    caller overrides it on branches that swallow a failure into a
    response instead of raising — both summary views return 503 rather
    than propagating, so an exception-only rule would score those as
    successes and charge for them.
    """

    result: str = AiRequestCost.RESULT_SUCCESS

    def mark(self, result: str) -> None:
        self.result = result


def spend_kwargs_for(
    surface: str,
    user_id: str = "",
    team_id=None,
    chosen=None,
    run_id=None,
    credit_budget_jpy_milli: int = 0,
) -> dict:
    """Build the cost-meter binding for one logical request.

    `plan` and `effort` are resolved ONCE here and denormalized onto every
    spend row. Resolving them per row would put a tier lookup — one
    60s-cache miss away from a DB query — on the request path once per
    provider call, and an ask makes six to ten.

    `credit_budget_jpy_milli` travels in the kwargs (rather than being
    computed here) because every `spend_context(**spend_kwargs)` builds a
    FRESH context — including the agent worker's, on another thread — and
    the kwargs dict is the only thing all of them share. Default 0 keeps
    every surface that does not pass one exactly as it was.
    """
    plan = ""
    try:
        plan = get_effective_tier(str(user_id)) if user_id else ""
    except Exception:  # noqa: BLE001 — never fail a request over a label
        log.debug("Could not resolve plan for spend context", exc_info=True)
    kwargs = {
        "surface": surface,
        "request_id": str(uuid.uuid4()),
        "user_id": str(user_id or ""),
        "team_id": str(team_id or ""),
        "plan": plan,
        "effort": getattr(chosen, "effort", "") or "",
        "credit_budget_jpy_milli": int(credit_budget_jpy_milli or 0),
    }
    if run_id:
        kwargs["run_id"] = str(run_id)
    return kwargs


def open_request(spend_kwargs: dict) -> None:
    """Write the request's rollup row up front, with its quoted ceiling.

    The quote must be written BEFORE any spend: it is the maximum we
    promised not to exceed, and after the fact there is no way to prove
    what it would have been. No-ops when the meter is off.
    """
    try:
        spend_recorder.open_request(
            spend.SpendContext(**spend_kwargs),
            quoted_max_jpy_milli=int(
                settings.SEARCH_ENGINE.get("AI_REQUEST_MAX_JPY_MILLI", 0) or 0
            ),
        )
    except Exception:  # noqa: BLE001 — accounting never breaks a request
        log.debug("Failed to open spend request", exc_info=True)


def close_request(spend_kwargs: dict, result: str) -> None:
    """End this logical request: roll the events up, then settle the
    shadow credit charge. Never raises.

    `spend_recorder.finish_request`, not the bare `close_request`:
    the two summary surfaces are billable, so a bare close would roll
    up their cost and post no charge — they would consume a user's ask
    quota and appear in `ai_credit_report` as free. Settling lives on
    `finish_request` precisely so it stays out of `--rebuild`'s replay
    path (`RebuildGuardTests`).
    """
    try:
        spend_recorder.finish_request(spend.SpendContext(**spend_kwargs), result=result)
    except Exception:  # noqa: BLE001 — accounting never breaks a response
        log.debug("Failed to close spend request", exc_info=True)


@contextmanager
def metered_request(
    *,
    surface: str,
    user_id: str = "",
    team_id=None,
    chosen=None,
    run_id=None,
) -> Iterator[RequestOutcome]:
    """Bind, open and close one logical request on a non-streaming surface.

    Yields a `RequestOutcome` the body may `.mark()` with a non-success
    result. An escaping exception is recorded as an application failure
    and re-raised — the row is written either way, because "this request
    happened and cost money" is a fact the reconciliation needs whether or
    not the user got an answer.
    """
    if spend.current_context() is not None:
        # Already inside a logical request — see the module docstring.
        yield RequestOutcome()
        return

    spend_kwargs = spend_kwargs_for(surface, user_id, team_id, chosen, run_id)
    open_request(spend_kwargs)
    outcome = RequestOutcome()
    try:
        with spend.spend_context(**spend_kwargs):
            yield outcome
    except BaseException:
        # BaseException, not Exception: a client disconnect on a streaming
        # caller raises GeneratorExit, and a request that died still spent.
        outcome.mark(AiRequestCost.RESULT_APPLICATION_FAILURE)
        raise
    finally:
        close_request(spend_kwargs, outcome.result)
