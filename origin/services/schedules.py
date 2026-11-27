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
  loses it moves on. While the run is alive it refreshes `heartbeat_at` (`_heartbeat`); a claim
  whose heartbeat is older than `HEARTBEAT_STALE` is taken over (a killed run must not strand a
  schedule), so a job may, rarely, run twice after a crash: jobs only read.

## "Run now" (MIP-2457)

A job that can be slow (`runs_in_background`: a long model loop, a dashboard page, anything
that spends AI usage) NEVER runs inside the web request: gunicorn kills a worker past its
timeout, and a killed worker records nothing. `run_now` claims the row and hands it to a child
process (`schedule_runner`), and answers `started`. A plain report still runs in the request.
A child that dies (a deploy, out of memory) leaves a claim that goes stale within about a
minute; `fail_stale_hand_runs` then records the failure. Nothing is retried: it may have
spent AI usage already.
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
- **Core and up (MIP-1213).** Scheduled reports are not part of Free. Creating, editing,
  turning on and "run now" are refused with an upgrade sentence (`SchedulePlanRequired`);
  listing, pausing and deleting still work. No report is filed for a member without the plan:
  the tick pauses their schedules with the reason `plan` (`sync_plan`, and a guard in
  `_process`), keeps them, and resumes exactly those when the plan comes back. The EFFECTIVE
  tier decides (`quota.get_schedules_enabled`): the best of the member's own and their teams'.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from datetime import timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, transaction
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
    PAUSE_NOTE,
    PAUSE_PLAN,
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED_ACCESS,
    STATUS_SKIPPED_LATE,
    MemberSchedule,
)
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import (
    schedule_audience,
    schedule_delivery,
    schedule_message,
    schedule_runner,
    schedule_text,
)
from origin.services.mcp_oauth import clean_name
from origin.services.schedule_jobs import AI_EFFORT_KEY, AUDIENCE_KEY, JOBS, SHARED_KEY, Report
from origin.services.user_time import user_zone

log = logging.getLogger("origin.schedules")

# The fallback count of schedules a member may keep (across their teams) when the plan's own value
# cannot be read. The live cap is per tier (`schedules_max` in `TIER_QUOTAS`, MIP-1960: Core 5,
# Pro 20, Max 20, Enterprise 20): use `schedule_limit(user_id)`.
MAX_SCHEDULES_PER_MEMBER = 20
MAX_NAME_CHARS = 100
MAX_PARAMS_BYTES = 2000
MAX_ERROR_CHARS = 200

# A run later than this after its scheduled time is recorded as skipped, not run.
LATE_GRACE = timedelta(hours=3)
# A claim that never had a heartbeat (made before `heartbeat_at` existed) is taken over after
# this. Well above the tick's own budget (4 minutes).
STALE_CLAIM = timedelta(minutes=15)
# A live run refreshes its heartbeat this often, and one this old means the run is gone.
HEARTBEAT_EVERY_SECONDS = 10
HEARTBEAT_STALE = timedelta(seconds=60)
# `run_now`'s answer for a run that goes on after the request. Never stored on the row.
STATUS_STARTED = "started"
RUN_STOPPED_SENTENCE = (
    "The run stopped before it finished (the server may have restarted). It was not retried: "
    "run it again."
)
RUN_TOO_LONG_SENTENCE = "The report took too long and was stopped. It was not retried."
# Failures in a row before the schedule pauses itself.
MAX_FAILURES = 3
# "Run now" is a convenience, and it delivers a report each time: a second one this soon is
# a loop or a mistake, not a member checking their schedule.
RUN_NOW_COOLDOWN = timedelta(seconds=60)

_SEARCH_DAYS = 400
_MAX_SKIPPED_OCCURRENCES = 1000
_FREQUENCIES = tuple(key for key, _ in FREQUENCY_CHOICES)
_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")


class ScheduleError(Exception):
    """Something wrong with what the member asked for. The message is in plain words and is
    meant to be shown to them."""


class SchedulePlanRequired(ScheduleError):
    """The member's plan does not include scheduled reports (Free). The message is the upgrade
    sentence, written to be shown as it is; the REST layer adds `code: plan_required`."""


class ScheduleLimitReached(ScheduleError):
    """The member already has as many schedules as their plan allows (MIP-1960). Only a NEW
    schedule is refused; the ones that exist keep running. The message is a sentence to show;
    the REST layer adds `code: plan_limit` and the two numbers."""

    def __init__(self, message: str, *, limit: int, used: int) -> None:
        super().__init__(message)
        self.limit = limit
        self.used = used


class ScheduleNotFound(ScheduleError):
    """No such schedule for this member in this team. The same for an id that belongs to
    someone else, to another team, or that is not an id at all."""


class RunTimedOut(BaseException):
    """A background run passed its time limit. Not an `Exception`, so that no job's own
    failure handling swallows it."""


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


def clean_ai_effort(value: Any) -> str:
    """The AI effort a member chose, as stored: "" (the job's default; also `None`) or one of the
    catalog's levels. Anything else is refused by name when it is SAVED; at run time an unknown
    level (one the catalog dropped since) just falls back to the job's default."""
    from origin.services.report_ai import effort as effort_mod  # noqa: PLC0415

    if value is None or value == "":
        return ""
    level = effort_mod.valid(value)
    if level is None or value != level:
        raise ScheduleError(
            "`ai_effort` must be low, medium or high, or empty for the job's own default."
        )
    return level


def _offered(job) -> bool:
    """Whether a job may be chosen now: it is not behind a switch that is off (`Job.available`)."""
    return job.available is None or bool(job.available())


