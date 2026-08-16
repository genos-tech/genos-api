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

    def test_snooze_until_blocks_all_categories(self):
        # A Slack-style pause silences push regardless of per-category prefs.
        from datetime import timedelta

        from django.utils import timezone

        NotificationPreference.objects.create(
            user=self.user2, snooze_until=timezone.now() + timedelta(hours=1)
        )
        for cat in ("mention_chat", "task_comments", "chats", "reactions"):
            self.assertFalse(should_push(self.user2.id, cat), cat)

    def test_expired_snooze_does_not_block(self):
        from datetime import timedelta

        from django.utils import timezone

        NotificationPreference.objects.create(
            user=self.user2, snooze_until=timezone.now() - timedelta(minutes=1)
        )
        self.assertTrue(should_push(self.user2.id, "mention_chat"))

    def test_new_categories_default_on(self):
        for cat in ("mention_task", "mention_note", "task_comments", "comments", "reactions"):
            self.assertTrue(should_push(self.user2.id, cat), cat)

    def test_task_comments_coarse_gate(self):
        NotificationPreference.objects.create(user=self.user2, enable_task_comments=False)
        self.assertFalse(should_push(self.user2.id, "task_comments"))
        # An unrelated coarse toggle doesn't affect it.
        self.assertTrue(should_push(self.user, "task_comments"))

    def test_comments_ride_enable_task_comments(self):
        # The inline-comment participant fan-out has no master switch of its
        # own — it rides the task-comments coarse group, so turning that off
        # silences it too (and turning off an unrelated group does not).
        NotificationPreference.objects.create(user=self.user2, enable_task_comments=False)
        self.assertFalse(should_push(self.user2.id, "comments"))
        self.assertTrue(should_push(self.user, "comments"))

    def test_surface_mentions_ride_enable_mentions(self):
        NotificationPreference.objects.create(user=self.user2, enable_mentions=False)
        self.assertFalse(should_push(self.user2.id, "mention_task"))
        self.assertFalse(should_push(self.user2.id, "mention_note"))

    def test_reactions_have_no_coarse_gate(self):
        # No `enable_reactions` column exists; only the fine category /
        # default applies (master + push_enabled still hard-gate).
        NotificationPreference.objects.create(user=self.user2, enable_mentions=False)
        self.assertTrue(should_push(self.user2.id, "reactions"))


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

    def test_presence_is_per_device(self):
        """A visible laptop must not mark the phone as visible.

        This is the whole point of per-device presence: keyed per user,
        one open tab silenced every other device the user owned.
        """
        cache.clear()
        presence.mark_visible(self.user2.id, "laptop-abc")
        self.assertTrue(presence.is_device_visible(self.user2.id, "laptop-abc"))
        self.assertFalse(presence.is_device_visible(self.user2.id, "phone-xyz"))

    def test_clear_visible_reopens_push_immediately(self):
        """The iOS dead zone: a suspended page stops notifying long before
        the 90s TTL expires, so waiting it out means nothing notifies at
        all. Hiding the tab must reopen push right away."""
        cache.clear()
        presence.mark_visible(self.user2.id, "phone-xyz")
        self.assertTrue(presence.is_device_visible(self.user2.id, "phone-xyz"))
        presence.clear_visible(self.user2.id, "phone-xyz")
        self.assertFalse(presence.is_device_visible(self.user2.id, "phone-xyz"))

    def test_clear_is_scoped_to_the_device(self):
        cache.clear()
        presence.mark_visible(self.user2.id, "phone-xyz")
        presence.mark_visible(self.user2.id, "laptop-abc")
        presence.clear_visible(self.user2.id, "phone-xyz")
        self.assertFalse(presence.is_device_visible(self.user2.id, "phone-xyz"))
        self.assertTrue(presence.is_device_visible(self.user2.id, "laptop-abc"))

    def test_device_without_id_falls_back_to_any_tab(self):
        """Subscriptions predating per-device presence keep the old
        behavior rather than being pushed while the user is clearly
        looking at the app."""
        cache.clear()
        self.assertFalse(presence.is_device_visible(self.user2.id, ""))
        presence.mark_visible(self.user2.id, "laptop-abc")
        self.assertTrue(presence.is_device_visible(self.user2.id, ""))


