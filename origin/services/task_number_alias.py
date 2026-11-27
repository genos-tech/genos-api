"""Old display ids keep opening a task that moved project (MIP-2497).

A move re-claims the task's number in the destination, so "MIP-123" becomes "WEB-7".
`TaskNumberAlias` remembers (project, number) → task. Every place that turns a display id into a
task asks here, so the old id finds it too.

An alias widens WHAT a display id matches, never WHO may read it: callers keep their own team,
project and ACL checks, which run against the task as it is now. An alias also counts only while its
project and the task are in the same team, so no team's code ever names another team's task. A live
id wins over an alias.
"""

from __future__ import annotations

from django.db.models import Case, F, IntegerField, Q, Value, When

from origin.models.task.task_models import TaskNumberAlias


def remember_numbers(rows) -> None:
    """Record `(project_id, number, task_id)` rows. Call BEFORE a move frees the numbers, inside
    the same transaction. Rows with no project or no number have no display id to keep."""
    aliases = [
        TaskNumberAlias(project_id=project_id, number=number, task_id=task_id)
        for project_id, number, task_id in rows
        if project_id is not None and number is not None
    ]
    if aliases:
        TaskNumberAlias.objects.bulk_create(aliases, ignore_conflicts=True)


def remember_number(task) -> None:
    """`remember_numbers` for one task still holding its old project and number. A draft the
    member never saved (`is_init_task`) has shown its id to nobody."""
    if not task.is_init_task:
        remember_numbers([(task.project_id, task.project_task_number, task.task_id)])


def _alias_task_ids(**match):
    return TaskNumberAlias.objects.filter(
        task_id__isnull=False,
        project__code__isnull=False,
        project__team_id=F("task__team_id"),
        **match,
    ).values("task_id")


def with_display_id(tasks, code: str, number: int):
    """`tasks` narrowed to the ones "<code>-<number>" names, now or before a move, the live one
    first. Add the caller's own filters to `tasks`; read the result with `.first()`."""
    live = Q(project__code__iexact=code, project_task_number=number)
    former = Q(task_id__in=_alias_task_ids(project__code__iexact=code, number=number))
    return tasks.filter(live | former).order_by(
        Case(When(live, then=Value(0)), default=Value(1), output_field=IntegerField()), "task_id"
    )


def display_id_q(code: str, number: int) -> Q:
    """The same match as a filter, for a query that ranks or orders by its own rules."""
    return Q(project__code__iexact=code, project_task_number=number) | Q(
        task_id__in=_alias_task_ids(project__code__iexact=code, number=number)
    )


def former_task_ids(code: str, number: int, team_id) -> list[int]:
    """The team's tasks that were "<code>-<number>" before a move: for a caller that applies its
    own access filter afterwards."""
    aliases = _alias_task_ids(project__code__iexact=code, number=number)
    return [row["task_id"] for row in aliases.filter(project__team_id=team_id)]


def former_numbers_q(numbers, team_ids) -> Q:
    """Tasks that once held one of `numbers` in a project of `team_ids`: the candidates a branch or
    PR text could be naming by an old id."""
    return Q(task_id__in=_alias_task_ids(number__in=numbers, project__team_id__in=team_ids))


def display_ids(task) -> list[str]:
    """Every display id that names `task`: the current one first, then the ones it had before.
    Empty when it has none. Over many tasks, `prefetch_related("number_aliases__project")`."""
    current = task.display_id
    ids = [] if not current or current.startswith("#") else [current]
    for alias in task.number_aliases.all():
        project = alias.project
        if project.code and project.team_id == task.team_id:
            ids.append(f"{project.code}-{alias.number}")
    return ids
