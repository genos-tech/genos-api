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
messages come back in `deletes` (the array of UUIDs that disappeared since
the checkpoint) so the client can apply tombstones without parsing every
message row.
"""

from django.db import transaction
from django.db.models import F, Max
from django.http import Http404
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response

from rest_framework.parsers import FormParser, MultiPartParser

from origin.models.chat.unified_models import (
    Channel,
    ChannelMember,
    Message,
    MessageAttachment,
    MessageMention,
    MessageReaction,
)
from origin.serializers.chat.unified_serializers import (
    MessageAttachmentSerializer,
    MessageReactionSerializer,
    MessageSerializer,
)
from origin.services.mention_extractor import extract_mentioned_user_ids
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.incremental import (
    apply_row_count_cap,
    build_delta_response,
    capture_server_time,
    check_since,
)


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
    is_member = ChannelMember.objects.filter(channel=channel, user=user, is_deleted=False).exists()
    if not is_member:
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


def _allocate_seq_and_create_message_with_activities(
    *, channel, sender, body, body_text, parent_id, metadata
):
    """Wraps `_allocate_seq_and_create_message` and additionally fans out
    `Activity` rows for the recipients who should see a sidebar entry.

    Returns `(message, activities)` so the caller can include the
    activities in the wire response — the WS proxy layer reads them and
    broadcasts `activity.created` to each recipient's `user:{id}` room.
    """

    from origin.services import v3_activity

    msg, parent = _allocate_seq_and_create_message(
        channel=channel,
        sender=sender,
        body=body,
        body_text=body_text,
        parent_id=parent_id,
        metadata=metadata,
        return_parent=True,
    )
    activities = []
    activities.extend(
        v3_activity.create_mention_activities(
            message=msg,
            mentioned_user_ids=extract_mentioned_user_ids(body or []),
            actor=sender,
        )
    )
    if parent is not None:
        activities.extend(
            v3_activity.create_thread_reply_activity(reply=msg, parent=parent, actor=sender)
        )
    return msg, activities


def _allocate_seq_and_create_message(
    *, channel, sender, body, body_text, parent_id, metadata, return_parent=False
):
    """Atomically allocate the next per-channel seq and create the row.

    Uses `select_for_update` on the channel row to serialize concurrent
    inserts in the same channel. Throughput cap is per-channel; cross-
    channel inserts run concurrently. For chat workloads this is fine —
    contention only matters when multiple senders are typing into the
    same channel within microseconds, which is rare and bounded by
    typing speed.
    """
    with transaction.atomic():
        # Lock the channel row so two concurrent senders can't race to
        # allocate the same seq. `select_for_update()` blocks until the
        # other transaction commits.
        Channel.objects.select_for_update().filter(pk=channel.pk).first()

        # Compute next seq via Max() over the locked channel.
        last_seq = Message.objects.filter(channel=channel).aggregate(m=Max("seq"))["m"] or 0
        next_seq = last_seq + 1

        parent = None
        thread_root_id = None
        is_thread_reply = False
        if parent_id is not None:
            try:
                parent = Message.objects.get(id=parent_id, channel=channel)
            except Message.DoesNotExist:
                raise Http404("parent_id not found in this channel.")
            thread_root_id = _resolve_thread_root(parent)
            is_thread_reply = True

        msg = Message.objects.create(
            channel=channel,
            sender=sender,
            seq=next_seq,
            body=body,
            body_text=body_text,
            parent=parent,
            thread_root_id=thread_root_id,
            is_thread_reply=is_thread_reply,
            metadata=metadata or {},
        )

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
        mentioned_user_ids = extract_mentioned_user_ids(body)
        if mentioned_user_ids:
            # `ignore_conflicts=True` makes a duplicate (e.g. the same
            # user mentioned twice in one body) collapse to one row via
            # the `uniq_message_mention` unique constraint rather than
            # raising IntegrityError. Cheap correctness for free.
            MessageMention.objects.bulk_create(
                [MessageMention(message=msg, mentioned_user_id=uid) for uid in mentioned_user_ids],
                ignore_conflicts=True,
            )

        if return_parent:
            return msg, parent
        return msg


def _prefetched_messages(qs):
    """Apply the prefetch set every messages serializer needs.

    Centralized so the delta endpoint and the single-message endpoint
    produce identical wire shapes — the legacy code's serialize divergence
    between `*SingleMessageView` and `*MessagesDeltaView` is exactly the
    class of bug this rewrite eliminates.
    """
    return qs.select_related(
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


class MessagesDeltaView(AuthenticatedAPIView):
    """GET /api/v3/channels/{channel_id}/messages/?since=ISO

    Incremental sync of top-level messages (NOT thread replies — those
    have their own endpoint). The contract:

    - First load (no `since`): all non-deleted top-level messages.
    - Incremental (`since=<ts>`): rows updated since `since`, INCLUDING
      soft-deleted rows (so the client can apply tombstones). Plus any
      messages whose reactions changed (so the reaction chip refreshes
      without the client re-fetching the whole row).
    - Catastrophic delta (`since` older than 60 days): respond as a full
      load AND set `force_full_reload=true` so the client clears its
      IDB store before applying.
    - Row-count cap (`MAX_MESSAGES_PER_DELTA`): if the row count of any
      of the above branches exceeds the cap, slice to the most recent N
      and set `force_full_reload=true`. Bounds wire / parse cost for
      very active channels even when the client's checkpoint is fresh.
    """

    def get(self, request, channel_id):
        channel = _verify_member_or_404(channel_id, request.user)
        server_time = capture_server_time()
        since, force_full = check_since(request)

        qs = Message.objects.filter(channel=channel, is_thread_reply=False)
        qs = _apply_message_since_filter(qs, since)

        # Indirect change: messages whose reactions changed since checkpoint
        # need to be re-served so the chip count refreshes. Compute the
        # set of message ids with recent reaction activity and union them
        # in.
        if since is not None and not force_full:
            recent_reaction_msg_ids = set(
                MessageReaction.objects.filter(
                    message__channel=channel,
                    message__is_thread_reply=False,
                    ts_updated_at__gt=since,
                ).values_list("message_id", flat=True)
            )
            if recent_reaction_msg_ids:
                already_included = set(qs.values_list("id", flat=True))
                missing_ids = recent_reaction_msg_ids - already_included
                if missing_ids:
                    extra = Message.objects.filter(
                        channel=channel,
                        is_thread_reply=False,
                        id__in=missing_ids,
                    )
                    qs = qs | extra

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
        # tombstones directly. We keep `deletes` for hard-deletes (e.g.
        # post-purge) — currently always empty.
        envelope_data = {
            "messages": messages_data,
            "deletes": [],
        }
        return Response(build_delta_response(envelope_data, server_time, force_full))

    def post(self, request, channel_id):
        """POST /api/v3/channels/{channel_id}/messages/

        Send a message (top-level OR thread reply — `parent_id` decides).

        Request body:
            {
              "body": [...],             # JSON block array (required)
              "body_text": "<str>",      # first-line preview (optional, derived if missing)
              "parent_id": "<uuid>",     # thread reply target (optional)
              "metadata": {...}          # PM: taskId/displayId/etc. (optional)
            }

        Server allocates `id` (UUID) and `seq` (monotonic per channel).
        Returns the full serialized Message.
        """
        channel = _verify_member_or_404(channel_id, request.user)
        body = request.data or {}
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

        msg, activities = _allocate_seq_and_create_message_with_activities(
            channel=channel,
            sender=request.user,
            body=msg_body,
            body_text=msg_body_text,
            parent_id=parent_id,
            metadata=metadata,
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
        response_data["_v3_activities"] = ActivitySerializer(activities, many=True).data
        return Response(response_data, status=status.HTTP_201_CREATED)


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

        qs = qs.distinct().order_by("ts_sent_at")

        # Cap row count so an over-active thread can't blow up the wire
        # response. Flag composes (OR) with the 60-day time cap.
        qs, count_cap_tripped = apply_row_count_cap(qs)
        force_full = force_full or count_cap_tripped

        qs = _prefetched_messages(qs)
        return Response(
            build_delta_response(
                {"messages": MessageSerializer(qs, many=True).data, "deletes": []},
                server_time,
                force_full,
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

        is_member = ChannelMember.objects.filter(
            channel=message.channel, user=user, is_deleted=False
        ).exists()
        if not is_member:
            raise Http404("Message not found.")
        return message

    def get(self, request, message_id):
        message = self._fetch_for_user(message_id, request.user)
        return Response(MessageSerializer(message).data)

    def patch(self, request, message_id):
        """Edit the body of a message.

        Only the original sender can edit. Request body:
            {"body": [...], "body_text": "<str>"}
        Other fields are ignored. Sets `edited_at` to now.
        """
        message = self._fetch_for_user(message_id, request.user, with_prefetch=False)
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

        body_json = request.data.get("body") if request.data else None
        if body_json is None or not isinstance(body_json, list):
            return Response(
                {"error": "body must be a non-empty list of blocks."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        body_text = (request.data or {}).get("body_text") or ""

        # Body change + mention re-sync must be atomic — otherwise a
        # crash between the body save and the mention delete leaves the
        # `mentions[]` array on the serialized message disagreeing with
        # the rendered chips. The transaction wraps both writes.
        with transaction.atomic():
            message.body = body_json
            message.body_text = body_text
            message.edited_at = timezone.now()
            message.save(update_fields=["body", "body_text", "edited_at", "ts_updated_at"])

            # Re-sync MessageMention rows from the new body. The simple
            # wipe-and-recreate approach is correct (set semantics) and
            # cheap — typical messages have <10 mentions. We could diff
            # old vs new and only touch the changes, but the row count
            # makes the optimization not worth the complexity.
            #
            # Group mentions (mentionGroup nodes) are NOT expanded —
            # see the same comment block on `_allocate_seq_and_create_message`
            # for the schema-mismatch reason.
            new_mentioned_ids = extract_mentioned_user_ids(body_json)
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
        `deleted_at` to now (a tombstone marker — the body is kept so
        future audit/recovery works, but the FE renders it as deleted).

        Decrements the parent's reply_count if this is a thread reply.
        """
        message = self._fetch_for_user(message_id, request.user, with_prefetch=False)
        is_sender = message.sender_id == request.user.id
        is_channel_owner = message.channel.owner_id and message.channel.owner_id == request.user.id
        if not (is_sender or is_channel_owner):
            return Response(
                {"error": "Only the sender or channel owner can delete."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if message.deleted_at is not None:
            # Idempotent — already gone.
            return Response(status=status.HTTP_204_NO_CONTENT)

        with transaction.atomic():
            message.deleted_at = timezone.now()
            message.save(update_fields=["deleted_at", "ts_updated_at"])
            if message.parent_id is not None:
                # Decrement the parent's reply_count via an F-expression so
                # the read-modify-write happens atomically in SQL. The
                # `gt=0` filter floors at 0 so a double-delete (e.g. via
                # two clients) can't underflow.
                Message.objects.filter(pk=message.parent_id, reply_count__gt=0).update(
                    reply_count=F("reply_count") - 1
                )
        return Response(status=status.HTTP_204_NO_CONTENT)


# Cap individual uploads. Above this, the server returns 413. Keep in
# sync with the frontend client-side guard in `useAttachmentDraft` so
# the user gets a fast error instead of a slow multipart upload that
# fails at the end.
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
        is_member = ChannelMember.objects.filter(
            channel=message.channel, user=request.user, is_deleted=False
        ).exists()
        if not is_member:
            raise Http404("Message not found.")

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
        if file.size > MAX_ATTACHMENT_BYTES:
            return Response(
                {"error": f"File exceeds the {MAX_ATTACHMENT_BYTES}-byte limit."},
                status=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            )

        mime = (request.data.get("mime") or file.content_type or "").strip()

        with transaction.atomic():
            attachment = MessageAttachment.objects.create(
                message=message,
                uploader=request.user,
                file=file,
                mime=mime,
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
