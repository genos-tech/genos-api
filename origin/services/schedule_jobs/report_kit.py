"""Small helpers the built-in jobs share (MIP-1148): plain text out of the briefing's
fenced fields, and links to the things a report names.

The briefing's text fields are fenced (`wrap_workspace_content`) because a MODEL reads
them and must treat them as data. A report is read by the member, so the fence is taken
off here, once, at the edge: `plain`.

Links are in-app paths (`/workspace/...`, the same routes as `entity_links`). Each is built
only for something the briefing already decided the member may open, and the target view
checks access again when it is opened. A link we cannot build is left out: a line without
a link is better than a dead one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.schedule_jobs import ReportItem

# A report line is a sentence, not a document: a snippet or a title is cut here.
LINE_CHARS = 240


def plain(value: Any) -> str:
    """The text of a briefing field, without the fence the briefing put round it."""
    if not isinstance(value, str) or not value:
        return ""
    inner = unwrap(value)
    return (value if inner is None else inner).strip()


def clip(text: str, limit: int = LINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def item(text: str, link: str | None = None) -> ReportItem:
    out: ReportItem = {"text": clip(text)}
    if link:
        out["link"] = link
    return out


def _urgency(facts: TaskFacts) -> tuple:
    """Within a project: by due date, so overdue comes first (the longest overdue first) and then
    what is due soonest, tasks with no due date last, then by display id (the number: every
    task of a group has the same project code)."""
    due = facts.due_date
    return (due or date.max, facts.number or 0, facts.task_id)


def arrange(items: list[ReportItem], today: date) -> list[ReportItem]:
    """Group a section's TASK items by project and sort inside each group (MIP-1207).

    The rule, the same in every job: projects by name (case-insensitive, then id); inside a
    project, overdue first (longest overdue first), then by due date, tasks with no due date
    last, then by display id. Items that are not tasks of a known project (to-dos, events,
    notes, chats, requests) keep their own order and follow the project groups. Every item of
    a project is contiguous, so a card can draw a header where `project` changes. Call it
    BEFORE appending the closing "and N more" line, which stays last."""
    grouped = [i for i in items if "_facts" in i]
    rest = [i for i in items if "_facts" not in i]
    grouped.sort(
        key=lambda i: (
            i["_facts"].project_name.casefold(),  # type: ignore[typeddict-item]
            i["_facts"].project_id or 0,  # type: ignore[typeddict-item]
            *_urgency(i["_facts"]),  # type: ignore[typeddict-item]
        )
    )
    for i in grouped:
        del i["_facts"]  # type: ignore[typeddict-item]
    return [*grouped, *rest]


def more(count: int, noun: str) -> ReportItem:
    """The closing line of a capped list: how many are not shown."""
    return {"text": f"and {count} more {noun}"}


@dataclass(frozen=True)
class TaskFacts:
    """What a report knows about a task beyond the briefing's row: where it sits, and what it
    is sorted by. Read once per report, for tasks the briefing already decided the member may
    open."""

    task_id: int
    number: int | None
    project_id: int | None
    project_name: str
    project_code: str
    due_date: date | None
    status: str
    priority: str


class Links:
    """In-app paths for the tasks and notes of one report, resolved in two queries at most
    (the briefing's items name a task or a note but not its project)."""

    def __init__(self, ctx: ToolContext) -> None:
        self._team = ctx.team_id
        self._facts: dict[int, TaskFacts | None] = {}
        self._task_of_note: dict[int, tuple[int | None, int | None]] = {}

    def preload(self, *, tasks: Iterable[Any] = (), task_notes: Iterable[Any] = ()) -> None:
        wanted = {int(t) for t in tasks if t is not None} - self._facts.keys()
        if wanted:
            found = {
                row["task_id"]: TaskFacts(
                    task_id=row["task_id"],
                    number=row["project_task_number"],
                    project_id=row["project_id"],
                    project_name=row["project__project_name"] or "",
                    project_code=row["project__code"] or "",
                    due_date=row["due_date"],
                    status=row["status"] or "",
                    priority=row["priority"] or "",
                )
                for row in TaskMaster.objects.filter(
                    task_id__in=wanted, team_id=self._team, is_deleted=False
                ).values(
                    "task_id",
                    "project_task_number",
                    "project_id",
                    "project__project_name",
                    "project__code",
                    "due_date",
                    "status",
                    "priority",
                )
            }
            for task_id in wanted:
                self._facts[task_id] = found.get(task_id)
        notes = {int(n) for n in task_notes if n is not None} - self._task_of_note.keys()
        if notes:
            found_notes = {
                note_id: (project_id, task_id)
                for note_id, project_id, task_id in TaskNoteMaster.objects.filter(
                    note_id__in=notes, team_id=self._team
                ).values_list("note_id", "project_id", "task_id")
            }
            for note_id in notes:
                self._task_of_note[note_id] = found_notes.get(note_id, (None, None))

    def facts(self, task_id: Any) -> TaskFacts | None:
        return self._facts.get(int(task_id)) if task_id is not None else None

    def task(self, task_id: Any) -> str | None:
        facts = self.facts(task_id)
        if facts is None or not facts.project_id:
            return None
        return f"/workspace/tasks/project/{facts.project_id}/task/{int(task_id)}"

    def task_item(self, text: str, task_id: Any) -> ReportItem:
        """A report line about a task: its link, its project, and what `arrange` sorts it by
        (the private `_facts`, removed by `arrange`)."""
        out = item(text, self.task(task_id))
        facts = self.facts(task_id)
        if facts is not None and facts.project_id and facts.project_name:
            project: dict[str, Any] = {"id": facts.project_id, "name": facts.project_name}
            if facts.project_code:
                project["code"] = facts.project_code
            out["project"] = project  # type: ignore[typeddict-item]
            out["_facts"] = facts  # type: ignore[typeddict-unknown-key]
        return out

    def note(self, note_type: Any, note_id: Any) -> str | None:
        if note_id is None:
            return None
        if note_type == "personal":
            return f"/workspace/notes/my/{int(note_id)}"
        if note_type == "task":
            project_id, task_id = self._task_of_note.get(int(note_id), (None, None))
            if project_id and task_id:
                return (
                    f"/workspace/notes/task/project/{project_id}/task/{task_id}/note/{int(note_id)}"
                )
        return None  # a chat's note has no stable link

    @staticmethod
    def project(project_id: Any) -> str | None:
        return f"/workspace/tasks/project/{int(project_id)}" if project_id else None

    @staticmethod
    def milestone(project_id: Any, milestone_id: Any) -> str | None:
        if not project_id or not milestone_id:
            return None
        return f"/workspace/tasks/project/{int(project_id)}/milestone/{int(milestone_id)}"

    @staticmethod
    def chat(chat_type: Any, chat_id: Any, thread_id: Any = None) -> str | None:
        if chat_type not in ("dm", "gm", "pm", "mdm") or not chat_id:
            return None
        path = f"/workspace/chat/{chat_type}/{chat_id}"
        return f"{path}/thread/{thread_id}" if thread_id else path

    @staticmethod
    def todo(local_date: Any, item_id: Any) -> str | None:
        if not local_date or item_id is None:
            return None
        return f"/workspace/todo/{local_date}/item/{int(item_id)}"
