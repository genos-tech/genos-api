"""Which model and settings an AI job runs on, from an effort level (MIP-1210).

Reuses the catalog's low / medium / high (`apis/llm_models.yaml`, the rungs Spotlight uses): the
level picks the model rung of the SERVER's default provider, the same way an ask's does. It is
read from the catalog directly, not through `active_effort_profile()`, which is gated by the
`AGENT_EFFORT_LEVELS` flag and meant for the in-app ask: a report must not depend on that flag.

What the level changes for a report (and nothing else, so the cost follows from it):

    level     model rung (Gemini)         thinking budget   why
    low       gemini-3.5-flash-lite       0                 a short factual sentence needs no thinking
    medium    gemini-3.6-flash            0                 a stronger reader; thinking still off
    high      gemini-3.1-pro-preview      512               the top rung; a small thinking budget

The thinking budget is explicit because the models' default is dynamic thinking, billed at the
output rate (MIP-928 measured how that grows a bill). The member's own Spotlight preference is
NOT used: a report is a fixed product, as the digest is (MIP-928).

An unknown or retired level, or one the catalog cannot serve, never fails a run: it becomes the
job's default (`resolve`), and a level above the member's plan ceiling (`max_effort`) is clamped
down to it, as `resolve_user_effort` does for an ask.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings

log = logging.getLogger(__name__)

EFFORTS = ("low", "medium", "high")
THINKING_BUDGET = {"low": 0, "medium": 0, "high": 512}
FALLBACK_EFFORT = "low"


@dataclass(frozen=True)
class RunSettings:
    effort: str
    provider: str
    model: str
    thinking_budget: int


def valid(value: Any) -> str | None:
    """The level as written if it is one the catalog has, else None."""
    level = value.lower().strip() if isinstance(value, str) else ""
    return level if level in EFFORTS and level in settings.LLM_CATALOG.efforts else None


def _provider() -> str:
    provider = str(settings.SEARCH_ENGINE.get("LLM_PROVIDER") or "gemini").lower()
    return provider if provider in ("gemini", "claude", "openai") else "gemini"


def _ceiling(user_id: Any) -> str:
    try:
        from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

        return valid(quota.get_max_effort(str(user_id))) or "high"
    except Exception:  # noqa: BLE001 - a plan lookup never fails a report
        log.warning("report_ai: max effort not read", exc_info=True)
        return "high"


def resolve(
    requested: Any, default: str, user_id: Any, *, clamp: bool = True
) -> RunSettings | None:
    """The settings for a run. `requested` (a schedule's own level) that is not valid falls back
    to `default` (the job's), then to the plan ceiling. None only when the catalog has no model
    for the level at all (the caller then skips the AI part)."""
    effort = valid(requested) or valid(default) or FALLBACK_EFFORT
    ceiling = _ceiling(user_id) if clamp else "high"
    if EFFORTS.index(effort) > EFFORTS.index(ceiling):
        effort = ceiling
    provider = _provider()
    try:
        model = settings.LLM_CATALOG.model_for_effort(provider, effort)
    except Exception:  # noqa: BLE001
        log.warning("report_ai: no model for %s/%s", provider, effort)
        return None
    if not model:
        return None
    return RunSettings(effort, provider, model, THINKING_BUDGET[effort])
