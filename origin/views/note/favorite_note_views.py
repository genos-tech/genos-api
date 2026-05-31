from django.db.models import F, Value, IntegerField
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.note.note_serializers import NoteFavoriteMasterSerializer
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.views.utils.request_validators import validate_request_data, validate_request_user


class AllFavoriteNotesMetaView(AuthenticatedAPIView):
    """
    Get all favorite notes metadata for a user.
    Returns notes grouped by type with their full metadata for display in the sidebar.
    """

    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        # Get all favorite note records for this user
        favorites = NoteFavoriteMaster.objects.filter(
            team=data["team_id"], user=data["user_id"]
        ).values("note_id", "note_type", "ts_created_at")

        # Separate by note type
        personal_note_ids = [f["note_id"] for f in favorites if f["note_type"] == 1]
        task_note_ids = [f["note_id"] for f in favorites if f["note_type"] == 2]
        chat_note_ids = [f["note_id"] for f in favorites if f["note_type"] == 3]

        result = {
            "personalNotes": [],
            "taskNotes": [],
            "chatNotes": [],
        }

        # Fetch personal notes metadata
        if personal_note_ids:
            personal_notes = (
                PersonalNoteMaster.objects.filter(note_id__in=personal_note_ids)
                .annotate(
                    noteType=Value(1, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    tsCreated=F("ts_created_at"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "title",
                    "tsCreated",
                    "tsUpdated",
                )
            )
            result["personalNotes"] = list(personal_notes)

        # Fetch task notes metadata
        if task_note_ids:
            task_notes = (
                TaskNoteMaster.objects.filter(note_id__in=task_note_ids)
                .select_related("project", "task")
                .annotate(
                    noteType=Value(2, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    taskId=F("task"),
                    projectId=F("project"),
                    projectName=F("project__project_name"),
                    taskTitle=F("task__title"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "projectId",
                    "taskId",
                    "projectName",
                    "taskTitle",
                    "title",
                    "tsUpdated",
                )
            )
            result["taskNotes"] = list(task_notes)

        # Fetch chat notes metadata
        if chat_note_ids:
            chat_notes = (
                ChatNoteMaster.objects.filter(note_id__in=chat_note_ids)
                .annotate(
                    noteType=Value(3, output_field=IntegerField()),
                    noteId=F("note_id"),
                    parentNoteId=F("parent_note_id"),
                    chatType=F("chat_type"),
                    chatId=F("channel_id"),
                    isThread=F("is_thread"),
                    threadId=F("thread_root_id"),
                    tsUpdated=F("ts_updated_at"),
                )
                .values(
                    "noteType",
                    "noteId",
                    "parentNoteId",
                    "chatType",
                    "chatId",
                    "isThread",
                    "threadId",
                    "title",
                    "tsUpdated",
                )
            )
            result["chatNotes"] = list(chat_notes)

        return Response(result, status=status.HTTP_200_OK)


class NoteFavoriteView(AuthenticatedAPIView):
    """
    Add or remove a note from favorites.
    POST: Add a note to favorites
    DELETE: Remove a note from favorites
    """

    def post(self, request):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "user": request.data.get("user_id"),
            "note_id": request.data.get("note_id"),
            "note_type": request.data.get("note_type"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user"])):
            return res

        # Check if already favorited
        existing = NoteFavoriteMaster.objects.filter(
            team=data["team"],
            user=data["user"],
            note_id=data["note_id"],
            note_type=data["note_type"],
        ).first()

        if existing:
            return Response(
                {"message": "Note is already in favorites.", "isFavorited": True},
                status=status.HTTP_200_OK,
            )

        serializer = NoteFavoriteMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(
                {
                    "message": "Note added to favorites.",
                    "isFavorited": True,
                    "noteId": data["note_id"],
                    "noteType": data["note_type"],
                    "tsCreated": serializer.data["ts_created_at"],
                },
                status=status.HTTP_201_CREATED,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
            "note_type": request.GET.get("note_type"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        try:
            favorite = NoteFavoriteMaster.objects.get(
                team=data["team_id"],
                user=data["user_id"],
                note_id=data["note_id"],
                note_type=data["note_type"],
            )
            favorite.delete()
            return Response(
                {"message": "Note removed from favorites.", "isFavorited": False},
                status=status.HTTP_200_OK,
            )
        except NoteFavoriteMaster.DoesNotExist:
            return Response(
                {"error": "Favorite not found.", "isFavorited": False},
                status=status.HTTP_404_NOT_FOUND,
            )


class CheckNoteFavoriteView(AuthenticatedAPIView):
    """
    Check if a specific note is favorited by the user.
    """

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "note_id": request.GET.get("note_id"),
            "note_type": request.GET.get("note_type"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        is_favorited = NoteFavoriteMaster.objects.filter(
            team=data["team_id"],
            user=data["user_id"],
            note_id=data["note_id"],
            note_type=data["note_type"],
        ).exists()

        return Response({"isFavorited": is_favorited}, status=status.HTTP_200_OK)
