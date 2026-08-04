"""Cross-team object grants, and the delegated roster they authorize.

An `ExternalGrant` says "team B may participate in object X, owned by
team A". This module owns its lifecycle and — more importantly — owns the
single path by which a grant turns into actual access.

## Approve the relationship and the object, never the person

There are exactly two approvals in the life of a share, and both happen
once: the teams connect (`services/team_connection`), then the host
grants an object and the guest team accepts it. After that the guest
team's own owner/editors add and remove THEIR OWN people on that object
whenever they like, with no request back to the host. A share between two
organizations that needed a signature per head would be unusable, and
the host has already consented to the thing that matters — which team,
which object, up to which role.

Three bounds are what make that one-time consent sound:

1. **Only members of the guest team may be admitted.** Checked on every
   add against live membership, so the guest team cannot use its grant to
   walk a third party in. Without this, one grant would be transitively
   re-sharable to the whole world.
2. **`role_ceiling` clamps.** The guest team chooses within what the host
   allowed and cannot exceed it.
3. **Either side can revoke, and revoking deletes rows** (see below).

## The grant is not the access

Access is carried entirely by row types that predate this feature:
`ChannelMember`, `ProjectMembers`, `NoteFolderPermission`. Every existing
check therefore keeps working untouched — `can_access_task`,
`get_effective_role`, `serve_media`, the OpenSearch `acl_user_ids`
filter, the collab access check — and none of them has to learn that
cross-team sharing exists. That is the whole reason this design is small.

It has one sharp consequence, and it is the thing most likely to become a
security bug if someone forgets it: **nothing re-checks a grant at read
time.** Flipping `status` to `revoked` while the derived rows survive
revokes nothing whatsoever. Every path that ends consent must delete
rows, which is why `revoke_grant` exists and why the two cascades below
are not optional.

## Two cascades

- **Consent withdrawn** — `revoke_grant`, and `revoke_connection` above
  it, drop every participation row they authorized.
- **The roster changed** — `drop_team_member_participation` runs when
  someone leaves the guest team. Delegation makes the guest team's roster
  the source of truth for who reaches the host's data, so a departure
  that did not propagate would leave an ex-employee of team B holding
  access to team A's project. This hole does not exist under per-person
  approval; it is the price of delegation, and this is where it is paid.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction

from origin.models.common.team_models import ExternalGrant, ShareStatus, TeamMaster
from origin.services.member_roles import (
    EDITOR,
    VIEWER,
    can_manage,
    member_role_to_channel_role,
    resolve_team_role,
)
from origin.views.utils.scope_guards import is_team_member

# Note-permission role ids (`views/utils/note_role.py`): 1 owner,
# 2 editor, 3 viewer. Externals are never owners of a host's folder.
_NOTE_ROLE_IDS = {EDITOR: 2, VIEWER: 3}


class ExternalGrantError(Exception):
    """Raised when a grant operation is refused.

    `code` is a stable string the API surfaces to the client:
    `not_connected` | `not_found` | `not_a_manager` | `not_pending` |
    `not_active` | `not_owned` | `bad_object` | `bad_role` |
    `not_guest_member`.
    """

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


# ── object resolution ──────────────────────────────────────────────────
#
# A host may only lend out something it actually owns. Taking
# `owner_team` from the request and trusting it would let any manager
# grant another tenant's project to their own team — the request-supplied
# `team_id` problem the ACL audit was about, in a new place. So the
# object is resolved to its real owning team and the claim is checked
# against it.


def _resolve_object_team(object_type, object_id):
    """The team that owns this object, or None if it does not exist."""
    try:
        if object_type == ExternalGrant.ObjectType.CHANNEL:
            from origin.models.chat.unified_models import Channel

            row = Channel.objects.filter(id=object_id, is_deleted=False).values("team_id").first()
        elif object_type == ExternalGrant.ObjectType.PROJECT:
            from origin.models.project.prj_models import ProjectMaster

            row = (
                ProjectMaster.objects.filter(project_id=object_id, is_deleted=False)
                .values("team_id")
                .first()
            )
        elif object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
            from origin.models.note.personal_note_models import PersonalNoteFolder

            # Team folders only. A personal folder has no ACL carrier and
            # is owner-only by construction; sharing one externally would
            # mean inventing folder permissions for a space that has
            # deliberately never had them.
            row = (
                PersonalNoteFolder.objects.filter(
                    folder_id=object_id, scope=PersonalNoteFolder.SCOPE_TEAM
                )
                .values("team_id")
                .first()
            )
        else:
            return None
    except (DjangoValidationError, ValueError, TypeError):
        # Malformed id — ordinary request input, names nothing.
        return None
    return str(row["team_id"]) if row and row["team_id"] else None


def _manager_or_raise(team_id, actor) -> None:
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None:
        raise ExternalGrantError("not_found")
    if not can_manage(resolve_team_role(team, getattr(actor, "id", None))):
        raise ExternalGrantError("not_a_manager")


def _is_manager(team_id, actor) -> bool:
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None:
        return False
    return can_manage(resolve_team_role(team, getattr(actor, "id", None)))


# ── grant lifecycle ────────────────────────────────────────────────────


def offer_grant(
    *, owner_team_id, guest_team_id, object_type, object_id, role_ceiling, actor
) -> ExternalGrant:
    """Offer `object_id` to `guest_team`. Pending until they accept.

    Re-offering after a decline or revoke reuses the row — the unique
    constraint is on (guest team, object), so the grant is a standing
    fact about that pair rather than a log of attempts.
    """
    from origin.services.team_connection import are_connected, get_connection

    if role_ceiling not in (VIEWER, EDITOR):
        raise ExternalGrantError("bad_role")
    if object_type not in ExternalGrant.ObjectType.values:
        raise ExternalGrantError("bad_object")

    _manager_or_raise(owner_team_id, actor)

    if not are_connected(owner_team_id, guest_team_id):
        raise ExternalGrantError("not_connected")

    actual_owner = _resolve_object_team(object_type, object_id)
    if actual_owner is None:
        raise ExternalGrantError("bad_object")
    if actual_owner != str(owner_team_id):
        raise ExternalGrantError("not_owned")

    connection = get_connection(owner_team_id, guest_team_id)
    object_id = str(object_id)

    existing = ExternalGrant.objects.filter(
        guest_team_id=guest_team_id, object_type=object_type, object_id=object_id
    ).first()
    if existing is not None:
        if existing.status == ShareStatus.ACTIVE:
            return existing
        existing.connection = connection
        existing.owner_team_id = owner_team_id
        existing.role_ceiling = role_ceiling
        existing.status = ShareStatus.PENDING
        existing.invited_by = actor
        existing.approved_by = None
        existing.save(
            update_fields=[
                "connection",
                "owner_team",
                "role_ceiling",
                "status",
                "invited_by",
                "approved_by",
                "ts_updated_at",
            ]
        )
        return existing

    return ExternalGrant.objects.create(
        connection=connection,
        owner_team_id=owner_team_id,
        guest_team_id=guest_team_id,
        object_type=object_type,
        object_id=object_id,
        role_ceiling=role_ceiling,
        status=ShareStatus.PENDING,
        invited_by=actor,
    )


def respond_to_grant(grant: ExternalGrant, actor, accept: bool) -> ExternalGrant:
    """Accept or decline an offered grant, as a manager of the GUEST team.

    The guest side answers because it is the side taking on the access.
    Accepting admits nobody by itself — the roster is a separate,
    repeatable act (`add_external_participants`).
    """
    if grant is None:
        raise ExternalGrantError("not_found")
    if grant.status != ShareStatus.PENDING:
        raise ExternalGrantError("not_pending")

    _manager_or_raise(grant.guest_team_id, actor)

    grant.status = ShareStatus.ACTIVE if accept else ShareStatus.DECLINED
    grant.approved_by = actor
    grant.save(update_fields=["status", "approved_by", "ts_updated_at"])
    return grant


def set_role_ceiling(grant: ExternalGrant, role_ceiling: str, actor) -> ExternalGrant:
    """Change what the guest team may hand out. Host managers only.

    Lowering does NOT retroactively demote people already admitted;
    doing so silently would be a surprise, and the host's blunt
    instrument is revoke. The new ceiling binds every subsequent add.
    """
    if grant is None:
        raise ExternalGrantError("not_found")
    if role_ceiling not in (VIEWER, EDITOR):
        raise ExternalGrantError("bad_role")
    _manager_or_raise(grant.owner_team_id, actor)
    grant.role_ceiling = role_ceiling
    grant.save(update_fields=["role_ceiling", "ts_updated_at"])
    return grant


def revoke_grant(grant: ExternalGrant, actor=None) -> int:
    """Withdraw a grant and delete every participation row it authorized.

    Returns the number of rows withdrawn.

    `actor=None` skips the permission check and is for internal callers
    that have already established authority — `revoke_connection`
    cascading, principally. An external caller must always pass one.

    Either team's managers may revoke: the host is withdrawing what it
    lent, and the guest is declining to keep holding it. Neither needs
    the other's agreement to stop.
    """
    if grant is None:
        raise ExternalGrantError("not_found")
    if actor is not None and not (
        _is_manager(grant.owner_team_id, actor) or _is_manager(grant.guest_team_id, actor)
    ):
        raise ExternalGrantError("not_a_manager")

    with transaction.atomic():
        withdrawn = 0
        for user_id in list(participant_ids(grant)):
            withdrawn += _remove_participant(grant, user_id)
        grant.status = ShareStatus.REVOKED
        grant.save(update_fields=["status", "ts_updated_at"])
    return withdrawn


# ── lookups used by the surfaces ───────────────────────────────────────


def active_grant(object_type, object_id, guest_team_id) -> ExternalGrant | None:
    """The active grant letting `guest_team` into this object, if any."""
    if guest_team_id is None or object_id is None:
        return None
    return ExternalGrant.objects.filter(
        object_type=object_type,
        object_id=str(object_id),
        guest_team_id=guest_team_id,
        status=ShareStatus.ACTIVE,
    ).first()


def active_grants_for_object(object_type, object_id):
    """Every active grant on this object, one per guest team.

    A single object can be shared with several teams at once — that is
    how a chat spans more than two organizations — and each guest team
    administers only its own side.
    """
    if object_id is None:
        return ExternalGrant.objects.none()
    return ExternalGrant.objects.filter(
        object_type=object_type,
        object_id=str(object_id),
        status=ShareStatus.ACTIVE,
    )


def grant_admitting(object_type, object_id, user_id) -> ExternalGrant | None:
    """The active grant under which `user_id` may reach this object.

    Answers "is this person admissible from outside", which is the
    question the surfaces' membership gates need to widen by. Returns
    None for a host-team member: they reach the object by their own
    membership, and conflating the two would hide a missing internal
    check behind an external one.
    """
    if user_id is None or object_id is None:
        return None
    for grant in active_grants_for_object(object_type, object_id):
        if is_team_member(grant.guest_team_id, user_id):
            return grant
    return None


def host_team_ids_for_user(user_id) -> set[str]:
    """Host teams this person actually participates in from outside.

    `GetMyTeamsView` needs this to synthesize the team shell an external
    member switches into. It already covers project guests by looking for
    `ProjectMembers` rows; a channel or note-folder participant has no
    such row, so without this they hold access to an object in a team the
    client cannot even name — the chat exists and is unreachable.

    Keyed on participation, not on the grant: being a member of a team
    that HOLDS a grant is not participation, and returning those teams
    would put a team in every member's switcher the moment one colleague
    was admitted somewhere.
    """
    if user_id is None:
        return set()
    from origin.models.common.team_models import TeamMembers

    my_teams = TeamMembers.objects.filter(attendee_id=user_id, is_deleted=False).values_list(
        "team_id", flat=True
    )
    teams = set()
    for grant in ExternalGrant.objects.filter(
        status=ShareStatus.ACTIVE, guest_team_id__in=my_teams
    ):
        if str(grant.owner_team_id) in teams:
            continue
        if str(user_id) in {str(uid) for uid in _rows_for_grant(grant)}:
            teams.add(str(grant.owner_team_id))
    return teams


def participant_ids(grant: ExternalGrant) -> list[str]:
    """The guest-team people currently admitted under this grant.

    Derived from the participation rows rather than stored, so it cannot
    disagree with the access those rows actually confer. Narrowed to
    members of the guest team so a host-team member who happens to be on
    the object is never mistaken for someone the guest team admitted.
    """
    if grant is None:
        return []
    candidates = _rows_for_grant(grant)
    return [str(uid) for uid in candidates if is_team_member(grant.guest_team_id, uid)]


def _rows_for_grant(grant: ExternalGrant) -> list:
    """Every user id holding a participation row on the grant's object."""
    if grant.object_type == ExternalGrant.ObjectType.CHANNEL:
        from origin.models.chat.unified_models import ChannelMember

        return list(
            ChannelMember.objects.filter(channel_id=grant.object_id, is_deleted=False).values_list(
                "user_id", flat=True
            )
        )
    if grant.object_type == ExternalGrant.ObjectType.PROJECT:
        from origin.models.project.prj_models import ProjectMembers

        return list(
            ProjectMembers.objects.filter(project_id=grant.object_id).values_list(
                "attendee_id", flat=True
            )
        )
    if grant.object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
        from origin.models.note.common_note_models import NoteFolderPermission

        return list(
            NoteFolderPermission.objects.filter(folder_id=grant.object_id).values_list(
                "user_id", flat=True
            )
        )
    return []


