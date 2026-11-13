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

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
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
from origin.search_engine.agent.tools.task_anchors import enabled as anchors_enabled
from origin.search_engine.agent.tools.task_anchors import task_anchors
from origin.search_engine.agent.tools.task_body_edit import body_hash
from origin.search_engine.agent.tools.task_custom_fields import (
    project_fields,
    read_custom_fields,
    stored_member_ids,
)
from origin.search_engine.agent.tools.task_tags import tag_names
from origin.search_engine.agent.tools.template_prompts import without_untouched_prompts
from origin.search_engine.text_extraction import extract_text
from origin.services.task_delivery import delivery_as_dict
from origin.views.utils.request_validators import SAFE_LINK_SCHEMES, link_scheme

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


def _sprint(task: TaskMaster, ctx: ToolContext, is_member: bool) -> dict[str, Any] | None:
    """The task's sprint. A task has no sprint of its own to pick: it takes its
    milestone's. The name is the project's, so it is shown to a project member
    only (as `list_sprints` shows it); anyone else reading the task gets the id."""
    if task.sprint_id is None:
        return None
    sprint = Sprint.objects.filter(sprint_id=task.sprint_id, is_deleted=False).first()
    if sprint is None:
        return None
    named = is_member and str(sprint.team_id or "") == ctx.team_id
    return {
        "id": sprint.sprint_id,
        "name": wrap_workspace_content(sprint.name) if named else None,
    }


def _links(task: TaskMaster) -> list[dict[str, Any]]:
    """The task's links: the title as typed (fenced) and the URL, bare, like a
    delivery's `pr_url`. A link whose scheme is not http(s) is left out: the
    product no longer accepts one, and an older row is not worth handing on."""
    out = []
    for link in task.links if isinstance(task.links, list) else []:
        url = link.get("url") if isinstance(link, dict) else None
        if not isinstance(url, str) or not url:
            continue
        if link_scheme(url) not in ("", *SAFE_LINK_SCHEMES):
            continue
        title = link.get("title")
        out.append(
            {
                "title": wrap_workspace_content(title)
                if isinstance(title, str) and title
                else None,
                "url": url,
            }
        )
    return out


def _details(task: TaskMaster, ctx: ToolContext) -> dict[str, Any]:
    """Collaborators and custom fields, with the people named.

    Usernames come from `LabelAccess.usernames`: the people `get_team_members`
    would list to this caller, so reading a task never names someone the caller
    could not otherwise see. Custom fields are the project's, and the REST list
    of them is for its members, so a reader who is only the assignee or reporter
    gets collaborators but no `custom_fields` key (absent, not empty: empty would
    say the project has none).
    """
    # Imported here: `label_acl` imports `tools.base`, which runs this package's
    # `__init__`, which imports this module.
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415

    is_member = ProjectMembers.objects.filter(
        project_id=task.project_id, attendee_id=ctx.user_id
    ).exists()
    fields = project_fields(task.project_id) if is_member else []
    collaborators = list(task.collaborators.all())
    wanted = {str(c.id) for c in collaborators} | stored_member_ids(
        task.custom_field_values, fields
    )
    names = LabelAccess(ctx).usernames(wanted) if wanted else {}
    out: dict[str, Any] = {
        "start_date": task.start_date.isoformat() if task.start_date else None,
        "sprint": _sprint(task, ctx, is_member),
        "collaborators": sorted(
            (
                {
                    "user_id": str(c.id),
                    "username": wrap_workspace_content(names[str(c.id)])
                    if str(c.id) in names
                    else None,
                }
                for c in collaborators
            ),
            key=lambda row: (row["username"] or "", row["user_id"]),
        ),
        "links": _links(task),
    }
    if is_member:
        out["custom_fields"] = read_custom_fields(task.custom_field_values, fields, names)
    return out


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
    # Over MCP (MIP-826) the plain copy is left out when the markdown is
    # there: it is the same words again, and the caller pays for both.
    content_markdown = blocks_to_markdown(body)
    body_fields: dict[str, Any] = {}
    if not (ctx.surface == "mcp" and content_markdown):
        body_fields["content_text"] = wrap_workspace_content(content_text)
    body_fields["content_markdown"] = wrap_workspace_content(content_markdown)

    # Most-recent N comments. The chunker indexes all comments, but the
    # LLM rarely needs full history — last 20 is plenty for grounding.
    comments_qs = TaskComments.objects.filter(task=task, is_deleted=False).order_by("-comment_id")[
        :_COMMENTS_CAP
    ]
    comments = []
    for c in reversed(list(comments_qs)):  # back into chronological order
        text = extract_text(c.comment_body)
        # The skip stays keyed on the plain text so both surfaces list the
        # same comments (an image-only comment has no text and is hidden).
        if not text:
            continue
        if ctx.surface == "mcp":
            # MIP-854: an external caller gets the comment with its structure
            # (paragraphs, lists, code fences, chips as tokens) in place of
            # `text`, one copy of it. `blocks_to_markdown` degrades to the
            # plain text on shapes it does not know.
            body_key, body_val = "markdown", blocks_to_markdown(c.comment_body) or text
        else:
            body_key, body_val = "text", text
        comments.append(
            {
                "comment_id": c.comment_id,
                "sender_id": str(getattr(c, "sender_id", "") or ""),
                body_key: wrap_workspace_content(body_val),
                "ts": c.ts_sent_at.isoformat() if c.ts_sent_at else None,
            }
        )

    # The branches and PRs delivering the task (MIP-687), oldest first —
    # the order the work happened in.
    deliveries = [
        delivery_as_dict(d, wrap_workspace_content)
        for d in TaskDelivery.objects.filter(task=task).order_by("delivery_id")
    ]

    # Where the task's thread, chat and note are, when the caller may open them (MIP-1288).
    # Flag off: no key at all, the result is what it always was.
    anchors = {"anchors": task_anchors(task, ctx)} if anchors_enabled() else {}

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
        **_details(task, ctx),
        # What `update_task(base_body_hash=...)` checks a later write against
        # (MIP-824). Over the body as shown here, prompts left out.
        "body_hash": body_hash(task.content),
        **body_fields,
        "comments": comments,
        "deliveries": deliveries,
        **anchors,
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
        "row), plus `start_date`, `sprint` ({id, name}; it comes from the "
        "milestone), `collaborators` ({user_id, username}), `links` and "
        "`custom_fields` (by field name: {name, type, value}, or `values` "
        "for a tag field; only fields with a value are listed). Use after "
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
