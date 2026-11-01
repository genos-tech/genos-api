"""`fetch_task` tool — load one task with its comments.

Direct ORM access (no HTTP roundtrip to GetTaskView). Returns the
minimal LLM-friendly fields: title, status, content text, last N
comments. The existing detail endpoint returns base64 attachments
and UI-formatted dates — none of which help grounding.

ACL: requesting user must be in the task's project OR be the
assignee / reporter — same set the chunker stamps onto each chunk's
`acl_user_ids` field.
"""

from __future__ import annotations

from typing import Any

from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.task_tags import tag_names
from origin.search_engine.agent.tools.template_prompts import without_untouched_prompts
from origin.search_engine.text_extraction import extract_text
from origin.services.task_delivery import delivery_as_dict

_COMMENTS_CAP = 20


def _hierarchy(task: TaskMaster, ctx: ToolContext) -> dict[str, Any]:
    """Where the task sits (MIP-779): its parent, its milestone, and the
    milestone it is the backing row of.

    `milestone_id` is the task's own milestone link. A milestone's backing row
    carries ITS milestone's id there, which on its own cannot tell "in a
    milestone" from "is the milestone", so `backs_milestone_id` says which.
    A child attached to a milestone by parent only (no milestone link) shows
    the backing task as its parent and a null `milestone_id`.

    The parent's display id is a label from another row, so it follows the
    same rule as the rest of the tool: it is returned only to someone who may
    read the parent. Otherwise only the numeric id comes back.
    """
    parent_display_id = None
    parent_id = task.parent_task_id
    if parent_id is not None:
        parent = (
            TaskMaster.objects.select_related("project")
            .filter(task_id=parent_id, is_deleted=False)
            .first()
        )
        if (
            parent is not None
            and str(parent.team_id or "") == ctx.team_id
            and ctx.user_id
            in task_acl_user_ids(parent.project_id, parent.assignee_id, parent.reporter_id)
        ):
            parent_display_id = parent.display_id
    backs = (
        MilestoneMaster.objects.filter(task_id=task.task_id, is_deleted=False)
        .values_list("milestone_id", flat=True)
        .first()
    )
    return {
        "parent_task_id": parent_id,
        "parent_display_id": parent_display_id,
        "milestone_id": task.milestone_id,
        "is_milestone": bool(task.is_milestone),
        "backs_milestone_id": backs,
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"task_id must be an integer (got {raw_task_id!r}).")

    try:
        # select_related("project") lets the display_id property
        # ("<project.code>-<project_task_number>") resolve without an
        # extra query.
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")

    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Tenant guard first — never leak data across teams even if the
    # within-team ACL check would let the user through.
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: task is in a different team.")

    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to read task {task_id}.")

    # Template prompts the author never filled in are instructions, not
    # content (MIP-825); read the body without them.
    body = without_untouched_prompts(task.content)
    content_text = extract_text(body)
    # `content_markdown` keeps the body's structure — headings, list
    # markers, checkbox state, code fences. A task description is a spec,
    # and a reader acting on it needs to tell "- [x] done" from
    # "- [ ] todo", which the flattened form cannot express. Returned
    # ALONGSIDE `content_text` rather than replacing it: existing callers
    # (and the evals pinned to them) keep the string they were built on.
    content_markdown = blocks_to_markdown(body)

    # Most-recent N comments. The chunker indexes all comments, but the
    # LLM rarely needs full history — last 20 is plenty for grounding.
    comments_qs = TaskComments.objects.filter(task=task, is_deleted=False).order_by("-comment_id")[
        :_COMMENTS_CAP
    ]
    comments = []
    for c in reversed(list(comments_qs)):  # back into chronological order
        text = extract_text(c.comment_body)
        if not text:
            continue
        comments.append(
            {
                "comment_id": c.comment_id,
                "sender_id": str(getattr(c, "sender_id", "") or ""),
                "text": wrap_workspace_content(text),
                "ts": c.ts_sent_at.isoformat() if c.ts_sent_at else None,
            }
        )

    # The branches and PRs delivering the task (MIP-687), oldest first —
    # the order the work happened in.
    deliveries = [
        delivery_as_dict(d, wrap_workspace_content)
        for d in TaskDelivery.objects.filter(task=task).order_by("delivery_id")
    ]

    return {
        "task_id": task_id,
        "display_id": task.display_id,
        "title": task.title or "",
        "status": task.status,
        "priority": task.priority or None,  # "" and NULL both mean unset (MIP-781)
        "effort_level": task.effort_level or None,
        "due_date": task.due_date.isoformat() if task.due_date else None,
        "project_id": task.project_id or None,
        "assignee_id": str(task.assignee_id) if task.assignee_id else None,
        "reporter_id": str(task.reporter_id) if task.reporter_id else None,
        "tags": tag_names(task.tags),
        **_hierarchy(task, ctx),
        "content_text": wrap_workspace_content(content_text),
        "content_markdown": wrap_workspace_content(content_markdown),
        "comments": comments,
        "deliveries": deliveries,
        "__summary__": (
            f"Loaded task {task.display_id}" + (f" + {len(comments)} comments" if comments else "")
        ),
    }


FETCH_TASK = Tool(
    name="fetch_task",
    description=(
        "Load the full content of one task: title, description text, "
        "status, priority, assignee/reporter, tag names, the most recent "
        "comments, `deliveries` — the GitHub branches and PRs "
        "linked to it, with each PR's state — and where it sits: "
        "`parent_task_id` / `parent_display_id`, `milestone_id`, and "
        "`backs_milestone_id` (set when this task IS a milestone's own "
        "row). Use after "
        "`search_knowledge_base` if the snippet isn't enough and you "
        "need to read the task body or its discussion. ACL is enforced — only tasks the user can access "
        "are returned."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id (e.g. 123).",
            },
        },
        "required": ["task_id"],
    },
    run=_run,
)
