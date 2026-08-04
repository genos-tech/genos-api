"""Membership guards — the one place a view asks "may this user see this?".

Until this module there was no permission layer at all: DRF's
`DEFAULT_PERMISSION_CLASSES` is unset (so its fallback is `AllowAny`),
`AuthenticatedAPIView` adds `IsAuthenticated` and nothing else, and every
authorization decision lived inline in a view body. The same membership
query was therefore written by hand a dozen times, three of them subtly
differently, and a number of endpoints simply forgot.

## Why a role resolver is NOT a membership check

`services/member_roles.resolve_team_role` / `resolve_project_role` return
`VIEWER` for a user with no membership row at all — they answer "what can
this member do", not "is this a member". A guard written as

    if resolve_team_role(team, request.user.id) == VIEWER:  # WRONG

therefore admits every stranger. These helpers are the missing half:
establish membership FIRST, then resolve the role for the editor/viewer
decision. Both questions have to be asked, in that order.

## 404, never 403

`require_team_member` and friends raise `Http404` rather than returning
403, copying the convention `views/chat/channel_views.py` established:
answering 403 confirms that the id names a real team/project the caller
happens to be outside of, which is itself a disclosure. A caller who is
not a member is told the thing does not exist. The `*_or_response`
variants exist for the many v2 views that return a `Response` instead of
raising, so they can adopt the guard without restructuring.

## The owner is a member even without a membership row

`TeamMaster.owner` / `ProjectMaster.owner` are the single source of truth
for ownership (see `services/member_roles.py`), and an owner's
`TeamMembers` row is optional — team creation does not always write one.
A bare membership query therefore denies the one person who can never be
denied. The two pre-existing `_verify_team_member` copies (in
`chat/channel_views.py` and `common/team_emoji_views.py`) both have this
bug; `note_folder_role.is_team_member` does not. This module follows the
correct one.

`ProjectMembers` has **no** `is_deleted` column — removal is a hard
delete — so project queries must not filter on one. `TeamMembers` does.
"""

from __future__ import annotations

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db.models import Q
from django.http import Http404
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

# Deliberately vague, and identical for "no such team" and "not yours".
_TEAM_MISSING = "Team not found."
_PROJECT_MISSING = "Project not found."


# ── predicates ─────────────────────────────────────────────────────────
#
# Every predicate here takes ids that came from a request, so a malformed
# one is ordinary input, not an exceptional condition. `team_id` is a
# UUIDField and `project_id` an integer, and handing either a bad value
# raises out of the ORM's `to_python` — `ValidationError` for the UUID,
# `ValueError` for the integer, neither of them caught by callers. That
# made `?team_id=abc` a 500 on every endpoint the ACL series routed
# through here, which is most of them.
#
# `_no_such` swallows exactly those two and answers False, which is also
# the honest answer: a malformed id names nothing, so nobody is a member
# of it. Doing this once here is worth more than doing it at each of the
# hundred-odd call sites, and it cannot drift.


def _no_such(query, default=False):
    """Run a membership `.exists()`, treating an unparseable id as False.

    `default` is for the few callers that answer with a set of visible
    users rather than a boolean — an empty set is their fail-closed
    answer, and returning `False` there would be a truthiness accident.
    """
    try:
        return query()
    except (DjangoValidationError, ValueError, TypeError):
        return default


def is_team_member(team_id, user_id) -> bool:
    """True if `user_id` owns or actively belongs to `team_id`.

    Mirrors `views/utils/note_folder_role.is_team_member`, which this is
    intended to eventually replace; kept behaviourally identical so the
    two can be unified without changing any note-ACL outcome.
    """
    if team_id is None or user_id is None:
        return False
    if _no_such(lambda: TeamMaster.objects.filter(team_id=team_id, owner=user_id).exists()):
        return True
    return _no_such(
        lambda: TeamMembers.objects.filter(
            team=team_id, attendee=user_id, is_deleted=False
        ).exists()
    )


