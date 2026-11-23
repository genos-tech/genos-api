"""A PR that needs a person, shown through surfaces Genos already has (MIP-2054, MIP-2062).

Ken's rule (2026-11-23, MIP-2027): no new UI component. Five signals reach a person through the
EXISTING surfaces only:

  * review requested (MIP-2054): the reviewer GitHub named;
  * changes requested, approved, CI failing, approved and green (MIP-2062): the PR's author.
    These come from the GitHub App webhook (`pull_request_review`, `check_run`, `status`); the
    decisions are in `origin.services.pr_signals`, the writing is here.

For each, and only if the person can open the task, this writes:

  * one Inbox item of the EXISTING kind 0 ("Activity message": `item_body` {title, text}, no
    answer to give), with `item_optionals` {kind: "pr_review_requested" | "pr_changes_requested" |
    "pr_approved" | "pr_ci_failing" | "pr_ready_to_merge", task_id, pr_url, ...};
  * one comment on the linked task (null sender, the bold "Genos" label, as `pr_merge_comment`),
    mirrored into the task thread but not fanned out: the Inbox item is the alert, the comment is
    the record anyone on the task can read;
  * the briefing's `waiting_on_me.pr_reviews` (review requested) and `waiting_on_me.pr_updates` (the
    four others) rows are read from the unread Inbox items (`pending_review_rows`,
    `pending_signal_rows`), so there is no second store.

WHO. GitHub names a login and a numeric id; the id is matched to a Genos user through their
connected GitHub account (`ConnectedAccount.provider_user_id`). Nobody is guessed from a login or a
name: an id with no connected account, or a user who cannot open the task (`can_access_task`), gets
nothing, and the task comment is not written either. Team reviewers (`requested_team`) are ignored.

ONCE. The key is the id of the comment's first block (`genos-pr-review:<canonical PR url>:<user id>`
for a review request; `genos-pr-<signal>:<url>:<review id or commit sha>` for the others). It is
checked under the task row's lock, so a redelivery or two deliveries at once write one comment and
one Inbox item. A comment a person deleted is kept as a soft-deleted row, so it is not re-posted.

DEBOUNCED. A signal may name a `debounce_prefix` and a window: when a comment of that task with that
key prefix was written inside the window, nothing is written. CI failing uses it, so a PR whose
checks fail one after another (or on several pushes in a row) tells its author once per window, never
once per check run. The check is under the same lock.

STALE ITEMS. `resolve_items` marks an author's unread items of given kinds for a PR as read: "ready"
retires that PR's "CI failing" and "changes requested", and a closed PR retires all of its signals,
so the briefing never lists something that stopped being true.

PR titles and logins are GitHub text: stripped of markdown syntax before they go into any body.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.user_models import ConnectedAccount
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import realtime_push, unified_writer
from origin.services.pr_merge_comment import _chip_resolver, _paragraph, _plain, _pr_label
from origin.services.task_comment_ids import next_task_comment_id

logger = logging.getLogger("origin.pr_needs_you")

KIND_REVIEW_REQUESTED = "pr_review_requested"
KIND_CHANGES_REQUESTED = "pr_changes_requested"
KIND_APPROVED = "pr_approved"
KIND_CI_FAILING = "pr_ci_failing"
KIND_READY = "pr_ready_to_merge"
#: The four signals for a PR's author (MIP-2062), in the order the briefing's rows are described.
SIGNAL_KINDS: tuple[str, ...] = (KIND_CHANGES_REQUESTED, KIND_APPROVED, KIND_CI_FAILING, KIND_READY)
#: Every kind this module writes.
ALL_KINDS: tuple[str, ...] = (KIND_REVIEW_REQUESTED, *SIGNAL_KINDS)
#: The word the briefing row carries for a kind.
SIGNAL_NAMES = {
    KIND_CHANGES_REQUESTED: "changes_requested",
    KIND_APPROVED: "approved",
    KIND_CI_FAILING: "ci_failing",
    KIND_READY: "ready_to_merge",
}
_SIGNAL_ROW_TEXT = {
    KIND_CHANGES_REQUESTED: "Changes were requested on your PR.",
    KIND_APPROVED: "Your PR was approved.",
    KIND_CI_FAILING: "CI is failing on your PR.",
    KIND_READY: "Your PR is approved and CI is green: ready to merge.",
}
MARKER_PREFIX = "genos-pr-review:"
#: How many rows the briefing lists per list (the newest first).
MAX_ROWS = 8
#: At most one "CI failing" notice per PR and task inside this window, however many check runs fail.
CI_DEBOUNCE = timedelta(minutes=30)


def marker(pr_url: str, user_id: Any) -> str:
    return f"{MARKER_PREFIX}{pr_url}:{user_id}"


def signal_marker(signal: str, pr_url: str, discriminator: Any) -> str:
    """`genos-pr-<signal>:<pr url>:<discriminator>`: the key of one signal about one PR, the
    discriminator being a review id or a commit sha. Its prefix up to the last colon is what a
    debounce window looks for."""
    return f"genos-pr-{signal}:{pr_url}:{discriminator}"


def _comment_has(task_id: int, text: str, *, since=None) -> bool:
    rows = TaskComments.objects.filter(task_id=task_id, sender__isnull=True)
    if since is not None:
        rows = rows.filter(ts_sent_at__gte=since)
    return (
        rows.annotate(_text=Cast("comment_body", TextField())).filter(_text__contains=text).exists()
    )


def _debounced(
    task_id: int, prefix: str, window: timedelta | None, reset_prefix: str | None
) -> bool:
    """Was a comment with `prefix` written inside `window`, with no comment of `reset_prefix` since?
    (A "ready" after a failure ends that failure's quiet period: a new failure is news again.)"""
    since = timezone.now() - (window or timedelta())
    last = (
        TaskComments.objects.filter(task_id=task_id, sender__isnull=True, ts_sent_at__gte=since)
        .annotate(_text=Cast("comment_body", TextField()))
        .filter(_text__contains=f'"{prefix}')
        .order_by("-ts_sent_at")
        .values_list("ts_sent_at", flat=True)
        .first()
    )
    if last is None:
        return False
    return not (reset_prefix and _comment_has(task_id, f'"{reset_prefix}', since=last))


def _already_posted(task_id: int, key: str) -> bool:
    return _comment_has(task_id, f'"{key}"')


def reviewer_user(github_id: Any):
    """The Genos user whose connected GitHub account has this numeric id, or None."""
    if github_id in (None, "", 0):
        return None
    if isinstance(github_id, bool):
        return None
    account = (
        ConnectedAccount.objects.filter(provider="github", provider_user_id=str(github_id))
        .select_related("user")
        .first()
    )
    return account.user if account is not None else None


def _can_open(task: TaskMaster, user_id: Any) -> bool:
    from origin.views.utils.scope_guards import can_access_task  # noqa: PLC0415

    return can_access_task(task.task_id, user_id)


def write_pr_notice(
    task: TaskMaster,
    *,
    receiver,
    key: str,
    markdown: str,
    title: str,
    text: str,
    kind: str,
    pr_url: str,
    pr_label: str,
    extra: dict | None = None,
    debounce_prefix: str | None = None,
    debounce: timedelta | None = None,
    debounce_reset_prefix: str | None = None,
    resolve_kinds: tuple[str, ...] = (),
) -> bool:
    """The comment and the Inbox item for `receiver` on `task`. True when written, False when
    already there (the key, or a comment with `debounce_prefix` inside the `debounce` window), the
    task is gone, or the receiver cannot open it. `markdown` is the comment's sentence (the linked
    task line is added here); `resolve_kinds` are this receiver's unread items of those kinds for
    the PR, marked read in the same transaction."""
    current = (
        TaskMaster.objects.filter(task_id=task.task_id, is_deleted=False)
        .select_related("project")
        .first()
    )
    if current is None or current.project_id is None or current.project.is_deleted:
        return False
    if not _can_open(current, receiver.id):
        return False
    body = f"{markdown}\n\nLinked task: [#{current.display_id}](task:{current.task_id})"
    blocks = markdown_to_blocks(body, entity_chip_resolver=_chip_resolver([current]))
    blocks[0]["id"] = key  # the once-per-PR-and-signal key
    blocks.append(_paragraph(""))
    with transaction.atomic():
        comment_id = next_task_comment_id(current.task_id)  # locks the task row
        if _already_posted(current.task_id, key):
            return False
        if debounce_prefix and _debounced(
            current.task_id, debounce_prefix, debounce, debounce_reset_prefix
        ):
            return False
        comment = TaskComments.objects.create(
            task_id=current.task_id, sender=None, comment_id=comment_id, comment_body=blocks
        )
        optionals = {
            "kind": kind,
            "task_id": current.task_id,
            "display_id": current.display_id,
            "pr_url": pr_url,
            "pr_label": pr_label,
            **(extra or {}),
        }
        item = InboxItems.objects.create(
            team_id=current.team_id,
            sender=None,
            receiver=receiver,
            item_type=0,
            item_body={"title": title, "text": text.replace("{display_id}", current.display_id)},
            item_optionals=optionals,
            request_status="",
            is_read=False,
        )
        if resolve_kinds:
            resolve_items(pr_url, resolve_kinds, receiver_id=receiver.id, task_id=current.task_id)
    unified_writer.write_task_comment_as_thread_reply(
        task_id=current.task_id,
        comment_id=comment.comment_id,
        sender_id=None,
        body=blocks,
        project_id=current.project_id,
        bypass_flag=True,
    )
    realtime_push.push_task_comment(current.task_id, comment.comment_id, realtime_push.CREATED)
    try:
        from origin.services.webpush_dispatch import schedule_push_for_inbox_item  # noqa: PLC0415

        schedule_push_for_inbox_item(item)
    except Exception:  # noqa: BLE001 — the item exists; a push is a nicety
        logger.exception("PR inbox push failed: item=%s", item.item_id)
    return True


def resolve_items(
    pr_url: str,
    kinds: Iterable[str],
    *,
    receiver_id: Any = None,
    task_id: int | None = None,
    team_ids: Iterable[Any] | None = None,
) -> int:
    """Mark the unread Inbox items of `kinds` about `pr_url` as read (they stopped being true).
    Narrowed to one receiver, one task or the teams given. Returns how many."""
    rows = InboxItems.objects.filter(
        item_type=0,
        is_deleted=False,
        is_read=False,
        item_optionals__kind__in=list(kinds),
        item_optionals__pr_url=pr_url,
    )
    if receiver_id is not None:
        rows = rows.filter(receiver_id=receiver_id)
    if task_id is not None:
        rows = rows.filter(item_optionals__task_id=task_id)
    if team_ids is not None:
        rows = rows.filter(team_id__in=list(team_ids))
    with (
        transaction.atomic()
    ):  # a savepoint: a DB error here must not abort the caller's transaction
        return rows.update(is_read=True)


def notify_review_requested(
    task: TaskMaster,
    *,
    reviewer,
    reviewer_login: str,
    owner: str,
    repo: str,
    number: int,
    title: str,
    pr_url: str,
) -> bool:
    """Write the comment and the Inbox item for `reviewer` on `task`. True when written, False when
    already there, the task is gone, or the reviewer cannot open it. `pr_url` is canonical."""
    label = _pr_label(owner, repo, number)
    shown = f' "{_plain(title)}"' if _plain(title) else ""
    login = _plain(reviewer_login, 60) or "a reviewer"
    return write_pr_notice(
        task,
        receiver=reviewer,
        key=marker(pr_url, reviewer.id),
        markdown=(
            f"**Genos** (automatic note): review of [{label}]({pr_url}){shown} was requested from "
            f"{login}."
        ),
        title="Review requested",
        text=f"{label}{shown}: your review was requested ({{display_id}}).",
        kind=KIND_REVIEW_REQUESTED,
        pr_url=pr_url,
        pr_label=label,
    )


def notify_review_requested_tasks(
    tasks: Iterable[TaskMaster],
    *,
    requested_reviewer: dict | None,
    owner: str,
    repo: str,
    number: int,
    title: str,
    pr_url: str,
) -> int:
    """`notify_review_requested` for each task, each in its own failure boundary: a webhook answers
    200 whatever happens here (GitHub does not retry a 5xx). Returns how many were written."""
    if not isinstance(requested_reviewer, dict):
        return 0
    reviewer = reviewer_user(requested_reviewer.get("id"))
    if reviewer is None:
        return 0
    written = 0
    for task in tasks:
        try:
            if notify_review_requested(
                task,
                reviewer=reviewer,
                reviewer_login=str(requested_reviewer.get("login") or ""),
                owner=owner,
                repo=repo,
                number=number,
                title=title,
                pr_url=pr_url,
            ):
                written += 1
        except Exception:  # noqa: BLE001
            logger.exception("PR review notice failed: task=%s pr=%s", task.task_id, pr_url)
    return written


def notify_author(
    tasks: Iterable[TaskMaster],
    *,
    signal: str,
    author_github_id: Any,
    owner: str,
    repo: str,
    number: int,
    title: str,
    pr_url: str,
    discriminator: Any,
    actor_login: str = "",
    detail: str = "",
    extra: dict | None = None,
) -> int:
    """One of the four signals (`SIGNAL_KINDS`) for the PR's author, on each task, each in its own
    failure boundary. Returns how many tasks were written. Nothing is written when the author has no
    connected account or cannot open the task."""
    author = reviewer_user(author_github_id)
    if author is None:
        return 0
    label = _pr_label(owner, repo, number)
    shown = f' "{_plain(title)}"' if _plain(title) else ""
    actor = _plain(actor_login, 60) or "a reviewer"
    link = f"[{label}]({pr_url}){shown}"
    note = "**Genos** (automatic note): "
    notice = {
        KIND_CHANGES_REQUESTED: (
            "Changes requested",
            f"{note}{actor} requested changes on {link}.",
            f"{label}{shown}: changes were requested ({{display_id}}).",
        ),
        KIND_APPROVED: (
            "PR approved",
            f"{note}{actor} approved {link}.",
            f"{label}{shown}: approved ({{display_id}}).",
        ),
        KIND_CI_FAILING: (
            "CI failing",
            f"{note}CI failed on {link}{detail}.",
            f"{label}{shown}: CI is failing ({{display_id}}).",
        ),
        KIND_READY: (
            "Ready to merge",
            f"{note}{link} is approved and CI is green: ready to merge.",
            f"{label}{shown}: approved and CI is green ({{display_id}}).",
        ),
    }
    title_text, markdown, text = notice[signal]
    key = signal_marker(SIGNAL_NAMES[signal], pr_url, discriminator)
    debounce_prefix = (
        f"genos-pr-{SIGNAL_NAMES[signal]}:{pr_url}:" if signal == KIND_CI_FAILING else None
    )
    # What this signal makes untrue: "ready" supersedes the failing, changes-requested and approved
    # notices; a new failure or change request retires an earlier "ready". An approval retires
    # nothing (a change request by ANOTHER reviewer still stands).
    resolve = {
        KIND_READY: (KIND_CI_FAILING, KIND_CHANGES_REQUESTED, KIND_APPROVED),
        KIND_CI_FAILING: (KIND_READY,),
        KIND_CHANGES_REQUESTED: (KIND_READY,),
    }.get(signal, ())
    reset_prefix = (
        f"genos-pr-{SIGNAL_NAMES[KIND_READY]}:{pr_url}:" if signal == KIND_CI_FAILING else None
    )
    written = 0
    for task in tasks:
        try:
            if write_pr_notice(
                task,
                receiver=author,
                key=key,
                markdown=markdown,
                title=title_text,
                text=text,
                kind=signal,
                pr_url=pr_url,
                pr_label=label,
                extra={"signal": SIGNAL_NAMES[signal], **(extra or {})},
                debounce_prefix=debounce_prefix,
                debounce=CI_DEBOUNCE if debounce_prefix else None,
                debounce_reset_prefix=reset_prefix,
                resolve_kinds=resolve,
            ):
                written += 1
        except Exception:  # noqa: BLE001
            logger.exception("PR %s notice failed: task=%s pr=%s", signal, task.task_id, pr_url)
    return written


def pending_review_rows(user_id: Any, team_id: Any) -> list[dict[str, Any]]:
    """The caller's unread "review requested" Inbox items, newest first, for the briefing's
    `waiting_on_me.pr_reviews`. Reading the item marks it read, which drops the row: one store."""
    items = InboxItems.objects.filter(
        receiver_id=user_id,
        team_id=team_id,
        item_type=0,
        is_deleted=False,
        is_read=False,
        item_optionals__kind=KIND_REVIEW_REQUESTED,
    ).order_by("-ts_created_at", "-item_id")[:MAX_ROWS]
    rows = []
    for item in items:
        opt = item.item_optionals or {}
        rows.append(
            {
                "item_id": item.item_id,
                "task_id": opt.get("task_id"),
                "display_id": opt.get("display_id"),
                "pr_url": opt.get("pr_url"),
                "pr": opt.get("pr_label"),
                "at": item.ts_created_at.isoformat(),
                "text": "Your review was requested.",
            }
        )
    return rows


def pending_signal_rows(user_id: Any, team_id: Any) -> list[dict[str, Any]]:
    """The caller's unread changes-requested / approved / CI-failing / ready-to-merge items, newest
    first, for the briefing's `waiting_on_me.pr_updates`; one store, as `pending_review_rows`."""
    items = InboxItems.objects.filter(
        receiver_id=user_id,
        team_id=team_id,
        item_type=0,
        is_deleted=False,
        is_read=False,
        item_optionals__kind__in=list(SIGNAL_KINDS),
    ).order_by("-ts_created_at", "-item_id")[:MAX_ROWS]
    rows = []
    for item in items:
        opt = item.item_optionals or {}
        kind = opt.get("kind")
        rows.append(
            {
                "item_id": item.item_id,
                "signal": SIGNAL_NAMES.get(kind),
                "task_id": opt.get("task_id"),
                "display_id": opt.get("display_id"),
                "pr_url": opt.get("pr_url"),
                "pr": opt.get("pr_label"),
                "at": item.ts_created_at.isoformat(),
                "text": _SIGNAL_ROW_TEXT.get(kind, ""),
            }
        )
    return rows