@override_settings(CACHES=LOCMEM)
class PresenceScopeTests(BaseAPITestCase):
    """WHICH devices a visible tab silences — a per-category choice.

    `"device"` is the default and is covered by `DispatchTests`; these pin
    the two deliberate departures from it.
    """

    def setUp(self):
        super().setUp()
        cache.clear()

    def _sub(self, device_id=""):
        return PushSubscription.objects.create(
            user=self.user2,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
            device_id=device_id,
        )

    def _queue(self, **kwargs):
        """Call the fan-out with the send mocked and the pool synchronous."""
        with (
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda fn, **kw: fn(**kw)
            ),
        ):
            webpush_dispatch._queue_push(
                recipient_id=self.user2.id,
                category="agent_run_done",
                title="Your AI answer is ready",
                body="",
                url="/workspace/genos",
                tag="agent_run_done:run-1",
                **kwargs,
            )
        return send

    def test_user_scope_silences_every_device_when_one_is_visible(self):
        """Asking Genos on the laptop must not buzz the phone.

        This answer is to a question the user asked seconds ago, and the
        laptop they asked from is already showing it. Under the default
        per-device scope the phone still got a card for an answer they
        were sitting there watching arrive.
        """
        self._sub(device_id="laptop-abc")
        self._sub(device_id="phone-xyz")
        presence.mark_visible(self.user2.id, "laptop-abc")

        self._queue(presence_scope="user").assert_not_called()

    def test_user_scope_still_pushes_when_no_device_is_visible(self):
        # They really did walk away — the case the notice exists for.
        self._sub(device_id="laptop-abc")
        self._sub(device_id="phone-xyz")
        self.assertEqual(self._queue(presence_scope="user").call_count, 2)

    def test_device_scope_leaves_the_other_devices_notifying(self):
        # The contrast that keeps this a per-category choice rather than
        # a blanket change: same presence, default scope, phone still
        # gets its card.
        self._sub(device_id="laptop-abc")
        phone = self._sub(device_id="phone-xyz")
        presence.mark_visible(self.user2.id, "laptop-abc")

        send = self._queue(presence_scope="device")
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["subscription_id"], phone.id)

    def test_ignore_scope_reaches_a_device_being_looked_at(self):
        # Reminders: the open app has no way to raise the moment itself,
        # so presence must not swallow it.
        self._sub(device_id="laptop-abc")
        presence.mark_visible(self.user2.id, "laptop-abc")
        self._queue(presence_scope="ignore").assert_called_once()


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

    def _activity(
        self,
        recipient,
        atype=ActivityType.MENTION,
        *,
        surface_type=None,
        meta=None,
        msg_metadata=None,
        channel=True,
        thread_root_id=None,
        parent_id=None,
    ):
        return SimpleNamespace(
            id=uuid.uuid4(),
            activity_type=atype,
            recipient_id=recipient.id,
            actor=SimpleNamespace(username="Alice", profile_image_file_name=""),
            channel=SimpleNamespace(kind=ChannelKind.DM, id="chan-uuid") if channel else None,
            message_id=uuid.uuid4(),
            message=SimpleNamespace(
                body_text="hello there",
                metadata=(msg_metadata or {}),
                thread_root_id=thread_root_id,
                parent_id=parent_id,
            ),
            surface_type=surface_type,
            meta=(meta or {}),
        )

    def _sub(self, user, device_id=""):
        return PushSubscription.objects.create(
            user=user,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
            device_id=device_id,
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

    def test_muted_chat_suppresses(self):
        """Muting a chat has to cover what happens INSIDE it.

        The plain-message path checked this; this one didn't, so muting a
        busy channel silenced its messages but not the mentions, reactions
        and thread replies in it — someone @mentions you in a chat you
        muted and your phone still lights up. The client already drops
        these locally, which is why the gap only showed on a hidden tab:
        there, the push IS the notification.
        """
        self._sub(self.user2)
        NotificationPreference.objects.create(
            user=self.user2, muted_chats=[{"chat_type": 1, "chat_id": "chan-uuid"}]
        )
        self._run([self._activity(self.user2)]).assert_not_called()

    def test_muting_another_chat_does_not_suppress(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(
            user=self.user2, muted_chats=[{"chat_type": 1, "chat_id": "other-uuid"}]
        )
        self._run([self._activity(self.user2)]).assert_called_once()

    def test_mute_lookup_survives_a_channelless_activity(self):
        """A task/note/project activity has no channel to mute by.

        Reaching for one must not throw — the dispatch loop swallows
        exceptions per activity, so a mistake here would silently drop the
        push rather than fail loudly.
        """
        self._sub(self.user2)
        NotificationPreference.objects.create(
            user=self.user2, muted_chats=[{"chat_type": 1, "chat_id": "chan-uuid"}]
        )
        send = self._run(
            [
                self._activity(
                    self.user2,
                    ActivityType.MENTION,
                    surface_type=5,  # SURFACE_TASK_BODY
                    meta={"displayId": "PRG-12", "projectId": 3, "taskId": 12},
                    channel=False,
                )
            ]
        )
        send.assert_called_once()

    # --- `muted_targets`: the fine per-object mute -------------------------
    # The thread/task/note header buttons write these. Until now the server
    # never read the list, so muting a task or a note silenced the in-app
    # toast and nothing else — close the tab and every "muted" thing was
    # loud again. These pin the matching rules, and in particular the two
    # places where the server deliberately declines to guess.

    def _task_mention(self, **kw):
        return self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=5,  # SURFACE_TASK_BODY
            meta={"displayId": "PRG-12", "projectId": 3, "taskId": 12},
            channel=False,
            **kw,
        )

    def _note_mention(self, surface_type, note_id=42):
        return self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=surface_type,
            meta={"noteId": note_id},
            channel=False,
        )

    def _mute(self, *entries):
        NotificationPreference.objects.create(user=self.user2, muted_targets=list(entries))

    def test_muted_task_suppresses(self):
        self._sub(self.user2)
        self._mute({"target_type": "task", "target_id": "12"})
        self._run([self._task_mention()]).assert_not_called()

    def test_muting_another_task_does_not_suppress(self):
        self._sub(self.user2)
        self._mute({"target_type": "task", "target_id": "999"})
        self._run([self._task_mention()]).assert_called_once()

    def test_task_id_matches_a_string_meta_id_too(self):
        """`target_id` is always a string, but `meta.taskId` is whichever the
        producer wrote — an int from the socket emitter, a string from the
        v3 endpoints. Both have to match or the mute is a silent no-op for
        half the producers."""
        self._sub(self.user2)
        self._mute({"target_type": "task", "target_id": "t1"})
        act = self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=5,
            meta={"displayId": "PRG-1", "projectId": "p1", "taskId": "t1"},
            channel=False,
        )
        self._run([act]).assert_not_called()

    def test_muted_thread_suppresses_a_reply_to_the_root(self):
        self._sub(self.user2)
        self._mute({"target_type": "thread", "target_id": "root-1"})
        act = self._activity(
            self.user2, ActivityType.THREAD_REPLY, thread_root_id="root-1", parent_id="root-1"
        )
        self._run([act]).assert_not_called()

    def test_muted_thread_suppresses_a_nested_reply(self):
        """A reply to a reply. The parent is NOT the thread root.

        The mute button stores the thread ROOT, and the native v3 send path
        deliberately walks to the root (`_resolve_thread_root`), so matching
        on the immediate parent would let every deep reply through — which is
        the bug the client still has, since its activity intents carry
        `message.parentId`.
        """
        self._sub(self.user2)
        self._mute({"target_type": "thread", "target_id": "root-1"})
        act = self._activity(
            self.user2, ActivityType.THREAD_REPLY, thread_root_id="root-1", parent_id="reply-7"
        )
        self._run([act]).assert_not_called()

    def test_thread_mute_falls_back_to_parent_when_root_is_unset(self):
        """The legacy mirror path stores `thread_root = parent`; older rows
        may have only a parent. Matching must still find them."""
        self._sub(self.user2)
        self._mute({"target_type": "thread", "target_id": "root-1"})
        act = self._activity(self.user2, ActivityType.THREAD_REPLY, parent_id="root-1")
        self._run([act]).assert_not_called()

    def test_note_mute_needs_its_note_type(self):
        """The refusal-to-guess case, and the reason it matters.

        Personal, task and chat notes are three tables with three
        independent primary keys, so `note 42` names up to three different
        notes. An entry with no `note_type` — every entry written before the
        field existed — must therefore mute NOTHING rather than silence two
        notes the user never touched. Failing open is a visible annoyance;
        failing closed is invisible.
        """
        self._sub(self.user2)
        self._mute({"target_type": "note", "target_id": "42"})
        self._run([self._note_mention(6)]).assert_called_once()

    def test_note_mute_with_matching_note_type_suppresses(self):
        self._sub(self.user2)
        self._mute({"target_type": "note", "target_id": "42", "note_type": 1})
        self._run([self._note_mention(6)]).assert_not_called()

    def test_note_mute_does_not_leak_across_note_types(self):
        """Muting personal note 42 must not silence task note 42."""
        self._sub(self.user2)
        self._mute({"target_type": "note", "target_id": "42", "note_type": 1})
        # surface 7 == task note == note_type 2
        self._run([self._note_mention(7)]).assert_called_once()

    def test_category_scoped_mute_only_covers_that_category(self):
        self._sub(self.user2)
        self._mute({"target_type": "task", "target_id": "12", "categories": ["thread_replies"]})
        # The activity is a `mention_task`, which the scope excludes.
        self._run([self._task_mention()]).assert_called_once()

    def test_category_scoped_mute_matches_its_own_category(self):
        self._sub(self.user2)
        self._mute({"target_type": "task", "target_id": "12", "categories": ["mention_task"]})
        self._run([self._task_mention()]).assert_not_called()

    def test_chat_target_entries_are_left_to_muted_chats(self):
        """`target_type: "chat"` is not honored here on purpose.

        No UI writes one, `muted_chats` already covers chats, and the client
        additionally requires a `chat_type` on both sides that the serializer
        does not mandate — so honoring these naively would mute where the
        client does not.
        """
        self._sub(self.user2)
        self._mute({"target_type": "chat", "target_id": "chan-uuid"})
        self._run([self._activity(self.user2)]).assert_called_once()

    def test_unknown_and_malformed_entries_are_ignored(self):
        """A hand-rolled API client can store junk. It must not throw: the
        dispatch loop swallows per-activity exceptions, so a crash here would
        silently drop the push instead of failing loudly."""
        self._sub(self.user2)
        self._mute(
            "not-a-dict",
            {},
            {"target_type": "task"},
            {"target_type": "task", "target_id": ""},
            {"target_type": "project", "target_id": "3"},
            {"target_type": "note", "target_id": "42", "note_type": True},
        )
        self._run([self._task_mention()]).assert_called_once()

    def test_zero_ids_are_not_treated_as_real(self):
        """The socket emitter writes `meta["threadId"] = 0` when a chat note
        has no parent thread. The client coerces that away with `||`; a
        server comparing it literally would mute on the sentinel."""
        self._sub(self.user2)
        self._mute({"target_type": "thread", "target_id": "0"})
        act = self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=8,  # SURFACE_CHAT_NOTE
            meta={"noteId": 5, "threadId": 0},
            channel=False,
        )
        self._run([act]).assert_called_once()

    def test_open_laptop_does_not_silence_the_phone(self):
        """The bug this per-device split exists to fix.

        Two devices, laptop foreground. The laptop must stay quiet (it
        shows the in-app toast) while the phone still gets its push —
        under per-user presence, BOTH were suppressed, so leaving Genos
        open on a desktop meant the phone never notified at all.
        """
        laptop = self._sub(self.user2, device_id="laptop-abc")
        phone = self._sub(self.user2, device_id="phone-xyz")
        presence.mark_visible(self.user2.id, "laptop-abc")

        send = self._run([self._activity(self.user2)])

        send.assert_called_once()
        sent_to = send.call_args.kwargs["subscription_id"]
        self.assertEqual(sent_to, phone.id)
        self.assertNotEqual(sent_to, laptop.id)

    def test_all_devices_push_when_nothing_is_visible(self):
        self._sub(self.user2, device_id="laptop-abc")
        self._sub(self.user2, device_id="phone-xyz")
        send = self._run([self._activity(self.user2)])
        self.assertEqual(send.call_count, 2)

    def test_payload_carries_badge_count(self):
        self._sub(self.user2)
        send = self._run([self._activity(self.user2)])
        # Exact value depends on fixture state; what matters is that the
        # key is present and numeric so the SW can set the icon badge
        # instead of guessing with a local increment.
        self.assertIn("badge_count", send.call_args.kwargs["payload"])
        self.assertIsInstance(send.call_args.kwargs["payload"]["badge_count"], int)

    def test_no_subscription_no_send(self):
        self._run([self._activity(self.user2)]).assert_not_called()

    def test_reaction_pushes_with_emoji_title(self):
        # Reactions now push (they used to be dropped by the MENTION-only
        # filter — the gap this backbone closes).
        self._sub(self.user2)
        act = self._activity(self.user2, ActivityType.REACTION, meta={"emoji": "👍"})
        send = self._run([act])
        send.assert_called_once()
        self.assertEqual(send.call_args.kwargs["payload"]["title"], "Alice reacted 👍")
        self.assertEqual(send.call_args.kwargs["payload"]["tag"], f"reactions:{act.id}")

    def test_thread_reply_pushes(self):
        self._sub(self.user2)
        send = self._run([self._activity(self.user2, ActivityType.THREAD_REPLY)])
        send.assert_called_once()
        self.assertEqual(
            send.call_args.kwargs["payload"]["title"], "Alice replied in a thread"
        )

    def test_task_comment_reply_routes_to_task_comments_category(self):
        # A THREAD_REPLY whose mirror message carries metadata.taskCommentId
        # is a task comment, not a chat thread reply — distinct title +
        # gated under enable_task_comments (verified in ShouldPushTests).
        self._sub(self.user2)
        send = self._run(
            [
                self._activity(
                    self.user2,
                    ActivityType.THREAD_REPLY,
                    msg_metadata={"taskCommentId": 7},
                )
            ]
        )
        send.assert_called_once()
        self.assertEqual(
            send.call_args.kwargs["payload"]["title"], "Alice commented on a task"
        )

    def test_task_body_surface_mention_pushes(self):
        self._sub(self.user2)
        act = self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=5,  # SURFACE_TASK_BODY
            meta={"displayId": "PRG-12", "projectId": 3, "taskId": 12},
            channel=False,
        )
        send = self._run([act])
        send.assert_called_once()
        payload = send.call_args.kwargs["payload"]
        self.assertEqual(payload["title"], "Alice mentioned you in a task")
        self.assertEqual(payload["url"], "/workspace/tasks/project/3/task/12")

    def test_note_surface_mention_pushes(self):
        self._sub(self.user2)
        act = self._activity(
            self.user2,
            ActivityType.MENTION,
            surface_type=6,  # SURFACE_PERSONAL_NOTE
            meta={"noteId": 88},
            channel=False,
        )
        send = self._run([act])
        send.assert_called_once()
        payload = send.call_args.kwargs["payload"]
        self.assertEqual(payload["title"], "Alice mentioned you in a note")
        self.assertEqual(payload["url"], "/workspace/notes/my/88")

    def test_comment_participant_task_body_pushes(self):
        # The general-comment fan-out: a channel-less THREAD_REPLY tagged
        # `isCommentParticipant` on a task-body surface is "someone left a
        # comment on your task", routed to the `comments` category with the
        # task deep-link — NOT a chat thread reply. (Regression guard: an
        # earlier cut read `act.channel_id`, absent on these fixtures, so the
        # branch raised AttributeError and NOTHING pushed.)
        self._sub(self.user2)
        act = self._activity(
            self.user2,
            ActivityType.THREAD_REPLY,
            surface_type=5,  # SURFACE_TASK_BODY
            meta={"isCommentParticipant": True, "displayId": "PRG-12", "projectId": 3, "taskId": 12},
            channel=False,
        )
        send = self._run([act])
        send.assert_called_once()
        payload = send.call_args.kwargs["payload"]
        self.assertEqual(payload["title"], "Alice commented")
        self.assertEqual(payload["body"], "PRG-12")
        self.assertEqual(payload["url"], "/workspace/tasks/project/3/task/12")

    def test_comment_participant_note_pushes(self):
        # Same fan-out on a note surface: routes to `comments` with the note
        # deep-link and no body (notes carry no displayId).
        self._sub(self.user2)
        act = self._activity(
            self.user2,
            ActivityType.THREAD_REPLY,
            surface_type=6,  # SURFACE_PERSONAL_NOTE
            meta={"isCommentParticipant": True, "noteId": 88},
            channel=False,
        )
        send = self._run([act])
        send.assert_called_once()
        payload = send.call_args.kwargs["payload"]
        self.assertEqual(payload["title"], "Alice commented")
        self.assertEqual(payload["url"], "/workspace/notes/my/88")

    def test_thread_reply_without_participant_flag_is_plain_reply(self):
        # A channel-less THREAD_REPLY that is NOT the comment fan-out (no
        # `isCommentParticipant`) must fall through to the ordinary
        # thread-reply spec, not the `comments` branch — proves the guard is
        # the flag, not merely the absence of a channel.
        self._sub(self.user2)
        act = self._activity(
            self.user2,
            ActivityType.THREAD_REPLY,
            surface_type=5,
            meta={"taskId": 12},
            channel=False,
        )
        send = self._run([act])
        send.assert_called_once()
        self.assertEqual(
            send.call_args.kwargs["payload"]["title"], "Alice replied in a thread"
        )

    def test_category_override_suppresses_per_type(self):
        # Turning off only the reactions fine-category blocks reaction
        # pushes but not mentions.
        self._sub(self.user2)
        NotificationPreference.objects.create(
            user=self.user2, category_settings={"reactions": False}
        )
        self._run(
            [self._activity(self.user2, ActivityType.REACTION, meta={"emoji": "x"})]
        ).assert_not_called()
        self._run([self._activity(self.user2)]).assert_called_once()

    def test_push_disabled_recipient_no_send(self):
        self._sub(self.user2)
        NotificationPreference.objects.create(user=self.user2, push_enabled=False)
        self._run([self._activity(self.user2)]).assert_not_called()


