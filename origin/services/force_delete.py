"""Force delete: a person asks for their data to be REALLY gone (MIP-1584).

The app's normal deletes are soft (a message is a tombstone, a task is `status = "Deleted"` for 30
days, `purge_deleted_tasks`). Genos must not keep what nobody needs, so these functions are the
immediate, irreversible version, one per object family, all going through the helpers the
automatic purges already use so that every path removes the same things.

WHO MAY (decided 2026-11-22, note "Decision: who can force-delete what (MIP-1584)"; the check is
`may_*`, the caller answers 404 to someone who cannot even see the object and 403 to someone who
can but may not):
  * a chat message: its sender; in a group chat also the group's owner (`resolve_gm_role`). A
    message that belongs to a task (its card, a comment's mirror) goes through the task or the
    comment instead (409 `task_linked`);
  * "all my messages" of a chat: the caller's own rows only. A DM partner's copy is never touched;
  * a task: its creator (the actor of its CREATED history row; `reporter` is client-editable and
    proves nothing) or its project's owner (a task with no project: the team's owner);
  * a task comment: its author;
  * a note: the note's OWNER role (`get_effective_role`, which for a note in a Team Notes folder
    comes from the folder).

WHAT STAYS, and is counted under `left`: a message or comment ROW without any content (the client's
incremental sync reports deletes through it, comment ids are a COUNT + 1 sequence and the mirror's id
derives from it, and a thread root cascades to other people's replies). The highest-numbered task
of a project is emptied but its row stays, so a display id is never handed out twice.
`purge_deleted_tasks` removes that row later.

Each function runs in one transaction, writes ONE `data.force_deleted` security event (ids and
counts, never a title or a word of text), and does the files, the search chunks and the
collaborative document after the commit, best-effort.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from django.core.files.storage import default_storage
from django.db import transaction
from django.db.models import F, Q

from origin.models.chat.unified_models import (
    Activity,
    Channel,
    ChannelKind,
    Flag,
    Message,
    MessageAttachment,
    MessageReaction,
    MessageReminder,
)
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
    TaskTags,
)
from origin.models.task.task_recent_models import TaskRecent
from origin.services import collab_push, member_roles, message_tombstone, realtime_push
from origin.services.activity_suppression import suppress_task_activity
from origin.services.deleted_task_purge import (
    PurgeReport,
    _files_to_remove,
    _inbox_rows,
    _inventory,
    erase_notification_traces,
)
from origin.services.note_delete import (
    FAMILIES,
    DeleteRefused,
    child_note_count,
    delete_note,
    forget_deleted_notes,
    forget_note_attachments,
)
from origin.services.note_satellites import batched_note_cleanup
from origin.services.security_events import record_security_event
from origin.services.task_comment_manage import soft_delete_comment
from origin.services.task_files import remove_now
from origin.views.utils.note_role import NOTE_TYPE_TASK, ROLE_OWNER, get_effective_role

logger = logging.getLogger(__name__)

CONFIRM_VALUE = "delete-permanently"
#: One call takes at most this many of a person's messages; the response says how many remain, and
#: the client calls again. Each message costs about a dozen queries, so a batch must fit a request
#: (2,000 in one transaction risked the gunicorn timeout and a rollback of all of it, MIP-1943).
MY_MESSAGES_BATCH = 200
SCRUBBED_TITLE = "(deleted)"


class ForceDeleteRefused(Exception):
    """A force delete the rules do not allow. `status` is the HTTP code, `code` a stable word."""

    def __init__(self, code: str, message: str, status: int = 403, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


@dataclass
class ForceDeleteReport:
    kind: str
    id: str
    removed: Counter = field(default_factory=Counter)
    left: Counter = field(default_factory=Counter)
    scrubbed: bool = False
    remaining: int = 0

    def as_response(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "deleted": True,
            "kind": self.kind,
            "id": self.id,
            "removed": dict(self.removed),
            "left": dict(self.left),
        }
        if self.kind == "task":
            body["scrubbed"] = self.scrubbed
        if self.kind == "dm_messages":
            body["remaining"] = self.remaining
        return body


def confirmed(data) -> bool:
    return isinstance(data, dict) and data.get("confirm") == CONFIRM_VALUE


def _event(*, request, team_id, kind: str, target_id, report: ForceDeleteReport, **ids) -> None:
    """The one security event. Ids and counts only: no title, no text, no file name."""
    record_security_event(
        SecurityAction.DATA_FORCE_DELETED,
        request=request,
        team_id=team_id,
        target_type=kind,
        target_id=target_id,
        target_label="",
        metadata={
            "kind": kind,
            "id": str(target_id),
            **{k: str(v) for k, v in ids.items() if v is not None},
            "removed": dict(report.removed),
            "left": dict(report.left),
            "scrubbed": report.scrubbed,
        },
    )


def _remove_files_after_commit(names: list[str], family: str) -> None:
    def _run():
        for name in names:
            try:
                default_storage.delete(name)
            except Exception:  # noqa: BLE001 - the row is already gone; never log the file name
                logger.warning("force delete: could not remove a stored file (%s)", family)

    if names:
        transaction.on_commit(_run, robust=True)


# ---------------------------------------------------------------------------------------------
# Who may
# ---------------------------------------------------------------------------------------------


def may_force_delete_message(message: Message, user) -> bool:
    if str(message.sender_id) == str(user.id):
        return True
    channel = message.channel
    if channel.kind == ChannelKind.GM:
        # Strictly the owner: a "host-side member" of an ownerless group (`acts_as_owner`) is not
        # enough for an irreversible delete.
        return member_roles.resolve_gm_role(channel, user.id) == member_roles.OWNER
    return False


def task_creator_id(task_id: int):
    return (
        TaskActivity.objects.filter(task_id=task_id, action_type=TaskActivityActionType.CREATED)
        .order_by("activity_id")
        .values_list("actor_id", flat=True)
        .first()
    )


def may_force_delete_task(task: TaskMaster, user) -> bool:
    creator = task_creator_id(task.task_id)
    if creator is not None and str(creator) == str(user.id):
        return True
    if task.project_id is not None:
        owner = ProjectMaster.objects.filter(project_id=task.project_id).values_list(
            "owner_id", flat=True
        )
        owner_id = owner.first()
    else:
        owner_id = (
            TeamMaster.objects.filter(team_id=task.team_id)
            .values_list("owner_id", flat=True)
            .first()
        )
    return owner_id is not None and str(owner_id) == str(user.id)


def may_force_delete_comment(comment: TaskComments, user) -> bool:
    return comment.sender_id is not None and str(comment.sender_id) == str(user.id)


def may_force_delete_note(family: str, note_id, user) -> bool:
    _model, code = FAMILIES[family]
    return get_effective_role(user.id, code, note_id) == ROLE_OWNER


# ---------------------------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------------------------


def _erase_chat_email_rows(message: Message) -> int:
    """MIP-1950: the PENDING `chats` outbox row of a plain message has the text and the chat url but
    no link to the message or an inbox row (`email_enqueue.enqueue_email_for_message`), so
    `erase_notification_traces` cannot find it. Match it the way it was written, BEFORE the text
    is blanked. Rows already SENT stay (they left the building). An edited message's row quotes the
    text before the edit and does not match: the outbox retention is the backstop."""
    from origin.models.common.notification_models import EmailNotificationEvent
    from origin.services.webpush_dispatch import _chat_url, _truncate

    body = _truncate(message.body_text)
    if not body or message.channel_id is None:
        return 0
    return EmailNotificationEvent.objects.filter(
        status=EmailNotificationEvent.STATUS_PENDING,
        category="chats",
        url=_chat_url(message.channel),
        body=body,
        ts_created_at__gte=message.ts_sent_at,
    ).delete()[0]


def _erase_message(message: Message, report: ForceDeleteReport, files: list[str]) -> None:
    """Everything a message says or hangs off it, except its (now empty) row. In the caller's
    transaction."""
    report.removed["email rows and reminder cards"] += _erase_chat_email_rows(message)
    newly = message_tombstone.tombstone_message(message)
    if newly and message.parent_id is not None:
        Message.objects.filter(pk=message.parent_id, reply_count__gt=0).update(
            reply_count=F("reply_count") - 1
        )
    attachments = list(MessageAttachment.objects.filter(message=message))
    for attachment in attachments:
        if attachment.file and attachment.file.name:
            files.append(attachment.file.name)
    report.removed["attachments"] += MessageAttachment.objects.filter(message=message).delete()[0]
    report.removed["reactions"] += MessageReaction.objects.filter(message=message).delete()[0]
    report.removed["flags"] += Flag.objects.filter(message=message).delete()[0]
    report.removed["reminders"] += MessageReminder.objects.filter(message=message).delete()[0]
    report.removed["email rows and reminder cards"] += erase_notification_traces(
        Activity.objects.filter(message=message), [message.id]
    )
    report.removed["inbox rows"] += Activity.objects.filter(message=message).delete()[0]
    report.removed["messages"] += 1
    report.left["empty message rows"] += 1


def force_delete_message(message: Message, *, actor, request=None) -> ForceDeleteReport:
    if message.task_id is not None:
        raise ForceDeleteRefused(
            "task_linked",
            "This message belongs to a task. Delete the task or the comment instead.",
            409,
        )
    if not may_force_delete_message(message, actor):
        raise ForceDeleteRefused("forbidden", "Only the sender or the group owner can do this.")
    report = ForceDeleteReport("message", str(message.id))
    files: list[str] = []
    with transaction.atomic():
        _erase_message(message, report, files)
        _event(
            request=request,
            team_id=message.channel.team_id,
            kind="message",
            target_id=message.id,
            report=report,
            channel_id=message.channel_id,
        )
        _remove_files_after_commit(files, "message")
    return report


def force_delete_my_messages(
    channel: Channel, *, actor, request=None, limit: int | None = None
) -> ForceDeleteReport:
    """The caller's own messages in one chat, `limit` per call. Other people's rows are never
    read. Task-linked rows (cards, comment mirrors) are skipped."""
    limit = limit or MY_MESSAGES_BATCH
    report = ForceDeleteReport("dm_messages", str(channel.id))
    files: list[str] = []
    todo = (
        Message.objects.filter(channel=channel, sender=actor, task_id__isnull=True)
        .filter(
            message_tombstone.has_text_q()
            | Q(deleted_at__isnull=True)
            | Q(attachments__isnull=False)
            | Q(reactions__isnull=False)
        )
        .distinct()
        .order_by("seq")
    )
    with transaction.atomic():
        batch = list(todo[:limit])
        for message in batch:
            _erase_message(message, report, files)
        report.remaining = todo.count()
        # The open chats (the caller's other tabs, the partner's app) show the blank rows now.
        realtime_push.push_messages(batch, realtime_push.UPDATED)
        _event(
            request=request,
            team_id=channel.team_id,
            kind="dm_messages",
            target_id=channel.id,
            report=report,
            channel_id=channel.id,
        )
        _remove_files_after_commit(files, "message")
    return report


# ---------------------------------------------------------------------------------------------
# Task comments
# ---------------------------------------------------------------------------------------------


def force_delete_comment(comment: TaskComments, *, actor, request=None) -> ForceDeleteReport:
    if not may_force_delete_comment(comment, actor):
        raise ForceDeleteRefused("forbidden", "Only the author can do this.")
    task_id = int(comment.task_id)
    report = ForceDeleteReport("comment", f"{task_id}:{comment.comment_id}")
    with transaction.atomic():
        soft_delete_comment(comment)
        report.removed["comments"] += 1
        report.removed["reactions"] += TaskCommentReactionFact.objects.filter(
            task_id=task_id, comment_id=comment.comment_id
        ).delete()[0]
        report.removed["mentions"] += TaskCommentMentionFact.objects.filter(
            task_id=task_id, comment_id=comment.comment_id
        ).delete()[0]
        from origin.services.unified_writer import task_comment_message_uuid

        mirror = Message.objects.filter(
            id=task_comment_message_uuid(task_id, int(comment.comment_id))
        ).first()
        files: list[str] = []
        if mirror is not None:
            _erase_message(mirror, report, files)
            report.removed["messages"] -= 1  # the mirror is the comment, already counted
            report.left["empty message rows"] -= 1
        report.left["empty comment rows"] += 1
        task = TaskMaster.objects.filter(task_id=task_id).only("team_id").first()
        _event(
            request=request,
            team_id=task.team_id if task else None,
            kind="comment",
            target_id=f"{task_id}:{comment.comment_id}",
            report=report,
            task_id=task_id,
        )
        _remove_files_after_commit(files, "comment")
    return report


# ---------------------------------------------------------------------------------------------
# Notes
# ---------------------------------------------------------------------------------------------


def force_delete_note(family: str, note, *, actor, request=None) -> ForceDeleteReport:
    """`note` is a row of the family's table. The note's own delete already removes the row, its
    permissions, versions, files, recents, favourites, inbox rows and search chunks."""
    note_id = note.note_id
    if not may_force_delete_note(family, note_id, actor):
        raise ForceDeleteRefused("forbidden", "Only the note's owner can do this.")
    report = ForceDeleteReport("note", f"{family}:{note_id}")
    team_id = note.team_id
    with transaction.atomic():
        # The agent's delete tool and the task rule refuse the same way (MIP-1941): sub-pages
        # would be left pointing at nothing, and a teammate's page would lose its folder path.
        children = child_note_count(team_id=team_id, family=family, note_id=note_id)
        if children:
            raise ForceDeleteRefused(
                "has_children",
                "Delete the sub-pages first.",
                409,
                {"children": children},
            )
        try:
            delete_note(team_id=team_id, family=family, note_id=note_id)
        except DeleteRefused as exc:
            raise ForceDeleteRefused("not_found", exc.message, exc.status)
        report.removed["notes"] += 1
        report.left["collaborative document (removed by collab)"] += 1
        _event(
            request=request,
            team_id=team_id,
            kind="note",
            target_id=f"{family}:{note_id}",
            report=report,
            family=family,
        )
        _forget_doc_after_commit(collab_push.note_document_name(family, note_id))
    return report


def _forget_doc_after_commit(name: str) -> None:
    transaction.on_commit(lambda: collab_push.forget_document(name), robust=True)


# ---------------------------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------------------------


def _refusal_for_task(task: TaskMaster) -> None:
    if (
        task.is_milestone
        or MilestoneMaster.objects.filter(
            Q(task_id=task.task_id) | Q(converted_to_task_id=task.task_id)
        ).exists()
    ):
        raise ForceDeleteRefused(
            "milestone", "A milestone's task is deleted with the milestone.", 409
        )
    subtasks = (
        TaskMaster.objects.filter(Q(parent_task_id=task.task_id) | Q(root_task_id=task.task_id))
        .exclude(task_id=task.task_id)
        .count()
    )
    if subtasks:
        raise ForceDeleteRefused(
            "has_subtasks",
            "Delete the sub-tasks first.",
            409,
            {"subtasks": subtasks},
        )


def _is_highest_number(task: TaskMaster) -> bool:
    number = task.project_task_number
    return (
        number is not None
        and task.project_id is not None
        and not TaskMaster.objects.filter(
            project_id=task.project_id, project_task_number__gt=number
        ).exists()
    )


def force_delete_task(task: TaskMaster, *, actor, request=None) -> ForceDeleteReport:
    if not may_force_delete_task(task, actor):
        raise ForceDeleteRefused("forbidden", "Only the task's creator or the project owner.")
    task_id = task.task_id
    report = ForceDeleteReport("task", str(task_id))
    sink = PurgeReport(apply=True, limit=1)
    with transaction.atomic():
        task = TaskMaster.objects.select_for_update().get(task_id=task_id)
        _refusal_for_task(task)
        team_id, project_id = task.team_id, task.project_id
        removes, left = _inventory(task_id)
        note_ids = list(
            TaskNoteMaster.objects.filter(task_id=task_id).values_list("note_id", flat=True)
        )
        report.removed["email rows and reminder cards"] += erase_notification_traces(
            _inbox_rows(task_id),
            Message.objects.filter(task_id=task_id).values_list("id", flat=True),
        )
        _inbox_rows(task_id).delete()
        for message in Message.objects.filter(task_id=task_id):
            message_tombstone.tombstone_message(message)
        forget_note_attachments(NOTE_TYPE_TASK, note_ids)
        files = _files_to_remove(task_id, sink)
        # An edge to a task that is still live goes too: the person asked, and the other task's
        # status is re-derived by the dependency signals.
        scrub = _is_highest_number(task)
        with batched_note_cleanup(), suppress_task_activity():
            TaskDependency.objects.filter(
                Q(blocker_task_id=task_id) | Q(blocked_task_id=task_id)
            ).delete()
            if scrub:
                _scrub(task)
            else:
                task.delete()
        forget_deleted_notes(NOTE_TYPE_TASK, note_ids)
        from origin.models.common.item_activity_models import MemberItemDay
        from origin.models.common.member_importance_models import MemberImportanceMark

        MemberItemDay.objects.filter(task_id=task_id).delete()
        MemberImportanceMark.objects.filter(task_id=task_id).delete()

        report.removed.update(removes)
        report.left.update({k: v for k, v in left.items() if k != "member counters"})
        report.scrubbed = scrub
        if scrub:
            report.left["empty task row (display id kept)"] += 1
        _event(
            request=request,
            team_id=team_id,
            kind="task",
            target_id=task_id,
            report=report,
            project_id=project_id,
        )
        names = list(files)
        transaction.on_commit(lambda: _after_commit(task_id, note_ids, names), robust=True)
    return report


def _scrub(task: TaskMaster) -> None:
    """Empty a task whose row must stay (its number is the project's highest): everything the
    person wrote and everything that hangs off it goes; the row keeps an id-only shell that
    `purge_deleted_tasks` removes after the grace."""
    task_id = task.task_id
    TaskComments.objects.filter(task_id=task_id).delete()
    TaskCommentReactionFact.objects.filter(task_id=task_id).delete()
    TaskCommentMentionFact.objects.filter(task_id=task_id).delete()
    TaskAttachments.objects.filter(task_id=task_id).delete()
    TaskBodyAttachmentFact.objects.filter(task_id=task_id).delete()
    TaskNoteMaster.objects.filter(task_id=task_id).delete()
    TaskActivity.objects.filter(task_id=task_id).delete()
    TaskDelivery.objects.filter(task_id=task_id).delete()
    TaskTags.objects.filter(task_id=task_id).delete()
    TaskRecent.objects.filter(task_id=task_id).delete()
    task.collaborators.clear()
    TaskMaster.objects.filter(pk=task.pk).update(
        linked_calendar_event_id=None,
        linked_calendar_id=None,
        title=SCRUBBED_TITLE,
        content=None,
        links=None,
        tags=None,
        custom_field_values=None,
        mentioned_user_ids=None,
        assignee=None,
        reporter=None,
        milestone=None,
        sprint=None,
        due_date=None,
        start_date=None,
        status="Deleted",
        is_deleted=True,
    )
    # The one history row that tells `purge_deleted_tasks` when it became Deleted. Ids only.
    TaskActivity.objects.create(
        task_id=task_id,
        team_id=task.team_id,
        project_id=task.project_id,
        action_type=TaskActivityActionType.STATUS,
        field_name="status",
        new_value="Deleted",
    )


def _after_commit(task_id: int, note_ids: list[int], files: list[str]) -> None:
    if files:
        remove_now(files, family="task")
    from origin.services.project_delete import _forget_search

    _forget_search([task_id], [], note_ids, [])
    collab_push.forget_document(f"task-body:{task_id}")
    for note_id in note_ids:
        collab_push.forget_document(collab_push.note_document_name("task", note_id))
