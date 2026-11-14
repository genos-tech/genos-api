"""Per-day activity per item, for the member it describes (MIP-1084).

See `models/common/item_activity_models.py` for what a row is. This module is the only writer
and the only reader of `MemberItemDay` / `MemberItemDayState`.

**Counted with the rules of lines, not a second set.** A day's events are read by
`item_lines.read_events` (the same sources, the same attachment rules, the same access check
as `get_item_line`) plus the member's own `ACTIVITY_KINDS` rows in `MemberEvent`; `classify`
puts each event in exactly one column. The items a day is read for are the member's own: the
tasks they are assignee, reporter, collaborator or commenter on (open, or updated in the
range) and the tasks they acted on in the range (their history rows, comments, AI-app markers
and thread messages), all passed through `LabelAccess` at build time.

**Who writes.** `fill_forward`, which the briefing calls: it builds the closed local days
since the watermark up to yesterday, at most the last `FILL_DAYS`, in one transaction. No
cron: older history is only built by `rebuild` (the `rebuild_item_activity` command). It never
makes the briefing fail (`briefing_section` swallows an error, logs a WARNING).

**Rebuild = reproduce.** `rebuild(ctx, since_day, until_day)` deletes the member's rows for the
range and writes them again from the sources, in one transaction, so running it twice cannot
double a count and a day-by-day fill equals one range rebuild. The range is clipped to the
ledger window (`member_events.RETENTION`): the member's own `MemberEvent` rows older than
that are gone, so a rebuild there would drop what they contributed. Older days are frozen,
not rewritten.

**The switch.** `member_events_enabled` off: nothing is built or read; the PATCH that turns it
off deletes the rows and the watermark in the same transaction (`purge`). The writer locks
the user row `FOR UPDATE`, re-reads the flag and writes nothing when it is off, so a build
that was already running either wrote before the purge (and is purged) or waits and writes
nothing.

**Reading.** `week` gives the last seven complete days per item, summed on read. Rows whose
task the member cannot open now are dropped before anything is summed or shown. Weekly totals
and the rising / cooling call (`trend`) are never stored.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from origin.models.chat.unified_models import Message
from origin.models.common.item_activity_models import (
    COUNT_FIELDS,
    MemberItemDay,
    MemberItemDayState,
    purge_for_user,
)
from origin.models.common.member_event_models import ACTIVITY_KINDS, MCP_KINDS
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext, wrap_workspace_content
from origin.services import item_lines, member_events
from origin.services.channel_scope import readable_chats_qs
from origin.services.user_time import zone_for_user_id

log = logging.getLogger(__name__)

# Rows older than this are deleted by the writer's own transaction, and never read.
RETENTION = timedelta(days=365)
# The most closed days one fill builds (a long absence is not back-filled by a read).
FILL_DAYS = 7
# The most tasks one build follows; past it the days it builds are marked `capped`.
CANDIDATE_MAX = 400
# The most rows of one source a build reads for its own traces; past it the days are `capped`.
ROW_CAP = 5000
# The briefing shows the busiest items of the week.
WEEK_ITEMS = 5
WEEK_DAYS = 7
# Trend: the average of the last three days against the four before them.
RECENT_DAYS = 3
RISING_RATIO = 1.5
COOLING_RATIO = 0.5

_DONE = ("Closed", "Deleted")


# --------------------------------------------------------------------------- #
# Counting                                                                     #
# --------------------------------------------------------------------------- #


def classify(ev: item_lines.Ev, me: str) -> str | None:
    """The one column an event counts in, or None when it counts in none.

    A history row with no actor is automation (a task closed because its PR merged, a creation
    nobody is credited for): it says nothing about attention and is not counted; a PR event is
    a PR event whoever or whatever did it. A comment or reaction whose author has since left
    is still somebody else's. The member's own AI-app markers are not counted at all: an MCP
    write already left a history row, and a read says nothing was saved."""
    mine = str(ev.actor or "") == me
    if ev.source == "activity":
        if ev.what == "pr":
            return "prs"
        if not ev.actor:
            return None
        return "mine" if mine else "others"
    if ev.source in ("comment", "reaction"):
        return "mine" if mine else "others"
    if ev.source == "message":
        return "chat"
    if ev.source == "note":
        return "notes"
    if ev.source == "delivery":
        return "prs"
    return None


def day_bounds(day: date, zone) -> tuple[datetime, datetime]:
    """The member's local `day` as `[start, end)` instants."""
    start = datetime.combine(day, time.min, tzinfo=zone)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    return start, end


