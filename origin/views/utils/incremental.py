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

# Row-count cap per delta response. Composes with the 60-day time cap so
# both axes are bounded: if a channel is very active (e.g. a channel
# that posts 100 messages/day for 60 days = 6000 rows), the time cap
# would let it through but the wire/parse cost is unbounded. When this
# trips, we slice to the most recent N messages (by ts_sent_at DESC),
# re-order ascending for wire consistency, and signal `force_full_reload`
# so the client evicts its store before applying — same semantic as the
# time cap, just driven by row count instead of clock skew.
MAX_MESSAGES_PER_DELTA = 500


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


def apply_row_count_cap(
    qs: QuerySet,
    *,
    order_field: str = "ts_sent_at",
    max_n: int = MAX_MESSAGES_PER_DELTA,
) -> tuple:
    """Cap the row count of a delta queryset.

    Returns `(qs, force_full_reload)`:
      - If `qs.count() <= max_n`: returns `(qs, False)` — caller serializes
        the queryset as-is.
      - Otherwise: returns `(<most_recent_max_n_qs>, True)` — caller treats
        the result as a full-reload payload (the client will evict the
        channel's store before applying the slice).

    The slice is the most recent `max_n` rows by `order_field` DESC,
    re-queried via `id__in` then re-ordered ASC so the wire shape matches
    the no-cap case. Re-querying lets the caller re-apply prefetches
    (`select_related` / `prefetch_related`) without losing them.

    Two queries (`count()` then `values_list().slice()` + `filter(id__in)`)
    on indexed columns is acceptable here — this path only runs on
    delta responses that would otherwise be too large to ship, so the
    extra round trips are cheap relative to the response size they
    prevent.

    Caller is responsible for re-applying `select_related` / `prefetch_related`
    after this function returns — the returned queryset is a fresh
    `id__in` query and won't carry over any annotations from the input.
    """
    if qs.count() <= max_n:
        return qs, False
    recent_ids = list(qs.order_by(f"-{order_field}").values_list("id", flat=True)[:max_n])
    capped = qs.model.objects.filter(id__in=recent_ids).order_by(order_field)
    return capped, True


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
