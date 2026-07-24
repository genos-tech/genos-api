from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.purge import purge_milestone, purge_task
from origin.services import mention_extractor
from origin.services.custom_fields import sanitize_custom_field_values
from origin.services.milestone_service import (
    create_milestone,
    ensure_backing_task,
    parse_iso_date,
    sync_backing_task,
    sync_milestone_assignees,
)
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.services.thread_link import find_thread_link_conflict
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.mention_handler import resolve_group_members
from origin.views.utils.request_validators import validate_request_data

CLOSED_STATUSES = {"Closed", "Deleted"}

# Backing-task invariants moved to `origin/services/milestone_service.py`
# so the agent's composite write tool (`create_task_plan`) shares them.
# Aliases keep every call site in this module byte-identical.
_parse_iso_date = parse_iso_date
_ensure_backing_task = ensure_backing_task
_sync_backing_task = sync_backing_task


def _format_due_date(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _serialize_user(user) -> dict | None:
    if user is None:
        return None
    return {
        "userId": user.id,
        "username": getattr(user, "username", None),
        "firstName": getattr(user, "first_name", None),
        "lastName": getattr(user, "last_name", None),
        "email": getattr(user, "email", None),
        "profileImageUrl": (
            user.profile_image_url.url if getattr(user, "profile_image_url", None) else None
        ),
    }


def _serialize_assignee(a: MilestoneAssignees) -> dict:
    user = a.user
    if user is None:
        return {"userId": None, "username": None}
    return _serialize_user(user) or {"userId": None, "username": None}


def _serialize_milestone(m: MilestoneMaster, *, with_aggregates: bool = True) -> dict:
    out = {
        "milestoneId": m.milestone_id,
        "taskId": m.task_id,
        # Human-readable id from the backing task's `display_id`
        # property ("<code>-<n>"). Lets the table / preview surfaces
        # show "TP-3" instead of "#3" for milestone rows without an
        # extra lookup. Falls back to None when the milestone has no
        # backing task yet (legacy rows lazy-backfilled below).
        "displayId": m.task.display_id if m.task_id is not None and m.task else None,
        # Chat-thread origin, carried on the backing TaskMaster row —
        # set when the milestone was created from a DM/GM/MDM thread.
        # Same `-1` legacy-junk normalization as GetTaskView. Drives
        # the milestone preview's "Check thread" affordance.
        "chatType": (
            m.task.chat_type
            if m.task_id is not None and m.task and m.task.chat_type and m.task.chat_type != -1
            else None
        ),
        "chatId": (
            m.task.chat_id
            if m.task_id is not None and m.task and m.task.chat_id and m.task.chat_id != "-1"
            else None
        ),
        "threadId": (
            m.task.thread_id
            if m.task_id is not None and m.task and m.task.thread_id and m.task.thread_id != "-1"
            else None
        ),
        "projectId": m.project_id,
        "teamId": m.team_id,
        "sprintId": m.sprint_id,
        "reporterId": m.reporter_id,
        # Resolved reporter user object so the milestone preview can
        # render an avatar / name without an extra team-member lookup.
        "reporter": _serialize_user(getattr(m, "reporter", None)),
        "title": m.title,
        "description": m.description,
        "status": m.status,
        "statusCode": m.status_code,
        "priority": m.priority,
        "priorityCode": m.priority_code,
        "effortLevel": m.effort_level,
        "effortLevelCode": m.effort_level_code,
        "startDate": _format_due_date(m.start_date),
        "dueDate": _format_due_date(m.due_date),
        "tags": m.tags,
        "links": m.links,
        # Custom field values live ONLY on the backing task row (so
        # `sync_backing_task` — which rewrites the backing row from
        # milestone fields — can never clobber them, and the project
        # task table gets them with no extra plumbing). Serialized here
        # so the milestone preview can edit them like a task does.
        "customFieldValues": (
            (m.task.custom_field_values or {}) if m.task_id is not None and m.task else {}
        ),
        "isDeleted": m.is_deleted,
        "tsCreatedAt": m.ts_created_at,
        "tsUpdatedAt": m.ts_updated_at,
        "assignees": [
            _serialize_assignee(a) for a in m.milestone_assignees.select_related("user").all()
        ],
    }
    if with_aggregates:
        # Tasks belong to a milestone via the explicit FK *or* by being
        # children of the milestone's backing task (parent_task_id).
        # Either path counts as "in this milestone" for rollups.
        q = Q(milestone=m, is_deleted=False, is_init_task=False)
        if m.task_id is not None:
            q = q | Q(
                parent_task_id=m.task_id,
                is_deleted=False,
                is_init_task=False,
                is_milestone=False,
            )
        tasks = TaskMaster.objects.filter(q).distinct()
        total = tasks.count()
        closed = tasks.filter(status__in=list(CLOSED_STATUSES)).count()
        out["tasksTotal"] = total
        out["tasksClosed"] = closed
    return out


class ProjectMilestonesView(AuthenticatedAPIView):
    """List milestones for a project with optional status / sprint filters."""

    def get(self, request):
        project_id = request.GET.get("project_id")
        if res := validate_request_data({"project_id": project_id}):
            return res

        try:
            project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        statuses_param = request.GET.get("statuses")
        sprint_id_param = request.GET.get("sprint_id")

        qs = (
            MilestoneMaster.objects.filter(project=project, is_deleted=False)
            .select_related("task", "reporter")
            .prefetch_related("milestone_assignees__user")
            .order_by("-ts_updated_at")
        )

        if statuses_param:
            wanted = [s.strip() for s in statuses_param.split(",") if s.strip()]
            qs = qs.filter(status__in=wanted)

        if sprint_id_param is not None and sprint_id_param != "":
            if sprint_id_param == "null":
                qs = qs.filter(sprint__isnull=True)
            else:
                qs = qs.filter(sprint_id=sprint_id_param)

        # Lazy backfill for legacy milestones that predate the backing
        # task FK. Walking the queryset is cheap because `.select_related`
        # already pulled the task row; only milestones with `task_id is
        # None` actually trigger a write.
        milestones_list = list(qs)
        for m in milestones_list:
            if m.task_id is None:
                _ensure_backing_task(m)

        milestones = [_serialize_milestone(m) for m in milestones_list]
        return Response({"milestones": milestones}, status=status.HTTP_200_OK)


class MilestoneView(AuthenticatedAPIView):
    """CRUD for a single milestone.

    PATCH explicitly supports moving a milestone between sprints by
    sending `{ sprint_id: <int|null> }`. Tasks ride along automatically
    because they only FK the milestone, not the sprint.
    """

    def get(self, request, milestone_id: int):
        try:
            m = (
                MilestoneMaster.objects.select_related(
                    "project", "team", "sprint", "task", "reporter"
                )
                .prefetch_related("milestone_assignees__user")
                .get(milestone_id=milestone_id, is_deleted=False)
            )
        except MilestoneMaster.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)
        # Lazy-create the backing task for legacy milestones so the
        # client always gets a non-null `taskId` to drive comments /
        # notes / attachments tabs against.
        _ensure_backing_task(m)
        return Response(
            {"milestone": _serialize_milestone(m)},
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        data = {
            "project_id": request.data.get("project_id"),
            "title": request.data.get("title"),
        }
        if res := validate_request_data(data):
            return res

        try:
            project = ProjectMaster.objects.get(project_id=data["project_id"], is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        sprint_id = request.data.get("sprint_id")
        sprint = None
        if sprint_id is not None:
            try:
                sprint = Sprint.objects.get(sprint_id=sprint_id, project=project, is_deleted=False)
            except Sprint.DoesNotExist:
                return Response(
                    {"error": "Sprint not found in this project."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        # Chat-thread origin (milestone created from a DM/GM/MDM thread).
        # Stored on the backing TaskMaster row — same columns and same
        # one-task-per-thread rule as regular task creation, since the
        # backing row IS a task row.
        chat_type = request.data.get("chat_type")
        chat_id = request.data.get("chat_id")
        thread_id = request.data.get("thread_id")
        if chat_id and thread_id:
            conflict_task_id = find_thread_link_conflict(project.team_id, chat_id, thread_id)
            if conflict_task_id is not None:
                return Response(
                    {
                        "error": "This thread is already linked to a task.",
                        "code": "thread_already_has_task",
                        "existing_task_id": conflict_task_id,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            milestone = create_milestone(
                project,
                reporter_id=request.data.get("reporter_id") or request.user.id,
                title=data["title"],
                description_blocks=request.data.get("description"),
                status=request.data.get("status") or "Open",
                status_code=request.data.get("status_code"),
                priority=request.data.get("priority"),
                priority_code=request.data.get("priority_code"),
                effort_level=request.data.get("effort_level"),
                effort_level_code=request.data.get("effort_level_code"),
                start_date=request.data.get("start_date"),
                due_date=request.data.get("due_date"),
                sprint=sprint,
                tags=request.data.get("tags"),
                links=request.data.get("links"),
                assignee_ids=request.data.get("assignee_ids") or [],
            )
            if chat_id and thread_id:
                backing = _ensure_backing_task(milestone)
                backing.chat_type = chat_type
                backing.chat_id = str(chat_id)
                backing.thread_id = str(thread_id)
                backing.save(update_fields=["chat_type", "chat_id", "thread_id", "ts_updated_at"])

            # Seed custom field values from the create form. Stored on
            # the backing task only — see _serialize_milestone.
            raw_custom_values = request.data.get("custom_field_values")
            if raw_custom_values is not None:
                cleaned_values = sanitize_custom_field_values(raw_custom_values)
                if cleaned_values:
                    backing = _ensure_backing_task(milestone)
                    backing.custom_field_values = cleaned_values
                    backing.save(update_fields=["custom_field_values", "ts_updated_at"])

        # Creating a milestone always writes a backing TaskMaster row,
        # which the project task table renders alongside regular tasks.
        invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
        return Response(
            {"milestone": _serialize_milestone(milestone)},
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request, milestone_id: int):
        try:
            milestone = MilestoneMaster.objects.get(milestone_id=milestone_id, is_deleted=False)
        except MilestoneMaster.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)

        # Validate custom field values BEFORE the transaction below —
        # rejecting them mid-transaction (after milestone.save()) would
        # return a 400 for a patch that had already half-applied.
        custom_values_in_request = "custom_field_values" in request.data
        cleaned_custom_values = None
        if custom_values_in_request:
            cleaned_custom_values = sanitize_custom_field_values(
                request.data.get("custom_field_values")
            )
            if cleaned_custom_values is None:
                return Response(
                    {"error": "custom_field_values must be an object."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        with transaction.atomic():
            # Sprint move support: explicit `sprint_id` key (including
            # `None`) is honored; absent key leaves the sprint as-is.
            sprint_changed = False
            if "sprint_id" in request.data:
                sprint_changed = True
                new_sprint_id = request.data.get("sprint_id")
                if new_sprint_id in (None, "null", ""):
                    milestone.sprint = None
                else:
                    try:
                        target = Sprint.objects.get(
                            sprint_id=new_sprint_id,
                            project_id=milestone.project_id,
                            is_deleted=False,
                        )
                    except Sprint.DoesNotExist:
                        return Response(
                            {"error": "Target sprint not found in this project."},
                            status=status.HTTP_400_BAD_REQUEST,
                        )
                    milestone.sprint = target

            for field in (
                "title",
                "description",
                "status",
                "status_code",
                "priority",
                "priority_code",
                "effort_level",
                "effort_level_code",
                "reporter_id",
                "tags",
                "links",
            ):
                if field in request.data:
                    setattr(milestone, field, request.data.get(field))

            if "start_date" in request.data:
                milestone.start_date = _parse_iso_date(request.data.get("start_date"))

            if "due_date" in request.data:
                milestone.due_date = _parse_iso_date(request.data.get("due_date"))

            milestone.save()
            _sync_backing_task(milestone)

            # Custom field values are stored on the backing task only —
            # written AFTER _sync_backing_task so this save is never
            # overwritten by the mirror pass (which doesn't touch the
            # column). Replace-whole-map semantics, same as the task PUT.
            # (Validated before the transaction opened.)
            if custom_values_in_request:
                backing = _ensure_backing_task(milestone)
                backing.custom_field_values = cleaned_custom_values
                backing.save(update_fields=["custom_field_values", "ts_updated_at"])

            # Tasks linked to this milestone inherit the milestone's
            # sprint by convention (the frontend doesn't expose a
            # direct sprint picker on tasks). When the milestone moves
            # between sprints, push the new sprint to every task that
            # cites it so sprint board / sprint analytics stay in sync.
            # `_sync_backing_task` already handles the backing row;
            # this catches the rest. Scoped to the sprint-changed path
            # so a no-op patch (e.g. status flip) doesn't churn rows.
            if sprint_changed:
                TaskMaster.objects.filter(milestone=milestone).exclude(
                    sprint_id=milestone.sprint_id
                ).update(sprint_id=milestone.sprint_id)

            if "assignee_ids" in request.data:
                self._sync_assignees(milestone, request.data.get("assignee_ids") or [])

        # Re-read with prefetch to render the latest assignee list.
        milestone = (
            MilestoneMaster.objects.prefetch_related("milestone_assignees__user")
            .select_related("task", "reporter")
            .get(milestone_id=milestone.milestone_id)
        )
        # `_sync_backing_task` mirrors most milestone fields onto the
        # backing TaskMaster row, and the sprint-changed branch updates
        # every milestone-linked task. Drop the project-tasks cache so
        # the table reflects the changes on next read.
        invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)

        # Compute @mention delta for the description so the FE can emit
        # `task_body_mention` and Flask can broadcast live activity rows.
        # Milestones share the task-body (surface_type=5) mention path: the
        # backing TaskMaster is the authoritative task row, so we use its
        # `task_id` and `display_id`. We compute the delta from the backing
        # task's previously stored `mentioned_user_ids` vs the new body —
        # avoiding a separate `mentioned_user_ids` column on MilestoneMaster.
        newly_mentioned: list[str] = []
        all_mentioned: list[str] = []
        removed_mentioned: list[str] = []
        if "description" in request.data and milestone.task_id is not None:
            new_body = request.data.get("description") or []
            new_set: set[str] = set(mention_extractor.extract_mentioned_user_ids(new_body))
            grp_ids = mention_extractor.extract_mention_group_ids(new_body)
            if grp_ids:
                new_set |= resolve_group_members(grp_ids)
            backing = milestone.task
            prev_set: set[str] = set(backing.mentioned_user_ids or []) if backing else set()
            newly_mentioned = list(new_set - prev_set)
            removed_mentioned = list(prev_set - new_set)
            all_mentioned = list(new_set)
            # Mirror the new mention set onto the backing task so future edits
            # compute the correct delta (matching the task PUT path).
            if backing is not None and new_set != prev_set:
                backing.mentioned_user_ids = list(new_set)
                backing.save(update_fields=["mentioned_user_ids", "ts_updated_at"])

        response_data = {"milestone": _serialize_milestone(milestone)}
        if newly_mentioned or removed_mentioned:
            response_data["newly_mentioned_user_ids"] = newly_mentioned
            response_data["all_mentioned_user_ids"] = all_mentioned
            response_data["removed_user_ids"] = removed_mentioned
        return Response(response_data, status=status.HTTP_200_OK)

    def delete(self, request, milestone_id: int):
        try:
            milestone = MilestoneMaster.objects.get(milestone_id=milestone_id)
        except MilestoneMaster.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)
        milestone.is_deleted = True
        milestone.save(update_fields=["is_deleted", "ts_updated_at"])
        # Soft-delete the backing task too so the milestone disappears
        # from the project's task table on the next reload.
        if milestone.task_id is not None:
            TaskMaster.objects.filter(task_id=milestone.task_id).update(
                is_deleted=True, status="Deleted"
            )
        # Detach child tasks so they remain visible in the project view
        # rather than disappearing alongside the milestone. Children
        # tracked via parent_task_id are also unparented.
        TaskMaster.objects.filter(milestone=milestone).update(milestone=None)
        if milestone.task_id is not None:
            TaskMaster.objects.filter(parent_task_id=milestone.task_id).update(parent_task_id=None)
        # Backing task got soft-deleted + every child detached → next
        # project-tasks read needs a fresh DB pull.
        invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
        # Best-effort: drop the milestone's chunks (and its soft-deleted
        # backing task's) from OpenSearch — chunkers skip `is_deleted`
        # rows, so nothing else cleans these up until the orphan sweep.
        purge_milestone(milestone_id)
        if milestone.task_id is not None:
            purge_task(milestone.task_id)
        return Response(
            {"message": "Milestone soft-deleted."},
            status=status.HTTP_200_OK,
        )

    def _sync_assignees(self, milestone: MilestoneMaster, assignee_ids):
        sync_milestone_assignees(milestone, assignee_ids)


class MilestoneAssigneesView(AuthenticatedAPIView):
    """Add or remove a single assignee on a milestone."""

    def post(self, request, milestone_id: int):
        user_id = request.data.get("user_id")
        if res := validate_request_data({"user_id": user_id}):
            return res

        try:
            milestone = MilestoneMaster.objects.get(milestone_id=milestone_id, is_deleted=False)
        except MilestoneMaster.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            user = CustomUser.objects.get(id=user_id)
        except CustomUser.DoesNotExist:
            return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)

        MilestoneAssignees.objects.get_or_create(
            milestone=milestone,
            user=user,
            defaults={"team": milestone.team},
        )
        # Mirror the picked assignee onto the backing task so the
        # project task table reflects the change without a reload.
        _sync_backing_task(milestone)
        invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
        milestone = (
            MilestoneMaster.objects.prefetch_related("milestone_assignees__user")
            .select_related("task", "reporter")
            .get(milestone_id=milestone_id)
        )
        return Response(
            {"milestone": _serialize_milestone(milestone)},
            status=status.HTTP_200_OK,
        )

    def delete(self, request, milestone_id: int, user_id):
        try:
            milestone = MilestoneMaster.objects.get(milestone_id=milestone_id, is_deleted=False)
        except MilestoneMaster.DoesNotExist:
            return Response({"error": "Milestone not found."}, status=status.HTTP_404_NOT_FOUND)
        MilestoneAssignees.objects.filter(milestone=milestone, user_id=user_id).delete()
        # Backing task assignee may need to fall back to the reporter
        # when the removed user was the previously synced assignee.
        _sync_backing_task(milestone)
        invalidate_project_tasks_cache(milestone.team_id, milestone.project_id)
        milestone = (
            MilestoneMaster.objects.prefetch_related("milestone_assignees__user")
            .select_related("task", "reporter")
            .get(milestone_id=milestone_id)
        )
        return Response(
            {"milestone": _serialize_milestone(milestone)},
            status=status.HTTP_200_OK,
        )
