from django.db import transaction
from django.db.models import F, Value, IntegerField
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.note.note_serializers import *

from origin.views.utils.request_validators import validate_request_data, validate_request_user

NOTE_TYPE = 3  # Chat Notes


class AllChatNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        notes = []

        role_id = 1  # owner
        owner_chat_note_ids = list(
            NotePermissionMaster.objects.filter(
                team=data["team_id"], user=request_user_id, note_type=NOTE_TYPE, role_id=role_id
            ).values_list("note_id", flat=True)
        )
        owner_chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team_id"], owner=data["user_id"], note_id__in=owner_chat_note_ids
            )
            .annotate(
                # Add the static field here
                roleId=Value(role_id, output_field=IntegerField()),
                # Your existing annotations
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        role_id = 2  # editor
        editor_chat_note_ids = list(
            NotePermissionMaster.objects.filter(
                team=data["team_id"], user=request_user_id, note_type=NOTE_TYPE, role_id=role_id
            ).values_list("note_id", flat=True)
        )
        editor_chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team_id"], owner=data["user_id"], note_id__in=editor_chat_note_ids
            )
            .annotate(
                # Add the static field here
                roleId=Value(role_id, output_field=IntegerField()),
                # Your existing annotations
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        role_id = 3  # viewer
        viewer_chat_note_ids = list(
            NotePermissionMaster.objects.filter(
                team=data["team_id"], user=request_user_id, note_type=NOTE_TYPE, role_id=role_id
            ).values_list("note_id", flat=True)
        )
        viewer_chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team_id"], owner=data["user_id"], note_id__in=viewer_chat_note_ids
            )
            .annotate(
                # Add the static field here
                roleId=Value(role_id, output_field=IntegerField()),
                # Your existing annotations
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "teamId",
                "ownerId",
                "roleId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        notes.extend(list(owner_chat_notes))
        notes.extend(list(editor_chat_notes))
        notes.extend(list(viewer_chat_notes))

        return Response(notes, status=status.HTTP_200_OK)


class AllChatNoteMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        chat_note_ids = list(
            NotePermissionMaster.objects.filter(
                team=data["team_id"], user=request_user_id, note_type=NOTE_TYPE
            ).values_list("note_id", flat=True)
        )
        chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team_id"], owner=data["user_id"], note_id__in=chat_note_ids
            )
            .annotate(
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "noteId",
                "parentNoteId",
                "title",
                "tsUpdated",
            )
        )

        return Response(list(chat_notes), status=status.HTTP_200_OK)


class ChatNoteMasterView(AuthenticatedAPIView):
    def post(self, request, *args, **kwargs):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "owner": request.data.get("user_id"),
            "chat_type": request.data.get("chat_type"),
            "chat_id": request.data.get("chat_id"),
            "is_thread": request.data.get("is_thread"),
            "thread_id": request.data.get("thread_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        data["parent_note_id"] = request.data.get("parent_note_id")

        serializer = ChatNoteMasterSerializer(data=data)
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
                        "noteId": serializer.data["note_id"],
                        "parentNoteId": serializer.data["parent_note_id"],
                        "chatType": serializer.data["chat_type"],
                        "chatId": serializer.data["chat_id"],
                        "isThread": serializer.data["is_thread"],
                        "threadId": serializer.data["thread_id"],
                        "title": serializer.data["title"],
                        "body": serializer.data["body"],
                        "tsCreated": serializer.data["ts_created_at"],
                        "tsUpdated": serializer.data["ts_updated_at"],
                    }

                    # Second, create the associated role for that note
                    NotePermissionMaster.objects.create(
                        team=TeamMaster.objects.get(team_id=data["team"]),
                        user=CustomUser.objects.get(id=request_user_id),
                        note_id=note["noteId"],
                        note_type=NOTE_TYPE,
                        role_id=1,  # Assign the creator as the 'owner' (= 1)
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
            "user_id": request.data.get("user_id"),
            "note_id": request.data.get("note_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        try:
            note = ChatNoteMaster.objects.get(note_id=data["note_id"])
        except ChatNoteMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = ChatNoteMasterSerializer(note, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        try:
            note = ChatNoteMaster.objects.get(team=data["team"], note_id=data["note_id"])
            note.delete()
            return Response(
                {"message": f"Note deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except ChatNoteMaster.DoesNotExist:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class SingleChatNoteView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "owner": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team"],
                owner=data["owner"],
                note_id=data["note_id"],
            )
            .annotate(
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        if len(chat_notes) == 0:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(chat_notes[0], status=status.HTTP_200_OK)


class SingleChatNoteByIdView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "owner": request.GET.get("user_id"),
            "chat_type": request.GET.get("chat_type"),
            "chat_id": request.GET.get("chat_id"),
            "is_thread": request.GET.get("is_thread"),
            "thread_id": request.GET.get("thread_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        chat_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team"],
                owner=data["owner"],
                chat_type=data["chat_type"],
                chat_id=data["chat_id"],
                is_thread=data["is_thread"],
                thread_id=data["thread_id"],
            )
            .annotate(
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsCreated")  # ASC by created at
            .values(
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(chat_notes), status=status.HTTP_200_OK)


class ChatNoteAttachmentView(AuthenticatedAPIView):
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

        serializer = ChatNoteAttachmentViewSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "noteId": serializer.data["note"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "noteAttachmentUrl": serializer.data["note_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class ChatSubNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.GET.get("team_id"),
            "owner": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        sub_notes = (
            ChatNoteMaster.objects.filter(
                team=data["team"],
                owner=data["owner"],
                parent_note_id=data["note_id"],
            )
            .annotate(
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                chatType=F("chat_type"),
                chatId=F("chat_id"),
                isThread=F("is_thread"),
                threadId=F("thread_id"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "chatType",
                "chatId",
                "isThread",
                "threadId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(sub_notes), status=status.HTTP_200_OK)
