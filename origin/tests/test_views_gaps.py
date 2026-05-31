"""Tests for v3/common DRF view modules that lacked a dedicated test file.

Covers five previously-untested view modules:
  - chat/read_cursor_views.py      (ReadCursorView)
  - chat/pin_flag_views.py         (PinView, FlagView)
  - chat/reaction_views_v3.py      (MessageReactionsView)
  - common/runtime_config_views.py (RuntimeConfigView)
  - common/notification_views.py   (NotificationPreferenceView)

All five subclass `AuthenticatedAPIView` (IsAuthenticated + SimpleJWT), so
an unauthenticated request returns 401.

The membership-scoped chat views deliberately return 404 (not 403) for
non-members so they don't leak channel/message existence — see the
`_verify_member_or_404` / `_verify_message_member` helpers. We assert that
actual 404 behaviour.

`BaseAPITestCase` puts self.user + self.user2 on self.team but NOT on any
Channel; we create the Channel + ChannelMember rows per-test. We use GM
channels throughout to avoid the DM `ChannelDirectPair` uniqueness signal.

These view paths touch no external services (OpenSearch/LLM/HTTP): we
insert Message/Channel rows directly via the ORM rather than hitting the
send endpoint, so the reaction view's `v3_activity` producer is the only
side-effect and it is pure-DB (writes an Activity row). No mocking needed.
"""

from django.test import override_settings
from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageReaction,
    Pin,
    ReadCursor,
)
from origin.models.common.notification_models import NotificationPreference
from origin.tests.test_base import BaseAPITestCase


class _ChannelMixin:
    """Helpers to build a GM channel + messages for the chat-view tests."""

    def _make_gm(self, *, members=("user",), owner="user"):
        """Create a GM channel with ChannelMember rows for the named users.

        `members` / `owner` are attribute names on self ("user" / "user2").
        """
        owner_obj = getattr(self, owner)
        channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Gaps Test GM",
            owner=owner_obj,
        )
        for name in members:
            user = getattr(self, name)
            ChannelMember.objects.create(
                channel=channel,
                user=user,
                role="owner" if name == owner else "member",
            )
        return channel

    def _make_message(self, channel, *, sender=None, seq=1, text="hello"):
        """Insert a Message via the ORM. `seq` is required + UNIQUE per
        channel and `body` is a required JSONField, so both are explicit."""
        return Message.objects.create(
            channel=channel,
            sender=sender if sender is not None else self.user,
            seq=seq,
            body={"text": text},
            body_text=text,
        )


