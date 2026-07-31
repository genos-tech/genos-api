"""Tier → agent tool surface: the AGENCY ladder.

The UX tier model's first pillar (genos-docs
operations/UX_TIER_MODEL_PLAN.md §4): what Genos *does* at each tier.

    read      Answer.   Search the workspace, cite, summarize.
    act       Act.      One thing at a time, with approval.
    organize  Organize. Composite writes — a whole milestone + task
              tree in one approval, 30 tasks reprioritized at once.

Gating works by NOT DECLARING a tool to the model — never by letting a
call error. A `read` user's Genos simply answers instead of acting and
never mentions a capability it doesn't have (the conversational-only
rule: no padlocks in the conversation, see mvp_roadmap/23 §standing
rules). The sets computed here join the same `disabled_tools` union the
web-search toggle and the `AGENT_DISABLED_TOOLS` ops kill-switch
already flow through (`controller._build_tool_declarations`).
"""

from __future__ import annotations

from origin.search_engine.quota import (
    get_agent_memory,
    get_agent_tool_level,
    get_integrations,
)

from .tools import REGISTRY

# The SINGLE-write tools `act` unlocks. Hand-maintained; everything
# else is derived, and the direction of the derivation is the whole
# point — see `disabled_tools_for_level`.
SINGLE_WRITE_TOOLS = frozenset(
    {
        "create_task",
        "create_note",
        "update_task",
        "update_note",
        "add_comment",
        "assign_task",
        "create_todo_item",
        "update_todo_item",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }
)

# Documentation + test anchor ONLY — the runtime never reads this.
# `test_agent_tool_tiers` asserts every `requires_approval` tool sits
# in exactly one of {SINGLE_WRITE_TOOLS, COMPOSITE_WRITE_TOOLS}, so a
# new write tool cannot merge unclassified.
COMPOSITE_WRITE_TOOLS = frozenset({"create_task_plan", "update_tasks_bulk"})


def disabled_tools_for_level(level: str) -> set[str]:
    """Tools to hide from the model at this agency level.

    Both the write set and the composite set are DERIVED, so an
    unclassified new tool fails CLOSED at every level:
      * a new tool with requires_approval=True is automatically hidden
        from `read`;
      * a new write tool nobody adds to SINGLE_WRITE_TOOLS falls into
        the composite remainder, so it is hidden from `act` too, and
        only `organize` gets it.
    Hand-maintaining the SINGLE list rather than the COMPOSITE one is
    what buys that second property — the reverse (deriving singles from
    a hand-listed composite set) would silently hand every forgotten
    tool to Core. Same structural reasoning as `model_daily` being
    derived from llm_models.yaml rather than written per tier.
    """
    if level == "organize":
        return set()
    writes = {t.name for t in REGISTRY.values() if t.requires_approval}
    if level == "read":
        return writes
    return writes - SINGLE_WRITE_TOOLS  # level == "act"


def tier_disabled_tools(user_id: str) -> set[str]:
    """The agency-ladder gate for the disabled-tools union.

    Inherits `get_agent_tool_level`'s fail-open contract: any infra
    doubt resolves to `organize`, i.e. an empty set — a Redis hiccup
    must never shrink someone's agent mid-conversation.
    """
    return disabled_tools_for_level(get_agent_tool_level(user_id))


def memory_disabled_tools(user_id: str) -> set[str]:
    """The memory-ladder gate (UX tier model §6): at `agent_memory ==
    "none"` the recall tool is simply not declared — the model never
    mentions a capability it doesn't have. `own`/`team` (and fail-open)
    leave it declared; the lane itself stays reachable because
    `search_past_conversations` opts in via `entity_types`, which the
    tier's `exclude_lanes` never blankets (see search.py §6.2)."""
    if get_agent_memory(user_id) == "none":
        return {"search_past_conversations"}
    return set()


# Integration → the tools it lights up (UX tier model §7 Reach). The
# tier's `integrations` allowlist disables the COMPLEMENT — remove the
# tool, never let it error: a tool that always raises burns an agent
# step and confuses the model into retrying. The three calendar WRITES
# also sit in SINGLE_WRITE_TOOLS; the two gates union, which is the
# intended composition (Free has no calendar at all; Core has calendar
# AND `act`, so it can create events).
INTEGRATION_TOOLS: dict[str, frozenset[str]] = {
    "web": frozenset({"search_web"}),
    "google_calendar": frozenset(
        {
            "list_calendars",
            "list_calendar_events",
            "create_calendar_event",
            "update_calendar_event",
            "delete_calendar_event",
        }
    ),
    "github": frozenset(
        {
            "fetch_pr",
            "list_pr_comments",
            "list_pr_commits",
            "list_pr_files",
            "list_pr_reviews",
        }
    ),
}


def reach_disabled_tools(user_id: str) -> set[str]:
    """The reach gate: tools of every integration the tier lacks.

    `get_integrations` fails open to the full list (and unknown names
    in the allowlist are inert), so infra doubt disables nothing.
    """
    allowed = set(get_integrations(user_id))
    disabled: set[str] = set()
    for name, tools in INTEGRATION_TOOLS.items():
        if name not in allowed:
            disabled |= tools
    return disabled


def disabled_tools_for_user(user_id: str) -> set[str]:
    """Every TIER-derived tool gate for this user, unioned.

    The single call sites in `agent_views` (fresh ask + the /decide/
    resume leg) use this so a new pillar's gate lands in ONE place —
    the two legs can never disagree on the surface again.
    """
    return (
        tier_disabled_tools(user_id)
        | memory_disabled_tools(user_id)
        | reach_disabled_tools(user_id)
    )
