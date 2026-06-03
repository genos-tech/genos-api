"""PR4a — activity-generation gaps closed:

1. @group mentions in a CHAT message are expanded to member ids and
   notified (chat path previously dropped group mentions entirely).
2. Thread replies fan out to ALL prior thread participants, not just the
   immediate parent's author.
3. Reacting to a TASK COMMENT creates a v3 REACTION activity (the legacy
   TaskCommentReactionFact alone wrote none, so it notified nobody).
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services.v3_activity import create_thread_reply_activity
from origin.tests.test_base import BaseAPITestCase
from origin.views.chat.message_views import _valid_mention_user_ids

User = get_user_model()


class ChatGroupMentionExpansionTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="u3gap", email="u3gap@e.com", password="pass12345"
        )
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )
        for u in (self.user, self.user2, self.user3):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self.group = MentionGroupMaster.objects.create(team=self.team, group_name="devs")
        for u in (self.user2, self.user3):
            MentionGroupMembers.objects.create(team=self.team, group=self.group, user=u)

    def _body(self):
        return [
            {
                "type": "paragraph",
                "content": [{"type": "mentionGroup", "props": {"groupId": self.group.group_id}}],
            }
        ]

    def test_group_mention_expands_to_channel_members(self):
        self.assertEqual(
            _valid_mention_user_ids(self.channel, self._body()),
            {str(self.user2.id), str(self.user3.id)},
        )

    def test_group_member_outside_channel_is_dropped(self):
        ChannelMember.objects.filter(channel=self.channel, user=self.user3).update(is_deleted=True)
        self.assertEqual(
            _valid_mention_user_ids(self.channel, self._body()), {str(self.user2.id)}
        )


class ThreadParticipantFanoutTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="u3thr", email="u3thr@e.com", password="pass12345"
        )
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="C", owner=self.user
        )

    def _msg(self, sender, seq, *, parent=None, root=None):
        return Message.objects.create(
            channel=self.channel,
            sender=sender,
            seq=seq,
            body={"text": "x"},
            body_text="x",
            parent=parent,
            thread_root=root,
            is_thread_reply=bool(parent),
        )

    def test_fans_out_to_root_author_and_prior_repliers(self):
        root = self._msg(self.user2, 1)
        self._msg(self.user3, 2, parent=root, root=root)  # prior reply by user3
        new_reply = self._msg(self.user, 3, parent=root, root=root)  # actor's reply
        rows = create_thread_reply_activity(reply=new_reply, parent=root, actor=self.user)
        self.assertEqual(
            {str(r.recipient_id) for r in rows},
            {str(self.user2.id), str(self.user3.id)},
        )
        self.assertTrue(all(r.activity_type == ActivityType.THREAD_REPLY for r in rows))


class TaskCommentReactionV3Tests(BaseAPITestCase):
    URL = "/api/v2/task/comment/reaction/"

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="GapsProj",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        # The PM channel is auto-created by the pm_channel signal on project
        # save — reuse it (a second PM channel would violate the 1:1 unique).
        self.channel = Channel.objects.get(
            project_id=self.project.project_id, kind=ChannelKind.PM
        )
        # The PM task header message the comment mirror threads under.
        header = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        # The task comment's v3 mirror message, authored by user2.
        self.mirror = Message.objects.create(
            channel=self.channel,
            sender=self.user2,
            seq=2,
            body={"text": "c"},
            body_text="comment text",
            task_id=self.task.task_id,
            metadata={"taskCommentId": 1},
            parent=header,
            thread_root=header,
            is_thread_reply=True,
        )

    def _react(self, sender):
        return self.client.post(
            self.URL,
            {
                "team_id": str(self.team.team_id),
                "task_id": str(self.task.task_id),
                "comment_id": 1,
                "reaction_emoji": "👍",
                "sender_id": str(sender.id),
            },
            format="json",
        )

    def test_reacting_to_comment_creates_v3_reaction_activity(self):
        self.authenticate(self.user)
        resp = self._react(self.user)
        self.assertEqual(resp.status_code, 201)
        act = Activity.objects.filter(
            activity_type=ActivityType.REACTION, message=self.mirror
        ).first()
        self.assertIsNotNone(act)
        self.assertEqual(str(act.recipient_id), str(self.user2.id))  # comment author
        self.assertEqual(act.meta.get("emoji"), "👍")

    def test_self_reaction_creates_no_activity(self):
        self.authenticate(self.user2)
        resp = self._react(self.user2)
        self.assertEqual(resp.status_code, 201)
        self.assertFalse(Activity.objects.filter(activity_type=ActivityType.REACTION).exists())
