from datetime import date, datetime

from django.db import transaction
from django.db.models import Count, Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.services.task_cache import invalidate_project_tasks_cache
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import validate_request_data

CLOSED_STATUSES = {"Closed", "Deleted"}


def _parse_iso_date(value):
    """Coerce ISO-string / date / datetime / None into `date` or `None`.

    Django's `DateField` normally accepts ISO strings on save, but the
    in-memory instance returned from `objects.create(...)` keeps whatever
    Python value was passed in. That's fine for the database round-trip
    but trips the serializer below, which calls `.isoformat()`. Parse on
    the way in so the in-memory value is always a `date`.
    """

    if value in (None, "", "null"):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _format_due_date(value):
    if value in (None, ""):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _ensure_backing_task(milestone: MilestoneMaster) -> TaskMaster:
    """Return the milestone's backing TaskMaster row, creating it if needed.

    A milestone is conceptually a task with `is_milestone=True`. We keep
    metadata (sprint linkage, multi-assignees, etc.) on `MilestoneMaster`
    but reuse all task plumbing (comments, notes, attachments, body,
    sub-tasks via `parent_task_id`) by giving each milestone a backing
    TaskMaster row. Legacy milestones get one created on first access.
    """

    if milestone.task_id is not None:
        # Defensive: keep is_milestone in sync if a backing task exists
        # but somehow lost the flag (data drift / hand edits).
        if not milestone.task.is_milestone:
            milestone.task.is_milestone = True
            milestone.task.save(update_fields=["is_milestone", "ts_updated_at"])
        return milestone.task

    backing = TaskMaster.objects.create(
        team=milestone.team,
        project=milestone.project,
        sprint=milestone.sprint,
        milestone=milestone,
        title=milestone.title or "Untitled milestone",
        content=milestone.description,
        status=milestone.status or "Open",
        status_code=milestone.status_code,
        priority=milestone.priority,
        priority_code=milestone.priority_code,
        effort_level=milestone.effort_level,
        effort_level_code=milestone.effort_level_code,
        due_date=milestone.due_date,
        tags=milestone.tags,
        links=milestone.links,
        reporter_id=milestone.reporter_id,
        assignee_id=milestone.reporter_id,
        is_milestone=True,
        is_init_task=False,
    )
    milestone.task = backing
    milestone.save(update_fields=["task", "ts_updated_at"])
    return backing


def _sync_backing_task(milestone: MilestoneMaster) -> None:
    """Mirror milestone fields onto the backing task so the table view
    (which renders the backing task row) stays in sync with the
    milestone's authoritative values."""

    backing = _ensure_backing_task(milestone)
    backing.title = milestone.title or backing.title
    backing.content = milestone.description
    backing.status = milestone.status or backing.status
    backing.status_code = milestone.status_code
    backing.priority = milestone.priority
    backing.priority_code = milestone.priority_code
    backing.effort_level = milestone.effort_level
    backing.effort_level_code = milestone.effort_level_code
    backing.due_date = milestone.due_date
    backing.tags = milestone.tags
    # Mirror the milestone's external links onto the backing task so
    # callers that read the task row directly (table view, sprint
    # board, dashboards) stay consistent with the milestone preview.
    backing.links = milestone.links
    backing.sprint = milestone.sprint
    backing.milestone = milestone
    backing.is_milestone = True
    backing.is_deleted = milestone.is_deleted
    # Reporter mirrors the milestone's reporter so the table row's
    # reporter chip stays consistent with the milestone preview.
    if milestone.reporter_id is not None:
        backing.reporter_id = milestone.reporter_id
    # Pick a deterministic single assignee from the milestone's multi
    # assignee table (oldest first). This keeps the milestone preview's
    # single-assignee UX in sync with the table row's assignee column.
    # Falls back to the reporter when no explicit assignees are set.
    first_assignee_id = (
        MilestoneAssignees.objects.filter(milestone=milestone)
        .order_by("ts_created_at", "id")
        .values_list("user_id", flat=True)
        .first()
    )
    backing.assignee_id = first_assignee_id or milestone.reporter_id
    backing.save()


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
        "dueDate": _format_due_date(m.due_date),
        "tags": m.tags,
        "links": m.links,
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

        with transaction.atomic():
            milestone = MilestoneMaster.objects.create(
                team=project.team,
                project=project,
                sprint=sprint,
                reporter_id=request.data.get("reporter_id") or request.user.id,
                title=data["title"],
                description=request.data.get("description"),
                status=request.data.get("status") or "Open",
                status_code=request.data.get("status_code"),
                priority=request.data.get("priority"),
                priority_code=request.data.get("priority_code"),
                effort_level=request.data.get("effort_level"),
                effort_level_code=request.data.get("effort_level_code"),
                due_date=_parse_iso_date(request.data.get("due_date")),
                tags=request.data.get("tags"),
                links=request.data.get("links"),
            )
            _ensure_backing_task(milestone)

            assignee_ids = request.data.get("assignee_ids") or []
            self._sync_assignees(milestone, assignee_ids)

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

            if "due_date" in request.data:
                milestone.due_date = _parse_iso_date(request.data.get("due_date"))

            milestone.save()
            _sync_backing_task(milestone)

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
        return Response(
            {"milestone": _serialize_milestone(milestone)},
            status=status.HTTP_200_OK,
        )

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
        return Response(
            {"message": "Milestone soft-deleted."},
            status=status.HTTP_200_OK,
        )

    def _sync_assignees(self, milestone: MilestoneMaster, assignee_ids):
        # `CustomUser.id` is a UUID, so keep ids as strings here. We
        # normalize to `str(...)` so comparisons with `user_id` (which
        # comes back from the ORM as a `UUID`) match consistently.
        normalized = {str(uid) for uid in assignee_ids if uid not in (None, "")}
        existing = {
            str(a.user_id): a for a in MilestoneAssignees.objects.filter(milestone=milestone)
        }
        existing_ids = set(existing.keys())

        to_add = normalized - existing_ids
        to_remove = existing_ids - normalized

        if to_add:
            users = list(CustomUser.objects.filter(id__in=list(to_add)))
            MilestoneAssignees.objects.bulk_create(
                [
                    MilestoneAssignees(
                        milestone=milestone,
                        team=milestone.team,
                        user=u,
                    )
                    for u in users
                ],
                ignore_conflicts=True,
            )
        if to_remove:
            MilestoneAssignees.objects.filter(
                milestone=milestone, user_id__in=list(to_remove)
            ).delete()


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
