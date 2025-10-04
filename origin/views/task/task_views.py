import os
import base64
from datetime import datetime
from django.db.models import F, Max, Q
from rest_framework.response import Response
from rest_framework import status
from rest_framework.parsers import MultiPartParser

from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.task.task_models import *
from origin.serializers.task.task_serializers import *
from origin.models.project.prj_models import *
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.models.chat.reaction_models import *
from origin.serializers.chat.reaction_serializers import *

from origin.views.utils.request_validators import validate_request_data, validate_request_user

from .common_color import STATUS_COLOR_MAP, PRIORITY_EFFORT_LEVEL_COLOR_MAP


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        data = {
            "team": request.data["team"],
            "project": request.data["project"],
            "chat_type": request.data.get("chat_type", None),
            "chat_id": request.data.get("chat_id", None),
            "thread_id": request.data.get("thread_id", None),
            "parent_task_id": request.data.get("parent_task_id", None),
            "root_task_id": request.data.get("root_task_id", None),
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
            task_id = request.data.get("task_id")
            if task_id is None:
                return Response(
                    {"error": "task_id is required."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            task = TaskMaster.objects.get(task_id=task_id)
        except TaskMaster.DoesNotExist:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = TaskMasterSerializer(task, data=update_data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class TaskMetaView(AuthenticatedAPIView):
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

        raw_personal_notes = (
            TaskMaster.objects.filter(team=data["team_id"], project__in=project_ids)
            .filter(~Q(status="Deleted"))
            .annotate(
                taskId=F("task_id"),
                parentTaskId=F("parent_task_id"),
                rootTaskId=F("root_task_id"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("tsUpdated")
            .reverse()
            .values(
                "taskId",
                "rootTaskId",
                "parentTaskId",
                "project__project_id",
                "project__project_name",
                "project__project_system_user",
                "title",
                "status",
                "tsUpdated",
            )
        )

        deleted_task_ids = set(
            TaskMaster.objects.filter(team=data["team_id"], project__in=project_ids)
            .filter(Q(status="Deleted"))
            .values_list("task_id", flat=True)
        )

        personal_notes = []
        for raw_personal_note in raw_personal_notes:
            # If the root task is deleted, skip the task
            if raw_personal_note["rootTaskId"] in deleted_task_ids:
                continue

            personal_notes.append(
                {
                    "taskId": raw_personal_note["taskId"],
                    "parentTaskId": raw_personal_note["parentTaskId"],
                    "project": {
                        "projectId": raw_personal_note["project__project_id"],
                        "projectName": raw_personal_note["project__project_name"],
                        "systemUserId": raw_personal_note["project__project_system_user"],
                    },
                    "title": raw_personal_note["title"],
                    "status": {
                        "code": 0,
                        "status": raw_personal_note["status"],
                        "color": STATUS_COLOR_MAP[raw_personal_note["status"].lower()][
                            "chipColor"
                        ],
                        "textColor": STATUS_COLOR_MAP[raw_personal_note["status"].lower()][
                            "textColor"
                        ],
                    },
                    "tsUpdated": raw_personal_note["tsUpdated"],
                }
            )

        return Response(personal_notes, status=status.HTTP_200_OK)


class GetTeamTasksView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")

        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(team=team_id)
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": str(t.task_id),
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "assigneeImgPath": t.assignee.profile_image_file_name,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags,
                    "concatTags": "/" + "/".join([tag["tagName"] for tag in t.tags]) + "/",
                    "teamId": str(t.team.team_id),
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

        return Response(list(projects.values()), status=status.HTTP_200_OK)


class ChildTaskView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = int(request.GET.get("project_id"))
        current_task_id = int(request.GET.get("current_task_id"))

        if not team_id or not project_id or not current_task_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_tasks = TaskMaster.objects.filter(
            team=team_id,
            project=project_id,
            parent_task_id=current_task_id,
        ).values_list("project", "task_id")

        if len(target_tasks) == 0:
            return Response({}, status=status.HTTP_200_OK)

        response_data = []

        for target_task in target_tasks:
            task_attachments = TaskMaster.objects.prefetch_related("task_attachments").filter(
                team=team_id, project_id=target_task[0], task_id=target_task[1]
            )

            for t in task_attachments:
                attached_files = []
                for _file in t.task_attachments.all().values_list(
                    "attached_file", "attached_type"
                ):
                    file_path = _file[0]
                    file_type = _file[1]
                    with open("./uploads/" + file_path, "rb") as f:
                        encoded_file = base64.b64encode(f.read()).decode("utf-8")
                        attached_files.append(
                            {
                                "file": file_path,
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
                            "systemUserId": t.project.project_system_user.id,
                        },
                        "title": t.title,
                        "body": t.content,
                        "assignee": {
                            "teamId": t.team.team_id,
                            "userId": t.assignee.id,
                            "userName": t.assignee.username,
                            "userEmail": t.assignee.email,
                            "avatarImgPath": t.assignee.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "reporter": {
                            "teamId": t.team.team_id,
                            "userId": t.reporter.id,
                            "userName": t.reporter.username,
                            "userEmail": t.reporter.email,
                            "avatarImgPath": t.reporter.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "createdDate": str(t.ts_created_at.date()),
                        "updatedAt": str(t.ts_updated_at),
                        "dueDate": str(t.due_date) if t.due_date else None,
                        "daysLeft": (
                            max(-1, (t.due_date - datetime.now().date()).days)
                            if t.due_date
                            else None
                        ),
                        "status": {
                            "code": 0,
                            "status": t.status,
                            "color": STATUS_COLOR_MAP[t.status.lower()]["chipColor"],
                            "textColor": STATUS_COLOR_MAP[t.status.lower()]["textColor"],
                        },
                        "priority": {
                            "code": 0,
                            "priority": t.priority,
                            "color": (
                                PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["chipColor"]
                                if t.priority
                                else None
                            ),
                            "textColor": (
                                PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["textColor"]
                                if t.priority
                                else None
                            ),
                        },
                        "effortLevel": {
                            "code": 0,
                            "level": t.effort_level,
                            "color": (
                                PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()][
                                    "chipColor"
                                ]
                                if t.effort_level
                                else None
                            ),
                            "textColor": (
                                PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()][
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
                        "rootTaskId": t.root_task_id,
                        "threadId": t.thread_id,
                    },
                )

        if len(response_data) > 0:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "Failed to fetch expected task data"}, status=status.HTTP_400_BAD_REQUEST
            )


class GetTaskByThreadIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        chat_type = request.GET.get("chat_type")  # "dm" or "gm"
        chat_id = int(request.GET.get("chat_id"))
        thread_id = int(request.GET.get("thread_id"))

        if not team_id or not chat_type or not chat_id or not thread_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        target_task = TaskMaster.objects.filter(
            team=team_id,
            chat_type=chat_type,
            chat_id=chat_id,
            thread_id=thread_id,
        ).values_list("project", "task_id")

        if len(target_task) > 1:
            return Response(
                {"error": "Duplicated tasks found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if len(target_task) == 0:
            return Response({}, status=status.HTTP_200_OK)

        task_attachments = TaskMaster.objects.prefetch_related("task_attachments").filter(
            team=team_id, project_id=target_task[0][0], task_id=target_task[0][1]
        )

        response_data = []
        for t in task_attachments:
            attached_files = []
            for _file in t.task_attachments.all().values_list("attached_file", "attached_type"):
                file_path = _file[0]
                file_type = _file[1]
                with open("./uploads/" + file_path, "rb") as f:
                    encoded_file = base64.b64encode(f.read()).decode("utf-8")
                    attached_files.append(
                        {
                            "file": file_path,
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
                        "systemUserId": t.project.project_system_user.id,
                    },
                    "title": t.title,
                    "body": t.content,
                    "assignee": {
                        "teamId": t.team.team_id,
                        "userId": t.assignee.id,
                        "userName": t.assignee.username,
                        "userEmail": t.assignee.email,
                        "avatarImgPath": t.assignee.profile_image_file_name,
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "reporter": {
                        "teamId": t.team.team_id,
                        "userId": t.reporter.id,
                        "userName": t.reporter.username,
                        "userEmail": t.reporter.email,
                        "avatarImgPath": t.reporter.profile_image_file_name,
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": {
                        "code": 0,
                        "status": t.status,
                        "color": STATUS_COLOR_MAP[t.status.lower()]["chipColor"],
                        "textColor": STATUS_COLOR_MAP[t.status.lower()]["textColor"],
                    },
                    "priority": {
                        "code": 0,
                        "priority": t.priority,
                        "color": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
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
                    "rootTaskId": t.root_task_id,
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


class GetTaskView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        project_id = int(request.GET.get("project_id"))
        task_id = int(request.GET.get("task_id"))

        if not team_id or not project_id or not task_id:
            return Response(
                {"error": "team_id/project_id/task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_attachments = TaskMaster.objects.prefetch_related("task_attachments").filter(
            team=team_id, project_id=project_id, task_id=task_id
        )

        response_data = []
        for t in task_attachments:
            attached_files = []
            for _file in t.task_attachments.all().values_list(
                "attachment_id", "attached_file", "attached_type"
            ):
                attachment_id = _file[0]
                file_path = _file[1]
                file_type = _file[2]
                with open("./uploads/" + file_path, "rb") as f:
                    encoded_file = base64.b64encode(f.read()).decode("utf-8")
                    attached_files.append(
                        {
                            "attachment_id": attachment_id,
                            "file": file_path,
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
                        "systemUserId": t.project.project_system_user.id,
                    },
                    "title": t.title,
                    "body": t.content,
                    "assignee": {
                        "teamId": t.team.team_id,
                        "userId": t.assignee.id,
                        "userName": t.assignee.username,
                        "userEmail": t.assignee.email,
                        "avatarImgPath": t.assignee.profile_image_file_name,
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "reporter": {
                        "teamId": t.team.team_id,
                        "userId": t.reporter.id,
                        "userName": t.reporter.username,
                        "userEmail": t.reporter.email,
                        "avatarImgPath": t.reporter.profile_image_file_name,
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": {
                        "code": 0,
                        "status": t.status,
                        "color": STATUS_COLOR_MAP[t.status.lower()]["chipColor"],
                        "textColor": STATUS_COLOR_MAP[t.status.lower()]["textColor"],
                    },
                    "priority": {
                        "code": 0,
                        "priority": t.priority,
                        "color": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            PRIORITY_EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
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
                    "rootTaskId": t.root_task_id,
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

        if team_id is None or project_id is None:
            return Response(
                {"error": "team_id and project_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(
            team=team_id, project=project_id
        )
        response_data = []
        for t in task_with_tags:
            response_data.append(
                {
                    "id": str(t.task_id),
                    "title": t.title,
                    "priority": t.priority,
                    "effortLevel": t.effort_level,
                    "createdDate": str(t.ts_created_at.date()),
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "assigneeImgPath": t.assignee.profile_image_file_name,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags,
                    "concatTags": "/" + "/".join([tag["tagName"] for tag in t.tags]) + "/",
                    "teamId": str(t.team.team_id),
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
                    "updatedAt": str(t.ts_updated_at),
                    "dueDate": str(t.due_date) if t.due_date else None,
                    "daysLeft": (
                        max(-1, (t.due_date - datetime.now().date()).days) if t.due_date else None
                    ),
                    "status": t.status,
                    "assigneeId": t.assignee.id,
                    "assigneeEmail": t.assignee.email,
                    "assigneeName": t.assignee.username,
                    "assigneeImgPath": t.assignee.profile_image_file_name,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
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
        attachment_id = request.POST.get("attachment_id")
        attached_type = request.POST.get("attached_type")
        attached_file = request.FILES.get("attached_file")

        # Add only a new attachment
        if attachment_id != "" and int(attachment_id) == -1:

            curr_attachments_id = TaskAttachments.objects.filter(task=task).aggregate(
                Max("attachment_id")
            )["attachment_id__max"]

            data = {
                "task": task,
                "attachment_id": (int(curr_attachments_id) if curr_attachments_id else 0) + 1,
                "attached_file": attached_file,
                "attached_type": attached_type,
            }

            serializer = TaskAttachmentsSerializer(data=data)
            if serializer.is_valid():
                serializer.save()

                file_path = serializer.data["attached_file"].replace("/media/", "/uploads/")
                with open("." + file_path, "rb") as f:
                    encoded_file = base64.b64encode(f.read()).decode("utf-8")

                return Response(
                    {
                        **serializer.data,
                        "file_base64": encoded_file,
                        "name": os.path.basename(file_path),
                    },
                    status=status.HTTP_201_CREATED,
                )

            error = serializer.errors
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({}, status=status.HTTP_201_CREATED)

    def get(self, request):
        task = int(request.GET.get("task_id"))

        if not task:
            return Response(
                {"error": "task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        attachments = TaskMaster.objects.filter(task=task)
        serializer = TaskMasterSerializer(attachments, many=True)
        return Response(serializer.data, status=status.HTTP_200_OK)

    def delete(self, request):
        task = request.GET.get("task")
        attachment_id = request.GET.get("attachment_id")

        if not task or not attachment_id:
            return Response(
                {"error": "Both 'task' and 'attachment_id' are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            attachment = TaskAttachments.objects.get(task=task, attachment_id=attachment_id)
            attachment.delete()
            return Response(
                {"message": "Attachment deleted successfully."}, status=status.HTTP_204_NO_CONTENT
            )
        except TaskAttachments.DoesNotExist:
            return Response(
                {"error": "Attachment not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


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

    def put(self, request):
        task_id = request.data.get("task_id")
        comment_id = request.data.get("comment_id")

        if task_id is None or comment_id is None:
            return Response(
                {"error": "task_id and comment_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        message = TaskComments.objects.get(task=task_id, comment_id=comment_id)

        update_data = request.data.copy()

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        serializer = TaskCommentsSerializer(message, data=update_data, partial=True)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def get(self, request):
        user_id = request.GET.get("user_id")
        task_id = int(request.GET.get("task_id"))
        if task_id:

            # Fetch reactions
            raw_reactions = TaskCommentReactionFact.objects.filter(task_id=task_id)

            comments = (
                TaskComments.objects.filter(task=task_id)
                .select_related("sender")
                .values(
                    "task",
                    "comment_id",
                    "comment_body",
                    "ts_sent_at",
                    "ts_updated_at",
                    "sender__id",
                    "sender__username",
                )
            )

            response_data = []
            for comment in comments:
                reactions = raw_reactions.filter(
                    comment_id=int(comment["comment_id"])
                ).values_list(
                    "reaction_id",
                    "reaction_emoji",
                    "sender__username",
                    "sender__id",
                    "sender__profile_image_file_name",
                    "ts_created_at",
                )
                all_reactions = []
                for reaction in reactions:
                    _reaction = {
                        "id": int(reaction[0]),
                        "emoji": reaction[1],
                        "sender": {
                            "userName": reaction[2],
                            "userId": reaction[3],
                            "avatarImgPath": reaction[4],
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "tsSent": reaction[5],
                    }
                    all_reactions.append(_reaction)

                response_data.append(
                    {
                        "taskId": comment["task"],
                        "senderId": comment["sender__id"],
                        "senderName": comment["sender__username"],
                        "commentId": comment["comment_id"],
                        "commentBody": comment["comment_body"],
                        "reactions": all_reactions,
                        "tsSent": str(comment["ts_sent_at"]),
                        "tsUpdated": str(comment["ts_updated_at"]),
                    }
                )

            return Response(
                sorted(response_data, key=lambda x: x["tsSent"], reverse=False),
                status=status.HTTP_201_CREATED,
            )
        else:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)


class TaskCommentReactionView(AuthenticatedAPIView):
    def post(self, request):

        current_max_reaction_id = TaskCommentReactionFact.objects.filter(
            team_id=request.data["team_id"],
            task_id=request.data["task_id"],
            comment_id=request.data["comment_id"],
        ).aggregate(max_id=Max("reaction_id"))["max_id"]

        data = {
            "team": request.data["team_id"],
            "task": request.data["task_id"],
            "comment_id": int(request.data["comment_id"]),
            "reaction_id": current_max_reaction_id + 1 if current_max_reaction_id else 1,
            "reaction_emoji": request.data["reaction_emoji"],
            "sender": request.data["sender_id"],
        }

        serializer = TaskCommentReactionFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        sender_id = request.GET.get("sender_id")
        task_id = request.GET.get("task_id")
        comment_id = int(request.GET.get("comment_id"))
        reaction_emoji = request.GET.get("reaction_emoji")

        if not team_id or not sender_id or not task_id or not comment_id or not reaction_emoji:
            return Response(
                {
                    "error": "`team_id`, `sender_id`, `task_id`, `comment_id`, and `reaction_emoji` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            reaction = TaskCommentReactionFact.objects.get(
                team=team_id,
                sender=sender_id,
                task_id=int(task_id),
                comment_id=int(comment_id),
                reaction_emoji=reaction_emoji,
            )
            reaction.delete()
            return Response(
                {"message": f"Reaction deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskCommentReactionFact.DoesNotExist:
            return Response(
                {"error": "Reaction not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


class TaskCommentMentionView(AuthenticatedAPIView):
    def post(self, request):
        res = []
        try:
            for mentioned_user_id in list(request.data["mentioned_user_ids"]):
                data = {
                    "team": request.data["team_id"],
                    "task": request.data["task_id"],
                    "comment_id": int(request.data["comment_id"]),
                    "mentioned_user": mentioned_user_id,
                }

                serializer = TaskCommentMentionFactSerializer(data=data)
                if serializer.is_valid():
                    serializer.save()
                    res.append(serializer.data)
        except:
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        return Response(res, status=status.HTTP_201_CREATED)

    def get(self, request):
        team_id = request.GET.get("team_id")
        task_id = request.GET.get("task_id")
        comment_id = request.GET.get("comment_id")

        if not team_id or not task_id or not comment_id:
            return Response(
                {"error": "team_id, task_id, and comment_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        mentions = TaskCommentMentionFact.objects.filter(
            team=team_id,
            task=task_id,
            comment_id=comment_id,
        ).values()

        mentioned_user_ids = []
        for mention in mentions:
            mentioned_user_ids.append(mention["mentioned_user_id"])

        return Response(mentioned_user_ids, status=status.HTTP_200_OK)

    def delete(self, request):
        team_id = request.GET.get("team_id")
        task_id = request.GET.get("task_id")
        comment_id = request.GET.get("comment_id")
        mentioned_user_ids = request.GET.get("mentioned_user_ids")

        if not team_id or not mentioned_user_ids or not task_id or not comment_id:
            return Response(
                {
                    "error": "`team_id`, `mentioned_user_ids`, `task_id`, `comment_id` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            for mentioned_user_id in list(str(mentioned_user_ids).split(",")):
                reaction = TaskCommentMentionFact.objects.get(
                    team=team_id,
                    task=int(task_id),
                    comment_id=comment_id,
                    mentioned_user=mentioned_user_id,
                )
                reaction.delete()
            return Response(
                {"message": f"Mention deleted successfully."},
                status=status.HTTP_204_NO_CONTENT,
            )
        except TaskCommentMentionFact.DoesNotExist:
            return Response(
                {"error": "Mention not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
