"""Starring milestones (MIP-1053): the sidebar's Favorites.

A favorite is a private bookmark, but the sidebar prints the milestone's title and
its project's name, so only a milestone the caller may open can be starred or
listed: a project member's, not deleted, not converted back into a task (a
conversion retires the milestone row). A star outlives a lost membership, so
the list checks access every time rather than trusting the row.
"""

from __future__ import annotations

from django.db.models import F, Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from origin.models.task.milestone_models import MilestoneFavorite, MilestoneMaster
from origin.views.utils.scope_guards import is_project_member


def can_see_milestone(user_id, milestone: MilestoneMaster) -> bool:
    """Same door as reading the milestone: live, and the caller is in its project."""
    if milestone.is_deleted or milestone.project.is_deleted:
        return False
    return is_project_member(milestone.project_id, user_id)


def star_milestone(*, team_id, user_id, milestone: MilestoneMaster) -> bool:
    """True when it was newly starred, False when it already was."""
    _fav, created = MilestoneFavorite.objects.get_or_create(
        user_id=user_id,
        milestone=milestone,
        defaults={"team_id": team_id if team_id is not None else milestone.team_id},
    )
    return created


def unstar_milestone(*, user_id, milestone_id) -> bool:
    """True when a star was removed, False when there was none."""
    deleted, _ = MilestoneFavorite.objects.filter(
        user_id=user_id, milestone_id=milestone_id
    ).delete()
    return bool(deleted)


def mark_opened(*, user_id, milestone_id) -> bool:
    """Move a starred milestone to the top of the user's list. True when the user
    has it starred; a milestone that is not a favorite is left alone."""
    return bool(
        MilestoneFavorite.objects.filter(user_id=user_id, milestone_id=milestone_id).update(
            ts_last_opened_at=timezone.now()
        )
    )


def visible_favorites(user_id, team_id=None) -> list[MilestoneFavorite]:
    """The user's favorites they can still open, the most recent progress first
    (MIP-1352): the milestone's `last_progress_at`, i.e. when it or anything under it
    last changed. A milestone with none recorded (null) goes after the ones that
    have, and ties and those nulls keep the earlier rule: most recently opened first,
    a star never opened sorting by when it was starred."""
    qs = MilestoneFavorite.objects.filter(
        user_id=user_id,
        milestone__is_deleted=False,
        milestone__project__is_deleted=False,
    ).select_related("milestone__project", "milestone__task__project")
    if team_id is not None:
        qs = qs.filter(Q(team_id=team_id))
    qs = qs.annotate(ts_last_touched=Coalesce("ts_last_opened_at", "ts_created_at")).order_by(
        F("milestone__task__last_progress_at").desc(nulls_last=True), "-ts_last_touched", "-id"
    )
    allowed: dict = {}
    out = []
    for fav in qs:
        project_id = fav.milestone.project_id
        if project_id not in allowed:
            allowed[project_id] = is_project_member(project_id, user_id)
        if allowed[project_id]:
            out.append(fav)
    return out
