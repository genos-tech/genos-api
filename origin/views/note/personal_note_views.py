from django.db import transaction
from django.db.models import F, IntegerField, Value
from rest_framework import status
from rest_framework.response import Response

from origin.serializers.note.note_serializers import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.mention_handler import extractMentionedUsers, resolve_group_members
from origin.views.utils.note_role import (
    ROLE_OWNER,
    delete_note_permissions,
    get_effective_role,
    require_read_role,
    require_write_role,
)
from origin.views.utils.note_version import (
    delete_note_versions,
    snapshot_note_version,
)
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.views.utils.upload_limits import check_upload_size

NOTE_TYPE = 1  # Personal Notes


class AllPersonalNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        personal_notes = (
            PersonalNoteMaster.objects.filter(team=data["team_id"], owner=data["user_id"])
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                roleId=Value(ROLE_OWNER, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                folderId=F("folder_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "folderId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(personal_notes), status=status.HTTP_200_OK)


class AllPersonalNoteMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        personal_notes = (
            PersonalNoteMaster.objects.filter(team=data["team_id"], owner=data["user_id"])
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                folderId=F("folder_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "noteType",
                "noteId",
                "parentNoteId",
                "folderId",
                "title",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(personal_notes), status=status.HTTP_200_OK)


class PersonalNoteMasterView(AuthenticatedAPIView):
    def post(self, request, *args, **kwargs):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "owner": request.data.get("user_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        data["parent_note_id"] = request.data.get("parent_note_id")

        # Optional folder placement — "New note here" on a sidebar
        # folder. Validate ownership so a note can't be filed into
        # someone else's folder.
        folder_id = request.data.get("folder_id")
        if folder_id is not None:
            from origin.models.note.personal_note_models import PersonalNoteFolder

            if not PersonalNoteFolder.objects.filter(
                folder_id=folder_id, team=data["team"], owner=data["owner"]
            ).exists():
                return Response(
                    {"error": "Target folder not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        data["folder_id"] = folder_id

        serializer = PersonalNoteMasterSerializer(data=data)
        if serializer.is_valid():
            try:
                # Wrap the database operations in a transaction
                with transaction.atomic():
                    # First, create the main note
                    # The serializer's save() method will call its .create() method
                    serializer.save()

                    note = {
                        "teamId": serializer.data["team"],
                        "ownerId": serializer.data["owner"],
                        "noteType": NOTE_TYPE,
                        "noteId": serializer.data["note_id"],
                        "parentNoteId": serializer.data["parent_note_id"],
                        "folderId": serializer.data["folder_id"],
                        "title": serializer.data["title"],
                        "body": serializer.data["body"],
                        "tsCreated": serializer.data["ts_created_at"],
                        "tsUpdated": serializer.data["ts_updated_at"],
                    }

                    # Second, create the associated role for that note
                    team_obj = TeamMaster.objects.get(team_id=data["team"])
                    NotePermissionMaster.objects.create(
                        team=team_obj,
                        user=CustomUser.objects.get(id=request_user_id),
                        note_id=note["noteId"],
                        note_type=NOTE_TYPE,
                        role_id=ROLE_OWNER,
                    )

                    # Third, write the initial version snapshot (v1).
                    snapshot_note_version(
                        team=team_obj,
                        editor=request.user,
                        note_type=NOTE_TYPE,
                        note_id=note["noteId"],
                        title=note["title"],
                        body=note["body"],
                    )

            except Exception as e:
                # If anything fails, return a server error
                return Response(
                    {"error": "Failed to create note and role.", "details": str(e)},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

            # If the transaction is successful, return the created note data
            return Response(note, status=status.HTTP_201_CREATED)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        request_user_id = request.user.id

        data = {
            "note_id": request.data.get("note_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            note = PersonalNoteMaster.objects.get(note_id=data["note_id"])
        except PersonalNoteMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        # Walk the BlockNote body and compute the three mention lists,
        # mirroring task_views.py PUT. `newly_*` drives the per-user
        # toast, `all_*` is what the ActivityFact row stores so prior
        # recipients keep their feed entry, and `removed_*` tells the
        # handler to DELETE the row when the body is mention-free.
        newly_mentioned_user_ids = []
        all_mentioned_user_ids = []
        removed_user_ids = []
        if "body" in update_data:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(update_data["body"])
            full_mentioned = set(extract_user_handler.mentioned_user_ids)
            full_mentioned |= resolve_group_members(extract_user_handler.mentioned_group_ids)
            update_data["mentioned_user_ids"] = list(full_mentioned)

            prev_set = set(note.mentioned_user_ids or [])
            newly_mentioned_user_ids = list(full_mentioned - prev_set)
            removed_user_ids = list(prev_set - full_mentioned)
            all_mentioned_user_ids = list(full_mentioned)

        serializer = PersonalNoteMasterSerializer(note, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            # Snapshot the post-save state. The helper handles
            # same-session coalescing internally.
            try:
                snapshot_note_version(
                    team=note.team,
                    editor=request.user,
                    note_type=NOTE_TYPE,
                    note_id=note.note_id,
                    title=note.title,
                    body=note.body,
                )
            except Exception as e:
                # Version write failure shouldn't fail the user's save.
                # Log and continue.
                print(f"NoteVersion snapshot failed for personal note {note.note_id}: {e}")
            return Response(
                {
                    **serializer.data,
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                    "all_mentioned_user_ids": all_mentioned_user_ids,
                    "removed_user_ids": removed_user_ids,
                },
                status=status.HTTP_200_OK,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note_id"]):
            return res

        try:
            with transaction.atomic():
                note = PersonalNoteMaster.objects.get(team=data["team"], note_id=data["note_id"])
                note.delete()
                delete_note_permissions(NOTE_TYPE, data["note_id"])
                delete_note_versions(NOTE_TYPE, data["note_id"])
            return Response(
                {"message": "Note deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except PersonalNoteMaster.DoesNotExist:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class SinglePersonalNoteView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        # 404 if missing, 403 if no access — `require_read_role` does
        # both checks in the right order so the existing single-note
        # not-found contract still returns 404.
        if res := require_read_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"]):
            return res

        role = get_effective_role(request_user_id, NOTE_TYPE, data["note_id"], data["team"])

        personal_notes = (
            PersonalNoteMaster.objects.filter(team=data["team"], note_id=data["note_id"])
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                roleId=Value(role, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                folderId=F("folder_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "folderId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        if len(personal_notes) == 0:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(personal_notes[0], status=status.HTTP_200_OK)


class PersonalNoteAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "note": request.data.get("note_id"),
            "uploader": request.data.get("uploader"),
            "note_attachment_url": request.FILES.get("note_attachment_file"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        if res := require_write_role(request_user_id, NOTE_TYPE, data["note"]):
            return res

        # Tier quota: per-file upload size.
        if res := check_upload_size(request.user, data["note_attachment_url"]):
            return res

        serializer = PersonalNoteAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "noteType": NOTE_TYPE,
                "noteId": serializer.data["note"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "noteAttachmentUrl": serializer.data["note_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
