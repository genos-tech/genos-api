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

## Recipients who are already looking

A conversational activity tells someone about a conversation they are not
in. When they ARE in it — the DM open on screen right now, the message
they would be told about already rendered a few pixels away — the row is
noise, and the feed it lands in is the one place the product uses to say
"here is what you missed". So every conversational producer here drops
recipients whom `services/presence` reports as viewing that message's
surface: plain messages, thread replies, task comments, @-mentions and
reactions alike. Being @-mentioned or reacted to in a chat you are
reading is the same non-event as being messaged in it — you watched it
happen.

The surface is the message's own (`message_surface`), never the
recipient's whereabouts in general, so a mention inside a thread is
suppressed only for someone reading THAT thread — having the channel
open behind the pane still earns the row.

The one @-mention that survives is a system-user one: a task-assignment
card is a bot handing you work, not a conversation, and PM channels write
no plain-message row, so that feed entry is the only durable trace of it.

Suppression is per-recipient, which `Activity` already is — the other
members of the same channel still get their row.
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


def channel_surface(channel_id) -> str:
    """Viewing token for a channel's main timeline.

    These three builders are the backend half of a contract with the
    frontend, which mints the same strings from its notification "active
    surface" (`services/notifications/viewingSurface.ts`). Both sides have
    a test pinning the literal format; change one and the other stops
    matching, which fails open (the row gets written) rather than loudly.
    """
    return f"channel:{channel_id}"


def thread_surface(thread_root_message_id) -> str:
    """Viewing token for an open thread pane, keyed on the thread root."""
    return f"thread:{thread_root_message_id}"


def task_surface(task_id) -> str:
    """Viewing token for an open task preview, whose comment stream is the
    conversation surface for that task."""
    return f"task:{task_id}"


def message_surface(message: Message) -> str:
    """The one surface that shows `message` to a reader.

    Each branch picks the place the message is actually *read*, which is
    not always the channel it is stored in:

      - A task comment is mirrored into the project channel, but people
        read it in the task preview's comment stream (`metadata
        .taskCommentId` is what marks the mirror — a task's own card
        message carries `task_id` too and is read in the channel).
      - A thread reply lives behind a pane, so the channel timeline does
        NOT show it — hence thread before channel, and why someone
        reading the main timeline still hears about replies.

    Kept single-valued to agree exactly with the surface each fan-out
    producer already picks for the same message. If a mention were judged
    against a wider set than the thread-reply fan-out that follows it,
    suppressing the mention would just re-file the recipient under the
    other type — a moved row, not a silenced one.
    """
    metadata = message.metadata or {}
    if message.task_id is not None and metadata.get("taskCommentId") is not None:
        return task_surface(message.task_id)
    if message.thread_root_id:
        return thread_surface(message.thread_root_id)
    if message.channel_id:
        return channel_surface(message.channel_id)
    return ""


def _drop_active_viewers(recipient_ids: Iterable[str], surface: str, *, kind: str) -> List[str]:
    """`recipient_ids` minus whoever is looking at `surface` right now."""
    from origin.services import presence

    ids = list(recipient_ids)
    if not ids or not surface:
        return ids
    viewers = presence.viewers_of(surface, ids)
    if not viewers:
        return ids
    logger.info(
        "[v3_activity] %s: skipped %s of %s recipient(s) viewing %s",
        kind,
        len(viewers),
        len(ids),
        surface,
    )
    return [uid for uid in ids if uid not in viewers]


def surface_activity_id(*, surface_type: int, entity_key: str, recipient_id: str) -> uuid.UUID:
    """Deterministic Activity PK for a surface @-mention so re-saves are
    idempotent and removed mentions can be deleted by key."""
    return uuid.uuid5(_SURFACE_ACTIVITY_NS, f"{surface_type}:{entity_key}:{recipient_id}")


def _backfill_task_routing_meta(meta: dict) -> None:
    """Fill the task routing fields the emit path couldn't compute from the
    task FK alone, mutating `meta` in place. Shared by every surface
    producer (mention + comment) so the deep-link + chip resolve the same
    way regardless of which one wrote the row.

    Guarded on `taskId`, so the task-less note surfaces (personal=6 /
    chat=8) are untouched; task body (5) and task note (7) get it.
      - `displayId` ("<code>-<n>", e.g. "PRG-123"): the note/comment save
        knows the task, not its project code, so the chip would otherwise
        show the bare "#<task_id>" fallback.
      - `projectId`: a comment on the TASK BODY only knows its task id (that
        editor has no project in scope), but the task-body deep-link needs
        the project id. Fill it from the task's project FK when absent.
    Both are skipped when the caller already supplied them.
    """
    surface_task_id = meta.get("taskId")
    if surface_task_id and not (meta.get("displayId") and meta.get("projectId")):
        from origin.models.task.task_models import TaskMaster

        task = TaskMaster.objects.select_related("project").filter(task_id=surface_task_id).first()
        if task is not None:
            if not meta.get("displayId"):
                meta["displayId"] = task.display_id
            if not meta.get("projectId") and task.project_id is not None:
                meta["projectId"] = task.project_id


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

    # Backfill task routing fields the emit path couldn't compute from the
    # task FK alone — resolved at the single convergence point for every
    # surface producer.
    _backfill_task_routing_meta(meta)

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