def is_project_member(project_id, user_id) -> bool:
    """True if `user_id` owns or belongs to `project_id`.

    No `is_deleted` filter: `ProjectMembers` has no such column, removal
    is a hard delete (`prj_views.LeaveProjectView`).
    """
    if project_id is None or user_id is None:
        return False
    if _no_such(
        lambda: ProjectMaster.objects.filter(project_id=project_id, owner=user_id).exists()
    ):
        return True
    return _no_such(
        lambda: ProjectMembers.objects.filter(project=project_id, attendee=user_id).exists()
    )


def is_guest(team_id, user_id) -> bool:
    """Is `user_id` an EXTERNAL collaborator in this team?

    True when they hold at least one `ProjectMembers` row in the team but
    no `TeamMembers` row. That absence *is* the guest model — see the
    `services/member_roles` docstring — so this function is a
    description of the data, not a separate flag that could drift out of
    sync with it.

    Note the deliberate asymmetry: `is_team_member` returning False is
    what denies a guest everything team-wide, and it does so without
    knowing guests exist. This predicate is only for the places that
    need to treat a guest *differently* from a stranger — showing them
    the team shell so the client can boot, for instance — never for
    granting access.
    """
    if team_id is None or user_id is None:
        return False
    if is_team_member(team_id, user_id):
        return False
    return _no_such(lambda: ProjectMembers.objects.filter(team=team_id, attendee=user_id).exists())


def is_team_participant(team_id, user_id) -> bool:
    """Does `user_id` belong to `team_id` in ANY capacity?

    Full member, team owner, guest, or a member of a connected team that
    holds an active share on something this team owns. This answers a
    different question from `is_team_member`, and the difference matters
    at exactly one kind of site: **may these two people be put in a room
    together.**

    `is_team_member` asks "may you act on the team" and correctly denies
    guests everything team-wide. But a guest you share a project with is
    a legitimate person to message, so gating a DM counterparty on
    `is_team_member` would deny it. Gating on nothing — which is what the
    channel-create paths did — lets you open a DM with a stranger in
    another tenant entirely.

    Deliberately not narrowed to "shares a project with the caller".
    Which teammates may message which is a product decision; being able
    to reach across tenants is the security defect, and that is what this
    closes. Note also that a caller must already KNOW the counterparty's
    user id: guests appear in no roster and no people picker, so this is
    not an enumeration surface.

    ## NOT an admission check for an object

    The external clause makes two organizations that already share
    *something* mutually reachable, which is the point — you cannot
    collaborate with someone you may not message. It does NOT say the
    person may join any given channel, project, or folder. Object
    membership always requires a grant on THAT object
    (`external_grants.grant_admitting`). Using this predicate to gate an
    add-to-object path would let one share silently become access to
    everything the host owns.
    """
    return (
        is_team_member(team_id, user_id)
        or is_guest(team_id, user_id)
        or is_external_participant(team_id, user_id)
    )


def are_teams_connected(team_a_id, team_b_id) -> bool:
    """Do these two teams have an ACTIVE `TeamConnection`?

    Thin re-export of `services/team_connection.are_connected` so view
    code has one import for membership questions. Imported inside the
    function because the connection service reaches back into this module
    for `is_team_member`, and a module-level import would close the loop.

    Being connected authorizes *naming* the other team in a grant. It is
    never sufficient for access on its own — that always comes from a
    participation row — so a guard written as "connected, therefore
    allowed" is wrong at every call site.
    """
    from origin.services.team_connection import are_connected

    return are_connected(team_a_id, team_b_id)


