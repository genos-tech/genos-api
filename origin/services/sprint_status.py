"""What state a sprint is in today (MIP-922).

`Sprint.status` is a snapshot: it is written when the row is created and when a
person patches it by hand, and nothing rolls it over as the dates pass. A sprint
created as "upcoming" stays "upcoming" for ever. So the stored column is not the
answer to "which sprint are we in?"; the dates are.

`derive_status` is that answer, the one the sprint board has always shown:

  * `archived` is sticky. It is the one value a person sets that the dates
    cannot contradict, so it is never promoted back.
  * otherwise, `start_date <= today <= end_date` (both ends inclusive) is
    `active`, a start after today is `upcoming`, and the rest is `completed`.

Everything that tells a person or the model what state a sprint is in goes
through this module: the sprint endpoints, and the `list_sprints` and
`get_sprint_summary` agent/MCP tools. `status_q` and `derived_status_rank` are
the same rule written for the database, so a list can filter and order on the
derived status BEFORE it applies a limit (sorting in Python after a slice drops
the current sprint on a project with a long history). They are tested against
`derive_status` row for row; change the rule in both or the tests fail.

Nothing here writes the stored column.
"""

from __future__ import annotations

from datetime import date

from django.db.models import Case, IntegerField, Q, Value, When

SPRINT_STATUS_UPCOMING = "upcoming"
SPRINT_STATUS_ACTIVE = "active"
SPRINT_STATUS_COMPLETED = "completed"
SPRINT_STATUS_ARCHIVED = "archived"

# The order a list reads in: what is happening now first.
STATUS_ORDER = (
    SPRINT_STATUS_ACTIVE,
    SPRINT_STATUS_UPCOMING,
    SPRINT_STATUS_COMPLETED,
    SPRINT_STATUS_ARCHIVED,
)


def derive_status(sprint, today: date) -> str:
    """The status of `sprint` on `today`, from its dates.

    Archived sprints are sticky — they're never automatically promoted
    back. Everything else is recomputed from the date window so a stale
    `status` field never lies to the UI.
    """

    if sprint.status == SPRINT_STATUS_ARCHIVED:
        return SPRINT_STATUS_ARCHIVED
    if sprint.start_date <= today <= sprint.end_date:
        return SPRINT_STATUS_ACTIVE
    if today < sprint.start_date:
        return SPRINT_STATUS_UPCOMING
    return SPRINT_STATUS_COMPLETED


def status_q(status: str, today: date) -> Q:
    """The rows `derive_status` would label `status` on `today`.

    The four Qs partition every sprint, whatever its dates (the completed one is
    written as "started, and ended before today" rather than just "ended before
    today" so a sprint whose end precedes its start still lands in exactly one).
    """
    not_archived = ~Q(status=SPRINT_STATUS_ARCHIVED)
    if status == SPRINT_STATUS_ARCHIVED:
        return Q(status=SPRINT_STATUS_ARCHIVED)
    if status == SPRINT_STATUS_ACTIVE:
        return not_archived & Q(start_date__lte=today, end_date__gte=today)
    if status == SPRINT_STATUS_UPCOMING:
        return not_archived & Q(start_date__gt=today)
    if status == SPRINT_STATUS_COMPLETED:
        return not_archived & Q(start_date__lte=today, end_date__lt=today)
    raise ValueError(f"Unknown sprint status {status!r}.")


def derived_status_rank(today: date) -> Case:
    """An annotation: 0 active, 1 upcoming, 2 completed, 3 archived (`STATUS_ORDER`)."""
    return Case(
        *[When(status_q(s, today), then=Value(rank)) for rank, s in enumerate(STATUS_ORDER)],
        default=Value(len(STATUS_ORDER)),
        output_field=IntegerField(),
    )
