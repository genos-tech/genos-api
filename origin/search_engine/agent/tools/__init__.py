"""Agent tool registry.

The tools the agent can call. Each tool module exports a single `Tool`
instance; `REGISTRY` aggregates them by name so the controller can
dispatch a function-call to the right `run(...)`.

Phase 11 — write-tool surface expansion. Write tools flagged
`requires_approval=True` route through the pause/resume protocol from
Phase 7. Read-only tools execute inline.

Phase 13 — internal tool expansion. Seven new tools covering structured
queries and write operations that were previously impossible or required
fragile semantic search workarounds:
  Read (inline):
    list_projects, list_tasks, get_team_members,
    get_current_user, get_project_summary
  Write (requires_approval):
    assign_task, update_note

Phase 15 — aggregation/analytics surface. Five read-only tools that
return cross-project, time-ranged statistics so the model can answer
PM-style questions ("throughput last week", "top contributors", "which
project has the most notes") without enumerating individual records:
  Read (inline):
    get_task_throughput_stats, get_top_task_closers,
    get_project_activity_ranking, get_workload_distribution,
    get_stale_tasks

Phase 18 — me-scoped (user-specific) aggregation. Eight read-only
tools that always operate on `ctx.user_id`, so the agent can answer
"what kind of WIP tasks do I have?", "what should I handle first?",
"my schedule this week", "what milestones am I on?", "what's blocking
me?", "what did I close this week?", "what's in my inbox?", and "who
@mentioned me?" with a single call:
  Read (inline):
    get_my_task_summary, get_my_focus_tasks, get_my_schedule,
    list_my_milestones, list_my_inbox, list_my_mentions,
    get_my_blockers, get_my_throughput
Plus `list_milestones` gains an `assignee_user_id` filter for the
general "which milestones is Alice on?" case.
"""

