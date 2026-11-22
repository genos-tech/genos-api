"""A PR that needs a person, shown through surfaces Genos already has (MIP-2054).

Ken's rule (2026-11-23, MIP-2027): no new UI component. A GitHub `pull_request` delivery with the
action `review_requested` names ONE person GitHub asked to review a PR whose head branch carries a
task's display id. For that person, and only if they can open the task, this writes:

  * one Inbox item of the EXISTING kind 0 ("Activity message": `item_body` {title, text}, no
    answer to give), with `item_optionals` {kind: "pr_review_requested", task_id, pr_url, ...};
  * one comment on the linked task (null sender, the bold "Genos" label, as `pr_merge_comment`),
    mirrored into the task thread but not fanned out: the Inbox item is the alert, the comment is
    the record anyone on the task can read;
  * the briefing's `waiting_on_me.pr_reviews` rows are read from the unread Inbox items (see
    `pending_review_rows`), so there is no second store.

WHO. GitHub names a login and a numeric id; the id is matched to a Genos user through their
connected GitHub account (`ConnectedAccount.provider_user_id`). Nobody is guessed from a login or a
name: an id with no connected account, or a user who cannot open the task (`can_access_task`), gets
nothing, and the task comment is not written either. Team reviewers (`requested_team`) are ignored.

ONCE. The key is the id of the comment's first block, `genos-pr-review:<canonical PR url>:<user id>`.
It is checked under the task row's lock, so a redelivery or two deliveries at once write one comment
and one Inbox item. A comment a person deleted is kept as a soft-deleted row, so it is not re-posted.

NOT DELIVERED TODAY (the webhook's events are `pull_request`, `issue_comment`,
`pull_request_review_comment`): `pull_request_review` (changes requested, approved) and
`check_suite` / `status` (CI failed, green). Handling them needs those events added to the hook,
which is GitHub configuration this task does not change; see the findings comment on the task.

PR titles and logins are GitHub text: stripped of markdown syntax before they go into any body.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast

from origin.models.common.inbox_models import InboxItems
from origin.models.common.user_models import ConnectedAccount
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import realtime_push, unified_writer
from origin.services.pr_merge_comment import _chip_resolver, _paragraph, _plain, _pr_label
from origin.services.task_comment_ids import next_task_comment_id

logger = logging.getLogger("origin.pr_needs_you")

KIND_REVIEW_REQUESTED = "pr_review_requested"
MARKER_PREFIX = "genos-pr-review:"
#: How many review rows the briefing lists (the newest first).
MAX_ROWS = 8


def marker(pr_url: str, user_id: Any) -> str:
    return f"{MARKER_PREFIX}{pr_url}:{user_id}"


def _already_posted(task_id: int, key: str) -> bool:
    return (
        TaskComments.objects.filter(task_id=task_id, sender__isnull=True)
        .annotate(_text=Cast("comment_body", TextField()))
        .filter(_text__contains=f'"{key}"')
        .exists()
    )


def reviewer_user(github_id: Any):
    """The Genos user whose connected GitHub account has this numeric id, or None."""
    if github_id in (None, "", 0):
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
    current = (
        TaskMaster.objects.filter(task_id=task.task_id, is_deleted=False)
        .select_related("project")
        .first()
    )
    if current is None or current.project_id is None or current.project.is_deleted:
        return False
    if not _can_open(current, reviewer.id):
        return False
    label = _pr_label(owner, repo, number)
    shown = f' "{_plain(title)}"' if _plain(title) else ""
    login = _plain(reviewer_login, 60) or "a reviewer"
    markdown = (
        f"**Genos** (automatic note): review of [{label}]({pr_url}){shown} was requested from "
        f"{login}.\n\nLinked task: [#{current.display_id}](task:{current.task_id})"
    )
    key = marker(pr_url, reviewer.id)
    blocks = markdown_to_blocks(markdown, entity_chip_resolver=_chip_resolver([current]))
    blocks[0]["id"] = key  # the once-per-PR-and-reviewer key
    blocks.append(_paragraph(""))
    with transaction.atomic():
        comment_id = next_task_comment_id(current.task_id)  # locks the task row
        if _already_posted(current.task_id, key):
            return False
        comment = TaskComments.objects.create(
            task_id=current.task_id, sender=None, comment_id=comment_id, comment_body=blocks
        )
        item = InboxItems.objects.create(
            team_id=current.team_id,
            sender=None,
            receiver=reviewer,
            item_type=0,
            item_body={
                "title": "Review requested",
                "text": f"{label}{shown}: your review was requested ({current.display_id}).",
            },
            item_optionals={
                "kind": KIND_REVIEW_REQUESTED,
                "task_id": current.task_id,
                "display_id": current.display_id,
                "pr_url": pr_url,
                "pr_label": label,
            },
            request_status="",
            is_read=False,
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
    try:
        from origin.services.webpush_dispatch import schedule_push_for_inbox_item  # noqa: PLC0415

        schedule_push_for_inbox_item(item)
    except Exception:  # noqa: BLE001 — the item exists; a push is a nicety
        logger.exception("PR review inbox push failed: item=%s", item.item_id)
    return True


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
