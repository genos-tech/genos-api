"""Is a linked PR ready to merge, and does it need a nudge? (MIP-2056)

Decision (Ken, 2026-11-23, "no new UI", MIP-2027): this surfaces on what exists, a task comment and a
`merge_readiness` field in the `get_task` deliveries and the `get_pr` reply. Nothing else: no Inbox
item, no Today row, no push, no mail.

WHAT IT READS. For one open PR: the PR itself (draft, `mergeable_state`, `updated_at`), its reviews and
the head commit's CI (combined status + check runs). `assess` is pure; `fetch_readiness` makes the
three GitHub calls through a `get(path)` the caller supplies (the cron has the registrar's token, the
MCP tool the user's), and returns None when any answer is missing, so a GitHub hiccup never posts.

STATES (`assess()["state"]`): `draft`, `conflicts`, `changes_requested`, `ci_failed`, `ready`
(approved, CI not red or pending, no conflict), `waiting` (anything else: not approved yet, CI running).
`stale` is a separate flag: the PR has had no activity for `PR_STALE_NUDGE_DAYS`.

NUDGES. At most one comment per PR per task per state change. Two kinds:
  * `ready`: the PR has been ready (approved + green) for `PR_READY_NUDGE_DAYS` and is still open;
  * `stale`: no activity for `PR_STALE_NUDGE_DAYS` and not ready (a ready PR gets the `ready` nudge).
The comment's first block id is `genos-pr-nudge:<kind>:<canonical PR URL>`; a nudge is posted only when
the LAST nudge for this PR on this task has another kind (or there is none). A webhook, a second cron
run or a restart therefore find it and post nothing; ready -> stale posts once more, and so does
stale -> ready. A person's deleted comment still counts (soft-deleted rows are kept).

HOW IT RUNS. `reconcile_pr_deliveries` (every 15 minutes, no new service) already asks GitHub about
every open PR; it calls `check_open_pr` for the ones still open. It also stores the assessment in the
cache (`cache_key`), which is where `delivery_as_dict` reads `merge_readiness` for `get_task`: a task
read never calls GitHub.

WHO WRITES THE COMMENT. Nobody (null sender), the bold label "Genos" says it is automatic, as for
`pr_merge_comment`. No fan-out: no mention, push, mail or Inbox item. Closed and deleted tasks get none.

SWITCH. `PR_NUDGES_ENABLED` (default off): off means no GitHub calls, no comment and no field, so the
model-visible replies are what they were.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import realtime_push, unified_writer
from origin.services.pr_merge_comment import _paragraph, _plain, _pr_label
from origin.services.task_comment_ids import next_task_comment_id

logger = logging.getLogger("origin.pr_merge_readiness")

MARKER_PREFIX = "genos-pr-nudge:"
_MARKER_RE = re.compile(r'"genos-pr-nudge:(ready|stale):')
_CACHE_TTL = 6 * 3600
_CLOSED_TASK = ("Closed", "Deleted")

#: A `get(path)` answers a requests-like response (`.ok`, `.json()`), or raises.
Getter = Callable[[str], Any]


def enabled() -> bool:
    return bool(getattr(settings, "PR_NUDGES_ENABLED", False))


def ready_days() -> int:
    return max(int(getattr(settings, "PR_READY_NUDGE_DAYS", 1)), 0)


def stale_days() -> int:
    return max(int(getattr(settings, "PR_STALE_NUDGE_DAYS", 7)), 1)


def cache_key(pr_url: str) -> str:
    return f"pr_merge_readiness:{pr_url}"


# --- reading GitHub's answers -------------------------------------------------------------------


def _ts(value: Any) -> datetime | None:
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is not None and timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, timezone.utc)
    return parsed


def review_verdict(reviews: Iterable[dict]) -> tuple[str, datetime | None]:
    """`("approved" | "changes_requested" | "none", when the latest approval was given)`.

    As GitHub counts them: each reviewer's LAST review that is an approval, a change request or a
    dismissal decides (a plain comment does not undo an approval); a dismissal removes the verdict.
    One standing change request blocks."""
    last: dict[str, tuple[str, datetime | None]] = {}
    for review in sorted(
        (r for r in reviews if isinstance(r, dict)),
        key=lambda r: str(r.get("submitted_at") or ""),
    ):
        state = str(review.get("state") or "").upper()
        who = str((review.get("user") or {}).get("login") or review.get("id") or "")
        if state in ("APPROVED", "CHANGES_REQUESTED"):
            last[who] = (state, _ts(review.get("submitted_at")))
        elif state == "DISMISSED":
            last.pop(who, None)
    if any(s == "CHANGES_REQUESTED" for s, _ in last.values()):
        return "changes_requested", None
    approvals = [at for s, at in last.values() if s == "APPROVED"]
    if approvals:
        known = [a for a in approvals if a is not None]
        return "approved", (max(known) if known else None)
    return "none", None


def ci_verdict(combined: Any, checks: Any) -> str:
    """`success`, `failure`, `pending` or `none` (no CI at all) for the head commit; `unknown` when an
    answer is missing or the check runs span several pages."""
    if not isinstance(combined, dict) or not isinstance(checks, dict):
        return "unknown"
    runs = checks.get("check_runs")
    total = checks.get("total_count")
    if not isinstance(runs, list) or not isinstance(total, int) or total > len(runs):
        return "unknown"
    bad = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
    statuses = combined.get("total_count") or 0
    if any(isinstance(r, dict) and r.get("conclusion") in bad for r in runs):
        return "failure"
    if statuses > 0 and combined.get("state") in ("failure", "error"):
        return "failure"
    if any(not isinstance(r, dict) or r.get("status") != "completed" for r in runs):
        return "pending"
    if statuses > 0 and combined.get("state") == "pending":
        return "pending"
    return "success" if runs or statuses else "none"


def assess(
    pr: dict, reviews: Iterable[dict], combined: Any, checks: Any, *, now: datetime | None = None
) -> dict[str, Any]:
    """The readiness of one OPEN PR, from GitHub's answers. Pure."""
    now = now or timezone.now()
    review, approved_at = review_verdict(reviews)
    ci = ci_verdict(combined, checks)
    mergeable = str(pr.get("mergeable_state") or "unknown")
    updated = _ts(pr.get("updated_at"))
    idle_days = max((now - updated).days, 0) if updated else 0
    if pr.get("draft"):
        state = "draft"
    elif mergeable == "dirty":
        state = "conflicts"
    elif review == "changes_requested":
        state = "changes_requested"
    elif ci == "failure":
        state = "ci_failed"
    elif review == "approved" and ci in ("success", "none"):
        state = "ready"
    else:
        state = "waiting"
    ready_since = approved_at if state == "ready" else None
    out: dict[str, Any] = {
        "state": state,
        "review": review,
        "ci": ci,
        "mergeable_state": mergeable,
        "idle_days": idle_days,
        "stale": idle_days >= stale_days(),
        "last_activity_at": updated.isoformat() if updated else None,
        "checked_at": now.isoformat(),
    }
    if ready_since is not None:
        out["ready_since"] = ready_since.isoformat()
    out["nudge"] = _nudge_kind(out, ready_since, now)
    return out


