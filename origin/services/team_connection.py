"""The team-to-team relationship: request, approve, decline, revoke.

A `TeamConnection` is the prerequisite for every cross-team share and
grants nothing on its own (see the model docstring). This module owns its
lifecycle and is the ONLY place that writes the row, because the pair is
normalized and a hand-written insert with the teams the other way round
would create a second, invisible relationship.

## Authorization lives here, not in the view

Every function takes the acting user and enforces the rule itself,
rather than trusting a view to have checked. Cross-team consent is the
kind of gate that gets forgotten when it is spread across endpoints —
the ACL audit found exactly that class of omission — and there is only
one correct answer per operation:

    request   a manager of the REQUESTING team
    approve   a manager of the OTHER team (never the asker)
    decline   a manager of the OTHER team
    revoke    a manager of EITHER side

Revoke is deliberately available to both. A connection is a mutual
arrangement, so either party can end it without the other's agreement,
and neither has to negotiate its way out of a share it regrets.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from origin.models.common.team_models import (
    ExternalGrant,
    ShareStatus,
    TeamConnection,
    TeamMaster,
)
from origin.services.cross_team_notices import notify_connection_answer
from origin.services.member_roles import can_manage, resolve_team_role


class TeamConnectionError(Exception):
    """Raised when a connection operation is refused.

    `code` is a stable string the API surfaces to the client:
    `same_team` | `team_unavailable` | `not_found` | `not_a_manager` |
    `not_pending` | `already_connected`.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def normalize_team_pair(team_a_id, team_b_id) -> tuple[str, str]:
    """Order two team ids so the pair identifies the relationship.

    Compared as strings, which is stable for UUIDs and does not care
    whether the caller passed `UUID` or `str`.
    """
    a, b = str(team_a_id), str(team_b_id)
    return (a, b) if a < b else (b, a)


def _manager_or_raise(team_id, user) -> TeamMaster:
    """The team, iff `user` may administer it. Raises otherwise."""
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None:
        raise TeamConnectionError("team_unavailable")
    if not can_manage(resolve_team_role(team, getattr(user, "id", None))):
        raise TeamConnectionError("not_a_manager")
    return team


def get_connection(team_a_id, team_b_id) -> TeamConnection | None:
    """The connection row for this pair in either direction, or None."""
    if team_a_id is None or team_b_id is None:
        return None
    lo, hi = normalize_team_pair(team_a_id, team_b_id)
    try:
        return TeamConnection.objects.filter(team_lo=lo, team_hi=hi).first()
    except (DjangoValidationError, ValueError, TypeError):
        # A malformed id names no team, so it is connected to nothing.
        # Same reasoning as `scope_guards._no_such`: this arrives from a
        # request, so it is ordinary input rather than an exception.
        return None


def are_connected(team_a_id, team_b_id) -> bool:
    """Is there an ACTIVE connection between these two teams?

    False for a team and itself. Being your own team is not a
    connection, and answering True here would let every "is the other
    side connected" guard treat the host as external to itself.
    """
    if team_a_id is None or team_b_id is None:
        return False
    if str(team_a_id) == str(team_b_id):
        return False
    conn = get_connection(team_a_id, team_b_id)
    return conn is not None and conn.status == ShareStatus.ACTIVE


def connected_team_ids(team_id) -> list[str]:
    """Every team with an ACTIVE connection to this one."""
    if team_id is None:
        return []
    active = TeamConnection.objects.filter(status=ShareStatus.ACTIVE)
    try:
        rows = list(active.filter(team_lo=team_id).values_list("team_hi_id", flat=True)) + list(
            active.filter(team_hi=team_id).values_list("team_lo_id", flat=True)
        )
    except (DjangoValidationError, ValueError, TypeError):
        return []
    return [str(t) for t in rows if t]


