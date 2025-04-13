import os
import base64
from datetime import datetime
from django.db.models import Q
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.task.task_models import *
from origin.serializers.task.task_serializers import *
from origin.models.project.prj_models import *


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        data = {
            "team": request.data["team"],
            "project": request.data["project"],
            "thread_id": request.data.get("thread_id", None),
            "parent_task_id": request.data.get("parent_task_id", None),
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
        print("create task data:", data)
        serializer = TaskMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        error = serializer.errors
        return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request):
        try:
            print("request.data:", request.data)
            task = TaskMaster.objects.get(task_id=request.data["task_id"])
        except TaskMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        data = {
            "team": request.data.get("team", task.team),
            "project": request.data.get("project", task.project),
            "thread_id": request.data.get("thread_id", task.thread_id),
            "parent_task_id": request.data.get("parent_task_id", task.parent_task_id),
            "assignee": request.data.get("assignee", task.assignee),
            "reporter": request.data.get("reporter", task.reporter),
            "title": request.data.get("title", task.title),
            "priority": request.data.get("priority", task.priority),
            "priority_code": task.priority_code,
            "effort_level": request.data.get("effort_level", task.effort_level),
            "effort_level_code": task.effort_level_code,
            "status": request.data.get("status", task.status),
            "status_code": task.status_code,
            "content": request.data.get("content", task.content),
            "due_date": request.data.get("due_date", task.due_date),
            "github_url": request.data.get("github_url", task.github_url),
            "github_url_title": request.data.get("github_url_title", task.github_url_title),
            "general_url": request.data.get("general_url", task.general_url),
            "general_url_title": request.data.get("general_url_title", task.general_url_title),
            "tags": request.data.get("tags", task.tags),
        }

        serializer = TaskMasterSerializer(task, data=data)
        if serializer.is_valid():
            serializer.save()
            return Response([serializer.data], status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


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
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
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


class GetTeamTasksByTagView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(team=team_id)
        response_data = []

        projects = {}
        for t in task_with_tags:
            if t.tags:
                if t.project.project_id not in projects:
                    projects[t.project.project_id] = {
                        "projectId": t.project.project_id,
                        "projectName": t.project.project_name,
                        "tags": {},
                    }

                for tag in t.tags:
                    if tag["tag_name"] not in projects[t.project.project_id]["tags"]:
                        projects[t.project.project_id]["tags"][tag["tag_name"]] = {
                            "tagName": tag["tag_name"],
                            "tagColor": tag["tag_color"],
                            "tagTextColor": tag["tag_text_color"],
                            "tasks": [],
                        }
                        projects[t.project.project_id]["tags"][tag["tag_name"]]["tasks"].append(
                            {
                                "taskId": t.task_id,
                                "title": t.title,
                                "status": t.status,
                            }
                        )

        for project_id, project_tasks in projects.items():
            response_data.append(project_tasks)

        return Response(response_data, status=status.HTTP_200_OK)


class GetPreviewTasksView(AuthenticatedAPIView):
    STATUS_COLOR_MAP = {
        "open": {"chipColor": "#0044c2", "textColor": "white"},
        "wip": {"chipColor": "#ffff23", "textColor": "black"},
        "pending": {"chipColor": "#ffa823", "textColor": "white"},
        "closed": {"chipColor": "#1dc200", "textColor": "white"},
        "deleted": {"chipColor": "#ff2323", "textColor": "white"},
    }

    PRIORITY_EFFORT_LEVEL_COLOR_MAP = {
        "low": {"chipColor": "#0044c2", "textColor": "white"},
        "medium": {"chipColor": "#1dc200", "textColor": "white"},
        "high": {"chipColor": "#ff2323", "textColor": "white"},
    }

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
            attached_files = []
            for _file in t.task_attachments.all().values_list("attached_file", "attached_type"):
                file_path = _file[0]
                file_type = _file[1]
                with open(file_path, "rb") as f:
                    encoded_file = base64.b64encode(f.read()).decode("utf-8")
                    attached_files.append(
                        {
                            "file": None,
                            "file_base64": encoded_file,
                            "name": os.path.basename(file_path),
                            "type": file_type,
                        }
                    )

            response_data.append(
                {
                    "id": t.task_id,
                    "project": {
                        "projectId": t.project.project_id,
                        "projectName": t.project.project_name,
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
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": {
                        "code": 0,
                        "status": t.status,
                        "color": self.STATUS_COLOR_MAP[t.status.lower()]["chipColor"],
                        "textColor": self.STATUS_COLOR_MAP[t.status.lower()]["textColor"],
                    },
                    "priority": {
                        "code": 0,
                        "priority": t.priority,
                        "color": (
                            self.PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            self.PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            self.PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()][
                                "chipColor"
                            ]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            self.PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()][
                                "textColor"
                            ]
                            if t.effort_level
                            else None
                        ),
                    },
                    "tags": t.tags,
                    "githubLink": {
                        "url": t.github_url,
                        "title": t.github_url_title,
                    },
                    "generalLink": {
                        "url": t.general_url,
                        "title": t.general_url_title,
                    },
                    "attachments": attached_files,
                    "parentTaskId": t.parent_task_id,
                    "threadId": t.thread_id,
                },
            )

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
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
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
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
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
