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


def more(count: int, noun: str) -> ReportItem:
    """The closing line of a capped list: how many are not shown."""
    return {"text": f"and {count} more {noun}"}


class Links:
    """In-app paths for the tasks and notes of one report, resolved in two queries at most
    (the briefing's items name a task or a note but not its project)."""

    def __init__(self, ctx: ToolContext) -> None:
        self._team = ctx.team_id
        self._project_of: dict[int, int | None] = {}
        self._task_of_note: dict[int, tuple[int | None, int | None]] = {}

    def preload(self, *, tasks: Iterable[Any] = (), task_notes: Iterable[Any] = ()) -> None:
        wanted = {int(t) for t in tasks if t is not None} - self._project_of.keys()
        if wanted:
            found = dict(
                TaskMaster.objects.filter(
                    task_id__in=wanted, team_id=self._team, is_deleted=False
                ).values_list("task_id", "project_id")
            )
            for task_id in wanted:
                self._project_of[task_id] = found.get(task_id)
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

    def task(self, task_id: Any) -> str | None:
        project_id = self._project_of.get(int(task_id)) if task_id is not None else None
        if not project_id:
            return None
        return f"/workspace/tasks/project/{project_id}/task/{int(task_id)}"

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