# ---------------------------------------------------------------------------
# ReadCursorView — PUT /api/v3/channels/{id}/read_cursor/
# ---------------------------------------------------------------------------
class ReadCursorViewTests(_ChannelMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.m1 = self._make_message(self.channel, seq=1)
        self.m2 = self._make_message(self.channel, seq=2)
        self.url = reverse("v3_read_cursor", args=[self.channel.id])

    # ----- auth --------------------------------------------------------
    def test_unauthenticated_returns_401(self):
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    # ----- happy path --------------------------------------------------
    def test_advance_cursor_creates_row(self):
        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["lastReadMessageId"], str(self.m1.id))
        self.assertIsNone(resp.data["threadRootId"])
        self.assertEqual(resp.data["channelId"], str(self.channel.id))
        cursor = ReadCursor.objects.get(
            user=self.user, channel=self.channel, thread_root__isnull=True
        )
        self.assertEqual(cursor.last_read_message_id, self.m1.id)

    def test_forward_only_advance_does_not_rewind(self):
        """Sending a lower-seq message id after a higher one is a no-op —
        server-side truth wins (forward-only)."""
        self.authenticate()
        # Advance to m2 (seq=2) first.
        self.client.put(
            self.url, {"last_read_message_id": str(self.m2.id)}, format="json"
        )
        # Now try to rewind to m1 (seq=1).
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Cursor should still point at m2, not be rewound to m1.
        self.assertEqual(resp.data["lastReadMessageId"], str(self.m2.id))
        cursor = ReadCursor.objects.get(
            user=self.user, channel=self.channel, thread_root__isnull=True
        )
        self.assertEqual(cursor.last_read_message_id, self.m2.id)

    # ----- validation / errors -----------------------------------------
    def test_missing_message_id_returns_400(self):
        self.authenticate()
        resp = self.client.put(self.url, {}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_message_not_in_channel_returns_404(self):
        """A message id that exists nowhere in this channel → 404."""
        self.authenticate()
        resp = self.client.put(
            self.url,
            {"last_read_message_id": "00000000-0000-0000-0000-000000000000"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_message_from_other_channel_returns_404(self):
        """A real message that belongs to a DIFFERENT channel → 404 (the
        view scopes the lookup to `channel=channel`)."""
        other = self._make_gm(members=("user",))
        other_msg = self._make_message(other, seq=1)
        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(other_msg.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    # ----- membership 404 ----------------------------------------------
    def test_non_member_returns_404(self):
        """user2 is not a ChannelMember → 404 (existence hidden)."""
        self.authenticate(self.user2)
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# PinView — POST/DELETE /api/v3/channels/{id}/pin/
# ---------------------------------------------------------------------------
class PinViewTests(_ChannelMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.url = reverse("v3_channel_pin", args=[self.channel.id])

    def test_unauthenticated_returns_401(self):
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_pin_creates_then_idempotent(self):
        self.authenticate()
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["channelId"], str(self.channel.id))
        self.assertEqual(Pin.objects.filter(user=self.user, channel=self.channel).count(), 1)

        # Re-pin: idempotent → 200, no duplicate row.
        resp2 = self.client.post(self.url)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(Pin.objects.filter(user=self.user, channel=self.channel).count(), 1)

    def test_unpin_returns_204_and_removes_row(self):
        self.authenticate()
        Pin.objects.create(user=self.user, channel=self.channel)
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(Pin.objects.filter(user=self.user, channel=self.channel).exists())

    def test_unpin_when_not_pinned_still_204(self):
        """Idempotent unpin: deleting a pin that doesn't exist → 204."""
        self.authenticate()
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_member_pin_returns_404(self):
        self.authenticate(self.user2)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(Pin.objects.filter(user=self.user2, channel=self.channel).exists())

    def test_pin_nonexistent_channel_returns_404(self):
        self.authenticate()
        url = reverse(
            "v3_channel_pin", args=["00000000-0000-0000-0000-000000000000"]
        )
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# FlagView — POST/DELETE /api/v3/messages/{id}/flag/
# ---------------------------------------------------------------------------
class FlagViewTests(_ChannelMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = self._make_gm(members=("user",))
        self.message = self._make_message(self.channel, seq=1)
        self.url = reverse("v3_message_flag", args=[self.message.id])

    def test_unauthenticated_returns_401(self):
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_flag_creates_then_idempotent(self):
        self.authenticate()
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["messageId"], str(self.message.id))
        self.assertEqual(
            Flag.objects.filter(user=self.user, message=self.message).count(), 1
        )

        resp2 = self.client.post(self.url)
        self.assertEqual(resp2.status_code, status.HTTP_200_OK)
        self.assertEqual(
            Flag.objects.filter(user=self.user, message=self.message).count(), 1
        )

    def test_unflag_returns_204(self):
        self.authenticate()
        Flag.objects.create(user=self.user, message=self.message)
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            Flag.objects.filter(user=self.user, message=self.message).exists()
        )

    def test_unflag_when_not_flagged_still_204(self):
        self.authenticate()
        resp = self.client.delete(self.url)
        self.assertEqual(resp.status_code, status.HTTP_204_NO_CONTENT)

    def test_non_member_flag_returns_404(self):
        """user2 is not a member of the message's channel → 404."""
        self.authenticate(self.user2)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_flag_nonexistent_message_returns_404(self):
        self.authenticate()
        url = reverse(
            "v3_message_flag", args=["00000000-0000-0000-0000-000000000000"]
        )
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# MessageReactionsView — POST/DELETE /api/v3/messages/{id}/reactions/
# ---------------------------------------------------------------------------
class MessageReactionsViewTests(_ChannelMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # Both users on the channel so reactions cross-user (to exercise
        # the activity-fanout branch).
        self.channel = self._make_gm(members=("user", "user2"))
        # Message sent by user2 so a reaction by self.user produces an
        # Activity row (actor != sender).
        self.message = self._make_message(self.channel, sender=self.user2, seq=1)
        self.url = reverse("v3_message_reactions", args=[self.message.id])

    def test_unauthenticated_returns_401(self):
        resp = self.client.post(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_add_reaction_creates_and_fans_out_activity(self):
        self.authenticate()  # self.user reacts to user2's message
        resp = self.client.post(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["emoji"], "👍")
        # Server-derived channel coordinates for the WS broadcast.
        self.assertEqual(resp.data["channelId"], str(self.channel.id))
        self.assertEqual(resp.data["channelKind"], ChannelKind.GM)
        self.assertTrue(
            MessageReaction.objects.filter(
                message=self.message, user=self.user, emoji="👍"
            ).exists()
        )
        # An Activity (REACTION) row should exist for the sender (user2).
        act = Activity.objects.get(recipient=self.user2, activity_type=ActivityType.REACTION)
        self.assertEqual(act.actor_id, self.user.id)
        self.assertEqual(act.meta.get("emoji"), "👍")
        # Proxy field for the WS handler.
        self.assertEqual(len(resp.data["_v3_activities"]), 1)

    def test_add_reaction_idempotent_returns_200(self):
        self.authenticate()
        self.client.post(self.url, {"emoji": "👍"}, format="json")
        resp = self.client.post(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            MessageReaction.objects.filter(
                message=self.message, user=self.user, emoji="👍"
            ).count(),
            1,
        )
        # Re-add is a no-op: no second activity row.
        self.assertEqual(
            Activity.objects.filter(
                recipient=self.user2, activity_type=ActivityType.REACTION
            ).count(),
            1,
        )

    def test_self_reaction_produces_no_activity(self):
        """A user reacting to their OWN message gets no activity row."""
        self.authenticate(self.user2)  # user2 reacts to user2's own message
        resp = self.client.post(self.url, {"emoji": "🎉"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(resp.data["_v3_activities"], [])
        self.assertEqual(
            Activity.objects.filter(activity_type=ActivityType.REACTION).count(), 0
        )

    def test_remove_reaction_returns_200_with_channel_coords(self):
        """DELETE returns 200 with {channelId, channelKind} (NOT 204) so
        the WS layer can broadcast `reaction.removed` to the real room."""
        self.authenticate()
        self.client.post(self.url, {"emoji": "👍"}, format="json")
        resp = self.client.delete(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["channelId"], str(self.channel.id))
        self.assertEqual(resp.data["channelKind"], ChannelKind.GM)
        self.assertFalse(
            MessageReaction.objects.filter(
                message=self.message, user=self.user, emoji="👍"
            ).exists()
        )

    def test_remove_reaction_when_absent_still_200(self):
        """Idempotent removal of a reaction that doesn't exist → 200."""
        self.authenticate()
        resp = self.client.delete(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

    def test_add_reaction_empty_emoji_returns_400(self):
        self.authenticate()
        resp = self.client.post(self.url, {"emoji": ""}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_add_reaction_non_string_emoji_returns_400(self):
        self.authenticate()
        resp = self.client.post(self.url, {"emoji": 123}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_member_reaction_returns_404(self):
        """A user with no membership on the message's channel → 404."""
        # Remove user from the channel so they are no longer a member,
        # but keep user2. Authenticate as the now-removed user.
        ChannelMember.objects.filter(channel=self.channel, user=self.user).update(
            is_deleted=True
        )
        self.authenticate()
        resp = self.client.post(self.url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)

    def test_reaction_nonexistent_message_returns_404(self):
        self.authenticate()
        url = reverse(
            "v3_message_reactions", args=["00000000-0000-0000-0000-000000000000"]
        )
        resp = self.client.post(url, {"emoji": "👍"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


# ---------------------------------------------------------------------------
# RuntimeConfigView — GET /api/runtime-config/
# ---------------------------------------------------------------------------
class RuntimeConfigViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("runtime_config")

    def test_unauthenticated_returns_401(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_defaults_when_no_settings_override(self):
        """settings_test does not define RUNTIME_CONFIG → conservative
        fail-closed defaults: version 1, all rollout flags 0, panic off."""
        self.authenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["version"], 1)
        self.assertEqual(
            resp.data["use_new_chat"], {"dm": 0, "gm": 0, "mdm": 0, "pm": 0}
        )
        self.assertFalse(resp.data["panic_switch"])

    @override_settings(
        RUNTIME_CONFIG={
            "use_new_chat": {"dm": 5000},  # partial nested override
            "panic_switch": True,
        }
    )
    def test_settings_override_merges_over_defaults(self):
        """`_read_config` merges nested dicts one level deep: the supplied
        `dm` wins, the omitted gm/mdm/pm fall back to the 0 defaults, and
        the top-level panic switch is overridden."""
        self.authenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["version"], 1)
        self.assertEqual(
            resp.data["use_new_chat"], {"dm": 5000, "gm": 0, "mdm": 0, "pm": 0}
        )
        self.assertTrue(resp.data["panic_switch"])


# ---------------------------------------------------------------------------
# NotificationPreferenceView — GET/PUT /api/v2/user/notification-preferences/
# ---------------------------------------------------------------------------
class NotificationPreferenceViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.url = reverse("user_notification_preferences")

    def test_unauthenticated_get_returns_401(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_get_lazily_creates_row_with_defaults(self):
        self.assertFalse(
            NotificationPreference.objects.filter(user=self.user).exists()
        )
        self.authenticate()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # All five master toggles default True; muted_chats empty list.
        self.assertTrue(resp.data["master_enabled"])
        self.assertTrue(resp.data["enable_chats"])
        self.assertTrue(resp.data["enable_mentions"])
        self.assertEqual(resp.data["muted_chats"], [])
        # Row was created lazily.
        self.assertTrue(
            NotificationPreference.objects.filter(user=self.user).exists()
        )

    def test_put_partial_update_persists(self):
        self.authenticate()
        resp = self.client.put(
            self.url,
            {
                "enable_chats": False,
                "muted_chats": [{"chat_type": 2, "chat_id": "abc"}],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertFalse(resp.data["enable_chats"])
        # Untouched fields keep their defaults (partial update).
        self.assertTrue(resp.data["master_enabled"])
        self.assertEqual(
            resp.data["muted_chats"], [{"chat_type": 2, "chat_id": "abc"}]
        )
        prefs = NotificationPreference.objects.get(user=self.user)
        self.assertFalse(prefs.enable_chats)
        self.assertEqual(prefs.muted_chats, [{"chat_type": 2, "chat_id": "abc"}])

    def test_put_normalizes_and_dedupes_muted_chats(self):
        """The serializer drops duplicate (chat_type, chat_id) pairs and
        keeps optional chat_name only when truthy."""
        self.authenticate()
        resp = self.client.put(
            self.url,
            {
                "muted_chats": [
                    {"chat_type": 1, "chat_id": "x", "chat_name": "Alice"},
                    {"chat_type": 1, "chat_id": "x"},  # dup → dropped
                    {"chat_type": 2, "chat_id": "y"},
                ]
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        muted = resp.data["muted_chats"]
        self.assertEqual(len(muted), 2)
        self.assertEqual(muted[0], {"chat_type": 1, "chat_id": "x", "chat_name": "Alice"})
        self.assertEqual(muted[1], {"chat_type": 2, "chat_id": "y"})

    def test_put_invalid_muted_chats_type_returns_400(self):
        """chat_type must be an int — a string value is rejected."""
        self.authenticate()
        resp = self.client.put(
            self.url,
            {"muted_chats": [{"chat_type": "gm", "chat_id": "abc"}]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_muted_chats_not_a_list_returns_400(self):
        self.authenticate()
        resp = self.client.put(
            self.url, {"muted_chats": "nope"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
