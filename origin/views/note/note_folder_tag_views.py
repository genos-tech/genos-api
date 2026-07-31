"""Tags for Team Notes folders.

Once a team accumulates more folders than fit on a screen, the tree stops
being a way to find things. Tags are the cross-cutting axis: a folder can
be "engineering" AND "onboarding" without living in two places.

The vocabulary is TEAM-scoped and shared — a tag means the same thing to
everyone, which is what makes filtering by it useful. Creating one is
therefore open to any team member, but DELETING one removes it from every
folder that uses it, so that stays with its creator (or the team owner).

Attaching tags to a folder is not done here: it rides on the team-folder
PUT/POST via `tag_ids`, because it is an edit to the folder and takes the
same editor+ permission. This module only manages the vocabulary itself.
"""

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NoteFolderTag, NoteFolderTagLink
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_folder_role import get_folder_role, is_team_member
from origin.views.utils.note_role import ROLE_EDITOR
from origin.views.utils.request_validators import validate_request_data

MAX_TAG_NAME = 40


def tag_dict(tag: NoteFolderTag) -> dict:
    return {
        "tagId": tag.tag_id,
        "name": tag.name,
        "color": tag.color,
        "createdBy": tag.created_by_id,
    }


def sync_folder_tags(folder, team_id, tag_ids):
    """Replace a folder's tag links with `tag_ids`.

    Set semantics, not append: the caller sends the tags the folder
    should END UP with, so unchecking one in the UI actually detaches it.
    Unknown or foreign tag ids are dropped rather than erroring — a stale
    tag in a request shouldn't fail the whole folder edit.
    """
    if tag_ids is None:
        return
    valid_ids = set(
        NoteFolderTag.objects.filter(tag_id__in=tag_ids, team=team_id).values_list(
            "tag_id", flat=True
        )
    )
    existing = set(
        NoteFolderTagLink.objects.filter(folder=folder).values_list("tag_id", flat=True)
    )
    NoteFolderTagLink.objects.filter(folder=folder, tag_id__in=existing - valid_ids).delete()
    NoteFolderTagLink.objects.bulk_create(
        [NoteFolderTagLink(folder=folder, tag_id=tid) for tid in valid_ids - existing]
    )


class NoteFolderTagView(AuthenticatedAPIView):
    def get(self, request):
        """The team's whole tag vocabulary, with how many folders use each."""
        request_user_id = request.user.id
        data = {"team_id": request.GET.get("team_id")}
        if res := validate_request_data(data):
            return res

        if not is_team_member(data["team_id"], request_user_id):
            return Response(
                {"error": "You are not a member of this team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        tags = list(NoteFolderTag.objects.filter(team=data["team_id"]).order_by("name"))
        # One grouped query rather than a count per tag.
        usage = {}
        for tag_id in NoteFolderTagLink.objects.filter(
            tag_id__in=[t.tag_id for t in tags]
        ).values_list("tag_id", flat=True):
            usage[tag_id] = usage.get(tag_id, 0) + 1

        return Response(
            [{**tag_dict(t), "folderCount": usage.get(t.tag_id, 0)} for t in tags],
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        request_user_id = request.user.id
        data = {
            "team_id": request.data.get("team_id"),
            "name": request.data.get("name"),
        }
        if res := validate_request_data(data):
            return res

        if not is_team_member(data["team_id"], request_user_id):
            return Response(
                {"error": "You are not a member of this team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        name = str(data["name"]).strip()
        if name == "" or len(name) > MAX_TAG_NAME:
            return Response(
                {"error": f"Tag name must be 1-{MAX_TAG_NAME} characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Idempotent: the vocabulary is shared, so two people naming the
        # same tag should converge on one rather than hit a 500 on the
        # unique constraint.
        tag, _ = NoteFolderTag.objects.get_or_create(
            team_id=data["team_id"],
            name=name,
            defaults={
                "color": request.data.get("color"),
                "created_by_id": request_user_id,
            },
        )
        return Response(tag_dict(tag), status=status.HTTP_201_CREATED)

    def delete(self, request):
        """Remove a tag from the vocabulary and from every folder."""
        request_user_id = request.user.id
        data = {
            "team_id": request.GET.get("team_id"),
            "tag_id": request.GET.get("tag_id"),
        }
        if res := validate_request_data(data):
            return res

        try:
            tag = NoteFolderTag.objects.get(tag_id=data["tag_id"], team=data["team_id"])
        except NoteFolderTag.DoesNotExist:
            return Response({"error": "Tag not found."}, status=status.HTTP_404_NOT_FOUND)

        # Deleting detaches the tag everywhere, so it isn't a per-folder
        # edit — restrict it to the person who introduced it, or the team
        # owner as the backstop for a departed creator.
        is_team_owner = TeamMaster.objects.filter(
            team_id=data["team_id"], owner=request_user_id
        ).exists()
        if str(tag.created_by_id) != str(request_user_id) and not is_team_owner:
            return Response(
                {"error": "Only the tag's creator or the team owner can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        with transaction.atomic():
            # Links cascade on the FK; deleting explicitly keeps the
            # intent obvious at the call site.
            NoteFolderTagLink.objects.filter(tag=tag).delete()
            tag.delete()

        return Response({"message": "Tag deleted."}, status=status.HTTP_200_OK)


class NoteFolderTagLinkView(AuthenticatedAPIView):
    """Set the tags on ONE folder. Editor+ on that folder."""

    def put(self, request):
        request_user_id = request.user.id
        data = {
            "team_id": request.data.get("team_id"),
            "folder_id": request.data.get("folder_id"),
        }
        if res := validate_request_data(data):
            return res

        if "tag_ids" not in request.data:
            return Response(
                {"error": "tag_ids is required."}, status=status.HTTP_400_BAD_REQUEST
            )

        try:
            folder = PersonalNoteFolder.objects.get(
                folder_id=data["folder_id"],
                team=data["team_id"],
                scope=PersonalNoteFolder.SCOPE_TEAM,
            )
        except PersonalNoteFolder.DoesNotExist:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        role = get_folder_role(request_user_id, folder.folder_id, data["team_id"])
        if role is None or role > ROLE_EDITOR:
            return Response(
                {"error": "You do not have permission to tag this folder."},
                status=status.HTTP_403_FORBIDDEN,
            )

        sync_folder_tags(folder, data["team_id"], request.data.get("tag_ids") or [])

        links = NoteFolderTagLink.objects.filter(folder=folder).values(
            "tag__tag_id", "tag__name", "tag__color"
        )
        return Response(
            [
                {"tagId": r["tag__tag_id"], "name": r["tag__name"], "color": r["tag__color"]}
                for r in links
            ],
            status=status.HTTP_200_OK,
        )
