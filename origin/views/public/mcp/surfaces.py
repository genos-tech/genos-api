"""The MCP endpoints a client may connect to: the full catalog, a curated core, and domains (MIP-1594).

Every endpoint is the same server over the same registry (`tools.CATALOG`). An endpoint is only a
fixed list of tool NAMES: which of the catalog it publishes. Names, titles, descriptions, schemas
and annotations are the catalog's, so a tool looks the same wherever it is connected.

* `full` is `POST /api/public/v1/mcp`, unchanged: every tool in the catalog.
* `core` is `POST /api/public/v1/mcp/core`: the small set a client needs for day-to-day work.
* the domain endpoints each hold a part of the catalog. Together with `core` and `full` they are
  NOT a partition: `core` repeats tools from the domains on purpose.

A surface is fixed per URL. Its list never changes per session or per request (the MCP spec's rule
against a server that varies `tools/list` on one connection), and a call to a name outside the
surface is refused as an unknown tool, exactly as a name nobody publishes is. Scope, the
destructive opt-in and flags are still applied by `tools.visible_tools`, as on the full endpoint.

Adding a tool to the catalog does NOT add it to a surface: a new tool is on `full` only until it is
listed here on purpose. `test_mcp_surfaces` checks that the domains partition the catalog and that
every name listed exists.
"""

from __future__ import annotations

FULL = "full"
CORE = "core"

# The core: what the genos-workflow skill calls, plus the tools with the most real use in the
# 2026-11-15..21 log window (MIP-1594, findings note). Not in it on purpose: list_my_unread,
# get_note_history, restore_task_body and restore_note_version (they are in the domains).
CORE_TOOLS: tuple[str, ...] = (
    "get_my_briefing",
    "search_workspace",
    "list_my_tasks",
    "get_task",
    "list_tasks",
    "get_task_history",
    "get_task_blockers",
    "list_task_dependencies",
    "get_my_blockers",
    "create_task",
    "create_task_plan",
    "update_task",
    "update_tasks_bulk",
    "add_comment",
    "add_task_delivery",
    "find_task_by_delivery",
    "get_milestone_summary",
    "list_project_tags",
    "get_note",
    "create_note",
    "update_note",
    "edit_note_block",
    # MIP-2343: exists only while `HTML_NOTES` is on (`McpTool.setting`); the plugin and the setup
    # guide hand out `core`, so an agent can only save an HTML note from here if it is listed.
    "save_html_note",
    # MIP-2049 (D1 of the skills v2 design): every tool a shipped general skill calls.
    "get_current_user",
    "get_team_members",
    "get_genos_help",
    "list_projects",
    "list_milestones",
    "list_my_inbox",
    "list_my_mentions",
    "get_chat_thread",
    "list_today_todos",
    "create_todo_item",
    "list_calendar_events",
    "get_stale_tasks",
    "update_task_dependencies",
)

# Domain endpoints. Each name here is in exactly one domain, and every catalog tool is in one.
DOMAIN_TOOLS: dict[str, tuple[str, ...]] = {
    "tasks": (
        "get_task",
        "list_tasks",
        "get_task_history",
        "get_task_blockers",
        "list_task_dependencies",
        "update_task_dependencies",
        "find_task_by_delivery",
        "search_workspace",
        "create_task",
        "create_task_plan",
        "update_task",
        "update_tasks_bulk",
        "assign_task",
        "add_comment",
        "update_comment",
        "delete_comment",
        "delete_task_attachment",
        "restore_task_body",
        "comment_on_task_body",
        "reply_to_task_body_comment",
        "resolve_task_body_comment",
        "add_task_delivery",
        "remove_task_delivery",
    ),
    "notes": (
        "get_note",
        "get_note_history",
        "list_note_folders",
        "list_note_access",
        "create_note",
        # MIP-2214: exists only while `HTML_NOTES` is on (`McpTool.setting`). Also on `core` (MIP-2343).
        "save_html_note",
        "update_note",
        "edit_note_block",
        "manage_note_folder",
        "favorite_note",
        "comment_on_note",
        "reply_to_note_comment",
        "resolve_note_comment",
        "delete_note",
        "delete_note_folder",
        "manage_note_sharing",
        "restore_note_version",
    ),
    "projects": (
        "list_projects",
        "get_project_summary",
        "list_milestones",
        "get_milestone_summary",
        "get_milestone_assignee_counts",
        "manage_milestone",
        "delete_milestone",
        "list_sprints",
        "get_sprint_summary",
        "manage_sprint",
        "delete_sprint",
        "manage_project",
        "manage_project_members",
        "list_project_members",
        "list_project_tags",
        "add_project_tag",
        "manage_project_labels",
        "approve_join_request",
        "decline_join_request",
        "get_project_activity_ranking",
    ),
    "me": (
        "get_my_briefing",
        "list_my_tasks",
        "list_my_unread",
        "get_my_blockers",
        "list_my_inbox",
        "get_item_line",
        "get_timeline",
        "get_my_importance",
        "set_item_importance",
        "tune_my_importance",
        "get_my_memory",
        "forget_my_memory",
        "list_uncompleted_todos",
        "list_today_todos",
        "create_todo_item",
        "update_todo_item",
        "delete_todo_item",
        "list_my_schedules",
        "create_my_schedule",
        "update_my_schedule",
        "delete_my_schedule",
        "run_my_schedule",
        "get_current_user",
        "get_team_members",
        "get_genos_help",
    ),
    "calendar-chat": (
        "list_calendars",
        "list_calendar_events",
        "get_calendar_event",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "list_my_chats",
        "get_chat_thread",
        "list_my_mentions",
        "list_channel_members",
    ),
    "analytics": (
        "get_my_task_summary",
        "get_team_task_summary",
        "get_my_throughput",
        "get_task_throughput_stats",
        "get_workload_distribution",
        "get_top_task_closers",
        "get_stale_tasks",
        "get_progress_history",
    ),
    "github": (
        "get_pr",
        "list_pr_reviews",
        "list_pr_comments",
        "list_pr_files",
        "list_pr_commits",
    ),
    # MIP-2046: the Genos Skills tools. They exist only while `GENOS_SKILLS` is on (`McpTool.setting`);
    # off, this endpoint lists nothing. NOT on `core` on purpose: core is what the general skills call.
    "skills": (
        "list_my_skills",
        "get_skill",
        "run_skill",
    ),
}

# URL segment -> surface key. `full` is the bare path and has no segment.
SURFACE_SEGMENTS: dict[str, str] = {"core": CORE, **{d: d for d in DOMAIN_TOOLS}}

_SURFACE_TOOLS: dict[str, frozenset[str]] = {
    CORE: frozenset(CORE_TOOLS),
    **{d: frozenset(names) for d, names in DOMAIN_TOOLS.items()},
}


def tool_names(surface: str | None) -> frozenset[str] | None:
    """The names a surface publishes, or None for the full catalog (no filter)."""
    if surface is None or surface == FULL:
        return None
    return _SURFACE_TOOLS[surface]


def publishes(surface: str | None, name: str) -> bool:
    """Is `name` on this surface? The full catalog publishes every name."""
    names = tool_names(surface)
    return names is None or name in names