def _followed(task: TaskMaster, day_start: datetime) -> bool:
    """A task of the member's by role is followed on a day when it is open, or was updated since
    that day began: a task closed before the day and untouched since is not followed."""
    return task.status not in _DONE or task.ts_updated_at >= day_start


def _role_ids(ctx: ToolContext, since: datetime) -> tuple[list[int], bool]:
    """Ids of the tasks the member is assignee, reporter, collaborator or commenter on (open, or
    updated since `since`), newest-updated first and at most `CANDIDATE_MAX`; `True` when more
    were left out."""
    me, team = str(ctx.user_id), str(ctx.team_id)
    found = list(
        TaskMaster.objects.filter(team_id=team, is_deleted=False, is_init_task=False)
        .filter(
            Q(assignee_id=me)
            | Q(reporter_id=me)
            | Q(collaborators__id=me)
            | Q(
                task_id__in=TaskComments.objects.filter(sender_id=me, is_deleted=False).values(
                    "task_id"
                )
            )
        )
        .filter(~Q(status__in=_DONE) | Q(ts_updated_at__gte=since))
        .distinct()
        .order_by("-ts_updated_at", "-task_id")
        .values_list("task_id", flat=True)[: CANDIDATE_MAX + 1]
    )
    return found[:CANDIDATE_MAX], len(found) > CANDIDATE_MAX


def _own_traces(
    ctx: ToolContext, zone, days: list[date], since: datetime, until: datetime
) -> tuple[dict[date, set[int]], Counter, bool]:
    """What the member did in the range, read once for all its days: the tasks they acted on
    each day (their history rows, comments, AI-app markers and ledger changes, thread
    messages), and `(task_id, day) -> n` of their own changes that only `MemberEvent` holds (a
    collaborator or dependency added or removed). `True` when a source hit its row cap."""
    me, team = str(ctx.user_id), str(ctx.team_id)
    acted: dict[date, set[int]] = {d: set() for d in days}
    own: Counter = Counter()
    capped = False

    def note(task_id: int | None, at: datetime) -> None:
        day = at.astimezone(zone).date()
        if task_id is not None and day in acted:
            acted[day].add(task_id)

    rows = list(
        TaskActivity.objects.filter(
            team_id=team, actor_id=me, ts_created_at__gte=since, ts_created_at__lt=until
        ).values_list("task_id", "ts_created_at")[:ROW_CAP]
    )
    capped |= len(rows) >= ROW_CAP
    for task_id, at in rows:
        note(task_id, at)
    rows = list(
        TaskComments.objects.filter(
            task__team_id=team,
            sender_id=me,
            is_deleted=False,
            ts_sent_at__gte=since,
            ts_sent_at__lt=until,
        ).values_list("task_id", "ts_sent_at")[:ROW_CAP]
    )
    capped |= len(rows) >= ROW_CAP
    for task_id, at in rows:
        note(task_id, at)

    markers = member_events.recent(
        me, team, since, until, ROW_CAP, kinds=(*MCP_KINDS, *ACTIVITY_KINDS)
    )
    capped |= len(markers) >= ROW_CAP
    for marker, task_id in item_lines._resolve_markers(team, markers).items():
        note(task_id, marker.ts)
        if task_id is not None and marker.kind in ACTIVITY_KINDS:
            day = marker.ts.astimezone(zone).date()
            own[(task_id, day)] += max(1, int(marker.count or 1))

    messages = list(
        Message.objects.filter(
            sender_id=me,
            channel_id__in=readable_chats_qs(team, me).values("id"),
            deleted_at__isnull=True,
            task_id__isnull=True,
            ts_sent_at__gte=since,
            ts_sent_at__lt=until,
        ).values_list("id", "thread_root_id", "ts_sent_at")[:ROW_CAP]
    )
    capped |= len(messages) >= ROW_CAP
    claims = item_lines._claims(team, sorted({str(root or mid) for mid, root, _at in messages}))
    for mid, root, at in messages:
        found = claims.get(str(root or mid), [])
        if len(found) == 1:
            note(found[0], at)
    return acted, own, capped