def _check_params_for_owner(
    ctx: ToolContext, job_key: str, params: dict[str, Any], schedule_id: Any
) -> dict[str, Any]:
    """Run the job's own check of `params` against what the OWNER may use (`Job.check_params`,
    MIP-2045), and keep the size limit on what it returns. Call inside the transaction that holds
    the member's row lock, so a count it makes cannot be raced."""
    job = JOBS.get(job_key)
    if job is None or job.check_params is None:
        return params
    limit = job.max_params_bytes or MAX_PARAMS_BYTES
    checked = job.check_params(ctx, dict(params), schedule_id)
    try:
        size = len(json.dumps(checked, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise ScheduleError("`params` must be plain JSON values.") from None
    if size > limit:
        raise ScheduleError(f"`params` is too large: at most {limit} bytes.")
    return checked


def clean_params(job_key: str, params: Any) -> dict[str, Any]:
    """Check `params` against the job's `params_schema` (see `schedule_jobs`)."""
    job = JOBS.get(job_key)
    if job is None or not _offered(job):
        known = ", ".join(sorted(k for k, j in JOBS.items() if _offered(j))) or "none yet"
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
    limit = job.max_params_bytes or MAX_PARAMS_BYTES
    if size > limit:
        # A message is the member's own text: say it in those words.
        raise ScheduleError(
            schedule_message.TOO_LONG
            if job.posts_as_owner
            else f"`params` is too large: at most {limit} bytes."
        )
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

PLAN_SENTENCE = (
    "Scheduled reports are part of the Core plan and up. Upgrade to turn them on; "
    "your schedules are kept until then."
)


def plan_allows(user_id: Any) -> bool:
    """Whether the member's plan includes scheduled reports (Core and up, MIP-1213). The
    EFFECTIVE tier decides: the best of their own tier and their teams' plans (`quota`)."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    return quota.get_schedules_enabled(str(user_id))


def tiers_without_schedules() -> set[str]:
    """The tiers whose `schedules_enabled` reads off in the live `TIER_QUOTAS`: the table's own
    value, or the tier default (Free no, every paid tier yes) when it is missing or not a bool,
    exactly the rule `quota.get_schedules_enabled` applies (MIP-1226). Used to leave members out
    of a SQL query that certainly cannot pass the gate; the gate itself still decides."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    off: set[str] = set()
    for tier in quota._TIER_RANK:  # noqa: SLF001 - the table of tier names
        value = quota._tier_cfg(tier).get("schedules_enabled")  # noqa: SLF001
        if not (value if isinstance(value, bool) else tier != "free"):
            off.add(tier)
    return off


def schedule_limit(user_id: Any) -> int:
    """How many schedules this member may keep: their plan's `schedules_max` (MIP-1960). An
    unlimited plan reads as the old ceiling, so a count is always a number."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    cap = quota.get_schedules_max(str(user_id))
    return MAX_SCHEDULES_PER_MEMBER if cap is None else cap


def _require_plan(user: CustomUser) -> None:
    if not plan_allows(user.pk):
        raise SchedulePlanRequired(PLAN_SENTENCE)


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
    "ai_effort",
    "deliver_channel_id",
}
_RECURRENCE_FIELDS = {"frequency", "weekdays", "month_day", "run_date", "time_of_day"}


def list_schedules(ctx: ToolContext) -> list[MemberSchedule]:
    return list(
        MemberSchedule.objects.filter(user_id=ctx.user_id, team_id=ctx.team_id)
        .select_related("deliver_channel__project")
        .order_by("time_of_day", "ts_created_at")
    )


def _first_run(rec: Recurrence, zone, now: datetime) -> datetime:
    moment = next_run_after(rec, now, zone)
    if moment is None:
        raise ScheduleError(
            "That time has already passed, so the schedule would never run. Pick a later date."
        )
    return moment


def _name_key(name: str) -> str:
    """What "the same name" means: trimmed, one line, runs of spaces folded, case ignored."""
    return clean_name(name).casefold()


def _clean_target(
    ctx: ToolContext, value: Any, job: str, params: dict[str, Any]
) -> uuid.UUID | None:
    """The chat a schedule posts its report in (MIP-1420): None (empty) is the Inbox. A chat the
    member cannot read, or one this report may not be posted in (who would read it, an AI part),
    is refused in words; see `schedule_audience`."""
    if value is None or value == "":
        owner_posts = JOBS.get(job)
        if owner_posts is not None and owner_posts.posts_as_owner:
            raise ScheduleError(schedule_audience.MESSAGE_NEEDS_CHAT)
        return None
    channel = schedule_audience.load_target(ctx.user_id, ctx.team_id, value)
    if channel is None:
        raise ScheduleError(schedule_audience.NOT_FOUND)
    why = schedule_audience.problem(ctx.user_id, ctx.team_id, channel, job, params)
    if why:
        raise ScheduleError(why)
    return channel.id


def _refuse_duplicate(
    ctx: ToolContext,
    *,
    name: str,
    job: str,
    params: dict[str, Any],
    rec: Recurrence,
    target: Any = None,
    check_name: bool = True,
    check_content: bool = True,
) -> None:
    """Refuse a schedule that would be the SAME as another one of the member's in this team
    (MIP-1201): the same job, parameters and recurrence at the same time of day, or the same
    name (two reports with one name cannot be told apart in the Inbox). A different time or
    different parameters is a different schedule. A paused schedule still counts. The sentence
    names the one in the way.

    An edit calls it before the row is saved, asking only about what it CHANGED (`check_name`,
    `check_content`): the row being edited is still read as it was, which differs from what it
    is being changed to, so it is never "another" by itself, and a row that is already
    duplicated is not refused for what it did not change. Call it inside the transaction that
    holds the member's row lock."""
    others = list_schedules(ctx)
    if check_content:
        for other in others:
            if (
                other.job == job
                and (other.params or {}) == params
                and _recurrence_of(other) == rec
                and other.deliver_channel_id == target
            ):
                raise ScheduleError(
                    f"You already have {other.name!r} {schedule_text.describe_recurrence(other)}, "
                    "which does the same thing."
                )
    if check_name:
        key = _name_key(name)
        for other in others:
            if _name_key(other.name) == key:
                raise ScheduleError(
                    f"You already have a schedule called {other.name!r} "
                    f"({schedule_text.describe_recurrence(other)}). Pick a different name."
                )


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
    deliver_push: bool = True,
    ai_effort: Any = None,
    deliver_channel_id: Any = None,
    now: datetime | None = None,
    immediate: bool = False,
) -> MemberSchedule:
    """Save a schedule. `immediate` (MIP-2402, `create_and_run` only) makes a one-time schedule
    that is not waiting for its time: it is saved with no next run, so no tick can pick it up, and
    its time is allowed to be now. The caller runs it at once."""
    now = now or timezone.now()
    user = _require_member(ctx)
    _require_plan(user)
    clean = _clean_name(name)
    effort = clean_ai_effort(ai_effort)
    cleaned_params = clean_params(job, params)
    target = _clean_target(ctx, deliver_channel_id, job, cleaned_params)
    rec = clean_recurrence(
        frequency=frequency,
        time_of_day=time_of_day,
        weekdays=weekdays,
        month_day=month_day,
        run_date=run_date,
    )
    zone = user_zone(user)
    first = None if immediate else _first_run(rec, zone, now)
    with transaction.atomic():
        # Two creates at once must not both slip under the cap: take the member's row.
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        if immediate:
            _drop_failed_right_away(ctx, clean, job, cleaned_params)
        cap = schedule_limit(user.pk)
        used = MemberSchedule.objects.filter(user_id=user.pk).count()
        if used >= cap:
            raise ScheduleLimitReached(
                f"Your plan allows {cap} schedules, and you have {used}. Delete one you no "
                "longer need, or upgrade your plan. Your existing schedules keep running.",
                limit=cap,
                used=used,
            )
        cleaned_params = _check_params_for_owner(ctx, job, cleaned_params, None)
        _refuse_duplicate(ctx, name=clean, job=job, params=cleaned_params, rec=rec, target=target)
        return MemberSchedule.objects.create(
            user_id=user.pk,
            team_id=ctx.team_id,
            name=clean,
            job=job,
            params=cleaned_params,
            deliver_channel_id=target,
            frequency=rec.frequency,
            weekdays=list(rec.weekdays),
            month_day=rec.month_day,
            run_date=rec.run_date,
            time_of_day=rec.time_of_day,
            enabled=True,
            tz_used=zone.key,
            next_run_at=first,
            deliver_push=bool(deliver_push),
            ai_effort=effort,
        )


def _drop_failed_right_away(ctx: ToolContext, name: str, job: str, params: dict[str, Any]) -> None:
    """Remove the rows "Run right away" left behind for a background run that failed and made
    nothing, under this name: otherwise trying again would be refused as a duplicate name
    (MIP-2457). Only a try again of the SAME report (job and parameters) replaces one; a paused
    schedule, and a row that ever produced a report or a page, are kept."""
    key = _name_key(name)
    for row in MemberSchedule.objects.filter(
        user_id=ctx.user_id,
        team_id=ctx.team_id,
        frequency=FREQ_ONCE,
        job=job,
        enabled=True,
        next_run_at__isnull=True,
        claimed_at__isnull=True,
        last_status=STATUS_FAILED,
    ):
        same = all((row.params or {}).get(k) == v for k, v in params.items())
        if same and _name_key(row.name) == key and not row.last_result and not row.output:
            row.delete()


def create_and_run(
    ctx: ToolContext, *, frequency: Any, now: datetime | None = None, **fields: Any
) -> tuple[MemberSchedule, dict[str, Any]]:
    """A one-time schedule that runs RIGHT NOW (MIP-2402): "Run right away" in the form. Returns
    the schedule and what the run gave (as `run_now`).

    The server picks the date and the time (now, in the member's zone); whatever the caller sent
    for them is ignored. The schedule is saved with no next run and then run by hand, which ends a
    one-time schedule, so the tick can never file it a second time (MIP-2403). It stays in the
    list as a finished one-time schedule, with its run time.

    A run that failed in the request leaves NO schedule behind and the sentence is the error:
    there is nothing to wait for, and the form still holds what the member typed, so they can
    press again. A report whose AI part was skipped (no credits) did not fail: it is the plain
    report. A slow job answers `started` (MIP-2457) and the row stays: it is where the outcome
    is written, and a failed one is replaced by the next try under the same name.
    Everything `create` refuses (the plan, the count, the checked parameters, a name or a
    content already used by another of the member's schedules) is refused here before anything
    runs. The finished row keeps its name, so the same report right away again needs another
    name, or the finished row deleted (MIP-2407)."""
    if str(frequency or "").strip().lower() != FREQ_ONCE:
        raise ScheduleError("Run right away is only for a one-time schedule.")
    now = now or timezone.now()
    local = now.astimezone(user_zone(_require_member(ctx)))
    for own in ("run_date", "time_of_day", "weekdays", "month_day", "immediate"):
        fields.pop(own, None)
    sched = create(
        ctx,
        frequency=FREQ_ONCE,
        run_date=local.date().isoformat(),
        time_of_day=local.strftime("%H:%M"),
        now=now,
        immediate=True,
        **fields,
    )
    try:
        out = run_now(ctx, sched.pk, now=now)
    except ScheduleError:
        delete(ctx, sched.pk)
        raise
    if out["status"] == STATUS_FAILED:
        delete(ctx, sched.pk)
        raise ScheduleError(out["error"] or "The report could not be made. Try again.")
    sched.refresh_from_db()
    return sched, out


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
    _require_plan(user)
    with transaction.atomic():
        # The member's row first, as `create` does: two edits (or an edit and a create) that
        # would each make the same schedule must not both pass the duplicate check.
        CustomUser.objects.select_for_update().filter(pk=user.pk).first()
        sched = _get(ctx, schedule_id, lock=True)
        keep_claim = _held_by_hand_run(sched, now)  # asked before the edit moves the next run
        was_name = _name_key(sched.name)
        was_content = (
            sched.job,
            dict(sched.params or {}),
            _recurrence_of(sched),
            sched.deliver_channel_id,
        )
        dirty = ["claimed_at", "heartbeat_at", "ts_updated_at"]
        if "name" in changes:
            sched.name = _clean_name(changes["name"])
            dirty.append("name")
        if "job" in changes or "params" in changes:
            job = changes.get("job", sched.job)
            sched.params = _check_params_for_owner(
                ctx, job, clean_params(job, changes.get("params", sched.params)), sched.pk
            )
            if job != sched.job:
                # The living note a dashboard keeps belongs to THAT report. Left behind, switching
                # back would write over it, and a new place would be refused as a "move" (MIP-2405).
                # The page itself stays, as an ordinary note.
                sched.output = {}
                dirty.append("output")
            sched.job = job
            dirty += ["job", "params"]
        if {"deliver_channel_id", "job", "params"} & set(changes):
            # A new report for the same chat, or a new chat for the same report: the rule that
            # decides who may read it is asked again, of what the schedule is now.
            sched.deliver_channel_id = _clean_target(
                ctx,
                changes.get("deliver_channel_id", sched.deliver_channel_id),
                sched.job,
                dict(sched.params or {}),
            )
            dirty.append("deliver_channel_id")
        if "deliver_push" in changes:
            sched.deliver_push = bool(changes["deliver_push"])
            dirty.append("deliver_push")
        if "ai_effort" in changes:
            sched.ai_effort = clean_ai_effort(changes["ai_effort"])
            dirty.append("ai_effort")
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
        # Refused only when what this edit CHANGED now equals another row: rows that were
        # already duplicated stay editable (deliver_push, a pause, a rename, a time that makes
        # them different), and an edit that leaves the name or the settings alone is not asked
        # about them.
        content = (
            sched.job,
            dict(sched.params or {}),
            _recurrence_of(sched),
            sched.deliver_channel_id,
        )
        _refuse_duplicate(
            ctx,
            name=sched.name,
            job=sched.job,
            params=dict(sched.params or {}),
            rec=content[2],
            target=sched.deliver_channel_id,
            check_name=_name_key(sched.name) != was_name,
            check_content=content != was_content,
        )
        # An edit takes over a TICK's run in progress: the tick's own write-back is conditional
        # on its claim, so it will find the claim gone and leave the member's edit alone. A live
        # "Run now" keeps its claim (MIP-2457): freeing it would let a second, billed run start.
        if keep_claim:
            dirty = [name for name in dirty if name not in ("claimed_at", "heartbeat_at")]
        else:
            sched.claimed_at = None
            sched.heartbeat_at = None
        sched.save(update_fields=sorted(set(dirty)))
        return sched


def set_enabled(
    ctx: ToolContext, schedule_id: Any, enabled: bool, *, now: datetime | None = None
) -> MemberSchedule:
    """Pause (`paused_reason` manual) or resume. Resuming forgets past failures and counts
    the next run from now."""
    now = now or timezone.now()
    user = _require_member(ctx)
    if enabled:
        _require_plan(user)  # pausing is always allowed; turning one on is a Core feature
    with transaction.atomic():
        sched = _get(ctx, schedule_id, lock=True)
        keep_claim = _held_by_hand_run(sched, now)
        if enabled:
            zone = user_zone(user)
            sched.next_run_at = _first_run(_recurrence_of(sched), zone, now)
            sched.tz_used = zone.key
            sched.enabled = True
            if sched.paused_reason == PAUSE_NOTE:
                # The dashboard note is gone: resuming means "make a new one" (MIP-2353).
                sched.output = {}
            sched.paused_reason = ""
            sched.consecutive_failures = 0
        else:
            sched.enabled = False
            sched.paused_reason = PAUSE_MANUAL
            sched.next_run_at = None
        if not keep_claim:  # as in `update`: a live "Run now" keeps its claim
            sched.claimed_at = None
            sched.heartbeat_at = None
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


def _execute(sched: MemberSchedule, *, run_at: datetime | None = None) -> tuple[str, str]:
    """Run the schedule's job as its owner and deliver the report. Returns `(status, error)`.

    Never raises for a job's own failure. The run is one savepoint: a job whose query fails
    would otherwise leave the connection aborted and fail every schedule after it.

    `run_at` is the SCHEDULED time of a tick's run. It keys the delivery, so a run the tick
    repeats after a crash files its report once. "Run now" passes none: each press is its own.
    """
    job = JOBS.get(sched.job)
    if job is None:
        return STATUS_FAILED, "That job no longer exists."
    ctx = ToolContext(team_id=str(sched.team_id), user_id=str(sched.user_id))
    try:
        if job.posts_as_owner:
            # A message, not a report (MIP-2482): nothing to run, it is posted as the owner.
            with transaction.atomic():
                schedule_delivery.post_as_owner(sched, run_at=run_at)
            return STATUS_OK, ""
        if sched.deliver_channel_id and job.ai_applies(sched.params):
            # A report that spends AI usage and goes to a chat: ask the audience rule BEFORE the
            # model is called (MIP-2196). Someone added to the chat since the schedule was saved
            # stops the run here, and nothing is spent on a report that could not be posted.
            schedule_delivery.check_chat_target(sched)
        # The job and the delivery are two savepoints, not one (MIP-1210): a job that calls a
        # model has recorded what it spent (`AiSpendEvent`, `AiRequestCost`) by the time it
        # returns, and a delivery that then fails must not roll that record back. The money is
        # spent either way; losing its row would be spend nobody can see. Jobs write nothing to
        # the workspace, so nothing else needs the two to stand or fall together.
        if job.unwrapped:
            # A job that holds a long model loop (a skill run, MIP-2045) is NOT wrapped: inside a
            # savepoint every spend row it records would wait on the end of the loop, and a process
            # that dies mid-run (a deploy, an OOM) would roll back the record of money the provider
            # already charged. Such a job writes nothing else, records its spend as it goes, and
            # turns its own failures into a report, so it needs no savepoint of its own.
            report: Report = job.run(ctx, _run_params(sched, job))
        else:
            with transaction.atomic():
                report = job.run(ctx, _run_params(sched, job))
        _check_report(report)
        if job.delivers_note:
            # A dashboard (MIP-2353): save its ONE living note first, then file the report with a
            # link to it. The note is its own savepoint (MIP-2355): a chat that some reader cannot
            # open the note for refuses the NOTICE, and the note must stand so its owner has
            # something to share. A repeat is keyed (`output["run"]`) and replaces, never doubles.
            from origin.services.schedule_dashboard_note import deliver_note  # noqa: PLC0415

            with transaction.atomic():
                report = deliver_note(sched, report, run_at=run_at)
        with transaction.atomic():
            schedule_delivery.deliver(sched, report, run_at=run_at)
    except ScheduleRunError as exc:
        # A run that cannot go on until the member acts (a deleted dashboard note) pauses the
        # schedule: `_process` reads this after the run.
        sched._pause_reason = getattr(exc, "reason", "")
        return STATUS_FAILED, exc.public[:MAX_ERROR_CHARS]
    except Exception as exc:  # noqa: BLE001 - one schedule's failure is that schedule's
        return STATUS_FAILED, describe_exception(exc)[:MAX_ERROR_CHARS]
    return STATUS_OK, ""


def _run_params(sched: MemberSchedule, job) -> dict[str, Any]:
    """What the job is run with: the schedule's own parameters, and for a job that uses AI the
    schedule's effort level under a key no member can send (`AI_EFFORT_KEY`). An empty or
    unknown level is the job's default (`report_ai.effort.resolve`)."""
    params = dict(sched.params or {})
    if sched.deliver_channel_id:
        # Posted in a chat: the job writes for everyone who reads it, not for its owner.
        params[SHARED_KEY] = True
        if job.share_team or job.share_chats:
            # Who reads that chat NOW: the job names only what each of them may read.
            channel = schedule_audience.load_target(
                sched.user_id, sched.team_id, sched.deliver_channel_id
            )
            if channel is not None:
                params[AUDIENCE_KEY] = sorted(schedule_audience.readers(channel))
    if job.delivers_note and job.note_target is not None:
        # The saved page is read by everyone its place lets in, as it sits NOW (MIP-2354): the job
        # names only what every one of them may open. A place that cannot be read fails the run.
        output = sched.output or {}
        note_id = output.get("note_id") if output.get("target") == "note" else None
        found: set[str] | None = None
        if isinstance(note_id, int):
            try:
                found = schedule_audience.saved_note_readers(sched.user_id, sched.team_id, note_id)
            except schedule_audience.NoteTargetError:
                found = (
                    None  # the note is gone: the delivery step pauses the schedule with a reason
                )
        try:
            if found is None:
                found = schedule_audience.note_readers(
                    sched.user_id, sched.team_id, job.note_target(sched.params or {})
                )
        except schedule_audience.NoteTargetError as exc:
            raise ScheduleRunError(str(exc)) from None
        params[AUDIENCE_KEY] = sorted(found)
    if job.uses_ai:
        params[AI_EFFORT_KEY] = sched.ai_effort or ""
    return params


# "Run now" on a job that spends AI usage: a few a day per schedule, then the sentence below.
# Each press is a billed model call made inside a web request, and "never spend silently" is as
# much about a button pressed in a loop as about a cron. A scheduled run is not counted, and a
# run that used no AI (turned off, cached, the model not reachable) is not either.
AI_RUN_NOW_PER_DAY = 3


def _ai_run_now_count(sched: MemberSchedule, now: datetime) -> int:
    """How many hand-made reports of this schedule spent AI usage since the member's midnight."""
    from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems  # noqa: PLC0415

    midnight = now.astimezone(user_zone(sched.user)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    in_inbox = (
        InboxItems.objects.filter(
            receiver_id=sched.user_id,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_optionals__schedule_id=str(sched.id),
            item_optionals__has_key="ai",
            ts_created_at__gte=midnight,
        )
        .exclude(item_optionals__has_key="run_at")
        .count()
    )
    return in_inbox


def _require_ai_run_now_room(sched: MemberSchedule, now: datetime) -> None:
    job = JOBS.get(sched.job)
    if job is None or not job.ai_applies(sched.params):
        return
    if _ai_run_now_count(sched, now) >= AI_RUN_NOW_PER_DAY:
        raise ScheduleError(
            f"This report uses AI each time it runs, and you have already run it by hand "
            f"{AI_RUN_NOW_PER_DAY} times today. It still runs on its schedule. Turn off its AI "
            "sentences to run it as often as you like."
        )


def runs_in_background(sched: MemberSchedule) -> bool:
    """Whether "Run now" of this schedule must not run inside the web request (MIP-2457): a long
    model loop, a dashboard page, or anything that spends AI usage. `SCHEDULE_RUN_NOW_MODE` =
    `sync` is the rollback: everything runs in the request, as before."""
    if getattr(settings, "SCHEDULE_RUN_NOW_MODE", "subprocess") == "sync":
        return False
    job = JOBS.get(sched.job)
    return job is not None and (job.unwrapped or job.delivers_note or job.ai_applies(sched.params))


def running_since(sched: MemberSchedule, now: datetime | None = None) -> datetime | None:
    """When the run that holds this schedule started, or None when no live run holds it."""
    if sched.claimed_at is None:
        return None
    now = now or timezone.now()
    if sched.heartbeat_at is not None:
        alive = sched.heartbeat_at >= now - HEARTBEAT_STALE
    else:
        alive = sched.claimed_at >= now - STALE_CLAIM
    return sched.claimed_at if alive else None


def _held_by_hand_run(sched: MemberSchedule, now: datetime) -> bool:
    """Whether a live "Run now" holds the row. A tick's claim is on a DUE row; any other is a
    hand run's."""
    if sched.next_run_at is not None and sched.next_run_at <= now:
        return False
    return running_since(sched, now) is not None


def _other_background_runs(pk: Any, now: datetime) -> int:
    """How many other hand runs are alive in a child process. Counted in the database, so a web
    worker that was recycled still knows about the children its predecessor started."""
    live = MemberSchedule.objects.filter(
        Q(next_run_at__isnull=True) | Q(next_run_at__gt=now),
        claimed_at__isnull=False,
        heartbeat_at__gte=now - HEARTBEAT_STALE,
    ).exclude(pk=pk)
    return sum(1 for row in live if runs_in_background(row))


def _busy_sentence(sched: MemberSchedule) -> str:
    """Why a claimed schedule cannot be run now, saying only what is known."""
    if sched.claimed_at is None:  # released between our try and this read
        return "This schedule is running right now. Try again in a minute."
    zone = user_zone(sched.user)
    started = sched.claimed_at.astimezone(zone).strftime("%H:%M")
    if sched.heartbeat_at is None:
        # No heartbeat to go by: only the time it will be taken over is known (rounded up).
        again = (sched.claimed_at + STALE_CLAIM + timedelta(seconds=59)).astimezone(zone)
        return (
            f"This schedule is still marked as running: a run that started at {started} has not "
            f"finished. You can run it again at {again:%H:%M}."
        )
    return (
        f"This schedule is running: a run that started at {started} has not finished. Its "
        "report arrives in your Inbox (or the chat) when it is done. If that run stopped, you "
        "can run it again in about a minute."
    )


def _beat(pk: Any, claim: datetime) -> bool:
    """Say that the run holding `claim` is alive. False when the claim is no longer ours."""
    return bool(
        MemberSchedule.objects.filter(pk=pk, claimed_at=claim).update(heartbeat_at=timezone.now())
    )


@contextmanager
def _heartbeat(pk: Any, claim: datetime) -> Iterator[None]:
    """Refresh the claim's heartbeat from a second thread while the run goes on. It has its own
    database connection, so it writes whatever the run's transactions are doing."""
    every = getattr(settings, "SCHEDULE_HEARTBEAT_EVERY_SECONDS", HEARTBEAT_EVERY_SECONDS)
    if every <= 0:  # the tests: a second connection cannot see a test's uncommitted rows
        yield
        return
    stop = threading.Event()

    def beat() -> None:
        try:
            while not stop.wait(every):
                try:
                    if not _beat(pk, claim):
                        return
                except Exception as exc:  # noqa: BLE001 - try again at the next beat
                    log.warning(
                        "schedule_heartbeat_failed schedule=%s error=%s", pk, type(exc).__name__
                    )
                    connection.close()
        finally:
            connection.close()

    thread = threading.Thread(target=beat, daemon=True, name="schedule-heartbeat")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5)


def _release(pk: Any, claim: datetime) -> None:
    MemberSchedule.objects.filter(pk=pk, claimed_at=claim).update(
        claimed_at=None, heartbeat_at=None
    )


def _record_hand_run(sched: MemberSchedule, claim: datetime, status: str, error: str) -> None:
    """Write what a hand run gave and let go of the claim. The run is dated when it started."""
    fields: dict[str, Any] = {
        "last_run_at": claim,
        "last_status": status,
        "last_error": error,
        "ts_updated_at": timezone.now(),
    }
    if sched.frequency == FREQ_ONCE:
        # Written only while our claim stands. A time the member set during the run is kept:
        # the next run is cleared only if it is still the one this run started with.
        if status == STATUS_OK:
            MemberSchedule.objects.filter(
                pk=sched.pk, claimed_at=claim, next_run_at=sched.next_run_at
            ).update(next_run_at=None)
        MemberSchedule.objects.filter(pk=sched.pk, claimed_at=claim).update(
            claimed_at=None, heartbeat_at=None, **fields
        )
    else:
        MemberSchedule.objects.filter(pk=sched.pk).update(**fields)
        _release(sched.pk, claim)


def _run_claimed(sched: MemberSchedule, claim: datetime) -> tuple[str, str]:
    """Run a hand-claimed schedule and record it. The same in the request and in the child."""
    status, error = STATUS_FAILED, ""
    try:
        with _heartbeat(sched.pk, claim):
            status, error = _execute(sched)
    except RunTimedOut:
        error = RUN_TOO_LONG_SENTENCE
    finally:
        _record_hand_run(sched, claim, status, error)
    if status == STATUS_FAILED:
        log.warning(
            "schedule_run_now_failed schedule=%s job=%s error=%s", sched.pk, sched.job, error
        )
    return status, error


def run_claimed_by_id(schedule_id: Any, claim: datetime) -> bool:
    """The child process's entry (`schedule_run_now`): run the schedule `run_now` claimed. False,
    and nothing runs, when that claim no longer stands (edited, paused, deleted, taken over)."""
    if not _beat(schedule_id, claim):
        return False
    sched = (
        MemberSchedule.objects.select_related("user")
        .filter(pk=schedule_id, claimed_at=claim)
        .first()
    )
    if sched is None:
        return False
    _run_claimed(sched, claim)
    return True


def run_now(ctx: ToolContext, schedule_id: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Run one of the member's schedules right now, whatever the clock says.

    Records `last_*` like a run, but leaves the next run, the failure count and the pause
    alone: a test run does not use up today's run or push a schedule toward pausing.

    A one-time schedule is the exception (MIP-2403): its one run IS the report, so a run that
    went through ends it (no next run), and the tick never files the same report again at the
    set time. A run that failed leaves the set time standing.

    The row is claimed for the run, as a tick would, so a tick that comes while the job is going
    skips it and a second press is refused. A slow job (`runs_in_background`, MIP-2457) goes on
    in a child process and the answer is `{"status": "started", "started_at": ...}`.
    """
    now = now or timezone.now()
    _require_plan(_require_member(ctx))
    sched = _get(ctx, schedule_id)
    if sched.last_run_at is not None and timedelta(0) <= now - sched.last_run_at < RUN_NOW_COOLDOWN:
        wait = int((RUN_NOW_COOLDOWN - (now - sched.last_run_at)).total_seconds()) + 1
        raise ScheduleError(f"This schedule ran a moment ago. Try again in {wait} seconds.")
    _require_ai_run_now_room(sched, now)
    if not _claim_for_run_now(sched.pk, now):
        raise ScheduleError(_busy_sentence(_get(ctx, schedule_id)))
    out: dict[str, Any] = {"status": STATUS_STARTED, "error": "", "title": "", "items": 0}
    if runs_in_background(sched):
        if _other_background_runs(sched.pk, now) >= getattr(
            settings, "SCHEDULE_RUN_NOW_MAX_PROCS", 2
        ):
            _release(sched.pk, now)
            raise ScheduleError(schedule_runner.BUSY_SENTENCE)
        try:
            schedule_runner.start(sched.pk, now)
        except schedule_runner.RunnerUnavailable as exc:
            _release(sched.pk, now)
            raise ScheduleError(str(exc)) from None
        out["started_at"] = schedule_text.local_time(now, user_zone(sched.user))
        return out
    status, error = _run_claimed(sched, now)
    sched.refresh_from_db()
    out.update(status=status, error=error)
    if status == STATUS_OK:
        out["title"] = (sched.last_result or {}).get("title", "")
        out["items"] = (sched.last_result or {}).get("items", 0)
    return out


def fail_stale_hand_runs(now: datetime) -> int:
    """Record the hand runs that died (their heartbeat stopped): the failure on the row, the claim
    gone. A tick's own claim is on a DUE row, which the next tick takes over and runs; a row that
    is not due was claimed by "Run now", which is never retried."""
    gone = Q(claimed_at__isnull=False, heartbeat_at__lt=now - HEARTBEAT_STALE) & (
        Q(next_run_at__isnull=True) | Q(next_run_at__gt=now)
    )
    count = 0
    for pk, claim in list(MemberSchedule.objects.filter(gone).values_list("pk", "claimed_at")):
        changed = MemberSchedule.objects.filter(gone, pk=pk, claimed_at=claim).update(
            claimed_at=None,
            heartbeat_at=None,
            last_run_at=claim,
            last_status=STATUS_FAILED,
            last_error=RUN_STOPPED_SENTENCE,
            ts_updated_at=now,
        )
        if changed:
            log.warning("schedule_run_now_stopped schedule=%s", pk)
        count += changed
    return count


# ── the tick ────────────────────────────────────────────────────────────


@dataclass
class RunSummary:
    due: int = 0
    ran: int = 0
    failed: int = 0
    skipped_late: int = 0
    skipped_access: int = 0
    skipped_plan: int = 0
    claimed_elsewhere: int = 0
    rezoned: int = 0
    deferred: int = 0
    paused: int = 0
    max_late_seconds: int = 0


def _unclaimed(now: datetime) -> Q:
    return (
        Q(claimed_at__isnull=True)
        | Q(heartbeat_at__lt=now - HEARTBEAT_STALE)
        | Q(heartbeat_at__isnull=True, claimed_at__lt=now - STALE_CLAIM)
    )


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
        ).update(claimed_at=now, heartbeat_at=now)
        == 1
    )


def _claim_for_run_now(pk: Any, now: datetime) -> bool:
    """Take the row for a hand run, so no tick runs it meanwhile (and a second press is
    refused). Like `_claim`, but it asks neither for a due time nor for the schedule to be on:
    a paused one may be run by hand."""
    return (
        MemberSchedule.objects.filter(_unclaimed(now), pk=pk).update(
            claimed_at=now, heartbeat_at=now
        )
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
        claimed_at=None, heartbeat_at=None, ts_updated_at=now, **fields
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
    # `now` is when the TICK began; date the heartbeat from when this row is taken.
    _beat(row.pk, now)
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

    if not plan_allows(sched.user_id):
        # Free (MIP-1213): no report is filed. Kept, not deleted; `sync_plan` resumes it when
        # the plan comes back.
        summary.skipped_plan += 1
        _finish(
            sched,
            now,
            now,
            last_status=STATUS_SKIPPED_ACCESS,
            last_error="",
            enabled=False,
            paused_reason=PAUSE_PLAN,
            next_run_at=None,
        )
        log.info("schedule_paused schedule=%s reason=%s", sched.pk, PAUSE_PLAN)
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

    # `sched.next_run_at` is still the scheduled time here: `_finish` is what advances it.
    with _heartbeat(sched.pk, now):
        status, error = _execute(sched, run_at=sched.next_run_at)
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
        if reason := getattr(sched, "_pause_reason", ""):
            # The reason the run named wins over "repeated failures": the member's next step is
            # the one it states (MIP-2353).
            if fields.get("enabled", True):  # not already counted as paused by failures
                summary.paused += 1
            fields.update(enabled=False, paused_reason=reason, next_run_at=None)
            log.warning("schedule_paused schedule=%s reason=%s", sched.pk, reason)
    _finish(sched, now, now, **fields)


@dataclass
class PlanSync:
    paused: int = 0
    resumed: int = 0


def _pages(qs, size: int, max_pages: int):
    """Rows of `qs` in primary-key order, `size` at a time, up to `max_pages` pages: each page
    starts after the last row of the one before, so rows changed while we go are not met twice
    and a pass is bounded without ever looking only at the same first rows."""
    last = None
    for _ in range(max_pages):
        page = qs.order_by("pk")
        if last is not None:
            page = page.filter(pk__gt=last)
        rows = list(page[:size])
        if not rows:
            return
        yield from rows
        last = rows[-1].pk


def sync_plan(now: datetime | None = None, limit: int = 500, max_pages: int = 20) -> PlanSync:
    """Make the schedules follow the member's plan (MIP-1213), kept and never deleted.

    A schedule that is ON for a member whose plan does not include scheduled reports is paused
    with the reason `plan`; one paused for `plan` whose member has the plan again is resumed (the
    next run counted from now, failures forgotten). A schedule paused for any other reason
    (the member's own pause, repeated failures, left the team) is never touched, so a manual
    pause stays manual. Rows are read `limit` at a time, at most `max_pages` pages of each kind per
    pass (so a pass is bounded, and never looks only at the same first rows); the tick runs it every
    five minutes, which is also how rows seeded before the gate existed are paused after a deploy.
    """
    now = now or timezone.now()
    out = PlanSync()
    verdict: dict[str, bool] = {}

    def allowed(user_id: Any) -> bool:
        key = str(user_id)
        if key not in verdict:
            verdict[key] = plan_allows(key)
        return verdict[key]

    for row in _pages(MemberSchedule.objects.filter(enabled=True), limit, max_pages):
        if not allowed(row.user_id):
            changed = MemberSchedule.objects.filter(pk=row.pk, enabled=True).update(
                enabled=False,
                paused_reason=PAUSE_PLAN,
                next_run_at=None,
                claimed_at=None,
                heartbeat_at=None,
                ts_updated_at=now,
            )
            out.paused += changed
            if changed:
                log.info("schedule_paused schedule=%s reason=%s", row.pk, PAUSE_PLAN)
    held = MemberSchedule.objects.filter(enabled=False, paused_reason=PAUSE_PLAN)
    for row in _pages(held.select_related("user"), limit, max_pages):
        if not allowed(row.user_id) or _access_problem(row.user, str(row.team_id)):
            continue
        zone = user_zone(row.user)
        moment = next_run_after(row, now, zone)
        if moment is None:  # a one-off whose day has passed: nothing to resume
            continue
        changed = MemberSchedule.objects.filter(
            pk=row.pk, enabled=False, paused_reason=PAUSE_PLAN
        ).update(
            enabled=True,
            paused_reason="",
            consecutive_failures=0,
            next_run_at=moment,
            tz_used=zone.key,
            ts_updated_at=now,
        )
        out.resumed += changed
        if changed:
            log.info("schedule_resumed schedule=%s reason=%s", row.pk, PAUSE_PLAN)
    return out


def run_due(
    now: datetime | None = None, budget_seconds: float = 240, limit: int = 200
) -> RunSummary:
    """Run every schedule that has come due, oldest first, at most `limit` of them and
    stopping to pick up new ones once `budget_seconds` is spent (the rest are due again on
    the next tick)."""
    now = now or timezone.now()
    started = time.monotonic()
    try:
        fail_stale_hand_runs(now)
    except Exception:  # noqa: BLE001 - never stops the due runs
        log.exception("schedule_tick: could not record stopped runs")
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
