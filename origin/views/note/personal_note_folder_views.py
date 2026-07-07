"""Personal-note folders — user-created sidebar organization.

Folders are a pure organization layer over personal notes: personal-only
(owner-scoped on every handler), no NotePermissionMaster rows, never
shared, never surfaced in tabs/search/recents. See
`PersonalNoteFolder` in `origin/models/note/personal_note_models.py`.

Contract notes:
  - This module uses KEY-PRESENCE semantics for optional structural
    fields ("parent_folder_id" / "folder_id" present-but-null means
    "move to root"), unlike the legacy note PUTs whose None-strip makes
    explicit null inexpressible. Structure changes must therefore go
    through these endpoints, never through the legacy PUTs.
  - Personal moves deliberately do NOT bump `ts_updated_at`
    (queryset `.update()` skips auto_now): the sidebar sorts notes by
    -tsUpdated, and folder membership isn't indexed in OpenSearch, so a
    move should neither reshuffle the list nor trigger a re-embed.
"""

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data, validate_request_user

NOTE_TYPE = 1  # Personal Notes


def _folder_dict(folder: PersonalNoteFolder) -> dict:
    """CamelCase wire shape, mirroring the meta endpoints' manual style."""
    return {
        "folderId": folder.folder_id,
        "parentFolderId": folder.parent_folder_id,
        "name": folder.name,
        "tsCreated": folder.ts_created_at,
        "tsUpdated": folder.ts_updated_at,
    }


def _get_owned_folder(folder_id, team_id, owner_id):
    """Fetch a folder scoped to (team, owner). Raises DoesNotExist."""
    return PersonalNoteFolder.objects.get(folder_id=folder_id, team=team_id, owner=owner_id)


def _creates_cycle(owner_id, team_id, folder_id, target_parent_id) -> bool:
    """True if putting `folder_id` under `target_parent_id` would create
    a cycle (target is the folder itself or one of its descendants).
    Walks the target's ancestor chain upward; the visited set defends
    against pre-existing corrupt loops."""
    current = target_parent_id
    visited = set()
    while current is not None:
        if current == folder_id:
            return True
        if current in visited:
            # Pre-existing loop that doesn't include folder_id — treat
            # as invalid target rather than spinning forever.
            return True
        visited.add(current)
        current = (
            PersonalNoteFolder.objects.filter(folder_id=current, team=team_id, owner=owner_id)
            .values_list("parent_folder_id", flat=True)
            .first()
        )
    return False


class PersonalNoteFolderView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        folders = PersonalNoteFolder.objects.filter(
            team=data["team_id"], owner=data["user_id"]
        ).order_by("name")

        return Response([_folder_dict(f) for f in folders], status=status.HTTP_200_OK)

    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "name": request.data.get("name"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        name = str(data["name"]).strip()
        if name == "" or len(name) > 255:
            return Response(
                {"error": "Folder name must be 1-255 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parent_folder_id = request.data.get("parent_folder_id")
        if parent_folder_id is not None:
            try:
                _get_owned_folder(parent_folder_id, data["team_id"], data["user_id"])
            except PersonalNoteFolder.DoesNotExist:
                return Response(
                    {"error": "Parent folder not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        folder = PersonalNoteFolder.objects.create(
            team_id=data["team_id"],
            owner_id=data["user_id"],
            parent_folder_id=parent_folder_id,
            name=name,
        )

        return Response(_folder_dict(folder), status=status.HTTP_201_CREATED)

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "folder_id": request.data.get("folder_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        try:
            folder = _get_owned_folder(data["folder_id"], data["team_id"], data["user_id"])
        except PersonalNoteFolder.DoesNotExist:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        # Rename — only when a non-empty name is supplied.
        if "name" in request.data and request.data.get("name") is not None:
            name = str(request.data["name"]).strip()
            if name == "" or len(name) > 255:
                return Response(
                    {"error": "Folder name must be 1-255 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            folder.name = name

        # Move — key presence means intent; an explicit null moves the
        # folder to root.
        if "parent_folder_id" in request.data:
            target_parent_id = request.data.get("parent_folder_id")
            if target_parent_id is not None:
                try:
                    _get_owned_folder(target_parent_id, data["team_id"], data["user_id"])
                except PersonalNoteFolder.DoesNotExist:
                    return Response(
                        {"error": "Target folder not found."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if _creates_cycle(
                    data["user_id"], data["team_id"], folder.folder_id, target_parent_id
                ):
                    return Response(
                        {"error": "Cannot move a folder into its own descendant."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            folder.parent_folder_id = target_parent_id

        folder.save()
        return Response(_folder_dict(folder), status=status.HTTP_200_OK)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "folder_id": request.GET.get("folder_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        try:
            with transaction.atomic():
                folder = _get_owned_folder(data["folder_id"], data["team_id"], data["user_id"])
                # Contents move UP one level (to the deleted folder's
                # parent, or to root). `.update()` skips auto_now on the
                # notes, so they keep their -tsUpdated sidebar position.
                new_parent = folder.parent_folder_id
                PersonalNoteFolder.objects.filter(
                    team=data["team_id"],
                    owner=data["user_id"],
                    parent_folder_id=folder.folder_id,
                ).update(parent_folder_id=new_parent)
                PersonalNoteMaster.objects.filter(
                    team=data["team_id"],
                    owner=data["user_id"],
                    folder_id=folder.folder_id,
                ).update(folder_id=new_parent)
                folder.delete()
        except PersonalNoteFolder.DoesNotExist:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(
            {"message": "Folder deleted successfully."},
            status=status.HTTP_204_NO_CONTENT,
        )


class PersonalNoteMoveView(AuthenticatedAPIView):
    """Move a personal note into a folder (or to root with an explicit
    `folder_id: null`). Owner-only — editors of a shared personal note
    must not rearrange the owner's sidebar. The note is re-rooted
    (`parent_note_id=None`): folders own ROOT-level notes, and the
    note's own child subtree rides along implicitly."""

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "note_id": request.data.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        # `folder_id` is required BY KEY (null is a valid value meaning
        # "root") — validate_request_data would reject the null.
        if "folder_id" not in request.data:
            return Response(
                {"error": "folder_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        target_folder_id = request.data.get("folder_id")

        try:
            note = PersonalNoteMaster.objects.get(team=data["team_id"], note_id=data["note_id"])
        except PersonalNoteMaster.DoesNotExist:
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)

        if str(note.owner_id) != str(request_user_id):
            return Response(
                {"error": "Only the note owner can move it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if target_folder_id is not None:
            try:
                _get_owned_folder(target_folder_id, data["team_id"], data["user_id"])
            except PersonalNoteFolder.DoesNotExist:
                return Response(
                    {"error": "Target folder not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Queryset .update() on purpose: skips auto_now so the move
        # neither reshuffles the -tsUpdated sidebar ordering nor routes
        # the note into the incremental reindex window (folder
        # membership isn't indexed). See module docstring.
        PersonalNoteMaster.objects.filter(note_id=note.note_id).update(
            folder_id=target_folder_id,
            parent_note_id=None,
        )

        return Response(
            {
                "noteType": NOTE_TYPE,
                "noteId": note.note_id,
                "parentNoteId": None,
                "folderId": target_folder_id,
                "title": note.title,
                "tsUpdated": note.ts_updated_at,
            },
            status=status.HTTP_200_OK,
        )