def compute(ctx: ToolContext, days: list[date]) -> dict[tuple[int, date], dict[str, Any]]:
    """`(task_id, day) -> {"mine", ..., "capped"}` for the member's local `days`, from the
    sources. Only items with something counted appear. What a day follows and counts depends on
    that day alone (the tasks of the member's by role, the ones they acted on that day), so a
    day built by itself equals the same day inside a range, unless a cap cut (`capped`).
    Reads one day at a time, so a read cap in one day cannot change another's counts. Writes
    nothing."""
    if not days:
        return {}
    me = str(ctx.user_id)
    zone = zone_for_user_id(me)
    days = sorted(set(days))
    since, _ = day_bounds(days[0], zone)
    _, until = day_bounds(days[-1], zone)
    access = LabelAccess(ctx)
    role_ids, role_capped = _role_ids(ctx, since)
    acted, own, traces_capped = _own_traces(ctx, zone, days, since, until)
    readable = access.tasks({*role_ids, *(t for ids in acted.values() for t in ids)})
    out: dict[tuple[int, date], dict[str, Any]] = {}
    for day in days:
        start, end = day_bounds(day, zone)
        followed = {t for t in role_ids if t in readable and _followed(readable[t], start)}
        followed |= {t for t in acted[day] if t in readable}
        ranked = sorted(
            (readable[t] for t in followed),
            key=lambda t: (t.ts_updated_at, t.task_id),
            reverse=True,
        )
        tasks = {t.task_id: t for t in ranked[:CANDIDATE_MAX]}
        events, capped = item_lines.read_events(ctx, access, tasks, start, end)
        flag = capped or role_capped or traces_capped or len(ranked) > CANDIDATE_MAX
        for task_id, found in events.items():
            counts = Counter(classify(e, me) for e in found)
            counts.pop(None, None)
            counts["mine"] += own.get((task_id, day), 0)
            if not any(counts[k] for k in COUNT_FIELDS):
                continue
            out[(task_id, day)] = {**{k: counts[k] for k in COUNT_FIELDS}, "capped": flag}
    return out


# --------------------------------------------------------------------------- #
# Writing                                                                      #
# --------------------------------------------------------------------------- #


def _enabled(user_id: str) -> bool:
    return CustomUser.objects.filter(id=user_id, member_events_enabled=True).exists()


def _store(
    ctx: ToolContext,
    first: date,
    last: date,
    rows: dict[tuple[int, date], dict[str, Any]],
    today: date,
    advance: bool = True,
) -> bool:
    """Replace the member's rows for `first..last` with `rows`, move the watermark, sweep the
    year-old rows: one transaction. False (and nothing written) when the member's switch is
    off, read again here under the row lock the opt-out PATCH takes."""
    me, team = str(ctx.user_id), str(ctx.team_id)
    with transaction.atomic():
        user = CustomUser.objects.select_for_update().filter(pk=me).first()
        if user is None or not user.member_events_enabled:
            return False
        mine = MemberItemDay.objects.filter(user_id=me)
        mine.filter(day__lt=today - RETENTION).delete()
        mine.filter(team_id=team, day__gte=first, day__lte=last).delete()
        MemberItemDay.objects.bulk_create(
            [
                MemberItemDay(user_id=me, team_id=team, task_id=task_id, day=day, **counts)
                for (task_id, day), counts in sorted(rows.items(), key=lambda kv: kv[0])
            ]
        )
        if advance:
            state = MemberItemDayState.objects.filter(user_id=me, team_id=team).first()
            if state is None:
                MemberItemDayState.objects.create(user_id=me, team_id=team, built_through=last)
            elif state.built_through < last:
                state.built_through = last
                state.save(update_fields=["built_through"])
    return True


def fill_forward(ctx: ToolContext, now: datetime | None = None) -> int:
    """Build the closed days since the watermark, up to yesterday (member's zone), at most the
    last `FILL_DAYS`. Returns the number of rows written; 0 when nothing was due, or the
    member's switch is off."""
    now = now or timezone.now()
    me, team = str(ctx.user_id), str(ctx.team_id)
    if not _enabled(me):
        return 0
    today = now.astimezone(zone_for_user_id(me)).date()
    yesterday = today - timedelta(days=1)
    state = MemberItemDayState.objects.filter(user_id=me, team_id=team).first()
    earliest = yesterday - timedelta(days=FILL_DAYS - 1)
    first = max(state.built_through + timedelta(days=1), earliest) if state else earliest
    if first > yesterday:
        return 0
    days = [first + timedelta(days=i) for i in range((yesterday - first).days + 1)]
    rows = compute(ctx, days)
    return len(rows) if _store(ctx, first, yesterday, rows, today) else 0


def rebuild(ctx: ToolContext, since_day: date, until_day: date, now: datetime | None = None) -> int:
    """Rebuild the member's rows for `since_day..until_day` (inclusive) from the sources.
    Clipped to the complete days inside the ledger window and before today. Returns the number
    of rows written (0 when the clipped range is empty or the member's switch is off). It does
    not move the fill watermark: what the briefing fills next is not decided by a repair."""
    now = now or timezone.now()
    me = str(ctx.user_id)
    zone = zone_for_user_id(me)
    today = now.astimezone(zone).date()
    floor = (now - member_events.RETENTION).astimezone(zone).date() + timedelta(days=1)
    first = max(since_day, floor)
    last = min(until_day, today - timedelta(days=1))
    if first > last or not _enabled(me):
        return 0
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    rows = compute(ctx, days)
    return len(rows) if _store(ctx, first, last, rows, today, advance=False) else 0


