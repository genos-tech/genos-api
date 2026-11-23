"""The effort level of a Genos Skill: what each level means, and how a run uses it (MIP-2198).

A skill's owner picks an effort (`Skill.effort`: low, medium or high; empty = "my setting", the
caller's own). The run uses it as the effort of the in-app agent for that ask, so the level picks
everything the agent already derives from an effort, from ONE table:

    level   model tier (the provider's rung)   step limit   thinking budget      `efforts:` in apis/llm_models.yaml
    credit  where a run starts when the balance is low                           `skill_effort:` in apis/credit_policy.yaml

Nothing here invents a model id, a budget or a step count: `table()` reads them from
`settings.LLM_CATALOG` (the model list and the per-level profile the agent's loop reads for every ask)
and from `settings.CREDIT_POLICY`. Changing what a level is changes it for the skill editor, the run
and the cost shown beside the choice at once.

CREDITS FOLLOW THE LEVEL through the engine every ask goes through. The run binds the level's
`LlmChoice` (provider of the caller, model of the level), so the cost meter stamps the level and
the model on every spend row and the ledger charges what that model cost, to the CALLER. There is no
second price list.

LOW CREDITS. Under `skill_effort.<level>.degrade_below_credits` a run starts ONE level lower and says
so in one sentence; low is the floor. A spent balance is refused by the ordinary credit gate before
this is reached, so the order is: gate (zero = refused like any ask), then degrade, then run.

THE PLAN CEILING still applies (`get_max_effort`): a level above what the caller's plan allows runs at
the ceiling, as the caller's own setting does. Per-run lowering by the caller is NOT part of this
(there is no surface to choose it on; the level is the owner's).

The whole thing needs `AGENT_EFFORT_LEVELS` (without it the agent has no effort profile to bind and
every ask runs on the server's legacy model values, so a skill's level would change nothing): with
that switch off, `apply` returns the caller's own choice untouched, and the editor is told the levels
are not active yet.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from django.conf import settings

from apis.llm_catalog import EFFORTS

logger = logging.getLogger(__name__)

LEVELS: tuple[str, ...] = tuple(EFFORTS)  # cheapest first: low, medium, high


def active() -> bool:
    """True when the agent runs on effort levels at all (`AGENT_EFFORT_LEVELS`)."""
    return bool(settings.SEARCH_ENGINE.get("AGENT_EFFORT_LEVELS"))


@dataclass(frozen=True)
class Level:
    """One row of the table, for one provider."""

    level: str
    model: str
    model_label: str
    max_steps: int
    thinking_budget: int | None
    #: What a run costs next to a low one, from the two models' rates (a mean of the input and the
    #: output price per million tokens). None when a rate is unknown. A guide, not a quote.
    relative_cost: float | None
    #: The balance, in milli-credits, under which a run at this level starts one level lower.
    degrade_below_milli: int
    #: Above the caller's plan ceiling: it runs at the ceiling.
    locked: bool


def _blended_rate(model: str) -> float | None:
    price = settings.LLM_CATALOG.price_for(model)
    if price is None:
        return None
    return (price.input + price.output) / 2


def table(provider: str, max_effort: str = "high") -> list[Level]:
    """The levels, cheapest first, for `provider` (the caller's). `max_effort` is the plan ceiling."""
    catalog = settings.LLM_CATALOG
    floors = getattr(getattr(settings, "CREDIT_POLICY", None), "skill_effort_floor_milli", {}) or {}
    ceiling = LEVELS.index(max_effort) if max_effort in LEVELS else len(LEVELS) - 1
    base = _blended_rate(catalog.model_for_effort(provider, LEVELS[0]))
    rows = []
    for index, level in enumerate(LEVELS):
        model = catalog.model_for_effort(provider, level)
        rate = _blended_rate(model)
        profile = catalog.efforts[level]
        rows.append(
            Level(
                level=level,
                model=model,
                model_label=(catalog.by_model.get(model) or {}).get("label") or model,
                max_steps=profile.max_steps,
                thinking_budget=profile.thinking_budget,
                relative_cost=round(rate / base, 1) if rate and base else None,
                degrade_below_milli=int(floors.get(level, 0)),
                locked=index > ceiling,
            )
        )
    return rows


def clean(value: Any) -> str:
    """A skill's effort as a level, or "" (my setting)."""
    return value if isinstance(value, str) and value in LEVELS else ""


@dataclass(frozen=True)
class Decision:
    """What `apply` decided, for the run and for the note."""

    requested: str  # the level the skill asks for
    used: str  # the level the run starts at (after the plan ceiling and the credit step-down)
    degraded: bool  # stepped down one level because the balance is low

    @property
    def note(self) -> str:
        """The sentence the person is told when the run started one level lower, else ""."""
        if not self.degraded:
            return ""
        return (
            f"This skill is set to {self.requested} effort, but your AI credits are running low, "
            f"so it ran at {self.used} effort to save them."
        )


def _balance_milli(user_id: str, plan: str) -> int | None:
    """The caller's balance in milli-credits, None = unlimited or credits not authoritative."""
    from origin.search_engine import credit_ledger  # noqa: PLC0415 - search_engine is heavy

    if not credit_ledger.credits_authoritative():
        return None
    return credit_ledger.balance_milli(str(user_id), plan)


def apply(chosen: Any, effort: str, *, user_id: str, plan: str) -> tuple[Any, Decision | None]:
    """The `LlmChoice` a skill run binds, and what was decided; `(chosen, None)` when the skill
    carries no level or the agent is not on effort levels (the caller's own choice stands).

    Never raises: a ledger that cannot be read runs the skill at its own level, never breaks it."""
    from origin.search_engine.llm.choice import resolve_user_effort  # noqa: PLC0415
    from origin.search_engine.quota import get_max_effort  # noqa: PLC0415

    requested = clean(effort)
    if not requested or not active():
        return chosen, None
    ceiling = get_max_effort(str(user_id))
    level = requested
    degraded = False
    try:
        floor = int(getattr(settings.CREDIT_POLICY, "skill_effort_floor_milli", {}).get(level, 0))
        if floor > 0:
            balance = _balance_milli(user_id, plan)
            if balance is not None and balance < floor:
                level, degraded = LEVELS[max(LEVELS.index(level) - 1, 0)], True
    except Exception:  # noqa: BLE001 - a check that cannot run must not block a skill
        logger.debug("skills: could not read the balance for the effort step-down", exc_info=True)
    # `resolve_user_effort` applies the plan ceiling and maps the level to the model of the
    # caller's provider; the legacy model argument is unused because the level is explicit.
    bound = resolve_user_effort(chosen.provider, level, "", max_effort=ceiling)
    used = bound.effort
    return bound, Decision(requested=requested, used=used, degraded=degraded and used != requested)


def bind_kept(chosen: Any, effort: str, *, user_id: str) -> Any:
    """The `LlmChoice` of a resumed leg: the level the first leg ran at, under today's plan ceiling.
    No second step-down: a run that started keeps its level across an approval."""
    from origin.search_engine.llm.choice import resolve_user_effort  # noqa: PLC0415
    from origin.search_engine.quota import get_max_effort  # noqa: PLC0415

    level = clean(effort)
    if not level or not active():
        return chosen
    return resolve_user_effort(chosen.provider, level, "", max_effort=get_max_effort(str(user_id)))