# ── the delegated roster ───────────────────────────────────────────────


def add_external_participants(grant: ExternalGrant, user_ids, actor, role: str | None = None):
    """Admit guest-team people to the granted object.

    The repeatable half of the design: callable any time after the grant
    went active, by the guest team's own managers, with no host
    involvement. Returns the list of user ids actually admitted.

    Refuses, in this order: an inactive grant, an actor who does not
    manage the guest team, a role above the ceiling, and any user who is
    not a live member of the guest team. That last check is what stops a
    grant being re-shared onwards to a third party.
    """
    if grant is None:
        raise ExternalGrantError("not_found")
    if grant.status != ShareStatus.ACTIVE:
        raise ExternalGrantError("not_active")

    _manager_or_raise(grant.guest_team_id, actor)

    role = role or grant.role_ceiling
    if role not in (VIEWER, EDITOR):
        raise ExternalGrantError("bad_role")
    # Clamp rather than reject: the ceiling is the host's decision and
    # the guest team asking for more is a mistake, not an attack.
    if grant.role_ceiling == VIEWER:
        role = VIEWER

    admitted = []
    with transaction.atomic():
        for user_id in user_ids or []:
            if not is_team_member(grant.guest_team_id, user_id):
                raise ExternalGrantError("not_guest_member")
            _write_participant(grant, user_id, role)
            admitted.append(str(user_id))
    return admitted


