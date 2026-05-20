import os
import base64
from collections import defaultdict
from datetime import datetime
from django.db.models import Case, F, IntegerField, Max, Q, Value, When
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

from origin.services.github_webhooks import ensure_webhooks_for_links
from origin.views.utils.request_validators import validate_request_data, validate_request_user
from origin.views.utils.mention_handler import extractMentionedUsers

from .common_color import STATUS_COLOR_MAP, PRIORITY_COLOR_MAP, EFFORT_LEVEL_COLOR_MAP


def _bridge_milestone_to_parent(task, requested_milestone_id, parent_task_id):
    """Keep `parent_task_id` / `root_task_id` in sync with a milestone change.

    Mirrors the bridge logic in `TaskMasterView.post` so PUT updates
    behave the same way as creation. The contract (see
    `MilestoneMaster.task` doc) is that tasks "living in a milestone"
    have `parent_task_id == milestone.task_id` so the project task
    table nests them as sub-tasks of the milestone row.

    `requested_milestone_id` is the value pulled from `request.data`:
        - a milestone id  -> SET / CHANGE the link
        - `None`          -> CLEAR the link (caller must distinguish
                             "key absent" from "key present + null"
                             before invoking this helper)

    Writes are scoped via `save(update_fields=[...])` so the broader
    `serializer.save()` upstream can run independently without us
    stepping on its updates.
    """
    from origin.models.task.milestone_models import MilestoneMaster

    if requested_milestone_id is not None:
        try:
            m = MilestoneMaster.objects.select_related("task").get(
                milestone_id=requested_milestone_id, is_deleted=False
            )
        except MilestoneMaster.DoesNotExist:
            return
        # Don't let a task become its own parent (defensive: would only
        # happen if a milestone's backing task hit this endpoint, which
        # shouldn't be possible via the picker but might via direct
        # API calls).
        if m.task_id is None or m.task_id == task.task_id:
            return
        task.milestone_id = requested_milestone_id
        # Set parent task id to the requested parent task id if it exists, otherwise set it to the milestone's backing task id
        task.parent_task_id = parent_task_id if parent_task_id else m.task_id
        # Root task id is always the milestone's backing task id
        task.root_task_id = m.task_id
        # The frontend doesn't expose a direct sprint picker on tasks
        # — sprint is always inherited from the task's milestone. Sync
        # it here so a task moved into / between milestones lands in
        # the right sprint bucket without the client having to send
        # `sprint` explicitly.
        task.sprint_id = m.sprint_id
        task.save(
            update_fields=[
                "milestone_id",
                "parent_task_id",
                "root_task_id",
                "sprint_id",
                "ts_updated_at",
            ]
        )
    else:
        if task.milestone_id is None:
            return
        # Only break the parent link when the current parent IS the old
        # milestone's backing task. A genuine sub-task chain that
        # happened to share a milestone (e.g. Milestone -> Task A ->
        # Sub-task B; user clears milestone on B) must keep its A->B
        # parent edge intact.
        try:
            old_m = MilestoneMaster.objects.get(milestone_id=task.milestone_id)
            cleared_parent = old_m.task_id is not None and task.parent_task_id == old_m.task_id
        except MilestoneMaster.DoesNotExist:
            cleared_parent = False
        task.milestone_id = None
        # Mirror the milestone clear on the sprint so a row that used
        # to inherit a sprint via its milestone doesn't keep the now-
        # orphaned chip lingering on the table. Tasks without a
        # milestone are unscheduled by definition in the current UX.
        task.sprint_id = None
        update_fields = ["milestone_id", "sprint_id", "ts_updated_at"]
        if cleared_parent:
            task.parent_task_id = None
            task.root_task_id = task.task_id
            update_fields += ["parent_task_id", "root_task_id"]
        task.save(update_fields=update_fields)


