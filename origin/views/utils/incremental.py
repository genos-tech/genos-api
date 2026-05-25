"""Shared helpers for incremental ("delta") sync endpoints.

The pattern every delta endpoint follows:

    server_time = capture_server_time()        # snapshot BEFORE queries
    since = parse_since(request)               # None = full load
    qs = apply_since_filter(MyModel.objects.all(), since)
    data = serialize(qs)
    return Response(build_delta_response({"items": data}, server_time))

The race-safety invariant: `server_time` is captured before any query
runs, and the client stores exactly that value as its next `since`.
Any write that commits during the query window has commit_time
> server_time, so the next sync will include it. Storing
`max(ts_updated_at)` of returned rows instead would silently lose such
writes.
"""

from datetime import datetime, timedelta
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime

# Catastrophic-delta cap. If the client's checkpoint is older than this,
# the delta query would scan a huge window — we re-run as a full load
# and signal the client (via `force_full_reload`) to clear its IDB store
# before applying. Keeps both server CPU and client memory bounded.
MAX_DELTA_AGE_DAYS = 60


def parse_since(request) -> Optional[datetime]:
    """Read `?since=ISO_TIMESTAMP` from a request's query string.

    Returns None when absent or unparseable, which the caller should
    treat as "full load" (no `since` filter applied).
    """
    raw = request.GET.get("since")
    if not raw:
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


def capture_server_time() -> datetime:
    """Snapshot the 'as-of' time before running delta queries. The caller
    stores this on the response and the client persists it as the next
    sync's `since` value."""
    return timezone.now()


def apply_since_filter(
    queryset: QuerySet,
    since: Optional[datetime],
    ts_field: str = "ts_updated_at",
    deleted_field: str = "is_deleted",
) -> QuerySet:
    """Filter a queryset for incremental sync.

    - Full load (`since=None`): exclude deleted rows.
    - Incremental (`since=<ts>`): return rows where ts_field > since,
      INCLUDING rows whose `deleted_field` is True so the client can
      apply tombstones.

    Pass `deleted_field=None` for tables without a soft-delete column
    (e.g. activity events that are conceptually append-only).
    """
    if since is None:
        if deleted_field is not None:
            return queryset.filter(**{deleted_field: False})
        return queryset
    return queryset.filter(**{f"{ts_field}__gt": since})


def check_since(request) -> tuple:
    """One-stop helper for a delta view: parse `?since=` and decide
    whether to force a full load.

    Returns `(since, force_full_reload)`:
      - `(<datetime>, False)`: normal incremental sync — caller filters
        by `since`.
      - `(None, False)`: no checkpoint provided — caller does a full load.
      - `(None, True)`: checkpoint exists but is too old (catastrophic
        delta cap). Caller should run the full-load query AND set
        `force_full_reload=True` on the response so the client clears
        its IDB store before applying.
    """
    since = parse_since(request)
    if is_since_too_old(since):
        return None, True
    return since, False


def is_since_too_old(since: Optional[datetime]) -> bool:
    """True when the requested checkpoint is older than the catastrophic-
    delta threshold. The view should respond as if `since` were None
    (full load) AND set `force_full_reload=True` on the response so the
    client clears its IDB store before applying the payload."""
    if since is None:
        return False
    return since < timezone.now() - timedelta(days=MAX_DELTA_AGE_DAYS)


def build_delta_response(
    data: dict,
    server_time: datetime,
    force_full_reload: bool = False,
) -> dict:
    """Wrap an endpoint payload in the standard delta envelope:
    `{server_time: ISO, data: {...}}`. When `force_full_reload=True`,
    adds a top-level flag that tells the client to treat this response
    as a full load (clear-before-insert) even though it sent a `since`
    value. Used for the catastrophic-delta fallback."""
    response = {
        "server_time": server_time.isoformat(),
        "data": data,
    }
    if force_full_reload:
        response["force_full_reload"] = True
    return response
