"""Member schedules (MIP-1147): next-run arithmetic, the member's own CRUD, and the tick.

A schedule is "run this job at this local time on these days" (`MemberSchedule`). This
module owns three things, kept apart so each can be tested alone:

1. `next_run_after`: pure date arithmetic in the member's zone, DST-safe.
2. `create / update / set_enabled / delete / run_now / list_schedules`: what a member (later,
   the app and the MCP tools) may do, always for their own rows in their own team.
3. `run_due`: what `schedule_tick` does every five minutes.

## The next-run rules (`next_run_after(fields, after_utc, zone)`)

It returns the first scheduled moment STRICTLY after `after_utc`, as an aware UTC datetime,
or None when there is none. The wall-clock time is read in `zone`:

- `daily`: every local day.            `weekdays`: Monday to Friday.
- `weekly`: the local days in `weekdays` (0-6, Monday = 0).
- `monthly`: local day `month_day`; a month without that day (31 in April, 30 in February)
  runs on the month's last day, so no month is ever skipped.
- `once`: `run_date` at the time, if still ahead, else None.
- A local time that does not exist (spring forward, 02:30 when the clocks jump 02:00 to
  03:00) runs at the first valid moment that day (03:00): the day's run is never lost and
  never doubled.
- A local time that happens twice (fall back, 01:30 twice) runs once, at its first
  occurrence.

## What the tick promises

- A due run happens at most once. The claim is one UPDATE (`claimed_at`); a second tick that
  loses it moves on. A claim older than `STALE_CLAIM` is taken over (a crashed tick must
  not strand a schedule), so a job may, rarely, run twice after a crash: jobs only read.
- A run later than `LATE_GRACE` after its time is skipped and recorded: a "what to do
  today" report at 22:00 is wrong, not late. A long outage yields one recorded outcome per
  schedule, then the schedule moves to its next future time. Missed runs are never replayed.
- The next run is computed from the SCHEDULED time, not from when the run happened, so a
  09:00 schedule that ran at 09:04 is still due at 09:00 tomorrow.
- A failing run counts; `MAX_FAILURES` in a row pauses the schedule (`paused_reason`
  `failures`). Failures are per member: they log at WARNING and never stop the tick.
  ERROR is for the tick's own infrastructure only, since `CronCommand` turns it into a red run.
- If the member's zone changed since the next run was computed, it is recomputed first, so
  "09:00" follows the member to a new city.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from datetime import timezone as dt_timezone
from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apis.request_id import describe_exception
from origin.models.common.schedule_models import (
    FREQ_DAILY,
    FREQ_MONTHLY,
    FREQ_ONCE,
    FREQ_WEEKDAYS,
    FREQ_WEEKLY,
    FREQUENCY_CHOICES,
    PAUSE_FAILURES,
    PAUSE_INACTIVE,
    PAUSE_LEFT_TEAM,
    PAUSE_MANUAL,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_ACCESS,
    STATUS_SKIPPED_LATE,
    MemberSchedule,
)
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import schedule_delivery
from origin.services.mcp_oauth import clean_name
from origin.services.schedule_jobs import JOBS, Report
from origin.services.user_time import user_zone

log = logging.getLogger("origin.schedules")

# A member may keep this many schedules (across their teams). A module constant on
# purpose: `apis/settings.py` is not touched for a limit this small.
MAX_SCHEDULES_PER_MEMBER = 20
MAX_NAME_CHARS = 100
MAX_PARAMS_BYTES = 2000
MAX_ERROR_CHARS = 200

# A run later than this after its scheduled time is recorded as skipped, not run.
LATE_GRACE = timedelta(hours=3)
# A claim older than this is taken over. Well above the tick's own budget (4 minutes).
STALE_CLAIM = timedelta(minutes=15)
# Failures in a row before the schedule pauses itself.
MAX_FAILURES = 3

_SEARCH_DAYS = 400
_MAX_SKIPPED_OCCURRENCES = 1000
_FREQUENCIES = tuple(key for key, _ in FREQUENCY_CHOICES)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


class ScheduleError(Exception):
    """Something wrong with what the member asked for. The message is in plain words and is
    meant to be shown to them."""


class ScheduleNotFound(ScheduleError):
    """No such schedule for this member in this team. The same for an id that belongs to
    someone else, to another team, or that is not an id at all."""


class ScheduleRunError(Exception):
    """A run failed for a reason we can state without quoting anything (`public`)."""

    def __init__(self, public: str) -> None:
        super().__init__(public)
        self.public = public


# ── next-run arithmetic ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Recurrence:
    frequency: str
    time_of_day: dtime
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None
    run_date: date | None = None


def _field(fields: Any, name: str, default: Any = None) -> Any:
    if isinstance(fields, Mapping):
        return fields.get(name, default)
    return getattr(fields, name, default)


def _recurrence_of(fields: Any) -> Recurrence:
    when = _field(fields, "time_of_day")
    if isinstance(when, str):
        when = _parse_clock(when)
    return Recurrence(
        frequency=_field(fields, "frequency"),
        time_of_day=when,
        weekdays=tuple(_field(fields, "weekdays") or ()),
        month_day=_field(fields, "month_day"),
        run_date=_field(fields, "run_date"),
    )


def _local_to_utc(day: date, when: dtime, zone) -> datetime:
    """The UTC instant of `when` on local `day`.

    A time that exists is itself (an ambiguous one, in the hour the clocks fall back, takes
    its first occurrence). One that does not exist is the first valid instant after the gap.
    """
    naive = datetime.combine(day, when.replace(second=0, microsecond=0))
    first = naive.replace(tzinfo=zone, fold=0).astimezone(dt_timezone.utc)
    if first.astimezone(zone).replace(tzinfo=None) == naive:
        return first
    # In a gap, fold=1 reads the wall time with the offset from AFTER the jump, which puts
    # it before the jump. Walk forward to the first instant whose wall time has reached it.
    probe = naive.replace(tzinfo=zone, fold=1).astimezone(dt_timezone.utc)
    for _ in range(24 * 60):
        if probe.astimezone(zone).replace(tzinfo=None) >= naive:
            return probe
        probe += timedelta(minutes=1)
    return first  # pragma: no cover - no zone has a gap this long


def _matches(rec: Recurrence, day: date) -> bool:
    if rec.frequency == FREQ_DAILY:
        return True
    if rec.frequency == FREQ_WEEKDAYS:
        return day.weekday() < 5
    if rec.frequency == FREQ_WEEKLY:
        return day.weekday() in rec.weekdays
    if rec.frequency == FREQ_MONTHLY and rec.month_day:
        return day.day == min(rec.month_day, calendar.monthrange(day.year, day.month)[1])
    return False


def next_run_after(fields: Any, after_utc: datetime, zone) -> datetime | None:
    """The first scheduled moment strictly after `after_utc` (UTC), or None. See the module
    docstring for the rules. `fields` is a `MemberSchedule`, a `Recurrence` or a dict."""
    rec = _recurrence_of(fields)
    after = after_utc.astimezone(dt_timezone.utc)
    if rec.frequency == FREQ_ONCE:
        if rec.run_date is None:
            return None
        moment = _local_to_utc(rec.run_date, rec.time_of_day, zone)
        return moment if moment > after else None
    day = after.astimezone(zone).date()
    for _ in range(_SEARCH_DAYS):
        if _matches(rec, day):
            moment = _local_to_utc(day, rec.time_of_day, zone)
            if moment > after:
                return moment
        day += timedelta(days=1)
    return None


# ── validation ──────────────────────────────────────────────────────────


def _parse_clock(value: str) -> dtime:
    match = _CLOCK.match(value.strip())
    if not match or int(match[1]) > 23 or int(match[2]) > 59:
        raise ScheduleError(f"The time must look like 09:30 (24-hour clock), not {value[:20]!r}.")
    return dtime(int(match[1]), int(match[2]))


def _clean_time(value: Any) -> dtime:
    if isinstance(value, str):
        return _parse_clock(value)
    if isinstance(value, dtime):
        if value.second or value.microsecond:
            raise ScheduleError("The time must be a whole minute, such as 09:30.")
        return value.replace(tzinfo=None)
    raise ScheduleError("A schedule needs a time of day, such as 09:30.")


def _clean_date(value: Any) -> date:
    if isinstance(value, datetime):
        raise ScheduleError("The date must be a day, such as 2026-12-01, without a time.")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            pass
    raise ScheduleError("The date must look like 2026-12-01.")


def _clean_weekdays(value: Any) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ScheduleError(
            "A weekly schedule needs at least one weekday: a list of numbers from 0 (Monday) "
            "to 6 (Sunday)."
        )
    days: set[int] = set()
    for day in value:
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ScheduleError(
                f"Weekdays are numbers from 0 (Monday) to 6 (Sunday), but got {str(day)[:20]!r}."
            )
        days.add(day)
    return tuple(sorted(days))


def clean_recurrence(
    *,
    frequency: Any,
    time_of_day: Any,
    weekdays: Any = None,
    month_day: Any = None,
    run_date: Any = None,
) -> Recurrence:
    """Validate the recurrence fields. Fields the frequency does not use are dropped, so
    changing weekly to daily needs no tidying of the days."""
    if frequency not in _FREQUENCIES:
        raise ScheduleError(f"`frequency` must be one of {', '.join(_FREQUENCIES)}.")
    when = _clean_time(time_of_day)
    days: tuple[int, ...] = ()
    day_of_month = None
    on_date = None
    if frequency == FREQ_WEEKLY:
        days = _clean_weekdays(weekdays)
    elif frequency == FREQ_MONTHLY:
        if (
            isinstance(month_day, bool)
            or not isinstance(month_day, int)
            or not 1 <= month_day <= 31
        ):
            raise ScheduleError(
                "A monthly schedule needs `month_day`, a number from 1 to 31 (a month "
                "without that day runs on its last day)."
            )
        day_of_month = month_day
    elif frequency == FREQ_ONCE:
        if run_date in (None, ""):
            raise ScheduleError("A one-time schedule needs `run_date`, such as 2026-12-01.")
        on_date = _clean_date(run_date)
    return Recurrence(frequency, when, days, day_of_month, on_date)


def _clean_name(value: Any) -> str:
    name = clean_name(value) if isinstance(value, str) else ""
    if not name:
        raise ScheduleError("A schedule needs a name.")
    if len(name) > MAX_NAME_CHARS:
        raise ScheduleError(f"The name is too long: at most {MAX_NAME_CHARS} characters.")
    return name


_JSON_TYPES = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def clean_params(job_key: str, params: Any) -> dict[str, Any]:
    """Check `params` against the job's `params_schema` (see `schedule_jobs`)."""
    job = JOBS.get(job_key)
    if job is None:
        known = ", ".join(sorted(JOBS)) or "none yet"
        raise ScheduleError(
            f"There is no job called {str(job_key)[:40]!r}. Available jobs: {known}."
        )
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ScheduleError('`params` must be an object, such as {} or {"days": 7}.')
    try:
        size = len(json.dumps(params, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise ScheduleError("`params` must be plain JSON values.") from None
    if size > MAX_PARAMS_BYTES:
        raise ScheduleError(f"`params` is too large: at most {MAX_PARAMS_BYTES} bytes.")
    schema = job.params_schema or {}
    properties = schema.get("properties") or {}
    for name in params:
        if name not in properties:
            allowed = ", ".join(sorted(properties)) or "none"
            raise ScheduleError(
                f"The job {job.key!r} has no parameter {str(name)[:40]!r}. Its parameters: {allowed}."
            )
    for name in schema.get("required") or []:
        if name not in params:
            raise ScheduleError(f"The job {job.key!r} needs the parameter {name!r}.")
    for name, value in params.items():
        spec = properties[name]
        kind = spec.get("type")
        if kind in _JSON_TYPES and not _JSON_TYPES[kind](value):
            raise ScheduleError(f"Parameter {name!r} must be of type {kind}.")
        if "enum" in spec and value not in spec["enum"]:
            raise ScheduleError(
                f"Parameter {name!r} must be one of {', '.join(map(str, spec['enum']))}."
            )
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if "minimum" in spec and value < spec["minimum"]:
                raise ScheduleError(f"Parameter {name!r} must be at least {spec['minimum']}.")
            if "maximum" in spec and value > spec["maximum"]:
                raise ScheduleError(f"Parameter {name!r} must be at most {spec['maximum']}.")
    return params


# ── access and lookup ───────────────────────────────────────────────────


def _access_problem(user: CustomUser | None, team_id: str) -> str | None:
    """Why this member cannot have schedules run in this team, as a `paused_reason`; None
    when they can."""
    if user is None or not user.is_active or user.is_deleted:
        return PAUSE_INACTIVE
    still_member = TeamMembers.objects.filter(
        team_id=team_id, attendee_id=user.id, is_deleted=False, team__is_deleted=False
    ).exists()
    return None if still_member else PAUSE_LEFT_TEAM


def _require_member(ctx: ToolContext) -> CustomUser:
    user = CustomUser.objects.filter(pk=ctx.user_id).first()
    if _access_problem(user, ctx.team_id) is not None:
        raise ScheduleError("You are not a member of this team, so you cannot schedule jobs in it.")
    return user


def _owned(ctx: ToolContext, schedule_id: Any):
    try:
        key = uuid.UUID(str(schedule_id))
    except (ValueError, AttributeError):
        raise ScheduleNotFound("Schedule not found.") from None
    return MemberSchedule.objects.filter(pk=key, user_id=ctx.user_id, team_id=ctx.team_id)


def _get(ctx: ToolContext, schedule_id: Any, *, lock: bool = False) -> MemberSchedule:
    qs = _owned(ctx, schedule_id)
    if lock:
        qs = qs.select_for_update()
    try:
        return qs.get()
    except (MemberSchedule.DoesNotExist, ValidationError):
        raise ScheduleNotFound("Schedule not found.") from None


# ── what a member can do ────────────────────────────────────────────────

_EDITABLE = {
    "name",
    "job",
    "params",
    "frequency",
    "weekdays",
    "month_day",
    "run_date",
    "time_of_day",
    "deliver_push",
}
_RECURRENCE_FIELDS = {"frequency", "weekdays", "month_day", "run_date", "time_of_day"}


def list_schedules(ctx: ToolContext) -> list[MemberSchedule]:
    return list(
        MemberSchedule.objects.filter(user_id=ctx.user_id, team_id=ctx.team_id).order_by(
            "time_of_day", "ts_created_at"
        )
    )


def _first_run(rec: Recurrence, zone, now: datetime) -> datetime:
    moment = next_run_after(rec, now, zone)
    if moment is None:
        raise ScheduleError(
            "That time has already passed, so the schedule would never run. Pick a later date."
        )
    return moment


def create(
    ctx: ToolContext,
    *,
    name: Any,
    job: Any,
    params: Any = None,
    frequency: Any,
    time_of_day: Any,
    weekdays: Any = None,
    month_day: Any = None,
    run_date: Any = None,
    deliver_push: bool = False,
    now: datetime | None = None,
) -> MemberSchedule:
    now = now or timezone.now()
    user = _require_member(ctx)
    clean = _clean_name(name)
    cleaned_params = clean_params(job, params)
    rec = clean_recurrence(
        frequency=frequency,
        time_of_day=time_of_day,
        weekdays=weekdays,
        month_day=month_day,
        run_date=run_date,
    )
    zone = user_zone(user)
    first = _first_run(rec, zone, now)
    with transaction.atomic():
        # Two creates at once must not both slip under the cap: take the member's row.
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        if MemberSchedule.objects.filter(user_id=user.pk).count() >= MAX_SCHEDULES_PER_MEMBER:
            raise ScheduleError(
                f"You can keep at most {MAX_SCHEDULES_PER_MEMBER} schedules. "
                "Delete one you no longer need first."
            )
        return MemberSchedule.objects.create(
            user_id=user.pk,
            team_id=ctx.team_id,
            name=clean,
            job=job,
            params=cleaned_params,
            frequency=rec.frequency,
            weekdays=list(rec.weekdays),
            month_day=rec.month_day,
            run_date=rec.run_date,
            time_of_day=rec.time_of_day,
            enabled=True,
            tz_used=zone.key,
            next_run_at=first,
            deliver_push=bool(deliver_push),
        )


def update(
    ctx: ToolContext, schedule_id: Any, *, now: datetime | None = None, **changes: Any
) -> MemberSchedule:
    """Change the named fields of one of the member's schedules. Changing the recurrence
    recomputes the next run; anything else leaves it alone."""
    now = now or timezone.now()
    unknown = sorted(set(changes) - _EDITABLE)
    if unknown:
        raise ScheduleError(f"These cannot be changed: {', '.join(unknown)}.")
    user = _require_member(ctx)
    with transaction.atomic():
        sched = _get(ctx, schedule_id, lock=True)
        dirty = ["claimed_at", "ts_updated_at"]
        if "name" in changes:
            sched.name = _clean_name(changes["name"])
            dirty.append("name")
        if "job" in changes or "params" in changes:
            job = changes.get("job", sched.job)
            sched.params = clean_params(job, changes.get("params", sched.params))
            sched.job = job
            dirty += ["job", "params"]
        if "deliver_push" in changes:
            sched.deliver_push = bool(changes["deliver_push"])
            dirty.append("deliver_push")
        if _RECURRENCE_FIELDS & set(changes):
            merged = {
                name: changes.get(name, getattr(sched, name))
                for name in ("frequency", "weekdays", "month_day", "run_date", "time_of_day")
            }
            rec = clean_recurrence(**merged)
            zone = user_zone(user)
            sched.frequency = rec.frequency
            sched.weekdays = list(rec.weekdays)
            sched.month_day = rec.month_day
            sched.run_date = rec.run_date
            sched.time_of_day = rec.time_of_day
            dirty += ["frequency", "weekdays", "month_day", "run_date", "time_of_day"]
            if sched.enabled:
                sched.next_run_at = _first_run(rec, zone, now)
                sched.tz_used = zone.key
                dirty += ["next_run_at", "tz_used"]
        # An edit takes over a run in progress: the tick's own write-back is conditional on
        # its claim, so it will find the claim gone and leave the member's edit alone.
        sched.claimed_at = None
        sched.save(update_fields=sorted(set(dirty)))
        return sched


def set_enabled(
    ctx: ToolContext, schedule_id: Any, enabled: bool, *, now: datetime | None = None
) -> MemberSchedule:
    """Pause (`paused_reason` manual) or resume. Resuming forgets past failures and counts
    the next run from now."""
    now = now or timezone.now()
    user = _require_member(ctx)
    with transaction.atomic():
        sched = _get(ctx, schedule_id, lock=True)
        if enabled:
            zone = user_zone(user)
            sched.next_run_at = _first_run(_recurrence_of(sched), zone, now)
            sched.tz_used = zone.key
            sched.enabled = True
            sched.paused_reason = ""
            sched.consecutive_failures = 0
        else:
            sched.enabled = False
            sched.paused_reason = PAUSE_MANUAL
            sched.next_run_at = None
        sched.claimed_at = None
        sched.save()
        return sched


def delete(ctx: ToolContext, schedule_id: Any) -> None:
    deleted, _ = _owned(ctx, schedule_id).delete()
    if not deleted:
        raise ScheduleNotFound("Schedule not found.")


# ── running a job ───────────────────────────────────────────────────────


def _check_report(report: Any) -> None:
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("title"), str)
        or not isinstance(report.get("sections"), list)
    ):
        raise ScheduleRunError("The job returned a report in the wrong shape.")


