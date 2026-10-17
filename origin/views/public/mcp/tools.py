"""The published tool catalog, and the adapter down to the registry.

Genos has 60 agent tools. This exposes 39. That is not all of them and
not a staging accident — every tool costs the connected agent context on
*every* request it makes, and much of the 60 answers questions Genos's
own assistant gets asked ("who closed the most tasks last sprint"), not
questions something working in the workspace has.

What the 39 are chosen to be is **every write Genos has actually built,
plus the reads needed to drive them**, plus the five GitHub PR reads that
let an agent see what became of the PR it opened (MIP-689), plus
`get_task_history` — on a surface with no approval gate and no undo, the
read that lets an agent check its write stuck and who else touched the
task (MIP-690) — plus the branch/PR → task lookup and its write, which
record which PR delivers which task so that join is no longer only a
comment (MIP-687). The earlier cut was 14, read-heavy and aimed narrowly
at the code loop: find the task, read it, do the work, report back. It
was sized by a fear that turns out to be misplaced — the measured cost
of the full catalog is ~7.5k tokens against ~14k for the in-app
agent's 57, so the ceiling that constrains the in-app prompt
(`SPOTLIGHT_QUALITY_ASSESSMENT_2026_07.md` F6) has far more headroom
here. Withholding a *built* write bought no measurable context back and
cost the connected agent the ability to do what the user can do.

The second half of that sentence is load-bearing and was got wrong once:
the calendar and todo writes shipped without the reads that return the
`event_id` and `item_id` they require, leaving three published writes
callable only by inventing an opaque id — `delete_calendar_event`, the
one destructive tool here, among them. `test_every_published_write_can_be_driven`
now fails if a write's required id has no published producer, because
"published" and "usable" turned out to be different properties.

Four things have to change on the way through, and each is a real
mismatch rather than tidying:

1. **Schema dialect.** `parameters_schema` is written for Gemini's
   function-calling, which spells types in uppercase (`"STRING"`).
   MCP wants JSON Schema. Lowercased recursively here.
2. **Descriptions.** The registry's are addressed to Genos's own
   assistant — they promise an approval card that only exists in the
   Genos UI, and they cross-reference tools by names this surface
   renames. Left alone they would instruct the caller to invoke tools
   that 404. Rewritten below, which also stops MCP's contract from
   moving every time the in-app prompt is tuned for evals.
3. **Task references.** Tools take a numeric `task_id`; people type
   `PRJ-123` or paste a URL. Resolved in `resolve.py` before the tool
   runs — including inside the array `update_tasks_bulk` takes, since a
   reference a person can type has to work everywhere one is accepted.
4. **Note references.** A note id collides across the three note tables,
   so every note tool needs a `note_type` alongside it. A pasted note URL
   carries both, so one paste answers both arguments (`resolve.py`).

**There is no approval gate on this surface, and that is the one thing to
understand before adding a write here.** In the app every
`requires_approval` tool pauses for a card the user confirms; MCP has no
user watching, so the API key's `write` scope *is* the standing
authorization, granted once when the key was minted. Nothing here
consults `requires_approval` — deliberately, since a pause nobody can
answer is a hang. Two consequences, both load-bearing:

* A published write runs the moment it is called. Prefer writes whose
  effect is **recoverable** — note and task edits snapshot their prior
  state (`snapshot_note_version`, `TaskActivity`), so a wrong one can be
  walked back.
* **A destructive tool needs a real undo before it belongs here.**
  `delete_calendar_event` is published because the registry's own
  description calls it out as destructive and a user asks for it by name;
  it is the only delete in the registry, and the asymmetry is on purpose
  rather than an oversight to be "fixed" by adding more.

Scope is enforced here rather than by `HasRequiredScope`, because MCP is
all-POST: the method-based rule would refuse a read-only key on a pure
read. Write tools are hidden from `tools/list` for a read key (the spec
blesses varying the set by the caller's authorization) *and* refused at
call time, since a client may be working from a cached list.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from django.conf import settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.views.public.mcp.resolve import resolve_note_ref, resolve_task_ref

log = logging.getLogger(__name__)

# Registry name -> published name, applied to every description string so
# a cross-reference can't outlive a rename. Renames are for legibility to
# an outside agent: `fetch_task` and `search_knowledge_base` describe
# Genos's internals; `get_task` and `search_workspace` describe the job.
_RENAMES = {
    "fetch_task": "get_task",
    "get_my_focus_tasks": "list_my_tasks",
    "search_knowledge_base": "search_workspace",
    "fetch_note": "get_note",
    "fetch_pr": "get_pr",
}
_RENAME_RE = re.compile(r"\b(" + "|".join(map(re.escape, _RENAMES)) + r")\b")

# A task reference as a person writes it. Replaces the `task_id` integer
# on every tool that takes one.
_TASK_REF_SCHEMA = {
    "type": "string",
    "description": ("The task: its display id (PRJ-123), its Genos URL, or its numeric id."),
}

# A note reference. Unlike a task there is no display-id form, so this
# widens the integer to also accept a pasted URL rather than replacing
# it — and the URL is worth accepting because it carries the `note_type`
# the id alone cannot imply.
_NOTE_REF_SCHEMA = {
    "type": "string",
    "description": (
        "The note: its Genos URL, or its numeric id. Note ids are only "
        "unique within a note family, so pass `note_type` too — unless you "
        "paste a URL, which carries both."
    ),
}


@dataclass(frozen=True)
class McpTool:
    """One published tool: what to call it, and what it maps to."""

    name: str
    registry_name: str
    description: str
    write: bool = False
    # Argument names whose value is a task reference to resolve before
    # the underlying tool sees it.
    task_ref_args: tuple[str, ...] = ()
    # Argument names holding a note reference. Resolving one may also
    # determine `note_type`, so it is resolved as a pair.
    note_ref_args: tuple[str, ...] = ()
    # `(list_arg, item_key)` pairs: a task reference nested inside an
    # array of objects, as `update_tasks_bulk` takes. A reference a
    # person can type has to work everywhere one is accepted, and a
    # batch is exactly where hand-typed ids show up.
    nested_task_ref_args: tuple[tuple[str, str], ...] = ()
    # Applied to the registry schema after dialect conversion.
    schema_overrides: dict[str, Any] = field(default_factory=dict)
    # Applied to a property nested inside an array item's schema, keyed
    # `(list_arg, item_key)` — the schema half of `nested_task_ref_args`.
    nested_schema_overrides: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)


def _rewrite_refs(text: str) -> str:
    return _RENAME_RE.sub(lambda m: _RENAMES[m.group(1)], text or "")


CATALOG: tuple[McpTool, ...] = (
    # ---- find and read ----
    McpTool(
        name="get_task",
        registry_name="fetch_task",
        description=(
            "Read one task in full: title, status, priority, assignee, dates, "
            "its description, and its comments. Prefer `content_markdown` for "
            "the description when it is present — it keeps headings, checklists "
            "and code fences — and fall back to `content_text`, the same body "
            "flattened. This is the first call when the user names a task to "
            "work on."
        ),
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="list_my_tasks",
        registry_name="get_my_focus_tasks",
        description=(
            "The caller's own tasks, most worth attention first (overdue and "
            "in-progress before the rest). Use this for 'what am I working "
            "on', 'my queue', or when the user asks you to pick up work "
            "without naming a task. `delivery` filters by GitHub PR: "
            "'open_pr' is what is in flight, 'merged_pr' is merged but the "
            "task is still not Closed (usually ready to close — check with "
            "`get_pr` first), 'no_pr' has no PR recorded."
        ),
    ),
    McpTool(
        name="list_tasks",
        registry_name="list_tasks",
        description=(
            "List tasks with filters — by project, milestone, status, "
            "priority, assignee, or overdue. Use this to survey a milestone "
            "before starting, or to find a task whose id you don't have."
        ),
    ),
    McpTool(
        name="list_projects",
        registry_name="list_projects",
        description=(
            "Every project the caller can see, with the numeric ids and "
            "short codes other tools take. Call this to turn a project name "
            "into an id."
        ),
    ),
    McpTool(
        name="list_milestones",
        registry_name="list_milestones",
        description=(
            "Milestones in a project, with due dates and progress. A "
            "milestone is how a multi-task piece of work is grouped — start "
            "here when the user names one rather than a single task. A "
            "milestone is also backed by a task, so `update_task` on that "
            "backing id is how a milestone's own title and dates change."
        ),
        # The registry description of `assignee_user_id` points at
        # `list_my_milestones`, which this surface does not publish —
        # `_rewrite_refs` can only rename a tool, not remove a reference
        # to an absent one. Overridden rather than renamed: the caller
        # already has `get_current_user` for "mine".
        schema_overrides={
            "assignee_user_id": {
                "type": "string",
                "description": (
                    "Only milestones this user is assigned to. A user UUID — "
                    "`get_current_user` for the caller's own, or "
                    "`get_team_members` to resolve a name."
                ),
            }
        },
    ),
    McpTool(
        name="get_milestone_summary",
        registry_name="get_milestone_summary",
        description=(
            "One milestone in depth: its tasks, their statuses, what is done "
            "and what remains. Use it to plan the order of work across a "
            "milestone."
        ),
    ),
    McpTool(
        name="get_task_blockers",
        registry_name="get_task_blockers",
        description=(
            "What a task is waiting on — its unfinished dependencies. Check "
            "before starting work, since a blocked task usually means "
            "finishing something else first."
        ),
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="get_task_history",
        registry_name="get_task_history",
        description=(
            "One task's change history, newest first: what changed, old and "
            "new value, who, when, and `via` — how it arrived: `app` (the "
            "Genos UI), `agent` (Genos's own assistant), `api_key` (with the "
            "key's name and prefix, so your own writes are recognisable), "
            "`pr_merge` (closed by a merged PR), `dependencies` (an automatic "
            "Blocked/Open flip), `github` (a linked PR or PR comment), or "
            "`unknown` for older rows. Use it to check "
            "a write stuck, or to find out who changed a task you are "
            "working on. Description edits are listed without their text."
        ),
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="find_task_by_delivery",
        registry_name="find_task_by_delivery",
        description=(
            "Which task a GitHub branch or PR is for. Pass `branch` (and "
            "`repo`, owner/name, if the name is used in several repos) or "
            "`pr_url`. Recorded links are checked first, then a display id "
            "in the branch name (feature/PRJ-42-x → PRJ-42). An empty "
            "`matches` means the branch is not linked to any task — nothing "
            "is guessed from titles. Call it when you resume work on a branch, "
            "to confirm the task before writing to it."
        ),
    ),
    McpTool(
        name="search_workspace",
        registry_name="search_knowledge_base",
        description=(
            "Semantic search across the workspace — tasks, notes, chat "
            "messages and todos. Use it for context the task description "
            "assumes: prior discussion of a bug, a design note, an earlier "
            "decision. Natural-language queries work better than keywords."
        ),
    ),
    McpTool(
        name="get_note",
        registry_name="fetch_note",
        description=(
            "Read a note's full body. Notes hold specs, designs and "
            "decisions — follow one when a task description points at it."
        ),
    ),
    McpTool(
        name="get_current_user",
        registry_name="get_current_user",
        description=(
            "Who this API key acts as: user id, name, team. Call it when you "
            "need the caller's own id for an assignee filter, or to confirm "
            "which account is making changes."
        ),
    ),
    McpTool(
        name="get_team_members",
        registry_name="get_team_members",
        description=(
            "The team's members and their user ids. Use it to turn a person's "
            "name into the id that assignee filters take."
        ),
    ),
    McpTool(
        name="list_project_members",
        registry_name="list_project_members",
        description=(
            "The members of ONE project, with their user ids — not the whole "
            "team. Use it before assigning work inside a project, since only "
            "a member of it can be given a task there."
        ),
    ),
    McpTool(
        name="list_task_dependencies",
        registry_name="list_task_dependencies",
        description=(
            "Every blocker→blocked edge in one project or one milestone — the "
            "whole dependency graph in a single call, rather than "
            "`get_task_blockers` per task. Read this before reordering a "
            "milestone's work: a blocker generally has to land first."
        ),
    ),
    McpTool(
        name="list_note_folders",
        registry_name="list_note_folders",
        description=(
            "How the caller's personal notes are filed into sidebar folders, "
            "with each folder's id and the notes in it. Folders are personal-"
            "only, and this is the only tool that sees them — `search_workspace` "
            "does not. Call it to turn a folder NAME into the `folder_id` that "
            "`create_note` and `update_note` take."
        ),
    ),
    McpTool(
        name="list_uncompleted_todos",
        registry_name="list_uncompleted_todos",
        description=(
            "The caller's still-open to-dos, newest first, with the `item_id` "
            "that `update_todo_item` takes. This is how a to-do is found: "
            "`search_workspace` can surface one but does not return its id. "
            "Scans back `date_range_days` (default 14), so widen it for an "
            "older item."
        ),
    ),
    McpTool(
        name="list_today_todos",
        registry_name="list_today_todos",
        description=(
            "The caller's to-dos for today, with their `item_id`s. Narrower "
            "than `list_uncompleted_todos` and it includes items already "
            "ticked off, so prefer it for 'what is left today' and that one "
            "for anything older."
        ),
    ),
    McpTool(
        name="list_calendar_events",
        registry_name="list_calendar_events",
        description=(
            "The caller's Google Calendar events in a date range, with the "
            "`event_id` that `update_calendar_event` and "
            "`delete_calendar_event` take. Call this first: those tools need "
            "an id that only comes from here, and for a delete it is the only "
            "way to confirm you have the right event. Recurring events are "
            "expanded into individual instances, so cancelling one occurrence "
            "means deleting that instance's own id. Needs Google connected "
            "under Integrations."
        ),
    ),
    McpTool(
        name="list_calendars",
        registry_name="list_calendars",
        description=(
            "The caller's Google Calendars and their ids. Only needed for a "
            "non-primary calendar — every calendar tool defaults to the "
            "primary one, so skip this unless the user names a specific "
            "calendar."
        ),
    ),
    # ---- GitHub pull requests ----
    #
    # The other half of the task → branch → PR loop: an agent that opened
    # a PR can see what happened to it. Every one takes a PR URL, which
    # is usually in hand already from a task comment. They run with the
    # key owner's own GitHub OAuth token, so the agent sees exactly the
    # repos that person can, and nothing when they haven't connected one.
    # A failure to reach GitHub is a tool error, never an empty list:
    # "no reviews" is a claim about the PR (`_github_errors.py`).
    McpTool(
        name="get_pr",
        registry_name="fetch_pr",
        description=(
            "Read one GitHub pull request by URL: its state (open, draft, "
            "merged or closed), when it merged, branches, diff size, and the "
            "start of its description. Use it to check what happened to a PR "
            "linked from a task, e.g. whether it merged before you close the "
            "task. Runs as the API key's owner, through the GitHub account "
            "they connected under Integrations in Genos; a repo they can't "
            "see returns not-found."
        ),
    ),
    McpTool(
        name="list_pr_reviews",
        registry_name="list_pr_reviews",
        description=(
            "The formal reviews on a GitHub pull request, given its URL, "
            "newest first: who reviewed, the verdict (APPROVED, "
            "CHANGES_REQUESTED, COMMENTED, DISMISSED) and the review summary. "
            "Call it to find out whether your PR was approved or needs "
            "changes. Line-level feedback is in `list_pr_comments`."
        ),
    ),
    McpTool(
        name="list_pr_comments",
        registry_name="list_pr_comments",
        description=(
            "Every comment on a GitHub pull request, given its URL, newest "
            "first: the conversation and the inline review comments, with "
            "`file_path` and `line` for the inline ones. This is where the "
            "concrete review feedback to act on lives."
        ),
    ),
    McpTool(
        name="list_pr_files",
        registry_name="list_pr_files",
        description=(
            "The files a GitHub pull request changes, given its URL, each "
            "with its status (added, modified, removed, renamed) and line "
            "counts. No patch content."
        ),
    ),
    McpTool(
        name="list_pr_commits",
        registry_name="list_pr_commits",
        description=(
            "The commits on a GitHub pull request, given its URL, in order: "
            "short sha, author, first line of the message, date. Use it to "
            "see whether a fix you pushed has landed on the PR."
        ),
    ),
    # ---- write: needs a write-scoped key ----
    McpTool(
        name="update_task",
        registry_name="update_task",
        description=(
            "Change a task: status, title, description, priority, effort, "
            "due date or milestone. Set status to 'WIP' when you start and "
            "'Closed' when the work is merged. `content_text` replaces the "
            "whole description, so read the task first and send the full new "
            "body rather than a fragment. `milestone_id` (from "
            "`list_milestones`, same project) files the task and its sub-tasks "
            "under that milestone; '' takes it out. Requires a write-scoped "
            "API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="add_comment",
        registry_name="add_comment",
        description=(
            "Post a comment on a task. This is how you report back — the PR "
            "link, what you changed, anything you had to decide. The comment "
            "appears in the task's thread and notifies the assignee. "
            "Requires a write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="add_task_delivery",
        registry_name="add_task_delivery",
        description=(
            "Record on a task which GitHub branch and/or PR is delivering it, "
            "so `find_task_by_delivery`, `get_task` and `list_my_tasks` can "
            "see it. A PR on a branch named after the task (PRJ-42, "
            "feature/PRJ-42-x) is linked automatically once the repo's "
            "webhook is registered, so this is for a branch that doesn't "
            "name the task, or a branch with no PR yet. Pass `pr_url`, or "
            "`branch` + `repo` (owner/name). Repeating it is harmless. "
            "Requires a write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="create_task",
        registry_name="create_task",
        description=(
            "Create a task in a project. Use it to split discovered work out "
            "of the task you're on, rather than widening that one. Get the "
            "project id from `list_projects`. `content_text` is markdown — "
            "headings, check lists, code fences and tables are preserved — "
            "and `milestone_id` (from `list_milestones`) files the task under "
            "an existing milestone in the same project. Requires a "
            "write-scoped API key."
        ),
        write=True,
    ),
    McpTool(
        name="assign_task",
        registry_name="assign_task",
        description=(
            "Assign a task to someone, or unassign it by omitting "
            "`assignee_id`. The assignee must already be on the team or the "
            "task's project — `list_project_members` or `get_team_members` to "
            "resolve a name, `get_current_user` for yourself. Requires a "
            "write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={"task_id": _TASK_REF_SCHEMA},
    ),
    McpTool(
        name="update_tasks_bulk",
        registry_name="update_tasks_bulk",
        description=(
            "Change priority, due date, status, effort or milestone on MANY "
            "tasks in one call — for reprioritizing or tidying a milestone, or "
            "filing a batch of tasks under one (`milestone_id`, '' to take a "
            "task out). Read the "
            "current state first (`list_tasks`, and `list_task_dependencies` "
            "so blockers land before what they block), since each entry needs "
            "a one-sentence `rationale` recorded with the change. "
            "All-or-nothing: one invalid entry rejects the whole batch. Use "
            "`update_task` for a single task. Requires a write-scoped API key."
        ),
        write=True,
        nested_task_ref_args=(("updates", "task_id"),),
        nested_schema_overrides={
            ("updates", "task_id"): _TASK_REF_SCHEMA,
            # The registry's text promises "the user reads this in the
            # approval card" — there is no card on this surface. The
            # field is still worth requiring: it is recorded with the
            # change, so it is what someone reading the task's history
            # later finds instead of an unexplained edit.
            ("updates", "rationale"): {
                "type": "string",
                "description": (
                    "One sentence: WHY this task is changing. Recorded with "
                    "the change, so it is what the task's history shows."
                ),
            },
        },
    ),
    McpTool(
        name="create_task_plan",
        registry_name="create_task_plan",
        description=(
            "Create a whole plan in one call: a new milestone with its tasks "
            "and their dependencies, or a batch of tasks under an existing "
            "milestone (`existing_milestone_id`) or as sub-tasks of an "
            "existing task (`parent_task_id`). Use this instead of repeated "
            "`create_task` calls whenever the work is related — it is the only "
            "way to wire dependencies between tasks being created, by array "
            "index (`blocked_by_indexes`). Max 20 tasks. Requires a "
            "write-scoped API key."
        ),
        write=True,
        task_ref_args=("parent_task_id",),
        schema_overrides={
            "parent_task_id": {
                **_TASK_REF_SCHEMA,
                "description": (
                    "Optional: nest every task in this batch under this "
                    "existing task, as sub-tasks. Its display id (PRJ-123), "
                    "its Genos URL, or its numeric id."
                ),
            }
        },
    ),
    McpTool(
        name="create_note",
        registry_name="create_note",
        description=(
            "Write a new note — a document, not a task. Use it for a spec, a "
            "design, a research write-up or a hand-off: anything whose value "
            "is the prose. Two kinds: 'personal' (private to you, optionally "
            "filed into a folder via `folder_id` from `list_note_folders`) or "
            "'task' (shared with a project, needs `project_id`, optionally "
            "`task_id` to attach it to one task). `content_text` is markdown — "
            "headings, lists and code fences are preserved. When the work "
            "wants TASKS rather than a document, use `create_task_plan`. "
            "Chat-attached notes cannot be created here. Requires a "
            "write-scoped API key."
        ),
        write=True,
        task_ref_args=("task_id",),
        schema_overrides={
            "task_id": {
                **_TASK_REF_SCHEMA,
                "description": (
                    "Optional, and only with note_type='task': attach the note "
                    "to one task in the project. Its display id (PRJ-123), its "
                    "Genos URL, or its numeric id."
                ),
            }
        },
    ),
    McpTool(
        name="update_note",
        registry_name="update_note",
        description=(
            "Edit an existing note's title and/or body, or move a personal "
            "note between folders. `content_text` REPLACES the entire body, so "
            "read the note with `get_note` first and send the full new text — "
            "every section you are not changing included — rather than a "
            "fragment. Every edit is snapshotted into the note's version "
            "history, so a change can be rolled back in the Genos UI. You must "
            "be the note's owner, an editor on it, or (for a task note) a "
            "member of its project. Chat notes cannot be edited here. Requires "
            "a write-scoped API key."
        ),
        write=True,
        note_ref_args=("note_id",),
        schema_overrides={"note_id": _NOTE_REF_SCHEMA},
    ),
    McpTool(
        name="create_todo_item",
        registry_name="create_todo_item",
        description=(
            "Add a personal to-do for the caller — a private checklist item, "
            "not project work anyone else can see. Use a task for work that "
            "belongs to a project. Only `title` is required; `target_date` "
            "defaults to today. Requires a write-scoped API key."
        ),
        write=True,
    ),
    McpTool(
        name="update_todo_item",
        registry_name="update_todo_item",
        description=(
            "Change one of the caller's to-dos: tick it off with "
            "`is_completed`, edit the title, or replace its notes. Get "
            "`item_id` from `list_uncompleted_todos` or `list_today_todos`. "
            '`notes_text: ""` clears the body; `clear_category: true` removes '
            "the category. Requires a write-scoped API key."
        ),
        write=True,
    ),
    McpTool(
        name="create_calendar_event",
        registry_name="create_calendar_event",
        description=(
            "Put an event on the caller's Google Calendar. Pass "
            "`YYYY-MM-DD` for an all-day event — `end_iso` is EXCLUSIVE, so a "
            "single day ends on the next — or a full `YYYY-MM-DDTHH:MM:SS` for "
            "a timed one. Supply `time_zone` whenever you know it rather than "
            "letting the server guess. Requires a write-scoped API key."
        ),
        write=True,
    ),
    McpTool(
        name="update_calendar_event",
        registry_name="update_calendar_event",
        description=(
            "Change an existing calendar event; omit any field you are "
            "leaving alone. Get `event_id` from `list_calendar_events`. To "
            "move it, send `start_iso` AND `end_iso` together — Google "
            "rejects a half-updated time. `attendees` replaces the whole "
            "invitee list, so `[]` clears it. Requires a write-scoped API key."
        ),
        write=True,
    ),
    McpTool(
        name="delete_calendar_event",
        registry_name="delete_calendar_event",
        description=(
            "Delete a calendar event. **This is not reversible from here** — "
            "there is no undo on this surface and the event would have to be "
            "recreated by hand, so only call it when the user has asked for "
            "that specific event to be cancelled. Get `event_id` from "
            "`list_calendar_events` and check the summary and time you got "
            "back really are the event meant: an id is not something to "
            "construct or guess. Deleting an already-deleted event succeeds. "
            "Requires a write-scoped API key."
        ),
        write=True,
    ),
)

BY_NAME = {t.name: t for t in CATALOG}


# --------------------------------------------------------------------------
# schema conversion
# --------------------------------------------------------------------------


def _to_json_schema(node: Any) -> Any:
    """Lowercase Gemini's uppercase type names, recursively.

    The registry schemas use only `type` / `properties` / `required` /
    `description` / `enum` / `items`, all of which are standard JSON
    Schema once the type spelling is fixed — so this is the whole
    conversion, not the first pass of one.
    """
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key == "type" and isinstance(value, str):
                out[key] = value.lower()
            elif key == "description" and isinstance(value, str):
                out[key] = _rewrite_refs(value)
            else:
                out[key] = _to_json_schema(value)
        return out
    if isinstance(node, list):
        return [_to_json_schema(v) for v in node]
    return node


def input_schema(tool: McpTool) -> dict[str, Any]:
    schema = _to_json_schema(REGISTRY[tool.registry_name].parameters_schema)
    if tool.schema_overrides:
        properties = dict(schema.get("properties") or {})
        properties.update(tool.schema_overrides)
        schema = {**schema, "properties": properties}
    for (list_arg, item_key), override in tool.nested_schema_overrides.items():
        # Rebuild the path `properties[list_arg].items.properties[item_key]`
        # without mutating the registry's own schema dict, which is module
        # state shared with the in-app agent.
        properties = dict(schema.get("properties") or {})
        array_schema = dict(properties.get(list_arg) or {})
        items = dict(array_schema.get("items") or {})
        item_properties = dict(items.get("properties") or {})
        if item_key not in item_properties:
            # The nesting moved under us. Leave the schema as the registry
            # wrote it rather than inventing a property: a wrong override
            # here is how a silent no-op gets shipped.
            log.warning(
                "MCP nested schema override %s.%s did not match the registry schema",
                list_arg,
                item_key,
            )
            continue
        item_properties[item_key] = override
        items["properties"] = item_properties
        array_schema["items"] = items
        properties[list_arg] = array_schema
        schema = {**schema, "properties": properties}
    return schema


def _disabled_tools() -> set[str]:
    """The `AGENT_DISABLED_TOOLS` ops kill switch, honoured here too.

    It names REGISTRY tools, so it is matched on `registry_name`: an
    operator switching off `add_comment` in an incident means everywhere,
    not just in the app. Fail-open on an unset value, matching the
    agent controller — a service that misses the env var runs at full
    capability rather than silently losing tools.
    """
    configured = settings.SEARCH_ENGINE.get("AGENT_DISABLED_TOOLS") or frozenset()
    return set(configured)


def visible_tools(scope: str) -> list[McpTool]:
    """The catalog this key may see, in a stable order.

    Write tools are hidden from a read-only key rather than listed and
    then refused: the spec allows the set to vary by the caller's
    authorization, and hiding stops the agent spending turns on a tool
    it can never call. Call-time refusal still exists for a client
    working from a cached list.

    Order is the catalog's, which is fixed — clients cache the list, and
    a stable order keeps prompt caches warm.
    """
    disabled = _disabled_tools()
    return [
        t
        for t in CATALOG
        if t.registry_name not in disabled and (scope == SCOPE_WRITE or not t.write)
    ]


def describe(tool: McpTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": input_schema(tool),
    }


# --------------------------------------------------------------------------
# invocation
# --------------------------------------------------------------------------


def _strip_workspace_content(value: Any) -> Any:
    """Remove the `<workspace_content>` fences from tool output.

    They are a prompt-injection guard aimed at Genos's own system prompt,
    which instructs the model to treat anything inside them as data. The
    agent on the other end of MCP has its own defences and no idea what
    these tags mean, so leaving them in is noise that costs tokens and
    reads like markup in the task body.
    """
    if isinstance(value, str):
        if value.startswith("<workspace_content>\n") and value.endswith("\n</workspace_content>"):
            return value[len("<workspace_content>\n") : -len("\n</workspace_content>")]
        return value
    if isinstance(value, dict):
        return {k: _strip_workspace_content(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_strip_workspace_content(v) for v in value]
    return value


def _text_result(text: str, *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def call(
    name: str,
    arguments: dict[str, Any],
    ctx: ToolContext,
    scope: str,
    *,
    on_unknown_tool: Callable[[str], Exception],
) -> dict[str, Any]:
    """Run a published tool and shape its output as a `CallToolResult`.

    Returns `isError: true` for anything the caller could plausibly fix
    by trying differently — a bad task reference, a missing permission,
    the wrong key scope. Those go back to the model, which is the only
    party that can act on them. An *unknown tool* is not that: it means
    the client and server disagree about what exists, so it raises and
    becomes a JSON-RPC protocol error instead.
    """
    tool = BY_NAME.get(name)
    if tool is None or tool.registry_name in _disabled_tools():
        raise on_unknown_tool(name)

    if tool.write and scope != SCOPE_WRITE:
        return _text_result(
            f"`{name}` writes to the workspace, and this API key is read-only. "
            f"Create a key with write scope in Genos under Settings → Developer, "
            f"then reconnect. Reads will keep working with the current key.",
            is_error=True,
        )

    args = dict(arguments or {})
    try:
        for arg in tool.task_ref_args:
            if arg in args and args[arg] is not None:
                args[arg] = resolve_task_ref(args[arg], ctx.team_id)
        for arg in tool.note_ref_args:
            if arg in args and args[arg] is not None:
                note_id, note_type = resolve_note_ref(args[arg], ctx.team_id)
                args[arg] = note_id
                # Only a reference that actually determined the family
                # overrides what the caller passed. A bare id determines
                # nothing, and guessing would turn a wrong `note_type`
                # into a confident write against the wrong table.
                if note_type is not None:
                    args["note_type"] = note_type
        for list_arg, item_key in tool.nested_task_ref_args:
            entries = args.get(list_arg)
            if not isinstance(entries, list):
                continue
            resolved = []
            for entry in entries:
                if isinstance(entry, dict) and entry.get(item_key) is not None:
                    entry = {**entry, item_key: resolve_task_ref(entry[item_key], ctx.team_id)}
                resolved.append(entry)
            args[list_arg] = resolved
        payload = REGISTRY[tool.registry_name].run(args, ctx)
    except ToolError as exc:
        # The tool's own vocabulary — not authorized, not found, invalid
        # argument. Exactly the feedback a model can retry against.
        return _text_result(str(exc), is_error=True)

    payload = _strip_workspace_content(dict(payload or {}))
    # `__summary__` is the one-line human summary Genos renders in its own
    # UI. Promoted to the first line here so the answer leads with what
    # happened, and dropped from the body so it isn't said twice.
    summary = payload.pop("__summary__", None)

    body = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    text = f"{summary}\n\n{body}" if summary else body
    return {
        "content": [{"type": "text", "text": text}],
        # The parsed payload alongside the text, so a client that
        # understands structured results doesn't have to re-parse ours.
        "structuredContent": payload,
        "isError": False,
    }
