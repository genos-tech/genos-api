"""The one model call of a report, metered (MIP-1210).

Follows how the digest and the other server-side callers do it: bind the provider and model with
an `LlmChoice` around the call, open one metered request (`surface="schedule"`, so every
`AiSpendEvent` and the `AiRequestCost` rollup carry it and the effort), and pass the model and
the generation settings explicitly.

Three guards, in this order, each a reason NOT to spend:

1. the operator switch `REPORT_AI_SENTENCES` (default on; "false" turns every report's AI off
   without a deploy of code);
2. the cost meter is ON. A call that would not be recorded is silent spend, which is never
   allowed: the `schedule-tick` cron service has not been given the meter or the model's
   credentials (see operations/SCHEDULER.md), so there an AI report is a plain one;
3. the provider's credentials exist in this process (`configured`). Checked from settings and
   files only: nothing is called.

A request is opened lazily, only when a call will really be made, so a skipped run leaves no
empty `AiRequestCost` row.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from origin.services.report_ai.effort import RunSettings

log = logging.getLogger(__name__)

PURPOSE = "report_sentences"
SURFACE = "schedule"

# Why a run had no AI part (kept on the run's result and logged; never an error).
SKIP_OFF = "operator_off"
SKIP_METER_OFF = "cost_meter_off"
SKIP_NOT_CONFIGURED = "model_unavailable"
SKIP_NO_MODEL = "no_model_for_level"
SKIP_FAILED = "call_failed"


@dataclass
class CallResult:
    text: str = ""
    request_id: str = ""  # set once a request was opened: the cost is attached from it
    skipped: str = ""


def enabled() -> bool:
    return os.environ.get("REPORT_AI_SENTENCES", "true").strip().lower() not in (
        "false",
        "0",
        "off",
        "no",
    )


def configured(provider: str) -> bool:
    cfg: Any = settings.SEARCH_ENGINE
    if provider == "gemini":
        if cfg.get("GEMINI_USE_VERTEX"):
            sa = cfg.get("GEMINI_SERVICE_ACCOUNT_FILE") or ""
            adc = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or ""
            return bool(cfg.get("GEMINI_PROJECT")) and any(
                p and os.path.isfile(p) for p in (sa, adc)
            )
        return bool(cfg.get("GEMINI_API_KEY"))
    if provider == "claude":
        return bool(cfg.get("CLAUDE_API_KEY"))
    if provider == "openai":
        return bool(cfg.get("OPENAI_API_KEY"))
    return False


def _client():
    from origin.search_engine.llm import get_model_client  # noqa: PLC0415 - heavy

    return get_model_client()


def preflight(run: RunSettings | None) -> str:
    """ "" when a call may be made, else the reason it may not."""
    if not enabled():
        return SKIP_OFF
    if run is None:
        return SKIP_NO_MODEL
    if not settings.SEARCH_ENGINE.get("AI_COST_METER"):
        return SKIP_METER_OFF
    if not configured(run.provider):
        return SKIP_NOT_CONFIGURED
    return ""


def complete(
    run: RunSettings, *, system: str, user: str, user_id: str, team_id: str, max_output_tokens: int
) -> CallResult:
    """One metered model call. Never raises: a failure is a `CallResult` with `skipped` set, and
    with `request_id` when the request had been opened (so the cost it may have are attached)."""
    reason = preflight(run)
    if reason:
        return CallResult(skipped=reason)

    from origin.search_engine.llm import spend  # noqa: PLC0415
    from origin.search_engine.llm.choice import (  # noqa: PLC0415
        LlmChoice,
        reset_llm_choice,
        set_llm_choice,
    )
    from origin.search_engine.llm.types import AgentMessage, GenerationParams  # noqa: PLC0415
    from origin.search_engine.metered import metered_request  # noqa: PLC0415

    choice = LlmChoice(
        provider=run.provider,
        model=run.model,
        effort=run.effort,
        subprocess_pins=False,
    )
    result = CallResult()
    token = set_llm_choice(choice)
    try:
        with metered_request(surface=SURFACE, user_id=user_id, team_id=team_id, chosen=choice):
            ctx = spend.current_context()
            result.request_id = ctx.request_id if ctx is not None else ""
            chunks: list[str] = []
            with spend.spend_purpose(PURPOSE):
                for text, call in _client().generate_step(
                    messages=[AgentMessage(role="user", text=user)],
                    tools=[],
                    system_instruction=system,
                    model_override=run.model,
                    params=GenerationParams(
                        max_output_tokens=max_output_tokens,
                        thinking_budget=run.thinking_budget,
                    ),
                ):
                    if text:
                        chunks.append(text)
                    if call is not None:  # no tools were offered: ignore, never run one
                        log.warning("report_ai: model asked for a tool; ignored")
            result.text = "".join(chunks)
    except Exception as exc:  # noqa: BLE001 - a report never fails because the model did
        # The type only: the message can quote the prompt.
        log.warning("report_ai: model call failed (%s)", type(exc).__name__)
        result.text = ""
        result.skipped = SKIP_FAILED
    finally:
        reset_llm_choice(token)
    return result
