"""Tool categories — declared, machine-readable grouping of the registry.

One map, one place, enforced two-way by `test_agent_tool_registry`:
every registered tool must appear here, every name here must exist in
the registry, every value must be a known category. Forgetting to
categorize a new tool fails CI with the missing name spelled out —
the same mechanism that keeps `WRITE_PREFIXES` honest.

Why a central map rather than a field on each `Tool(...)`: the 57
definitions live in 57 files, and the consumers of the grouping (the
peripheral-family subsetting in `controller._tool_family`, the
registry-generated prompt sections in `prompts.py`, and any future
per-category telemetry slice) all want the whole taxonomy at once.
A reviewed dict plus a two-way completeness test gives the same
guarantee as a dataclass field with a fraction of the surface.

The taxonomy mirrors the semantic groups the system prompt's
TOOL SELECTION CHEAT-SHEET already teaches the model — categories are
ROUTING metadata (what is this tool for), not permissions
(`requires_approval` stays the write gate) and not cost.

⚠️ `PERIPHERAL_FAMILIES` members feed `RAG_TOOL_SUBSETTING` (OFF — it
regressed a pro-model eval once; see SPOTLIGHT_Q0_Q2_PROGRESS.md).
Moving a category in or out of that tuple changes which tools that
flag may drop. The four families reproduce the old name-regex
`_tool_family` heuristic exactly — parity is pinned by test.
"""

from __future__ import annotations

CATEGORIES = (
    "search",  # cross-workspace retrieval
    "web",  # live internet search (Tavily)
    "fetch",  # one entity by id, full detail
    "tasks_read",  # structured task queries + analytics
    "tasks_write",  # task mutations (approval-gated)
    "projects",  # project listings + rollups
    "milestones",  # milestone + sprint listings/rollups
    "team",  # people, rosters, workload, team analytics
    "identity",  # who am I
    "me",  # the asking user's own work
    "pr",  # GitHub pull requests
    "calendar",  # Google Calendar (reads + gated writes)
    "todo",  # personal daily todos
    "notes",  # note folders + note writes
)

# Categories the (dormant) per-query subsetting may drop when the query
# shows no signal for them. Exactly the old `_tool_family` families.
PERIPHERAL_FAMILIES = ("pr", "calendar", "todo", "me")

TOOL_CATEGORY: dict[str, str] = {
    # --- search ---
    "search_knowledge_base": "search",
    "search_past_conversations": "search",
    # --- web ---
    "search_web": "web",
    # --- fetch (single entity; fetch_pr lives in "pr" — it was always
    # part of the pr family, and category parity with the old heuristic
    # is a hard requirement) ---
    "fetch_task": "fetch",
    "fetch_note": "fetch",
    "fetch_chat_thread": "fetch",
    # --- tasks_read ---
    "list_tasks": "tasks_read",
    "list_task_dependencies": "tasks_read",
    "get_task_blockers": "tasks_read",
    "get_stale_tasks": "tasks_read",
    "get_task_throughput_stats": "tasks_read",
    # --- tasks_write ---
    "create_task": "tasks_write",
    "create_task_plan": "tasks_write",
    "update_task": "tasks_write",
    "update_tasks_bulk": "tasks_write",
    "assign_task": "tasks_write",
    "add_comment": "tasks_write",
    # --- projects ---
    "list_projects": "projects",
    "get_project_summary": "projects",
    "get_project_activity_ranking": "projects",
    # --- milestones ---
    "list_milestones": "milestones",
    "get_milestone_summary": "milestones",
    "get_milestone_assignee_counts": "milestones",
    "list_sprints": "milestones",
    "get_sprint_summary": "milestones",
    # --- team ---
    "get_team_members": "team",
    "list_project_members": "team",
    "list_channel_members": "team",
    "get_workload_distribution": "team",
    "get_top_task_closers": "team",
    "get_team_task_summary": "team",
    # --- identity ---
    "get_current_user": "identity",
    # --- me ---
    "get_my_task_summary": "me",
    "get_my_focus_tasks": "me",
    "get_my_schedule": "me",
    "get_my_throughput": "me",
    "get_my_blockers": "me",
    "list_my_milestones": "me",
    "list_my_inbox": "me",
    "list_my_mentions": "me",
    # --- pr ---
    "fetch_pr": "pr",
    "list_pr_comments": "pr",
    "list_pr_files": "pr",
    "list_pr_reviews": "pr",
    "list_pr_commits": "pr",
    # --- calendar ---
    "list_calendars": "calendar",
    "list_calendar_events": "calendar",
    "create_calendar_event": "calendar",
    "update_calendar_event": "calendar",
    "delete_calendar_event": "calendar",
    # --- todo ---
    "list_today_todos": "todo",
    "list_uncompleted_todos": "todo",
    "create_todo_item": "todo",
    "update_todo_item": "todo",
    # --- notes ---
    "list_note_folders": "notes",
    "create_note": "notes",
    "update_note": "notes",
}


def tool_category(name: str) -> str | None:
    """The tool's category, or None for a name outside the registry."""
    return TOOL_CATEGORY.get(name)
