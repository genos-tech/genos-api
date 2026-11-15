"""A task of a soft-deleted project reads like a missing task (MIP-1590 item 5, flag `AGENT_PARITY_FOLLOWUPS`).

`ProjectMaster.is_deleted` marks a project that is gone (the app's own delete removes the row; the flag survives
on older rows). Its tasks are not reachable any more: `list_tasks` and the summaries filter them out, and with the
follow-ups flag on every single-task read the agent has answers as it does for a task that is not there: the same
class, the same words, one lookup and no log line that tells them apart. This module is the one place that says
which rows those are, for the tools that already hold the task (`in_deleted_project`) and for search results,
which carry only a `project_id` (`drop_deleted_projects`).
"""

from __future__ import annotations

from typing import Any

from origin.models.project.prj_models import ProjectMaster
from origin.search_engine.agent.parity import followups_on


def in_deleted_project(task: Any) -> bool:
    """Whether `task` (loaded with `select_related("project")`) belongs to a soft-deleted project.
    False while the flag is off, so a caller needs no gate of its own."""
    return bool(
        followups_on()
        and task.project_id is not None
        and (task.project is None or task.project.is_deleted)
    )


def drop_deleted_projects(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`matches` (search hits, each with a `project_id` or None) without the ones that belong to a
    soft-deleted project: one query for all of them, none when no hit names a project. The hit is
    simply absent, as one that was never indexed would be. Unchanged while the flag is off."""
    if not followups_on():
        return matches
    ids = {m["project_id"] for m in matches if isinstance(m.get("project_id"), int)}
    if not ids:
        return matches
    gone = set(
        ProjectMaster.objects.filter(project_id__in=ids, is_deleted=True).values_list(
            "project_id", flat=True
        )
    )
    if not gone:
        return matches
    return [m for m in matches if m.get("project_id") not in gone]