def is_external_participant(team_id, user_id) -> bool:
    """Is `user_id` admissible to this team's data from ANOTHER team?

    True when they are not a member of `team_id` themselves, but belong
    to a team holding an active `ExternalGrant` on something `team_id`
    owns. The cross-team sibling of `is_guest`, and like `is_guest` it is
    a description of the data rather than a stored flag.

    Two things it deliberately does NOT mean:

    * Not "has access". Admissibility is the guest team's managers being
      *able* to admit them; access is the participation row they may or
      may not have been given. Only the row grants anything.
    * Not a substitute for `is_team_member`. Use it where an outsider must
      be told apart from a stranger — labelling, or letting a client boot
      into a host team's shell — never to grant.

    Note that an external participant on a shared PROJECT also satisfies
    `is_guest`, because the access they get is an ordinary
    `ProjectMembers` row. That overlap is intended: it is what makes every
    existing guest narrowing apply to them for free.
    """
    if team_id is None or user_id is None:
        return False
    if is_team_member(team_id, user_id):
        return False
    from origin.models.common.team_models import ExternalGrant, ShareStatus

    def _query():
        guest_team_ids = ExternalGrant.objects.filter(
            owner_team_id=team_id, status=ShareStatus.ACTIVE
        ).values_list("guest_team_id", flat=True)
        return any(is_team_member(gt, user_id) for gt in set(guest_team_ids))

    return _no_such(_query)


def has_external_participation(team_id, user_id) -> bool:
    """Do they hold an actual participation row on something this team owns?

    The stricter sibling of `is_external_participant`, which asks only
    whether the guest team's managers COULD admit them. Use this one
    wherever the answer decides what the person may read, because a
    colleague of an admitted person must not inherit their reach.
    """
    if team_id is None or user_id is None:
        return False
    from origin.services.external_grants import host_team_ids_for_user

    return _no_such(lambda: str(team_id) in host_team_ids_for_user(user_id))


def participates_in_team(team_id, user_id) -> bool:
    """May this person read anything at all inside `team_id`?

    The gate for whole-team read surfaces — search, a Genos ask, the MCP
    tool context — where the answer is "let them in, then narrow", never
    "let them in, therefore show them everything". Each of those surfaces
    re-filters by per-object ACL underneath; this only decides whether the
    request is nonsense.

    Three ways in, and the third is why this exists as a function rather
    than an inline `or`: a member, a project guest, and someone a
    cross-team grant admitted to a single chat or note folder. That last
    kind holds no `ProjectMembers` row, so a gate written as "member or
    guest" refused them — leaving a person with an active share unable to
    search the very chat they were invited to.
    """
    return (
        is_team_member(team_id, user_id)
        or is_guest(team_id, user_id)
        or has_external_participation(team_id, user_id)
    )


def guest_project_ids(team_id, user_id) -> list:
    """The projects a guest may see in this team — nothing else exists
    for them. Same shape as `member_project_ids`, narrowed to one team."""
    if not is_guest(team_id, user_id):
        return []
    return member_project_ids(user_id, team_id=team_id)


