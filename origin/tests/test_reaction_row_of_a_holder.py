"""A REACTION on a task comment, told to a commenter who can open the task but not its project
chat (MIP-1609, item 2 of the leftovers of MIP-1599).

The comment is mirrored into the project chat as a reply by its author, and a reaction on it
writes `Activity(type=REACTION)` for that author. When the author is the task's assignee or
reporter and is not on the project (or is the project's owner with no member row), they can open
the TASK and cannot open the CHAT. The "X commented" row and the comment @mention row follow the
task's rule since #889 and #896; the reaction row was still judged by the chat's rule, so:

  * the activity feed listed it as a tombstone (the client dropped the row it had),
  * the web push and the notification email linked to the project chat, which answers 404.

Now it follows the same rule through the same function (`task_reader_pairs`, reached by
`COMMENT_ROW_TYPES`): kept in the feed, linked to the task, and only while they can still open
the task. Nothing else of the chat is opened by that.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, Message
from origin.models.common.notification_models import (
    EmailNotificationEvent,
    NotificationPreference,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services.activity_minimal_card import is_comment_row, task_links
from origin.tests.test_nonmember_mention_card import (
    COMMENT,
    COMMENT_TEXT,
    FEED,
    Cards,
    comment_body,
)

User = get_user_model()

REACTION = "/api/v2/task/comment/reaction/"


class ReactionRowBase(Cards):
    """`self.assignee` (the assignee of `self.held`) is not on the project. They comment on
    `self.held`; `self.user`, a project member, reacts."""

    def setUp(self):
        super().setUp()
        self.aid = str(self.assignee.id)
        self.url = f"/workspace/tasks/project/{self.project.project_id}/task/{self.held.task_id}"
        self.mirror, self.pushes, self.emails = self.react_to_comment_of(self.assignee, self.held)

    def react_to_comment_of(self, author, task, emoji="👍"):
        """`author` comments on `task` (no mention), then `self.user` reacts to it: the mirror
        message, `{recipient id: push spec}`, `{recipient id: [email rows]}` of the reaction."""
        self.authenticate(author)
        res = self.client.post(
            COMMENT,
            {"task_id": str(task.task_id), "comment_body": comment_body()},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        comment = TaskComments.objects.filter(task=task, sender=author).order_by("-comment_id")[0]
        mirror = Message.objects.get(
            task_id=task.task_id, metadata__taskCommentId=comment.comment_id
        )
        EmailNotificationEvent.objects.all().delete()
        # reactions are not emailed unless the member asks (`email_gating._EMAIL_DEFAULTS`)
        NotificationPreference.objects.update_or_create(
            user=author, defaults={"category_settings": {"email:reactions": True}}
        )
        self.authenticate(self.user)

        def call():
            return self.client.post(
                REACTION,
                {
                    "task_id": str(task.task_id),
                    "comment_id": comment.comment_id,
                    "reaction_emoji": emoji,
                },
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        return mirror, pushes, emails

    def row(self, recipient):
        return Activity.objects.get(
            recipient=recipient, activity_type=ActivityType.REACTION, message=self.mirror
        )

    def feed(self, user):
        self.authenticate(user)
        res = self.client.get(FEED, {"team_id": self.team_id})
        self.assertEqual(res.status_code, 200)
        return res.json()


class TheCommenterWhoIsNotOnTheProject(ReactionRowBase):
    def test_the_row_is_written_for_them_as_before(self):
        self.assertEqual(self.row(self.assignee).actor_id, self.user.id)
        self.assertEqual(
            Activity.objects.filter(activity_type=ActivityType.REACTION).count(),
            1,
        )

    def test_it_is_a_comment_row(self):
        self.assertTrue(is_comment_row(self.row(self.assignee)))

    def test_the_feed_keeps_the_row_and_lists_no_tombstone(self):
        got = self.feed(self.assignee)

        (row,) = got["activities"]
        self.assertEqual(row["activityType"], int(ActivityType.REACTION))
        self.assertEqual(row["message"]["taskId"], self.held.task_id)
        self.assertEqual(got["tombstones"], [])

    def test_the_push_and_the_email_link_to_the_task_not_to_a_chat_that_answers_404(self):
        self.assertEqual(self.pushes[self.aid]["url"], self.url)
        (mail,) = self.emails[self.aid]
        self.assertEqual(mail.url, self.url)
        # what else they say is as before: the group, the title, the comment's own text
        self.assertEqual(self.pushes[self.aid]["category"], "reactions")
        self.assertEqual(self.pushes[self.aid]["title"], f"{self.user.username} reacted 👍")
        self.assertIn(COMMENT_TEXT, self.pushes[self.aid]["body"])

    def test_the_daily_digest_line_links_to_the_task(self):
        from origin.management.commands.email_digest import Command

        _, lines = Command()._collect(self.aid, None, timezone.now() + timedelta(seconds=1))

        reaction = [x for x in lines if "reacted" in x["title"]]
        self.assertEqual(len(reaction), 1)
        self.assertTrue(reaction[0]["url"].endswith(self.url))

    def test_the_chat_and_its_messages_stay_closed_to_them(self):
        self.authenticate(self.assignee)

        chat = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/")
        one = self.client.get(f"/api/v3/messages/{self.mirror.id}/")

        for res in (chat, one):
            self.assertIn(res.status_code, (403, 404))
            self.assertNotIn(COMMENT_TEXT, res.content.decode())

    def test_a_project_member_keeps_the_link_to_the_chat(self):
        mine = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.colleague,
            title="Mine",
            status="WIP",
        )
        # the colleague is ON the project: their row's link is the chat's, as before
        _, pushes, emails = self.react_to_comment_of(self.colleague, mine)

        cid = str(self.colleague.id)
        self.assertIn("/workspace/chat/pm/", pushes[cid]["url"])
        self.assertIn("/workspace/chat/pm/", emails[cid][0].url)

    def test_a_reaction_on_a_message_that_is_not_a_task_comment_stays_out(self):
        """The task rule reaches the comment mirror and nothing else of the project chat: a
        reply under the task's card is the chat's content, whoever wrote it."""
        card = Message.objects.get(
            channel=self.channel, task_id=self.held.task_id, thread_root__isnull=True
        )
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.assignee,
            seq=70,
            body=[],
            body_text="a reply on the card",
            task_id=self.held.task_id,
            thread_root=card,
            is_thread_reply=True,
        )
        row = Activity.objects.create(
            team=self.team,
            recipient=self.assignee,
            actor=self.user,
            activity_type=ActivityType.REACTION,
            channel=self.channel,
            message=reply,
            meta={"emoji": "🎉"},
        )

        self.assertFalse(is_comment_row(row))
        self.assertEqual(task_links([row]), {})
        got = self.feed(self.assignee)
        self.assertEqual({a["messageId"] for a in got["activities"]}, {str(self.mirror.id)})
        self.assertEqual(got["tombstones"], [str(row.id)])

    def test_it_is_judged_when_the_feed_is_read(self):
        TaskMaster.objects.filter(task_id=self.held.task_id).update(assignee=self.user)

        got = self.feed(self.assignee)

        self.assertEqual(got["activities"], [])
        self.assertEqual(len(got["tombstones"]), 1)

    def test_a_deleted_task_gives_no_row(self):
        TaskMaster.objects.filter(task_id=self.held.task_id).update(is_deleted=True)

        got = self.feed(self.assignee)

        self.assertEqual(got["activities"], [])
        self.assertEqual(len(got["tombstones"]), 1)

    def test_someone_who_left_the_team_is_given_neither_row_nor_link(self):
        row = self.row(self.assignee)
        self.assertEqual(task_links([row]), {row.id: self.url})
        TeamMembers.objects.filter(team=self.team, attendee=self.assignee).update(is_deleted=True)

        self.assertEqual(task_links([row]), {})
        got = self.feed(self.assignee)
        self.assertEqual(got["activities"], [])
        self.assertEqual(len(got["tombstones"]), 1)

    def test_someone_who_lost_the_task_is_given_no_link_to_it(self):
        row = self.row(self.assignee)
        TaskMaster.objects.filter(task_id=self.held.task_id).update(assignee=self.user)

        self.assertEqual(task_links([row]), {})

    def test_a_stranger_gains_nothing(self):
        """The rule is the task's: a team member who is not on the project and holds nothing is
        given no link, and a reaction row addressed to them (it cannot be written by the app:
        the recipient is the comment's author) would still be a tombstone."""
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.REACTION,
            channel=self.channel,
            message=self.mirror,
            meta={"emoji": "👍"},
        )

        self.assertEqual(task_links([row]), {})
        got = self.feed(self.user2)
        self.assertEqual(got["activities"], [])
        self.assertEqual(got["tombstones"], [str(row.id)])

    def test_the_feed_and_the_links_cost_the_same_for_one_such_row_as_for_many(self):
        def feed_queries():
            self.authenticate(self.assignee)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(FEED, {"team_id": self.team_id}).status_code, 200)
            return len(ctx)

        one = list(Activity.objects.filter(recipient=self.assignee).select_related("message"))
        before = feed_queries()
        for emoji in ("🎉", "🙏", "👀", "🔥"):
            Activity.objects.create(
                team=self.team,
                recipient=self.assignee,
                actor=self.user,
                activity_type=ActivityType.REACTION,
                channel=self.channel,
                message=self.mirror,
                meta={"emoji": emoji},
            )
        many = list(Activity.objects.filter(recipient=self.assignee).select_related("message"))

        with CaptureQueriesContext(connection) as small:
            task_links(one)
        with CaptureQueriesContext(connection) as large:
            got = task_links(many)

        self.assertEqual((len(one), len(many), len(got)), (1, 5, 5))
        self.assertEqual(len(small), len(large))
        self.assertEqual(len(self.feed(self.assignee)["activities"]), 5)
        self.assertEqual(feed_queries(), before)


class TheProjectOwnerWithNoMemberRow(ReactionRowBase):
    """The owner opens the project's tasks and not its chat (a chat is read by a member row)."""

    def test_the_owner_is_given_the_task_link_and_the_row(self):
        owner = User.objects.create_user(
            username="powner", email="powner@m1609.test", password="pass12345"
        )
        TeamMembers.objects.create(team=self.team, attendee=owner)
        ProjectMaster.objects.filter(pk=self.project.pk).update(owner=owner)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=owner).exists()
        )

        mirror, pushes, emails = self.react_to_comment_of(owner, self.held)

        oid = str(owner.id)
        self.assertEqual(pushes[oid]["url"], self.url)
        self.assertEqual(emails[oid][0].url, self.url)
        got = self.feed(owner)
        self.assertEqual([a["messageId"] for a in got["activities"]], [str(mirror.id)])
        self.assertEqual(got["tombstones"], [])
