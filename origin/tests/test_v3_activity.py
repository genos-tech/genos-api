"""Tests for `origin/services/v3_activity.py` — the Activity-feed producer.

Covers the four previously-untested producers:
  - create_surface_mention_activities: channel-less surface mentions
    (task body / notes) — member validation, delta create/delete,
    idempotent re-save, and the task displayId backfill.
  - create_self_assign_activity: the self-assigned-task MENTION row.
  - create_thread_reply_activity: THREAD_REPLY to the parent's sender.
  - create_reaction_activity: REACTION to the message sender.

All four are DB-only (no OpenSearch / LLM), so they run against the
`BaseAPITestCase` Postgres fixtures with no external mocking.
"""

import uuid

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    Message,
)
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services.v3_activity import (
    SURFACE_TASK_BODY,
    SURFACE_TASK_NOTE,
    create_reaction_activity,
    create_self_assign_activity,
    create_surface_mention_activities,
    create_thread_reply_activity,
)
from origin.tests.test_base import BaseAPITestCase


class CreateSurfaceMentionActivitiesTests(BaseAPITestCase):
    """Channel-less surface mentions (task body + the three note types)."""

    def _surface(self, **overrides):
        kw = dict(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=SURFACE_TASK_BODY,
            entity_key="task:42",
            newly_mentioned_user_ids=[str(self.user2.id)],
        )
        kw.update(overrides)
        return create_surface_mention_activities(**kw)

    def test_creates_rows_for_valid_team_members(self):
        rows = self._surface(newly_mentioned_user_ids=[str(self.user.id), str(self.user2.id)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(Activity.objects.count(), 2)
        a = Activity.objects.get(recipient_id=self.user2.id)
        self.assertEqual(a.activity_type, ActivityType.MENTION)
        self.assertEqual(a.surface_type, SURFACE_TASK_BODY)
        # Surface mentions are channel-less / message-less.
        self.assertIsNone(a.channel)
        self.assertIsNone(a.message)
        # Unlike create_mention_activities, the actor is NOT skipped
        # (tagging yourself in a task body is a deliberate reminder).
        self.assertTrue(Activity.objects.filter(recipient_id=self.user.id).exists())

    def test_skips_non_team_member(self):
        # A recipient id that isn't an active team member is dropped
        # (the FK would otherwise 500 the request).
        stranger = str(uuid.uuid4())
        rows = self._surface(newly_mentioned_user_ids=[str(self.user.id), stranger])
        self.assertEqual(len(rows), 1)
        self.assertEqual(str(rows[0].recipient_id), str(self.user.id))

    def test_empty_targets_returns_empty(self):
        self.assertEqual(self._surface(newly_mentioned_user_ids=[]), [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_all_targets_non_members_returns_empty(self):
        # Non-empty input but every id fails the team-membership filter ->
        # no rows survive -> early return before bulk_create.
        rows = self._surface(newly_mentioned_user_ids=[str(uuid.uuid4()), str(uuid.uuid4())])
        self.assertEqual(rows, [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_idempotent_resave_does_not_duplicate(self):
        # Deterministic (surface, entity, recipient) PK -> a re-save
        # collapses onto the same row via ignore_conflicts.
        self._surface(entity_key="task:9", newly_mentioned_user_ids=[str(self.user.id)])
        self._surface(entity_key="task:9", newly_mentioned_user_ids=[str(self.user.id)])
        self.assertEqual(Activity.objects.count(), 1)

    def test_removed_user_ids_are_deleted(self):
        self._surface(entity_key="task:5", newly_mentioned_user_ids=[str(self.user.id)])
        self.assertEqual(Activity.objects.count(), 1)
        # newly=[] + removed=[user] -> the row is deleted by deterministic key.
        result = self._surface(
            entity_key="task:5",
            newly_mentioned_user_ids=[],
            removed_user_ids=[str(self.user.id)],
        )
        self.assertEqual(result, [])
        self.assertEqual(Activity.objects.count(), 0)

    def test_display_id_backfilled_from_task(self):
        # meta carries the task FK but not the display id; the producer
        # resolves "<code>-<n>" at this convergence point.
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Surface Proj",
            owner=self.user,
            project_system_user=self.user,
            code="SRF",
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            assignee=self.user,
            reporter=self.user,
            title="T",
            status="Open",
            project_task_number=7,
        )
        self._surface(
            surface_type=SURFACE_TASK_NOTE,
            entity_key="note:2:55",
            newly_mentioned_user_ids=[str(self.user.id)],
            meta={"taskId": task.task_id},
        )
        row = Activity.objects.get(recipient_id=self.user.id)
        self.assertEqual(row.meta["displayId"], task.display_id)


class CreateSelfAssignActivityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Self Assign Proj",
            owner=self.user,
            project_system_user=self.user,
            code="SA",
        )

    def _task(self, assignee):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=assignee,
            reporter=self.user,
            title="T",
            status="Open",
        )

    def _msg(self, task, *, is_thread_reply=False, seq=1):
        return Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=seq,
            body={"text": "x"},
            body_text="x",
            task=task,
            is_thread_reply=is_thread_reply,
        )

    def test_self_assigned_task_creates_mention(self):
        # assignee == actor -> one MENTION row flagged taskAssign.
        msg = self._msg(self._task(self.user))
        rows = create_self_assign_activity(message=msg, actor=self.user)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activity_type, ActivityType.MENTION)
        self.assertEqual(str(rows[0].recipient_id), str(self.user.id))
        self.assertTrue(rows[0].meta.get("taskAssign"))

    def test_assigned_to_other_is_noop(self):
        # assignee != actor -> handled by the normal mention fan-out, not here.
        msg = self._msg(self._task(self.user2))
        self.assertEqual(create_self_assign_activity(message=msg, actor=self.user), [])

    def test_thread_reply_message_is_noop(self):
        # A thread reply requires a thread_root (DB check constraint), so
        # build a root first. Even carrying a task, a reply is skipped —
        # self-assign only fires on the top-level task-card message.
        task = self._task(self.user)
        root = self._msg(task, seq=1)
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body={"text": "x"},
            body_text="x",
            task=task,
            is_thread_reply=True,
            thread_root=root,
        )
        self.assertEqual(create_self_assign_activity(message=reply, actor=self.user), [])

    def test_message_without_task_is_noop(self):
        msg = Message.objects.create(
            channel=self.channel, sender=self.user, seq=99, body={"text": "x"}, body_text="x"
        )
        self.assertEqual(create_self_assign_activity(message=msg, actor=self.user), [])


class CreateThreadReplyActivityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )

    def _msg(self, sender, seq):
        return Message.objects.create(
            channel=self.channel, sender=sender, seq=seq, body={"text": "x"}, body_text="x"
        )

    def test_creates_thread_reply_for_parent_sender(self):
        parent = self._msg(self.user2, 1)  # parent authored by user2
        reply = self._msg(self.user, 2)  # reply authored by user
        rows = create_thread_reply_activity(reply=reply, parent=parent, actor=self.user)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activity_type, ActivityType.THREAD_REPLY)
        self.assertEqual(str(rows[0].recipient_id), str(self.user2.id))
        self.assertEqual(rows[0].meta["parent_message_id"], str(parent.id))

    def test_self_reply_is_noop(self):
        # Replying to your own thread root pings nobody.
        parent = self._msg(self.user, 1)
        reply = self._msg(self.user, 2)
        self.assertEqual(create_thread_reply_activity(reply=reply, parent=parent, actor=self.user), [])

    def test_parent_none_is_noop(self):
        reply = self._msg(self.user, 1)
        self.assertEqual(create_thread_reply_activity(reply=reply, parent=None, actor=self.user), [])

    def test_parent_with_hard_deleted_sender_is_noop(self):
        parent = self._msg(self.user2, 1)
        Message.objects.filter(pk=parent.pk).update(sender=None)  # sender hard-deleted
        parent.refresh_from_db()
        reply = self._msg(self.user, 2)
        self.assertEqual(create_thread_reply_activity(reply=reply, parent=parent, actor=self.user), [])


class CreateReactionActivityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )

    def _msg(self, sender, seq=1):
        return Message.objects.create(
            channel=self.channel, sender=sender, seq=seq, body={"text": "x"}, body_text="x"
        )

    def test_creates_reaction_for_message_sender(self):
        msg = self._msg(self.user2)  # message authored by user2
        rows = create_reaction_activity(message=msg, emoji="👍", actor=self.user)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].activity_type, ActivityType.REACTION)
        self.assertEqual(str(rows[0].recipient_id), str(self.user2.id))
        self.assertEqual(rows[0].meta["emoji"], "👍")

    def test_self_reaction_is_noop(self):
        msg = self._msg(self.user)
        self.assertEqual(create_reaction_activity(message=msg, emoji="👍", actor=self.user), [])

    def test_message_with_hard_deleted_sender_is_noop(self):
        msg = self._msg(self.user2)
        Message.objects.filter(pk=msg.pk).update(sender=None)
        msg.refresh_from_db()
        self.assertEqual(create_reaction_activity(message=msg, emoji="👍", actor=self.user), [])
