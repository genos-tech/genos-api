"""Activity-feed producer for the unified messaging schema.

Called from `message_views.MessagesView.post` and
`reaction_views_v3.MessageReactionsView.post` after the underlying row
(Message / MessageReaction) is committed. Writes `Activity` rows for
every user who should see a sidebar entry — direct @-mentions on a new
message, the message sender when someone reacts to it, and the thread
root's sender on a thread reply.

Returned `Activity` rows are picked up by the WS layer
(`socketio_events_v3/message_handlers` and `reaction_handlers`) and
broadcast as `activity.created` to each recipient's `user:{id}` room
so their sidebar updates without a refresh.
"""

from __future__ import annotations

import uuid
from typing import Iterable, List, Optional

import logging

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    Message,
)
from origin.models.common.user_models import CustomUser

logger = logging.getLogger(__name__)

# Fixed namespace for deterministic surface-activity ids. A surface
# @-mention has no natural unique key (Activity has a uuid4 PK), and
# task/note bodies are edited repeatedly, so a naive create-per-save
# piles up duplicate rows. Deriving the PK from (surface, entity,
# recipient) makes re-mentioning the same user on a re-save collapse to
# the existing row (idempotent), and lets a removed mention be deleted
# by the same key.
_SURFACE_ACTIVITY_NS = uuid.UUID("6f9619ff-8b86-d011-b42d-00cf4fc964ff")

# Legacy chat_type namespace for channel-less mention surfaces. Mirrors
# the frontend `chatListItemForActivity` routing + the legacy
# `activity_views` docstring.
SURFACE_TASK_BODY = 5
SURFACE_PERSONAL_NOTE = 6
SURFACE_TASK_NOTE = 7
SURFACE_CHAT_NOTE = 8


def surface_activity_id(*, surface_type: int, entity_key: str, recipient_id: str) -> uuid.UUID:
    """Deterministic Activity PK for a surface @-mention so re-saves are
    idempotent and removed mentions can be deleted by key."""
    return uuid.uuid5(_SURFACE_ACTIVITY_NS, f"{surface_type}:{entity_key}:{recipient_id}")


def create_surface_mention_activities(
    *,
    team_id,
    actor: Optional[CustomUser],
    surface_type: int,
    entity_key: str,
    newly_mentioned_user_ids: Iterable[str],
    removed_user_ids: Iterable[str] = (),
    meta: Optional[dict] = None,
) -> List[Activity]:
    """Create / delete channel-less surface MENTION activities (task body
    + the three note types). Delta-driven so repeated body edits don't
    pile up rows: `newly_mentioned_user_ids` get a row,
    `removed_user_ids` have theirs deleted.

    Unlike `create_mention_activities`, this does NOT skip the actor —
    tagging yourself in a task body / note is a deliberate reminder, and
    is exactly what the user reported wanting.

    Recipient ids arrive from a JSON column with no FK, so they're
    validated against active team membership before insert (an
    `Activity.recipient` FK violation would otherwise 500 the whole
    request — `bulk_create(ignore_conflicts=True)` suppresses only
    UNIQUE, not FK, violations).
    """
    from origin.models.common.team_models import TeamMembers

    meta = meta or {}

    # Backfill the task's human-readable display id ("<code>-<n>", e.g.
    # "PRG-123") so the activity chip shows it instead of the bare
    # "#<task_id>" fallback. The surface emit path carries the task FK
    # (`taskId`) but not the computed display id — the note save knows the
    # task, not its project code — so resolve it here at the single
    # convergence point for every surface producer. Guarded on `taskId`,
    # so the task-less note surfaces (personal=6 / chat=8) are untouched;
    # task body (5) and task note (7) get it. Skipped when the caller
    # already supplied `displayId`.
    surface_task_id = meta.get("taskId")
    if surface_task_id and not meta.get("displayId"):
        from origin.models.task.task_models import TaskMaster

        task = (
            TaskMaster.objects.select_related("project").filter(task_id=surface_task_id).first()
        )
        if task is not None:
            meta["displayId"] = task.display_id

    removed = [str(u) for u in removed_user_ids if u]
    if removed:
        del_ids = [
            surface_activity_id(surface_type=surface_type, entity_key=entity_key, recipient_id=r)
            for r in removed
        ]
        Activity.objects.filter(id__in=del_ids).delete()

    targets = [str(u) for u in newly_mentioned_user_ids if u]
    if not targets:
        return []
    valid = {
        str(uid)
        for uid in TeamMembers.objects.filter(
            team_id=team_id, attendee_id__in=targets, is_deleted=False
        ).values_list("attendee_id", flat=True)
    }
    rows = [
        Activity(
            id=surface_activity_id(
                surface_type=surface_type, entity_key=entity_key, recipient_id=uid
            ),
            team_id=team_id,
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.MENTION,
            channel=None,
            message=None,
            surface_type=surface_type,
            meta=meta,
        )
        for uid in targets
        if uid in valid
    ]
    if not rows:
        return []
    # `ignore_conflicts=True`: a re-save re-mentioning the same user
    # collapses onto the existing deterministic PK rather than raising.
    Activity.objects.bulk_create(rows, ignore_conflicts=True)
    logger.info(
        "[v3_activity] surface mentions: surface=%s entity=%s created=%s removed=%s",
        surface_type,
        entity_key,
        len(rows),
        len(removed),
    )
    return rows