purge = purge_for_user


def start_fresh(user_id: str, now: datetime | None = None, *, through_today: bool = False) -> None:
    """Begin recording from here: the watermark of every team the member is in is moved, so
    nothing from before is rebuilt by the next fill (an explicit `rebuild` still can).

    By default it sets the watermark to yesterday in the member's zone, so the next fill builds
    today. Both ways a member can start over pass `through_today` (MIP-1088 delete, MIP-1229
    turning recording back on): the watermark is today, so the day itself is never stored,
    because it would be built from task history, this morning's activity included, and
    "deleted" / "not recorded while it was off" must stay true."""
    now = now or timezone.now()
    today = now.astimezone(zone_for_user_id(user_id)).date()
    through = today if through_today else today - timedelta(days=1)
    for team_id in TeamMembers.objects.filter(attendee_id=user_id).values_list(
        "team_id", flat=True
    ):
        MemberItemDayState.objects.update_or_create(
            user_id=user_id, team_id=team_id, defaults={"built_through": through}
        )


# --------------------------------------------------------------------------- #
# Reading                                                                      #
# --------------------------------------------------------------------------- #


def trend(totals: list[int]) -> str:
    """`rising`, `cooling`, `steady` or `quiet` from per-day totals, oldest first (seven
    days): the average of the last `RECENT_DAYS` against the average of the days before."""
    recent, earlier = totals[-RECENT_DAYS:], totals[:-RECENT_DAYS]
    if not any(totals):
        return "quiet"
    recent_avg = sum(recent) / len(recent)
    earlier_avg = sum(earlier) / len(earlier) if earlier else 0.0
    if earlier_avg == 0:
        return "rising" if recent_avg > 0 else "steady"
    if recent_avg > earlier_avg * RISING_RATIO:
        return "rising"
    if recent_avg < earlier_avg * COOLING_RATIO:
        return "cooling"
    return "steady"


@dataclass
class WeekSeries:
    """The member's last seven complete days, per task they can open now: `days` (oldest
    first), `readable` (`task_id -> TaskMaster`), `series` (`task_id -> seven daily totals`),
    `kinds` (`task_id -> counts by kind`), `capped` (tasks with a floor, not a count), and
    `complete`: some row sits on the week's first two days, which is how a week that was really
    recorded in full differs from one that begins where recording was switched back on."""

    days: list[date]
    readable: dict[int, Any]
    series: dict[int, list[int]]
    kinds: dict[int, Counter]
    capped: set[int]
    complete: bool
    # `task_id -> seven {kind: count}`, oldest day first: what a ranking needs to leave a kind
    # out of the days and the trend as well as out of the points.
    daily: dict[int, list[dict[str, int]]] = field(default_factory=dict)


def week_series(ctx: ToolContext, now: datetime | None = None) -> WeekSeries | None:
    """The week's rows summed per task; None when there are no rows (nothing built yet, or
    recording is off). Only tasks the member can open now: the rest are dropped before anything
    is summed."""
    now = now or timezone.now()
    me, team = str(ctx.user_id), str(ctx.team_id)
    today = now.astimezone(zone_for_user_id(me)).date()
    days = [today - timedelta(days=WEEK_DAYS - i) for i in range(WEEK_DAYS)]
    rows = list(
        MemberItemDay.objects.filter(
            user_id=me, team_id=team, day__gte=days[0], day__lte=days[-1]
        ).order_by("task_id", "day")
    )
    if not rows:
        return None
    access = LabelAccess(ctx)
    readable = access.tasks({r.task_id for r in rows})
    series: dict[int, list[int]] = {}
    kinds: dict[int, Counter] = {}
    daily: dict[int, list[dict[str, int]]] = {}
    capped: set[int] = set()
    for r in rows:
        if r.task_id not in readable:
            continue
        total = series.setdefault(r.task_id, [0] * WEEK_DAYS)
        by_kind = kinds.setdefault(r.task_id, Counter())
        per_day = daily.setdefault(r.task_id, [{} for _ in range(WEEK_DAYS)])[days.index(r.day)]
        for name in COUNT_FIELDS:
            n = getattr(r, name)
            total[days.index(r.day)] += n
            by_kind[name] += n
            if n:
                per_day[name] = n
        if r.capped:
            capped.add(r.task_id)
    complete = any(r.day <= days[1] for r in rows)
    return WeekSeries(days, readable, series, kinds, capped, complete, daily)


