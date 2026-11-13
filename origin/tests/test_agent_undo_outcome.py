"""MIP-889: an undo keeps what it did on its step (`AgentStep.undo_outcome_json`).

Stored whatever `AGENT_TELL_MODEL_UNDO` says: by itself it is not model-visible, and the
undo's answer to the browser already carries the same. It is what lets the model be told,
later, which fields were put back and which were skipped. Field names and reason codes only.
"""

from __future__ import annotations

from origin.tests.test_agent_undo_endpoint import UndoBase


class UndoOutcomeTests(UndoBase):
    def write(self, **fields):
        return self.go("update_task", {"task_id": self.task.task_id, **fields})

    def test_what_was_restored_and_skipped_is_kept(self):
        run, step = self.write(title="Renamed", priority="High", status="WIP")
        self.someone_else_edits(self.task, priority="Critical")
        body = self.undone(run, step)
        step.refresh_from_db()
        self.assertEqual(
            step.undo_outcome_json,
            {
                "action": "restored",
                "restored": ["title", "status"],
                "skipped": [{"field": "priority", "reason": "changed_since"}],
            },
        )
        # The same as the browser was told.
        self.assertEqual(step.undo_outcome_json["restored"], body["restored"])
        self.assertEqual(step.undo_outcome_json["skipped"], body["skipped"])

    def test_the_undo_of_a_creation_is_a_deleted_outcome(self):
        run, step = self.go(
            "create_task", {"title": "New one", "project_id": self.project.project_id}
        )
        self.undone(run, step)
        step.refresh_from_db()
        self.assertEqual(step.undo_outcome_json["action"], "deleted")

    def test_a_refused_undo_stores_nothing(self):
        run, step = self.write(title="Renamed")
        self.someone_else_edits(self.task, title="Theirs")
        self.refused(run, step, "changed_since", 409)
        step.refresh_from_db()
        self.assertIsNone(step.undo_outcome_json)
        self.assertIsNone(step.undone_at)

    def test_an_unundone_step_has_none(self):
        _run, step = self.write(title="Renamed")
        self.assertIsNone(step.undo_outcome_json)