def external_people_for_member(team_id, user_id) -> dict:
    """The other teams' people this person shares work with, from `team_id`.

    Answers both directions, because the client asks the same question
    either way: it caches one roster per team and renders everyone from
    it. Viewing the team that OWNS a share, the answer is the guest team's
    admitted people; viewing the team that RECEIVED one, it is the host's
    people on that object. Splitting this into two helpers meant one side
    of every shared chat rendered with names and the other with blanks.

    Co-participation on a specific object is the unit — the same rule that
    decides access, and the same one `external_visible_user_ids` uses from
    the outside. Never "everyone in a team that holds a grant": a
    colleague of an admitted person gets no access and is nobody here, and
    a colleague of the ADMITTING person sees nothing either, so sharing
    one project does not put the other company in every desk's directory.

    Returns `{user_id: {"teamId": ..., "teamName": ...}}` rather than a
    set, because the caller's next question is always which team they are
    from: a name with no team beside it reads as one of your own staff,
    which is the misunderstanding this whole feature exists to avoid.
    """
    if team_id is None or user_id is None:
        return {}
    from origin.models.chat.unified_models import ChannelMember
    from origin.models.common.team_models import ExternalGrant, ShareStatus
    from origin.models.note.common_note_models import NoteFolderPermission
    from origin.services.external_grants import rows_for_grant

    def _reaches(grant) -> bool:
        """Is this person on the granted object themselves?"""
        if grant.object_type == ExternalGrant.ObjectType.CHANNEL:
            return ChannelMember.objects.filter(
                channel_id=grant.object_id, user_id=user_id, is_deleted=False
            ).exists()
        if grant.object_type == ExternalGrant.ObjectType.PROJECT:
            return is_project_member(grant.object_id, user_id)
        if grant.object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
            return NoteFolderPermission.objects.filter(
                folder_id=grant.object_id, user_id=user_id
            ).exists()
        return False

    def _query():
        people: dict = {}
        names: dict = {}

        def team_name(tid):
            if tid not in names:
                names[tid] = (
                    TeamMaster.objects.filter(team_id=tid)
                    .values_list("team_name", flat=True)
                    .first()
                    or ""
                )
            return names[tid]

        grants = ExternalGrant.objects.filter(
            Q(owner_team_id=team_id) | Q(guest_team_id=team_id),
            status=ShareStatus.ACTIVE,
        )
        for grant in grants:
            if not _reaches(grant):
                continue
            sides = [str(grant.owner_team_id), str(grant.guest_team_id)]
            uids = {str(uid) for uid in rows_for_grant(grant)} - {str(user_id)}
            if not uids:
                continue
            # Which of the two teams each person belongs to, in two
            # queries rather than one per head: a busy channel makes the
            # per-head form the most expensive thing on the roster path.
            homes: dict = {}
            for tid, aid in TeamMembers.objects.filter(
                team_id__in=sides, attendee_id__in=uids, is_deleted=False
            ).values_list("team_id", "attendee_id"):
                homes.setdefault(str(aid), set()).add(str(tid))
            for tid, oid in TeamMaster.objects.filter(
                team_id__in=sides, owner_id__in=uids
            ).values_list("team_id", "owner_id"):
                homes.setdefault(str(oid), set()).add(str(tid))
            for uid, member_of in homes.items():
                # Anyone in the roster being asked about is already in it.
                if str(team_id) in member_of or uid in people:
                    continue
                home = sorted(member_of)[0]
                people[uid] = {"teamId": home, "teamName": team_name(home)}
        return people

    return _no_such(_query, default={})


def reachable_project_ids(user_id, team_id) -> list:
    """Every project this person may open while viewing `team_id`.

    `member_project_ids` answers only for projects filed under the team
    being viewed, which is right until another team shares one: the
    guest's `ProjectMembers` row is written against the HOST team, so a
    team-wide task list scoped that way returns nothing for exactly the
    work they were invited to do. Team-wide lists (the dashboard's
    capacity roll-up, the task-meta tree, assigned-task counts) were all
    empty in a shared project for that reason while the project's own
    list was full.

    Safe to filter tasks on without a `team=` clause beside it: every id
    in here is either filed under `team_id` or is an object this person
    holds a participation row on.
    """
    if user_id is None or team_id is None:
        return []
    from origin.models.common.team_models import ExternalGrant
    from origin.services.external_grants import external_objects_for_member

    mine = set(member_project_ids(user_id, team_id=team_id))
    shared = _no_such(
        lambda: set(
            external_objects_for_member(
                ExternalGrant.ObjectType.PROJECT, team_id, user_id
            ).keys()
        ),
        default=set(),
    )
    # `object_id` is a text column; the project FK is an integer.
    return list(mine | {int(pid) for pid in shared if str(pid).isdigit()})


def guest_visible_user_ids(team_id, user_id) -> set:
    """Everyone a guest may see in this team: the members of the projects
    they were invited to, plus themselves.

    This is the answer to "who exists?" for an external collaborator. It
    is deliberately a *positive* list rather than a filter over the team
    roster, because the roster is the thing being withheld — a guest who
    can enumerate the team is the exact failure §4.1 of the readiness
    plan warns about.

    Returns an empty set for anyone who is not a guest, so a caller that
    forgets to branch fails closed rather than open.
    """
    project_ids = guest_project_ids(team_id, user_id)
    if not project_ids:
        return set()
    visible = {
        str(uid)
        for uid in ProjectMembers.objects.filter(project_id__in=project_ids).values_list(
            "attendee_id", flat=True
        )
        if uid
    }
    # Project owners need not hold a ProjectMembers row.
    visible |= {
        str(uid)
        for uid in ProjectMaster.objects.filter(project_id__in=project_ids).values_list(
            "owner_id", flat=True
        )
        if uid
    }
    visible.add(str(user_id))
    return visible


