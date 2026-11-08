"""A report that does not repeat itself, and answers the member can give it (MIP-1211).

A report that lists every task matching a rule is ignored. So a task row is HELD BACK from a
report when nothing about it changed since the member's last report, and counted instead
("4 unchanged since your last report"). The report is good with no taps at all; the two answers
a member can give a row ("On it", "Not important") only make it shorter.

## What is remembered, and why there is no new table

The member's previous report of the SAME JOB in the same team, which is already an Inbox item
(`InboxItems`, type 10). It carries `item_optionals["seen"]`: `{task_id: signature}` for every
task row that report listed or held back as unchanged, as it stood then. Nothing else is
stored for this: the memory is as old, as private and as deletable as the report itself (30 days,
`prune_schedule_reports`; the account deletion and export already cover it).

* a deleted report still counts (deleting one does not make its items come back);
* a report counts only once it was OPENED (`is_read`): one nobody opened has not reported
  anything. The newest opened report within 30 days is the memory; none = every task is new;
* NOT the recording switch: reports are deliverables, not activity data, so turning recording
  off does not purge them (the answers below ARE purged by it).

## The signature

`status|priority|due date|overdue (0/1)`: what a person would call "the task changed". Not "days
late" and not "days idle": they tick up every day and would make everything look new.

## What is reported now whatever the memory says (the hard rules), in this order of `why`

  * `due_soon`   due today or within 2 days (and not closed);
  * `sprint`     in an active sprint and has a due date;
  * `blocked`    Blocked now and not Blocked in the memory (or not in it);
  * `overdue`    overdue now and not overdue in the memory (or not in it);
  * `activity`   somebody else touched it since the last report (it is in "Changed");
  * `important`  one of the top tasks Genos picked ("Start with").

Anything else: held back as `same` when the memory has it with the same signature, else listed
as `new` (never in the memory) or `changed` (in it, with another signature).

## The member's answer beats every rule above (Ken's decision 3)

  * `on_it`         held back (counted as quieted) for 7 days, or until the signature changes;
  * `not_important` held back until the signature changes. A mark made before MIP-1211 has no
    signature and keeps its old meaning (never a top item or suggestion): it does not touch a
    report, because "until it changes" cannot be told.

A task the member cannot open, or one with no project, has no signature and is never held.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from origin.models.common.inbox_models import ITEM_TYPE_GENOS_REPORT, InboxItems
from origin.models.common.member_importance_models import MARK_NOT_IMPORTANT, MARK_ON_IT
from origin.search_engine.agent.tools.base import ToolContext

# The `why` of a report row (MIP-1211): why it is listed. A card may draw it; none is required.
WHY_VALUES = (
    "due_soon",
    "sprint",
    "blocked",
    "overdue",
    "activity",
    "important",
    "new",
    "changed",
)
# The stat keys a report adds (their emoji are in `schedule_jobs.EMOJI`).
STAT_STILL_SAME = "still_same"
STAT_QUIETED = "quieted"

ON_IT_DAYS = 7
DUE_SOON_DAYS = 2
MEMORY_DAYS = 30
MAX_SEEN = 100
_DONE = ("Closed", "Deleted")


def _part(value: Any) -> str:
    return str(value or "").replace("|", "/")[:30]


def signature(status: Any, priority: Any, due: Any, overdue: bool) -> str:
    return f"{_part(status)}|{_part(priority)}|{_part(due)}|{1 if overdue else 0}"


def _overdue(due: date | None, status: str, today: date) -> bool:
    return due is not None and due < today and status not in _DONE


def signature_of_task(task: Any, today: date) -> str:
    """The signature of a `TaskMaster` row."""
    return signature(
        task.status,
        task.priority,
        task.due_date.isoformat() if task.due_date else "",
        _overdue(task.due_date, task.status or "", today),
    )


def signature_of_facts(facts: Any, today: date) -> str:
    """The signature of a report's `TaskFacts`."""
    return signature(
        facts.status,
        facts.priority,
        facts.due_date.isoformat() if facts.due_date else "",
        _overdue(facts.due_date, facts.status or "", today),
    )


def _status_of(sig: str) -> str:
    return sig.split("|", 1)[0]


def _was_overdue(sig: str) -> bool:
    return sig.rsplit("|", 1)[-1] == "1"


# --------------------------------------------------------------------------- #
# The memory: the previous report                                              #
# --------------------------------------------------------------------------- #


