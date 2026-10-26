from collections import defaultdict
from datetime import date, datetime, timedelta

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import can_access_task

# Mirrors `milestone_views.CLOSED_STATUSES` — kept duplicated here on
# purpose so the burndown view doesn't import from the milestone view
# (and risk a circular import as the modules grow).
_CLOSED_STATUSES = {"Closed", "Deleted"}


# Tasks generate audit rows liberally — clamp the default page so we
# never return an unbounded list. The frontend can request more via the
# `limit` / `offset` query params if needed.
DEFAULT_LIMIT = 100
MAX_LIMIT = 500

# A run of `description_edited` rows by the same actor whose adjacent
# timestamps fall within this window collapses into a single row in the
# response (see `_collapse_description_edits`). The body editor
# auto-saves every few seconds, so without this a 5-minute edit session
# floods the Activity tab with near-duplicates.
DESCRIPTION_EDIT_GROUP_WINDOW = timedelta(minutes=15)

# Defensive cap on the pre-collapse fetch. Typical tasks fall well
# under this; collapse-then-paginate beyond it is degraded by design
# (callers asking for `offset` past the cap will see an empty page).
MAX_FETCH = 2000


def _serialize_actor(user) -> dict | None:
    if user is None:
        return None
    return {
        "userId": getattr(user, "id", None),
        "userName": getattr(user, "username", None),
        # `profile_image_file_name` matches the casing used by the
        # other task endpoints (see GetTaskView) so the existing
        # `AvatarWithStatus` component path resolution Just Works.
        "avatarImgPath": getattr(user, "profile_image_file_name", None),
    }


def _serialize_activity(row: TaskActivity) -> dict:
    # A description row from an agent or API rewrite keeps the previous BODY in
    # `old_value` so it can be restored (MIP-738). That is a whole document,
    # and the feed has no use for it: it is fetched by the restore flow only.
    # Sent here it would make every feed response carry old bodies, and hand a
    # replaced description to anyone who can read the history. The row says a
    # body is available (`metadata.previousBodyKept`) and nothing more.
    is_body = row.action_type == TaskActivityActionType.DESCRIPTION
    return {
        "activityId": row.activity_id,
        "actionType": row.action_type,
        "fieldName": row.field_name,
        "oldValue": None if is_body else row.old_value,
        "newValue": row.new_value,
        "metadata": row.metadata or {},
        "actor": _serialize_actor(row.actor),
        "tsCreatedAt": row.ts_created_at.isoformat() if row.ts_created_at else None,
    }


def _collapse_description_edits(rows: list[TaskActivity]) -> list[TaskActivity]:
    """Merge consecutive `description_edited` rows by the same actor that
    sit within `DESCRIPTION_EDIT_GROUP_WINDOW` of each other into a
    single anchor row (the **newest** one in the run, since `rows` is
    newest-first).

    The anchor row's `metadata` is mutated in-memory with two extra
    keys for the frontend to optionally surface later — we never
    `.save()` the row so the audit table stays untouched:
      - `grouped_count`: int (>= 2 only when collapse actually happened)
      - `grouped_first_ts`: ISO timestamp of the *oldest* edit in the
        run (handy for "edited 4 times between X and Y" UIs)

    Non-description rows always pass through verbatim, and any change
    in actor / action / a >window gap closes the active run.

    A description row that **kept the previous body** (`previousBodyKept`, an
    agent or API-key rewrite, MIP-738) is never folded into a run and never
    has one folded into it. It is the one place a restore is offered, and the
    usual sequence is a rewrite followed by the user's own autosave within the
    window; folding by "newest wins" would leave only the autosave row, which
    kept nothing, and the restore would vanish exactly when it is needed.
    """
    desc = TaskActivityActionType.DESCRIPTION
    out: list[TaskActivity] = []

    # State for the currently-open run. The anchor (newest row of the
    # run) lives at `out[-1]`; we never re-walk the list to find it.
    run_count = 0
    run_actor_id: int | None = None
    run_first_ts = None  # oldest ts seen in the run so far
    run_prev_ts = None  # ts of the most recent row added to the run

    def _stamp_anchor() -> None:
        if run_count > 1 and out:
            anchor = out[-1]
            anchor.metadata = {
                **(anchor.metadata or {}),
                "grouped_count": run_count,
                "grouped_first_ts": run_first_ts.isoformat() if run_first_ts else None,
            }

    for r in rows:
        is_desc = r.action_type == desc
        keeps_body = is_desc and bool((r.metadata or {}).get("previousBodyKept"))
        if keeps_body:
            # Closes the open run, stands alone, and starts none, so the
            # edits on either side of it cannot fold into it.
            _stamp_anchor()
            out.append(r)
            run_count = 0
            run_actor_id = None
            run_first_ts = None
            run_prev_ts = None
            continue
        # Tuples compare equal even when both actor_ids are None, so a
        # run of anonymous edits (rare but possible — actor FK is
        # SET_NULL) merges sensibly without merging into a named-actor
        # run.
        same_actor = is_desc and r.actor_id == run_actor_id
        within_window = (
            run_prev_ts is not None
            and (run_prev_ts - r.ts_created_at) <= DESCRIPTION_EDIT_GROUP_WINDOW
        )
        if is_desc and run_count > 0 and same_actor and within_window:
            run_count += 1
            run_first_ts = r.ts_created_at
            run_prev_ts = r.ts_created_at
            continue

        # New row breaks any open run — finalise the anchor before
        # appending.
        _stamp_anchor()
        out.append(r)
        if is_desc:
            run_count = 1
            run_actor_id = r.actor_id
            run_first_ts = r.ts_created_at
            run_prev_ts = r.ts_created_at
        else:
            run_count = 0
            run_actor_id = None
            run_first_ts = None
            run_prev_ts = None

    # Finalise the trailing run (loop exited mid-run).
    _stamp_anchor()
    return out


