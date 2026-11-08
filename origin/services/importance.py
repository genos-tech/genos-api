"""What matters to a member, worked out from what they do (MIP-1087).

**A small, deterministic, explainable score. No model call, no stored score.** For each task the
member can open, a number of points is added up from named signals, and every point that was
added has a sentence ("6 from others, 2 in chat, on 4 of the last 7 days, rising"), so the
member can see exactly why an item is where it is, and disagree:

  =========  ==================================================  =====================
  signal     what it counts                                      points
  =========  ==================================================  =====================
  others     others' comments, changes and reactions             3 each, up to 6 events
  chat       messages in the chat thread linked to the task      2 each, up to 6
  notes      edits of the task's notes                           2 each, up to 6
  prs        PR events                                           2 each, up to 6
  mine       the member's own changes and comments               1 each, up to 6
  spread     days with activity (the last 7 days, and today)     2 per day, up to 7 days
  trend      rising or cooling (the 3 days against the 4 before) +6 / -6
  hints      due date, priority, Blocked: ALL WEAK               see `_hints`
  =========  ==================================================  =====================

The counts are the per-day rollups (`item_activity`, MIP-1084): saved actions only. There is no
time spent, no duration and no count of AI-app calls anywhere in this. Today, which no rollup
holds yet, is counted live with the same rules (`item_activity.compute`), never stored.

**The member tunes it.** Each signal is multiplied by a level the member chose: `off` 0, `low`
0.5, `normal` 1 (the default), `high` 2. They can also mark one task `not_important` (it is
never a top item or a suggestion), or `priority_is_right` (no "raise it?" for it). Both are
stored (`models/common/member_importance_models.py`) and are the ONLY things stored: the score
itself is recomputed on every read. A member who never sets anything is never asked to.

**Priority, due date and status are weak hints.** They add a few points and never decide on
their own more than a little activity does. When activity DISAGREES with them it becomes a
SUGGESTION ("busy all week but marked Low, raise it?"), computed on read, never stored, and
NEVER applied: nothing here writes a task.

**Private.** Every function takes the caller's `ToolContext` and reads and writes only that
member's rows. People appear only as a COUNT of other people on an item, never as names.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from origin.models.common.member_importance_models import (
    ALL_MARKS,
    MARK_NOT_IMPORTANT,
    MARK_ON_IT,
    MARK_PRIORITY_IS_RIGHT,
    MARK_SOURCES,
    MARKS,
    MemberImportanceMark,
    MemberImportanceTuning,
    purge_for_user,
)
from origin.models.common.user_models import CustomUser
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.tools.base import ToolContext, ToolError, wrap_workspace_content
from origin.services import item_activity, item_lines
from origin.services.user_time import zone_for_user_id

log = logging.getLogger(__name__)

purge = purge_for_user


class TaskNotFound(ToolError):
    """The task is not one the member can open (or does not exist): one answer for both."""


class RecordingOff(ToolError):
    """A write refused because the member switched recording off."""


# --- the signals ------------------------------------------------------------------------

# Per event, by the kind the rollups count; a kind stops adding after SATURATE events, so one
# noisy day cannot bury a quiet week.
KIND_POINTS = {"others": 3, "chat": 2, "notes": 2, "prs": 2, "mine": 1}
SATURATE = 6
SPREAD_POINTS = 2
TREND_POINTS = 6
SIGNALS = ("others", "mine", "chat", "notes", "prs", "spread", "trend", "hints")
LEVELS = {"off": 0.0, "low": 0.5, "normal": 1.0, "high": 2.0}
DEFAULT_LEVEL = "normal"
# What a member is told each signal is, in their words.
SIGNAL_WORDS = {
    "others": "what other people do on it: comments, changes, reactions",
    "mine": "what you do on it: your changes and comments",
    "chat": "messages in the chat thread linked to it",
    "notes": "edits of its notes",
    "prs": "pull-request events on it",
    "spread": "how many of the last seven days it was busy on",
    "trend": "whether it is heating up or cooling down",
    "hints": "its due date, priority and Blocked status (weak hints)",
}
# The same signals as a short title for a settings page (`SIGNAL_WORDS` is the sentence after it).
# Keys plus English: the page owns the translations, keyed by the signal.
SIGNAL_NAMES = {
    "others": "Other people",
    "mine": "You",
    "chat": "Chat",
    "notes": "Notes",
    "prs": "Pull requests",
    "spread": "Busy days",
    "trend": "Heating up or cooling down",
    "hints": "Due date, priority, Blocked",
}
# What each level does to a signal, for the same page.
LEVEL_WORDS = {
    "off": ("Don't count", "Genos ignores this signal."),
    "low": ("Count less", "This signal counts half as much."),
    "normal": ("Normal", "The default."),
    "high": ("Count more", "This signal counts twice as much."),
}
MARK_WORDS = {
    MARK_NOT_IMPORTANT: (
        "Not important",
        "Never offered as a top item or a suggestion, until you clear it. Reports leave it out "
        "until the task changes.",
    ),
    MARK_ON_IT: (
        "On it",
        "Reports leave it out for 7 days, or until the task changes.",
    ),
    MARK_PRIORITY_IS_RIGHT: (
        "Priority is right",
        "Genos stops asking whether to change its priority. Its ranking does not change.",
    ),
}
_KIND_WORDS = {
    "others": "from others",
    "mine": "of your own",
    "chat": "in chat",
    "notes": "note edits",
    "prs": "PR events",
}

# Below this a task is not worth a place in the top few. A single event from somebody else (3)
# or a due date tomorrow (3) is not; due today, or two events, is.
MIN_SCORE = 6
# The top items the briefing shows, and the most `get_my_importance` shows.
TOP_BRIEFING = 3
TOP_TOOL = 10
# Suggestions: "busy all week but marked low" needs this much activity on this many days.
RAISE_POINTS = 12
RAISE_DAYS = 3
MAX_SUGGESTIONS = 2
# Nothing due within this many days counts as "nothing due soon" for "still that important?".
QUIET_DUE_DAYS = 7
MAX_MARKS = 500
MARKED_SHOWN = 50
# Candidate tasks scored per call.
POOL = 300
_DONE = ("Closed", "Deleted")
_LOW = ("Low", "Minimal")
_HIGH = ("Critical", "High")
_PRIORITY_HINT = {"Critical": 4, "High": 3, "Normal": 1}


# --------------------------------------------------------------------------- #
# What the member set                                                          #
# --------------------------------------------------------------------------- #


def enabled(user_id: Any) -> bool:
    """The recording switch (`member_events_enabled`): off means nothing is learned and nothing
    is kept, this included."""
    return CustomUser.objects.filter(id=user_id, member_events_enabled=True).exists()


class Prefs:
    """What the caller set for one team: `marks` (`task_id -> mark`), `levels`
    (`signal -> level name`, only those that differ from the default) and `info` (`task_id ->
    (sig, ts)`: the task's state signature and the time when the member answered, MIP-1211;
    `sig` is None on a mark made before)."""

    def __init__(
        self,
        marks: dict[int, str],
        levels: dict[str, str],
        info: dict[int, tuple[str | None, datetime]] | None = None,
    ):
        self.marks = marks
        self.levels = levels
        self.info = info or {}

    @property
    def tuned(self) -> bool:
        return bool(self.levels)

    @property
    def any(self) -> bool:
        return bool(self.marks or self.levels)

    def factor(self, signal: str) -> float:
        return LEVELS[self.levels.get(signal, DEFAULT_LEVEL)]


def clean_levels(raw: Any) -> dict[str, str]:
    """The valid part of a stored `weights` value. A name or a level this code does not know
    (an old row after a signal was renamed) is dropped, never trusted."""
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in raw.items()
        if k in SIGNALS and isinstance(v, str) and v in LEVELS and v != DEFAULT_LEVEL
    }


def load(ctx: ToolContext) -> Prefs:
    me, team = str(ctx.user_id), str(ctx.team_id)
    marks: dict[int, str] = {}
    info: dict[int, tuple[str | None, datetime]] = {}
    for task_id, mark, sig, ts in MemberImportanceMark.objects.filter(
        user_id=me, team_id=team, mark__in=ALL_MARKS
    ).values_list("task_id", "mark", "sig", "ts"):
        marks[task_id] = mark
        info[task_id] = (sig, ts)
    row = MemberImportanceTuning.objects.filter(user_id=me, team_id=team).first()
    return Prefs(marks, clean_levels(row.weights) if row else {}, info)


def _lock_enabled_member(ctx: ToolContext) -> None:
    """Inside a transaction: take the user row lock the opt-out PATCH takes and refuse when
    recording is off. A write after the purge would break "off keeps nothing"."""
    user = CustomUser.objects.select_for_update().filter(pk=str(ctx.user_id)).first()
    if user is None or not user.member_events_enabled:
        raise RecordingOff(
            "Genos is not recording your activity, so there is nothing to rank and nothing "
            "is kept. Turn recording on first (Settings, What Genos remembers)."
        )


def _task_id(raw: Any) -> int:
    """The numeric id (the MCP layer has already turned a display id or URL into it). What is
    not a whole number is refused here; a number no task has (0, negative, past the database's
    range) is "not found" like any other, because `LabelAccess` leaves such an id out."""
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) < 19:
        raw = int(raw)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ToolError("`task_id` must be a task: its display id (PRJ-123) or numeric id.")
    return raw


def set_mark(
    ctx: ToolContext,
    raw_task_id: Any,
    mark: Any,
    *,
    allowed: tuple[str, ...] = MARKS,
    source: str | None = None,
) -> dict[str, Any]:
    """Mark a task `not_important` or `priority_is_right` (and `on_it` where the caller allows
    it: the REST door, MIP-1211), or `clear` the mark. Only a task the caller can open (the same
    words as one that does not exist, so nothing is given away). Answers with the mark that was
    there before (`previous`), so an Undo can put it back. The task's state signature and the
    source of the answer are kept with it, so a report can tell "until the task changes"."""
    if mark not in (*allowed, "clear"):
        raise ToolError(f"`mark` must be one of: {', '.join((*allowed, 'clear'))}.")
    task_id = _task_id(raw_task_id)
    task = LabelAccess(ctx).task(task_id)
    if task is None:
        raise TaskNotFound(f"Task {raw_task_id} not found.")
    me, team = str(ctx.user_id), str(ctx.team_id)
    from origin.services import report_memory  # noqa: PLC0415 - it reads this module

    now = timezone.now()
    sig = report_memory.signature_of_task(task, now.astimezone(zone_for_user_id(me)).date())
    with transaction.atomic():
        _lock_enabled_member(ctx)
        mine = MemberImportanceMark.objects.filter(user_id=me, team_id=team)
        previous = mine.filter(task_id=task_id).values_list("mark", flat=True).first()
        if mark == "clear":
            mine.filter(task_id=task_id).delete()
        else:
            if not mine.filter(task_id=task_id).exists() and mine.count() >= MAX_MARKS:
                raise ToolError(
                    f"You have marked {MAX_MARKS} tasks already. Clear some with mark 'clear' first."
                )
            MemberImportanceMark.objects.update_or_create(
                user_id=me,
                team_id=team,
                task_id=task_id,
                defaults={
                    "mark": mark,
                    "ts": now,
                    "sig": sig,
                    "source": source if source in MARK_SOURCES else None,
                },
            )
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "mark": None if mark == "clear" else mark,
        "previous": previous,
    }


def set_levels(ctx: ToolContext, weights: Any = None, reset: Any = None) -> dict[str, str]:
    """Change how much signals count (`weights`: `{signal: level}`), or put them all back
    (`reset`). Returns the member's levels that differ from the default afterwards. A
    signal set to `normal` is simply no longer stored."""
    if reset is not None and not isinstance(reset, bool):
        raise ToolError("`reset` must be true or false.")
    if weights is not None:
        if not isinstance(weights, dict):
            raise ToolError(
                f"`weights` must be an object of signal: level. Signals: {', '.join(SIGNALS)}."
            )
        if len(weights) > len(SIGNALS):
            raise ToolError(f"`weights` names more signals than there are ({', '.join(SIGNALS)}).")
        for name, level in weights.items():
            if name not in SIGNALS:
                raise ToolError(
                    f"Unknown signal {str(name)[:40]!r}. Signals: {', '.join(SIGNALS)}."
                )
            if not isinstance(level, str) or level not in LEVELS:
                raise ToolError(
                    f"Unknown level for `{name}`. Levels: {', '.join(LEVELS)} ({DEFAULT_LEVEL} is the default)."
                )
    if not weights and not reset:
        raise ToolError("Nothing to change: give `weights`, or `reset` true.")
    me, team = str(ctx.user_id), str(ctx.team_id)
    with transaction.atomic():
        _lock_enabled_member(ctx)
        row = MemberImportanceTuning.objects.filter(user_id=me, team_id=team).first()
        current = {} if reset or row is None else clean_levels(row.weights)
        for name, level in (weights or {}).items():
            if level == DEFAULT_LEVEL:
                current.pop(name, None)
            else:
                current[name] = level
        if not current:
            MemberImportanceTuning.objects.filter(user_id=me, team_id=team).delete()
        else:
            MemberImportanceTuning.objects.update_or_create(
                user_id=me, team_id=team, defaults={"weights": current, "ts": timezone.now()}
            )
    return current


def export_rows(user_id: str) -> dict[str, list[dict[str, Any]]]:
    """The member's own marks and tuning for their account export. Marks go through the same
    rule as a read: a task the member cannot open now is not listed."""
    marks = list(MemberImportanceMark.objects.filter(user_id=user_id).order_by("ts", "mark_id"))
    readable: dict[str, set[int]] = {}
    for team_id in {str(m.team_id) for m in marks}:
        access = LabelAccess(ToolContext(team_id=team_id, user_id=str(user_id)))
        readable[team_id] = set(
            access.tasks({m.task_id for m in marks if str(m.team_id) == team_id})
        )
    return {
        "marks": [
            {
                "team_id": str(m.team_id),
                "task_id": m.task_id,
                "mark": m.mark,
                "marked_at": m.ts.isoformat(),
                "source": m.source,
            }
            for m in marks
            if m.task_id in readable[str(m.team_id)]
        ],
        "tuning": [
            {
                "team_id": str(t.team_id),
                "levels": clean_levels(t.weights),
                "changed_at": t.ts.isoformat(),
            }
            for t in MemberImportanceTuning.objects.filter(user_id=user_id).order_by(
                "ts", "tuning_id"
            )
        ],
    }


# --------------------------------------------------------------------------- #
# The score                                                                    #
# --------------------------------------------------------------------------- #


class Scored:
    """One task's score and why. `activity` is the points that came from activity alone (the
    suggestions read it); `days` how many days it was busy on; `counts` its events by kind."""

    def __init__(
        self,
        task: TaskMaster,
        score: float,
        reasons: list[str],
        activity: float,
        days: int,
        counts: Counter,
        trend: str,
    ):
        self.task = task
        self.counts = counts
        self.score = score
        self.reasons = reasons
        self.activity = activity
        self.days = days
        self.trend = trend

    @property
    def shown_score(self) -> int | float:
        value = round(self.score, 1)
        return int(value) if value == int(value) else value


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _activity_reason(counts: Counter, prefs: Prefs, days: int, trend: str) -> str:
    parts = [f"{counts[k]} {_KIND_WORDS[k]}" for k in KIND_POINTS if counts[k] and prefs.factor(k)]
    text = f"{', '.join(parts)}, on {days} of the last 7 days"
    if trend in ("rising", "cooling") and prefs.factor("trend"):
        text += f", {trend}"
    return text


def _hints(task: TaskMaster, today: date) -> tuple[float, list[str]]:
    """Due date and Blocked, in points and words (priority is added by the caller, only when
    something else scored)."""
    points, reasons = 0.0, []
    if task.due_date is not None:
        days = (task.due_date - today).days
        if days < 0:
            points += min(6 - days, 12)
            reasons.append(f"overdue by {_plural(-days, 'day')}")
        elif days == 0:
            points += 6
            reasons.append("due today")
        elif days == 1:
            points += 3
            reasons.append("due tomorrow")
    return points, reasons


def effective_series(daily: list[dict[str, int]] | None, prefs: Prefs) -> list[int] | None:
    """The per-day totals counting only the kinds the member has not turned off, so spread and
    trend follow what they chose to count (`None` when there is no per-day detail)."""
    if daily is None:
        return None
    on = [k for k in KIND_POINTS if prefs.factor(k) > 0]
    return [sum(day.get(k, 0) for k in on) for day in daily]


def score_task(
    task: TaskMaster,
    today: date,
    prefs: Prefs,
    counts: Counter | None = None,
    series: list[int] | None = None,
    today_counts: Counter | None = None,
) -> Scored:
    """The score of one task and the reasons for it. Pure: the same input gives the same
    answer. `counts` and `series` are its last seven complete days (by kind, and per day), and
    `today_counts` what has happened since local midnight."""
    counts = Counter(counts or {})
    for kind, n in (today_counts or {}).items():
        counts[kind] += n
    series = series or [0] * item_activity.WEEK_DAYS
    today_on = any(n and prefs.factor(k) > 0 for k, n in (today_counts or {}).items())
    days = sum(1 for n in series if n) + (1 if today_on else 0)
    days = min(days, item_activity.WEEK_DAYS)
    kind_points = {
        k: KIND_POINTS[k] * min(counts[k], SATURATE) * prefs.factor(k) for k in KIND_POINTS
    }
    activity = sum(kind_points.values())
    trend = item_activity.trend(series) if any(series) else "quiet"
    reasons: list[str] = []

    hint_factor = prefs.factor("hints")
    hint_points, hint_reasons = _hints(task, today)
    hint_points *= hint_factor
    score = hint_points
    if hint_points:
        reasons += hint_reasons

    if activity > 0:
        spread = SPREAD_POINTS * days * prefs.factor("spread")
        trend_points = {"rising": TREND_POINTS, "cooling": -TREND_POINTS}.get(
            trend, 0
        ) * prefs.factor("trend")
        activity += spread + trend_points
        score += activity
        reasons.append(_activity_reason(counts, prefs, days, trend))
    if task.status == "Blocked" and hint_factor:
        score -= 5 * hint_factor
        reasons.append("currently Blocked: clear what blocks it first")
    priority = _PRIORITY_HINT.get(task.priority or "", 0) * hint_factor
    if priority and score > 0:
        score += priority
        reasons.append(f"priority {task.priority}")
    return Scored(task, score, reasons, activity, days, counts, trend)


# --------------------------------------------------------------------------- #
# Ranking, suggestions                                                         #
# --------------------------------------------------------------------------- #


def today_counts(ctx: ToolContext, now: datetime) -> dict[int, Counter]:
    """What has happened since the member's local midnight, per task, with the rules the
    rollups use (it is the rollups' own `compute` for one day, never stored). A failure is
    logged and means "nothing counted today", never a failed briefing."""
    try:
        today = now.astimezone(zone_for_user_id(str(ctx.user_id))).date()
        found = item_activity.compute(ctx, [today])
    except Exception:  # noqa: BLE001 - the importance view never fails a briefing
        log.warning("Today's activity not counted for importance", exc_info=True)
        return {}
    return {
        task_id: Counter({k: v for k, v in row.items() if k in KIND_POINTS})
        for (task_id, _day), row in found.items()
    }


def engaged(week: item_activity.WeekSeries | None) -> bool:
    """Whether the briefing ranks by importance for this member: a week of activity has really
    been recorded. Anyone else keeps the plain ranking (minus what they marked not important),
    because a score with no activity under it would only throw away what that ranking knows."""
    return bool(week and week.complete)


def rank(
    ctx: ToolContext,
    now: datetime,
    prefs: Prefs,
    week: item_activity.WeekSeries | None,
    pool: list[TaskMaster],
    project_id: int | None = None,
) -> tuple[list[Scored], dict[int, Counter]]:
    """Every candidate with a score worth showing, best first, and what happened today per
    task. `pool` is the member's open tasks as the briefing already knows them; the tasks of
    their week are added. Marked `not_important` tasks never appear."""
    zone = zone_for_user_id(str(ctx.user_id))
    today = now.astimezone(zone).date()
    candidates: dict[int, TaskMaster] = {t.task_id: t for t in pool}
    if week is not None:
        for task_id, task in week.readable.items():
            if task.status in _DONE or task.is_init_task:
                continue
            if project_id is not None and task.project_id != project_id:
                continue
            candidates.setdefault(task_id, task)
    live = today_counts(ctx, now)
    access = LabelAccess(ctx)
    extra = {t for t in live if t not in candidates}
    if extra:
        for task_id, task in access.tasks(extra).items():
            if task.status in _DONE or task.is_init_task:
                continue
            if project_id is not None and task.project_id != project_id:
                continue
            candidates[task_id] = task
    scored = []
    for task_id, task in candidates.items():
        if prefs.marks.get(task_id) == MARK_NOT_IMPORTANT:
            continue
        counts = week.kinds.get(task_id) if week else None
        series = (effective_series(week.daily.get(task_id), prefs) if week else None) or (
            week.series.get(task_id) if week else None
        )
        scored.append(score_task(task, today, prefs, counts, series, live.get(task_id)))
    scored = [s for s in scored if s.score >= MIN_SCORE]
    scored.sort(key=lambda s: (-s.score, -s.task.ts_updated_at.timestamp(), s.task.task_id))
    return scored[:POOL], live


def suggestions(
    ctx: ToolContext,
    now: datetime,
    prefs: Prefs,
    week: item_activity.WeekSeries | None,
    pool: list[TaskMaster],
    scored: list[Scored],
    live: dict[int, Counter],
) -> list[dict[str, Any]]:
    """Where activity and the fields disagree, as a question to the member. Computed on read,
    never stored, never applied: nothing here changes a task."""
    zone = zone_for_user_id(str(ctx.user_id))
    today = now.astimezone(zone).date()
    out: list[dict[str, Any]] = []
    raise_it = [
        s
        for s in scored
        if (s.task.priority or "") in _LOW
        and s.activity >= RAISE_POINTS
        and s.days >= RAISE_DAYS
        and s.task.status != "Blocked"
        and prefs.marks.get(s.task.task_id) is None
    ]
    raise_it.sort(key=lambda s: (-s.activity, s.task.task_id))
    for s in raise_it:
        busy = _activity_reason(s.counts, prefs, s.days, s.trend)
        out.append(
            _suggestion(
                s.task,
                "raise_priority",
                f"busy all week ({busy}) but marked {s.task.priority}: raise it?",
            )
        )
    if week is not None and week.complete:
        quiet = [
            t
            for t in pool
            if (t.priority or "") in _HIGH
            and t.task_id not in week.series
            and t.task_id not in live
            and t.status not in (*_DONE, "Blocked")
            and prefs.marks.get(t.task_id) is None
            and (t.due_date is None or (t.due_date - today).days > QUIET_DUE_DAYS)
        ]
        quiet.sort(key=lambda t: (0 if t.priority == "Critical" else 1, t.ts_updated_at, t.task_id))
        for task in quiet:
            out.append(
                _suggestion(
                    task,
                    "review_priority",
                    f"marked {task.priority} but nothing has happened on it in the last 7 days: "
                    "still that important?",
                )
            )
    return out[:MAX_SUGGESTIONS]


def _suggestion(task: TaskMaster, kind: str, text: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "task_id": task.task_id,
        "display_id": task.display_id,
        "title": wrap_workspace_content(task.title or ""),
        "priority": task.priority,
        "suggestion": text,
    }


def people_counts(ctx: ToolContext, now: datetime, tasks: list[TaskMaster]) -> dict[int, int]:
    """`task_id -> how many OTHER people did something on it in the last 7 days`: a count, never
    a name. One read of the lines' sources for all `tasks`; a failure gives no counts."""
    if not tasks:
        return {}
    me = str(ctx.user_id)
    try:
        events, _capped = item_lines.read_events(
            ctx, LabelAccess(ctx), {t.task_id: t for t in tasks}, now - timedelta(days=7), now
        )
    except Exception:  # noqa: BLE001 - a count is never a reason to fail
        log.warning("People on items not counted for importance", exc_info=True)
        return {}
    return {
        task_id: len(
            {str(e.actor) for e in found if e.actor and str(e.actor) != me and e.source != "app"}
        )
        for task_id, found in events.items()
    }


def shaped(
    s: Scored,
    row: dict[str, Any],
    people: dict[int, int] | None = None,
    structured: bool = False,
) -> dict[str, Any]:
    """An item as the briefing and the tool show it: the task's own row, the score and why.
    `structured` (the settings page) adds `why`: the same reasons as numbers, so a page can say
    them in the member's language; the sentences in `reasons` are English only."""
    reasons = list(s.reasons)
    n = (people or {}).get(s.task.task_id, 0)
    if n and sum(s.counts.values()):
        reasons.append(f"{_plural(n, 'other person')} involved")
    out = {**row, "score": s.shown_score, "reasons": reasons}
    if structured:
        out["why"] = {
            **{kind: s.counts[kind] for kind in KIND_POINTS},
            "days": s.days,
            "trend": s.trend,
            "people": n if sum(s.counts.values()) else 0,
        }
    return out


def excluded(prefs: Prefs) -> set[int]:
    """The tasks the member marked not important: never a top item, in either ranking."""
    return {task_id for task_id, mark in prefs.marks.items() if mark == MARK_NOT_IMPORTANT}


def briefing_block(prefs: Prefs, found: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The briefing's additive `importance` key: what is offered, and how much the member has
    set. None when there is nothing to say."""
    marked = sum(1 for m in prefs.marks.values() if m == MARK_NOT_IMPORTANT)
    if not (found or marked or prefs.tuned):
        return None
    return {"suggestions": found, "marked_not_important": marked, "tuned": prefs.tuned}


# --------------------------------------------------------------------------- #
# The member's own view                                                        #
# --------------------------------------------------------------------------- #

_OFF_NOTE = (
    "Genos is not recording your activity (Settings, What Genos remembers), so nothing is "
    "learned about what matters to you and nothing is kept."
)


def describe(
    ctx: ToolContext, limit: Any = None, project_id: Any = None, structured: bool = False
) -> dict[str, Any]:
    """`get_my_importance`: the caller's top items with the reason for each, the suggestions,
    how each signal is tuned and what they marked. Brings the per-day table up to yesterday
    first, like the briefing. `structured` is the settings page's read (see `shaped`)."""
    from origin.services import briefing  # noqa: PLC0415 - it reads item_lines, which reads this module

    shown = TOP_TOOL
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= TOP_TOOL:
            raise ToolError(f"`limit` must be a whole number from 1 to {TOP_TOOL}.")
        shown = limit
    if not enabled(ctx.user_id):
        return {
            "enabled": False,
            "note": _OFF_NOTE,
            "top": [],
            "suggestions": [],
            "tuning": [],
            "marked": [],
            "__summary__": "Recording is off: nothing is learned or kept.",
        }
    now = timezone.now()
    project = briefing._check_project(ctx, project_id)
    _section, week = item_activity.briefing_data(ctx, now)
    prefs = load(ctx)
    pool = briefing._suggestion_pool(ctx, [], project)
    scored, live = rank(ctx, now, prefs, week, pool, project)
    top = scored[:shown]
    people = people_counts(ctx, now, [s.task for s in top])
    today = now.astimezone(zone_for_user_id(str(ctx.user_id))).date()
    found = suggestions(ctx, now, prefs, week, pool, scored, live)
    marked_tasks = LabelAccess(ctx).tasks(set(prefs.marks))
    marked = [
        {
            "task_id": t.task_id,
            "display_id": t.display_id,
            "title": wrap_workspace_content(t.title or ""),
            "mark": prefs.marks[t.task_id],
        }
        for t in sorted(marked_tasks.values(), key=lambda t: t.task_id)[:MARKED_SHOWN]
    ]
    history = "none" if week is None else ("full" if week.complete else "partial")
    return {
        "enabled": True,
        "history": history,
        "top": [shaped(s, briefing._task_row(s.task, today), people, structured) for s in top],
        "suggestions": found,
        "tuning": [
            {
                "signal": name,
                "level": prefs.levels.get(name, DEFAULT_LEVEL),
                "means": SIGNAL_WORDS[name],
            }
            for name in SIGNALS
        ],
        "levels": list(LEVELS),
        "marked": marked,
        "__summary__": (
            f"{len(top)} item(s) matter most to you now; {len(found)} suggestion(s); "
            f"{len(marked)} marked."
        ),
    }