def week(ctx: ToolContext, now: datetime | None = None) -> dict[str, Any] | None:
    """The member's last seven complete days, per item, summed on read; None when there are
    no rows (nothing built yet, or recording is off). Only items the member can open now."""
    return shape_week(ctx, week_series(ctx, now))


def shape_week(ctx: ToolContext, data: WeekSeries | None) -> dict[str, Any] | None:
    """The briefing's `activity` section from a read week."""
    if data is None:
        return None
    days, readable, series, kinds, capped = (
        data.days,
        data.readable,
        data.series,
        data.kinds,
        data.capped,
    )
    ranked = sorted(
        series,
        key=lambda t: (-sum(series[t]), -series[t][-1], t),
    )
    shown = ranked[:WEEK_ITEMS]
    seen, seen_cut = item_lines.seen_at_checked(ctx, shown)
    items = []
    for task_id in shown:
        task = readable[task_id]
        entry: dict[str, Any] = {
            "type": "task",
            "task_id": task.task_id,
            "display_id": task.display_id,
            "title": wrap_workspace_content(task.title or ""),
            "days": series[task_id],
            # Only the kinds that happened, so a quiet column is not repeated per item.
            "week": {
                "total": sum(series[task_id]),
                **{k: kinds[task_id][k] for k in COUNT_FIELDS if kinds[task_id][k]},
            },
            "trend": trend(series[task_id]),
            "last_seen_at": seen[task_id].isoformat() if task_id in seen else None,
        }
        if task_id in capped:
            entry["capped"] = True
        items.append(entry)
    section = {
        "from": days[0].isoformat(),
        "through": days[-1].isoformat(),
        "items": items,
        "total_items": len(ranked),
        "truncated": len(ranked) > len(items),
    }
    if seen_cut:
        # One look-up serves every item, so one flag: a `last_seen_at` above may be missing or
        # older than it was (the markers behind it were cut, `item_lines.seen_at_checked`).
        section["seen_capped"] = True
    return section


def export_rows(user_id: str) -> list[dict[str, Any]]:
    """The member's own rows for their account export, oldest first: ids, days and counts, no
    title. Through the same rule as a read: a task the member cannot open now is not listed."""
    rows = list(MemberItemDay.objects.filter(user_id=user_id).order_by("day", "task_id"))
    readable: dict[str, set[int]] = {}
    for team_id in {str(r.team_id) for r in rows}:
        access = LabelAccess(ToolContext(team_id=team_id, user_id=str(user_id)))
        wanted = {r.task_id for r in rows if str(r.team_id) == team_id}
        readable[team_id] = set(access.tasks(wanted))
    return [
        {
            "team_id": str(r.team_id),
            "task_id": r.task_id,
            "day": r.day.isoformat(),
            **{name: getattr(r, name) for name in COUNT_FIELDS},
            "capped": r.capped,
        }
        for r in rows
        if r.task_id in readable[str(r.team_id)]
    ]


def _safely(call):
    """Run `call` so that a database error in it cannot leave the caller's transaction aborted:
    inside someone's transaction (a test, an atomic request) it gets its own savepoint, and in
    autocommit (a request here) it costs nothing extra."""
    if connection.in_atomic_block:
        with transaction.atomic():
            return call()
    return call()


def briefing_data(
    ctx: ToolContext, now: datetime | None = None
) -> tuple[dict[str, Any] | None, WeekSeries | None]:
    """What the briefing carries, and the week it was made from: bring the table up to
    yesterday, then read the week ONCE. The section is None when recording is off or nothing
    has been built. A failure in either step is logged at WARNING and never fails the briefing
    (a failed fill still shows what was already built). The fill and the read are separate, so
    the row lock the fill takes is not held while reading."""
    try:
        _safely(lambda: fill_forward(ctx, now))
    except Exception:  # noqa: BLE001 - a rollup is never a reason for a briefing to fail
        log.warning("Item activity rollups not filled for the briefing", exc_info=True)
    try:
        data = _safely(lambda: week_series(ctx, now))
        return shape_week(ctx, data), data
    except Exception:  # noqa: BLE001 - a rollup is never a reason for a briefing to fail
        log.warning("Item activity rollups not read for the briefing", exc_info=True)
        return None, None


def briefing_section(ctx: ToolContext, now: datetime | None = None) -> dict[str, Any] | None:
    """The briefing's `activity` section alone: see `briefing_data`."""
    return briefing_data(ctx, now)[0]
