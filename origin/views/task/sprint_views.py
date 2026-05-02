from datetime import date, datetime, timedelta

from django.db import transaction
from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.serializers.task.task_serializers import (
    SprintConfigSerializer,
    SprintSerializer,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.request_validators import (
    validate_request_data,
    validate_request_user,
)


SPRINT_STATUS_UPCOMING = "upcoming"
SPRINT_STATUS_ACTIVE = "active"
SPRINT_STATUS_COMPLETED = "completed"
SPRINT_STATUS_ARCHIVED = "archived"

DEFAULT_SPRINT_DURATION_DAYS = 14
DEFAULT_SPRINT_UPCOMING_HORIZON = 6


def _ensure_default_config(project: ProjectMaster) -> SprintConfig:
    """Return the project's `SprintConfig`, creating a sensible default
    (2-week sprints, anchor today, auto-roll, 6 upcoming) if none exists.

    This is the bootstrap path users hit the very first time they open
    the dashboard or try to assign a milestone to a sprint: rather than
    forcing them to configure cadence before doing anything, we provide
    a default they can tweak later via `SprintConfigDialog`.
    """

    config = SprintConfig.objects.filter(project=project).first()
    if config is not None:
        return config
    config = SprintConfig.objects.create(
        project=project,
        team=project.team,
        duration_days=DEFAULT_SPRINT_DURATION_DAYS,
        anchor_date=date.today(),
        auto_roll=True,
        upcoming_horizon=DEFAULT_SPRINT_UPCOMING_HORIZON,
    )
    return config


def _parse_iso_date(value):
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _derive_status(sprint: Sprint, today: date) -> str:
    """Compute the runtime status of a sprint based on today's date.

    Archived sprints are sticky — they're never automatically promoted
    back. Everything else is recomputed from the date window so a stale
    `status` field never lies to the UI.
    """

    if sprint.status == SPRINT_STATUS_ARCHIVED:
        return SPRINT_STATUS_ARCHIVED
    if sprint.start_date <= today <= sprint.end_date:
        return SPRINT_STATUS_ACTIVE
    if today < sprint.start_date:
        return SPRINT_STATUS_UPCOMING
    return SPRINT_STATUS_COMPLETED


def _year_sequence(sprint: Sprint, all_sprints) -> int:
    """Return the 1-based ordinal of `sprint` among same-year project sprints.

    The display number resets to 1 every January 1st: a sprint whose
    `start_date` falls in year Y is the Nth sprint of that year, where N
    is the count of project sprints (auto + ad-hoc) starting in year Y
    up to and including this one, ordered by `start_date`.
    """

    year = sprint.start_date.year
    same_year = sorted(
        (s for s in all_sprints if not s.is_deleted and s.start_date.year == year),
        key=lambda s: (s.start_date, s.sequence_number),
    )
    for idx, s in enumerate(same_year, start=1):
        if s.sprint_id == sprint.sprint_id:
            return idx
    # Fallback: this can happen for a freshly created sprint passed in
    # before the in-memory `all_sprints` list is rebuilt.
    return 1


def _ensure_upcoming_sprints(project: ProjectMaster, config: SprintConfig) -> None:
    """Materialize enough auto-rolled `Sprint` rows for the project.

    Idempotent: only creates rows that don't exist yet. The horizon
    (`config.upcoming_horizon`) is interpreted as "keep at least N
    upcoming sprints (including the active one) on hand at any moment".
    The cadence anchors on `config.anchor_date` and uses
    `config.duration_days`. Existing sprints (auto or ad-hoc) are never
    rewritten by this function.

    Sprint *display* numbers reset to 1 each calendar year (Jan 1st);
    the underlying `sequence_number` stays globally monotonic per
    project so the unique-together constraint keeps holding.
    """

    if not config.auto_roll:
        return
    if config.duration_days <= 0:
        return

    today = date.today()
    duration = timedelta(days=config.duration_days)

    # Find the "current cycle" by walking from anchor_date forward in
    # `duration` strides until today is inside the window.
    cycle_start = config.anchor_date
    if today < cycle_start:
        cycle_start = config.anchor_date
    else:
        steps = (today - config.anchor_date).days // config.duration_days
        cycle_start = config.anchor_date + timedelta(days=steps * config.duration_days)

    # Determine the next sequence number to assign to any newly created
    # auto sprints. We use the maximum existing sequence_number for the
    # project (auto + ad-hoc) and bump from there to keep ordering.
    existing_sprints = list(
        Sprint.objects.filter(project=project, is_deleted=False).order_by("start_date")
    )
    max_seq = 0
    occupied_starts = set()
    sprints_per_year: dict = {}
    for s in existing_sprints:
        if s.sequence_number > max_seq:
            max_seq = s.sequence_number
        occupied_starts.add(s.start_date)
        sprints_per_year[s.start_date.year] = sprints_per_year.get(s.start_date.year, 0) + 1

    horizon = max(1, config.upcoming_horizon)
    cursor = cycle_start
    created = 0
    target_count = horizon
    sprints_in_window = sum(1 for s in existing_sprints if s.start_date >= cycle_start)
    if sprints_in_window >= target_count:
        return

    while created + sprints_in_window < target_count:
        if cursor not in occupied_starts:
            max_seq += 1
            end = cursor + duration - timedelta(days=1)
            year = cursor.year
            sprints_per_year[year] = sprints_per_year.get(year, 0) + 1
            display_n = sprints_per_year[year]
            Sprint.objects.create(
                project=project,
                team=project.team,
                name=f"Sprint {display_n} ({year})",
                sequence_number=max_seq,
                start_date=cursor,
                end_date=end,
                status=SPRINT_STATUS_ACTIVE if cursor <= today <= end else SPRINT_STATUS_UPCOMING,
                is_auto_generated=True,
            )
            created += 1
        cursor = cursor + duration


def _serialize_sprint(sprint: Sprint, today: date, all_sprints=None) -> dict:
    return {
        "sprintId": sprint.sprint_id,
        "projectId": sprint.project_id,
        "teamId": sprint.team_id,
        "name": sprint.name,
        "sequenceNumber": sprint.sequence_number,
        "yearSequence": (_year_sequence(sprint, all_sprints) if all_sprints is not None else None),
        "startYear": sprint.start_date.year if sprint.start_date else None,
        "startDate": sprint.start_date.isoformat() if sprint.start_date else None,
        "endDate": sprint.end_date.isoformat() if sprint.end_date else None,
        "status": _derive_status(sprint, today),
        "isAutoGenerated": sprint.is_auto_generated,
        "isDeleted": sprint.is_deleted,
        "tsCreatedAt": sprint.ts_created_at,
        "tsUpdatedAt": sprint.ts_updated_at,
    }


class SprintConfigView(AuthenticatedAPIView):
    """Read or upsert the sprint cadence config for a single project."""

    def get(self, request):
        project_id = request.GET.get("project_id")
        if res := validate_request_data({"project_id": project_id}):
            return res
        try:
            project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        # Bootstrap a default config on first read so the config dialog
        # always lands on sane prefilled values (2-week cadence,
        # anchored today) instead of an empty form.
        config = _ensure_default_config(project)
        return Response(
            {
                "config": {
                    "projectId": config.project_id,
                    "teamId": config.team_id,
                    "durationDays": config.duration_days,
                    "anchorDate": config.anchor_date.isoformat(),
                    "autoRoll": config.auto_roll,
                    "upcomingHorizon": config.upcoming_horizon,
                }
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        data = {
            "project_id": request.data.get("project_id"),
            "duration_days": request.data.get("duration_days"),
            "anchor_date": request.data.get("anchor_date"),
        }
        if res := validate_request_data(data):
            return res

        anchor = _parse_iso_date(data["anchor_date"])
        if anchor is None:
            return Response(
                {"error": "anchor_date must be an ISO date (YYYY-MM-DD)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project = ProjectMaster.objects.get(project_id=data["project_id"], is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        auto_roll = request.data.get("auto_roll", True)
        upcoming_horizon = request.data.get("upcoming_horizon", 6)

        config, _created = SprintConfig.objects.update_or_create(
            project=project,
            defaults={
                "team": project.team,
                "duration_days": int(data["duration_days"]),
                "anchor_date": anchor,
                "auto_roll": bool(auto_roll),
                "upcoming_horizon": int(upcoming_horizon),
            },
        )

        _ensure_upcoming_sprints(project, config)

        return Response(
            {
                "config": {
                    "projectId": config.project_id,
                    "teamId": config.team_id,
                    "durationDays": config.duration_days,
                    "anchorDate": config.anchor_date.isoformat(),
                    "autoRoll": config.auto_roll,
                    "upcomingHorizon": config.upcoming_horizon,
                }
            },
            status=status.HTTP_200_OK,
        )


class ProjectSprintsView(AuthenticatedAPIView):
    """List sprints for a project, auto-generating missing upcoming ones."""

    def get(self, request):
        project_id = request.GET.get("project_id")
        if res := validate_request_data({"project_id": project_id}):
            return res

        try:
            project = ProjectMaster.objects.get(project_id=project_id, is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        # Bootstrap a default 2-week cadence so a brand-new project
        # doesn't return an empty sprint list and trap the user into
        # opening the config dialog before anything is usable.
        config = _ensure_default_config(project)
        _ensure_upcoming_sprints(project, config)

        statuses_param = request.GET.get("statuses")
        include_past = request.GET.get("include_past", "true").lower() != "false"
        from_date = _parse_iso_date(request.GET.get("from"))

        qs = Sprint.objects.filter(project=project, is_deleted=False).order_by("start_date")
        today = date.today()

        if not include_past:
            qs = qs.filter(end_date__gte=today)
        if from_date is not None:
            qs = qs.filter(start_date__gte=from_date)

        all_sprints = list(qs)
        sprints = [_serialize_sprint(s, today, all_sprints) for s in all_sprints]
        if statuses_param:
            wanted = {s.strip() for s in statuses_param.split(",") if s.strip()}
            sprints = [s for s in sprints if s["status"] in wanted]

        return Response(
            {"sprints": sprints, "today": today.isoformat()},
            status=status.HTTP_200_OK,
        )


class SprintView(AuthenticatedAPIView):
    """Create / patch / soft-delete a single sprint."""

    def post(self, request):
        data = {
            "project_id": request.data.get("project_id"),
            "name": request.data.get("name"),
            "start_date": request.data.get("start_date"),
            "end_date": request.data.get("end_date"),
        }
        if res := validate_request_data(data):
            return res

        start = _parse_iso_date(data["start_date"])
        end = _parse_iso_date(data["end_date"])
        if start is None or end is None or end < start:
            return Response(
                {"error": "start_date / end_date must be valid ISO dates with end >= start."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            project = ProjectMaster.objects.get(project_id=data["project_id"], is_deleted=False)
        except ProjectMaster.DoesNotExist:
            return Response({"error": "Project not found."}, status=status.HTTP_404_NOT_FOUND)

        # Reject overlap with existing non-deleted sprints.
        overlapping = Sprint.objects.filter(project=project, is_deleted=False).filter(
            Q(start_date__lte=end) & Q(end_date__gte=start)
        )
        if overlapping.exists():
            return Response(
                {"error": "Sprint window overlaps an existing sprint."},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            max_seq = (
                Sprint.objects.filter(project=project)
                .order_by("-sequence_number")
                .values_list("sequence_number", flat=True)
                .first()
                or 0
            )
            sprint = Sprint.objects.create(
                project=project,
                team=project.team,
                name=data["name"],
                sequence_number=max_seq + 1,
                start_date=start,
                end_date=end,
                status=SPRINT_STATUS_UPCOMING,
                is_auto_generated=False,
            )

        return Response(
            {"sprint": _serialize_sprint(sprint, date.today())},
            status=status.HTTP_201_CREATED,
        )

    def patch(self, request, sprint_id: int):
        try:
            sprint = Sprint.objects.get(sprint_id=sprint_id, is_deleted=False)
        except Sprint.DoesNotExist:
            return Response({"error": "Sprint not found."}, status=status.HTTP_404_NOT_FOUND)

        new_name = request.data.get("name")
        new_start = _parse_iso_date(request.data.get("start_date"))
        new_end = _parse_iso_date(request.data.get("end_date"))
        new_status = request.data.get("status")

        if new_name is not None:
            sprint.name = new_name
        if new_start is not None:
            sprint.start_date = new_start
        if new_end is not None:
            sprint.end_date = new_end
        if sprint.end_date < sprint.start_date:
            return Response(
                {"error": "end_date must be >= start_date."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if new_status in {
            SPRINT_STATUS_UPCOMING,
            SPRINT_STATUS_ACTIVE,
            SPRINT_STATUS_COMPLETED,
            SPRINT_STATUS_ARCHIVED,
        }:
            sprint.status = new_status

        # Overlap guard against other live sprints in the same project.
        overlap = (
            Sprint.objects.filter(project_id=sprint.project_id, is_deleted=False)
            .exclude(sprint_id=sprint.sprint_id)
            .filter(Q(start_date__lte=sprint.end_date) & Q(end_date__gte=sprint.start_date))
        )
        if overlap.exists():
            return Response(
                {"error": "Updated window overlaps another sprint."},
                status=status.HTTP_409_CONFLICT,
            )

        sprint.save()
        return Response(
            {"sprint": _serialize_sprint(sprint, date.today())},
            status=status.HTTP_200_OK,
        )

    def delete(self, request, sprint_id: int):
        try:
            sprint = Sprint.objects.get(sprint_id=sprint_id)
        except Sprint.DoesNotExist:
            return Response({"error": "Sprint not found."}, status=status.HTTP_404_NOT_FOUND)
        sprint.is_deleted = True
        sprint.save(update_fields=["is_deleted", "ts_updated_at"])
        # Detach milestones from a deleted sprint so they fall back into
        # the "No sprint / Backlog" bucket and the frontend doesn't try
        # to render them inside a now-invisible sprint.
        MilestoneMaster.objects.filter(sprint=sprint).update(sprint=None)
        return Response(
            {"message": "Sprint soft-deleted."},
            status=status.HTTP_200_OK,
        )