class TaskActivityListView(AuthenticatedAPIView):
    """`GET /api/v2/task/activity/?team_id=&task_id=&limit=&offset=`

    Returns the audit log for a task in **reverse chronological order**
    (newest first). Backs the "Activity" tab in TaskTabBlock and can be
    used by the chat thread's Activities tab if we ever swap the
    PM-message feed for the structured log.

    Consecutive `description_edited` rows by the same actor whose
    adjacent timestamps fall within `DESCRIPTION_EDIT_GROUP_WINDOW`
    (15 minutes) are merged into a single row — the latest edit wins
    and gains `metadata.grouped_count` + `metadata.grouped_first_ts`.
    The body editor auto-saves every few seconds, so without this an
    edit session would flood the feed; the audit table itself still
    keeps every row.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        raw_task_id = request.GET.get("task_id")
        if not team_id or not raw_task_id:
            return Response(
                {"error": "team_id and task_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            task_id = int(raw_task_id)
        except ValueError:
            return Response(
                {"error": "task_id must be an integer."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # The audit log is the task's full edit history — old and new
        # values of every field, with the actor on each row. It was
        # reachable with nothing but a walkable integer `task_id` and a
        # `team_id` the caller made up.
        if not can_access_task(task_id, request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        try:
            limit = int(request.GET.get("limit") or DEFAULT_LIMIT)
            offset = int(request.GET.get("offset") or 0)
        except ValueError:
            return Response(
                {"error": "limit / offset must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        limit = max(1, min(limit, MAX_LIMIT))
        offset = max(0, offset)

        # team_id scopes the read to the requesting user's team — the
        # FK is nullable (legacy rows + cross-project edge cases) so
        # don't filter rows whose team is null; just exclude
        # other-team rows.
        #
        # Collapsing happens in Python after the fetch (see the helper
        # docstring). To keep `limit` meaning "up to N rows in the
        # response" we need to pull more than `limit` raw rows in case
        # several collapse into one. `MAX_FETCH` is a defensive ceiling
        # — large enough to swallow any realistic task's full history,
        # small enough that the in-memory walk stays cheap.
        raw_rows = list(
            TaskActivity.objects.filter(task_id=task_id)
            .filter(Q(team_id=team_id) | Q(team__isnull=True))
            .select_related("actor")
            .order_by("-ts_created_at", "-activity_id")[:MAX_FETCH]
        )
        collapsed = _collapse_description_edits(raw_rows)
        page = collapsed[offset : offset + limit]

        return Response([_serialize_activity(r) for r in page], status=status.HTTP_200_OK)


def _parse_iso_day(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class MilestoneBurndownView(AuthenticatedAPIView):
    """`GET /api/v2/task/burndown/?task_ids=1,2,3&start=YYYY-MM-DD&end=YYYY-MM-DD`

    Returns a daily remaining-task series for the given task set across
    the requested window. Powers the burndown sparkline on the diagram
    modal.

    Algorithm: for each currently-closed task, find the most recent
    `status_changed` activity whose `new_value` is in CLOSED_STATUSES —
    that timestamp is the task's effective close date. Tasks closed
    *before* the window are counted as "closed at start"; tasks closed
    inside the window subtract from `remaining` on their close date.
    Tasks currently open never decrement the series, even if they
    bounced through a temporary Closed state during the window (since
    they're not "burned down" today).

    Days where nothing happens carry forward the previous day's count,
    so the series is dense — `data.length === end_day - start + 1`.
    """

    MAX_TASK_IDS = 500

    def get(self, request):
        raw_ids = request.GET.get("task_ids") or ""
        raw_start = request.GET.get("start")
        raw_end = request.GET.get("end")

        try:
            task_ids = sorted({int(p) for p in raw_ids.split(",") if p.strip()})
        except ValueError:
            return Response(
                {"error": "task_ids must be a comma-separated list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if not task_ids:
            return Response({"burndown": [], "total": 0}, status=status.HTTP_200_OK)
        if len(task_ids) > self.MAX_TASK_IDS:
            return Response(
                {"error": f"Too many task_ids (max {self.MAX_TASK_IDS})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_day = _parse_iso_day(raw_start)
        end_day = _parse_iso_day(raw_end)
        if start_day is None or end_day is None:
            return Response(
                {"error": "start and end must be YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if end_day < start_day:
            return Response(
                {"error": "end must be on or after start."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        today = date.today()
        # Cap at today — the series can't show points beyond now,
        # they'd just be flat extrapolations of the current state.
        last_day = min(end_day, today)
        if last_day < start_day:
            return Response({"burndown": [], "total": 0}, status=status.HTTP_200_OK)

        # Walkable-id batch, same shape as TaskDependencyBatchListView
        # (closed in #255). Narrow to the ids the caller can actually
        # reach rather than refusing the whole call, so a chart
        # containing one stray id still renders the rest.
        task_ids = [t for t in task_ids if can_access_task(t, request.user.id)]

        tasks = list(
            TaskMaster.objects.filter(task_id__in=task_ids).values_list("task_id", "status")
        )
        total = len(tasks)
        closed_now_ids = [tid for tid, s in tasks if s in _CLOSED_STATUSES]

        # Most-recent close activity per currently-closed task. Ordered
        # by `-ts_created_at` so the first row we see for each task IS
        # its most recent transition into a closed state. Walking in one
        # pass + a `seen` set is O(n) over the candidate activities.
        close_dates: dict[int, date] = {}
        if closed_now_ids:
            acts = (
                TaskActivity.objects.filter(
                    task_id__in=closed_now_ids,
                    action_type=TaskActivityActionType.STATUS,
                    new_value__in=list(_CLOSED_STATUSES),
                )
                .order_by("task_id", "-ts_created_at")
                .values_list("task_id", "ts_created_at")
            )
            for tid, ts in acts:
                if tid in close_dates:
                    continue
                close_dates[tid] = ts.date()

        closed_before_start = 0
        closes_by_day: dict[date, int] = defaultdict(int)
        for tid in closed_now_ids:
            cd = close_dates.get(tid)
            if cd is None or cd < start_day:
                # Either no recorded close activity (task created
                # closed, or pre-signal era) or closed before the
                # window — count toward the baseline.
                closed_before_start += 1
            elif cd > last_day:
                # Defensive: shouldn't happen since `last_day <= today`
                # and the close already occurred, but guard anyway.
                pass
            else:
                closes_by_day[cd] += 1

        series: list[dict[str, object]] = []
        cur_closed = closed_before_start
        cursor = start_day
        while cursor <= last_day:
            cur_closed += closes_by_day.get(cursor, 0)
            series.append(
                {
                    "date": cursor.isoformat(),
                    "remaining": max(0, total - cur_closed),
                }
            )
            cursor += timedelta(days=1)

        return Response({"burndown": series, "total": total}, status=status.HTTP_200_OK)


# The task status that means "work has actually started". `started` in
# the velocity series counts tasks that transitioned INTO this status in
# a bucket. If more in-progress statuses are added later (see
# blocked-task-status rollout), widen this into a set and switch the
# `new_value ==` check below to a membership test.
_WIP_STATUS = "WIP"


class TaskVelocityView(AuthenticatedAPIView):
    """`GET /api/v2/task/velocity/?team_id=&task_ids=1,2,3&start=YYYY-MM-DD&end=YYYY-MM-DD&granularity=day|week`

    Returns a dense per-bucket flow series over the given task set — how
    many tasks were **created / started / closed / updated** in each day
    (or ISO-week) of the window. Powers the "Velocity" section on the
    task dashboard's Sprint Insights + My Tasks tabs, so PMs can see how
    fast a sprint is moving and spot the days nothing advanced.

    Counts are **distinct tasks per bucket** (a task edited ten times in
    a day counts once toward `updated`), so a chatty task can't dominate
    the bars. The series intentionally overlap: a task closed on a day is
    counted under both `closed` AND `updated`.

    Category rules (all keyed off the `TaskActivity` audit log):
      - created: a `CREATED` row
      - started: a `STATUS` row whose `new_value` is `_WIP_STATUS`
      - closed:  a `CLOSED` row, OR a `STATUS` row whose `new_value` is a
                 closed status (mirrors the burndown close detection)
      - updated: ANY audit row (the task was touched at all)

    Read-only over existing rows — no model change. `granularity`
    defaults to `day`; `week` buckets by the activity date's ISO Monday.
    Like burndown, `end` is clamped to today and the series is dense
    (every bucket in range, zero-filled), so the frontend chart can map
    straight over it.
    """

    MAX_TASK_IDS = 500

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response(
                {"error": "team_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_ids = request.GET.get("task_ids") or ""
        try:
            task_ids = sorted({int(p) for p in raw_ids.split(",") if p.strip()})
        except ValueError:
            return Response(
                {"error": "task_ids must be a comma-separated list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        granularity = (request.GET.get("granularity") or "day").lower()
        if granularity not in ("day", "week"):
            return Response(
                {"error": "granularity must be 'day' or 'week'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not task_ids:
            return Response(
                {"velocity": [], "granularity": granularity},
                status=status.HTTP_200_OK,
            )
        if len(task_ids) > self.MAX_TASK_IDS:
            return Response(
                {"error": f"Too many task_ids (max {self.MAX_TASK_IDS})."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        start_day = _parse_iso_day(request.GET.get("start"))
        end_day = _parse_iso_day(request.GET.get("end"))
        if start_day is None or end_day is None:
            return Response(
                {"error": "start and end must be YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if end_day < start_day:
            return Response(
                {"error": "end must be on or after start."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Clamp at today — future buckets are always empty.
        last_day = min(end_day, date.today())
        if last_day < start_day:
            return Response(
                {"velocity": [], "granularity": granularity},
                status=status.HTTP_200_OK,
            )

        def bucket_of(d: date) -> date:
            # Day: the date itself. Week: that date's ISO Monday, so a
            # bucket key is stable regardless of which weekday the
            # activity fell on.
            return d if granularity == "day" else d - timedelta(days=d.weekday())

        # One scan. Team-scoped like `TaskActivityListView` (the FK is
        # nullable for legacy rows, so keep null-team rows in). The
        # `taskact_task_created_idx (task, -ts_created_at)` index covers
        # the task_id__in + ts filter. `new_value` is JSON but status
        # diffs are stored as plain strings, so equality/membership works.
        # Walkable-id batch, same shape as TaskDependencyBatchListView
        # (closed in #255). Narrow to the ids the caller can actually
        # reach rather than refusing the whole call, so a chart
        # containing one stray id still renders the rest.
        task_ids = [t for t in task_ids if can_access_task(t, request.user.id)]

        rows = (
            TaskActivity.objects.filter(task_id__in=task_ids)
            .filter(Q(team_id=team_id) | Q(team__isnull=True))
            .filter(
                ts_created_at__date__gte=start_day,
                ts_created_at__date__lte=last_day,
            )
            .values_list("task_id", "action_type", "new_value", "ts_created_at")
        )

        # Distinct task_ids per (bucket, category).
        created: dict[date, set[int]] = defaultdict(set)
        started: dict[date, set[int]] = defaultdict(set)
        closed: dict[date, set[int]] = defaultdict(set)
        updated: dict[date, set[int]] = defaultdict(set)
        for tid, action, new_value, ts in rows:
            b = bucket_of(ts.date())
            updated[b].add(tid)
            if action == TaskActivityActionType.CREATED:
                created[b].add(tid)
            elif action == TaskActivityActionType.CLOSED:
                closed[b].add(tid)
            elif action == TaskActivityActionType.STATUS:
                if new_value == _WIP_STATUS:
                    started[b].add(tid)
                elif new_value in _CLOSED_STATUSES:
                    closed[b].add(tid)

        # Dense series: every bucket from start to last_day, zero-filled.
        series: list[dict[str, object]] = []
        step = timedelta(days=1 if granularity == "day" else 7)
        cursor = bucket_of(start_day)
        end_bucket = bucket_of(last_day)
        while cursor <= end_bucket:
            series.append(
                {
                    "date": cursor.isoformat(),
                    "created": len(created.get(cursor, ())),
                    "started": len(started.get(cursor, ())),
                    "closed": len(closed.get(cursor, ())),
                    "updated": len(updated.get(cursor, ())),
                }
            )
            cursor += step

        return Response(
            {"velocity": series, "granularity": granularity},
            status=status.HTTP_200_OK,
        )


class TaskBodyRestoreView(AuthenticatedAPIView):
    """`POST /api/v2/task/activity/restore-body/`  body: `{task_id, activity_id}`

    Put back the description a task had before an agent or API-key rewrite
    (MIP-738). `update_task` with `content_text` replaces the WHOLE body, and
    after it the task held only the new text; the signal now keeps the previous
    body on the `description_edited` row (`old_value`, `previousBodyKept`), and
    this is how it comes back.

    Allowed to anyone who can edit the task (`can_access_task`, which is the
    read and the write audience at once; a role tier is a product decision and
    is not made here).

    The row is looked up BY TASK as well as by id, so an activity id from
    another task, or one the caller cannot see, is a 404 and never a read of
    someone else's body. It must be a description row that kept its body; any
    other row is a 400.

    A restore is itself a body rewrite, and `app` autosave does not keep the
    previous body. So that a mistaken restore can be undone too, the body it
    replaces is recorded explicitly on the row it writes.

    Like the agent's `update_task`, it also pushes the body into the task's live
    Yjs document (MIP-694): the editor renders that, not `content`, so without
    the push a task that was ever opened keeps showing the rewrite.
    """

    def post(self, request):
        raw_task_id = request.data.get("task_id")
        raw_activity_id = request.data.get("activity_id")
        try:
            task_id = int(raw_task_id)
            activity_id = int(raw_activity_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "task_id and activity_id must be integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not can_access_task(task_id, request.user.id):
            return Response({"error": "Task not found."}, status=status.HTTP_404_NOT_FOUND)

        row = TaskActivity.objects.filter(
            activity_id=activity_id,
            task_id=task_id,
            action_type=TaskActivityActionType.DESCRIPTION,
        ).first()
        if row is None:
            return Response({"error": "Activity not found."}, status=status.HTTP_404_NOT_FOUND)
        if not (row.metadata or {}).get("previousBodyKept") or not isinstance(row.old_value, list):
            return Response(
                {"error": "That edit did not keep the previous description."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        task = TaskMaster.objects.get(task_id=task_id)
        previous_body = row.old_value
        replaced_body = task.content

        from origin.search_engine.agent.tools.body_mentions import (  # noqa: PLC0415
            notify_task_body_mentions,
        )
        from origin.services.collab_push import push_task_body  # noqa: PLC0415

        # A restore must be restorable: the signal keeps the previous body only
        # for agent and API-key writes, and this one arrives as the app.
        task.content = previous_body
        task._keep_previous_body = True
        task.save(update_fields=["content"])
        push_task_body(task.task_id, previous_body)
        notify_task_body_mentions(task, actor_id=str(request.user.id))

        return Response(
            {
                "taskId": task.task_id,
                "restoredFrom": row.activity_id,
                "replacedBodyKept": isinstance(replaced_body, list),
            },
            status=status.HTTP_200_OK,
        )
