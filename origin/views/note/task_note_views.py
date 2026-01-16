from django.db import transaction
from django.db.models import F, Value, IntegerField
from rest_framework.response import Response
from rest_framework import status

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.serializers.note.note_serializers import *
from origin.models.project.prj_models import ProjectMembers
from origin.views.utils.request_validators import validate_request_data, validate_request_user

NOTE_TYPE = 2  # Task Notes


class AllTaskNotesView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        project_ids = list(
            ProjectMembers.objects.filter(
                team=data["team_id"], attendee=request_user_id
            ).values_list("project_id", flat=True)
        )

        notes = (
            TaskNoteMaster.objects.filter(team=data["team_id"], project__in=project_ids)
            .annotate(
                # Add the static field here
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                roleId=Value(
                    3, output_field=IntegerField()
                ),  # TODO: use the correct role id (default: viewer)
                # Your existing annotations
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                projectId=F("project"),
                taskId=F("task"),
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
                "projectId",
                "taskId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(notes, status=status.HTTP_200_OK)


class AllTaskNoteMetaView(AuthenticatedAPIView):
    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        project_ids = list(
            ProjectMembers.objects.filter(
                team=data["team_id"], attendee=request_user_id
            ).values_list("project_id", flat=True)
        )

        notes = (
            TaskNoteMaster.objects.filter(team=data["team_id"], project__in=project_ids)
            .select_related("project", "task")
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                noteId=F("note_id"),
                parentNoteId=F("parent_note_id"),
                taskId=F("task"),
                projectId=F("project"),
                projectName=F("project__project_name"),
                taskTitle=F("task__title"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
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

        return Response(list(notes), status=status.HTTP_200_OK)


class TaskNoteMasterView(AuthenticatedAPIView):
    def get(self, request):
        data = {
            "team": request.GET.get("team_id"),
            "project_id": request.GET.get("project_id"),
            "task_id": request.GET.get("task_id"),
        }

        if res := validate_request_data(data):
            return res

        task_notes = (
            TaskNoteMaster.objects.filter(
                team=data["team"],
                project=data["project_id"],
                task=data["task_id"],
            )
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                taskId=F("task"),
                projectId=F("project"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsCreated")  # ASC by created at
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
                "title",
                "body",
                "tsCreated",
                "tsUpdated",
            )
        )

        return Response(list(task_notes), status=status.HTTP_200_OK)

    def post(self, request, *args, **kwargs):
        request_user_id = request.user.id

        data = {
            "team": request.data.get("team_id"),
            "owner": request.data.get("user_id"),
            "project": request.data.get("project_id"),
            "task": request.data.get("task_id"),
            "title": request.data.get("title"),
            "body": request.data.get("body"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["owner"])):
            return res

        data["parent_note_id"] = request.data.get("parent_note_id")

        serializer = TaskNoteMasterSerializer(data=data)
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
                        "projectId": serializer.data["project"],
                        "taskId": serializer.data["task"],
                        "title": serializer.data["title"],
                        "body": serializer.data["body"],
                        "tsCreated": serializer.data["ts_created_at"],
                        "tsUpdated": serializer.data["ts_updated_at"],
                    }

                    print(
                        "{team}, {user}, {note_id}, {note_type}, {role_id}".format(
                            team=TeamMaster.objects.get(team_id=data["team"]),
                            user=CustomUser.objects.get(id=request_user_id),
                            note_id=note["noteId"],
                            note_type=NOTE_TYPE,
                            role_id=1,  # Assign the creator as the 'owner' (= 1))
                        )
                    )

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
            note = TaskNoteMaster.objects.get(note_id=data["note_id"])
        except TaskNoteMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = TaskNoteMasterSerializer(note, data=update_data, partial=True)
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
            note = TaskNoteMaster.objects.get(team=data["team"], note_id=data["note_id"])
            note.delete()
            return Response(
                {"message": f"Note deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskNoteMaster.DoesNotExist:
            return Response(
                {"error": "Note not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class SingleTaskNoteView(AuthenticatedAPIView):
    def get(self, request):
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

        personal_notes = (
            TaskNoteMaster.objects.filter(team=data["team"], note_id=data["note_id"])
            .annotate(
                noteType=Value(NOTE_TYPE, output_field=IntegerField()),
                teamId=F("team"),
                ownerId=F("owner"),
                noteId=F("note_id"),
                projectId=F("project"),
                taskId=F("task"),
                parentNoteId=F("parent_note_id"),
                tsCreated=F("ts_created_at"),
                tsUpdated=F("ts_updated_at"),
            )
            .values(
                "noteType",
                "teamId",
                "ownerId",
                "noteId",
                "parentNoteId",
                "projectId",
                "taskId",
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


class TaskNoteAttachmentView(AuthenticatedAPIView):
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

        serializer = TaskNoteAttachmentFactSerializer(data=data)
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
