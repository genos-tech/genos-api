"""The table holds one reaction per person and emoji on a comment (MIP-1595).

The routes have refused a repeat since MIP-1582; `unique_task_comment_reaction_per_sender_emoji`
(migration 0246) makes the table refuse it too, so a writer that skips the routes (a script, a
future code path, a race the comment lock does not cover) cannot leave the pair the DELETE once
500'd on. PARTIAL: a reaction whose sender's account was deleted has a NULL sender, different
people share the NULL, and those rows must stay writable and never count as repeats.

The migration refuses to build the index over rows that already repeat, saying how to clean them,
and deletes nothing itself.
"""

import importlib

from django.apps import apps
from django.db import IntegrityError, transaction

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.services import task_comment_reactions
from origin.tests.reaction_repeats import allow_repeated_reactions
from origin.tests.test_base import BaseAPITestCase

migration = importlib.import_module(
    "origin.migrations.0246_task_comment_reaction_unique_per_sender"
)


class UniqueBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", reporter=self.user
        )
        self.other = TaskMaster.objects.create(
            team=self.team, project=project, title="O", status="Open", reporter=self.user
        )
        for task in (self.task, self.other):
            for comment_id in (1, 2):
                TaskComments.objects.create(
                    task=task, sender=self.user2, comment_id=comment_id, comment_body=[]
                )

    def add(self, sender, emoji="👍", task=None, comment_id=1):
        task = task or self.task
        return TaskCommentReactionFact.objects.create(
            team=self.team,
            task=task,
            comment_id=comment_id,
            reaction_id=task_comment_reactions.next_reaction_id(task.task_id, comment_id),
            reaction_emoji=emoji,
            sender=sender,
        )


class ConstraintTests(UniqueBase):
    def test_the_same_person_and_emoji_on_a_comment_twice_is_refused(self):
        self.add(self.user)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.add(self.user)
        self.assertEqual(TaskCommentReactionFact.objects.count(), 1)

    def test_another_emoji_person_comment_or_task_is_still_allowed(self):
        self.add(self.user)
        self.add(self.user, "🎉")
        self.add(self.user2)
        self.add(self.user, comment_id=2)
        self.add(self.user, task=self.other)
        self.assertEqual(TaskCommentReactionFact.objects.count(), 5)

    def test_rows_of_deleted_senders_may_share_a_comment_and_emoji(self):
        self.add(None)
        self.add(None)
        self.add(None)
        self.assertEqual(TaskCommentReactionFact.objects.filter(sender__isnull=True).count(), 3)

    def test_the_service_still_answers_a_repeat_with_the_existing_row(self):
        kwargs = dict(
            team_id=self.team.team_id,
            task_id=self.task.task_id,
            comment_id=1,
            user_id=self.user.id,
            emoji="👍",
        )
        first = task_comment_reactions.store_reaction(**kwargs)
        again = task_comment_reactions.store_reaction(**kwargs)
        self.assertTrue(first.created)
        self.assertFalse(again.created)
        self.assertEqual(again.reaction.pk, first.reaction.pk)
        self.assertEqual(TaskCommentReactionFact.objects.count(), 1)


class MigrationGuardTests(UniqueBase):
    """The step ahead of the index: it stops, and names the way out, rather than delete anything."""

    def guard(self):
        migration.refuse_if_repeats(apps, None)

    def test_a_clean_table_passes_deleted_senders_included(self):
        self.add(self.user)
        self.add(None)
        self.add(None)
        self.guard()

    def test_a_repeat_stops_the_migration_with_counts_and_the_command_to_run(self):
        allow_repeated_reactions()
        self.add(self.user)
        self.add(self.user)
        self.add(self.user2, "🎉")
        self.add(self.user2, "🎉")
        self.add(self.user2, "🎉")
        with self.assertRaises(RuntimeError) as caught:
            self.guard()
        text = str(caught.exception)
        self.assertIn("2 (comment, person, emoji) group", text)
        self.assertIn("dedupe_task_comment_reactions", text)
        for private in ("👍", "🎉", str(self.user.id), str(self.task.task_id)):
            self.assertNotIn(private, text)
        # it deleted nothing
        self.assertEqual(TaskCommentReactionFact.objects.count(), 5)

    def test_it_depends_on_the_latest_origin_migration_before_it(self):
        self.assertEqual(
            migration.Migration.dependencies, [("origin", "0245_task_note_role_revoke")]
        )