def _cascade_milestone_to_subtasks(parent_task_id, milestone_id, depth_limit=10):
    """Push `milestone_id` down the `parent_task_id` chain.

    When a task is moved between milestones, its descendant sub-tasks
    transitively move with it — `milestone_views.py` aggregations and
    the sprint board both filter by `milestone_id`, so failing to
    cascade silently under-counts.

    BFS down the chain with a depth cap to defang any cyclic / corrupt
    parent_task_id loops that might exist in the wild. The cap is
    generous (10) because real task hierarchies are shallow.
    """
    collected = set()
    frontier = {parent_task_id}
    for _ in range(depth_limit):
        if not frontier:
            break
        children = set(
            TaskMaster.objects.filter(parent_task_id__in=frontier)
            .exclude(task_id__in=collected | {parent_task_id})
            .values_list("task_id", flat=True)
        )
        if not children:
            break
        collected |= children
        frontier = children
    if collected:
        # Mirror the milestone's sprint onto the cascade so descendants
        # follow the same milestone → sprint mapping as their root. A
        # cleared milestone (`milestone_id is None`) cascades a cleared
        # sprint to keep the invariant "task without milestone has no
        # auto-derived sprint".
        sprint_id = None
        if milestone_id is not None:
            from origin.models.task.milestone_models import MilestoneMaster

            try:
                m_for_sprint = MilestoneMaster.objects.only("sprint_id").get(
                    milestone_id=milestone_id
                )
                sprint_id = m_for_sprint.sprint_id
            except MilestoneMaster.DoesNotExist:
                pass
        TaskMaster.objects.filter(task_id__in=collected).update(
            milestone_id=milestone_id, sprint_id=sprint_id
        )


