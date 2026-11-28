"""MIP-1785: `purge_deleted_tasks` hard-deletes tasks that have been in status "Deleted" for more
than 30 days, with what hangs off them, and touches nothing live.

The app's "delete" only writes `status = "Deleted"`; nothing ever removed such a task. The
deletion time the system records is the `status_changed` history row whose `new_value` is
"Deleted" (the post_save diff in `signals/task_signals.py`), so the tests make a deleted task the
way the app does (save the status, let the signal write the row) and then age the clock by moving
that row and `ts_updated_at`.

Files are real: a temporary MEDIA_ROOT, so "the file left storage" is checked, not assumed.
"""

from __future__ import annotations

import tempfile
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db.models import Max
from django.test import override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Activity, ActivityType, Message
from origin.models.note.task_note_models import TaskNoteAttachmentFact, TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.search_engine.models import AgentSession
from origin.services import deleted_task_purge as svc
from origin.services import unified_writer
from origin.tests.media_support import remove_media_root
from origin.tests.test_base import BaseAPITestCase

BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "secret words"}]}]
_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-purge-deleted-tasks-")
TITLE = "Quarterly secret plan"


def _call(*args) -> str:
    out = StringIO()
    call_command("purge_deleted_tasks", *args, stdout=out)
    return out.getvalue()


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class PurgeBase(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        remove_media_root(_MEDIA_ROOT)

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        # A live task that `bump_anchor` keeps at the project's highest number: the purge never removes
        # the task holding it (a display id must not be handed out twice).
        self.anchor = self.make_task(status="Open", title="live anchor")
        self.addCleanup(mock.patch.stopall)
        # The search purge needs OpenSearch; what it was asked to remove is asserted instead.
        patcher = mock.patch("origin.search_engine.purge.purge_entities")
        self.purge_entities = patcher.start()

    # -- builders ---------------------------------------------------------------------------

    def make_task(self, *, status="Open", title=TITLE, project=None, **extra):
        return TaskMaster.objects.create(
            team=self.team, project=project or self.project, title=title, status=status, **extra
        )

    def make_deleted(self, days=40, **extra):
        """A task deleted the way the app deletes: status saved, the signal writes the history row;
        then the clock moved `days` back."""
        task = self.make_task(status="Open", **extra)
        task.status = "Deleted"
        task.save()
        self.age(task, days)
        return task

    def age(self, task, days):
        when = timezone.now() - timedelta(days=days)
        TaskActivity.objects.filter(
            task=task, action_type="status_changed", field_name="status", new_value="Deleted"
        ).update(ts_created_at=when)
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=when)

    def exists(self, task):
        return TaskMaster.objects.filter(pk=task.pk).exists()

    def attach(self, task, name="a.txt"):
        row = TaskAttachments(task=task, attachment_id=1, original_filename=name)
        row.attached_file.save(name, ContentFile(b"bytes"), save=True)
        return row

    def body_attach(self, task, name="b.png"):
        row = TaskBodyAttachmentFact(task=task, uploader=self.user)
        row.body_attachment_url.save(name, ContentFile(b"img"), save=True)
        return row

    def bump_anchor(self):
        """Keep the live anchor the project's highest number, whatever the test made since."""
        top = (
            TaskMaster.objects.filter(project=self.project)
            .exclude(pk=self.anchor.pk)
            .aggregate(m=Max("project_task_number"))["m"]
            or 0
        )
        TaskMaster.objects.filter(pk=self.anchor.pk).update(project_task_number=top + 1)

    def cmd(self, *args):
        self.bump_anchor()
        return _call(*args)

    def service(self, **kwargs):
        self.bump_anchor()
        return svc.purge_deleted_tasks(**kwargs)

    def purge(self, *args):
        with self.captureOnCommitCallbacks(execute=True):
            return self.cmd("--apply", "--yes", *args)