def _execute(sched: MemberSchedule) -> tuple[str, str]:
    """Run the schedule's job as its owner and deliver the report. Returns `(status, error)`.

    Never raises for a job's own failure. The run is one savepoint: a job whose query fails
    would otherwise leave the connection aborted and fail every schedule after it.
    """
    job = JOBS.get(sched.job)
    if job is None:
        return STATUS_FAILED, "That job no longer exists."
    ctx = ToolContext(team_id=str(sched.team_id), user_id=str(sched.user_id))
    try:
        with transaction.atomic():
            report: Report = job.run(ctx, dict(sched.params or {}))
            _check_report(report)
            schedule_delivery.deliver(sched, report)
    except ScheduleRunError as exc:
        return STATUS_FAILED, exc.public[:MAX_ERROR_CHARS]
    except Exception as exc:  # noqa: BLE001 - one schedule's failure is that schedule's
        return STATUS_FAILED, describe_exception(exc)[:MAX_ERROR_CHARS]
    return STATUS_OK, ""


def run_now(ctx: ToolContext, schedule_id: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Run one of the member's schedules right now, whatever the clock says.

    Records `last_*` like a run, but leaves the next run, the failure count and the pause
    alone: a test run does not use up today's run or push a schedule toward pausing.
    """
    now = now or timezone.now()
    _require_member(ctx)
    sched = _get(ctx, schedule_id)
    status, error = _execute(sched)
    MemberSchedule.objects.filter(pk=sched.pk).update(
        last_run_at=now, last_status=status, last_error=error, ts_updated_at=now
    )
    sched.refresh_from_db()
    if status == STATUS_FAILED:
        log.warning(
            "schedule_run_now_failed schedule=%s job=%s error=%s", sched.id, sched.job, error
        )
    return {
        "status": status,
        "error": error,
        "title": (sched.last_result or {}).get("title", "") if status == STATUS_OK else "",
        "items": (sched.last_result or {}).get("items", 0) if status == STATUS_OK else 0,
    }


# ── the tick ────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    due: int = 0
    ran: int = 0
    failed: int = 0
    skipped_late: int = 0
    skipped_access: int = 0
    claimed_elsewhere: int = 0
    rezoned: int = 0
    deferred: int = 0
    paused: int = 0
    max_late_seconds: int = 0


def _unclaimed(now: datetime) -> Q:
    return Q(claimed_at__isnull=True) | Q(claimed_at__lt=now - STALE_CLAIM)


def due_schedules(*, now: datetime | None = None, limit: int = 200) -> list[MemberSchedule]:
    """Enabled schedules whose time has come and that nobody holds, oldest first."""
    now = now or timezone.now()
    return list(
        MemberSchedule.objects.filter(_unclaimed(now), enabled=True, next_run_at__lte=now)
        .select_related("user")
        .order_by("next_run_at")[:limit]
    )


def _claim(pk: Any, now: datetime) -> bool:
    """Take the right to run one due schedule. One conditional UPDATE, so of two ticks
    asking at once exactly one gets a row."""
    return (
        MemberSchedule.objects.filter(
            _unclaimed(now), pk=pk, enabled=True, next_run_at__lte=now
        ).update(claimed_at=now)
        == 1
    )


def _advance(sched: MemberSchedule, now: datetime, zone) -> datetime | None:
    """The next run after the one just handled: from its SCHEDULED time, then forward past
    any occurrence too old to run, so a long outage is not replayed one day per tick."""
    moment = next_run_after(sched, sched.next_run_at, zone)
    for _ in range(_MAX_SKIPPED_OCCURRENCES):
        if moment is None or moment + LATE_GRACE > now:
            return moment
        moment = next_run_after(sched, moment, zone)
    return next_run_after(sched, now, zone)


def _recompute_zone(row: MemberSchedule, now: datetime, zone) -> bool:
    """The member's zone is not the one `next_run_at` was computed in: compute it again, in
    the new one. From `now - LATE_GRACE`, so a run that is only a little late still runs."""
    fresh = next_run_after(row, now - LATE_GRACE, zone)
    changed = MemberSchedule.objects.filter(pk=row.pk, enabled=True, tz_used=row.tz_used).update(
        next_run_at=fresh, tz_used=zone.key, ts_updated_at=now
    )
    if changed:
        row.next_run_at, row.tz_used = fresh, zone.key
    return bool(changed)


def _finish(sched: MemberSchedule, claim: datetime, now: datetime, **fields: Any) -> bool:
    """Write the outcome, only while our claim still stands. False when the member edited or
    deleted the schedule meanwhile (or another tick took the claim over): their change wins."""
    done = MemberSchedule.objects.filter(pk=sched.pk, claimed_at=claim).update(
        claimed_at=None, ts_updated_at=now, **fields
    )
    if not done:
        log.info("schedule_changed_during_run schedule=%s", sched.pk)
    return bool(done)


def _process(row: MemberSchedule, now: datetime, summary: RunSummary) -> None:
    zone = user_zone(row.user)
    if row.tz_used != zone.key:
        if not _recompute_zone(row, now, zone):
            return
        summary.rezoned += 1
        if row.next_run_at is None or row.next_run_at > now:
            return

    if not _claim(row.pk, now):
        summary.claimed_elsewhere += 1
        return
    try:
        # Fresh after the claim: the member may have edited it since the tick listed it.
        sched = MemberSchedule.objects.select_related("user").get(pk=row.pk)
    except MemberSchedule.DoesNotExist:
        return

    problem = _access_problem(sched.user, str(sched.team_id))
    if problem:
        summary.skipped_access += 1
        _finish(
            sched,
            now,
            now,
            last_status=STATUS_SKIPPED_ACCESS,
            last_error="",
            enabled=False,
            paused_reason=problem,
            next_run_at=None,
        )
        log.info("schedule_paused schedule=%s reason=%s", sched.pk, problem)
        return

    next_run = _advance(sched, now, zone)
    if now - sched.next_run_at > LATE_GRACE:
        summary.skipped_late += 1
        _finish(
            sched,
            now,
            now,
            last_status=STATUS_SKIPPED_LATE,
            last_error="",
            next_run_at=next_run,
            tz_used=zone.key,
        )
        log.info("schedule_skipped_late schedule=%s", sched.pk)
        return

    status, error = _execute(sched)
    fields: dict[str, Any] = {
        "last_run_at": now,
        "last_status": status,
        "last_error": error,
        "next_run_at": next_run,
        "tz_used": zone.key,
    }
    if status == STATUS_OK:
        summary.ran += 1
        fields["consecutive_failures"] = 0
    else:
        summary.failed += 1
        failures = sched.consecutive_failures + 1
        fields["consecutive_failures"] = failures
        # WARNING, never ERROR: this is one member's job misbehaving, not the tick.
        log.warning(
            "schedule_failed schedule=%s job=%s failures=%d error=%s",
            sched.pk,
            sched.job,
            failures,
            error,
        )
        if failures >= MAX_FAILURES:
            summary.paused += 1
            fields.update(enabled=False, paused_reason=PAUSE_FAILURES, next_run_at=None)
            log.warning("schedule_paused schedule=%s reason=%s", sched.pk, PAUSE_FAILURES)
    _finish(sched, now, now, **fields)


def run_due(
    now: datetime | None = None, budget_seconds: float = 240, limit: int = 200
) -> RunSummary:
    """Run every schedule that has come due, oldest first, at most `limit` of them and
    stopping to pick up new ones once `budget_seconds` is spent (the rest are due again on
    the next tick)."""
    now = now or timezone.now()
    started = time.monotonic()
    rows = due_schedules(now=now, limit=limit)
    summary = RunSummary(due=len(rows))
    if rows:
        summary.max_late_seconds = max(0, int((now - rows[0].next_run_at).total_seconds()))
    for index, row in enumerate(rows):
        if time.monotonic() - started >= budget_seconds:
            summary.deferred = len(rows) - index
            break
        try:
            _process(row, now, summary)
        except Exception:  # noqa: BLE001
            # Bookkeeping failed (the database, not the member's job): a real fault, so
            # ERROR, and the tick fails loudly. The other schedules still get their turn.
            log.exception("schedule_tick: could not process schedule=%s", row.pk)
    return summary
