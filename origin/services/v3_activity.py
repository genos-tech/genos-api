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

import logging
import uuid
from typing import Iterable, List, Optional

from django.db.models import Q

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
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

# Channel kinds whose plain (non-mention / non-thread) top-level messages
# produce a per-recipient MESSAGE activity. PM (kind=3) is deliberately
# excluded: a project channel's feed is driven by task cards / mentions /
# comments, and a per-member row on every PM message would be noise (the
# product decision behind the chat-activity feedback round).
_MESSAGE_ACTIVITY_KINDS = (ChannelKind.DM, ChannelKind.GM, ChannelKind.MDM)


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

        task = TaskMaster.objects.select_related("project").filter(task_id=surface_task_id).first()
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
    # Which deterministic PKs already exist? `ignore_conflicts=True` makes
    # bulk_create silently skip them, but it can't tell the caller which
    # rows were genuinely inserted vs collapsed onto an existing row. A
    # task/note body is re-saved repeatedly, so without this distinction
    # every save would re-broadcast + re-PUSH an unchanged mention. We
    # diff against the pre-existing ids and return ONLY the new rows, so
    # callers (WS broadcast + web push) act on first-mention only.
    existing_ids = set(
        Activity.objects.filter(id__in=[r.id for r in rows]).values_list("id", flat=True)
    )
    # `ignore_conflicts=True`: a re-save re-mentioning the same user
    # collapses onto the existing deterministic PK rather than raising.
    Activity.objects.bulk_create(rows, ignore_conflicts=True)
    new_rows = [r for r in rows if r.id not in existing_ids]
    logger.info(
        "[v3_activity] surface mentions: surface=%s entity=%s new=%s existing=%s removed=%s",
        surface_type,
        entity_key,
        len(new_rows),
        len(existing_ids),
        len(removed),
    )
    return new_rows


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
        str(uid) for uid in mentioned_user_ids if uid and (not skip_actor or str(uid) != actor_id)
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
    *,
    reply: Message,
    parent: Optional[Message],
    actor: CustomUser,
    exclude_recipient_ids: Iterable[str] = (),
) -> List[Activity]:
    """`Activity(type=THREAD_REPLY)` for every prior participant of the
    thread when a user posts a reply — the thread root's author plus
    everyone who already replied (a "thread participant" fan-out, matching
    the task-comment participant fan-out). Previously only the immediate
    parent's author was notified, so earlier repliers were silently
    dropped from their own threads.

    Recipients = distinct senders of the thread root + all its replies,
    minus the actor (no self-ping), any hard-deleted (null) sender, and
    `exclude_recipient_ids`. The exclude set is how the caller enforces
    "mention beats reply": a thread reply that also @-mentions a
    participant produces a MENTION activity for them, so they're excluded
    here to avoid a duplicate THREAD_REPLY row in the same feed (the
    user-reported double-activity on thread mentions). Mirrors the
    already-@mentioned exclusion the plain-message push and the
    task-comment participant fan-out already apply.

    Returns `[]` when `parent` is null or the actor is the only
    remaining participant.
    """
    if parent is None:
        logger.info("[v3_activity] thread_reply skipped: parent is None")
        return []
    # The reply is already persisted with `thread_root` set; fall back to
    # the parent id for a direct reply to a top-level message.
    root_id = reply.thread_root_id or parent.id
    actor_id = str(actor.id)
    excluded = {str(u) for u in exclude_recipient_ids if u}
    channel = reply.channel

    # DM / MDM: notify every non-actor channel member, regardless of whether
    # they have sent a message in the thread yet.  The participant-tracking
    # approach (sender-based lookup below) silently produces an empty set when
    # the actor is also the only prior sender — e.g. User A sends a DM, then
    # User A replies to their own message before User B has written anything.
    # In a two-person DM that means nobody is notified.  Using the member list
    # directly is both correct (the other party must always be told) and cheap
    # (DM/MDM channels are small).
    if channel.kind in (ChannelKind.DM, ChannelKind.MDM):
        member_ids = (
            ChannelMember.objects.filter(channel=channel, is_deleted=False)
            .exclude(user_id=actor_id)
            .values_list("user_id", flat=True)
        )
        recipients = {str(uid) for uid in member_ids if str(uid) not in excluded}
    else:
        participant_ids = (
            Message.objects.filter(Q(id=root_id) | Q(thread_root_id=root_id))
            .exclude(sender_id=None)
            .values_list("sender_id", flat=True)
            .distinct()
        )
        recipients = {
            str(s) for s in participant_ids if str(s) != actor_id and str(s) not in excluded
        }

    if not recipients:
        logger.info("[v3_activity] thread_reply skipped: no other participants")
        return []
    rows = [
        Activity(
            team_id=_team_id_for_channel(channel),
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.THREAD_REPLY,
            channel=channel,
            message=reply,
            meta={"parent_message_id": str(parent.id)},
        )
        for uid in recipients
    ]
    Activity.objects.bulk_create(rows)
    logger.info(
        "[v3_activity] thread_reply created: count=%s root=%s channel_kind=%s",
        len(rows),
        root_id,
        channel.kind,
    )
    return rows


