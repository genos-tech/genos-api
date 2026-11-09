"""Milestone favorites (MIP-1053): star, unstar, list, and "I just opened it"."""

from rest_framework import status
from rest_framework.response import Response

from origin.models.task.milestone_models import MilestoneMaster
from origin.services.milestone_favorites import (
    can_see_milestone,
    mark_opened,
    star_milestone,
    unstar_milestone,
    visible_favorites,
)
from origin.services.task_progress import progress_at
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data


def _milestone_id(raw):
    """A milestone id from a request, or None when it is not a whole number."""
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _not_found():
    # One answer for "no such milestone" and "not yours to see": milestone ids
    # are sequential, so telling them apart would be an existence oracle.
    return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)


def _serialize_favorite(fav) -> dict:
    m = fav.milestone
    task = m.task
    return {
        "milestoneId": m.milestone_id,
        "taskId": m.task_id,
        "displayId": task.display_id if task is not None else None,
        "title": m.title,
        "status": m.status,
        "projectId": m.project_id,
        "projectName": m.project.project_name,
        "teamId": fav.team_id,
        # When the milestone, or anything under it, last had progress (MIP-1351); the
        # sidebar orders the list by it (MIP-1352). Null until recorded.
        "lastProgressAt": progress_at(task) if task is not None else None,
        "tsFavorited": fav.ts_created_at,
        "tsLastOpened": fav.ts_last_opened_at,
    }


class MilestoneFavoriteListView(AuthenticatedAPIView):
    """The caller's starred milestones that they can still open."""

    def get(self, request):
        favs = visible_favorites(request.user.id, team_id=request.GET.get("team_id"))
        return Response(
            {"favorites": [_serialize_favorite(f) for f in favs]}, status=status.HTTP_200_OK
        )


class MilestoneFavoriteView(AuthenticatedAPIView):
    """Star (POST) or unstar (DELETE) a milestone."""

    def post(self, request):
        milestone_id = _milestone_id(request.data.get("milestone_id"))
        if res := validate_request_data({"milestone_id": milestone_id}):
            return res
        milestone = (
            MilestoneMaster.objects.select_related("project")
            .filter(milestone_id=milestone_id)
            .first()
        )
        if milestone is None or not can_see_milestone(request.user.id, milestone):
            return _not_found()
        created = star_milestone(
            team_id=request.data.get("team_id"), user_id=request.user.id, milestone=milestone
        )
        return Response(
            {"isFavorited": True, "milestoneId": milestone.milestone_id},
            status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
        )

    def delete(self, request):
        milestone_id = _milestone_id(request.GET.get("milestone_id"))
        if res := validate_request_data({"milestone_id": milestone_id}):
            return res
        # Only the caller's own row is touched, so a lost membership never
        # leaves a star the user cannot remove.
        if not unstar_milestone(user_id=request.user.id, milestone_id=milestone_id):
            return Response(
                {"error": "Favorite not found.", "isFavorited": False},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response({"isFavorited": False}, status=status.HTTP_200_OK)


class MilestoneFavoriteOpenedView(AuthenticatedAPIView):
    """The caller opened a milestone: if it is a favorite, move it up the list."""

    def post(self, request):
        milestone_id = _milestone_id(request.data.get("milestone_id"))
        if res := validate_request_data({"milestone_id": milestone_id}):
            return res
        updated = mark_opened(user_id=request.user.id, milestone_id=milestone_id)
        return Response({"isFavorited": updated}, status=status.HTTP_200_OK)