def external_visible_user_ids(team_id, user_id) -> set:
    """Everyone an external participant may see in a host team.

    The cross-team counterpart of `guest_visible_user_ids`, and needed for
    the same reason: the host's roster is withheld, so visibility has to
    be assembled from the objects the person actually shares. A channel or
    note-folder share leaves no `ProjectMembers` row, so
    `guest_visible_user_ids` returns nothing for those people and the host
    team would render with an empty roster — not even themselves in it,
    which breaks any client lookup keyed on "find me in this team".

    Only co-participants of objects they are already in, whose identities
    that object's own member list discloses anyway. Never the roster.
    """
    if team_id is None or user_id is None:
        return set()
    from origin.models.chat.unified_models import ChannelMember
    from origin.models.common.team_models import ExternalGrant, ShareStatus
    from origin.models.note.common_note_models import NoteFolderPermission

    def _query():
        visible = set()
        grants = ExternalGrant.objects.filter(owner_team_id=team_id, status=ShareStatus.ACTIVE)
        for grant in grants:
            if not is_team_member(grant.guest_team_id, user_id):
                continue
            if grant.object_type == ExternalGrant.ObjectType.CHANNEL:
                rows = ChannelMember.objects.filter(
                    channel_id=grant.object_id, is_deleted=False
                ).values_list("user_id", flat=True)
            elif grant.object_type == ExternalGrant.ObjectType.NOTE_FOLDER:
                rows = NoteFolderPermission.objects.filter(folder_id=grant.object_id).values_list(
                    "user_id", flat=True
                )
            else:
                # Projects already resolve through
                # `guest_visible_user_ids` — an external project member
                # holds a real `ProjectMembers` row.
                continue
            ids = {str(uid) for uid in rows if uid}
            if str(user_id) in ids:
                visible |= ids
        return visible

    return _no_such(_query, default=set())


def can_access_task(task_id, user_id) -> bool:
    """May `user_id` see (and therefore edit) this task?

    Project members, plus the assignee and the reporter. Deliberately the
    same rule as `search_engine/agent/acl.task_acl_user_ids`, which is
    what decides whether the task appears in search: an assignee with no
    `ProjectMembers` row can already find the task, so a narrower rule
    here would let someone open a task from search and then be refused.

    Note this is the READ audience and the WRITE audience at once. Task
    editing has always been open to anyone who can see the task; adding
    a role tier on top is a product decision, not a security fix, and
    isn't made here.
    """
    if task_id is None or user_id is None:
        return False
    from origin.models.task.task_models import TaskMaster

    try:
        row = (
            TaskMaster.objects.filter(task_id=task_id)
            .values("project_id", "assignee_id", "reporter_id")
            .first()
        )
    except (DjangoValidationError, ValueError, TypeError):
        return False
    if row is None:
        return False
    if str(user_id) in {str(row["assignee_id"]), str(row["reporter_id"])}:
        return True
    return is_project_member(row["project_id"], user_id)


def team_ids_for_user(user_id) -> list:
    """Every team `user_id` owns or actively belongs to.

    The team-level counterpart of `member_project_ids`. Used where a
    request arrives with no team context at all and one has to be
    derived from a user — the GitHub webhook being the case that forced
    it, since it is authenticated by a repo signature rather than by a
    session.
    """
    if user_id is None:
        return []
    return list(
        {
            *TeamMembers.objects.filter(attendee=user_id, is_deleted=False).values_list(
                "team_id", flat=True
            ),
            *TeamMaster.objects.filter(owner=user_id, is_deleted=False).values_list(
                "team_id", flat=True
            ),
        }
    )


