"""Tests for the email-outbox enqueue (PR A2 of the email series): the
flag gate, per-category row writing at each of the four choke points, and
the two wiring traps — enqueue must run even when VAPID is unconfigured,
and must be deferred to commit.

Activity-path tests use REAL `Activity` rows (unlike `test_webpush.py`'s
SimpleNamespace pattern) because the outbox row carries a genuine FK to
the activity. The message path has no FK, so it keeps SimpleNamespace.
"""

from types import SimpleNamespace

from django.test import override_settings

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import (
    EmailNotificationEvent,
    NotificationPreference,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services.email_enqueue import (
    _email_spec,
    enqueue_email_for_inbox_item,
    enqueue_email_for_message,
    enqueue_email_to_user,
)
from origin.services.v3_activity import SURFACE_TASK_BODY
from origin.services.webpush_dispatch import (
    _push_spec,
    schedule_push_for_activities,
    schedule_push_to_user,
)
from origin.tests.test_base import BaseAPITestCase

NO_VAPID = {
    "WEBPUSH_VAPID_PRIVATE_KEY": "",
    "WEBPUSH_VAPID_PUBLIC_KEY": "",
}


class EnqueueTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The task `_mention_activity` is about, which its recipient (`user2`, the reporter) can
        # open: a recipient who cannot gets the minimal card (`test_nonmember_mention_card`).
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=self.user2,
            assignee=self.user2,
            title="T",
            status="WIP",
        )
        self.task_url = f"/workspace/tasks/project/{project.project_id}/task/{self.task.task_id}"

    def _mention_activity(self, **overrides):
        fields = {
            "team": self.team,
            "recipient": self.user2,
            "actor": self.user,
            "activity_type": ActivityType.MENTION,
            "surface_type": SURFACE_TASK_BODY,
            "meta": {
                "displayId": self.task.display_id,
                "projectId": self.task.project_id,
                "taskId": self.task.task_id,
            },
        }
        fields.update(overrides)
        return Activity.objects.create(**fields)

    def _rows(self):
        return list(EmailNotificationEvent.objects.order_by("ts_created_at"))