class WhichTasksGo(PurgeBase):
    def test_a_task_deleted_more_than_30_days_ago_is_purged(self):
        old = self.make_deleted(days=31)
        self.purge()
        self.assertFalse(self.exists(old))

    def test_a_task_deleted_29_days_ago_stays(self):
        recent = self.make_deleted(days=29)
        self.purge()
        self.assertTrue(self.exists(recent))

    def test_a_restored_task_stays_whatever_its_old_history_says(self):
        task = self.make_deleted(days=60)
        task.status = "Open"  # the restore: the app writes the status back
        task.save()
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=60)
        )
        self.purge()
        self.assertTrue(self.exists(task))

    def test_a_task_deleted_again_is_dated_by_the_latest_deletion(self):
        task = self.make_deleted(days=90)
        task.status = "Open"
        task.save()
        task.status = "Deleted"
        task.save()  # deleted again just now: a second "Deleted" row, fresh
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=60)
        )
        self.purge()
        self.assertTrue(self.exists(task))

    def test_a_live_task_is_never_touched(self):
        live = [self.make_task(status=s) for s in ("Open", "WIP", "Blocked", "Pending", "Closed")]
        for task in live:
            TaskMaster.objects.filter(pk=task.pk).update(
                ts_updated_at=timezone.now() - timedelta(days=400)
            )
        self.purge()
        for task in live:
            self.assertTrue(self.exists(task), task.status)

    def test_a_task_with_no_deletion_record_stays(self):
        """Status set under the signals' radar (no history row): the time of the deletion is not
        known anywhere, so it is not guessed."""
        task = self.make_task(status="Open")
        TaskMaster.objects.filter(pk=task.pk).update(
            status="Deleted", ts_updated_at=timezone.now() - timedelta(days=400)
        )
        self.assertFalse(TaskActivity.objects.filter(task=task, new_value="Deleted").exists())
        out = self.purge()
        self.assertTrue(self.exists(task))
        self.assertIn("no deletion time recorded", out)

    def test_a_task_changed_since_its_deletion_waits_for_that_too(self):
        task = self.make_deleted(days=60)
        TaskMaster.objects.filter(pk=task.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=5)
        )
        self.purge()
        self.assertTrue(self.exists(task))

    def test_a_milestones_backing_task_is_never_purged(self):
        backing = self.make_task(status="Open", title="m", is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title="m"
        )
        backing.status = "Deleted"
        backing.save()
        self.age(backing, 90)
        # also a milestone row whose backing task lost its flag: pointed at by the milestone
        plain = self.make_deleted(days=90)
        MilestoneMaster.objects.filter(pk=milestone.pk).update(converted_to_task=plain)
        self.purge()
        self.assertTrue(self.exists(backing))
        self.assertTrue(self.exists(plain))
        milestone.refresh_from_db()
        self.assertEqual(milestone.task_id, backing.task_id)
        self.assertEqual(milestone.converted_to_task_id, plain.task_id)

    def test_the_task_with_its_projects_highest_number_stays(self):
        self.make_deleted(days=60)  # a lower number: goes
        TaskMaster.objects.filter(pk=self.anchor.pk).delete()  # now the deleted one is the highest
        newest = self.make_deleted(days=60)
        report = self.service(apply=True, limit=50)
        self.assertTrue(self.exists(newest))
        self.assertEqual(report.purged, 1)
        self.assertEqual(report.kept["highest number in its project"], 1)


