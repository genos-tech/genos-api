from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.task.task_models import *
from origin.serializers.task.task_serializers import *


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        print("request.data:", request.data)
        serializer = TaskMasterSerializer(data=request.data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class GetTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tasks = TaskMaster.objects.filter(team=team_id)
        serializer = TaskMasterSerializer(tasks, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class GetProjectTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tasks = TaskMaster.objects.filter(team=team_id, project=project_id)
        serializer = TaskMasterSerializer(tasks, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class GetMyAssignedTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "user_id and team_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tasks = TaskMaster.objects.filter(team=team_id, assignee=user_id)
        serializer = TaskMasterSerializer(tasks, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TaskAttachmentsView(AuthenticatedAPIView):
    def post(self, request):
        attachments_count = TaskAttachments.objects.filter(task=request.data["task"]).count()

        data = {
            "task": request.data["task"],
            "attachment_id": attachments_count + 1,
            "attachment_body": request.data["attached_file"],
        }

        serializer = TaskAttachmentsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        task = request.GET.get("task_id")

        if not task:
            return Response(
                {"error": "task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachments = TaskMaster.objects.filter(task=task)
        serializer = TaskMasterSerializer(attachments, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)


class TaskCommentsView(AuthenticatedAPIView):
    def post(self, request):
        comment_count = TaskComments.objects.filter(task=request.data["task_id"]).count()

        data = {
            "task": request.data["task_id"],
            "sender": request.data["sender_id"],
            "comment_id": comment_count + 1,
            "comment_body": request.data["comment_body"],
        }

        serializer = TaskCommentsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class TaskCommentsByIdView(AuthenticatedAPIView):
    def get(self, request):
        task_id = request.GET.get("task_id", None)
        if task_id:
            comments = TaskComments.objects.filter(task=task_id)
            serializer = TaskCommentsSerializer(comments, many=True)
            return Response(serializer.data)
        else:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)


class TaskTagsView(AuthenticatedAPIView):
    def post(self, request):
        tag_count = TaskTags.objects.filter(task=request.data["task_id"]).count()

        data = {
            "task": request.data["task_id"],
            "tag_id": tag_count + 1,
            "tag_name": request.data["tag_name"],
        }

        serializer = TaskTagsSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)


class TaskTagsByIdView(AuthenticatedAPIView):
    def get(self, request):
        task_id = request.GET.get("task_id", None)
        if task_id:
            tags = TaskTags.objects.filter(task=task_id)
            serializer = TaskTagsSerializer(tags, many=True)
            return Response(serializer.data)
        else:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)
