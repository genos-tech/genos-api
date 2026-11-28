"""The live fields of the project-chat task and milestone cards (MIP-2484).

The PM-channel card message stores a plain BlockNote body (`task_card`, `task_card_post`),
which is what search, notifications and a client that cannot read the task still show. The
app draws its card from what is here instead: the rows as they are now, for the person asking,
in whatever language their client speaks. Values are the stored enum strings; the client
translates them.

Permission: every task named in an answer (the card's own, its parent, its blockers) is one
`task_reader_pairs` says the viewer may open. A milestone card, and the milestone named on a
task card, also need the viewer on the project, which is the milestone view's rule. What may
not be read is left out, so it answers exactly as what does not exist.

A fixed number of queries however many cards are asked for
(`tests/test_task_cards_view.py`).
"""

from __future__ import annotations

from django.db.models import Prefetch, Q

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.text_extraction import extract_text
from origin.services.activity_minimal_card import task_reader_pairs
from origin.services.milestone_rollup import milestone_status_counts
from origin.services.task_status_values import FINISHED_STATUSES

# One request's cards. The client splits a longer list.
MAX_CARDS = 50
EXCERPT_CHARS = 160
MAX_TAGS = 5
MAX_ASSIGNEES = 5
MAX_BLOCKERS = 2
# A dependency stops blocking when its blocker is finished or gone.
_DONE_STATUSES = FINISHED_STATUSES


def task_cards(viewer_id, task_ids) -> list[dict]:
    """The cards of those of `task_ids` that `viewer_id` may open, in the order asked."""
    ids = list(dict.fromkeys(task_ids))
    tasks = {
        t.task_id: t
        for t in TaskMaster.objects.filter(task_id__in=ids, is_init_task=False).select_related(
            "project", "assignee", "reporter", "milestone"
        )
    }
    if not tasks:
        return []

    blockers: dict[int, list[int]] = {}
    blocked = [t.task_id for t in tasks.values() if t.status == "Blocked" and not t.is_milestone]
    if blocked:
        for blocked_id, blocker_id in (
            TaskDependency.objects.filter(
                blocked_task_id__in=blocked, blocker_task__is_deleted=False
            )
            .exclude(blocker_task__status__in=_DONE_STATUSES)
            .order_by("id")
            .values_list("blocked_task_id", "blocker_task_id")
        ):
            blockers.setdefault(blocked_id, []).append(blocker_id)

    # What the cards name beside themselves: parents, milestones' own rows, blockers.
    referenced = {t.parent_task_id for t in tasks.values() if t.parent_task_id}
    referenced |= {
        t.milestone.task_id for t in tasks.values() if t.milestone and t.milestone.task_id
    }
    referenced |= {b for found in blockers.values() for b in found}
    readable = {task_id for _, task_id in task_reader_pairs([viewer_id], set(tasks) | referenced)}
    tasks = {task_id: t for task_id, t in tasks.items() if task_id in readable}
    if not tasks:
        return []
    others = {
        t.task_id: t
        for t in TaskMaster.objects.filter(task_id__in=referenced & readable).select_related(
            "project"
        )
    }

    project_ids = {t.project_id for t in [*tasks.values(), *others.values()] if t.project_id}
    on_project = set(
        ProjectMembers.objects.filter(
            attendee_id=viewer_id, project_id__in=project_ids
        ).values_list("project_id", flat=True)
    ) | set(
        ProjectMaster.objects.filter(owner_id=viewer_id, project_id__in=project_ids).values_list(
            "project_id", flat=True
        )
    )

    # A milestone is one of the cards (its backing row was asked for), or is named by one: by
    # the task's own milestone, or because the task hangs under the milestone's row.
    backing_ids = {t.task_id for t in tasks.values() if t.is_milestone}
    named_backing_ids = {t.task_id for t in others.values() if t.is_milestone}
    named_ids = {t.milestone_id for t in tasks.values() if t.milestone_id}
    milestones: list[MilestoneMaster] = []
    if backing_ids or named_backing_ids or named_ids:
        milestones = list(
            MilestoneMaster.objects.filter(
                Q(task_id__in=backing_ids | named_backing_ids) | Q(milestone_id__in=named_ids),
                is_deleted=False,
                project_id__in=on_project,
            )
            .select_related("sprint", "reporter", "task__project")
            .prefetch_related(
                Prefetch(
                    "milestone_assignees",
                    queryset=MilestoneAssignees.objects.select_related("user").order_by("id"),
                )
            )
        )
    by_milestone_id = {m.milestone_id: m for m in milestones}
    by_backing_id = {m.task_id: m for m in milestones if m.task_id}
    counts = milestone_status_counts([by_backing_id[i] for i in backing_ids if i in by_backing_id])

    cards = []
    for task_id in ids:
        task = tasks.get(task_id)
        if task is None:
            continue
        if task.is_milestone:
            milestone = by_backing_id.get(task_id)
            if milestone is not None:
                cards.append(_milestone_card(milestone, task, counts[milestone.milestone_id]))
            continue
        cards.append(
            _task_card(
                task,
                others=others,
                by_milestone_id=by_milestone_id,
                by_backing_id=by_backing_id,
                blockers=[others[b] for b in blockers.get(task_id, []) if b in others],
            )
        )
    return cards