def member_project_ids(user_id, team_id=None) -> list:
    """Project ids `user_id` may see, optionally narrowed to one team.

    The canonical way to scope a team-wide task/note query down to the
    caller. `task_views.TaskMetaView` already derives exactly this list
    inline; endpoints that return "every task in the team" should filter
    through this instead.
    """
    if user_id is None:
        return []
    qs = ProjectMembers.objects.filter(attendee=user_id)
    if team_id is not None:
        qs = qs.filter(team=team_id)
    owned = ProjectMaster.objects.filter(owner=user_id, is_deleted=False)
    if team_id is not None:
        owned = owned.filter(team=team_id)
    return list(
        {
            *qs.values_list("project_id", flat=True),
            *owned.values_list("project_id", flat=True),
        }
    )


# ── raising guards (preferred for new code) ────────────────────────────


def require_team_member(user, team_id) -> TeamMaster:
    """Return the `TeamMaster` iff `user` belongs to it, else 404.

    Replaces the two hand-rolled `_verify_team_member` copies, and fixes
    their shared owner-FK omission.
    """
    if team_id is None:
        raise Http404(_TEAM_MISSING)
    user_id = getattr(user, "id", None)
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None or not is_team_member(team_id, user_id):
        raise Http404(_TEAM_MISSING)
    return team


def require_project_member(user, project_id) -> ProjectMaster:
    """Return the `ProjectMaster` iff `user` belongs to it, else 404."""
    if project_id is None:
        raise Http404(_PROJECT_MISSING)
    user_id = getattr(user, "id", None)
    project = ProjectMaster.objects.filter(project_id=project_id, is_deleted=False).first()
    if project is None or not is_project_member(project_id, user_id):
        raise Http404(_PROJECT_MISSING)
    return project


# ── Response-returning guards (for the v2 views that don't raise) ──────


def require_team_member_or_response(user, team_id):
    """`None` when allowed, else a 404 `Response`.

    Same semantics as `require_team_member`, shaped for the walrus idiom
    the v2 views already use for quota checks:

        if res := require_team_member_or_response(request.user, team_id):
            return res
    """
    if not is_team_member(team_id, getattr(user, "id", None)):
        return Response({"error": _TEAM_MISSING}, status=status.HTTP_404_NOT_FOUND)
    if not TeamMaster.objects.filter(team_id=team_id, is_deleted=False).exists():
        return Response({"error": _TEAM_MISSING}, status=status.HTTP_404_NOT_FOUND)
    return None


def require_project_member_or_response(user, project_id):
    """`None` when allowed, else a 404 `Response`."""
    if not is_project_member(project_id, getattr(user, "id", None)):
        return Response({"error": _PROJECT_MISSING}, status=status.HTTP_404_NOT_FOUND)
    if not ProjectMaster.objects.filter(project_id=project_id, is_deleted=False).exists():
        return Response({"error": _PROJECT_MISSING}, status=status.HTTP_404_NOT_FOUND)
    return None


# ── DRF permission classes (for new views) ─────────────────────────────
#
# The first `BasePermission` subclasses in this codebase. They read the
# scope id from the query string or body, which is how every existing
# endpoint receives it — there is no team middleware and no teamId
# header. New views should prefer these; existing views are being
# migrated to the function guards above, which is a smaller diff.


def _scope_id(request, *names):
    for name in names:
        val = request.query_params.get(name) or (
            request.data.get(name) if hasattr(request.data, "get") else None
        )
        if val:
            return val
    return None


class IsTeamMember(BasePermission):
    """Caller must own or belong to the `team_id` named in the request."""

    message = _TEAM_MISSING

    def has_permission(self, request, view) -> bool:
        return is_team_member(
            _scope_id(request, "team_id", "teamId"), getattr(request.user, "id", None)
        )


class IsProjectMember(BasePermission):
    """Caller must own or belong to the `project_id` named in the request."""

    message = _PROJECT_MISSING

    def has_permission(self, request, view) -> bool:
        return is_project_member(
            _scope_id(request, "project_id", "projectId"),
            getattr(request.user, "id", None),
        )
