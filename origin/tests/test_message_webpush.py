"""PR4b — plain-message web push (push only, NO activity-feed rows).

Every TOP-LEVEL message in any chat web-pushes the other channel members
(presence + per-chat mute + `enable_chats` gated). Thread replies are
excluded (covered by the thread-reply activity). Mentioned users are
excluded from the plain fan-out (they get the more-specific mention).
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.notification_models import NotificationPreference, PushSubscription
from origin.services import presence, webpush_dispatch
from origin.services.webpush_gating import is_chat_muted
from origin.tests.test_base import BaseAPITestCase

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class IsChatMutedTests(BaseAPITestCase):
    def test_no_prefs_not_muted(self):
        self.assertFalse(is_chat_muted(self.user2.id, "chan-uuid"))

    def test_muted_chat_matches_by_chat_id(self):
        NotificationPreference.objects.create(
            user=self.user2, muted_chats=[{"chat_type": 2, "chat_id": "chan-uuid"}]
        )
        self.assertTrue(is_chat_muted(self.user2.id, "chan-uuid"))
        self.assertFalse(is_chat_muted(self.user2.id, "other-uuid"))


@override_settings(CACHES=LOCMEM)
class MessagePushDispatchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _msg(self, kind=ChannelKind.DM, title="GM Title"):
        return SimpleNamespace(
            channel=SimpleNamespace(kind=kind, id="chan-uuid", title=title),
            sender=SimpleNamespace(username="Alice", profile_image_file_name=""),
            body_text="hello there",
        )

    def _sub(self, user):
        return PushSubscription.objects.create(
            user=user,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
        )

    def _run(self, message, recipients):
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **k: fn(**k)
            ),
        ):
            webpush_dispatch.dispatch_push_for_message(message, recipients)
        return send

    def test_dm_title_is_sender_name(self):
        self._sub(self.user2)
        send = self._run(self._msg(kind=ChannelKind.DM), [self.user2.id])
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["payload"]["title"], "Alice")
        self.assertEqual(send.call_args.kwargs["payload"]["body"], "hello there")

    def test_group_title_includes_channel(self):
        self._sub(self.user2)
        send = self._run(self._msg(kind=ChannelKind.GM, title="Eng"), [self.user2.id])
        self.assertEqual(send.call_args.kwargs["payload"]["title"], "Alice in Eng")
        # tag is the channel id so a burst collapses to one notification.
        self.assertEqual(send.call_args.kwargs["payload"]["tag"], "chats:chan-uuid")

    def test_muted_chat_suppresses(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(
            user=self.user2, muted_chats=[{"chat_type": 1, "chat_id": "chan-uuid"}]
        )
        self._run(self._msg(), [self.user2.id]).assert_not_called()

    def test_enable_chats_coarse_off_suppresses(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(user=self.user2, enable_chats=False)
        self._run(self._msg(), [self.user2.id]).assert_not_called()

    def test_visible_tab_suppresses(self):
        self._sub(self.user2)
        presence.mark_visible(self.user2.id)
        self._run(self._msg(), [self.user2.id]).assert_not_called()

    def test_default_recipient_gets_push(self):
        self._sub(self.user2)
        self._run(self._msg(), [self.user2.id]).assert_called_once()


class MessagePushWiringTests(BaseAPITestCase):
    """End-to-end: the message-send endpoint schedules the plain-message
    fan-out via on_commit for top-level messages, and excludes thread
    replies + already-mentioned users."""

    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Wiring GM", owner=self.user
        )
        ChannelMember.objects.create(channel=self.channel, user=self.user, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.user2, role="member")
        self.url = reverse("v3_messages_delta", args=[self.channel.id])

    def test_plain_message_fans_out_to_other_member(self):
        self.authenticate(self.user)
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_message") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    self.url,
                    {"body": [{"type": "paragraph"}], "body_text": "hi all"},
                    format="json",
                )
            self.assertEqual(resp.status_code, 201)
            disp.assert_called_once()
            _msg, recipients = disp.call_args.args
            self.assertEqual([str(r) for r in recipients], [str(self.user2.id)])

    def test_mentioned_user_excluded_from_plain_fanout(self):
        from django.contrib.auth import get_user_model

        user3 = get_user_model().objects.create_user(
            username="u3wire", email="u3wire@e.com", password="pass12345"
        )
        ChannelMember.objects.create(channel=self.channel, user=user3, role="member")
        self.authenticate(self.user)
        body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": str(self.user2.id), "userName": "U2"}}
                ],
            }
        ]
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_message") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(self.url, {"body": body, "body_text": "@U2"}, format="json")
            disp.assert_called_once()
            _msg, recipients = disp.call_args.args
            # user2 got the mention; only user3 is in the plain fan-out.
            self.assertEqual([str(r) for r in recipients], [str(user3.id)])

    def test_thread_reply_does_not_fan_out(self):
        self.authenticate(self.user)
        parent = self.client.post(
            self.url, {"body": [{"type": "paragraph"}], "body_text": "root"}, format="json"
        ).data["id"]
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_message") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(
                    self.url,
                    {"body": [{"type": "paragraph"}], "body_text": "reply", "parent_id": parent},
                    format="json",
                )
            disp.assert_not_called()
