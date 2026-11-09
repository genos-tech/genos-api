"""MIP-1351: `last_progress_at` and the `TaskProgressFact` history behind it.

A change to a task moves the task's own value and every ancestor's, up to the
milestone's backing task; a bulk cascade and a derived status flip do not; the
history stays with the parents the task had at the time.
"""

from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import TestCase
from django.utils import timezone

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.services import task_progress
from origin.services.activity_suppression import suppress_task_activity
from origin.signals.task_signals import update_tasks_recorded

User = get_user_model()


class ProgressBase(TestCase):
    def setUp(self):
        task_progress.reset_pending()
        self.user = User.objects.create_user(
            username="prog", email="prog@test.com", password="pass12345"
        )
        self.team = TeamMaster.objects.create(
            team_name="Progress Team", team_email="progteam@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Progress Project",
            owner=self.user,
            project_system_user=self.user,
        )
        with self.captureOnCommitCallbacks(execute=True):
            # milestone backing row -> task -> subtask, plus a sibling task
            self.ms = self._task("Milestone", is_milestone=True)
            self.task = self._task("Task A", parent=self.ms)
            self.sub = self._task("Sub A-A", parent=self.task)
            self.sibling = self._task("Task B", parent=self.ms)

    def _task(self, title, parent=None, **extra):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=title,
            status="Open",
            parent_task_id=parent.task_id if parent else None,
            **extra,
        )

    def _edit(self, task, **fields):
        """Save a change the way a view does, and run the on-commit work."""
        with self.captureOnCommitCallbacks(execute=True):
            task = TaskMaster.objects.get(pk=task.pk)
            for k, v in fields.items():
                setattr(task, k, v)
            task.save()

    def _lp(self, task):
        return TaskMaster.objects.get(pk=task.pk).last_progress_at

    def _updated(self, task):
        return TaskMaster.objects.get(pk=task.pk).ts_updated_at

    def _freeze(self):
        """Pin every value to one old time so a later change is visible. The
        facts go too: the value is recomputed from them, so an old value only
        stays old when nothing newer is on record."""
        old = timezone.now() - timedelta(days=30)
        TaskProgressFact.objects.filter(ancestor_task__project=self.project).delete()
        TaskMaster.objects.filter(project=self.project).update(last_progress_at=old)
        return old