from origin.search_engine.agent.model_fence import with_unfenced_arguments
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.add_project_tag import ADD_PROJECT_TAG
from origin.search_engine.agent.tools.add_task_delivery import ADD_TASK_DELIVERY
from origin.search_engine.agent.tools.approve_join_request import APPROVE_JOIN_REQUEST
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.base import (
    FLAGGED_REGISTRY,
    MCP_ONLY_REGISTRY,
    REGISTRY,
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    flagged_tool,
    flagged_tools,
    get_app_tool,
    get_mcp_tool,
)
from origin.search_engine.agent.tools.create_calendar_event import CREATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.create_task_plan import CREATE_TASK_PLAN
from origin.search_engine.agent.tools.create_todo_item import CREATE_TODO_ITEM
from origin.search_engine.agent.tools.decline_join_request import DECLINE_JOIN_REQUEST
from origin.search_engine.agent.tools.delete_calendar_event import DELETE_CALENDAR_EVENT
from origin.search_engine.agent.tools.delete_comment import DELETE_COMMENT
from origin.search_engine.agent.tools.delete_milestone import DELETE_MILESTONE
from origin.search_engine.agent.tools.delete_note import DELETE_NOTE
from origin.search_engine.agent.tools.delete_note_folder import DELETE_NOTE_FOLDER
from origin.search_engine.agent.tools.delete_project import DELETE_PROJECT
from origin.search_engine.agent.tools.delete_sprint import DELETE_SPRINT
from origin.search_engine.agent.tools.delete_todo_item import DELETE_TODO_ITEM
from origin.search_engine.agent.tools.favorite_note import FAVORITE_NOTE
from origin.search_engine.agent.tools.fetch_chat_thread import FETCH_CHAT_THREAD
from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE
from origin.search_engine.agent.tools.fetch_note_history import FETCH_NOTE_HISTORY
from origin.search_engine.agent.tools.fetch_pr import FETCH_PR
from origin.search_engine.agent.tools.fetch_task import FETCH_TASK
from origin.search_engine.agent.tools.find_task_by_delivery import FIND_TASK_BY_DELIVERY
from origin.search_engine.agent.tools.get_current_user import GET_CURRENT_USER
from origin.search_engine.agent.tools.get_milestone_assignee_counts import (
    GET_MILESTONE_ASSIGNEE_COUNTS,
)
from origin.search_engine.agent.tools.get_milestone_summary import GET_MILESTONE_SUMMARY
from origin.search_engine.agent.tools.get_my_blockers import GET_MY_BLOCKERS
from origin.search_engine.agent.tools.get_my_focus_tasks import GET_MY_FOCUS_TASKS
from origin.search_engine.agent.tools.get_my_schedule import GET_MY_SCHEDULE
from origin.search_engine.agent.tools.get_my_task_summary import GET_MY_TASK_SUMMARY
from origin.search_engine.agent.tools.get_my_throughput import GET_MY_THROUGHPUT
from origin.search_engine.agent.tools.get_project_activity_ranking import (
    GET_PROJECT_ACTIVITY_RANKING,
)
from origin.search_engine.agent.tools.get_project_summary import GET_PROJECT_SUMMARY
from origin.search_engine.agent.tools.get_sprint_summary import GET_SPRINT_SUMMARY
from origin.search_engine.agent.tools.get_stale_tasks import GET_STALE_TASKS
from origin.search_engine.agent.tools.get_task_blockers import GET_TASK_BLOCKERS
from origin.search_engine.agent.tools.get_task_history import GET_TASK_HISTORY
from origin.search_engine.agent.tools.get_task_throughput_stats import (
    GET_TASK_THROUGHPUT_STATS,
)
from origin.search_engine.agent.tools.get_team_members import GET_TEAM_MEMBERS
from origin.search_engine.agent.tools.get_team_task_summary import GET_TEAM_TASK_SUMMARY
from origin.search_engine.agent.tools.get_timeline import GET_TIMELINE_INAPP
from origin.search_engine.agent.tools.get_top_task_closers import GET_TOP_TASK_CLOSERS
from origin.search_engine.agent.tools.get_workload_distribution import (
    GET_WORKLOAD_DISTRIBUTION,
)
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.search_engine.agent.tools.list_calendars import LIST_CALENDARS
from origin.search_engine.agent.tools.list_channel_members import LIST_CHANNEL_MEMBERS
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.list_my_chats import LIST_MY_CHATS
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.search_engine.agent.tools.list_my_mentions import LIST_MY_MENTIONS
from origin.search_engine.agent.tools.list_my_milestones import LIST_MY_MILESTONES
from origin.search_engine.agent.tools.list_my_unread import LIST_MY_UNREAD
from origin.search_engine.agent.tools.list_note_access import LIST_NOTE_ACCESS
from origin.search_engine.agent.tools.list_note_folders import LIST_NOTE_FOLDERS
from origin.search_engine.agent.tools.list_pr_comments import LIST_PR_COMMENTS
from origin.search_engine.agent.tools.list_pr_commits import LIST_PR_COMMITS
from origin.search_engine.agent.tools.list_pr_files import LIST_PR_FILES
from origin.search_engine.agent.tools.list_pr_reviews import LIST_PR_REVIEWS
from origin.search_engine.agent.tools.list_project_members import LIST_PROJECT_MEMBERS
from origin.search_engine.agent.tools.list_project_tags import LIST_PROJECT_TAGS
from origin.search_engine.agent.tools.list_projects import LIST_PROJECTS
from origin.search_engine.agent.tools.list_sprints import LIST_SPRINTS
from origin.search_engine.agent.tools.list_task_dependencies import LIST_TASK_DEPENDENCIES
from origin.search_engine.agent.tools.list_tasks import LIST_TASKS
from origin.search_engine.agent.tools.list_today_todos import LIST_TODAY_TODOS
from origin.search_engine.agent.tools.list_uncompleted_todos import LIST_UNCOMPLETED_TODOS
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.manage_note_folder import MANAGE_NOTE_FOLDER
from origin.search_engine.agent.tools.manage_note_sharing import MANAGE_NOTE_SHARING
from origin.search_engine.agent.tools.manage_project import MANAGE_PROJECT
from origin.search_engine.agent.tools.manage_project_labels import MANAGE_PROJECT_LABELS
from origin.search_engine.agent.tools.manage_project_members import MANAGE_PROJECT_MEMBERS
from origin.search_engine.agent.tools.manage_sprint import MANAGE_SPRINT
from origin.search_engine.agent.tools.remove_task_delivery import REMOVE_TASK_DELIVERY
from origin.search_engine.agent.tools.restore_note_version import RESTORE_NOTE_VERSION
from origin.search_engine.agent.tools.restore_task_body import RESTORE_TASK_BODY
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.agent.tools.search_past_conversations import (
    SEARCH_PAST_CONVERSATIONS,
)
from origin.search_engine.agent.tools.update_calendar_event import UPDATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.update_comment import UPDATE_COMMENT
from origin.search_engine.agent.tools.update_note import UPDATE_NOTE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_task_dependencies import UPDATE_TASK_DEPENDENCIES
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.search_engine.agent.tools.update_todo_item import UPDATE_TODO_ITEM
from origin.search_engine.agent.tools.web_search import SEARCH_WEB