class DryRunAndSafety(PurgeBase):
    def test_default_is_a_dry_run_that_writes_nothing_and_prints_no_text(self):
        task = self.make_deleted(days=60)
        attachment = self.attach(task, "private-name.txt")
        name = attachment.attached_file.name
        self.body_attach(task)
        TaskComments.objects.create(task=task, sender=self.user, comment_id=1, comment_body=BODY)
        before = TaskActivity.objects.filter(task=task).count()
        out = self.cmd()
        self.assertIn("DRY RUN", out)
        self.assertIn("1 task(s) would be purged", out)
        self.assertIn("1 attachment(s)", out)
        self.assertIn("1 comment(s)", out)
        for secret in (TITLE, "private-name", "secret words", name):
            self.assertNotIn(secret, out)
        self.assertTrue(self.exists(task))
        self.assertTrue(default_storage.exists(name))
        self.assertEqual(TaskComments.objects.filter(task=task).count(), 1)
        self.assertEqual(TaskActivity.objects.filter(task=task).count(), before)
        self.purge_entities.assert_not_called()

    def test_apply_needs_yes(self):
        task = self.make_deleted(days=60)
        with self.assertRaises(CommandError):
            self.cmd("--apply")
        self.assertTrue(self.exists(task))

    def test_the_limit_must_be_positive(self):
        with self.assertRaises(CommandError):
            self.cmd("--limit", "0")

    def test_a_second_run_finds_nothing(self):
        self.make_deleted(days=60)
        self.purge()
        again = self.service(apply=True, limit=50)
        self.assertEqual(again.purged, 0)

    def test_each_run_takes_at_most_the_limit(self):
        tasks = [self.make_deleted(days=60) for _ in range(3)]
        first = self.service(apply=True, limit=2)
        self.assertEqual(first.purged, 2)
        self.assertEqual(sum(self.exists(t) for t in tasks), 1)
        second = self.service(apply=True, limit=2)
        self.assertEqual(second.purged, 1)

    def test_a_dry_run_reports_what_the_next_apply_would_take_not_more(self):
        for _ in range(3):
            self.make_deleted(days=60)
        report = self.service(apply=False, limit=2)
        self.assertEqual((report.purged, report.selected, report.waiting), (0, 2, 1))