class TaskMasterView(AuthenticatedAPIView):
    def post(self, request):
        # Two-way bridge between `milestone` and `parent_task_id`:
        #   1. If the client passes a `milestone` id and no
        #      `parent_task_id`, promote the milestone's backing task
        #      to be the parent (so the table nests the new task
        #      beneath the milestone row).
        #   2. If the client passes a `parent_task_id` that points at a
        #      milestone's backing task, derive `milestone` from that
        #      parent so the new task carries its own milestone link
        #      (used by aggregations / sprint analytics that key off
        #      `milestone_id`). This is the path the milestone preview
        #      "+ Task" button takes.
        from origin.models.task.milestone_models import MilestoneMaster

        milestone_id = request.data.get("milestone")
        parent_task_id = request.data.get("parent_task_id", None)
        if milestone_id and parent_task_id in (None, "", "null"):
            try:
                m = MilestoneMaster.objects.select_related("task").get(
                    milestone_id=milestone_id, is_deleted=False
                )
                if m.task_id is not None:
                    parent_task_id = m.task_id
            except Exception:
                pass
        elif parent_task_id not in (None, "", "null") and not milestone_id:
            try:
                parent = MilestoneMaster.objects.filter(
                    task_id=parent_task_id, is_deleted=False
                ).first()
                if parent is not None:
                    milestone_id = parent.milestone_id
                else:
                    # Deeper inference: the parent task isn't a
                    # milestone backing task, but it might itself live
                    # inside a milestone (e.g. Milestone -> Task A ->
                    # Sub-task B). Inherit the milestone link so the
                    # new sub-task still belongs to the milestone for
                    # filtering / aggregates.
                    from origin.models.task.task_models import TaskMaster

                    parent_task = (
                        TaskMaster.objects.filter(task_id=parent_task_id)
                        .only("milestone_id")
                        .first()
                    )
                    if parent_task is not None and parent_task.milestone_id is not None:
                        milestone_id = parent_task.milestone_id
            except Exception:
                pass

        # Derive the sprint from the (possibly inferred) milestone so a
        # newly created task always lands in the same sprint bucket as
        # its milestone. The frontend doesn't expose a direct sprint
        # picker on tasks today; sprint is purely a milestone roll-up
        # in the current UX, so we ignore any explicit `sprint` in the
        # payload when a milestone is in scope. When no milestone is
        # in scope we fall back to whatever the client sent (kept for
        # any legacy callers), defaulting to None.
        sprint_id = request.data.get("sprint")
        if milestone_id:
            try:
                m_for_sprint = MilestoneMaster.objects.only("sprint_id").get(
                    milestone_id=milestone_id, is_deleted=False
                )
                sprint_id = m_for_sprint.sprint_id
            except MilestoneMaster.DoesNotExist:
                sprint_id = None

        data = {
            "team": request.data["team"],
            "project": request.data["project"],
            "chat_type": request.data.get("chat_type", None),
            "chat_id": request.data.get("chat_id", None),
            "thread_id": request.data.get("thread_id", None),
            "milestone": milestone_id,
            "sprint": sprint_id,
            "parent_task_id": parent_task_id,
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
            "links": request.data["links"],
            "tags": request.data["tags"],
            "is_init_task": request.data["is_init_task"] == True,
        }

        newly_mentioned_user_ids = []
        if "content" in request.data and request.data["content"] is not None:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(request.data["content"])
            newly_mentioned_user_ids = list(extract_user_handler.mentioned_user_ids)
            data["mentioned_user_ids"] = newly_mentioned_user_ids

        serializer = TaskMasterSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            # Best-effort: if any of the task's links is a GitHub PR URL,
            # auto-register our webhook on that repo so PR merges sync
            # back to task status. Swallows all errors — user lacking
            # repo admin is the common failure path.
            try:
                ensure_webhooks_for_links(request.user, data.get("links"))
            except Exception:
                pass
            return Response(
                {
                    "task": serializer.data,
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                },
                status=status.HTTP_201_CREATED,
            )

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
            return Response(
                {"error": "Task not found to delete."}, status=status.HTTP_404_NOT_FOUND
            )

        update_data = request.data.copy()

        # Capture the milestone change intent BEFORE the None-strip
        # below: an explicit `milestone: null` in the payload means
        # "clear the link", which we must distinguish from "key
        # absent" (no change). The bridge below runs after
        # serializer.save() and writes parent_task_id / root_task_id /
        # milestone_id directly so the None-strip can keep its current
        # behavior for every other field.
        milestone_in_request = "milestone" in request.data
        requested_milestone_id = request.data.get("milestone") if milestone_in_request else None

        # Get parent task id from request data
        parent_task_id = (
            request.data.get("parent_task_id") if "parent_task_id" in request.data else None
        )

        # Same "key absent vs explicit null" trap for `due_date`: the
        # frontend sends `due_date: null` when the user picks TBD, but
        # the None-strip below would silently drop the key and the
        # serializer would never clear the column. Capture the intent
        # here, then re-write the field directly after `serializer.save()`
        # so the rest of the request can keep flowing through the
        # existing strip-then-save path. We treat the empty string the
        # same as null because some legacy callers ship `""` instead.
        clear_due_date = "due_date" in request.data and request.data.get("due_date") in (None, "")

        # Remove None values from the update_data
        for key, val in request.data.items():
            if val is None:
                update_data.pop(key)

        newly_mentioned_user_ids = []
        if "content" in update_data:
            extract_user_handler = extractMentionedUsers()
            extract_user_handler.extract(update_data["content"])
            update_data["mentioned_user_ids"] = list(set(extract_user_handler.mentioned_user_ids))

            current_mentioned_user_ids = task.mentioned_user_ids if task.mentioned_user_ids else []
            newly_mentioned_user_ids = list(
                set(extract_user_handler.mentioned_user_ids) - set(current_mentioned_user_ids)
            )

        serializer = TaskMasterSerializer(task, data=update_data)
        if serializer.is_valid():
            serializer.save()

            # Same best-effort webhook registration as the POST path:
            # whenever a task's links change, scan for new PR URLs and
            # try to register the webhook for each unseen (owner, repo).
            try:
                ensure_webhooks_for_links(request.user, update_data.get("links"))
            except Exception:
                pass

            # Apply the explicit-clear intent captured above. The
            # serializer never saw `due_date` (the None-strip removed
            # it), so we have to write it ourselves; otherwise the
            # column would still hold the previous value and a refresh
            # would resurrect the old date that the user thought they
            # cleared.
            if clear_due_date:
                task.refresh_from_db()
                if task.due_date is not None:
                    task.due_date = None
                    task.save(update_fields=["due_date", "ts_updated_at"])

            # Bridge milestone <-> parent_task_id / root_task_id and
            # cascade the new milestone_id to descendant sub-tasks so
            # aggregations (sprint board, milestone rollups) stay
            # consistent. Mirrors `TaskMasterView.post`'s bridge.
            if milestone_in_request:
                task.refresh_from_db()
                _bridge_milestone_to_parent(task, requested_milestone_id, parent_task_id)
                _cascade_milestone_to_subtasks(task.task_id, requested_milestone_id)

            return Response(
                {
                    "task": serializer.data,
                    "newly_mentioned_user_ids": newly_mentioned_user_ids,
                },
                status=status.HTTP_200_OK,
            )

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request):
        data = {
            "team": request.GET.get("team_id"),
            "task_id": request.GET.get("task_id"),
            "is_init_task_boolean": request.GET.get("is_init_task_boolean"),
        }

        if res := validate_request_data(data):
            return res

        try:
            task = TaskMaster.objects.get(
                team=data["team"],
                task_id=data["task_id"],
                is_init_task=int(data["is_init_task_boolean"]) == 1,
            )
            task.delete()
            return Response(
                {"message": "Task deleted successfully."}, status=status.HTTP_204_NO_CONTENT
            )
        except TaskMaster.DoesNotExist:
            return Response(
                {"error": "Task not found."},
                status=status.HTTP_404_NOT_FOUND,
            )


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
            TaskMaster.objects.filter(
                team=data["team_id"], project__in=project_ids, is_init_task=False
            )
            .filter(~Q(status="Deleted"))
            .annotate(
                taskId=F("task_id"),
                parentTaskId=F("parent_task_id"),
                rootTaskId=F("root_task_id"),
                tsUpdated=F("ts_updated_at"),
            )
            .order_by("taskId")
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

        finished_task_ids = set(
            TaskMaster.objects.filter(
                team=data["team_id"], project__in=project_ids, is_init_task=False
            )
            .filter(Q(status__in=["Deleted", "Closed"]))
            .values_list("task_id", flat=True)
        )

        personal_notes = []
        for raw_personal_note in raw_personal_notes:
            # If the root task is closed or deleted, skip the task
            if raw_personal_note["rootTaskId"] in finished_task_ids:
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

        # select_related on the FKs accessed in the loop (assignee, team,
        # project) collapses 3 per-row lookups into JOINs on the main query.
        task_with_tags = (
            TaskMaster.objects.filter(team=team_id, is_init_task=False)
            .select_related("assignee", "team", "project")
            .prefetch_related("task_tags")
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
                    "tags": t.tags or [],
                    "concatTags": (
                        ("/" + "/".join([tag["tagName"] for tag in t.tags]) + "/")
                        if t.tags
                        else None
                    ),
                    "teamId": str(t.team.team_id),
                    "projectId": t.project.project_id,
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
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

        task_with_tags = TaskMaster.objects.prefetch_related("task_tags").filter(
            team=team_id, is_init_task=False
        )

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
    # Status precedence used by the sub-task list. Mirrors the previous
    # two-pass Python sort but pushed into SQL so we don't have to
    # materialize/order rows in memory.
    _STATUS_ORDER = (
        ("Open", 0),
        ("WIP", 1),
        ("Pending", 2),
        ("Closed", 3),
        ("Deleted", 4),
    )

    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_project_id = request.GET.get("project_id")
        raw_current_task_id = request.GET.get("current_task_id")

        if not team_id or not raw_project_id or not raw_current_task_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project_id = int(raw_project_id)
            current_task_id = int(raw_current_task_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The only consumer (`TaskSubTasksBlock`) renders just id, title,
        # status, assignee.userId, project (id/name/systemUserId) and
        # tags (tagName/tagColor). The previous implementation:
        #   1. Ran an N+1 query per child task.
        #   2. Read every attachment file from disk and base64-encoded it
        #      inline, ballooning responses to ~9 MB even though
        #      attachments/body/reporter/dates/etc. are never read.
        #   3. Sorted the result twice in Python.
        # All three are collapsed into a single annotated query that
        # selects the FK rows we touch and orders rows in SQL.
        status_order_expr = Case(
            *[When(status=label, then=Value(rank)) for label, rank in self._STATUS_ORDER],
            default=Value(len(self._STATUS_ORDER)),
            output_field=IntegerField(),
        )

        child_tasks = (
            TaskMaster.objects.filter(
                is_init_task=False,
                team=team_id,
                project_id=project_id,
                parent_task_id=current_task_id,
            )
            .annotate(_status_rank=status_order_expr)
            .order_by("_status_rank", "-ts_updated_at")
            .values(
                "task_id",
                "title",
                "status",
                "tags",
                "root_task_id",
                "parent_task_id",
                "thread_id",
                "assignee__id",
                "assignee__username",
                "assignee__email",
                "assignee__profile_image_file_name",
                "team__team_id",
                "project__project_id",
                "project__project_name",
                "project__project_system_user__id",
            )
        )

        response_data = []
        for t in child_tasks:
            status_label = t["status"] or ""
            status_color = STATUS_COLOR_MAP.get(status_label.lower(), {})

            response_data.append(
                {
                    "id": t["task_id"],
                    "project": {
                        "projectId": t["project__project_id"],
                        "projectName": t["project__project_name"],
                        "systemUserId": t["project__project_system_user__id"],
                    },
                    "title": t["title"],
                    "assignee": {
                        "teamId": t["team__team_id"],
                        "userId": t["assignee__id"],
                        "userName": t["assignee__username"],
                        "userEmail": t["assignee__email"],
                        "avatarImgPath": t["assignee__profile_image_file_name"],
                        "tsLastSeen": "",
                        "tsJoined": "",
                        "customStatus": "",
                    },
                    "status": {
                        "code": 0,
                        "status": status_label,
                        "color": status_color.get("chipColor"),
                        "textColor": status_color.get("textColor"),
                    },
                    "tags": t["tags"] or [],
                    "parentTaskId": t["parent_task_id"],
                    "rootTaskId": t["root_task_id"],
                    "threadId": t["thread_id"],
                },
            )

        return Response(response_data, status=status.HTTP_200_OK)


class GetTaskByThreadIdView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        chat_type = request.GET.get("chat_type")
        raw_chat_id = request.GET.get("chat_id")
        raw_thread_id = request.GET.get("thread_id")

        if not team_id or not chat_type or not raw_chat_id or not raw_thread_id:
            return Response(
                {"error": "Wrong parameters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        chat_id = int(raw_chat_id)
        thread_id = int(raw_thread_id)

        target_task = TaskMaster.objects.filter(
            is_init_task=False,
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
            team=team_id,
            project_id=target_task[0][0],
            task_id=target_task[0][1],
            is_init_task=False,
        )

        response_data = []
        for t in task_attachments:
            attached_files = []
            for _file in t.task_attachments.all().values_list(
                "attached_file", "attached_type", "original_filename"
            ):
                file_path = _file[0]
                file_type = _file[1]
                orig_name = _file[2]
                try:
                    with open("./uploads/" + file_path, "rb") as f:
                        encoded_file = base64.b64encode(f.read()).decode("utf-8")
                        attached_files.append(
                            {
                                "file": file_path,
                                "file_base64": encoded_file,
                                "name": orig_name or os.path.basename(file_path),
                                "type": file_type,
                            }
                        )
                except FileNotFoundError:
                    print(f"File not found: {file_path}")
                    continue

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
                    "assignee": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.assignee.id,
                            "userName": t.assignee.username,
                            "userEmail": t.assignee.email,
                            "avatarImgPath": t.assignee.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.assignee
                        else None
                    ),
                    "reporter": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.reporter.id,
                            "userName": t.reporter.username,
                            "userEmail": t.reporter.email,
                            "avatarImgPath": t.reporter.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.reporter
                        else None
                    ),
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
                            PRIORITY_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
                            if t.effort_level
                            else None
                        ),
                    },
                    "tags": t.tags or [],
                    "links": t.links or [],
                    "attachments": attached_files,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    # Milestone hooks: TaskPreview's reroute branch
                    # relies on `currentPreviewTask.isMilestone` to
                    # detect milestone backing rows and route to
                    # MilestonePreview. When this endpoint feeds the
                    # preview (e.g. opening a thread that's tied to a
                    # milestone-backing task), omitting these would
                    # leave the user staring at a regular TaskPreview
                    # for what is actually a milestone.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                },
            )

        if len(response_data) == 1:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
            return Response(
                {"error": "Failed to fetch expected task data"}, status=status.HTTP_400_BAD_REQUEST
            )


