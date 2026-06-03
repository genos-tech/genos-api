"""Tests for the Web Push slice: preference gating, presence, the deep-link
URL builder, and the mention-activity dispatch decision matrix.

The dispatch tests pass a `SimpleNamespace` "activity" because
`dispatch_push_for_activities` only READS attributes off it — no ORM
re-query — so we avoid building the full Activity/Channel/Message graph.
`send_web_push` is mocked and the thread pool is made synchronous so the
decision logic is asserted without real HTTP. A locmem cache isolates the
presence key from the running app's Redis.
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.chat.unified_models import ActivityType, ChannelKind
from origin.models.common.notification_models import (
    NotificationPreference,
    PushSubscription,
)
from origin.services import presence, webpush_dispatch
from origin.services.webpush_gating import should_push
from origin.tests.test_base import BaseAPITestCase

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class ShouldPushTests(BaseAPITestCase):
    def test_no_prefs_row_uses_push_default_on(self):
        # user2 has no NotificationPreference row -> push default (mentions on).
        self.assertTrue(should_push(self.user2.id, "mention_chat"))

    def test_push_enabled_false_blocks(self):
        NotificationPreference.objects.create(user=self.user2, push_enabled=False)
        self.assertFalse(should_push(self.user2.id, "mention_chat"))

    def test_master_disabled_blocks(self):
        NotificationPreference.objects.create(user=self.user2, master_enabled=False)
        self.assertFalse(should_push(self.user2.id, "mention_chat"))

    def test_coarse_mentions_off_blocks(self):
        NotificationPreference.objects.create(user=self.user2, enable_mentions=False)
        self.assertFalse(should_push(self.user2.id, "mention_chat"))

    def test_category_override_off_blocks(self):
        NotificationPreference.objects.create(
            user=self.user2, category_settings={"mention_chat": False}
        )
        self.assertFalse(should_push(self.user2.id, "mention_chat"))

    def test_default_row_allows(self):
        NotificationPreference.objects.create(user=self.user2)
        self.assertTrue(should_push(self.user2.id, "mention_chat"))


class PushSubscriptionEndpointTests(BaseAPITestCase):
    """POST /api/v2/user/push-subscriptions/ is an upsert keyed on endpoint.

    Regression guard: the model's `endpoint` column is `unique=True`, which
    makes DRF's ModelSerializer auto-attach a UniqueValidator. That validator
    would 400 every re-registration of the same browser (the FE re-POSTs on
    each mount), defeating the view's deliberate `update_or_create`.
    """

    URL = "/api/v2/user/push-subscriptions/"

    def _payload(self, endpoint, p256dh="p256", auth="auth"):
        return {
            "endpoint": endpoint,
            "p256dh": p256dh,
            "auth": auth,
            "user_agent": "pytest",
        }

    def test_register_then_reregister_same_endpoint_upserts(self):
        self.authenticate(self.user)
        endpoint = "https://push.example.com/sub-abc"

        first = self.client.post(self.URL, self._payload(endpoint), format="json")
        self.assertEqual(first.status_code, 204)

        # Same browser re-subscribing (rotated keys) must upsert, not 400.
        second = self.client.post(
            self.URL,
            self._payload(endpoint, p256dh="p256-new", auth="auth-new"),
            format="json",
        )
        self.assertEqual(second.status_code, 204)

        subs = PushSubscription.objects.filter(endpoint=endpoint)
        self.assertEqual(subs.count(), 1)
        self.assertEqual(subs.first().p256dh, "p256-new")
        self.assertTrue(subs.first().is_active)

    def test_missing_keys_rejected(self):
        self.authenticate(self.user)
        resp = self.client.post(
            self.URL,
            {"endpoint": "https://push.example.com/x"},
            format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_non_http_endpoint_rejected(self):
        self.authenticate(self.user)
        resp = self.client.post(self.URL, self._payload("not-a-url"), format="json")
        self.assertEqual(resp.status_code, 400)


@override_settings(CACHES=LOCMEM)
class PresenceTests(BaseAPITestCase):
    def test_mark_then_read(self):
        cache.clear()
        self.assertFalse(presence.has_visible_tab(self.user2.id))
        presence.mark_visible(self.user2.id)
        self.assertTrue(presence.has_visible_tab(self.user2.id))


class ChatUrlTests(BaseAPITestCase):
    def test_url_token_per_kind(self):
        for kind, token in [
            (ChannelKind.DM, "dm"),
            (ChannelKind.GM, "gm"),
            (ChannelKind.PM, "pm"),
            (ChannelKind.MDM, "mdm"),
        ]:
            ch = SimpleNamespace(kind=kind, id="chan-id")
            self.assertEqual(webpush_dispatch._chat_url(ch), f"/workspace/chat/{token}/chan-id")


@override_settings(CACHES=LOCMEM)
class DispatchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _activity(self, recipient, atype=ActivityType.MENTION):
        return SimpleNamespace(
            id=uuid.uuid4(),
            activity_type=atype,
            recipient_id=recipient.id,
            actor=SimpleNamespace(username="Alice"),
            channel=SimpleNamespace(kind=ChannelKind.DM, id="chan-uuid"),
            message=SimpleNamespace(body_text="hello there"),
        )

    def _sub(self, user):
        return PushSubscription.objects.create(
            user=user,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
        )

    def _run(self, activities):
        """Dispatch with VAPID 'configured', send mocked, pool synchronous."""
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **kw: fn(**kw)
            ),
        ):
            webpush_dispatch.dispatch_push_for_activities(activities)
        return send

    def test_eligible_recipient_gets_send_with_payload(self):
        self._sub(self.user2)
        send = self._run([self._activity(self.user2)])
        send.assert_called_once()
        payload = send.call_args.kwargs["payload"]
        self.assertEqual(payload["title"], "Alice mentioned you")
        self.assertEqual(payload["body"], "hello there")
        self.assertEqual(payload["url"], "/workspace/chat/dm/chan-uuid")

    def test_visible_tab_suppresses(self):
        self._sub(self.user2)
        presence.mark_visible(self.user2.id)
        self._run([self._activity(self.user2)]).assert_not_called()

    def test_no_subscription_no_send(self):
        self._run([self._activity(self.user2)]).assert_not_called()

    def test_non_mention_ignored(self):
        self._sub(self.user2)
        self._run([self._activity(self.user2, ActivityType.REACTION)]).assert_not_called()

    def test_push_disabled_recipient_no_send(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(user=self.user2, push_enabled=False)
        self._run([self._activity(self.user2)]).assert_not_called()