class FlagGateTests(EnqueueTestBase):
    def test_flag_off_by_default_writes_nothing(self):
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class ActivityEnqueueTests(EnqueueTestBase):
    def test_task_mention_writes_row_with_activity_fk(self):
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(str(row.user_id), str(self.user2.id))
        self.assertEqual(row.category, "mention_task")
        self.assertEqual(row.url, self.task_url)
        self.assertEqual(row.actor_name, "testuser")
        self.assertEqual(row.activity_id, act.id)
        self.assertIsNone(row.inbox_item_id)
        self.assertEqual(row.status, EmailNotificationEvent.STATUS_PENDING)

    @override_settings(**NO_VAPID)
    def test_enqueue_runs_even_with_vapid_unconfigured(self):
        # THE wiring trap: the push dispatcher early-returns without VAPID
        # keys; the email enqueue must live outside that guard or every
        # VAPID-less environment silently loses email.
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(len(self._rows()), 1)

    def test_enqueue_is_deferred_to_commit(self):
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            schedule_push_for_activities([act])
            self.assertEqual(self._rows(), [])
        for cb in callbacks:
            cb()
        self.assertEqual(len(self._rows()), 1)

    def test_email_disabled_pref_blocks_enqueue(self):
        NotificationPreference.objects.create(user=self.user2, email_enabled=False)
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])

    def test_task_assign_is_email_only(self):
        act = self._mention_activity(activity_type=ActivityType.TASK_ASSIGN, surface_type=None)
        # Push deliberately has no TASK_ASSIGN branch; email does.
        self.assertIsNone(_push_spec(act))
        spec = _email_spec(act)
        self.assertEqual(spec["category"], "task_assign")
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category, "task_assign")
        self.assertEqual(rows[0].url, self.task_url)


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class ActivityMuteTests(EnqueueTestBase):
    """Mute has to reach the email outbox too.

    The message path checked `is_chat_muted`; the ACTIVITY path checked
    nothing, so muting a chat/thread/task/note still wrote outbox rows for
    every mention, reaction and reply inside it. Email is the worse channel
    to leak on: the row outlives the moment and still sends later, so the
    user gets mail about a thing they muted, minutes after muting it.

    Both channels now share one predicate, which is the point — a mute that
    silences push but not email is barely a mute.
    """

    def test_muted_chat_blocks_the_activity_path(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Busy")
        NotificationPreference.objects.create(
            user=self.user2,
            muted_chats=[{"chat_type": 2, "chat_id": str(channel.id)}],
        )
        act = self._mention_activity(channel=channel, surface_type=None)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])

    def test_muting_another_chat_still_emails(self):
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Busy")
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Other")
        NotificationPreference.objects.create(
            user=self.user2,
            muted_chats=[{"chat_type": 2, "chat_id": str(other.id)}],
        )
        act = self._mention_activity(channel=channel, surface_type=None)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(len(self._rows()), 1)

    def test_muted_task_blocks_the_activity_path(self):
        NotificationPreference.objects.create(
            user=self.user2,
            muted_targets=[{"target_type": "task", "target_id": str(self.task.task_id)}],
        )
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])

    def test_muted_task_also_blocks_the_email_only_assign(self):
        """`task_assign` has no push branch at all — email is its ONLY
        channel, so if the gate were wired into the dispatcher instead of
        the shared predicate, this row would still be written."""
        NotificationPreference.objects.create(
            user=self.user2,
            muted_targets=[{"target_type": "task", "target_id": str(self.task.task_id)}],
        )
        act = self._mention_activity(activity_type=ActivityType.TASK_ASSIGN, surface_type=None)
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])

    def test_unmuted_task_still_emails(self):
        NotificationPreference.objects.create(
            user=self.user2,
            muted_targets=[{"target_type": "task", "target_id": "some-other-task"}],
        )
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(len(self._rows()), 1)

    def test_scoped_mute_uses_the_email_category_not_the_push_one(self):
        """The two channels name the same activity differently, so each
        passes its OWN category to the shared gate. This activity is
        `mention_task` on both, but the seam is real for others, and a scope
        evaluated against the wrong vocabulary silently never matches."""
        NotificationPreference.objects.create(
            user=self.user2,
            muted_targets=[
                {
                    "target_type": "task",
                    "target_id": str(self.task.task_id),
                    "categories": ["mention_task"],
                }
            ],
        )
        act = self._mention_activity()
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_for_activities([act])
        self.assertEqual(self._rows(), [])


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class InboxEnqueueTests(EnqueueTestBase):
    def _item(self, **overrides):
        fields = {
            "team": self.team,
            "sender": self.user,
            "receiver": self.user2,
            "item_type": 1,
        }
        fields.update(overrides)
        return InboxItems.objects.create(**fields)

    def test_inbox_item_writes_row(self):
        item = self._item()
        enqueue_email_for_inbox_item(item)
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row.category, "inbox")
        self.assertEqual(row.title, "testuser asked to join your team")
        self.assertEqual(row.url, "/workspace/inbox")
        self.assertEqual(row.inbox_item_id, item.item_id)
        self.assertIsNone(row.activity_id)

    def test_title_override(self):
        enqueue_email_for_inbox_item(self._item(), title="Your request was approved")
        self.assertEqual(self._rows()[0].title, "Your request was approved")

    def test_senderless_pr_signal_emails_its_own_title_and_text(self):
        item = self._item(
            sender=None,
            item_type=0,
            item_body={"title": "CI failing\non PR 5", "text": " Tests  failed "},
        )
        enqueue_email_for_inbox_item(item)
        row = self._rows()[0]
        self.assertEqual(row.title, "CI failing on PR 5")
        self.assertEqual(row.body, "Tests failed")
        self.assertEqual(row.actor_name, "Genos")

    def test_normal_message_with_sender_is_unchanged(self):
        item = self._item(item_type=0, item_body={"title": "x", "text": "y"})
        enqueue_email_for_inbox_item(item)
        row = self._rows()[0]
        self.assertEqual(row.title, "testuser sent you a message")
        self.assertEqual(row.body, "")
        self.assertEqual(row.actor_name, "testuser")

    def test_senderless_item_without_title_keeps_the_kind_wording(self):
        enqueue_email_for_inbox_item(self._item(sender=None, item_type=0, item_body={"text": "t"}))
        self.assertEqual(self._rows()[0].title, "Someone sent you a message")

    def test_receiverless_item_is_skipped(self):
        item = self._item()
        item.receiver = None
        item.save(update_fields=["receiver"])
        enqueue_email_for_inbox_item(item)
        self.assertEqual(self._rows(), [])


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class ToUserEnqueueTests(EnqueueTestBase):
    def test_writes_sourceless_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_to_user(
                recipient_id=self.user2.id,
                category="inbox",
                title="Your request to join was approved",
                url="/workspace/inbox",
            )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].activity_id)
        self.assertIsNone(rows[0].inbox_item_id)

    @override_settings(**NO_VAPID)
    def test_to_user_enqueue_survives_missing_vapid(self):
        # `schedule_push_to_user` has its own vapid guard INSIDE `_run` —
        # the email enqueue must run before it.
        with self.captureOnCommitCallbacks(execute=True):
            schedule_push_to_user(
                recipient_id=self.user2.id,
                category="inbox",
                title="t",
                url="/workspace/inbox",
            )
        self.assertEqual(len(self._rows()), 1)

    def test_default_off_category_writes_nothing(self):
        enqueue_email_to_user(
            recipient_id=self.user2.id,
            category="agent_run_done",
            title="t",
            url="/workspace/inbox",
        )
        self.assertEqual(self._rows(), [])


@override_settings(EMAIL_NOTIFICATIONS_ENABLED=True)
class MessageEnqueueTests(EnqueueTestBase):
    """The plain-`chats` path — default OFF for email, so rows appear only
    for explicit `email:chats` opt-ins. SimpleNamespace stands in for the
    message/channel because this path writes no source FK."""

    def _message(self):
        channel = SimpleNamespace(id="chan-1", kind=2, title="Design team")
        return SimpleNamespace(
            channel=channel,
            sender=self.user,
            body_text="hello there",
        )

    def test_default_off_writes_nothing(self):
        enqueue_email_for_message(self._message(), [self.user2.id])
        self.assertEqual(self._rows(), [])

    def test_opt_in_writes_row(self):
        NotificationPreference.objects.create(
            user=self.user2, category_settings={"email:chats": True}
        )
        enqueue_email_for_message(self._message(), [self.user2.id])
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].category, "chats")
        self.assertEqual(rows[0].title, "testuser in Design team")
        self.assertEqual(rows[0].body, "hello there")

    def test_muted_chat_blocks_even_with_opt_in(self):
        NotificationPreference.objects.create(
            user=self.user2,
            category_settings={"email:chats": True},
            muted_chats=[{"chat_type": 2, "chat_id": "chan-1"}],
        )
        enqueue_email_for_message(self._message(), [self.user2.id])
        self.assertEqual(self._rows(), [])
