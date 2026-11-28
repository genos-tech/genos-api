"""
Message-level REST endpoints for the unified messaging schema.

`/api/v3/channels/{channel_id}/messages/?since=ISO` — delta sync, top-level messages.
`/api/v3/channels/{channel_id}/threads/?since=ISO` — delta sync, thread replies.
`/api/v3/messages/{message_id}/` — single message detail.

Send/edit/delete and reaction add/remove will live here in a follow-up
commit — those are intentionally paired with the unified SocketIO
handler rewrite so the REST + WS contracts ship together (the WS layer
proxies to the REST layer; see plan §3 in the plan file).

Delta envelope shape (matches `serializers.DeltaEnvelopeSerializer`):
    {
      "server_time": "<iso>",
      "force_full_reload": <bool>,        // omitted when false
      "data": {
        "messages":           [<MessageSerializer>, ...],
        "deletes":            ["<message_uuid>", ...],
        "reactions_changed":  [{ "message_id": "...", "reactions": [...] }],
      }
    }

The frontend persists `server_time` as the next `?since=` value. Soft-deleted
messages come back as rows with `deletedAt` set. `deletes` is the array of
UUIDs no longer in this chat at all since the checkpoint (moved with their
task to another chat, MIP-2505).
"""

from datetime import timedelta

from django.db import transaction
from django.db.models import F, Max, Q
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageMention,
    MessageReaction,
    main_timeline_q,
)
from origin.search_engine.quota import get_message_retention_days
from origin.serializers.chat.unified_serializers import (
    MessageAttachmentSerializer,
    MessageSerializer,
)
from origin.services import (
    message_tombstone,
    message_via,
    moved_messages,
    realtime_push,
    thread_summary_cleanup,
    webhook_enqueue,
)
from origin.services.archive import archived_channel_response
from origin.services.channel_scope import chat_member_rows, is_chat_member
from origin.services.deleted_account_channel_owners import acts_as_owner
from origin.services.mention_extractor import (
    extract_mention_group_ids,
    extract_mentioned_user_ids,
)
from origin.services.share_roles import view_only_channel_response
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    MAX_MESSAGES_PER_DELTA,
    apply_row_count_cap,
    build_delta_response,
    capture_server_time,
    check_since,
)
from origin.views.utils.mention_handler import resolve_group_members
from origin.views.utils.mention_membership import non_member_mentions
from origin.views.utils.request_validators import as_uuid, body_object
from origin.views.utils.upload_validation import check_attachment_upload


def _retention_cutoff(user):
    """Return `(days, cutoff_dt)` for the VIEWING user's message-history
    window, or `(None, None)` when their plan has unlimited history.

    Retention is per-viewer (Slack model): messages older than the
    window are HIDDEN from reads, never deleted — upgrading instantly
    restores them. `get_message_retention_days` resolves the effective
    tier (personal tier or a paying team's plan) and fails open to
    None, so an infra hiccup shows full history rather than hiding
    data.
    """
    days = get_message_retention_days(user.id)
    if days is None:
        return None, None
    return days, timezone.now() - timedelta(days=days)


def _apply_retention(qs, channel, user, *, is_thread_reply):
    """Apply the viewer's retention window to a delta queryset.

    Returns `(qs, retention_dict_or_None)`. Applied AFTER the
    reaction-union so a fresh reaction on an out-of-window message
    can't resurrect it. The `truncated` flag is one indexed EXISTS —
    it drives the client banner only when there actually is hidden
    history in this channel.
    """
    days, cutoff = _retention_cutoff(user)
    if cutoff is None:
        return qs, None
    qs = qs.filter(ts_sent_at__gte=cutoff)
    truncated = Message.objects.filter(
        channel=channel,
        is_thread_reply=is_thread_reply,
        deleted_at__isnull=True,
        ts_sent_at__lt=cutoff,
    ).exists()
    return qs, {"days": days, "cutoff": cutoff.isoformat(), "truncated": truncated}


def _apply_message_since_filter(qs, since):
    """Like `apply_since_filter` but adapted for the new schema's
    `deleted_at` timestamp (vs the legacy `is_deleted` boolean).

    - Full load (since=None): exclude deleted rows entirely (`deleted_at
      IS NULL`).
    - Incremental (since=<ts>): rows whose ts_updated_at > since,
      INCLUDING soft-deleted ones (so the client can apply tombstones
      via the row's `deletedAt` field).
    """
    if since is None:
        return qs.filter(deleted_at__isnull=True)
    return qs.filter(ts_updated_at__gt=since)


def _verify_member_or_404(channel_id, user):
    """Return Channel iff the user is an active member; else raise 404.

    404-not-403 so we don't leak channel existence to non-members. The
    same pattern is used in `channel_views._get_channel_for_user`; this
    helper is duplicated rather than imported to keep the two view files
    independently deletable when one of them is rewritten later.
    """
    try:
        channel = Channel.objects.get(id=channel_id, is_deleted=False)
    except Channel.DoesNotExist:
        raise Http404("Channel not found.")
    if not is_chat_member(channel.id, user.id):
        raise Http404("Channel not found.")
    return channel


def _resolve_thread_root(parent):
    """Given a parent Message, return the thread root id.

    If `parent` is itself a thread reply, the root is `parent.thread_root_id`
    (so the whole thread stays rooted at the original top-level message).
    Otherwise `parent` IS the root.
    """
    if parent.is_thread_reply and parent.thread_root_id is not None:
        return parent.thread_root_id
    return parent.id


