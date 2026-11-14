"""The closed-ancestor rule in the in-app agent's rollups and lists (MIP-1413 for MIP-1353/1389), behind
`SEARCH_ENGINE["AGENT_CLOSED_ANCESTOR_RULE"]` (default off).

THE RULE (`services/task_ancestry.py`): a task under a Closed or Deleted ancestor (its parent chain, and
the backing row of its milestone) reads as Closed or Deleted, whatever its own status says; its own status
is never changed. The web client, the progress bar, the briefing (`briefing._my_active_tasks`) and the MCP
task reads already apply it; the in-app tools did not, so the same person asking the agent "what is open?"
got a longer list than the dashboard and the briefing showed.

THE DESIGN, one rule for two kinds of tool:

  * COUNTS and BREAKDOWNS (`get_my_task_summary`, `get_milestone_summary`, `get_sprint_summary`,
    `list_milestones`, `list_my_milestones`): the tasks are read by their EFFECTIVE status. A task under a
    Closed ancestor is counted as Closed (and so is not overdue, not "open", not "ready"), one under a
    Deleted ancestor is not counted at all (it is gone, as a task whose own status is Deleted is). The
    same reading as `milestone_work_counts(ancestor_rule=True)`, the progress bar's; a test pins the two
    equal. Done as an annotation on the queryset (`eff_status`), so the aggregates keep their shape.
  * LISTS (`get_my_focus_tasks`, "what should I work on"): tasks that read as Closed or Deleted are left
    OUT, the briefing's rule. (MIP-1478 adds `get_project_summary` and `get_team_task_summary` to the
    counts, and `get_workload_distribution`, `get_stale_tasks` and `get_my_schedule` to the lists.) A generic list the model filters itself (`list_tasks`) and the single task
    (`fetch_task`) keep every row and CARRY `effective_status` (and `closed_by_ancestor`), exactly what the
    MCP `list_tasks` / `get_task` show: dropping a row the model asked for by an explicit status would
    read as "the task does not exist".

COST: ONE extra query per call however big the tree: the narrow columns (five) of the projects involved,
walked in Python by `task_ancestry.ancestor_verdicts`; the two id sets then ride in the existing queries.

Flag off: nothing here runs; every tool takes its old code path (`rule is None`).
"""

from __future__ import annotations

from typing import Any, NamedTuple

from django.conf import settings
from django.db.models import Case, CharField, F, QuerySet, Value, When

from origin.models.task.task_models import TaskMaster
from origin.services import task_ancestry

FLAG = "AGENT_CLOSED_ANCESTOR_RULE"

#: Sentence added to the declaration of every tool whose result the flag changes, by kind.
COUNTS_ADDITION = (
    " A task under a Closed or Deleted parent, grandparent or milestone reads as Closed (or "
    "Deleted) here even when its own status is still open: it is counted as closed, never as open or "
    "overdue, as the app's progress bars do."
)
FOCUS_ADDITION = (
    " A task under a Closed or Deleted parent, grandparent or milestone counts as Closed even when "
    "its own status is still open, so it is not listed."
)
ROWS_ADDITION = (
    " `effective_status` is the status a task reads as: a task under a Closed or Deleted parent, "
    "grandparent or milestone counts as Closed or Deleted even when its own `status` is still open, "
    "and then `closed_by_ancestor` names that ancestor. Do not count such a task as open work."
)


def enabled() -> bool:
    """Read at call time, so an eval arm or `override_settings` can flip it."""
    return bool(settings.SEARCH_ENGINE.get(FLAG))


class Rule(NamedTuple):
    """The tasks of some projects whose effective status differs from their own."""

    closed: frozenset[int]  # read as Closed, own status is not
    deleted: frozenset[int]  # read as Deleted, own status is not

    @property
    def not_open(self) -> frozenset[int]:
        return self.closed | self.deleted


def rule_for_projects(project_ids, team_id) -> Rule:
    """The rule over whole projects: ONE query (five narrow columns of every live task of the
    projects, ancestors included), walked in Python."""
    ids = {p for p in project_ids if p is not None}
    if not ids:
        return Rule(frozenset(), frozenset())
    rows = list(
        TaskMaster.objects.filter(
            project_id__in=ids, team_id=team_id, is_deleted=False, is_init_task=False
        ).values_list(*task_ancestry._COLUMNS)
    )
    verdicts = task_ancestry.ancestor_verdicts(rows)
    own = {r[0]: r[4] for r in rows}
    closed: set[int] = set()
    deleted: set[int] = set()
    for task_id, found in verdicts.items():
        reads = task_ancestry.effective(own[task_id], found).status
        if reads == task_ancestry.CLOSED and own[task_id] != task_ancestry.CLOSED:
            closed.add(task_id)
        elif reads == task_ancestry.DELETED and own[task_id] != task_ancestry.DELETED:
            deleted.add(task_id)
    return Rule(frozenset(closed), frozenset(deleted))


def with_effective_status(qs: QuerySet, rule: Rule) -> QuerySet:
    """`qs` with an `eff_status` column: the status the rule reads. Aggregates then filter on
    `eff_status` where they used `status`."""
    whens = []
    if rule.deleted:
        whens.append(When(task_id__in=sorted(rule.deleted), then=Value(task_ancestry.DELETED)))
    if rule.closed:
        whens.append(When(task_id__in=sorted(rule.closed), then=Value(task_ancestry.CLOSED)))
    expression = (
        Case(*whens, default=F("status"), output_field=CharField()) if whens else F("status")
    )
    return qs.annotate(eff_status=expression)


def read_by_rule(qs: QuerySet, rule: Rule | None) -> tuple[QuerySet, str]:
    """`(qs, status field)` for a counting tool: `(qs, "status")` untouched with no rule, else the
    queryset annotated, without the tasks that read as Deleted, and `"eff_status"`."""
    if rule is None:
        return qs, "status"
    return with_effective_status(qs, rule).exclude(eff_status=task_ancestry.DELETED), "eff_status"


def row_fields(task_ids: list[Any], ctx) -> dict[int, dict[str, Any]]:
    """`{task_id: {effective_status, closed_by_ancestor?}}` for rows a list tool is returning: the
    fields the MCP `list_tasks` / `get_task` already carry (one shared implementation, so the ancestor's
    display id is shown only to a caller who may read it)."""
    # Imported here: the MCP layer imports the agent tools.
    from origin.views.public.mcp.ancestry import _fields  # noqa: PLC0415

    return _fields(task_ids, ctx)
