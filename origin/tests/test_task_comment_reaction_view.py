"""The comment reaction route's own guards (MIP-1582).

`/api/v2/task/comment/reaction/` stored whatever it was sent: a repeat POST made a second row (and
the DELETE that followed then found two and answered 500), a reaction could name a comment that
does not exist, the emoji was an unbounded string, and the row's team was the one the BODY named.

What these pin, for the app (the sockets handler is its only caller):

  * a repeat POST is a no-op: one row, 200 with that row, and nobody is told a second time;
  * DELETE takes every copy of the caller's reaction (rows that already doubled up) and never 500s;
  * a comment that is missing, deleted or another task's reads as missing (404), the tool's rule;
  * the emoji is the tools' rule (`task_comment_reactions.check_emoji`), so both writers agree;
  * the row's team is the TASK's team, whatever `team_id` the body carries (or leaves out).
"""

from unittest import mock
from urllib.parse import urlencode

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.services import task_comment_reactions
from origin.tests.test_base import BaseAPITestCase

REACTION = "/api/v2/task/comment/reaction/"


def _body(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class ReactionViewBase(BaseAPITestCase):
    """`self.user` reacts; `self.user2` wrote comment 1 on `self.task`."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Reactions",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )
        self.comment = TaskComments.objects.create(
            task=self.task, sender=self.user2, comment_id=1, comment_body=_body("hello")
        )
        self.authenticate(self.user)

    def react(self, emoji="👍", **over):
        body = {
            "team_id": str(self.team.team_id),
            "task_id": self.task.task_id,
            "comment_id": 1,
            "reaction_emoji": emoji,
            "sender_id": str(self.user.id),
            **over,
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.client.post(REACTION, body, format="json")

    def unreact(self, emoji="👍", **over):
        query = {
            "team_id": str(self.team.team_id),
            "sender_id": str(self.user.id),
            "task_id": self.task.task_id,
            "comment_id": 1,
            "reaction_emoji": emoji,
            **over,
        }
        return self.client.delete(f"{REACTION}?{urlencode(query)}")

    def rows(self, **filters):
        return TaskCommentReactionFact.objects.filter(task=self.task, **filters)

    def double_up(self, emoji="👍", sender=None, times=2):
        """Rows as the old view left them: the same person's same emoji, once per counter value."""
        for _ in range(times):
            TaskCommentReactionFact.objects.create(
                team=self.team,
                task=self.task,
                comment_id=1,
                reaction_id=task_comment_reactions.next_reaction_id(self.task.task_id, 1),
                reaction_emoji=emoji,
                sender=sender or self.user,
            )


class DuplicateTests(ReactionViewBase):
    def test_a_repeat_post_is_a_no_op_that_answers_with_the_existing_row(self):
        first = self.react()
        self.assertEqual(first.status_code, 201, first.data)
        again = self.react()
        self.assertEqual(again.status_code, 200, again.data)
        self.assertEqual(self.rows().count(), 1)
        self.assertEqual(again.data["reaction_id"], first.data["reaction_id"])
        # the socket layer reads the project and the comment's author off the answer
        self.assertEqual(again.data["projectId"], self.project.project_id)
        self.assertEqual(again.data["commentSenderId"], str(self.user2.id))

    def test_the_body_says_whether_the_reaction_is_new(self):
        """`created` (MIP-1595): the sockets handler reads it, so a repeat is not announced as a
        live toast again. The status code says the same, but a client library that treats every
        2xx alike (the handler's does) cannot see it. Additive: every other key is as before."""
        first = self.react()
        self.assertIs(first.data["created"], True)
        again = self.react()
        self.assertIs(again.data["created"], False)
        self.assertEqual(
            set(again.data) - {"created"},
            set(first.data) - {"created"},
        )
        # a different emoji is new again
        self.assertIs(self.react("🎉").data["created"], True)

    def test_the_delete_answer_carries_no_created(self):
        self.react()
        self.assertNotIn("created", self.unreact().data)

    def test_a_repeat_post_tells_the_author_nothing_again(self):
        with mock.patch.object(task_comment_reactions, "announce_reaction") as announce:
            self.react()
            self.react()
        self.assertEqual(announce.call_count, 1)

    def test_a_different_emoji_or_a_different_person_is_still_a_new_reaction(self):
        self.assertEqual(self.react("👍").status_code, 201)
        self.assertEqual(self.react("🎉").status_code, 201)
        self.authenticate(self.user2)
        self.assertEqual(self.react("👍").status_code, 201)
        self.assertEqual(self.rows().count(), 3)
        self.assertEqual([r.reaction_id for r in self.rows().order_by("reaction_id")], [1, 2, 3])

    def test_the_counter_is_per_comment_whatever_team_an_old_row_carries(self):
        """Rows written when the body named the team can carry another one; the counter and the
        unique `(task, comment, reaction_id)` key do not care, so neither may the next id."""
        stray = TeamMaster.objects.create(
            team_name="Stray", team_email="s@example.com", owner=self.user
        )
        TaskCommentReactionFact.objects.create(
            team=stray, task=self.task, comment_id=1, reaction_id=1, reaction_emoji="🎉",
            sender=self.user2,
        )  # fmt: skip
        res = self.react("👍")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(res.data["reaction_id"], 2)

    def test_deleting_takes_back_a_reaction_that_doubled_up_and_does_not_500(self):
        self.double_up(times=2)
        self.double_up("🎉", sender=self.user2, times=1)
        res = self.unreact()
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual(self.rows(sender=self.user).count(), 0)
        # nobody else's reaction went with it
        self.assertEqual(self.rows(sender=self.user2).count(), 1)

    def test_deleting_one_that_is_not_there_is_still_a_404(self):
        self.assertEqual(self.unreact().status_code, 404)

    def test_the_delete_answers_with_the_comment_the_socket_layer_needs(self):
        self.react()
        res = self.unreact()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["reaction_id"], 1)
        self.assertEqual(res.data["projectId"], self.project.project_id)

    def test_a_delete_does_not_trust_the_teams_the_query_names(self):
        """The task fixes the team; a query naming another cannot hide the caller's own row."""
        self.react()
        res = self.unreact(team_id="not-a-uuid")
        self.assertEqual(res.status_code, 200, getattr(res, "data", None))
        self.assertEqual(self.rows().count(), 0)