def _nudge_kind(a: dict, ready_since: datetime | None, now: datetime) -> str | None:
    if a["state"] == "ready":
        waited = now - (ready_since or _ts(a["last_activity_at"]) or now)
        return "ready" if waited >= timedelta(days=ready_days()) else None
    # A stale draft is still stale: someone parked work in the open.
    return "stale" if a["stale"] else None


def fetch_readiness(pr: dict, get: Getter) -> dict[str, Any] | None:
    """`assess` for `pr` (a GitHub pull body, open), asking `get` for the reviews and the CI. None when
    GitHub did not answer all three, so a hiccup never reads as "not approved"."""
    owner_repo = str((pr.get("base") or {}).get("repo", {}).get("full_name") or "")
    number = pr.get("number")
    sha = (pr.get("head") or {}).get("sha")
    if not owner_repo or not isinstance(number, int) or not sha:
        return None
    try:
        rv = get(f"/repos/{owner_repo}/pulls/{number}/reviews?per_page=100")
        cs = get(f"/repos/{owner_repo}/commits/{sha}/status")
        ck = get(f"/repos/{owner_repo}/commits/{sha}/check-runs?per_page=100")
        if not (rv.ok and cs.ok and ck.ok):
            return None
        reviews = rv.json()
        if not isinstance(reviews, list):
            return None
        return assess(pr, reviews, cs.json(), ck.json())
    except Exception:  # noqa: BLE001 — network, JSON: no answer, no nudge
        logger.warning("PR readiness not read: %s #%s", owner_repo, number, exc_info=True)
        return None


# --- the comment ----------------------------------------------------------------------------------


def marker(kind: str, pr_url: str) -> str:
    return f"{MARKER_PREFIX}{kind}:{pr_url}"


def last_nudge_kind(task_id: int, pr_url: str) -> str | None:
    """The kind of the last nudge written for this PR on this task, or None. Deleted comments count."""
    rows = (
        TaskComments.objects.filter(task_id=task_id, sender__isnull=True)
        .annotate(_text=Cast("comment_body", TextField()))
        .filter(_text__contains=MARKER_PREFIX)
        .filter(_text__contains=f':{pr_url}"')
        .order_by("-comment_id")
        .values_list("_text", flat=True)[:1]
    )
    for text in rows:
        found = _MARKER_RE.search(text)
        if found:
            return found.group(1)
    return None