class SurfaceMentionPushWiringTests(BaseAPITestCase):
    """Proves the WIRING the old tests missed: the surface-mention view
    actually schedules dispatch via `transaction.on_commit`. The prior
    suite called `dispatch_push_for_activities` directly, so a view that
    created activities but never reached dispatch passed — exactly the gap
    this fixes. `captureOnCommitCallbacks(execute=True)` runs the on_commit
    hooks that a plain TestCase transaction would otherwise swallow.
    """

    URL = "/api/v3/activities/surface/"

    def test_surface_post_schedules_push_for_new_mention(self):
        self.authenticate(self.user)
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_activities") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(
                    self.URL,
                    {
                        "surface_type": 5,
                        "team_id": str(self.team.team_id),
                        "entity_key": "task:101",
                        "newly_mentioned_user_ids": [str(self.user2.id)],
                        "meta": {"taskId": 101},
                    },
                    format="json",
                )
            self.assertEqual(resp.status_code, 201)
            disp.assert_called_once()
            acts = disp.call_args.args[0]
            self.assertEqual(len(acts), 1)
            self.assertEqual(str(acts[0].recipient_id), str(self.user2.id))
            self.assertEqual(acts[0].activity_type, ActivityType.MENTION)
            self.assertEqual(acts[0].surface_type, 5)

    def test_surface_resave_does_not_repush(self):
        # The idempotency fix: re-mentioning the same user on a body
        # re-save creates no new Activity, so dispatch must NOT fire again.
        self.authenticate(self.user)
        payload = {
            "surface_type": 5,
            "team_id": str(self.team.team_id),
            "entity_key": "task:202",
            "newly_mentioned_user_ids": [str(self.user2.id)],
            "meta": {"taskId": 202},
        }
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(self.URL, payload, format="json")  # first mention
        with mock.patch.object(webpush_dispatch, "dispatch_push_for_activities") as disp:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self.URL, payload, format="json")  # re-save
            self.assertEqual(resp.status_code, 201)
            disp.assert_not_called()