def register_tool(tool: Tool) -> None:
    """Put `tool` where it belongs: `REGISTRY`, or `MCP_ONLY_REGISTRY` when
    `tool.mcp_only` (MIP-1070).

    Every tool, read and write, takes its arguments with any
    <workspace_content> fence the model copied from a result taken off
    (MIP-735), so a tool added later is covered without remembering to.
    Wrapped IN PLACE, not copied: the module constant (`LIST_TASKS`, which
    tests and other code call directly) and the registry entry must be the
    same object, or one of them would skip the stripping.

    One name is one tool: a name already taken in EITHER registry is refused,
    so an MCP-only tool can never shadow, or be shadowed by, one the app runs.
    A tool that lives outside this package (an MCP-only one can) calls this too,
    so it gets the same wrapping.
    """
    if tool.name in REGISTRY or tool.name in MCP_ONLY_REGISTRY:
        raise ValueError(f"tool {tool.name!r} is already registered")
    if not hasattr(tool.run, "__wrapped__"):
        object.__setattr__(tool, "run", with_unfenced_arguments(tool.run))
    (MCP_ONLY_REGISTRY if tool.mcp_only else REGISTRY)[tool.name] = tool


def register_flagged_tool(tool: Tool, flag: str) -> None:
    """File an in-app tool that exists only while `SEARCH_ENGINE[flag]` is on (MIP-1289).

    Wrapped like every other tool (`with_unfenced_arguments`, in place). Refused when the name is
    in `REGISTRY` or filed twice; a name in `MCP_ONLY_REGISTRY` is allowed on purpose (see the
    note on `FLAGGED_REGISTRY`). Write tools cannot be flag-gated this way: the approval and
    resume paths look tools up in `REGISTRY` only.
    """
    if tool.name in REGISTRY or tool.name in FLAGGED_REGISTRY:
        raise ValueError(f"tool {tool.name!r} is already registered")
    if tool.requires_approval or tool.mcp_only:
        raise ValueError(f"flag-gated tool {tool.name!r} must be a read tool for the app")
    if not hasattr(tool.run, "__wrapped__"):
        object.__setattr__(tool, "run", with_unfenced_arguments(tool.run))
    FLAGGED_REGISTRY[tool.name] = (flag, tool)


