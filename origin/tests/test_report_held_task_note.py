"""The "What to do today" report reads the briefing's `changed_for_me`, so the task note a
teammate wrote on a task the member holds (MIP-1285) reaches the report too, as a linked note."""

from __future__ import annotations

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.tests.test_schedule_jobs import JobBase


class TestTheReportCarriesTheHeldTasksNote(JobBase):
    def test_a_teammates_task_note_is_a_linked_changed_item(self):
        task = self.make_task("Fix Y", assignee=self.user2, reporter=self.user)
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=task.project,
            owner=self.user2,
            task=task,
            title="Y summary",
            body=[],
        )
        version = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=2,
            note_id=note.note_id,
            version_no=1,
            editor=self.user2,
            title="Y summary",
        )
        NoteVersionMaster.objects.filter(pk=version.pk).update(ts_updated_at=self.ago(hours=1))
        report = self.run_job("what_to_do_today")
        changed = self.section(report, "Changed in the last 24 hours")
        self.assertEqual(len(changed), 1)
        self.assertIn("Note · Y summary", changed[0]["text"])
        self.assertIn("by otheruser", changed[0]["text"])
        self.assertTrue(changed[0]["link"])