def _team_id_for_channel(channel: Channel):
    """`Activity.team` mirrors the channel's team. Cheap accessor that
    works whether `channel` was prefetched via select_related or not."""
    return channel.team_id


def create_mention_activities(
    *,
    message: Message,
    mentioned_user_ids: Iterable[str],
    actor: CustomUser,
    skip_actor: bool = True,
) -> List[Activity]:
    """One `Activity(type=MENTION)` per directly @-mentioned user.

    Skips:
      - the actor themselves when `skip_actor` (the default — mentioning
        yourself in a normal chat message doesn't ping). Task-comment
        mentions pass `skip_actor=False` so tagging yourself in a comment
        still produces a feed entry (consistent with the task-body / note
        surfaces, and verifiable in a single-account demo).
      - empty / falsy ids
      - users that don't exist (silent skip — the mention row was already
        rejected at the MessageMention.bulk_create boundary if the id
        wasn't valid).
    """
    actor_id = str(actor.id)
    targets = [
        str(uid)
        for uid in mentioned_user_ids
        if uid and (not skip_actor or str(uid) != actor_id)
    ]
    if not targets:
        return []
    channel = message.channel
    rows = [
        Activity(
            team_id=_team_id_for_channel(channel),
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.MENTION,
            channel=channel,
            message=message,
            meta={},
        )
        for uid in targets
    ]
    Activity.objects.bulk_create(rows)
    return rows


def create_self_assign_activity(*, message: Message, actor: CustomUser) -> List[Activity]:
    """Surface a self-assigned task as a MENTION activity.

    A task-create posts a task-card header `Message` whose body
    @-mentions the assignee. `create_mention_activities` skips the actor,
    so assigning a task to *yourself* produced no sidebar entry at all —
    the user reported exactly this. Assigning the task to someone else is
    already covered by the normal mention fan-out, so this only fires the
    self case (assignee == actor) to avoid a duplicate row.

    Scoped to the top-level task-card message (`task_id` set,
    `is_thread_reply` false) so task comments don't re-trigger it. Uses
    `ActivityType.MENTION` rather than `TASK_ASSIGN` because the legacy
    activity feed + notification router only render mention/reaction/
    thread-reply types — a `TASK_ASSIGN` row wouldn't surface today.
    """
    if message.is_thread_reply or message.task_id is None:
        return []
    from origin.models.task.task_models import TaskMaster

    assignee_id = (
        TaskMaster.objects.filter(task_id=message.task_id)
        .values_list("assignee_id", flat=True)
        .first()
    )
    if assignee_id is None or str(assignee_id) != str(actor.id):
        return []
    channel = message.channel
    row = Activity.objects.create(
        team_id=_team_id_for_channel(channel),
        recipient_id=str(assignee_id),
        actor=actor,
        activity_type=ActivityType.MENTION,
        channel=channel,
        message=message,
        meta={"taskAssign": True},
    )
    logger.info(
        "[v3_activity] self-assign mention created: id=%s recipient=%s task=%s",
        row.id,
        assignee_id,
        message.task_id,
    )
    return [row]


def create_thread_reply_activity(
    *, reply: Message, parent: Optional[Message], actor: CustomUser
) -> List[Activity]:
    """Single `Activity(type=THREAD_REPLY)` for the thread root's sender
    when a different user posts a reply.

    Returns `[]` when:
      - `parent` is null (the reply has no parent — shouldn't happen
        for a thread reply, but defend against the caller)
      - the parent's sender is the same user posting the reply
      - the parent's sender was hard-deleted (sender FK is null)
    """
    if parent is None or parent.sender_id is None:
        logger.info(
            "[v3_activity] thread_reply skipped: parent=%s parent.sender_id=%s",
            parent and parent.id,
            parent and parent.sender_id,
        )
        return []
    actor_id = str(actor.id)
    parent_sender_id = str(parent.sender_id)
    if parent_sender_id == actor_id:
        logger.info(
            "[v3_activity] thread_reply skipped (self-reply): parent_sender=%s actor=%s",
            parent_sender_id,
            actor_id,
        )
        return []
    channel = reply.channel
    row = Activity.objects.create(
        team_id=_team_id_for_channel(channel),
        recipient_id=parent_sender_id,
        actor=actor,
        activity_type=ActivityType.THREAD_REPLY,
        channel=channel,
        message=reply,
        meta={"parent_message_id": str(parent.id)},
    )
    logger.info(
        "[v3_activity] thread_reply created: id=%s recipient=%s channel_kind=%s",
        row.id,
        parent_sender_id,
        channel.kind,
    )
    return [row]


def create_reaction_activity(*, message: Message, emoji: str, actor: CustomUser) -> List[Activity]:
    """Single `Activity(type=REACTION)` for the message sender when
    someone else reacts. Self-reactions produce no row."""
    if message.sender_id is None:
        return []
    actor_id = str(actor.id)
    sender_id = str(message.sender_id)
    if sender_id == actor_id:
        return []
    channel = message.channel
    row = Activity.objects.create(
        team_id=_team_id_for_channel(channel),
        recipient_id=sender_id,
        actor=actor,
        activity_type=ActivityType.REACTION,
        channel=channel,
        message=message,
        meta={"emoji": emoji},
    )
    return [row]
