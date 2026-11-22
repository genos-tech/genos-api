"""A status change made through an app leaves a visible comment saying why (MIP-1997).

The rule. When a task's or milestone's STATUS is changed by the connector (the MCP tools and the public
REST API) or by the in-app assistant, the caller should say why, and Genos posts that reason as a normal
task comment in the caller's name: "Status: Open → Pending: waits for the release window". The reason
used to live only in an `update_tasks_bulk` rationale in the task's change history, so a task that
turned `Pending` or `Blocked` showed nobody why.

Not covered, on purpose: the web app's own endpoints (a person clicking a status is not asked to type
a reason), a status given when a task is CREATED (there is no old status), the automatic Open/Blocked
flip, the GitHub PR-merge close, and an undo.

One helper, called from the tool or view layer after its own permission check and never from a signal,
so a status written by anything else (the web app, the flip, `sync_backing_task`) posts nothing.

Why it is not a call to `add_comment`. That tool parses the text as markdown and resolves chips, so an
`@name` in a reason would mention and notify a person. The reason is stored as ONE literal paragraph
(the same BlockNote shape `add_comment` stores, with the editor's trailing empty paragraph): no mention
node can be built from it. The comment is marked "made through the connected app" by the existing
`comment_record` signal (`metadata.via` from the current request's credential, or the in-app agent), so
no marker is added here.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from django.db import transaction

from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.task_comment_fanout import fan_out_task_comment
from origin.services.task_comment_ids import next_task_comment_id

STATUS_REASON_MAX = 1000

# Same cleaning as `agent/approval_reason.clean` (copied, not imported: this module is used by the
# public API as well): tab / newline / line and paragraph separators become a space; other control
# characters and the bidirectional overrides are dropped.
_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r  ]+")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")

_PARA_PROPS = {
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}


def clean_status_reason(raw: Any) -> str:
    """The reason as it may be stored: text only, one line, controls gone, trimmed. NOT capped: a
    reason that is too long is refused (`status_reason_error`), not cut."""
    if not isinstance(raw, str):
        return ""
    return _CONTROLS.sub("", _WHITESPACE_CONTROLS.sub(" ", raw)).strip()


def status_reason_error(old: str, new: str, raw_reason: Any, *, arg: str = "reason") -> str | None:
    """Why this status change cannot go ahead, or None. The only refusal left is a reason too long
    to be one comment: a missing reason is NOT refused, it earns a suggestion
    (`status_reason_suggestions`). Cutting a long reason would store words nobody wrote."""
    if old == new:
        return None
    reason = clean_status_reason(raw_reason)
    if len(reason) > STATUS_REASON_MAX:
        return (
            f"`{arg}` is too long ({len(reason)} characters; at most {STATUS_REASON_MAX}): "
            "one or two sentences. Nothing was changed."
        )
    return None


STATUS_REASON_MESSAGE = (
    "The status changed ({old} \u2192 {new}) but no `{arg}` was given, so the task shows nobody why. "
    "Add a comment saying why: Closed = what shipped; Pending or Blocked = what it waits for and "
    "when it comes back; reopened = why."
)


def status_reason_suggestions(
    old: str, new: str, raw_reason: Any, *, arg: str = "reason"
) -> list[dict[str, Any]]:
    """The `suggestions` list for a status change made without a reason (MIP-1997), else [].

    Decision (Ken, 2026-11-22, "Decision: Genos guides agents with suggestions in tool replies, not
    hard rejections" on MIP-2014): the change goes ahead and the reply says what was forgotten. Same
    shape as the organize suggestions of MIP-2015 (`kind`, `message`, `candidates`), so a caller
    reads one `suggestions` key; `candidates` is empty, there is nothing to pick from."""
    if old == new or clean_status_reason(raw_reason):
        return []
    return [
        {
            "kind": "status_reason",
            "message": STATUS_REASON_MESSAGE.format(old=old, new=new, arg=arg),
            "candidates": [],
        }
    ]


def status_comment_text(old: str, new: str, reason: str) -> str:
    return f"Status: {old} → {new}: {reason}"


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "paragraph",
        "props": dict(_PARA_PROPS),
        "content": ([{"text": text, "type": "text", "styles": {}}] if text else []),
        "children": [],
    }


def locked_status_change(task_id: int, new: str) -> tuple[str, str] | None:
    """`(status now, new)` read under the task row's lock, or None when the task already has `new`.

    The caller read the status a moment before its transaction. Two callers setting the same status at the
    same moment both read it, and both would post "Status: Open → Closed": the second finds the
    status already set and posts nothing, and one that lost a race to a DIFFERENT status names the
    status it really replaced. Call it first thing inside the caller's `transaction.atomic()`, before
    the status is written (the lock is held to the end of that transaction)."""
    current = (
        TaskMaster.objects.select_for_update()
        .filter(task_id=task_id)
        .values_list("status", flat=True)
        .first()
    )
    if current is None or current == new:
        return None
    return (current, new)


def post_status_change_comment(
    *, task_id: int, actor_id: str, old: str, new: str, reason: Any
) -> TaskComments | None:
    """Post "Status: <old> → <new>: <reason>" on the task as `actor_id`. None when nothing changed
    or no reason was given.

    Call it inside the same `transaction.atomic()` as the status write, so a status never lands
    without its comment (and a rolled-back bulk batch leaves no comments). The thread mirror and the
    live push run after commit. Participants (assignee, collaborators, earlier commenters) are NOT
    notified: the status change is already on the task, and a bulk change of twenty tasks should not
    ping twenty people; the comment still appears in the task's thread and its comment list.
    """
    reason = clean_status_reason(reason)
    if old == new or not reason:
        # No reason given: the change stands and the reply suggests one; a comment with nothing
        # after the colon would only repeat the history.
        return None
    text = status_comment_text(old, new, reason)
    with transaction.atomic():
        task = TaskMaster.objects.only("task_id").get(task_id=task_id)
        comment = TaskComments.objects.create(
            task=task,
            sender_id=actor_id,
            comment_id=next_task_comment_id(task_id),
            comment_body=[_paragraph(text), _paragraph("")],
        )
    transaction.on_commit(lambda: fan_out_task_comment(comment, notify_participants=False))
    return comment
