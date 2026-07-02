from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.team_models import TeamMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.serializers.note.note_serializers import (
    NoteVersionDetailSerializer,
    NoteVersionListItemSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import (
    require_read_role,
    require_write_role,
)
from origin.views.utils.note_version import snapshot_note_version
from origin.views.utils.request_validators import validate_request_data

VALID_NOTE_TYPES = {1, 2, 3}

NOTE_MODEL_BY_TYPE = {
    1: PersonalNoteMaster,
    2: TaskNoteMaster,
    3: ChatNoteMaster,
}


def _parse_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _validate_common(data, *, allow_missing=()):
    """Coerce `note_type` / `note_id` (and optional `version_no`) to ints.

    Returns either a tuple `(note_type, note_id[, version_no])` on
    success or a `Response` on validation failure.
    """
    note_type = _parse_int(data.get("note_type"))
    note_id = _parse_int(data.get("note_id"))
    version_no = _parse_int(data.get("version_no")) if "version_no" not in allow_missing else None

    if note_type is None or note_id is None:
        return Response(
            {"error": "note_type and note_id must be integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if note_type not in VALID_NOTE_TYPES:
        return Response({"error": "Invalid note_type."}, status=status.HTTP_400_BAD_REQUEST)

    if "version_no" not in allow_missing and version_no is None:
        return Response(
            {"error": "version_no must be an integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if "version_no" in allow_missing:
        return note_type, note_id
    return note_type, note_id, version_no


class NoteVersionListView(AuthenticatedAPIView):
    """List all versions for a note (newest first), without bodies."""

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "note_type": request.GET.get("note_type"),
            "note_id": request.GET.get("note_id"),
        }
        if res := validate_request_data(data):
            return res

        parsed = _validate_common(data, allow_missing=("version_no",))
        if isinstance(parsed, Response):
            return parsed
        note_type, note_id = parsed

        if res := require_read_role(request_user_id, note_type, note_id, data["team_id"]):
            return res

        versions = (
            NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id)
            .select_related("editor")
            .order_by("-version_no")
        )

        return Response(
            NoteVersionListItemSerializer(versions, many=True).data,
            status=status.HTTP_200_OK,
        )


class NoteVersionDetailView(AuthenticatedAPIView):
    """Fetch a single version's body for the modal preview."""

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "note_type": request.GET.get("note_type"),
            "note_id": request.GET.get("note_id"),
            "version_no": request.GET.get("version_no"),
        }
        if res := validate_request_data(data):
            return res

        parsed = _validate_common(data)
        if isinstance(parsed, Response):
            return parsed
        note_type, note_id, version_no = parsed

        if res := require_read_role(request_user_id, note_type, note_id, data["team_id"]):
            return res

        try:
            version = NoteVersionMaster.objects.select_related("editor").get(
                note_type=note_type, note_id=note_id, version_no=version_no
            )
        except NoteVersionMaster.DoesNotExist:
            return Response({"error": "Version not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response(NoteVersionDetailSerializer(version).data, status=status.HTTP_200_OK)


class NoteVersionRestoreView(AuthenticatedAPIView):
    """Restore a past version: copy its title/body onto the live row and
    write a new version marking the restore."""

    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "note_type": request.data.get("note_type"),
            "note_id": request.data.get("note_id"),
            "version_no": request.data.get("version_no"),
        }
        if res := validate_request_data(data):
            return res

        parsed = _validate_common(data)
        if isinstance(parsed, Response):
            return parsed
        note_type, note_id, version_no = parsed

        if res := require_write_role(request_user_id, note_type, note_id, data["team_id"]):
            return res

        try:
            source = NoteVersionMaster.objects.get(
                note_type=note_type, note_id=note_id, version_no=version_no
            )
        except NoteVersionMaster.DoesNotExist:
            return Response({"error": "Version not found."}, status=status.HTTP_404_NOT_FOUND)

        model = NOTE_MODEL_BY_TYPE.get(note_type)
        try:
            note = model.objects.get(note_id=note_id)
        except model.DoesNotExist:
            return Response({"error": "Note not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            team = TeamMaster.objects.get(team_id=data["team_id"])
        except TeamMaster.DoesNotExist:
            return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            with transaction.atomic():
                note.title = source.title
                note.body = source.body
                note.save(update_fields=["title", "body", "ts_updated_at"])

                new_version = snapshot_note_version(
                    team=team,
                    editor=request.user,
                    note_type=note_type,
                    note_id=note_id,
                    title=source.title,
                    body=source.body,
                    restored_from_version_no=version_no,
                )
        except Exception as e:
            return Response(
                {"error": "Failed to restore version.", "details": str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(
            NoteVersionDetailSerializer(new_version).data,
            status=status.HTTP_200_OK,
        )
