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

from datetime import datetime
from typing import Optional

from django.db.models import QuerySet
from django.utils import timezone
from django.utils.dateparse import parse_datetime


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


def build_delta_response(data: dict, server_time: datetime) -> dict:
    """Wrap an endpoint payload in the standard delta envelope:
    `{server_time: ISO, data: {...}}`."""
    return {
        "server_time": server_time.isoformat(),
        "data": data,
    }