class MissingCommentTests(ReactionViewBase):
    def test_a_comment_that_does_not_exist_reads_as_missing(self):
        res = self.react(comment_id=99)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertEqual(res.data, {"error": "Comment not found."})
        self.assertFalse(self.rows().exists())

    def test_a_deleted_comment_reads_as_missing(self):
        TaskComments.objects.filter(task=self.task, comment_id=1).update(is_deleted=True)
        res = self.react()
        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(self.rows().exists())

    def test_another_tasks_comment_number_is_not_found_on_this_one(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, title="O", status="Open", reporter=self.user
        )
        TaskComments.objects.create(
            task=other, sender=self.user2, comment_id=7, comment_body=_body("elsewhere")
        )
        res = self.react(comment_id=7)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertFalse(TaskCommentReactionFact.objects.filter(comment_id=7).exists())

    def test_a_missing_comment_is_the_same_answer_whoever_asks(self):
        """No existence oracle: a comment of a task the caller cannot open is the task's 404."""
        stranger = self.user.__class__.objects.create_user(
            username="s", email="s@example.com", password="x"
        )
        self.authenticate(stranger)
        self.assertEqual(self.react().data, {"error": "Task not found."})

    def test_taking_back_a_reaction_on_a_comment_that_has_since_gone_still_works(self):
        """Delete never asks for the comment: a reaction must stay removable."""
        self.react()
        TaskComments.objects.filter(task=self.task, comment_id=1).update(is_deleted=True)
        self.assertEqual(self.unreact().status_code, 200)
        self.assertFalse(self.rows().exists())


