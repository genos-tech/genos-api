"""Handing someone an EXISTING task tells them (MIP-1461).

Creating a task tells its assignee because the "task created" card @-mentions them.
Reassigning later raised nothing, in the app or over MCP: the browser only rewrote the card
and `assign_task` just saved a column. `notify_task_assigned` is the one rule, called from the
app's task save, `assign_task` and `update_tasks_bulk`.
"""

from unittest import mock

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind, Message
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.assign_task import ASSIGN_TASK
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services import task_assignment_notice, v3_activity, webpush_dispatch

from .test_base import BaseAPITestCase


class AssignmentNoticeBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        self.user3 = CustomUser.objects.create_user(
            email="third@example.com", username="thirduser", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        for member in (self.user, self.user2, self.user3):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.channel = Channel.objects.get(kind=ChannelKind.PM, legacy_chat_id=self.project.pk)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        # An unassigned task with its card, as MCP or the app makes one.
        out = CREATE_TASK.run({"title": "Ship it", "project_id": self.project.project_id}, self.ctx)
        self.task = TaskMaster.objects.get(task_id=out["task_id"])
        self.card = Message.objects.get(channel=self.channel, task_id=self.task.task_id)

    def _assign(self, user_id):
        return ASSIGN_TASK.run({"task_id": self.task.task_id, "assignee_id": user_id}, self.ctx)

    def _told(self, user):
        return Activity.objects.filter(recipient=user, activity_type=ActivityType.MENTION)


class AssignTaskNoticeTests(AssignmentNoticeBase):
    def test_the_new_assignee_is_told_on_the_tasks_card(self):
        self._assign(str(self.user2.id))

        (activity,) = self._told(self.user2)
        self.assertEqual(activity.message_id, self.card.id)
        self.assertEqual(activity.actor_id, self.user.id)
        self.assertEqual(activity.meta, {"taskAssign": True})
        self.assertEqual(Message.objects.filter(task_id=self.task.task_id).count(), 1)

    def test_a_reassignment_tells_the_new_person_not_the_old_one(self):
        self._assign(str(self.user2.id))
        self._assign(str(self.user3.id))

        self.assertEqual(self._told(self.user3).count(), 1)
        self.assertEqual(self._told(self.user2).count(), 1)  # only the first assignment

    def test_the_new_assignees_open_app_and_phone_are_told_at_once(self):
        with (
            mock.patch.object(task_assignment_notice.realtime_push, "push_activity") as live,
            mock.patch.object(task_assignment_notice, "schedule_push_for_activities") as push,
        ):
            self._assign(str(self.user2.id))

        (activity,) = self._told(self.user2)
        live.assert_called_once_with(activity)
        push.assert_called_once_with([activity])

    def test_the_push_says_assigned_not_mentioned(self):
        self._assign(str(self.user2.id))
        (activity,) = self._told(self.user2)

        spec = webpush_dispatch._push_spec(activity)

        self.assertEqual(spec["title"], f"{self.user.username} assigned you a task")
        self.assertEqual(spec["category"], "mention_chat")  # no new push category
        self.assertEqual(spec["body"], "Ship it")

    def test_nobody_is_told_for_assigning_yourself_unassigning_or_no_change(self):
        self._assign(str(self.user.id))  # yourself
        self._assign(str(self.user2.id))
        Activity.objects.all().delete()
        self._assign(str(self.user2.id))  # same person again
        self._assign(None)  # unassign

        self.assertFalse(Activity.objects.exists())

    def test_someone_who_cannot_open_the_chat_is_not_told(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user3).delete()
        # Still on the team, so the tool lets the assignment through.

        self._assign(str(self.user3.id))

        self.assertFalse(self._told(self.user3).exists())
        self.assertEqual(self.task_assignee(), self.user3.id)

    def test_someone_reading_that_chat_right_now_is_not_told(self):
        with mock.patch.object(v3_activity, "_drop_active_viewers", return_value=[]):
            self._assign(str(self.user2.id))

        self.assertFalse(self._told(self.user2).exists())

    def test_a_task_with_no_card_gets_one_that_mentions_the_new_assignee(self):
        Message.objects.filter(pk=self.card.pk).delete()

        self._assign(str(self.user2.id))

        (card,) = Message.objects.filter(channel=self.channel, task_id=self.task.task_id)
        (activity,) = self._told(self.user2)
        self.assertEqual(activity.message_id, card.id)

    def test_assigning_yourself_a_task_with_no_card_posts_no_card(self):
        Message.objects.filter(pk=self.card.pk).delete()

        self._assign(str(self.user.id))

        self.assertFalse(Message.objects.filter(task_id=self.task.task_id).exists())
        self.assertFalse(Activity.objects.exists())

    def test_a_failing_notice_never_undoes_the_assignment(self):
        with mock.patch.object(
            v3_activity, "create_task_assign_activity", side_effect=RuntimeError("boom")
        ):
            out = self._assign(str(self.user2.id))

        self.assertEqual(out["assignee_id"], str(self.user2.id))
        self.assertEqual(self.task_assignee(), self.user2.id)

    def test_a_milestones_backing_task_is_left_to_the_milestone(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_milestone=True, assignee=self.user2)

        self.assertEqual(
            task_assignment_notice.notify_task_assigned(
                self.task, actor_id=self.user.id, previous_assignee_id=None
            ),
            [],
        )

    def task_assignee(self):
        return TaskMaster.objects.get(pk=self.task.pk).assignee_id


class BulkNoticeTests(AssignmentNoticeBase):
    def setUp(self):
        super().setUp()
        out = CREATE_TASK.run(
            {
                "title": "Second",
                "project_id": self.project.project_id,
                "assignee_id": str(self.user2.id),
            },
            self.ctx,
        )
        self.second = TaskMaster.objects.get(task_id=out["task_id"])
        Activity.objects.all().delete()

    def _bulk(self, *rows):
        return UPDATE_TASKS_BULK.run(
            {"updates": [{"rationale": "organise", **row} for row in rows]}, self.ctx
        )

    def test_each_row_that_changed_its_assignee_tells_that_person_once(self):
        self._bulk(
            {"task_id": self.task.task_id, "assignee_id": str(self.user3.id)},
            {"task_id": self.second.task_id, "assignee_id": str(self.user3.id)},
        )

        self.assertEqual(self._told(self.user3).count(), 2)

    def test_a_row_that_does_not_change_the_assignee_tells_nobody(self):
        self._bulk(
            {"task_id": self.second.task_id, "assignee_id": str(self.user2.id)},  # unchanged
            {"task_id": self.task.task_id, "priority": "High"},  # other field
        )

        self.assertFalse(Activity.objects.exists())

    def test_a_refused_batch_tells_nobody(self):
        from origin.search_engine.agent.tools import ToolError

        with self.assertRaises(ToolError):
            self._bulk(
                {"task_id": self.task.task_id, "assignee_id": str(self.user3.id)},
                {"task_id": self.second.task_id},  # no change given: refuses the whole batch
            )

        self.assertFalse(Activity.objects.exists())


class AppSaveNoticeTests(AssignmentNoticeBase):
    def _put(self, task, **payload):
        self.authenticate()
        res = self.client.put("/api/v2/task/", {"task_id": task.task_id, **payload}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def test_reassigning_in_the_app_tells_the_new_assignee(self):
        self._put(self.task, assignee=str(self.user2.id))

        (activity,) = self._told(self.user2)
        self.assertEqual(activity.message_id, self.card.id)
        self.assertEqual(activity.actor_id, self.user.id)

    def test_an_edit_that_leaves_the_assignee_alone_tells_nobody(self):
        self._put(self.task, assignee=str(self.user2.id))
        Activity.objects.all().delete()

        self._put(self.task, title="Renamed", assignee=str(self.user2.id))

        self.assertFalse(Activity.objects.exists())

    def test_the_create_forms_finalising_save_is_left_to_the_browsers_own_card(self):
        """The create form POSTs a scaffold row, then PUTs the real content over it (assignee
        none -> chosen) and the browser posts the card itself. Telling them here too would ping
        them twice and post a second card."""
        scaffold = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="",
            status="Open",
            is_init_task=True,
        )
        before = Message.objects.count()

        self._put(scaffold, title="Real", assignee=str(self.user2.id), is_init_task=False)

        self.assertEqual(TaskMaster.objects.get(pk=scaffold.pk).assignee_id, self.user2.id)
        self.assertFalse(Activity.objects.exists())
        self.assertEqual(Message.objects.count(), before)


class AssignActivityTests(AssignmentNoticeBase):
    """`create_task_assign_activity` on its own: its guards hold without the service's."""

    def _make(self, recipient, message=None, actor=None):
        return v3_activity.create_task_assign_activity(
            message=message or self.card, recipient_id=recipient.id, actor=actor or self.user
        )

    def test_it_writes_one_flagged_mention_for_someone_else(self):
        (row,) = self._make(self.user2)
        row.refresh_from_db()  # the row as stored, not as built (ids are strings in memory)

        self.assertEqual(
            (row.recipient_id, row.actor_id, row.message_id, row.meta),
            (self.user2.id, self.user.id, self.card.id, {"taskAssign": True}),
        )

    def test_it_never_tells_the_actor(self):
        self.assertEqual(self._make(self.user), [])

    def test_it_only_speaks_for_the_top_level_task_card(self):
        reply = Message.objects.create(
            channel=self.channel,
            seq=99,
            sender=self.user,
            body=[],
            body_text="re",
            task=self.task,
            parent=self.card,
            thread_root=self.card,
            is_thread_reply=True,
        )
        plain = Message.objects.create(
            channel=self.channel, seq=100, sender=self.user, body=[], body_text="hi"
        )

        self.assertEqual(self._make(self.user2, message=reply), [])
        self.assertEqual(self._make(self.user2, message=plain), [])

    def test_it_skips_someone_outside_the_channel(self):
        outsider = CustomUser.objects.create_user(
            email="out@example.com", username="outsider", password="x"
        )

        self.assertEqual(self._make(outsider), [])