def remove_external_participants(grant: ExternalGrant, user_ids, actor) -> int:
    """Withdraw people from the granted object. Returns rows removed.

    Allowed to managers of EITHER team, deliberately asymmetric with
    `add_external_participants`: the guest team administers its roster,
    and the host keeps a veto over an individual so that ejecting one
    person does not mean revoking the whole share.
    """
    if grant is None:
        raise ExternalGrantError("not_found")
    if not (_is_manager(grant.guest_team_id, actor) or _is_manager(grant.owner_team_id, actor)):
        raise ExternalGrantError("not_a_manager")

    removed = 0
    with transaction.atomic():
        for user_id in user_ids or []:
            removed += _remove_participant(grant, user_id)
    return removed


def drop_team_member_participation(team_id, user_id) -> int:
    """Strip a departing member's access to every host team's objects.

    Called when a `TeamMembers` row is soft-deleted. Delegation makes the
    guest team's roster the authority on who reaches a host's data, so
    without this someone who leaves team B keeps whatever team A shared
    with team B — indefinitely, since no read path re-checks the grant.

    Only participation derived from a grant is touched. Access the person
    holds in their own team, or as an individually invited guest, is not
    this function's business.
    """
    if team_id is None or user_id is None:
        return 0
    removed = 0
    grants = ExternalGrant.objects.filter(guest_team_id=team_id, status=ShareStatus.ACTIVE)
    with transaction.atomic():
        for grant in grants:
            removed += _remove_participant(grant, user_id)
    return removed