def _valid_mention_user_ids(channel, body):
    """Mentioned user ids in `body` that are active members of `channel`.

    Intersecting with `ChannelMember` does double duty: it drops mentions
    of users who aren't in the channel AND guarantees every remaining id
    is a real `CustomUser`. Both `MessageMention.mentioned_user` and the
    v3 `Activity.recipient` inserts are FK'd to the user table, and
    `bulk_create(ignore_conflicts=True)` suppresses only UNIQUE
    violations — NOT foreign-key ones. Without this filter, a body that
    @-mentions a since-deleted account or a stale client-cache id raises
    `IntegrityError`, aborts the atomic send, and 500s the whole message
    (one bad mention makes the channel unsendable). Returns a set of
    `str` ids.
    """
    # Direct @user mentions PLUS @group mentions expanded to their member
    # user ids — the note / task save paths already expand groups via
    # `resolve_group_members`; chat messages used to drop group mentions
    # entirely (a group @-mention notified nobody). The channel-membership
    # intersection below then drops any group member who isn't in the
    # channel, so a group mention only pings members who can see the chat.
    return _split_mentions_by_membership(channel, body)[0]


def _mentioned_user_ids(channel, body) -> set:
    """Everyone `body` mentions, group mentions expanded, whether or not they are in `channel`."""
    mentioned = extract_mentioned_user_ids(body)
    group_ids = extract_mention_group_ids(body)
    if group_ids:
        mentioned = mentioned | resolve_group_members(group_ids, team_id=channel.team_id)
    return mentioned


def _split_mentions_by_membership(channel, body):
    """`(members, non_members)` for the users mentioned in `body`.

    The non-member half is why this is split out: a chat mention of
    someone outside the channel is dropped here and NEVER reaches a
    client, so the sender's chip renders as if it worked while the
    recipient is never told. Surfacing the dropped ids is the only way
    that case is observable at all.
    """
    mentioned = _mentioned_user_ids(channel, body)
    if not mentioned:
        return set(), set()
    valid = {
        str(uid)
        for uid in chat_member_rows(channel)
        .filter(user_id__in=mentioned)
        .values_list("user_id", flat=True)
    }
    return valid, {str(u) for u in mentioned} - valid


def _allocate_seq_and_create_message_with_activities(
    *,
    channel,
    sender,
    body,
    body_text,
    parent_id,
    metadata,
    correlation_id=None,
    self_assign=True,
    via=None,
    also_in_channel=False,
):
    """Wraps `_allocate_seq_and_create_message` and additionally fans out
    `Activity` rows for the recipients who should see a sidebar entry.

    Returns `(message, activities)` so the caller can include the
    activities in the wire response — the WS proxy layer reads them and
    broadcasts `activity.created` to each recipient's `user:{id}` room.

    `self_assign=False` leaves out the sidebar row for a task card that assigns
    the task to its own sender. The app keeps it (someone who clicked Create
    wants a trace); `task_card_post` turns it off, because a plan made over MCP
    assigns every task to its maker and would add one row per task to a feed
    that already carries the single "your AI app created N tasks" notice.
    """

    from origin.services import v3_activity

    msg, parent, created = _allocate_seq_and_create_message(
        channel=channel,
        sender=sender,
        body=body,
        body_text=body_text,
        parent_id=parent_id,
        metadata=metadata,
        correlation_id=correlation_id,
        return_parent=True,
        via=via,
        also_in_channel=also_in_channel,
    )
    # Idempotent re-send (reconnect flush after a lost ack): the row
    # already existed, so DON'T re-create activities. Doing so would
    # re-fire mention / thread-reply notifications to every recipient's
    # `user:{id}` room (and could duplicate Activity rows). The caller
    # re-broadcasts `message.created`, which is harmless — clients upsert
    # by message id — but activities are not idempotent, so we return an
    # empty set on the dedup path.
    if not created:
        return msg, []
    activities = []
    mention_target_ids = _valid_mention_user_ids(channel, body or [])
    mention_acts = v3_activity.create_mention_activities(
        message=msg,
        mentioned_user_ids=mention_target_ids,
        actor=sender,
    )
    activities.extend(mention_acts)
    # Recipients the MENTION activity was aimed at for THIS message. Used to
    # suppress a duplicate THREAD_REPLY row (mention beats reply) and, below,
    # to keep them out of the plain-message activity fan-out too.
    #
    # Keyed on who was mentioned rather than on `mention_acts`, because the
    # producer drops recipients who are reading the message's surface: reading
    # the returned rows would let a mention silenced for that reason come back
    # as a thread-reply row instead, which is a relabelled notification, not a
    # suppressed one.
    mentioned_recipient_ids = {
        str(uid) for uid in mention_target_ids if uid and str(uid) != str(sender.id)
    }
    if parent is not None:
        activities.extend(
            v3_activity.create_thread_reply_activity(
                reply=msg,
                parent=parent,
                actor=sender,
                exclude_recipient_ids=mentioned_recipient_ids,
            )
        )
    # Self-assigned task: the task-card body @-mentions the assignee
    # (== sender), which the mention fan-out above skips. Surface it so a
    # self-assigned task still pings the sidebar. No-op for non-task
    # messages and for tasks assigned to someone else.
    if self_assign:
        activities.extend(v3_activity.create_self_assign_activity(message=msg, actor=sender))

    # Web Push: notify away recipients (no visible tab). Deferred to
    # on_commit so a rollback never pushes a message that didn't persist.
    # The shared helper is the single wiring point every activity producer
    # uses (mentions, thread replies, self-assign — see webpush_dispatch).
    from origin.services.webpush_dispatch import (
        schedule_push_for_activities,
        schedule_push_for_message,
    )

    schedule_push_for_activities(activities)

    # Plain-message fan-out: every TOP-LEVEL message web-pushes the other
    # channel members (presence + per-chat mute gated). Thread replies are
    # excluded (their participants are covered by the thread-reply activity
    # above), except one also sent to the chat (MIP-2486), which tells the
    # members OUTSIDE the thread. Members who already got a more-specific
    # activity (a mention) for THIS message are excluded so they aren't
    # double-notified.
    if parent is None or msg.also_in_channel:
        # Unions the mention TARGETS for the same reason `mentioned_recipient_ids`
        # is built from them: a mention silenced because the recipient is reading
        # the chat must not resurface as a plain-message notification, which is
        # also a different push category and so a different mute setting.
        already_notified = {str(a.recipient_id) for a in activities} | mentioned_recipient_ids
        if parent is not None:
            # Keyed on who is IN the thread, not on the rows made: a participant reading the
            # thread gets no thread-reply row, and must not get a plain-message one instead.
            already_notified |= v3_activity.thread_participant_ids(
                reply=msg, parent=parent, actor=sender
            )
        member_ids = (
            chat_member_rows(channel).exclude(user_id=sender.id).values_list("user_id", flat=True)
        )
        msg_recipients = [str(m) for m in member_ids if str(m) not in already_notified]
        # Activity-feed rows for the plain message so DM/GM/MDM messages show
        # in the recipient's sidebar (not just a web push). Scoped to DM/GM/MDM
        # and bot-suppressed inside the producer; PM is excluded. APPENDED AFTER
        # `schedule_push_for_activities` (above) on purpose: these rows ride the
        # `activity.created` broadcast (live sidebar update) but the push is
        # handled by `schedule_push_for_message` below — appending earlier would
        # double-push them.
        activities.extend(
            v3_activity.create_message_activities(
                message=msg, recipient_ids=msg_recipients, actor=sender
            )
        )
        schedule_push_for_message(msg, msg_recipients)
    return msg, activities