# Register at import time so REGISTRY is populated by the time the
# controller asks for a tool by name. Read tools first, then write
# tools — the order only matters for any future iteration of REGISTRY
# (we'd want reads to surface first in tool-list dumps).
for _t in (
    # --- Read tools (Phase 1–11) ---
    SEARCH_KNOWLEDGE_BASE,
    # --- Conversation memory (Q2.3 / C1) ---
    SEARCH_PAST_CONVERSATIONS,
    FETCH_TASK,
    FETCH_CHAT_THREAD,
    FETCH_NOTE,
    LIST_NOTE_FOLDERS,
    LIST_NOTE_ACCESS,
    FETCH_NOTE_HISTORY,
    # --- Read tools (Phase 13) ---
    LIST_PROJECTS,
    LIST_TASKS,
    GET_TEAM_MEMBERS,
    GET_CURRENT_USER,
    GET_PROJECT_SUMMARY,
    # --- Read tools — membership rosters (A1-ext) ---
    LIST_PROJECT_MEMBERS,
    LIST_CHANNEL_MEMBERS,
    # --- Read tools (MIP-707) — a project's tag catalogue ---
    LIST_PROJECT_TAGS,
    # --- Write tools (Phase 11) ---
    CREATE_TASK,
    UPDATE_TASK,
    ADD_COMMENT,
    CREATE_NOTE,
    # --- Composite write tools (BAU Wave 1) — one approval per plan ---
    CREATE_TASK_PLAN,
    UPDATE_TASKS_BULK,
    # --- Write tools (Phase 13) ---
    ASSIGN_TASK,
    UPDATE_NOTE,
    # --- Read tools (Phase 14) ---
    SEARCH_WEB,
    # --- Read tools (Phase 15) — analytics/aggregation ---
    GET_TASK_THROUGHPUT_STATS,
    GET_TOP_TASK_CLOSERS,
    GET_PROJECT_ACTIVITY_RANKING,
    GET_WORKLOAD_DISTRIBUTION,
    GET_STALE_TASKS,
    # --- Read tools (Phase 17) — milestone, sprint & dependency aggregation ---
    GET_TEAM_TASK_SUMMARY,
    LIST_MILESTONES,
    GET_MILESTONE_SUMMARY,
    GET_MILESTONE_ASSIGNEE_COUNTS,
    LIST_SPRINTS,
    GET_SPRINT_SUMMARY,
    GET_TASK_BLOCKERS,
    GET_TASK_HISTORY,
    # --- Read tools (BAU Wave 1) — whole-scope dependency graph ---
    LIST_TASK_DEPENDENCIES,
    # --- Read tools (MIP-687) — branch / PR → task ---
    FIND_TASK_BY_DELIVERY,
    # --- Read tools (Phase 16) — GitHub PR introspection ---
    FETCH_PR,
    LIST_PR_COMMENTS,
    LIST_PR_FILES,
    LIST_PR_REVIEWS,
    LIST_PR_COMMITS,
    # --- Read tools — Google Calendar ---
    LIST_CALENDARS,
    LIST_CALENDAR_EVENTS,
    # --- Write tools — Google Calendar (requires_approval) ---
    CREATE_CALENDAR_EVENT,
    UPDATE_CALENDAR_EVENT,
    DELETE_CALENDAR_EVENT,
    # --- Read tools (Phase 18) — me-scoped (user-specific) aggregation ---
    GET_MY_TASK_SUMMARY,
    GET_MY_FOCUS_TASKS,
    GET_MY_SCHEDULE,
    LIST_MY_MILESTONES,
    LIST_MY_INBOX,
    LIST_MY_MENTIONS,
    LIST_MY_CHATS,
    LIST_MY_UNREAD,
    GET_MY_BLOCKERS,
    GET_MY_THROUGHPUT,
    # --- Todo tools ---
    LIST_TODAY_TODOS,
    LIST_UNCOMPLETED_TODOS,
    # --- Todo write tools (requires_approval) ---
    CREATE_TODO_ITEM,
    UPDATE_TODO_ITEM,
    DELETE_TODO_ITEM,
    # --- Write tools (MIP-687) — record a task's branch / PR ---
    ADD_TASK_DELIVERY,
    REMOVE_TASK_DELIVERY,
    # --- Write tools (MIP-730) — link / unlink existing tasks ---
    UPDATE_TASK_DEPENDENCIES,
    # --- Write tools (MIP-879) — milestones end to end ---
    MANAGE_MILESTONE,
    DELETE_MILESTONE,
    # --- Write tools (MIP-880) — comments and body history ---
    UPDATE_COMMENT,
    DELETE_COMMENT,
    RESTORE_TASK_BODY,
    # --- Write tools (MIP-882) — sprints ---
    MANAGE_SPRINT,
    DELETE_SPRINT,
    # --- Write tools (MIP-882) — projects and who is in them ---
    MANAGE_PROJECT,
    MANAGE_PROJECT_MEMBERS,
    ADD_PROJECT_TAG,
    MANAGE_PROJECT_LABELS,
    APPROVE_JOIN_REQUEST,
    DECLINE_JOIN_REQUEST,
    DELETE_PROJECT,
    # --- Write tools (MIP-883) — organizing notes ---
    MANAGE_NOTE_FOLDER,
    FAVORITE_NOTE,
    DELETE_NOTE,
    DELETE_NOTE_FOLDER,
    MANAGE_NOTE_SHARING,
    RESTORE_NOTE_VERSION,
):
    register_tool(_t)