def _task_card(task, *, others, by_milestone_id, by_backing_id, blockers) -> dict:
    parent = others.get(task.parent_task_id)
    milestone = by_milestone_id.get(task.milestone_id) or by_backing_id.get(task.parent_task_id)
    # The milestone's row is shown as the milestone, not a second time as the parent.
    if parent is not None and parent.is_milestone:
        parent = None
    stored = task.tags if isinstance(task.tags, list) else []
    tags = [t for t in stored if isinstance(t, dict) and t.get("tagName")]
    return {
        **_common(task, task.content),
        "kind": "task",
        "status": task.status or "Open",
        "priority": task.priority or None,
        "effortLevel": task.effort_level or None,
        "assignee": _person(task.assignee),
        "reporter": _person(task.reporter),
        "dueDate": _date(task.due_date),
        "startDate": _date(task.start_date),
        "tags": [
            {
                "tagName": str(t["tagName"]),
                "tagColor": str(t.get("tagColor") or ""),
                "tagTextColor": str(t.get("tagTextColor") or ""),
            }
            for t in tags[:MAX_TAGS]
        ],
        "tagCount": len(tags),
        "milestone": _milestone_ref(milestone),
        "parent": _task_ref(parent),
        "blockedBy": [_task_ref(b) for b in blockers[:MAX_BLOCKERS]],
        "blockedByCount": len(blockers),
    }


def _milestone_card(milestone, task, by_status) -> dict:
    """The milestone's row is the source of its fields; its backing task gives the id."""
    assignees = [row.user for row in milestone.milestone_assignees.all()]
    return {
        **_common(task, milestone.description),
        "kind": "milestone",
        "milestoneId": milestone.milestone_id,
        "title": milestone.title,
        "status": milestone.status or "Open",
        "priority": milestone.priority or None,
        "reporter": _person(milestone.reporter),
        "dueDate": _date(milestone.due_date),
        "startDate": _date(milestone.start_date),
        "createdAt": _date(milestone.ts_created_at),
        "updatedAt": _date(milestone.ts_updated_at),
        "sprint": (
            {"sprintId": milestone.sprint_id, "name": milestone.sprint.name}
            if milestone.sprint_id
            else None
        ),
        "assignees": [_person(u) for u in assignees[:MAX_ASSIGNEES]],
        "assigneeCount": len(assignees),
        "progress": {
            "total": sum(by_status.values()),
            "closed": by_status.get("Closed", 0),
            "byStatus": by_status,
        },
    }


def _common(task, body) -> dict:
    text = " ".join(extract_text(body).split())
    if len(text) > EXCERPT_CHARS:
        text = text[: EXCERPT_CHARS - 1].rstrip() + "…"
    return {
        "taskId": task.task_id,
        "displayId": task.display_id,
        "title": task.title,
        "excerpt": text,
        "createdAt": _date(task.ts_created_at),
        "updatedAt": _date(task.ts_updated_at),
        # Which project to open it in (a card in a DM may be of any project).
        "project": (
            {"projectId": task.project_id, "projectName": task.project.project_name}
            if task.project_id
            else None
        ),
    }


def _person(user) -> dict | None:
    return None if user is None else {"userId": str(user.id), "userName": user.username}


def _date(value) -> str | None:
    return value.isoformat() if value else None


def _task_ref(task) -> dict | None:
    if task is None:
        return None
    return {"taskId": task.task_id, "displayId": task.display_id, "title": task.title}


def _milestone_ref(milestone) -> dict | None:
    if milestone is None or milestone.task is None:
        return None
    return {
        "milestoneId": milestone.milestone_id,
        "taskId": milestone.task_id,
        "displayId": milestone.task.display_id,
        "title": milestone.title,
    }