def _existing_task_card(channel, metadata) -> Message | None:
    """The live card of the task `metadata.taskId` names, in a project chat; else None."""
    if channel.kind != ChannelKind.PM:
        return None
    try:
        task_id = int((metadata or {}).get("taskId"))
    except (TypeError, ValueError):
        return None
    return (
        Message.objects.filter(
            channel=channel, task_id=task_id, is_thread_reply=False, deleted_at__isnull=True
        )
        .order_by("seq")
        .first()
    )


def _unhide_for_new_message(channel) -> None:
    """A new message brings a DM / MDM back into the sidebar of everyone who had removed it
    (MIP-1414), the sender included (they may write from another device). Their open apps are told
    to re-read the chat list. A no-op for any other kind of chat and for a chat nobody hid."""
    if channel.kind not in (ChannelKind.DM, ChannelKind.MDM):
        return
    rows = ChannelMember.objects.filter(channel=channel, hidden_at__isnull=False)
    user_ids = list(rows.values_list("user_id", flat=True))
    if not user_ids:
        return
    rows.update(hidden_at=None)
    realtime_push.push_channel_appeared(
        user_ids=user_ids, team_id=channel.team_id, channel_id=channel.id, kind=channel.kind
    )


def _allocate_seq_and_create_message(
    *,
    channel,
    sender,
    body,
    body_text,
    parent_id,
    metadata,
    correlation_id=None,
    return_parent=False,
    via=None,
    also_in_channel=False,
):
    """Atomically allocate the next per-channel seq and create the row.

    Uses `select_for_update` on the channel row to serialize concurrent
    inserts in the same channel. Throughput cap is per-channel; cross-
    channel inserts run concurrently. For chat workloads this is fine —
    contention only matters when multiple senders are typing into the
    same channel within microseconds, which is rare and bounded by
    typing speed.

    Idempotency: when `correlation_id` is supplied, a row already bearing
    that (channel, correlation_id) is returned as-is instead of inserting
    a duplicate. This makes the socket `message.send` safe to re-emit on
    a reconnect flush after a lost ack. The check runs INSIDE the channel
    lock so two sequential re-sends serialize and the second observes the
    first's row (the partial unique constraint is defense-in-depth).

    Returns `msg` normally; `(msg, parent, created)` when `return_parent`
    is set, where `created` is False on the idempotent dedup path.

    `metadata.via` (which connected app wrote the message, MIP-1570) is the server's to set: a
    `via` in `metadata` is dropped here, whoever called, so a client's `message.send` can never
    draw an official app's icon on what it typed. `via` is the one way in, for the server-side
    writer that knows the write came through a connected app (`task_card_post`).
    """
    metadata = message_via.without_via(metadata)
    if via is not None:
        metadata = {**(metadata or {}), message_via.KEY: via}
    with transaction.atomic():
        # Lock the channel row so two concurrent senders can't race to
        # allocate the same seq. `select_for_update()` blocks until the
        # other transaction commits.
        Channel.objects.select_for_update().filter(pk=channel.pk).first()

        # Idempotency short-circuit (inside the lock): a re-emitted send
        # carrying a correlation_id we've already stored returns the
        # existing row rather than creating a duplicate.
        if correlation_id:
            existing = Message.objects.filter(
                channel=channel, correlation_id=correlation_id
            ).first()
            if existing is not None:
                if return_parent:
                    return existing, None, False
                return existing

        # One card per task in a project chat (MIP-2483). The server posts it when the task is
        # created; a browser tab that still sends its own gets that card back, not a second one.
        if parent_id is None and (card := _existing_task_card(channel, metadata)) is not None:
            if return_parent:
                return card, None, False
            return card

        # Compute next seq via Max() over the locked channel.
        last_seq = Message.objects.filter(channel=channel).aggregate(m=Max("seq"))["m"] or 0
        next_seq = last_seq + 1

        parent = None
        thread_root_id = None
        is_thread_reply = False
        if parent_id is not None:
            # An id that is not a UUID names no message here either, so it gets
            # the same 404; it used to be a ValidationError 500 (MIP-1014).
            parent_uuid = as_uuid(parent_id)
            if parent_uuid is not None:
                parent = Message.objects.filter(id=parent_uuid, channel=channel).first()
            if parent is None:
                raise Http404("parent_id not found in this channel.")
            thread_root_id = _resolve_thread_root(parent)
            is_thread_reply = True

        # Link the task FK on task-card messages. The task-create flow
        # posts the card with the task id in `metadata.taskId` (after
        # creating the task). Historically only the metadata was stored
        # and `Message.task` stayed null, which forced a metadata
        # fallback in `MessageSerializer.taskId` and — worse — broke the
        # task-comment thread mirror (`write_task_comment_as_thread_reply`
        # locates the header by `task_id`). Resolve + set it here; leave
        # it null if the id is missing / non-numeric / unknown so a stale
        # client id can never 500 a send.
        task_obj = None
        meta_task_id = (metadata or {}).get("taskId")
        if meta_task_id is not None:
            from origin.models.task.task_models import TaskMaster

            try:
                task_obj = (
                    TaskMaster.objects.filter(task_id=int(meta_task_id)).only("task_id").first()
                )
            except (TypeError, ValueError):
                task_obj = None

        msg = Message.objects.create(
            channel=channel,
            sender=sender,
            seq=next_seq,
            body=body,
            body_text=body_text,
            parent=parent,
            thread_root_id=thread_root_id,
            is_thread_reply=is_thread_reply,
            also_in_channel=also_in_channel and is_thread_reply,
            task=task_obj,
            metadata=metadata or {},
            correlation_id=correlation_id,
        )

        _unhide_for_new_message(channel)

        # Outbound webhook for `message.created`.
        #
        # This is the ONLY emit point for chat, and it is the endpoint
        # genos-sockets proxies every send through
        # (`socketio_events_v3/message_handlers.py` →
        # `POST api/v3/channels/{id}/messages/`), so one user action is
        # one event. Deliberately NOT hooked on `Message.objects.create`
        # generally: `unified_writer` also writes Message rows to mirror
        # a task comment into its PM thread, and an integrator should get
        # `task.comment_created` for that, not a second `message.created`
        # betraying an internal mirroring detail. `demo_seeder` writes
        # Message rows too, and nobody wants a webhook storm on seed.
        #
        # DM channels never reach the outbox — `schedule_message_event`
        # refuses them before a payload is built.
        webhook_enqueue.schedule_message_event(msg, channel)

        # Bump `reply_count` on the parent if this is a thread reply.
        # Denormalized to avoid an aggregate query when the chat list
        # renders reply-count chips. The channel-level select_for_update
        # above serializes concurrent inserts in this channel, so the
        # parent's counter doesn't need its own lock.
        if parent is not None:
            parent.reply_count += 1
            parent.save(update_fields=["reply_count", "ts_updated_at"])

        # Extract direct user mentions from the body and persist them
        # as `MessageMention` rows so the FE's `@you` indicator + the
        # inbox "all mentions of me" feed can find them. We intentionally
        # do this AFTER the message create (and inside the same atomic
        # block) — the mention rows are FK'd to message.id, so the
        # message row must exist first; if the bulk_create somehow
        # fails, the whole transaction rolls back including the
        # message, which is the right invariant.
        #
        # Group mentions (`mentionGroup` nodes) are NOT expanded here.
        # That requires resolving group_id → user_ids via
        # `MentionGroupMembers` and a schema fix for the
        # `MessageMention.via_group_id` UUIDField vs the BigAutoField
        # group PK — punted to a follow-up. The FE renders the group
        # chip from the body itself, so the visible UX still works;
        # only the "mentioned via group" inbox routing is deferred.
        mentioned_user_ids = _valid_mention_user_ids(channel, body)
        if mentioned_user_ids:
            # `ignore_conflicts=True` makes a duplicate (e.g. the same
            # user mentioned twice in one body) collapse to one row via
            # the `uniq_message_mention` unique constraint rather than
            # raising IntegrityError. It does NOT suppress FK violations,
            # which is why the ids are pre-filtered to real channel
            # members by `_valid_mention_user_ids` above.
            MessageMention.objects.bulk_create(
                [MessageMention(message=msg, mentioned_user_id=uid) for uid in mentioned_user_ids],
                ignore_conflicts=True,
            )

        if return_parent:
            return msg, parent, True
        return msg