class EmojiTests(ReactionViewBase):
    def test_what_the_app_sends_is_accepted(self):
        for emoji in ("👍", "❤️", "👍🏽", "👨‍👩‍👧", "🇯🇵", "1️⃣", "🏴󠁧󠁢󠁥󠁮󠁧󠁿"):
            with self.subTest(emoji):
                res = self.react(emoji)
                self.assertEqual(res.status_code, 201, res.data)

    def test_a_team_shortcode_is_accepted_only_when_the_teams_catalog_has_it(self):
        self.assertEqual(self.react(":nope:").status_code, 400)
        TeamEmojiMaster.objects.create(team=self.team, name="party_parrot", image="x.gif")
        res = self.react(":party_parrot:")
        self.assertEqual(res.status_code, 201, res.data)

    def test_empty_oversized_text_and_control_characters_are_refused(self):
        refused = {
            "empty": "",
            "blank": "   ",
            "text": "hello",
            "sentence": "this is a whole sentence, not a reaction",
            "oversized": "👍" * 65,
            "newline": "👍\n👍",
            "inner space": "👍 👍",
            "control": "👍\x00",
            "tab": "👍\t👍",
            "bell": "\x07👍",
        }
        for name, emoji in refused.items():
            with self.subTest(name):
                res = self.react(emoji)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertIn("error", res.data)
        self.assertFalse(self.rows().exists())

    def test_a_non_string_is_refused(self):
        for value in (1, ["👍"], {"a": 1}, True):
            with self.subTest(value=value):
                self.assertEqual(self.react(value).status_code, 400)
        self.assertFalse(self.rows().exists())

    def test_the_message_names_the_field_the_page_sends(self):
        res = self.react("hello")
        self.assertIn("reaction_emoji", res.data["error"])

    def test_it_is_stored_trimmed_as_the_tool_stores_it(self):
        self.assertEqual(self.react("  👍 ").status_code, 201)
        self.assertEqual(self.rows().get().reaction_emoji, "👍")
        # and a repeat in the other spelling is the same reaction
        self.assertEqual(self.react("👍").status_code, 200)
        self.assertEqual(self.rows().count(), 1)

    def test_a_reaction_stored_before_the_rule_can_still_be_removed(self):
        TaskCommentReactionFact.objects.create(
            team=self.team, task=self.task, comment_id=1, reaction_id=1, reaction_emoji="x",
            sender=self.user,
        )  # fmt: skip
        self.assertEqual(self.unreact("x").status_code, 200)
        self.assertFalse(self.rows().exists())

    def test_the_view_and_the_tool_share_one_rule(self):
        """The view calls the service's `check_emoji`, not a copy."""
        with mock.patch.object(
            task_comment_reactions,
            "check_emoji",
            side_effect=task_comment_reactions.ReactionRefused("nope"),
        ) as check:
            res = self.react()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(check.call_count, 1)


class TeamFromTheTaskTests(ReactionViewBase):
    def test_the_stored_team_is_the_tasks_whatever_the_body_says(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        res = self.react(team_id=str(other.team_id))
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.rows().get().team_id, self.team.team_id)

    def test_a_body_without_a_team_still_reacts(self):
        res = self.react(team_id=None)
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.rows().get().team_id, self.team.team_id)

    def test_a_malformed_team_is_not_a_reason_to_refuse(self):
        """It is not read at all now, so there is nothing in it to be malformed."""
        self.assertEqual(self.react(team_id="").status_code, 201)

    def test_a_team_that_does_not_exist_cannot_be_written_into_a_row(self):
        res = self.react(team_id="00000000-0000-0000-0000-000000000000")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.rows().get().team_id, self.team.team_id)


class QueryCostTests(ReactionViewBase):
    def test_a_repeat_post_does_not_cost_more_queries_than_the_first(self):
        with CaptureQueriesContext(connection) as first:
            self.react()
        with CaptureQueriesContext(connection) as again:
            self.react()
        self.assertLessEqual(len(again), len(first))