def _task_stakeholders(task_id) -> set:
    """Assignee + collaborators of a task — who to tell about a comment
    left on the task BODY (surface 5) even without an @-mention.

    Prior *task-comment* senders (the separate legacy task-comment feature)
    are intentionally NOT included: a BlockNote inline comment's own prior
    authors live in the collab Yjs doc, invisible to Django, so the durable
    server-side stakeholder set for a task is its assignee + collaborators —
    "the task's assigned user plus stakeholders", exactly the report's ask.
    """
    from origin.models.task.task_models import TaskMaster

    out: set = set()
    if task_id is None:
        return out
    assignee_id = (
        TaskMaster.objects.filter(task_id=task_id).values_list("assignee_id", flat=True).first()
    )
    if assignee_id is not None:
        out.add(str(assignee_id))
    for collab_id in (
        TaskMaster.objects.filter(task_id=task_id)
        .values_list("collaborators__id", flat=True)
        .distinct()
    ):
        if collab_id is not None:
            out.add(str(collab_id))
    return out


def _note_grant_user_ids(note_type: int, note_id) -> set:
    """Explicit per-note permission grantees (NotePermissionMaster). Shared
    tail for task-note (note_type=2) and chat-note (note_type=3) recipient
    resolution — a note shared with someone via the note-sharing dialog is a
    stakeholder in comments left on it."""
    from origin.models.note.common_note_models import NotePermissionMaster

    return {
        str(uid)
        for uid in NotePermissionMaster.objects.filter(
            note_type=note_type, note_id=note_id
        ).values_list("user_id", flat=True)
        if uid is not None
    }


def _comment_recipients(surface_type: int, meta: dict) -> set:
    """Owner + stakeholders to notify about a comment on a surface, resolved
    ENTIRELY server-side from the authoritative id on the row.

    For task/chat notes (surfaces 7/8) the routing FKs (`project_id`,
    `channel_id`) that decide who can reach the note live on the note row
    itself — so we resolve them from `noteId` alone and never trust the
    frontend-forwarded `taskId`/`projectId`/`chatId`, which are only carried
    for the deep-link. That is both cleaner (one source of truth) and safer
    (a client can't redirect a comment's fan-out to an arbitrary project).

      - 5 task body   -> task assignee + collaborators
      - 6 personal    -> note owner + explicit grants
      - 7 task note   -> note owner + its project's members + explicit grants
      - 8 chat note   -> note owner + its channel's members + explicit grants
                         (owner-only when the note has outlived its channel)
    """
    from origin.views.utils.mention_membership import (
        _grantees_of_personal_note,
        _members_of_channel,
        _members_of_project,
    )

    meta = meta or {}

    if surface_type == SURFACE_TASK_BODY:
        return _task_stakeholders(meta.get("taskId"))

    note_id = meta.get("noteId")
    if note_id is None:
        return set()

    if surface_type == SURFACE_PERSONAL_NOTE:
        # owner + explicit grants (personal notes have no implicit path).
        return _grantees_of_personal_note(note_id)

    if surface_type == SURFACE_TASK_NOTE:
        from origin.models.note.task_note_models import TaskNoteMaster

        row = TaskNoteMaster.objects.filter(note_id=note_id).values("project_id", "owner_id").first()
        if row is None:
            return set()
        out: set = set()
        if row["owner_id"] is not None:
            out.add(str(row["owner_id"]))
        out |= _members_of_project(row["project_id"])
        out |= _note_grant_user_ids(2, note_id)
        return out

    if surface_type == SURFACE_CHAT_NOTE:
        from origin.models.note.chat_note_models import ChatNoteMaster

        row = ChatNoteMaster.objects.filter(note_id=note_id).values("channel_id", "owner_id").first()
        if row is None:
            return set()
        out = set()
        if row["owner_id"] is not None:
            out.add(str(row["owner_id"]))
        if row["channel_id"] is not None:
            out |= _members_of_channel(row["channel_id"])
        out |= _note_grant_user_ids(3, note_id)
        return out

    return set()


