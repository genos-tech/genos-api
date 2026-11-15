"""The cleanup for reactions that doubled up before the route was guarded (MIP-1582), and the
read-only count the lead runs on production first.

Dry run by default; `--apply` removes the later copies of the same person's same emoji on the same
comment and nothing else. Counts only in the output.
"""

import contextlib
import io

from django.core.management import call_command

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.services import task_comment_reactions
from origin.tests.reaction_repeats import allow_repeated_reactions
from origin.tests.test_base import BaseAPITestCase

#: The snippet pasted into the PR body for a production shell: one read-only transaction, counts only.
COUNT_SNIPPET = """
from django.db import connection, transaction
from django.db.models import Count, F
from origin.models.task.task_models import TaskCommentReactionFact as R

with transaction.atomic():
    with connection.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY")
    live = R.objects.exclude(sender__isnull=True)
    groups = (
        live.values("task_id", "comment_id", "sender_id", "reaction_emoji")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
    )
    print("total rows:", R.objects.count())
    print("rows whose sender is gone (account deleted):", R.objects.filter(sender__isnull=True).count())
    print("duplicate groups:", groups.count())
    print("rows to remove:", sum(g["n"] - 1 for g in groups))
    print("rows filed under another team than their task:", R.objects.exclude(team_id=F("task__team_id")).count())
"""


class DedupeBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # rows that doubled up before the route was guarded (and before the table refused them)
        allow_repeated_reactions()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )
        self.other = TaskMaster.objects.create(
            team=self.team, project=self.project, title="O", status="Open", reporter=self.user
        )
        for task in (self.task, self.other):
            TaskComments.objects.create(task=task, sender=self.user2, comment_id=1, comment_body=[])

    def add(self, sender, emoji="👍", task=None, comment_id=1, team=None):
        task = task or self.task
        return TaskCommentReactionFact.objects.create(
            team=team or self.team,
            task=task,
            comment_id=comment_id,
            reaction_id=task_comment_reactions.next_reaction_id(task.task_id, comment_id),
            reaction_emoji=emoji,
            sender=sender,
        )

    def run_command(self, *args):
        out = io.StringIO()
        call_command("dedupe_task_comment_reactions", *args, stdout=out)
        return out.getvalue()

    def snapshot(self):
        return sorted(
            TaskCommentReactionFact.objects.values_list(
                "task_id", "comment_id", "reaction_id", "sender_id", "reaction_emoji"
            )
        )

    def messy(self):
        """Three copies of one reaction, two of another, and everything that must NOT be touched."""
        self.add(self.user)  # the keeper of the three
        self.add(self.user2)  # someone else's, same emoji
        self.add(self.user)  # copy
        self.add(self.user)  # copy
        self.add(self.user, "🎉")  # the same person, another emoji
        self.add(self.user, "🎉")  # copy
        self.add(self.user, "👍", task=self.other)  # another task, same comment number
        self.add(None, "👍")  # sender gone
        self.add(None, "👍")  # a different sender gone: NOT a repeat of the one above
        self.add(self.user, "👍", comment_id=2)  # another comment of the same task


class DryRunTests(DedupeBase):
    def test_the_default_counts_and_writes_nothing(self):
        self.messy()
        before = self.snapshot()
        out = self.run_command()
        self.assertIn("DRY RUN", out)
        self.assertIn("10 reaction rows scanned", out)
        self.assertIn("2 (comment, person, emoji) groups repeat", out)
        self.assertIn("would remove: 3", out)
        self.assertEqual(self.snapshot(), before)

    def test_nothing_to_do_says_so(self):
        self.add(self.user)
        out = self.run_command()
        self.assertIn("would remove: 0", out)
        self.assertNotIn("--apply", out)

    def test_the_output_names_no_task_comment_person_or_emoji(self):
        self.messy()
        out = self.run_command("--apply")
        for needle in (str(self.user.id), str(self.user2.id), "👍", "🎉", self.user.email):
            self.assertNotIn(needle, out)


class ApplyTests(DedupeBase):
    def test_it_removes_the_later_copies_and_keeps_the_first(self):
        self.messy()
        out = self.run_command("--apply")
        self.assertIn("APPLY", out)
        self.assertIn("removed: 3", out)
        mine = TaskCommentReactionFact.objects.filter(
            task=self.task, comment_id=1, sender=self.user
        ).order_by("reaction_id")
        self.assertEqual([(r.reaction_emoji, r.reaction_id) for r in mine], [("👍", 1), ("🎉", 5)])

    def test_it_never_touches_anyone_elses_or_another_comments_or_a_gone_senders(self):
        self.messy()
        self.run_command("--apply")
        self.assertEqual(
            TaskCommentReactionFact.objects.filter(task=self.task, sender=self.user2).count(), 1
        )
        self.assertEqual(TaskCommentReactionFact.objects.filter(task=self.other).count(), 1)
        self.assertEqual(
            TaskCommentReactionFact.objects.filter(task=self.task, comment_id=2).count(), 1
        )
        self.assertEqual(
            TaskCommentReactionFact.objects.filter(task=self.task, sender__isnull=True).count(), 2
        )

    def test_a_second_apply_finds_nothing(self):
        self.messy()
        self.run_command("--apply")
        before = self.snapshot()
        self.assertIn("removed: 0", self.run_command("--apply"))
        self.assertEqual(self.snapshot(), before)

    def test_the_removal_leaves_the_comment_and_the_task_alone(self):
        self.messy()
        stamp = TaskMaster.objects.get(pk=self.task.pk).ts_updated_at
        self.run_command("--apply")
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).ts_updated_at, stamp)
        self.assertEqual(TaskComments.objects.count(), 2)

    def test_after_it_a_delete_of_the_reaction_works_where_it_500d(self):
        """The failure this cleans up after: the old DELETE `.get()` found two rows."""
        self.add(self.user)
        self.add(self.user)
        self.run_command("--apply")
        self.authenticate(self.user)
        res = self.client.delete(
            "/api/v2/task/comment/reaction/"
            f"?team_id={self.team.team_id}&sender_id={self.user.id}"
            f"&task_id={self.task.task_id}&comment_id=1&reaction_emoji=%F0%9F%91%8D"
        )
        self.assertEqual(res.status_code, 200)


class CountSnippetTests(DedupeBase):
    def run_snippet(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            exec(COUNT_SNIPPET, {})  # noqa: S102 - the snippet under test
        return dict(
            (line.rsplit(":", 1)[0], int(line.rsplit(":", 1)[1]))
            for line in out.getvalue().splitlines()
        )

    def test_it_counts_what_the_command_would_remove(self):
        stray = TeamMaster.objects.create(
            team_name="Stray", team_email="s@example.com", owner=self.user
        )
        self.messy()
        self.add(self.user2, "🚀", team=stray)  # filed under a team that is not the task's
        counts = self.run_snippet()
        self.assertEqual(counts["total rows"], 11)
        self.assertEqual(counts["duplicate groups"], 2)
        self.assertEqual(counts["rows to remove"], 3)
        self.assertEqual(counts["rows whose sender is gone (account deleted)"], 2)
        self.assertEqual(counts["rows filed under another team than their task"], 1)
        self.assertEqual(task_comment_reactions.repeat_counts()["rows_to_remove"], 3)