class WhatGoesWithTheTask(PurgeBase):
    def test_attachments_and_body_images_leave_the_database_and_storage(self):
        task = self.make_deleted(days=60)
        a, b = self.attach(task), self.body_attach(task)
        names = [a.attached_file.name, b.body_attachment_url.name]
        for name in names:
            self.assertTrue(default_storage.exists(name))
        with self.captureOnCommitCallbacks(execute=True):
            report = self.service(apply=True, limit=5)
        self.assertEqual(report.counts["attachments"], 1)
        self.assertEqual(report.counts["body images"], 1)
        self.assertFalse(TaskAttachments.objects.filter(task_id=task.task_id).exists())
        self.assertFalse(TaskBodyAttachmentFact.objects.filter(task_id=task.task_id).exists())
        for name in names:
            self.assertFalse(default_storage.exists(name), "file left in storage")

    def test_another_tasks_files_and_a_file_a_live_row_names_stay(self):
        task = self.make_deleted(days=60)
        live = self.make_task(status="Open")
        live_file = self.attach(live, "live.txt")
        shared = self.attach(task, "same.txt")
        # a live row of ANOTHER task that names the very same stored file
        TaskAttachments.objects.filter(pk=live_file.pk).update(
            attached_file=shared.attached_file.name
        )
        self.purge()
        self.assertTrue(self.exists(live))
        self.assertTrue(TaskAttachments.objects.filter(pk=live_file.pk).exists())
        self.assertTrue(default_storage.exists(shared.attached_file.name))

    def test_a_file_outside_the_tasks_own_folder_is_not_deleted(self):
        task = self.make_deleted(days=60)
        row = self.attach(task, "x.txt")
        elsewhere = default_storage.save("somewhere/else/x.txt", ContentFile(b"1"))
        TaskAttachments.objects.filter(pk=row.pk).update(attached_file=elsewhere)
        report = self.service(apply=True, limit=5)
        self.assertFalse(self.exists(task))
        self.assertTrue(default_storage.exists(elsewhere))
        self.assertEqual(report.files_kept, 1)

    def test_comments_their_chat_mirrors_and_the_card_are_blanked_and_the_rows_removed(self):
        task = self.make_deleted(days=60)
        TaskComments.objects.create(task=task, sender=self.user, comment_id=1, comment_body=BODY)
        mirror = unified_writer.write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=1,
            sender_id=str(self.user.id),
            body=BODY,
            project_id=self.project.project_id,
            bypass_flag=True,
        )
        self.assertIsNotNone(mirror)
        header_id = mirror.parent_id
        # somebody's own chat reply in the card's thread: a message, not a comment
        reply = Message.objects.create(
            channel=mirror.channel,
            seq=mirror.seq + 1,
            sender=self.user2,
            body=BODY,
            body_text="a chat reply",
            parent_id=header_id,
            thread_root_id=header_id,
            is_thread_reply=True,
        )
        out = self.purge()
        self.assertFalse(TaskComments.objects.filter(task_id=task.task_id).exists())
        for pk in (mirror.pk, header_id):
            message = Message.objects.get(pk=pk)
            self.assertIsNotNone(message.deleted_at)
            self.assertEqual((message.body, message.body_text), ([], ""))
        kept = Message.objects.get(pk=reply.pk)
        self.assertIsNone(kept.deleted_at)
        self.assertEqual(kept.body_text, "a chat reply")
        self.assertIn("1 comment(s)", out)
        self.assertIn("left in place", out)

    def test_inbox_rows_that_point_at_the_task_go_and_others_stay(self):
        task = self.make_deleted(days=60)
        other = self.make_task(status="Open")
        mine = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"taskId": task.task_id},
        )
        as_text = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"taskId": str(task.task_id)},
        )
        theirs = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"taskId": other.task_id},
        )
        self.purge()
        self.assertFalse(Activity.objects.filter(pk__in=[mine.pk, as_text.pk]).exists())
        self.assertTrue(Activity.objects.filter(pk=theirs.pk).exists())

    def test_a_pending_email_for_the_tasks_inbox_row_goes_with_it(self):
        """MIP-1940: the outbox link is SET_NULL, so the email would be sent with its text."""
        from origin.models.common.notification_models import EmailNotificationEvent

        task = self.make_deleted(days=60)
        mention = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"taskId": task.task_id},
        )
        mail = EmailNotificationEvent.objects.create(
            user=self.user2, category="mention", title="t", body="secret", activity=mention
        )
        self.purge()
        self.assertFalse(EmailNotificationEvent.objects.filter(pk=mail.pk).exists())

    def test_the_tasks_note_and_its_files_go(self):
        task = self.make_deleted(days=60)
        note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=task,
            title="n",
            body=BODY,
        )
        row = TaskNoteAttachmentFact(note=note, uploader=self.user, task=task)
        row.note_attachment_url.save("n.txt", ContentFile(b"1"), save=True)
        name = row.note_attachment_url.name
        self.purge()
        self.assertFalse(TaskNoteMaster.objects.filter(pk=note.pk).exists())
        self.assertFalse(TaskNoteAttachmentFact.objects.filter(pk=row.pk).exists())
        self.assertFalse(default_storage.exists(name))

    def test_history_dependencies_between_deleted_tasks_and_the_search_index_go(self):
        a = self.make_deleted(days=60)
        b = self.make_deleted(days=60)
        TaskDependency.objects.create(blocker_task=a, blocked_task=b, team=self.team)
        self.assertTrue(TaskActivity.objects.filter(task=a).exists())
        self.purge()
        self.assertFalse(TaskDependency.objects.filter(blocker_task_id=a.task_id).exists())
        self.assertFalse(TaskActivity.objects.filter(task_id__in=[a.task_id, b.task_id]).exists())
        pairs = {pair for call in self.purge_entities.call_args_list for pair in call.args[0]}
        self.assertIn(("task", f"task:{a.task_id}"), pairs)
        self.assertIn(("task", f"task:{b.task_id}"), pairs)

    def test_a_search_index_that_fails_does_not_undo_the_purge(self):
        task = self.make_deleted(days=60)
        self.purge_entities.side_effect = RuntimeError("opensearch down")
        self.purge()
        self.assertFalse(self.exists(task))

    def test_agent_conversations_about_the_task_are_left_and_counted(self):
        task = self.make_deleted(days=60)
        AgentSession.objects.create(
            team_id=str(self.team.team_id), user_id=str(self.user.id), task_id=task.task_id
        )
        out = self.purge()
        self.assertEqual(AgentSession.objects.filter(task_id=task.task_id).count(), 1)
        self.assertIn("1 agent conversation(s)", out)


class NothingLiveIsTouched(PurgeBase):
    def test_a_live_task_with_a_deleted_dependency_is_untouched_and_the_deleted_one_stays(self):
        deleted = self.make_deleted(days=60)
        live = self.make_task(status="Blocked")
        TaskDependency.objects.create(blocker_task=deleted, blocked_task=live, team=self.team)
        TaskMaster.objects.filter(pk=live.pk).update(
            ts_updated_at=timezone.now() - timedelta(days=3)
        )
        live.refresh_from_db()
        status_before = live.status  # the edge itself may have re-derived it: that is not ours
        history = TaskActivity.objects.filter(task=live).count()
        report = self.service(apply=True, limit=50)
        live.refresh_from_db()
        self.assertEqual(live.status, status_before)
        self.assertEqual(TaskActivity.objects.filter(task=live).count(), history)
        self.assertEqual(live.ts_updated_at.date(), (timezone.now() - timedelta(days=3)).date())
        self.assertTrue(TaskDependency.objects.filter(blocked_task=live).exists())
        self.assertTrue(self.exists(deleted))
        self.assertEqual(report.kept["still linked to a live task"], 1)

    def test_a_task_with_live_subtasks_stays_and_so_do_they(self):
        parent = self.make_deleted(days=60)
        child = self.make_task(status="Open", parent_task_id=parent.task_id)
        self.purge()
        self.assertTrue(self.exists(parent))
        self.assertTrue(self.exists(child))
        child.refresh_from_db()
        self.assertEqual(child.parent_task_id, parent.task_id)

    def test_a_deleted_parent_goes_with_its_deleted_children_over_two_runs_at_most(self):
        parent = self.make_deleted(days=60)
        child = self.make_deleted(days=60, parent_task_id=parent.task_id)
        grandchild = self.make_deleted(days=60, parent_task_id=child.task_id)
        self.service(apply=True, limit=50)
        self.service(apply=True, limit=50)
        for task in (parent, child, grandchild):
            self.assertFalse(self.exists(task))

    def test_a_task_that_is_a_root_of_a_live_task_stays(self):
        top = self.make_deleted(days=60)
        live = self.make_task(status="Open")
        TaskMaster.objects.filter(pk=live.pk).update(root_task_id=top.task_id)
        self.purge()
        self.assertTrue(self.exists(top))

    def test_the_teams_counts_and_a_milestones_work_stay_right(self):
        milestone_task = self.make_task(status="Open", title="m", is_milestone=True)
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=milestone_task, title="m"
        )
        live = [self.make_task(status="Open", milestone=milestone) for _ in range(3)]
        gone = self.make_deleted(days=60, milestone=milestone)

        from origin.services.milestone_rollup import milestone_work_q

        def snapshot():
            return (
                TaskMaster.objects.filter(team=self.team).exclude(status="Deleted").count(),
                TaskMaster.objects.filter(milestone_work_q(milestone)).count(),
                TaskMaster.objects.filter(project=self.project, status="Open").count(),
            )

        before = snapshot()
        self.purge()
        self.assertFalse(self.exists(gone))
        self.assertEqual(snapshot(), before)
        self.assertEqual(before[1], len(live))
        for task in live + [self.anchor, milestone_task]:
            self.assertTrue(self.exists(task))

    def test_a_task_in_another_team_is_judged_the_same_way_and_the_first_is_not_confused(self):
        project2 = ProjectMaster.objects.create(
            team=self.team, project_name="Q", owner=self.user, project_system_user=self.user
        )
        keep = self.make_deleted(days=10, project=project2)
        # its project's highest number is a live task, so only the age decides
        go = self.make_deleted(days=45, project=project2)
        TaskMaster.objects.filter(pk=keep.pk).update(project_task_number=500)
        self.purge()
        self.assertTrue(self.exists(keep))
        self.assertFalse(self.exists(go))

    def test_a_task_restored_while_the_purge_ran_is_kept(self):
        """The status is read again, under a lock, in the task's own transaction."""
        task = self.make_deleted(days=60)
        real = svc._reason
        calls = {"n": 0}

        def second_look_sees_a_restore(task_id, cutoff):
            calls["n"] += 1
            if calls["n"] == 2:
                TaskMaster.objects.filter(pk=task_id).update(status="Open")
            return real(task_id, cutoff)

        with mock.patch.object(svc, "_reason", side_effect=second_look_sees_a_restore):
            report = self.service(apply=True, limit=5)
        self.assertTrue(self.exists(task))
        self.assertEqual(report.purged, 0)