_STATE_WORDS = {
    "draft": "a draft",
    "conflicts": "blocked by merge conflicts",
    "changes_requested": "waiting for the author after a change request",
    "ci_failed": "failing CI",
    "waiting": "not approved yet, or CI is still running",
    "ready": "approved with green CI",
}


def build_markdown(*, kind: str, label: str, pr_url: str, title: str, a: dict) -> str:
    shown = f' "{_plain(title)}"' if _plain(title) else ""
    head = f"**Genos** (automatic note): [{label}]({pr_url}){shown}"
    if kind == "ready":
        days = max(
            (
                timezone.now()
                - (_ts(a.get("ready_since")) or _ts(a.get("last_activity_at")) or timezone.now())
            ).days,
            0,
        )
        return (
            f"{head} is ready to merge: approved, CI green, no conflicts. "
            f"It has waited {days} day(s) for a merge."
        )
    return (
        f"{head} has had no activity for {a['idle_days']} days "
        f"(last: {str(a.get('last_activity_at') or 'unknown')[:10]}). "
        f"State: {_STATE_WORDS.get(a['state'], a['state'])}. "
        "Push it forward, close it, or say here what it waits for."
    )


def post_nudge(
    task: TaskMaster, *, owner: str, repo: str, number: int, title: str, pr_url: str, a: dict
) -> bool:
    """Post the nudge `a["nudge"]` for `task` unless the last one for this PR was of that kind."""
    kind = a.get("nudge")
    if kind not in ("ready", "stale"):
        return False
    current = (
        TaskMaster.objects.filter(task_id=task.task_id, is_deleted=False)
        .select_related("project")
        .first()
    )
    if (
        current is None
        or current.status in _CLOSED_TASK
        or current.project_id is None
        or current.project.is_deleted
    ):
        return False
    if last_nudge_kind(current.task_id, pr_url) == kind:
        return False
    markdown = build_markdown(
        kind=kind, label=_pr_label(owner, repo, number), pr_url=pr_url, title=title, a=a
    )
    blocks = markdown_to_blocks(markdown)
    blocks[0]["id"] = marker(kind, pr_url)
    blocks.append(_paragraph(""))
    with transaction.atomic():
        comment_id = next_task_comment_id(current.task_id)  # locks the task row
        if last_nudge_kind(current.task_id, pr_url) == kind:
            return False
        comment = TaskComments.objects.create(
            task_id=current.task_id, sender=None, comment_id=comment_id, comment_body=blocks
        )
    unified_writer.write_task_comment_as_thread_reply(
        task_id=current.task_id,
        comment_id=comment.comment_id,
        sender_id=None,
        body=blocks,
        project_id=current.project_id,
        bypass_flag=True,
    )
    realtime_push.push_task_comment(current.task_id, comment.comment_id, realtime_push.CREATED)
    return True


def check_open_pr(
    *, owner: str, repo: str, pr: dict, team_ids: list, get: Getter, budget: list[int]
) -> int:
    """Assess one open PR, cache the result, and post what is due on the tasks that deliver it.
    `budget` is a one-item list: how many comments this run may still post (a first run on a board
    with many old PRs must not flood it). Returns how many comments were posted."""
    number = pr.get("number")
    pr_url = f"https://github.com/{owner}/{repo}/pull/{number}".lower()
    a = fetch_readiness(pr, get)
    if a is None:
        return 0
    cache.set(cache_key(pr_url), a, _CACHE_TTL)
    if not a["nudge"] or budget[0] <= 0:
        return 0
    posted = 0
    rows = TaskDelivery.objects.filter(
        team_id__in=team_ids, pr_url=pr_url, state=TaskDeliveryState.OPEN
    ).select_related("task")
    for row in rows:
        if budget[0] <= 0:
            break
        try:
            if post_nudge(
                row.task,
                owner=owner,
                repo=repo,
                number=int(number),
                title=pr.get("title") or row.pr_title or "",
                pr_url=pr_url,
                a=a,
            ):
                posted += 1
                budget[0] -= 1
        except Exception:  # noqa: BLE001 — a note must not stop the cron
            logger.exception("PR nudge failed: task=%s pr=%s", row.task_id, pr_url)
    return posted


def cached_readiness(pr_url: str | None) -> dict[str, Any] | None:
    """What the cron last read for this PR (a field for `get_task`), or None. Never calls GitHub."""
    if not enabled() or not pr_url:
        return None
    value = cache.get(cache_key(pr_url))
    return value if isinstance(value, dict) else None
