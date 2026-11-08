"""The "What to do today" report reads the briefing's `changed_for_me`, so what the briefing
now tells the requester of a task (MIP-1283) reaches the report too, in the same item shape."""

from __future__ import annotations

from origin.tests.test_schedule_jobs import JobBase


class TestTheReportCarriesTheRequestersItem(JobBase):
    def test_a_task_made_from_my_thread_that_closed_is_a_linked_changed_item(self):
        channel = self.channel([self.user, self.user2])
        root = self.message(channel, self.user, "Please fix Y", at=self.ago(hours=9))
        task = self.make_task(
            "Fix Y", assignee=self.user2, reporter=self.user2, thread_id=str(root.id)
        )
        self.activity(task, self.user2, "status_changed", "WIP", "Closed", at=self.ago(hours=1))
        report = self.run_job("what_to_do_today")
        changed = self.section(report, "Changed in the last 24 hours")
        self.assertEqual(len(changed), 1)
        self.assertEqual(changed[0]["link"], self.task_link(task))
        self.assertIn("Closed", changed[0]["text"])
        self.assertIn("by otheruser", changed[0]["text"])
