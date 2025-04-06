from datetime import datetime
from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.task.task_models import *
from origin.serializers.task.task_serializers import *


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        data = {
            "team": request.data["team"],
            "project": request.data["project"],
            "assignee": request.data["assignee"],
            "reporter": request.data["reporter"],
            "title": request.data["title"],
            "priority": request.data["priority"],
            "priority_code": 0,
            "effort_level": request.data["effort_level"],
            "effort_level_code": 0,
            "status": request.data["status"],
            "status_code": 0,
            "content": request.data["content"],
            "due_date": request.data["due_date"],
            "github_url": request.data["github_url"],
            "github_url_title": request.data["github_url_title"],
            "general_url": request.data["general_url"],
            "general_url_title": request.data["general_url_title"],
            "tags": request.data["tags"],
        }
        serializer = TaskMasterSerializer(data=data)
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

        # tasks = TaskMaster.objects.filter(team=team_id)
        # serializer = TaskMasterSerializer(tasks, many=True)

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(team=team_id)
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": t.task_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "dueDate": str(t.due_date),
                    "daysLeft": (
                        max(0, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "parentTaskId": t.parent_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags,
                    "teamId": t.team.team_id,
                    "projectId": t.project.project_id,
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class GetPreviewTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")
        task_id = request.GET.get("task_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = TaskMaster.objects.prefetch_related("task_attachments").filter(
            team=team_id, project_id=project_id, task_id=task_id
        )

        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": t.task_id,
                    "project": {
                        "id": t.project.project_id,
                        "name": t.project.project_name,
                        "color": "primary",
                    },
                    "title": t.title,
                    "body": t.content,
                    "assignee": {
                        "teamId": t.team.team_id,
                        "userId": t.assignee.id,
                        "userName": t.assignee.username,
                        "userEmail": t.assignee.email,
                        "avatarImgPath": f"/img/path/to/{t.assignee.email}",
                        "online": True,
                    },
                    "reporter": {
                        "teamId": t.team.team_id,
                        "userId": t.reporter.id,
                        "userName": t.reporter.username,
                        "userEmail": t.reporter.email,
                        "avatarImgPath": f"/img/path/to/{t.reporter.email}",
                        "online": True,
                    },
                    "createdDate": str(t.ts_created_at.date()),
                    "dueDate": str(t.due_date),
                    "daysLeft": (
                        max(0, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "tags": t.tags,
                    "githubLink": {
                        "url": t.github_url,
                        "title": t.github_url_title,
                    },
                    "generalLink": {
                        "url": t.general_url,
                        "title": t.general_url_title,
                    },
                    "attachments": t.task_attachments.all().values_list(
                        "attached_file", flat=True
                    ),
                    "parentTaskId": t.parent_task_id,
                    "threadId": t.thread_id,
                },
            )
            break

        if len(response_data) == 1:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            print(response_data)
            return Response(
                {"error": "Failed to fetch expected task data"}, status=status.HTTP_400_BAD_REQUEST
            )


class GetProjectTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = request.GET.get("project_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # tasks = TaskMaster.objects.filter(team=team_id, project=project_id)
        # serializer = TaskMasterSerializer(tasks, many=True)

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(
            team=team_id, project_id=project_id
        )
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": t.task_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "dueDate": str(t.due_date),
                    "daysLeft": (
                        max(0, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "parentTaskId": t.parent_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags,
                    "teamId": t.team.team_id,
                    "projectId": t.project.project_id,
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class GetMyAssignedTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        user_id = request.GET.get("user_id")

        if not user_id or not team_id:
            return Response(
                {"error": "user_id and team_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # tasks = TaskMaster.objects.filter(team=team_id, assignee=user_id)
        # serializer = TaskMasterSerializer(tasks, many=True)

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(
            team=team_id, assignee=user_id
        )
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": t.task_id,
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "dueDate": str(t.due_date),
                    "daysLeft": (
                        max(0, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "parentTaskId": t.parent_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags,
                    "teamId": t.team.team_id,
                    "projectId": t.project.project_id,
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class TaskAttachmentsView(AuthenticatedAPIView):
    parser_classes = [MultiPartParser]

    def post(self, request):

        task = request.POST.get("task")
        attached_type = request.POST.get("attached_type")
        attached_file = request.FILES.get("attached_file")

        attachments_count = TaskAttachments.objects.filter(task=task).count()

        data = {
            "task": task,
            "attachment_id": attachments_count + 1,
            "attached_file": attached_file,
            "attached_type": attached_type,
        }

        print("attached_data:", data)

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
