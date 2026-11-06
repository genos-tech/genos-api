"""Personal-note folders — user-created sidebar organization.

Folders are a pure organization layer over personal notes: personal-only
(owner-scoped on every handler), no NotePermissionMaster rows, never
shared, never surfaced in tabs/search/recents. See
`PersonalNoteFolder` in `origin/models/note/personal_note_models.py`.

Contract notes:
  - `PersonalNoteFolder` is SHARED with Team Notes, told apart by
    `scope`. EVERY query in this module therefore pins
    `scope="personal"` — without it, team folders would surface inside
    users' My Notes, and this module's destructive delete would reach
    content it must never touch. Team folders are served by
    `team_note_folder_views.py`, which has its own handlers rather than
    reusing these; in particular its delete REFUSES to destroy other
    people's notes, where the delete here is unconditionally recursive.
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

from rest_framework import status
from rest_framework.response import Response

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services.note_delete import DeleteRefused, delete_personal_folder
from origin.services.note_folder_manage import (
    FolderRefused,
    clean_name,
    create_personal_folder,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.personal_note_move import NoteMoveRefused, move_personal_note
from origin.views.utils.request_validators import (
    as_int,
    as_uuid,
    validate_request_data,
    validate_request_user,
)

NOTE_TYPE = 1  # Personal Notes
SCOPE = PersonalNoteFolder.SCOPE_PERSONAL


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
    """Fetch a PERSONAL folder scoped to (team, owner). Raises DoesNotExist.

    A folder id is a number and a team id a UUID. One that is not parses to None, and
    `= None` is `IS NULL`, which matches no row: the same DoesNotExist as an id that names
    nothing (the views' 404), not a ValueError or ValidationError (500).
    """
    return PersonalNoteFolder.objects.get(
        folder_id=as_int(folder_id), team=as_uuid(team_id), owner=owner_id, scope=SCOPE
    )


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
            PersonalNoteFolder.objects.filter(
                folder_id=current, team=team_id, owner=owner_id, scope=SCOPE
            )
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
            team=data["team_id"], owner=data["user_id"], scope=SCOPE
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

        try:
            folder = create_personal_folder(
                team_id=data["team_id"],
                owner_id=data["user_id"],
                name=data["name"],
                parent_folder_id=request.data.get("parent_folder_id"),
            )
        except FolderRefused as refused:
            return Response({"error": refused.message}, status=refused.status)

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
        name = request.data.get("name")
        if name is not None:
            try:
                folder.name = clean_name(name)
            except FolderRefused as refused:
                return Response({"error": refused.message}, status=refused.status)

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
        """DESTRUCTIVE delete (per product spec): the folder, every
        descendant folder, every note filed anywhere in that subtree,
        and each such note's own `parent_note_id` child-note chain are
        hard-deleted — nothing is re-parented or retained. Note
        permission + version rows are purged like the single-note
        DELETE; search-index chunks are left to the reindex cycle,
        matching the existing single-note delete behavior."""
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
            folder_ids, note_ids = delete_personal_folder(
                team_id=data["team_id"], owner_id=data["user_id"], folder_id=data["folder_id"]
            )
        except DeleteRefused as refused:
            return Response({"error": refused.message}, status=refused.status)

        # 200 with the deleted ids (not 204): the frontend uses them to
        # close tabs and purge local caches for every destroyed note.
        return Response(
            {
                "message": "Folder and contents deleted.",
                "deletedFolderIds": sorted(folder_ids),
                "deletedNoteIds": sorted(note_ids),
            },
            status=status.HTTP_200_OK,
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

        try:
            move_personal_note(
                user_id=request_user_id,
                team_id=data["team_id"],
                note=note,
                target_folder_id=target_folder_id,
            )
        except NoteMoveRefused as e:
            return Response({"error": str(e)}, status=e.status)

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
