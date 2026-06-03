"""Web Push for inbox items.

Inbox items (activity message + join team/project/GM requests) are their
own surface, not the Activity feed, so they push via the dedicated
`dispatch_push_for_inbox_item` under the `inbox` category (coarse
`enable_inbox`). These tests assert the per-type titles, the category
gating, and that the create endpoint actually schedules a push through
`on_commit` (the wiring the audit found missing).
"""

import uuid
from types import SimpleNamespace
from unittest import mock

from django.core.cache import cache
from django.test import override_settings

from origin.models.common.notification_models import NotificationPreference, PushSubscription
from origin.services import presence, webpush_dispatch
from origin.services.webpush_gating import should_push
from origin.tests.test_base import BaseAPITestCase

LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


class InboxShouldPushTests(BaseAPITestCase):
    def test_inbox_default_on(self):
        self.assertTrue(should_push(self.user2.id, "inbox"))

    def test_enable_inbox_coarse_gate(self):
        NotificationPreference.objects.create(user=self.user2, enable_inbox=False)
        self.assertFalse(should_push(self.user2.id, "inbox"))

    def test_inbox_category_override(self):
        NotificationPreference.objects.create(
            user=self.user2, category_settings={"inbox": False}
        )
        self.assertFalse(should_push(self.user2.id, "inbox"))


@override_settings(CACHES=LOCMEM)
class InboxDispatchTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()

    def _item(self, item_type=1):
        return SimpleNamespace(
            item_id=99,
            item_type=item_type,
            receiver_id=self.user2.id,
            sender=SimpleNamespace(username="Alice", profile_image_file_name=""),
        )

    def _sub(self, user):
        return PushSubscription.objects.create(
            user=user,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
        )

    def _run(self, item, **kw):
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **k: fn(**k)
            ),
        ):
            webpush_dispatch.dispatch_push_for_inbox_item(item, **kw)
        return send

    def test_join_team_request_title(self):
        self._sub(self.user2)
        send = self._run(self._item(item_type=1))
        send.assert_called_once()
        self.assertEqual(
            send.call_args.kwargs["payload"]["title"], "Alice asked to join your team"
        )
        self.assertEqual(send.call_args.kwargs["payload"]["url"], "/workspace/inbox")

    def test_join_project_request_title(self):
        self._sub(self.user2)
        send = self._run(self._item(item_type=2))
        self.assertEqual(
            send.call_args.kwargs["payload"]["title"], "Alice asked to join your project"
        )

    def test_title_override_for_approval(self):
        self._sub(self.user2)
        send = self._run(self._item(item_type=1), title="Your request was approved")
        self.assertEqual(send.call_args.kwargs["payload"]["title"], "Your request was approved")

    def test_visible_tab_suppresses(self):
        self._sub(self.user2)
        presence.mark_visible(self.user2.id)
        self._run(self._item()).assert_not_called()

    def test_coarse_gate_blocks(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(user=self.user2, enable_inbox=False)
        self._run(self._item()).assert_not_called()


class InboxPushWiringTests(BaseAPITestCase):
    """The create endpoint must actually reach dispatch via on_commit —
    the gap the audit found (inbox views never dispatched)."""

    def test_inbox_create_schedules_push_for_receiver(self):
        self.authenticate(self.user)
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_inbox_item") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    "/api/v2/inbox/",
                    {
                        "team_id": str(self.team.team_id),
                        "sender_id": str(self.user.id),
                        "receiver_id": str(self.user2.id),
                        "item_body": {"text": "hi"},
                        "item_type": 0,
                    },
                    format="json",
                )
            self.assertEqual(resp.status_code, 201)
            disp.assert_called_once()
            item = disp.call_args.args[0]
            self.assertEqual(str(item.receiver_id), str(self.user2.id))
            self.assertEqual(item.item_type, 0)

    def test_inbox_create_duplicate_does_not_repush(self):
        self.authenticate(self.user)
        payload = {
            "team_id": str(self.team.team_id),
            "sender_id": str(self.user.id),
            "receiver_id": str(self.user2.id),
            "item_body": {"text": "dup"},
            "item_type": 0,
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post("/api/v2/inbox/", payload, format="json")
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_inbox_item") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post("/api/v2/inbox/", payload, format="json")  # idempotent dup
            disp.assert_not_called()