def request_connection(requesting_team_id, target_team_id, actor) -> TeamConnection:
    """Ask `target_team` to connect. Returns the pending row.

    Idempotent on an existing pending request from the same side, so a
    double-click does not create a second relationship (the unique
    constraint would refuse it anyway, less politely). A previously
    declined or revoked connection is re-opened by flipping this row
    back to pending — that is why `status` is a field and not a delete.
    """
    if requesting_team_id is None or target_team_id is None:
        raise TeamConnectionError("not_found")
    if str(requesting_team_id) == str(target_team_id):
        raise TeamConnectionError("same_team")

    _manager_or_raise(requesting_team_id, actor)
    target = TeamMaster.objects.filter(team_id=target_team_id, is_deleted=False).first()
    if target is None:
        raise TeamConnectionError("team_unavailable")

    lo, hi = normalize_team_pair(requesting_team_id, target_team_id)
    existing = get_connection(requesting_team_id, target_team_id)
    if existing is not None:
        if existing.status == ShareStatus.ACTIVE:
            raise TeamConnectionError("already_connected")
        existing.status = ShareStatus.PENDING
        existing.requested_by_team_id = requesting_team_id
        existing.requested_by = actor
        # Cleared so a re-request cannot display the previous approver
        # as though they had agreed to this one.
        existing.approved_by = None
        existing.save(
            update_fields=[
                "status",
                "requested_by_team",
                "requested_by",
                "approved_by",
                "ts_updated_at",
            ]
        )
        return existing

    return TeamConnection.objects.create(
        team_lo_id=lo,
        team_hi_id=hi,
        status=ShareStatus.PENDING,
        requested_by_team_id=requesting_team_id,
        requested_by=actor,
    )


def _other_side(connection: TeamConnection, team_id) -> str:
    """The id of the side that is not `team_id`."""
    return (
        str(connection.team_hi_id)
        if str(connection.team_lo_id) == str(team_id)
        else str(connection.team_lo_id)
    )


def respond_to_connection(connection: TeamConnection, actor, accept: bool) -> TeamConnection:
    """Approve or decline a pending request, as the side that was asked.

    The approver must manage the team that did NOT request, which is the
    whole point of the gate: a manager of the asking team approving its
    own request would make the connection unilateral.
    """
    if connection is None:
        raise TeamConnectionError("not_found")
    if connection.status != ShareStatus.PENDING:
        raise TeamConnectionError("not_pending")

    approving_team_id = _other_side(connection, connection.requested_by_team_id)
    _manager_or_raise(approving_team_id, actor)

    connection.status = ShareStatus.ACTIVE if accept else ShareStatus.DECLINED
    connection.approved_by = actor
    connection.save(update_fields=["status", "approved_by", "ts_updated_at"])
    # Here rather than in the view, for the same reason the request notice
    # is: this is the only place a connection is answered, so the asking
    # team cannot be left waiting on a decision that was already made.
    notify_connection_answer(connection, actor, accept)
    return connection


def revoke_connection(connection: TeamConnection, actor) -> int:
    """End the relationship and every share inside it.

    Returns the number of participation rows withdrawn. Either side may
    revoke (see the module docstring).

    The cascade is the security-critical half. No read path consults
    `TeamConnection` or `ExternalGrant`, so flipping statuses without
    deleting the derived `ChannelMember` / `ProjectMembers` /
    `NoteFolderPermission` rows would revoke precisely nothing.
    """
    from origin.services.external_grants import revoke_grant

    if connection is None:
        raise TeamConnectionError("not_found")

    actor_id = getattr(actor, "id", None)
    lo_ok = can_manage(
        resolve_team_role(
            TeamMaster.objects.filter(team_id=connection.team_lo_id).first(), actor_id
        )
    )
    hi_ok = can_manage(
        resolve_team_role(
            TeamMaster.objects.filter(team_id=connection.team_hi_id).first(), actor_id
        )
    )
    if not (lo_ok or hi_ok):
        raise TeamConnectionError("not_a_manager")

    withdrawn = 0
    with transaction.atomic():
        for grant in ExternalGrant.objects.filter(connection=connection).exclude(
            status=ShareStatus.REVOKED
        ):
            withdrawn += revoke_grant(grant)
        connection.status = ShareStatus.REVOKED
        connection.save(update_fields=["status", "ts_updated_at"])
    return withdrawn
