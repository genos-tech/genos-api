"""MIP-1486: `GET /api/v2/task/comment/` says how each comment's LATEST write arrived.

A comment's card has no `via` of its own. The signal records it on the task's `comment_added` /
`comment_edited` rows, so the web client had to read the whole activity feed (the newest 100 rows)
to learn that a comment came through a connected AI app. Each comment now carries `via`: the
newest of its rows decides, reduced with the SAME rule as the activity feed (MIP-1485): the whole
`via` for the row's actor, the kind only for everyone else, `null` when nothing is known.
"""

from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/task/comment/"
KEY = {"kind": "api_key", "key_id": "k-1", "key_name": "Alice's phone", "key_prefix": "gk_alice"}
APP = {"kind": "oauth", "connection_id": "conn-9", "client_name": "Alice's Claude"}
SECRETS = ("Alice's phone", "k-1", "gk_alice", "conn-9", "Alice's Claude")
BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.user2):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Spec", status="Open"
        )
        self._next = 0

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def write(self, comment_id, writer, via):
        """Save comment `comment_id` as `writer` through `via` (the signal stamps the row)."""
        set_current_user(writer, via=via)
        try:
            comment = TaskComments.objects.filter(task=self.task, comment_id=comment_id).first()
            if comment is None:
                TaskComments.objects.create(
                    task=self.task, comment_id=comment_id, sender=writer, comment_body=BODY
                )
            else:
                comment.comment_body = BODY + [{"type": "paragraph"}]
                comment.save()
        finally:
            clear_current_user()

    def listing(self, viewer):
        self.authenticate(viewer)
        res = self.client.get(URL, {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 200, res.content)
        return {c["commentId"]: c for c in res.data}


class TheViaOnTheCommentList(_Base):
    def test_a_colleague_reads_the_kind_of_a_key_or_app_write_and_nothing_else(self):
        self.write(1, self.user2, KEY)
        self.write(2, self.user2, APP)
        rows = self.listing(self.user)
        self.assertEqual(rows[1]["via"], {"kind": "api_key"})
        self.assertEqual(rows[2]["via"], {"kind": "oauth"})
        text = repr(rows)
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_the_writer_reads_their_own_via_whole(self):
        self.write(1, self.user2, KEY)
        self.write(2, self.user2, APP)
        rows = self.listing(self.user2)
        self.assertEqual(rows[1]["via"], KEY)
        self.assertEqual(rows[2]["via"], APP)

    def test_the_newest_write_decides_a_person_fixing_an_app_comment_clears_it(self):
        self.write(1, self.user2, APP)
        self.write(1, self.user2, {"kind": "app"})  # edited by hand afterwards
        self.assertEqual(self.listing(self.user)[1]["via"], {"kind": "app"})

    def test_the_newest_write_decides_an_app_rewriting_a_typed_comment_marks_it(self):
        self.write(1, self.user2, {"kind": "app"})
        self.write(1, self.user2, APP)
        self.assertEqual(self.listing(self.user)[1]["via"], {"kind": "oauth"})

    def test_the_actor_of_the_newest_row_owns_the_via_not_the_comments_author(self):
        # user2 wrote it by hand; user then rewrote it through their own key.
        self.write(1, self.user2, {"kind": "app"})
        self.write(1, self.user, KEY)
        self.assertEqual(self.listing(self.user)[1]["via"], KEY, "the key's owner reads it")
        self.assertEqual(self.listing(self.user2)[1]["via"], {"kind": "api_key"})

    def test_a_comment_with_no_activity_row_or_no_via_is_null(self):
        # 1: older than the audit log (no row at all). 2: a row with no `via` (a webhook, a
        # command: no current user, so the signal stamps no source).
        TaskComments.objects.create(
            task=self.task, comment_id=1, sender=self.user, comment_body=BODY
        )
        TaskActivity.objects.filter(task=self.task).delete()
        TaskComments.objects.create(
            task=self.task, comment_id=2, sender=self.user, comment_body=BODY
        )
        self.assertNotIn("via", TaskActivity.objects.get(task=self.task).metadata)
        rows = self.listing(self.user)
        self.assertIsNone(rows[1]["via"])
        self.assertIsNone(rows[2]["via"])

    def test_a_row_for_another_task_or_without_a_comment_id_pins_nothing(self):
        other = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Other", status="Open"
        )
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=other,
            actor=self.user2,
            action_type="comment_added",
            field_name="comment",
            metadata={"commentId": 1, "via": KEY},
        )
        TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user2,
            action_type="comment_added",
            field_name="comment",
            metadata={"commentId": None, "via": KEY},
        )
        TaskComments.objects.create(
            task=self.task, comment_id=1, sender=self.user, comment_body=BODY
        )
        TaskActivity.objects.filter(task=self.task, metadata__commentId=1).delete()
        self.assertIsNone(self.listing(self.user)[1]["via"])

    def test_every_other_field_is_unchanged(self):
        self.write(1, self.user2, KEY)
        row = self.listing(self.user)[1]
        self.assertEqual(
            sorted(row),
            sorted(
                [
                    "taskId",
                    "senderId",
                    "senderName",
                    "commentId",
                    "commentBody",
                    "messageId",
                    "reactions",
                    "tsSent",
                    "tsUpdated",
                    "via",
                ]
            ),
        )
        self.assertEqual(row["senderName"], self.user2.username)
        self.assertEqual(row["commentBody"], BODY)

    def test_reading_the_list_does_not_edit_the_stored_row(self):
        self.write(1, self.user2, KEY)
        self.listing(self.user)
        row = TaskActivity.objects.filter(task=self.task, action_type="comment_added").get()
        self.assertEqual(row.metadata["via"], KEY)


class TheQueryCount(_Base):
    def count(self):
        self.authenticate(self.user)
        with CaptureQueriesContext(connection) as ctx:
            res = self.client.get(URL, {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 200)
        return len(ctx), [q["sql"] for q in ctx.captured_queries]

    def without_via(self):
        """The list as it was before this change: the `via` lookup stubbed out."""
        with mock.patch("origin.views.task.task_views._comment_via_by_comment_id", return_value={}):
            return self.count()[0]

    def test_one_extra_query_for_the_whole_list_however_many_comments(self):
        self.write(1, self.user2, KEY)
        for comment_id in range(2, 12):
            self.write(comment_id, self.user2, APP if comment_id % 2 else KEY)
            self.write(comment_id, self.user2, {"kind": "app"})  # a second row per comment
        before = self.without_via()
        with_via, sqls = self.count()
        self.assertEqual(with_via, before + 1, "one query for 11 comments, not one per comment")
        reads = [s for s in sqls if TaskActivity._meta.db_table in s]
        self.assertEqual(len(reads), 1, "and it is on the activity table")

    def test_the_query_hands_back_one_row_per_comment_not_every_edit(self):
        for comment_id in (1, 2):
            self.write(comment_id, self.user2, KEY)
            self.write(comment_id, self.user2, APP)
            self.write(comment_id, self.user2, KEY)
        _, sqls = self.count()
        [read] = [s for s in sqls if TaskActivity._meta.db_table in s]
        self.assertIn("DISTINCT ON", read)
        res = self.listing(self.user2)
        self.assertEqual(res[1]["via"], KEY, "and the row it keeps is the newest")
