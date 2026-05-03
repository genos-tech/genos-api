from datetime import timedelta

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.views.common.base_auth_api_view import AuthenticatedAPIView


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
    return {
        "activityId": row.activity_id,
        "actionType": row.action_type,
        "fieldName": row.field_name,
        "oldValue": row.old_value,
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