# Flag-gated in-app tools (default off; NOT in REGISTRY, see `base.FLAGGED_REGISTRY`).
register_flagged_tool(GET_TIMELINE_INAPP, "AGENT_INAPP_TIMELINE_TOOL")


__all__ = [
    "register_tool",
    "register_flagged_tool",
    "get_mcp_tool",
    "get_app_tool",
    "flagged_tool",
    "flagged_tools",
    "FLAGGED_REGISTRY",
    "ADD_COMMENT",
    "ADD_TASK_DELIVERY",
    "DELETE_SPRINT",
    "MANAGE_PROJECT",
    "MANAGE_PROJECT_MEMBERS",
    "ADD_PROJECT_TAG",
    "MANAGE_PROJECT_LABELS",
    "APPROVE_JOIN_REQUEST",
    "DECLINE_JOIN_REQUEST",
    "DELETE_PROJECT",
    "MANAGE_NOTE_FOLDER",
    "FAVORITE_NOTE",
    "DELETE_NOTE",
    "DELETE_NOTE_FOLDER",
    "MANAGE_NOTE_SHARING",
    "LIST_NOTE_ACCESS",
    "FETCH_NOTE_HISTORY",
    "RESTORE_NOTE_VERSION",
    "MANAGE_SPRINT",
    "REMOVE_TASK_DELIVERY",
    "ASSIGN_TASK",
    "CREATE_CALENDAR_EVENT",
    "CREATE_NOTE",
    "CREATE_TASK",
    "CREATE_TASK_PLAN",
    "CREATE_TODO_ITEM",
    "DELETE_CALENDAR_EVENT",
    "DELETE_COMMENT",
    "DELETE_MILESTONE",
    "DELETE_TODO_ITEM",
    "FETCH_CHAT_THREAD",
    "FETCH_NOTE",
    "LIST_NOTE_FOLDERS",
    "FETCH_PR",
    "FETCH_TASK",
    "FIND_TASK_BY_DELIVERY",
    "GET_CURRENT_USER",
    "GET_MILESTONE_ASSIGNEE_COUNTS",
    "GET_MILESTONE_SUMMARY",
    "GET_MY_BLOCKERS",
    "GET_MY_FOCUS_TASKS",
    "GET_MY_SCHEDULE",
    "GET_MY_TASK_SUMMARY",
    "GET_MY_THROUGHPUT",
    "GET_PROJECT_ACTIVITY_RANKING",
    "GET_PROJECT_SUMMARY",
    "GET_SPRINT_SUMMARY",
    "GET_STALE_TASKS",
    "GET_TASK_BLOCKERS",
    "GET_TASK_HISTORY",
    "GET_TASK_THROUGHPUT_STATS",
    "GET_TEAM_MEMBERS",
    "GET_TEAM_TASK_SUMMARY",
    "GET_TOP_TASK_CLOSERS",
    "GET_WORKLOAD_DISTRIBUTION",
    "LIST_CALENDAR_EVENTS",
    "LIST_CALENDARS",
    "LIST_MILESTONES",
    "LIST_MY_CHATS",
    "LIST_MY_INBOX",
    "LIST_MY_UNREAD",
    "LIST_MY_MENTIONS",
    "LIST_MY_MILESTONES",
    "LIST_PR_COMMENTS",
    "LIST_PR_COMMITS",
    "LIST_PR_FILES",
    "LIST_PR_REVIEWS",
    "LIST_PROJECTS",
    "LIST_SPRINTS",
    "LIST_TASK_DEPENDENCIES",
    "LIST_TASKS",
    "LIST_TODAY_TODOS",
    "LIST_UNCOMPLETED_TODOS",
    "MANAGE_MILESTONE",
    "REGISTRY",
    "MCP_ONLY_REGISTRY",
    "RESTORE_TASK_BODY",
    "SEARCH_KNOWLEDGE_BASE",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolForbidden",
    "UPDATE_CALENDAR_EVENT",
    "UPDATE_COMMENT",
    "UPDATE_NOTE",
    "UPDATE_TASK",
    "UPDATE_TASKS_BULK",
    "UPDATE_TASK_DEPENDENCIES",
    "UPDATE_TODO_ITEM",
    "SEARCH_WEB",
]
