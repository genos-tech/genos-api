"""The agent's `add_comment` must notify the same people the REST
endpoint does.

Adding a comment is two halves: the `TaskComments` row, and the fan-out
that tells anyone about it — the v3 thread mirror, the mention and
participant activities, the web push. The tool only ever did the first
half, so a comment it wrote existed on the task but was **missing from
the PM thread entirely and notified nobody**; the assignee found out by
opening the task. That is the failure mode these tests pin.

They are written as *parity* tests rather than re-asserting the expected
fan-out: the property that matters is "both callers mean the same thing
by 'a comment was added'", and a parity assertion keeps holding when the
fan-out itself changes.
"""

from unittest.mock import patch

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind, Message
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT

from .test_base import BaseAPITestCase


class AgentCommentFanoutTests(BaseAPITestCase):
    REST_URL = "/api/v2/task/comment/"

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        # user2 is the assignee — the person a PR-link comment is for.
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user2,
            title="Implementation",
            status="WIP",
        )
        # The PM channel is auto-created by the pm_channel signal on
        # project save; the mirror threads under the task header message.
        self.channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _tool_comment(self, text="See https://github.com/org/repo/pull/1"):
        return ADD_COMMENT.run({"task_id": self.task.task_id, "body_text": text}, self.ctx)

    def _mirrors(self):
        return Message.objects.filter(task_id=self.task.task_id, is_thread_reply=True)

    # ---- the row still gets written, exactly as before ----

    def test_the_comment_row_is_still_created(self):
        result = self._tool_comment()
        self.assertEqual(result["comment_id"], 1)
        self.assertTrue(TaskComments.objects.filter(task=self.task, comment_id=1).exists())

    # ---- the half that was missing ----

    def test_the_comment_appears_in_the_v3_pm_thread(self):
        self._tool_comment()
        mirror = self._mirrors().first()
        self.assertIsNotNone(mirror, "no v3 mirror — the comment is invisible in the PM thread")
        self.assertEqual(mirror.metadata.get("taskCommentId"), 1)

    def test_the_assignee_is_notified(self):
        """The point of the whole fix: your PR link reaches someone."""
        self._tool_comment()
        recipients = {
            str(a.recipient_id)
            for a in Activity.objects.filter(activity_type=ActivityType.THREAD_REPLY)
        }
        self.assertIn(str(self.user2.id), recipients)

    def test_the_commenter_does_not_notify_themselves(self):
        self._tool_comment()
        recipients = {str(a.recipient_id) for a in Activity.objects.all()}
        self.assertNotIn(str(self.user.id), recipients)

    # ---- parity with the REST endpoint ----

    def test_tool_and_rest_produce_the_same_notifications(self):
        self.authenticate(self.user)
        resp = self.client.post(
            self.REST_URL,
            {
                "task_id": str(self.task.task_id),
                "sender_id": str(self.user.id),
                "comment_body": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "via rest"}]}
                ],
            },
            format="json",
        )
        self.assertEqual(resp.status_code, 201)
        rest_shape = {(a.activity_type, str(a.recipient_id)) for a in Activity.objects.all()}
        rest_mirrors = self._mirrors().count()
        Activity.objects.all().delete()

        self._tool_comment("via tool")

        tool_shape = {(a.activity_type, str(a.recipient_id)) for a in Activity.objects.all()}
        self.assertEqual(tool_shape, rest_shape)
        self.assertEqual(self._mirrors().count(), rest_mirrors + 1)

    # ---- the fan-out is an observer, never a gate ----

    def test_a_broken_fanout_does_not_lose_the_comment(self):
        """The comment is the source of truth; notifying is an observer.
        A downstream failure must leave the saved row and the caller's
        result untouched."""
        with patch(
            "origin.services.task_comment_fanout.unified_writer.write_task_comment_as_thread_reply",
            side_effect=RuntimeError("mirror exploded"),
        ):
            result = self._tool_comment()
        self.assertEqual(result["comment_id"], 1)
        self.assertTrue(TaskComments.objects.filter(task=self.task, comment_id=1).exists())
