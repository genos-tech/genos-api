"""One place that says which tasks sit in a live project (MIP-1621).

`ProjectMaster.is_deleted` marks a project that is gone. The app's own delete removes the row
(`services/project_delete.py`), so the flag is a legacy state, and nothing sets it or clears it
any more. A task of such a project is not reachable: every list leaves it out and every by-id
request answers as it does for a task that was never there (no existence oracle).

The rule, as ORM terms every caller shares:

  * `not_in_gone_project(prefix)`: a `Q` for a task query (`prefix` names the path to the task,
    e.g. `"task__"`). A task with NO project is live: it is `exclude`d only when its project IS
    deleted, so the `NULL` of a project-less task keeps it.
  * `gone_project_ids(team_id)`: the ids of the team's deleted projects, for a search backend
    that cannot join. One cheap query, an empty set for every team that has none.
  * `live_project_ids(ids)`: `ids` without the deleted projects.
  * `project_is_gone(project)`: the same rule for a project row a caller already holds.

The agent and MCP side asks through `agent/tools/project_gone.py`, which adds the flag
(`AGENT_PARITY_FOLLOWUPS`) the model-visible results are gated behind; the app asks here.
"""

from __future__ import annotations

from collections.abc import Iterable

from django.db.models import Q

from origin.models.project.prj_models import ProjectMaster


def not_in_gone_project(prefix: str = "") -> Q:
    """`Q` that keeps a task of a live project and a task of no project: `~Q(project__is_deleted=True)`."""
    return ~Q(**{f"{prefix}project__is_deleted": True})


def project_is_gone(project: ProjectMaster | None) -> bool:
    return project is not None and bool(project.is_deleted)


def gone_project_ids(team_id=None) -> set[int]:
    """The soft-deleted projects (of `team_id` when given). One query; empty where there are none."""
    qs = ProjectMaster.objects.filter(is_deleted=True)
    if team_id is not None:
        qs = qs.filter(team_id=team_id)
    return set(qs.values_list("project_id", flat=True))


def live_project_ids(ids: Iterable[int]) -> list[int]:
    """`ids` without the soft-deleted projects, in the order given. One query, none for no ids."""
    wanted = list(ids)
    if not wanted:
        return wanted
    gone = set(
        ProjectMaster.objects.filter(project_id__in=wanted, is_deleted=True).values_list(
            "project_id", flat=True
        )
    )
    return [i for i in wanted if i not in gone] if gone else wanted