def create_surface_comment_activities(
    *,
    team_id,
    actor: Optional[CustomUser],
    surface_type: int,
    entity_key: str,
    meta: Optional[dict] = None,
    mentioned_user_ids: Iterable[str] = (),
) -> List[Activity]:
    """Notify a surface's owner + stakeholders that a comment was left on it,
    even when the comment @-mentions no one — the general-comment fan-out the
    report asked for (the note owner / task assignee + stakeholders must hear
    about a comment the same way they'd hear about a mention).

    The twin of `create_surface_mention_activities`, with three differences:

      - **Recipients are resolved server-side** from the authoritative id on
        the surface (`_comment_recipients`) rather than passed in — a comment
        has no explicit target list the way a mention does.
      - **`activity_type = THREAD_REPLY`**, not MENTION: this is "someone
        commented", and THREAD_REPLY is the type the FE feed + notification
        router already render for comment-shaped events (a bespoke type
        wouldn't surface today — see `create_self_assign_activity`). It rides
        the host `surface_type` + `meta.isComment` so the FE routes it to the
        note/task it was left on and reads "commented", not "replied".
      - **@-mentioned users are excluded**: they receive the more-specific
        MENTION row from `create_surface_mention_activities`, so a
        participant row for them would double-notify (the same "mention beats
        the plain fan-out" rule the task-comment and thread-reply producers
        apply).

    Idempotent on a distinct per-comment `entity_key`
    (`comment-participant:{id}`), so the deterministic PK never collides with
    the mention producer's `comment:{id}` for the same comment, and a retried
    emit collapses onto the existing row instead of duplicating.
    """
    from origin.models.common.team_models import TeamMembers

    meta = dict(meta or {})
    # Tags the row for the "commented" FE render + the comment push wording,
    # and (with surface_type) routes it to the host note/task. Rides the host
    # surface exactly like a comment mention.
    meta["isComment"] = True
    meta["isCommentParticipant"] = True
    _backfill_task_routing_meta(meta)

    actor_id = str(actor.id) if actor is not None else None
    mentioned = {str(u) for u in (mentioned_user_ids or []) if u}

    recipients = _comment_recipients(surface_type, meta)
    if actor_id:
        recipients.discard(actor_id)
    recipients -= mentioned
    if not recipients:
        return []

    # Recipient ids come from membership/permission tables with no FK to
    # Activity.recipient, and a since-removed member would 500 the insert —
    # validate against active team membership first (bulk_create's
    # ignore_conflicts suppresses only UNIQUE, not FK, violations).
    valid = {
        str(uid)
        for uid in TeamMembers.objects.filter(
            team_id=team_id, attendee_id__in=list(recipients), is_deleted=False
        ).values_list("attendee_id", flat=True)
    }
    targets = [uid for uid in recipients if uid in valid]
    if not targets:
        return []

    rows = [
        Activity(
            id=surface_activity_id(
                surface_type=surface_type, entity_key=entity_key, recipient_id=uid
            ),
            team_id=team_id,
            recipient_id=uid,
            actor=actor,
            activity_type=ActivityType.THREAD_REPLY,
            channel=None,
            message=None,
            surface_type=surface_type,
            meta=meta,
        )
        for uid in targets
    ]
    # Return only genuinely-new rows so a retried emit (same comment_id)
    # doesn't re-broadcast / re-push — same idempotency the mention producer
    # relies on.
    existing_ids = set(
        Activity.objects.filter(id__in=[r.id for r in rows]).values_list("id", flat=True)
    )
    Activity.objects.bulk_create(rows, ignore_conflicts=True)
    new_rows = [r for r in rows if r.id not in existing_ids]
    logger.info(
        "[v3_activity] surface comment participants: surface=%s entity=%s new=%s existing=%s",
        surface_type,
        entity_key,
        len(new_rows),
        len(existing_ids),
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
      - anyone reading the surface the mention was written on: they saw it
        arrive (module docstring).

    Callers that treat "was mentioned" as beating another activity type
    must key that on the ids they PASSED IN, not on the rows returned —
    otherwise a mention suppressed here reappears as a thread-reply row.
    """
    actor_id = str(actor.id)
    targets = [
        str(uid) for uid in mentioned_user_ids if uid and (not skip_actor or str(uid) != actor_id)
    ]
    if not targets:
        return []
    # A bot @-mention is an assignment, not a conversation — see the
    # module docstring. Same guard `create_message_activities` uses to keep
    # task-create / status cards out of the plain-message fan-out.
    if not getattr(message.sender, "is_system_user", False):
        targets = _drop_active_viewers(targets, message_surface(message), kind="mention")
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
    # Keyed on the thread root, not the channel: someone reading the main
    # timeline is NOT reading this thread (replies live behind a pane), so
    # they still get told.
    recipients = _drop_active_viewers(recipients, thread_surface(root_id), kind="thread_reply")
    if not recipients:
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
      - Recipients with this channel open on screen are skipped — they are
        reading the message, not missing it (module docstring).

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
    targets = _drop_active_viewers(targets, channel_surface(channel.id), kind="message")
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
    # The comment's own surface is the task preview it renders in, so the
    # token is the task rather than the PM mirror channel — a project
    # member with the PM chat open is not reading this task's comments.
    if message.task_id is not None:
        targets = _drop_active_viewers(targets, task_surface(message.task_id), kind="task_comment")
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
    someone else reacts. Self-reactions produce no row.

    Nothing is written when the sender is reading the surface their
    message is on — the emoji appears under it live, and a feed row saying
    so is a second copy of something already on screen. Reactions on a
    task comment are judged against the task, so this covers the
    comment-reaction case too (`message_surface`).
    """
    if message.sender_id is None:
        return []
    actor_id = str(actor.id)
    sender_id = str(message.sender_id)
    if sender_id == actor_id:
        return []
    still_unaware = _drop_active_viewers([sender_id], message_surface(message), kind="reaction")
    if not still_unaware:
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