def create_message_activities(
    *,
    message: Message,
    recipient_ids: Iterable[str],
    actor: CustomUser,
) -> List[Activity]:
    """`Activity(type=MESSAGE)` per recipient for a plain TOP-LEVEL message
    so a DM/GM/MDM message appears in the recipient's activity feed instead
    of being a web-push only.

    Scoped and de-duped:
      - Only DM/GM/MDM channels (`_MESSAGE_ACTIVITY_KINDS`); PM is excluded.
      - System-user (project bot) senders produce nothing — task-create /
        status cards aren't user "messages" (mirrors the bot-suppression in
        the FE notification router).
      - The actor and falsy ids are skipped; the caller is expected to pass
        `recipient_ids` already minus anyone who got a more-specific MENTION
        activity for this message (mention beats a plain-message row).

    The caller appends the returned rows to the broadcast list so each
    recipient's sidebar updates live via `activity.created`. These rows are
    intentionally NOT routed through `schedule_push_for_activities` — the
    plain-message web push is sent separately via `schedule_push_for_message`,
    so pushing here too would double-notify. (`_push_spec` also returns None
    for the MESSAGE type, so it's a no-op there as defense-in-depth.)
    """
    channel = message.channel
    if channel is None or channel.kind not in _MESSAGE_ACTIVITY_KINDS:
        return []
    if getattr(message.sender, "is_system_user", False):
        return []
    actor_id = str(actor.id)
    seen = set()
    targets = []
    for uid in recipient_ids:
        s = str(uid) if uid is not None else ""
        if not s or s == actor_id or s in seen:
            continue
        seen.add(s)
        targets.append(s)
    if not targets:
        return []
    rows = [
        Activity(
            team_id=_team_id_for_channel(channel),
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.MESSAGE,
            channel=channel,
            message=message,
            meta={},
        )
        for uid in targets
    ]
    Activity.objects.bulk_create(rows)
    logger.info(
        "[v3_activity] message activities created: count=%s channel_kind=%s",
        len(rows),
        channel.kind,
    )
    return rows


def create_comment_participant_activities(
    *,
    message: Message,
    recipient_ids: Iterable[str],
    actor: CustomUser,
) -> List[Activity]:
    """`Activity(type=THREAD_REPLY)` per task-comment participant.

    A plain (no-@mention) task comment otherwise notifies nobody — only
    `create_mention_activities` runs on the comment mirror. This fans the
    comment out to the resolved participant set (the caller passes the
    task's assignee + prior commenters). The recipients ride the normal
    THREAD_REPLY activity path; because the mirror message carries
    `metadata.taskCommentId`, the web client routes them to the
    `task_comments` notification category.

    Skips the actor and falsy ids and dedupes. The caller is responsible
    for excluding @-mentioned users (they receive the more specific
    MENTION activity instead).
    """
    actor_id = str(actor.id)
    seen = set()
    targets = []
    for uid in recipient_ids:
        s = str(uid) if uid is not None else ""
        if not s or s == actor_id or s in seen:
            continue
        seen.add(s)
        targets.append(s)
    if not targets:
        return []
    channel = message.channel
    rows = [
        Activity(
            team_id=_team_id_for_channel(channel),
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.THREAD_REPLY,
            channel=channel,
            message=message,
            meta={},
        )
        for uid in targets
    ]
    Activity.objects.bulk_create(rows)
    return rows


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