class GetTaskView(AuthenticatedAPIView):
    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_project_id = request.GET.get("project_id")
        raw_task_id = request.GET.get("task_id")

        if not team_id or not raw_project_id or not raw_task_id:
            return Response(
                {"error": "team_id/project_id/task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        project_id = int(raw_project_id)
        task_id = int(raw_task_id)

        # Get the specific task with its attachments.
        task = TaskMaster.objects.prefetch_related("task_attachments").filter(
            team=team_id, project_id=project_id, task_id=task_id, is_init_task=False
        )

        response_data = []
        for t in task:
            attached_files = []
            for _file in t.task_attachments.all().values_list(
                "attachment_id", "attached_file", "attached_type", "original_filename"
            ):
                attachment_id = _file[0]
                file_path = _file[1]
                file_type = _file[2]
                orig_name = _file[3]
                try:
                    with open("./uploads/" + file_path, "rb") as f:
                        encoded_file = base64.b64encode(f.read()).decode("utf-8")
                        attached_files.append(
                            {
                                "attachment_id": attachment_id,
                                "file": file_path,
                                "file_base64": encoded_file,
                                "name": orig_name or os.path.basename(file_path),
                                "type": file_type,
                            }
                        )
                except FileNotFoundError:
                    print(f"File not found: {file_path}")
                    continue

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
                    "assignee": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.assignee.id,
                            "userName": t.assignee.username,
                            "userEmail": t.assignee.email,
                            "avatarImgPath": t.assignee.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.assignee
                        else None
                    ),
                    "reporter": (
                        {
                            "teamId": t.team.team_id,
                            "userId": t.reporter.id,
                            "userName": t.reporter.username,
                            "userEmail": t.reporter.email,
                            "avatarImgPath": t.reporter.profile_image_file_name,
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        }
                        if t.reporter
                        else None
                    ),
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
                            PRIORITY_COLOR_MAP[t.priority.lower()]["chipColor"]
                            if t.priority
                            else None
                        ),
                        "textColor": (
                            PRIORITY_COLOR_MAP[t.priority.lower()]["textColor"]
                            if t.priority
                            else None
                        ),
                    },
                    "effortLevel": {
                        "code": 0,
                        "level": t.effort_level,
                        "color": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["chipColor"]
                            if t.effort_level
                            else None
                        ),
                        "textColor": (
                            EFFORT_LEVEL_COLOR_MAP[t.effort_level.lower()]["textColor"]
                            if t.effort_level
                            else None
                        ),
                    },
                    "tags": t.tags or [],
                    "links": t.links or [],
                    "attachments": attached_files,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "chatType": t.chat_type if t.chat_type and t.chat_type != -1 else None,
                    "chatId": t.chat_id if t.chat_id and t.chat_id != -1 else None,
                    "threadId": t.thread_id if t.thread_id and t.thread_id != -1 else None,
                    # Milestone hooks: callers (e.g. the note->open-task
                    # routing in TaskPreview) need to know whether this
                    # task is the backing row of a milestone so they can
                    # route to MilestonePreview instead of TaskPreview.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
                },
            )

        if len(response_data) == 1:
            return Response(response_data, status=status.HTTP_200_OK)
        else:
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

        # `select_related("assignee")` collapses what was N additional
        # queries (one per task for assignee email/username/img) into a
        # single JOIN. `team_id` / `project_id` use the FK column values
        # directly (the FKs use `to_field="team_id"` / `to_field="project_id"`
        # so these match what `t.team.team_id` returned previously) — no
        # JOIN needed.
        task_with_tags = (
            TaskMaster.objects.select_related("assignee")
            .prefetch_related("task_tags")
            .filter(team=team_id, project=project_id, is_init_task=False)
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
                    "assigneeId": t.assignee.id if t.assignee else None,
                    "assigneeEmail": t.assignee.email if t.assignee else None,
                    "assigneeName": t.assignee.username if t.assignee else None,
                    "assigneeImgPath": t.assignee.profile_image_file_name if t.assignee else None,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags or [],
                    "concatTags": "/" + "/".join([tag["tagName"] for tag in (t.tags or [])]) + "/",
                    "teamId": str(t.team_id),
                    "projectId": t.project_id,
                    # Milestone hooks: TaskFilterMenu's milestone-scope
                    # filter and DraggableTaskTable's auto-expand effect
                    # rely on these to find the milestone's backing task
                    # and gather its children. Without them the milestone
                    # sidebar entry would render an empty table.
                    "isMilestone": t.is_milestone,
                    "milestoneId": t.milestone_id,
                    "sprintId": t.sprint_id,
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

        task_with_tags = (
            TaskMaster.objects.filter(team=team_id, assignee=user_id, is_init_task=False)
            .select_related("assignee", "team", "project")
            .prefetch_related("task_tags")
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
                    "assigneeId": t.assignee.id if t.assignee else None,
                    "assigneeEmail": t.assignee.email if t.assignee else None,
                    "assigneeName": t.assignee.username if t.assignee else None,
                    "assigneeImgPath": t.assignee.profile_image_file_name if t.assignee else None,
                    "parentTaskId": t.parent_task_id,
                    "rootTaskId": t.root_task_id,
                    "threadId": t.thread_id,
                    "tags": t.tags or [],
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

            original_name = attached_file.name if attached_file else ""
            data = {
                "task": task,
                "attachment_id": (int(curr_attachments_id) if curr_attachments_id else 0) + 1,
                "attached_file": attached_file,
                "attached_type": attached_type,
                "original_filename": original_name,
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
                        "name": original_name or os.path.basename(file_path),
                    },
                    status=status.HTTP_201_CREATED,
                )

            error = serializer.errors
            return Response(error, status=status.HTTP_400_BAD_REQUEST)
        else:
            return Response({}, status=status.HTTP_201_CREATED)

    def get(self, request):
        raw_task_id = request.GET.get("task_id")

        if not raw_task_id:
            return Response(
                {"error": "task_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task = int(raw_task_id)
        attachments = TaskMaster.objects.filter(task=task, is_init_task=False)
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
        raw_task_id = request.GET.get("task_id")
        if not raw_task_id:
            return Response("task_id is not found", status=status.HTTP_400_BAD_REQUEST)
        try:
            task_id = int(raw_task_id)
        except (TypeError, ValueError):
            return Response(
                "task_id must be an integer",
                status=status.HTTP_400_BAD_REQUEST,
            )
        if task_id:

            # Fetch ALL reactions for this task in a single SQL (JOIN via the
            # double-underscore traversal on sender). Group by comment_id in
            # Python so the per-comment loop below just looks up a dict
            # instead of re-querying — eliminates the previous nested N+1
            # (one reaction query per comment).
            reaction_rows = TaskCommentReactionFact.objects.filter(task_id=task_id).values_list(
                "comment_id",
                "reaction_id",
                "reaction_emoji",
                "sender__username",
                "sender__id",
                "sender__profile_image_file_name",
                "ts_created_at",
            )
            reactions_by_comment = defaultdict(list)
            for row in reaction_rows:
                reactions_by_comment[int(row[0])].append(
                    {
                        "id": int(row[1]),
                        "emoji": row[2],
                        "sender": {
                            "userName": row[3],
                            "userId": row[4],
                            "avatarImgPath": row[5],
                            "tsLastSeen": "",
                            "tsJoined": "",
                            "customStatus": "",
                        },
                        "tsSent": row[6],
                    }
                )

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
                response_data.append(
                    {
                        "taskId": comment["task"],
                        "senderId": comment["sender__id"],
                        "senderName": comment["sender__username"],
                        "commentId": comment["comment_id"],
                        "commentBody": comment["comment_body"],
                        "reactions": reactions_by_comment.get(int(comment["comment_id"]), []),
                        "tsSent": str(comment["ts_sent_at"]),
                        "tsUpdated": str(comment["ts_updated_at"]),
                    }
                )

            return Response(
                sorted(response_data, key=lambda x: x["tsSent"], reverse=False),
                status=status.HTTP_200_OK,
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
        raw_comment_id = request.GET.get("comment_id")
        reaction_emoji = request.GET.get("reaction_emoji")

        if not team_id or not sender_id or not task_id or not raw_comment_id or not reaction_emoji:
            return Response(
                {
                    "error": "`team_id`, `sender_id`, `task_id`, `comment_id`, and `reaction_emoji` are required."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        comment_id = int(raw_comment_id)

        try:
            reaction = TaskCommentReactionFact.objects.get(
                team=team_id,
                sender=sender_id,
                task_id=int(task_id),
                comment_id=comment_id,
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
                else:
                    return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        except Exception:
            return Response(
                {"error": "Failed to create mentions."}, status=status.HTTP_400_BAD_REQUEST
            )

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


class TaskBodyAttachmentView(AuthenticatedAPIView):
    def post(self, request):
        request_user_id = request.user.id

        data = {
            "task": request.data.get("task_id"),
            "uploader": request.data.get("uploader"),
            "body_attachment_url": request.FILES.get("body_attachment_file"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["uploader"])):
            return res

        serializer = TaskBodyAttachmentFactSerializer(data=data)
        if serializer.is_valid():
            serializer.save()
            res = {
                "taskId": serializer.data["task"],
                "uploader": serializer.data["uploader"],
                "attachmentId": serializer.data["attachment_id"],
                "taskBodyAttachmentUrl": serializer.data["body_attachment_url"],
                "tsCreated": serializer.data["ts_created_at"],
                "tsUpdated": serializer.data["ts_updated_at"],
            }
            return Response(res, status=status.HTTP_200_OK)

        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