# ── participation-row writers ──────────────────────────────────────────
#
# One per object type, and the only place cross-team access is written.
# Each writes the row type the surface's own permission code already
# reads, which is why none of that code needed to change.


def _write_participant(grant: ExternalGrant, user_id, role: str) -> None:
    if grant.object_type == ExternalGrant.ObjectType.CHANNEL:
        from origin.models.chat.unified_models import ChannelMember

        # `role` maps onto the channel table's own older vocabulary
        # (`admin`/`member`) — see `member_roles` for why that mapping
        # exists rather than a migration. Un-delete-or-create, because
        # leaving a channel is a soft delete and the unique constraint
        # would refuse a second row.
        ChannelMember.objects.update_or_create(
            channel_id=grant.object_id,
            user_id=user_id,
            defaults={"role": member_role_to_channel_role(role), "is_deleted": False},
        )
        return

    if grant.object_type == ExternalGrant.ObjectType.PROJECT:
        from origin.services.team_membership import add_project_guest

        # Always the GUEST role, whatever the ceiling says. On a project
        # the external role is a real value in the vocabulary
        # (`ASSIGNABLE_PROJECT_ROLES`) and `editor` there confers member
        # management — an outsider who could add people to the host's
        # project would defeat the grant. Read/write on the tasks
        # themselves does not come from this column anyway: it comes from
        # `can_access_task`, which asks only about membership.
        add_project_guest(grant.owner_team_id, grant.object_id, user_id)
        return

    if grant.object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
        from origin.models.note.common_note_models import NoteFolderPermission

        NoteFolderPermission.objects.update_or_create(
            folder_id=grant.object_id,
            user_id=user_id,
            defaults={
                "team_id": grant.owner_team_id,
                "role_id": _NOTE_ROLE_IDS[role],
                # Provenance: the grant, not a mention group. Lets the UI
                # say "via <team>" and lets the cascades find these rows
                # without guessing.
                "via_group_type": "external_grant",
                "via_group_id": str(grant.id),
                "granted_by": grant.invited_by,
            },
        )
        return


def _remove_participant(grant: ExternalGrant, user_id) -> int:
    """Delete one person's participation row. Returns 1 if there was one.

    Deleting, never flag-flipping, except where the table's own
    convention is a soft delete (channels). A row left behind keeps
    granting access — see the module docstring.
    """
    if grant.object_type == ExternalGrant.ObjectType.CHANNEL:
        from origin.models.chat.unified_models import ChannelMember

        return ChannelMember.objects.filter(
            channel_id=grant.object_id, user_id=user_id, is_deleted=False
        ).update(is_deleted=True)

    if grant.object_type == ExternalGrant.ObjectType.PROJECT:
        from origin.models.project.prj_models import ProjectMembers

        deleted, _ = ProjectMembers.objects.filter(
            project_id=grant.object_id, attendee_id=user_id
        ).delete()
        return 1 if deleted else 0

    if grant.object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
        from origin.models.note.common_note_models import NoteFolderPermission

        deleted, _ = NoteFolderPermission.objects.filter(
            folder_id=grant.object_id, user_id=user_id
        ).delete()
        return 1 if deleted else 0

    return 0