def _prefetched_messages(qs):
    """Apply the prefetch set every messages serializer needs.

    Centralized so the delta endpoint and the single-message endpoint
    produce identical wire shapes — the legacy code's serialize divergence
    between `*SingleMessageView` and `*MessagesDeltaView` is exactly the
    class of bug this rewrite eliminates.
    """
    qs = qs.select_related(
        "sender",
        "channel",
        # PM-only `task` FK + nested `task.project` for `displayId`
        # ("<code>-<n>"). `select_related` is a no-op on rows where
        # `task_id` is null (DM/GM/MDM) and avoids N+1 on PM channels
        # where every message references the same TaskMaster.
        "task",
        "task__project",
    ).prefetch_related(
        "reactions__user",
        "mentions",
        "attachments",
    )
    # PM bubble "N comments" chip: the authoritative live comment count,
    # so it matches the thread's Comments tab instead of `reply_count`
    # (which counts all thread replies). See `MessageSerializer`.
    return MessageSerializer.annotate_task_comment_count(qs)


def _deletes_since(channel, since, *, is_thread_reply) -> list[str]:
    # A load with no checkpoint (first, or too old) holds nothing to drop: the client evicts.
    if since is None:
        return []
    return moved_messages.deleted_ids_since(channel, since, is_thread_reply=is_thread_reply)


class MessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/messages/?since=ISO

    Incremental sync of top-level messages (NOT thread replies — those
    have their own endpoint). The contract:

    - First load (no `since`): the newest `MAX_MESSAGES_PER_DELTA` (500)
      non-deleted top-level messages, not the whole channel. There is no
      cursor for older ones.
    - Incremental (`since=<ts>`): rows updated since `since`, INCLUDING
      soft-deleted rows (so the client can apply tombstones). Plus any
      messages whose reactions changed (so the reaction chip refreshes
      without the client re-fetching the whole row).
    - Catastrophic delta (`since` older than 60 days): respond as a full
      load AND set `force_full_reload=true` so the client clears its
      IDB store before applying.
    - Row-count cap (`MAX_MESSAGES_PER_DELTA`): if the row count of any
      of the above branches (the first load included) exceeds the cap,
      slice to the most recent N and set `force_full_reload=true`. Bounds
      wire / parse cost for long channels, and for very active ones even
      when the client's checkpoint is fresh.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        server_time = capture_server_time()
        since, force_full = check_since(request)

        # Top-level messages, plus the replies also sent to the chat (MIP-2486).
        qs = Message.objects.filter(main_timeline_q(), channel=channel)
        qs = _apply_message_since_filter(qs, since)

        # Indirect change: messages whose reactions changed since checkpoint
        # need to be re-served so the chip count refreshes. Compute the
        # set of message ids with recent reaction activity and union them
        # in.
        if since is not None and not force_full:
            recent_reaction_msg_ids = set(
                MessageReaction.objects.filter(
                    main_timeline_q("message__"),
                    message__channel=channel,
                    ts_updated_at__gt=since,
                ).values_list("message_id", flat=True)
            )
            if recent_reaction_msg_ids:
                already_included = set(qs.values_list("id", flat=True))
                missing_ids = recent_reaction_msg_ids - already_included
                if missing_ids:
                    extra = Message.objects.filter(
                        main_timeline_q(),
                        channel=channel,
                        id__in=missing_ids,
                    )
                    qs = qs | extra

        # Tier retention: hide history beyond the viewer's window.
        # After the reaction-union (a reaction can't resurrect an
        # out-of-window message), before the row-count cap.
        qs, retention = _apply_retention(qs, channel, request.user, is_thread_reply=False)

        qs = qs.distinct().order_by("ts_sent_at")

        # Cap the row count so a single delta response is bounded in
        # size. If the cap trips, `force_full` flips so the client
        # evicts its store before applying the slice — same semantic as
        # the 60-day catastrophic-delta path.
        qs, count_cap_tripped = apply_row_count_cap(qs)
        force_full = force_full or count_cap_tripped

        qs = _prefetched_messages(qs)
        messages_data = MessageSerializer(qs, many=True).data
        # `deletes` is a separate array per the envelope spec, but since
        # `apply_since_filter` already returns soft-deleted rows in the
        # `messages` array (with deletedAt set), the client can read
        # tombstones directly. `deletes` names what is no longer in this
        # chat at all: rows that moved to another one (MIP-2505).
        envelope_data = {
            "messages": messages_data,
            "deletes": _deletes_since(channel, since, is_thread_reply=False),
        }
        return Response(
            build_delta_response(envelope_data, server_time, force_full, retention=retention)
        )

    def post(self, request, channel_id):
        """POST /api/v3/channels/{channel_id}/messages/

        Send a message (top-level OR thread reply — `parent_id` decides).

        Request body:
            {
              "body": [...],             # JSON block array (required)
              "body_text": "<str>",      # first-line preview (optional, derived if missing)
              "parent_id": "<uuid>",     # thread reply target (optional)
              "also_in_channel": true,   # with parent_id: also show it in the main chat
              "metadata": {...}          # PM: taskId/displayId/etc. (optional)
            }

        Server allocates `id` (UUID) and `seq` (monotonic per channel).
        Returns the full serialized Message.
        """
        channel = _verify_member_or_404(channel_id, request.user)
        # An archived group chat or project chat takes no messages until restored (MIP-1693).
        if res := archived_channel_response(channel):
            return res
        body = body_object(request)
        msg_body = body.get("body")
        if msg_body is None or not isinstance(msg_body, list):
            return Response(
                {"error": "body must be a non-empty list of blocks."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        msg_body_text = body.get("body_text") or ""
        parent_id = body.get("parent_id") or None
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            return Response(
                {"error": "metadata must be a JSON object."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # Optional idempotency key forwarded by the Flask `/v3` proxy from
        # the client's socket `correlation_id`. Lets a reconnect re-send
        # collapse to the existing row instead of duplicating.
        correlation_id = body.get("correlation_id") or None
        # "Also send to <chat>" (MIP-2486). Only `true` counts. Refused before anything is
        # written, so the client's composer gets its text back.
        also_in_channel = body.get("also_in_channel") is True
        if also_in_channel and not parent_id:
            return Response(
                {"error": "also_in_channel requires parent_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if also_in_channel and channel.kind == ChannelKind.PM:
            return Response(
                {"error": "A project chat has no 'also send to chat'."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        msg, activities = _allocate_seq_and_create_message_with_activities(
            channel=channel,
            sender=request.user,
            body=msg_body,
            body_text=msg_body_text,
            parent_id=parent_id,
            metadata=metadata,
            correlation_id=correlation_id,
            also_in_channel=also_in_channel,
        )

        # Refresh with prefetches so the response matches what the
        # delta endpoint and detail endpoint return.
        msg = _prefetched_messages(Message.objects.filter(pk=msg.pk)).first()
        from origin.serializers.chat.unified_serializers import ActivitySerializer

        response_data = MessageSerializer(msg).data
        # `_v3_activities` is an internal channel between Django and the
        # Flask socketio proxy in `socketio_events_v3/message_handlers.py`.
        # The WS handler reads this, emits `activity.created` to each
        # recipient's `user:{id}` room, then strips the key before the
        # broadcast payload is sent to the channel room. FE callers of
        # the REST endpoint directly (e.g. tests) get the key too —
        # ignoring it is fine since it's purely additive.
        # Every row embeds the message just posted. Hand them the one prefetched instance: left to
        # themselves each row lazy-loads its reactions, mentions and attachments, three queries per
        # recipient (MIP-2387).
        for act in activities:
            if act.message_id == msg.id:
                act.message = msg
        response_data["_v3_activities"] = ActivitySerializer(activities, many=True).data
        # Mentions of people outside the channel are dropped above and
        # never reach anyone — report them so the sender can offer to
        # add the person instead of the mention silently going nowhere.
        response_data["nonMemberMentions"] = non_member_mentions(
            _split_mentions_by_membership(channel, msg_body)[1],
            channel_id=channel.id,
            exclude_user_ids=[request.user.id],
        )
        return Response(response_data, status=status.HTTP_201_CREATED)


class OlderMessagesView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/messages/older/?before={message_id}

    The page of main-timeline messages (`main_timeline_q`) just before one the client already holds (MIP-2487: a jump to
    a pinned message older than the first load). At most `MAX_MESSAGES_PER_DELTA` rows, oldest
    first, deleted ones left out, the viewer's history window applied. `hasMore` says whether
    older ones remain. `before` must be a message of this chat.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        before_id = as_uuid(request.GET.get("before"))
        if before_id is None:
            return Response(
                {"error": "before must be a message id."}, status=status.HTTP_400_BAD_REQUEST
            )
        before = Message.objects.filter(id=before_id, channel=channel).first()
        if before is None:
            raise Http404("Message not found.")
        qs = Message.objects.filter(
            Q(ts_sent_at__lt=before.ts_sent_at)
            | Q(ts_sent_at=before.ts_sent_at, id__lt=before.id),
            main_timeline_q(),
            channel=channel,
            deleted_at__isnull=True,
        )
        _days, cutoff = _retention_cutoff(request.user)
        if cutoff is not None:
            qs = qs.filter(ts_sent_at__gte=cutoff)
        newest_first = qs.order_by("-ts_sent_at", "-id")
        ids = list(newest_first.values_list("id", flat=True)[: MAX_MESSAGES_PER_DELTA + 1])
        page = _prefetched_messages(
            Message.objects.filter(id__in=ids[:MAX_MESSAGES_PER_DELTA])
        ).order_by("ts_sent_at", "id")
        return Response(
            {
                "messages": MessageSerializer(page, many=True).data,
                "hasMore": len(ids) > MAX_MESSAGES_PER_DELTA,
            }
        )


class ThreadMessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/threads/?since=ISO

    Same shape as `MessagesDeltaView` but for `is_thread_reply=True`
    messages. Split so a client that hasn't opened any thread doesn't
    pay the cost of streaming thread replies on every sync.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        server_time = capture_server_time()
        since, force_full = check_since(request)

        qs = Message.objects.filter(channel=channel, is_thread_reply=True)
        qs = _apply_message_since_filter(qs, since)

        if since is not None and not force_full:
            recent_reaction_msg_ids = set(
                MessageReaction.objects.filter(
                    message__channel=channel,
                    message__is_thread_reply=True,
                    ts_updated_at__gt=since,
                ).values_list("message_id", flat=True)
            )
            if recent_reaction_msg_ids:
                already_included = set(qs.values_list("id", flat=True))
                missing_ids = recent_reaction_msg_ids - already_included
                if missing_ids:
                    extra = Message.objects.filter(
                        channel=channel,
                        is_thread_reply=True,
                        id__in=missing_ids,
                    )
                    qs = qs | extra

        # Tier retention — same placement as the top-level delta view.
        qs, retention = _apply_retention(qs, channel, request.user, is_thread_reply=True)

        qs = qs.distinct().order_by("ts_sent_at")

        # Cap row count so an over-active thread can't blow up the wire
        # response. Flag composes (OR) with the 60-day time cap.
        qs, count_cap_tripped = apply_row_count_cap(qs)
        force_full = force_full or count_cap_tripped

        qs = _prefetched_messages(qs)
        return Response(
            build_delta_response(
                {
                    "messages": MessageSerializer(qs, many=True).data,
                    "deletes": _deletes_since(channel, since, is_thread_reply=True),
                },
                server_time,
                force_full,
                retention=retention,
            )
        )


class MessageDetailView(AuthenticatedAPIView):
    """GET    /api/v3/messages/{message_id}/        fetch by id (deep links / tests).
    PATCH  /api/v3/messages/{message_id}/        edit body.
    DELETE /api/v3/messages/{message_id}/        soft-delete.
    """

    def _fetch_for_user(self, message_id, user, *, with_prefetch=True):
        """Get a Message scoped to the user's channel membership.

        404-not-403 for non-members (no existence leak). Returns the
        Message; raises Http404 otherwise.
        """
        qs = Message.objects.select_related("channel", "sender")
        if with_prefetch:
            qs = qs.prefetch_related("reactions__user", "mentions", "attachments")
        try:
            message = qs.get(id=message_id)
        except Message.DoesNotExist:
            raise Http404("Message not found.")

        if not is_chat_member(message.channel_id, user.id):
            raise Http404("Message not found.")
        return message

    def get(self, request, message_id):
        message = self._fetch_for_user(message_id, request.user)
        # Tier retention: an out-of-window message is hidden — 404, the
        # same existence-hiding rule used for non-members, so deep
        # links and the delta views agree on what the viewer can see.
        _days, cutoff = _retention_cutoff(request.user)
        if cutoff is not None and message.ts_sent_at < cutoff:
            raise Http404("Message not found.")
        return Response(MessageSerializer(message).data)

    def patch(self, request, message_id):
        """Edit the body of a message.

        Only the original sender can edit. Request body:
            {"body": [...], "body_text": "<str>"}
        Other fields are ignored. Sets `edited_at` to now.
        """
        message = self._fetch_for_user(message_id, request.user, with_prefetch=False)
        if res := archived_channel_response(message.channel):
            return res
        if message.sender_id != request.user.id:
            return Response(
                {"error": "Only the sender can edit a message."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if message.deleted_at is not None:
            return Response(
                {"error": "Cannot edit a deleted message."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        data = body_object(request)
        body_json = data.get("body")
        if body_json is None or not isinstance(body_json, list):
            return Response(
                {"error": "body must be a non-empty list of blocks."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        body_text = data.get("body_text") or ""

        # Body change + mention re-sync must be atomic — otherwise a
        # crash between the body save and the mention delete leaves the
        # `mentions[]` array on the serialized message disagreeing with
        # the rendered chips. The transaction wraps both writes.
        with transaction.atomic():
            old_body, old_body_text = message.body, message.body_text
            message.body = body_json
            message.body_text = body_text
            message.edited_at = timezone.now()
            update_fields = ["body", "body_text", "edited_at", "ts_updated_at"]
            # A person's edit: the text is theirs now, so a connected app's stamp goes (MIP-1570).
            cleaned = message_via.without_via(message.metadata)
            if cleaned is not message.metadata:
                message.metadata = cleaned
                update_fields.append("metadata")
            message.save(update_fields=update_fields)
            # The old text must not outlive the edit in the thread's cached digest (MIP-1819).
            thread_summary_cleanup.forget_edited_message(
                message, old_body=old_body, old_body_text=old_body_text
            )

            # Re-sync MessageMention rows from the new body. The simple
            # wipe-and-recreate approach is correct (set semantics) and
            # cheap — typical messages have <10 mentions. We could diff
            # old vs new and only touch the changes, but the row count
            # makes the optimization not worth the complexity.
            #
            # Group mentions (mentionGroup nodes) are NOT expanded —
            # see the same comment block on `_allocate_seq_and_create_message`
            # for the schema-mismatch reason.
            new_mentioned_ids = _valid_mention_user_ids(message.channel, body_json)
            MessageMention.objects.filter(message=message).delete()
            if new_mentioned_ids:
                MessageMention.objects.bulk_create(
                    [
                        MessageMention(message=message, mentioned_user_id=uid)
                        for uid in new_mentioned_ids
                    ],
                    ignore_conflicts=True,
                )

        # Re-fetch with prefetches for the response.
        message = _prefetched_messages(Message.objects.filter(pk=message.pk)).first()
        return Response(MessageSerializer(message).data)

    def delete(self, request, message_id):
        """Soft-delete a message.

        Authorization: sender always, OR the channel owner. Sets
        `deleted_at` to now and clears the text in the same save
        (`message_tombstone`, MIP-1784): the row stays as a tombstone so
        the thread, `seq` and the client's sync hold, but it says nothing.
        There is no server-side undo (the app asks first, and its Undo
        for task comments is client-side), so nothing needs the text.

        Decrements the parent's reply_count if this is a thread reply.
        """
        message = self._fetch_for_user(message_id, request.user, with_prefetch=False)
        if res := archived_channel_response(message.channel):
            return res
        is_sender = message.sender_id == request.user.id
        # A group with no live owner is moderated by any host-side member (MIP-1550).
        is_channel_owner = acts_as_owner(message.channel, request.user.id)
        if not (is_sender or is_channel_owner):
            return Response(
                {"error": "Only the sender or channel owner can delete."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if message.deleted_at is not None:
            # Idempotent — already gone. A row an older delete left with its text is cleaned
            # now (the sender, or the owner, asking again is enough).
            if message_tombstone.has_text(message):
                with transaction.atomic():
                    message_tombstone.tombstone_message(message)
            return Response(status=status.HTTP_204_NO_CONTENT)

        with transaction.atomic():
            message_tombstone.tombstone_message(message)
            if message.parent_id is not None:
                # Decrement the parent's reply_count via an F-expression so
                # the read-modify-write happens atomically in SQL. The
                # `gt=0` filter floors at 0 so a double-delete (e.g. via
                # two clients) can't underflow.
                Message.objects.filter(pk=message.parent_id, reply_count__gt=0).update(
                    reply_count=F("reply_count") - 1
                )
        return Response(status=status.HTTP_204_NO_CONTENT)


def rewrite_card_message(
    message, *, body, body_text, metadata=None, server_built=False, sync_mentions=True
):
    """Replace a task card's body (and metadata, when given) and re-sync its mentions.

    `metadata.via` (a connected app's stamp, MIP-1570) is not the caller's to set: unless the
    server built `metadata` itself from the stored row (`server_built`, the refresh after an
    edit that had no browser), a `via` in it is dropped, and so is one already on the card when
    `metadata` is not given: the body is a person's now.

    The one definition of "rewrite a card": `task_card_post.refresh_*_card` (after every write,
    MIP-1460 and MIP-2483) and the `repair_task_cards` backfill both go through it, so the two
    cannot drift. `sync_mentions=False` leaves the card's `@` rows as they are: the backfill of
    old cards must not put them in anyone's list of mentions today.
    """
    if not server_built:
        given = metadata if isinstance(metadata, dict) else message.metadata
        cleaned = message_via.without_via(given)
        if isinstance(metadata, dict) or cleaned is not given:
            metadata = cleaned
    _write_card_messages([(message, body, body_text, metadata)], sync_mentions=sync_mentions)


def rewrite_card_messages(rewrites) -> None:
    """`rewrite_card_message(..., server_built=True)` for many cards at once, in a fixed number of
    queries (MIP-2561). `rewrites` is a list of `(message, body, body_text, metadata)`, each
    message loaded with its `channel`, each `metadata` built by the server from the stored row."""
    _write_card_messages(list(rewrites), sync_mentions=True)


def _write_card_messages(rewrites, *, sync_mentions) -> None:
    if not rewrites:
        return
    with transaction.atomic():
        now = timezone.now()
        edited: dict = {}  # channel id -> threads whose text changed
        messages = []
        update_fields = {"body", "body_text", "edited_at", "ts_updated_at"}
        for message, body, body_text, metadata in rewrites:
            if message.body != body or message.body_text != body_text:
                edited.setdefault(message.channel_id, []).append(
                    message.thread_root_id or message.pk
                )
            message.body = body
            message.body_text = body_text
            message.edited_at = now
            # `bulk_update` skips `auto_now`.
            message.ts_updated_at = now
            if isinstance(metadata, dict):
                message.metadata = metadata
                update_fields.add("metadata")
            messages.append(message)
        # A row given no metadata is written back with its own, which is what it holds.
        Message.objects.bulk_update(messages, sorted(update_fields))
        for channel_id, thread_ids in edited.items():
            thread_summary_cleanup.forget_threads(channel_id=channel_id, thread_ids=thread_ids)

        # Re-sync MessageMention rows from the new card body (the card
        # @-mentions the assignee + reporter). Mirrors
        # `MessageDetailView.patch` so the `mentions[]` array stays
        # consistent after an assignee change. No Activity rows are
        # created here — task-body mention notifications are owned
        # separately by the task PUT / `task_body_mention` path, and a
        # reassignment's by `task_assignment_notice` (MIP-1461).
        if not sync_mentions:
            return
        mentioned = [
            (message, _mentioned_user_ids(message.channel, message.body)) for message in messages
        ]
        wanted: dict = {}  # channel id -> (channel, everyone its cards mention)
        for message, user_ids in mentioned:
            wanted.setdefault(message.channel_id, (message.channel, set()))[1].update(user_ids)
        # Only people in the chat get a row, as in `_valid_mention_user_ids`.
        members = {
            channel_id: {
                str(uid)
                for uid in chat_member_rows(channel)
                .filter(user_id__in=user_ids)
                .values_list("user_id", flat=True)
            }
            for channel_id, (channel, user_ids) in wanted.items()
            if user_ids
        }
        MessageMention.objects.filter(message__in=messages).delete()
        rows = [
            MessageMention(message=message, mentioned_user_id=uid)
            for message, user_ids in mentioned
            for uid in {str(u) for u in user_ids} & members.get(message.channel_id, set())
        ]
        if rows:
            MessageMention.objects.bulk_create(rows, ignore_conflicts=True)


# Historical flat cap for chat attachments — now the FALLBACK while
# the per-tier `upload_max_mb` ships dark (None). Above the resolved
# limit, the server returns 413. Keep in sync with the frontend
# client-side guard in `useAttachmentDraft` so the user gets a fast
# error instead of a slow multipart upload that fails at the end.
MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024  # 25 MiB


class MessageAttachmentsView(AuthenticatedAPIView):
    """POST /api/v3/messages/{message_id}/attachments/

    Upload one file as a `MessageAttachment` attached to an existing
    message. The uploader is recorded on the row; the file lives in
    whichever storage backend the `FileField` is wired to (local disk
    in dev, S3 in prod).

    Authorization: only the message's sender can attach files to it.
    This matches the editing rule (`MessageDetailView.patch`) — adding
    an attachment after the fact is treated as part of the same author
    intent. Channel owners are NOT granted attach rights because that
    would let them inject content into someone else's message.

    Bumps the parent message's `ts_updated_at` after the attachment
    create so the next `?since=` delta sync surfaces the new attachment
    to other clients without a separate broadcast — the existing socket
    `message.send` path is the only push channel we keep authoritative
    on the v3 surface; this REST endpoint is intentionally a polling
    fallback.

    Multi-file uploads: not supported here. Clients are expected to call
    this endpoint once per file. The per-file boundary makes the
    progress UI simpler (one row → one indicator) and lets a partial
    failure leave the other files attached.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request, message_id):
        try:
            message = Message.objects.select_related("channel").get(id=message_id)
        except Message.DoesNotExist:
            raise Http404("Message not found.")

        # 404-not-403 for non-members to match the existence-hiding
        # rule used everywhere else.
        if not is_chat_member(message.channel_id, request.user.id):
            raise Http404("Message not found.")
        if res := view_only_channel_response(message.channel, request.user.id):
            return res

        if message.sender_id != request.user.id:
            return Response(
                {"error": "Only the sender can attach files to a message."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if message.deleted_at is not None:
            return Response(
                {"error": "Cannot attach to a deleted message."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        file = request.FILES.get("file")
        if file is None:
            return Response(
                {"error": "Missing multipart field 'file'."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # The client's `mime` field is ignored: clients render `image/*`
        # inline, so the type is derived from the bytes and the extension.
        upload, res = check_attachment_upload(
            request.user, file, fallback_bytes=MAX_ATTACHMENT_BYTES
        )
        if res:
            return res

        with transaction.atomic():
            attachment = MessageAttachment.objects.create(
                message=message,
                uploader=request.user,
                file=upload.file,
                original_name=upload.display_name,
                mime=upload.mime,
                size_bytes=file.size,
            )
            # Touch the parent message so delta sync picks up the new
            # attachment on the next `?since=` poll. `ts_updated_at`
            # is auto_now=True so we just need to ensure it's in the
            # `update_fields` of the save.
            message.save(update_fields=["ts_updated_at"])

        return Response(
            MessageAttachmentSerializer(attachment, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )
