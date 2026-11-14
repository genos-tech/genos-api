from rest_framework import status
from rest_framework.response import Response

from apis.request_id import error_response
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.serializers.note.note_serializers import NoteRoleMemberSerializer
from origin.services.note_sharing import SharingRefused, share_note, unshare_note
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.note_role import (
    ROLE_OWNER,
    get_effective_role,
    note_exists,
)
from origin.views.utils.request_validators import validate_request_data, validate_request_user

VALID_NOTE_TYPES = {1, 2, 3}
VALID_ROLE_IDS = {1, 2, 3}


class NoteRoleView(AuthenticatedAPIView):
    """Grant / update / revoke a role for one user on one note."""

    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "note_type": request.data.get("note_type"),
            "note_id": request.data.get("note_id"),
            "target_user_id": request.data.get("target_user_id"),
            "role_id": request.data.get("role_id"),
        }

        if res := validate_request_data(data):
            return res

        try:
            note_type = int(data["note_type"])
            note_id = int(data["note_id"])
            role_id = int(data["role_id"])
        except (TypeError, ValueError):
            return Response(
                {"error": "note_type, note_id, and role_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if note_type not in VALID_NOTE_TYPES:
            return Response({"error": "Invalid note_type."}, status=status.HTTP_400_BAD_REQUEST)
        if role_id not in VALID_ROLE_IDS:
            return Response({"error": "Invalid role_id."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            permission = share_note(
                team_id=data["team_id"],
                granter_id=request_user_id,
                note_type=note_type,
                note_id=note_id,
                target_user_id=data["target_user_id"],
                role_id=role_id,
            )
        except SharingRefused as refused:
            return Response({"error": refused.message}, status=refused.status)
        except Exception as e:
            # The cause goes to the log, not to the client (MIP-622).
            return error_response("note role grant failed", e, message="Failed to grant role.")

        return Response(NoteRoleMemberSerializer(permission).data, status=status.HTTP_200_OK)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "note_type": request.GET.get("note_type"),
            "note_id": request.GET.get("note_id"),
            "target_user_id": request.GET.get("target_user_id"),
        }

        if res := validate_request_data(data):
            return res

        try:
            note_type = int(data["note_type"])
            note_id = int(data["note_id"])
        except (TypeError, ValueError):
            return Response(
                {"error": "note_type and note_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if note_type not in VALID_NOTE_TYPES:
            return Response({"error": "Invalid note_type."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            unshare_note(
                granter_id=request_user_id,
                note_type=note_type,
                note_id=note_id,
                target_user_id=data["target_user_id"],
            )
        except SharingRefused as refused:
            return Response({"error": refused.message}, status=refused.status)

        return Response(status=status.HTTP_204_NO_CONTENT)


class NoteRoleCheckView(AuthenticatedAPIView):
    """
    Permission probe used by the Hocuspocus collab server to gate
    document loads. The collab server forwards the user's JWT and the
    note coordinates it parsed from `documentName`; we return 200 on
    allow (with the resolved role_id so the caller can surface it) and
    403 on deny. JWT auth already verifies the caller's identity — we
    don't trust an explicit `user_id` field.
    """

    def post(self, request):
        request_user_id = request.user.id

        try:
            note_type = int(request.data.get("note_type"))
            note_id = int(request.data.get("note_id"))
        except (TypeError, ValueError):
            return Response(
                {"error": "note_type and note_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if note_type not in VALID_NOTE_TYPES:
            return Response({"error": "Invalid note_type."}, status=status.HTTP_400_BAD_REQUEST)

        # A note the caller cannot read answers as a note that does not exist (MIP-1497): the
        # 403/404 split let any signed-in user probe which note ids exist. genos-collab treats
        # 403 and 404 alike, and nothing in the app calls this route.
        role = (
            get_effective_role(request_user_id, note_type, note_id)
            if note_exists(note_type, note_id)
            else None
        )
        if role is None:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response({"role_id": role}, status=status.HTTP_200_OK)


class NoteRoleListView(AuthenticatedAPIView):
    """List explicit role members on a note."""

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "note_type": request.GET.get("note_type"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        try:
            note_type = int(data["note_type"])
            note_id = int(data["note_id"])
        except (TypeError, ValueError):
            return Response(
                {"error": "note_type and note_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if note_type not in VALID_NOTE_TYPES:
            return Response({"error": "Invalid note_type."}, status=status.HTTP_400_BAD_REQUEST)

        # Caller must have effective access to the note.
        if get_effective_role(request_user_id, note_type, note_id, data["team_id"]) is None:
            return Response(
                {"error": "You do not have access to this note."},
                status=status.HTTP_403_FORBIDDEN,
            )

        members = (
            NotePermissionMaster.objects.filter(note_type=note_type, note_id=note_id)
            .select_related("user")
            .order_by("role_id", "ts_created_at")
        )

        return Response(
            NoteRoleMemberSerializer(members, many=True).data,
            status=status.HTTP_200_OK,
        )


class SharedPersonalNoteMetaView(AuthenticatedAPIView):
    """Personal notes shared with me by others — drives the sidebar's noteType=4 bucket."""

    NOTE_TYPE = 1

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        # Notes where I have an explicit row but am NOT the owner.
        granted_note_ids = list(
            NotePermissionMaster.objects.filter(
                team=data["team_id"],
                user=request_user_id,
                note_type=self.NOTE_TYPE,
            )
            .exclude(role_id=ROLE_OWNER)
            .values_list("note_id", flat=True)
        )

        if not granted_note_ids:
            return Response([], status=status.HTTP_200_OK)

        notes = list(
            PersonalNoteMaster.objects.filter(team=data["team_id"], note_id__in=granted_note_ids)
            .exclude(owner=request_user_id)
            .select_related("owner")
            .order_by("-ts_updated_at")
            .values(
                "note_id",
                "parent_note_id",
                "title",
                "ts_updated_at",
                "owner__id",
                "owner__username",
            )
        )

        # Pull my role on each note so the frontend can render badges
        # without a second round-trip.
        role_map = {
            row["note_id"]: row["role_id"]
            for row in NotePermissionMaster.objects.filter(
                user=request_user_id,
                note_type=self.NOTE_TYPE,
                note_id__in=granted_note_ids,
            ).values("note_id", "role_id")
        }

        return Response(
            [
                {
                    "noteType": self.NOTE_TYPE,
                    "noteId": n["note_id"],
                    "parentNoteId": n["parent_note_id"],
                    "title": n["title"],
                    "tsUpdated": n["ts_updated_at"],
                    "ownerId": n["owner__id"],
                    "ownerName": n["owner__username"],
                    "roleId": role_map.get(n["note_id"]),
                }
                for n in notes
            ],
            status=status.HTTP_200_OK,
        )