def previous_seen(ctx: ToolContext, job_key: str, now: datetime) -> dict[int, str] | None:
    """`{task_id: signature}` from the member's newest OPENED report of this job in this team,
    at most `MEMORY_DAYS` old (deleted ones included), or None when there is none (every task is
    then new)."""
    row = (
        InboxItems.objects.filter(
            receiver_id=ctx.user_id,
            team_id=ctx.team_id,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_optionals__job=job_key,
            is_read=True,
            ts_created_at__gte=now - timedelta(days=MEMORY_DAYS),
        )
        .order_by("-ts_created_at", "-item_id")
        .first()
    )
    if row is None or not isinstance(row.item_optionals, dict):
        return None
    seen = row.item_optionals.get("seen")
    if not isinstance(seen, dict):
        return None
    out: dict[int, str] = {}
    for key, sig in seen.items():
        if isinstance(sig, str) and str(key).isascii() and str(key).isdigit():
            out[int(key)] = sig
    return out


# --------------------------------------------------------------------------- #
# The verdict on a report's task rows                                          #
# --------------------------------------------------------------------------- #


@dataclass
class Verdict:
    # task_id -> "same" (unchanged since the last report) | "quieted" (the member's answer)
    held: dict[int, str] = field(default_factory=dict)
    # task_id -> why it is listed, for the rows that are
    why: dict[int, str] = field(default_factory=dict)
    # task_id -> signature now, for every task row that was listed or held as unchanged (the
    # next report's memory; a quieted task is left out, so it comes back when its quiet ends)
    seen: dict[str, str] = field(default_factory=dict)
    # whether the card may offer the two answers (recording on)
    can_answer: bool = False

    @property
    def same(self) -> int:
        return sum(1 for v in self.held.values() if v == "same")

    @property
    def quieted(self) -> int:
        return sum(1 for v in self.held.values() if v == "quieted")


def _answer_holds(
    info: tuple[str | None, datetime] | None, mark: str | None, sig: str, now: datetime
) -> bool:
    """Whether the member's answer keeps this task out of the report now."""
    if info is None or mark is None:
        return False
    answered_sig, at = info
    if answered_sig is None:
        return False  # made before MIP-1211: it never touched a report
    if mark == MARK_ON_IT:
        return answered_sig == sig and now - at < timedelta(days=ON_IT_DAYS)
    if mark == MARK_NOT_IMPORTANT:
        return answered_sig == sig
    return False


def judge(
    ctx: ToolContext,
    job_key: str,
    task_ids: list[int],
    facts_of,
    *,
    today: date,
    now: datetime,
    start_with: set[int],
    changed: set[int],
    holdable: set[int] | None = None,
) -> Verdict:
    """Decide each task row. `task_ids` are the tasks the report would list; `facts_of(task_id)`
    is its `TaskFacts` (None when the member cannot open it). `holdable` limits the tasks that may
    be held back (None = all): a task outside it is always listed."""
    from origin.services import importance  # noqa: PLC0415 - it reads this module

    verdict = Verdict()
    verdict.can_answer = importance.enabled(ctx.user_id)
    prefs = importance.load(ctx) if verdict.can_answer else importance.Prefs({}, {})
    memory = previous_seen(ctx, job_key, now)
    soon = today + timedelta(days=DUE_SOON_DAYS)

    for task_id in dict.fromkeys(task_ids):
        facts = facts_of(task_id)
        if facts is None or not facts.project_id:
            continue  # no signature, nothing to hold it by, nothing to answer
        if holdable is not None and task_id not in holdable:
            continue  # a record, not an attention row (a closed task): always listed, no reason
        sig = signature_of_facts(facts, today)
        if _answer_holds(prefs.info.get(task_id), prefs.marks.get(task_id), sig, now):
            verdict.held[task_id] = "quieted"
            continue
        before = None if memory is None else memory.get(task_id)
        open_ = facts.status not in _DONE
        why = None
        if open_ and facts.due_date is not None and today <= facts.due_date <= soon:
            why = "due_soon"
        elif open_ and facts.in_active_sprint and facts.due_date is not None:
            why = "sprint"
        elif facts.status == "Blocked" and (before is None or _status_of(before) != "Blocked"):
            why = "blocked"
        elif _overdue(facts.due_date, facts.status or "", today) and (
            before is None or not _was_overdue(before)
        ):
            why = "overdue"
        elif task_id in changed:
            why = "activity"
        elif task_id in start_with:
            why = "important"
        if why is None and before is not None and before == sig:
            verdict.held[task_id] = "same"
            verdict.seen[str(task_id)] = sig
            continue
        verdict.why[task_id] = why or ("new" if before is None else "changed")
        verdict.seen[str(task_id)] = sig

    if len(verdict.seen) > MAX_SEEN:
        verdict.seen = dict(list(verdict.seen.items())[:MAX_SEEN])
    return verdict