class LiveWriterTests(ProgressBase):
    def test_created_task_starts_at_its_creation(self):
        self.assertIsNotNone(self._lp(self.sibling))

    def test_subtask_change_moves_it_its_parent_and_the_milestone_but_not_siblings(self):
        old = self._freeze()
        self._edit(self.sub, status="Closed")
        for moved in (self.sub, self.task, self.ms):
            self.assertGreater(self._lp(moved), old, moved.title)
        self.assertEqual(self._lp(self.sibling), old)

    def test_own_edit_moves_itself_and_ancestors_but_not_children(self):
        old = self._freeze()
        self._edit(self.task, priority="High")
        self.assertGreater(self._lp(self.task), old)
        self.assertGreater(self._lp(self.ms), old)
        self.assertEqual(self._lp(self.sub), old)

    def test_parent_ts_updated_at_is_untouched(self):
        before = self._updated(self.ms)
        self._edit(self.sub, status="Closed")
        self.assertEqual(self._updated(self.ms), before)
        self.assertEqual(
            self._updated(self.task), TaskMaster.objects.get(pk=self.task.pk).ts_updated_at
        )

    def test_facts_record_every_level_with_depth_and_actor(self):
        self._edit(self.sub, status="Closed")
        activity = TaskActivity.objects.filter(task=self.sub, action_type="status_changed").get()
        facts = {f.ancestor_task_id: f for f in TaskProgressFact.objects.filter(activity=activity)}
        self.assertEqual(
            {k: v.depth for k, v in facts.items()},
            {self.sub.task_id: 0, self.task.task_id: 1, self.ms.task_id: 2},
        )
        self.assertTrue(all(f.source_task_id == self.sub.task_id for f in facts.values()))
        self.assertTrue(all(f.ts == activity.ts_created_at for f in facts.values()))
        self.assertTrue(all(f.action_type == "status_changed" for f in facts.values()))

    def test_never_moves_backwards(self):
        future = timezone.now() + timedelta(days=1)
        TaskMaster.objects.filter(pk=self.ms.pk).update(last_progress_at=future)
        self._edit(self.sub, status="Closed")
        self.assertEqual(self._lp(self.ms), future)

    def test_a_cascade_is_not_progress(self):
        old = self._freeze()
        n = TaskProgressFact.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            update_tasks_recorded(
                TaskMaster.objects.filter(pk=self.sub.pk), cascade="milestone_move", priority="High"
            )
        self.assertEqual(TaskProgressFact.objects.count(), n)
        self.assertEqual(self._lp(self.sub), old)
        self.assertEqual(self._lp(self.ms), old)
        # ...but it did leave its audit row, and moved ts_updated_at for sync
        self.assertTrue(
            TaskActivity.objects.filter(task=self.sub, action_type="priority_changed").exists()
        )

    def test_a_derived_blocked_flip_is_not_progress(self):
        old = self._freeze()
        with self.captureOnCommitCallbacks(execute=True):
            task = TaskMaster.objects.get(pk=self.sub.pk)
            task.status = "Blocked"
            task._derived_blocked_sync = True
            task.save()
        self.assertEqual(self._lp(self.sub), old)
        self.assertEqual(self._lp(self.ms), old)

    def test_nothing_is_written_while_activity_is_suppressed(self):
        old = self._freeze()
        n = TaskProgressFact.objects.count()
        with suppress_task_activity():
            self._edit(self.sub, status="Closed")
        self.assertEqual(TaskProgressFact.objects.count(), n)
        self.assertEqual(self._lp(self.ms), old)

    def test_a_rolled_back_change_leaves_nothing_and_does_not_swallow_the_next(self):
        old = self._freeze()
        n = TaskProgressFact.objects.count()
        with self.captureOnCommitCallbacks(execute=True):
            try:
                with transaction.atomic():
                    task = TaskMaster.objects.get(pk=self.sub.pk)
                    task.status = "Closed"
                    task.save()
                    raise RuntimeError("roll back")
            except RuntimeError:
                pass
        self.assertEqual(TaskProgressFact.objects.count(), n)
        self.assertEqual(self._lp(self.ms), old)
        # the pending set must not strand the rolled-back time, nor block later writes
        self._edit(self.sibling, status="WIP")
        self.assertGreater(self._lp(self.ms), old)
        self.assertEqual(self._lp(self.sub), old)

    def test_a_parent_cycle_terminates_and_lists_each_task_once(self):
        a = self._task("Cycle A")
        b = self._task("Cycle B", parent=a)
        TaskMaster.objects.filter(pk=a.pk).update(parent_task_id=b.task_id)
        self._edit(b, priority="High")
        activity = TaskActivity.objects.filter(task=b, action_type="priority_changed").get()
        self.assertEqual(TaskProgressFact.objects.filter(activity=activity).count(), 2)

    def test_a_failure_in_the_progress_writer_does_not_fail_the_write(self):
        with mock.patch.object(
            task_progress.TaskProgressFact.objects, "bulk_create", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("origin.services.task_progress", level="ERROR"):
                self._edit(self.sub, status="Closed")
        self.assertEqual(TaskMaster.objects.get(pk=self.sub.pk).status, "Closed")
        self.assertTrue(
            TaskActivity.objects.filter(task=self.sub, action_type="status_changed").exists()
        )

    def test_history_stays_with_the_old_parent_and_the_move_counts_for_the_new_one(self):
        other = self._task("Task C", parent=self.ms)
        self._edit(self.sub, status="WIP")  # under Task A
        self._edit(self.sub, parent_task_id=other.task_id)  # moved to Task C
        before_move = TaskActivity.objects.get(task=self.sub, action_type="status_changed")
        move = TaskActivity.objects.get(task=self.sub, action_type="parent_changed")
        self.assertTrue(
            TaskProgressFact.objects.filter(activity=before_move, ancestor_task=self.task).exists()
        )
        self.assertTrue(
            TaskProgressFact.objects.filter(activity=move, ancestor_task=other).exists()
        )
        self.assertFalse(
            TaskProgressFact.objects.filter(activity=move, ancestor_task=self.task).exists()
        )
        # the old parent keeps what happened under it
        self.assertTrue(
            TaskProgressFact.objects.filter(activity=before_move, ancestor_task=self.task).exists()
        )

    def test_deleting_a_task_takes_its_facts_with_it(self):
        self._edit(self.sub, status="Closed")
        sub_id = self.sub.task_id
        with suppress_task_activity():
            TaskMaster.objects.filter(pk=self.sub.pk).delete()
        self.assertFalse(TaskProgressFact.objects.filter(source_task_id=sub_id).exists())
        self.assertFalse(TaskProgressFact.objects.filter(ancestor_task_id=sub_id).exists())

    def test_the_value_is_in_place_before_the_feed_version_moves(self):
        """A client that sees the new version must read the new value. The
        refresh therefore runs inside the feed's flush, before its upsert."""
        from origin.services import change_feed

        old = self._freeze()
        seen = []
        real = change_feed._upsert

        def spy(keys):
            seen.append(TaskMaster.objects.get(pk=self.ms.pk).last_progress_at)
            return real(keys)

        with mock.patch.object(change_feed, "_upsert", side_effect=spy):
            self._edit(self.sub, status="Closed")
        self.assertTrue(seen)
        self.assertGreater(seen[0], old)

    def test_a_change_with_no_other_bump_still_moves_the_feed(self):
        """A comment bumps nothing on its own, but the parent's value moved."""
        from origin.services import change_feed

        change_feed.reset_pending()
        with self.captureOnCommitCallbacks(execute=False):
            task_progress.offer(
                TaskActivity.objects.filter(task=self.sub).first(),
                TaskMaster.objects.get(pk=self.sub.pk),
            )
            self.assertIn(
                change_feed.tasks_project_key(self.project.project_id), change_feed.pending_keys()
            )

    def test_a_dangling_parent_gets_no_fact_pointing_nowhere(self):
        orphan = self._task("Orphan")
        TaskMaster.objects.filter(pk=orphan.pk).update(parent_task_id=999999)
        self._edit(orphan, priority="High")
        activity = TaskActivity.objects.get(task=orphan, action_type="priority_changed")
        self.assertEqual(
            list(
                TaskProgressFact.objects.filter(activity=activity).values_list(
                    "ancestor_task_id", flat=True
                )
            ),
            [orphan.task_id],
        )

    def test_a_stranded_id_from_a_rolled_back_change_moves_nothing(self):
        old = self._freeze()
        task_progress._pending.ids.update({self.ms.task_id, self.task.task_id})
        self._edit(self.sibling, status="WIP")  # an unrelated commit drains the set
        self.assertEqual(self._lp(self.task), old)
        self.assertGreater(self._lp(self.ms), old)  # only for the sibling's own change

    def test_the_value_moves_only_once_the_transaction_has_committed(self):
        """Not inside it: an edit under a busy milestone would otherwise hold the
        milestone's row lock until it commits."""
        old = self._freeze()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            task = TaskMaster.objects.get(pk=self.sub.pk)
            task.status = "Closed"
            task.save()
            self.assertEqual(self._lp(self.ms), old)
        for callback in callbacks:
            callback()
        self.assertGreater(self._lp(self.ms), old)


class BackfillTests(ProgressBase):
    def _run(self, *args):
        out = StringIO()
        call_command("backfill_task_progress", *args, stdout=out)
        return out.getvalue()

    def _snapshot(self):
        return (
            sorted(
                TaskProgressFact.objects.values_list(
                    "activity_id",
                    "ancestor_task_id",
                    "source_task_id",
                    "depth",
                    "ts",
                    "action_type",
                )
            ),
            {t.pk: t.last_progress_at for t in TaskMaster.objects.filter(project=self.project)},
        )

    def test_rebuilds_exactly_what_the_live_writer_wrote(self):
        self._edit(self.sub, status="Closed")
        self._edit(self.task, priority="High")
        live = self._snapshot()
        TaskProgressFact.objects.all().delete()
        TaskMaster.objects.filter(project=self.project).update(last_progress_at=None)
        self._run()
        self.assertEqual(self._snapshot(), live)

    def test_is_idempotent(self):
        self._edit(self.sub, status="Closed")
        TaskProgressFact.objects.all().delete()
        self._run()
        first = self._snapshot()
        out = self._run()
        self.assertEqual(self._snapshot(), first)
        self.assertIn("added 0 progress fact(s)", out)
        self.assertIn("moved last_progress_at on 0 task(s)", out)

    def test_dry_run_writes_nothing(self):
        self._edit(self.sub, status="Closed")
        TaskProgressFact.objects.all().delete()
        TaskMaster.objects.filter(project=self.project).update(last_progress_at=None)
        out = self._run("--dry-run")
        self.assertEqual(TaskProgressFact.objects.count(), 0)
        self.assertFalse(
            TaskMaster.objects.filter(project=self.project, last_progress_at__isnull=False).exists()
        )
        self.assertIn("would add", out)
        self.assertNotIn("would add 0", out)

    def test_skips_cascade_rows_and_never_lowers_a_value(self):
        update_tasks_recorded(
            TaskMaster.objects.filter(pk=self.sub.pk), cascade="milestone_move", priority="High"
        )
        TaskProgressFact.objects.all().delete()
        future = timezone.now() + timedelta(days=1)
        TaskMaster.objects.filter(pk=self.ms.pk).update(last_progress_at=future)
        self._run()
        moved = TaskActivity.objects.get(task=self.sub, action_type="priority_changed")
        self.assertFalse(TaskProgressFact.objects.filter(activity=moved).exists())
        self.assertEqual(self._lp(self.ms), future)

    def test_a_task_with_no_history_starts_at_its_creation(self):
        bare = self._task("Bare")
        TaskActivity.objects.filter(task=bare).delete()
        TaskMaster.objects.filter(pk=bare.pk).update(last_progress_at=None)
        self._run()
        self.assertEqual(self._lp(bare), TaskMaster.objects.get(pk=bare.pk).ts_created_at)

    def test_a_dangling_parent_gets_no_fact_pointing_nowhere(self):
        orphan = self._task("Orphan")
        TaskMaster.objects.filter(pk=orphan.pk).update(parent_task_id=999999)
        TaskProgressFact.objects.all().delete()
        self._run()
        self.assertEqual(
            set(
                TaskProgressFact.objects.filter(source_task=orphan).values_list(
                    "ancestor_task_id", flat=True
                )
            ),
            {orphan.task_id},
        )

    def test_leaves_ts_updated_at_alone(self):
        before = {t.pk: t.ts_updated_at for t in TaskMaster.objects.filter(project=self.project)}
        TaskMaster.objects.filter(project=self.project).update(last_progress_at=None)
        self._run()
        after = {t.pk: t.ts_updated_at for t in TaskMaster.objects.filter(project=self.project)}
        self.assertEqual(before, after)


class DeleteOrderTests(ProgressBase):
    """Adding `TaskProgressFact` to the task graph changed the order Django deletes
    models in, and with it WHEN a comment's `post_delete` ran relative to its task
    and project (it used to run after both were gone, so `_task_or_none` saw
    nothing). The audit row for a comment that goes with its task must not depend
    on that: it would be about to cascade away, and writing it leaves an orphan
    the foreign key rejects at COMMIT."""

    def _comment(self, task):
        from origin.models.task.task_models import TaskComments

        return TaskComments.objects.create(
            task=task, sender=self.user, comment_id=1, comment_body=[{"text": "hi"}]
        )

    def test_deleting_the_project_with_a_comment_on_a_task_is_clean(self):
        from origin.services.project_delete import delete_project

        self._comment(self.task)
        delete_project(self.project)  # the FK check at the end of the test is the assertion
        self.assertFalse(TaskMaster.objects.filter(project_id=self.project.project_id).exists())
        self.assertEqual(TaskProgressFact.objects.count(), 0)

    def test_deleting_a_task_with_a_comment_on_it_is_clean(self):
        from origin.models.task.task_models import TaskComments

        self._comment(self.sub)
        TaskMaster.objects.get(pk=self.sub.pk).delete()
        self.assertFalse(TaskComments.objects.exists())
        self.assertFalse(TaskActivity.objects.filter(action_type="comment_deleted").exists())

    def test_deleting_a_comment_on_its_own_is_still_audited(self):
        """The guard is for a comment that goes WITH its task, not for every hard delete."""
        comment = self._comment(self.sub)
        comment.delete()
        self.assertTrue(
            TaskActivity.objects.filter(task=self.sub, action_type="comment_deleted").exists()
        )

    def test_deleting_comments_through_a_queryset_is_still_audited(self):
        from origin.models.task.task_models import TaskComments

        self._comment(self.sub)
        TaskComments.objects.filter(task=self.sub).delete()
        self.assertTrue(
            TaskActivity.objects.filter(task=self.sub, action_type="comment_deleted").exists()
        )
