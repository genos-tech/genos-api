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


def is_team_member(team_id, user_id) -> bool:
    """True if `user_id` owns or actively belongs to `team_id`.

    Mirrors `views/utils/note_folder_role.is_team_member`, which this is
    intended to eventually replace; kept behaviourally identical so the
    two can be unified without changing any note-ACL outcome.
    """
    if team_id is None or user_id is None:
        return False
    if TeamMaster.objects.filter(team_id=team_id, owner=user_id).exists():
        return True
    return TeamMembers.objects.filter(team=team_id, attendee=user_id, is_deleted=False).exists()


def is_project_member(project_id, user_id) -> bool:
    """True if `user_id` owns or belongs to `project_id`.

    No `is_deleted` filter: `ProjectMembers` has no such column, removal
    is a hard delete (`prj_views.LeaveProjectView`).
    """
    if project_id is None or user_id is None:
        return False
    if ProjectMaster.objects.filter(project_id=project_id, owner=user_id).exists():
        return True
    return ProjectMembers.objects.filter(project=project_id, attendee=user_id).exists()


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
