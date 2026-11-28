"""A full save from a copy older than a project move must not move the task back (MIP-2524).

The app re-sends `project` on every save, from the copy it loaded. It also sends
`base_project`, the project that copy was loaded in: a `project` equal to it says the person
did not touch the project, so a different stored one is a move made elsewhere.
"""

from origin.models.task.task_models import TaskMaster

from .test_project_move_rules import ProjectMoveTestBase


class StaleProjectSaveTests(ProjectMoveTestBase):
    def setUp(self):
        super().setUp()
        self.milestone = self._make_milestone(self.project_a)
        self.task = self._make_task(
            self.project_a,
            title="before",
            milestone=self.milestone,
            parent_task_id=self.milestone.task_id,
        )
        # Moved by someone else (the app's picker, an agent, a rule) after the copy was read.
        self.assertEqual(self._move_task(self.task, self.project_b).status_code, 200)
        self.task.refresh_from_db()

    def _stale_full_save(self, **extra):
        """What the app sends from the old copy: every field of it, the old ones included."""
        return self._put(
            {
                "task_id": self.task.task_id,
                "team": str(self.team.team_id),
                "project": self.project_a.project_id,
                "title": "edited",
                "milestone": self.milestone.milestone_id,
                "parent_task_id": self.milestone.task_id,
                "tags": [{"tagName": "old"}],
                "custom_field_values": {},
                **extra,
            }
        )

    def test_a_stale_save_keeps_the_task_where_it_was_moved_and_keeps_the_edit(self):
        res = self._stale_full_save(base_project=self.project_a.project_id)

        self.assertEqual(res.status_code, 200, res.data)
        task = TaskMaster.objects.get(pk=self.task.task_id)
        self.assertEqual(task.project_id, self.project_b.project_id)
        self.assertEqual(task.title, "edited")
        # The old project's milestone, parent and tags do not come back with it.
        self.assertIsNone(task.milestone_id)
        self.assertIsNone(task.parent_task_id)
        self.assertFalse(task.tags)
        self.assertEqual(task.project_task_number, self.task.project_task_number)
        self.assertTrue(res.data["staleProject"])
        self.assertEqual(res.data["task"]["project"], self.project_b.project_id)
        self.assertIsNone(res.data["movedFromProjectId"])

    def test_the_base_may_be_a_string_as_the_project_is(self):
        res = self._stale_full_save(
            project=str(self.project_a.project_id), base_project=str(self.project_a.project_id)
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.project_b.project_id)

    def test_a_move_the_person_picked_still_moves(self):
        """`project` differs from the base: they chose it, even back to where it came from."""
        res = self._put(
            {
                "task_id": self.task.task_id,
                "team": str(self.team.team_id),
                "project": self.project_a.project_id,
                "base_project": self.project_b.project_id,
            }
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("staleProject", res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.project_a.project_id)

    def test_a_current_save_is_not_called_stale(self):
        res = self._put(
            {
                "task_id": self.task.task_id,
                "team": str(self.team.team_id),
                "project": self.project_b.project_id,
                "base_project": self.project_b.project_id,
                "title": "x",
            }
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("staleProject", res.data)

    def test_a_save_without_a_base_behaves_as_before(self):
        """An app from before MIP-2524, and any other client: the project they name is a move."""
        res = self._stale_full_save()
        self.assertEqual(res.status_code, 200, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.project_a.project_id)
