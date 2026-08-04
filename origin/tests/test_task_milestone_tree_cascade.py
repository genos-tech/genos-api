"""Moving a task between milestones must move its whole sub-tree.

A task's place in the hierarchy is denormalized across three columns:
`milestone_id` (which milestone owns it), `parent_task_id` (its edge to
the row above), and `root_task_id` (the top of the chain it belongs to).
Rollups read the first, the table nests by the second, and every
"rooted-at-chain-top" surface — task diagram, sub-task drawer, the
diagram keyboard shortcut — anchors on the third.

The milestone move used to cascade only `milestone_id` / `sprint_id`,
while rewriting the moved task's own `root_task_id` to the new
milestone's backing task. Descendants were left anchored to the OLD
milestone, which is the bug these tests pin: opening a sub-task's
diagram anchored on a tree the sub-task is no longer part of, so the
parent and the milestone were nowhere in it.

The reparent cases matter for the same reason from the other direction:
dragging a task under a new parent (the diagram's structure edges send
`parent_task_id` with no `milestone` key) changes which milestone and
which chain the task lives in, so the sub-tree has to follow that too.
"""

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster

User = get_user_model()


class MilestoneTreeCascadeTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="tree", email="tree@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

        self.team = TeamMaster.objects.create(
            team_name="Tree Team", team_email="tree@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Tree",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        self.milestone_a = self._make_milestone("MS A")
        self.milestone_b = self._make_milestone("MS B")

    def _make_milestone(self, title):
        backing = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=title,
            status="Open",
            is_milestone=True,
        )
        return MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            task=backing,
            title=title,
            reporter=self.user,
        )

    def _make_task(self, title, **kwargs):
        """Create through the ORM so the `set_root_task_id` signal fills
        `root_task_id` by walking up, exactly as a real create does."""
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open", **kwargs
        )

    def _chain_in_milestone_a(self):
        """Milestone A → task_a → subtask_a → sub_subtask_a.

        The shape from the bug report, built the way the product builds
        it: each row inherits its milestone from the parent at create
        time, so all three start rooted at A's backing task.
        """
        task_a = self._make_task(
            "task-a",
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=self.milestone_a.task_id,
        )
        subtask_a = self._make_task(
            "subtask-a",
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=task_a.task_id,
        )
        sub_subtask_a = self._make_task(
            "sub-subtask-a",
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=subtask_a.task_id,
        )
        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(row.root_task_id, self.milestone_a.task_id)
        return task_a, subtask_a, sub_subtask_a

    def _put(self, **payload):
        res = self.client.put(
            "/api/v2/task/",
            {"project": self.project.project_id, **payload},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

    # ── The reported bug ──────────────────────────────────────────────

    def test_milestone_move_cascades_milestone_to_every_descendant(self):
        """Pins the half that already worked, so a fix to roots can't
        regress it."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, milestone=self.milestone_b.milestone_id)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(
                row.milestone_id,
                self.milestone_b.milestone_id,
                f"{row.title} kept the old milestone",
            )

    def test_milestone_move_cascades_root_to_every_descendant(self):
        """The bug: descendants stayed anchored to milestone A's backing
        task, so a sub-task's diagram opened on a tree it had left."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, milestone=self.milestone_b.milestone_id)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(
                row.root_task_id,
                self.milestone_b.task_id,
                f"{row.title} is still rooted at the old milestone",
            )

    def test_milestone_move_keeps_the_parent_edges_intact(self):
        """Cascading the root must not flatten the chain — only the
        moved task's own parent changes (to B's backing task)."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, milestone=self.milestone_b.milestone_id)

        task_a.refresh_from_db()
        subtask_a.refresh_from_db()
        sub_subtask_a.refresh_from_db()
        self.assertEqual(task_a.parent_task_id, self.milestone_b.task_id)
        self.assertEqual(subtask_a.parent_task_id, task_a.task_id)
        self.assertEqual(sub_subtask_a.parent_task_id, subtask_a.task_id)

    def test_cascade_reaches_deeper_than_three_levels(self):
        """Depth isn't special-cased anywhere in the product, so the
        cascade shouldn't stop at the depth the bug report happened to
        use."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()
        deepest = sub_subtask_a
        for i in range(4):
            deepest = self._make_task(
                f"level-{i}",
                milestone_id=self.milestone_a.milestone_id,
                parent_task_id=deepest.task_id,
            )

        self._put(task_id=task_a.task_id, milestone=self.milestone_b.milestone_id)

        deepest.refresh_from_db()
        self.assertEqual(deepest.milestone_id, self.milestone_b.milestone_id)
        self.assertEqual(deepest.root_task_id, self.milestone_b.task_id)

    # ── Clearing the milestone ────────────────────────────────────────

    def test_clearing_milestone_reroots_the_subtree_on_the_task(self):
        """With no milestone the chain's top is the task itself, so its
        descendants must be rooted there — not left pointing at the
        milestone they were just removed from."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, milestone=None)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertIsNone(row.milestone_id, f"{row.title} kept a milestone")
            self.assertEqual(
                row.root_task_id,
                task_a.task_id,
                f"{row.title} is still rooted at the removed milestone",
            )

    # ── Reparenting without a milestone key (diagram edges) ───────────

    def test_reparent_into_another_milestone_cascades_to_descendants(self):
        """The diagram sends `parent_task_id` alone. Dropping the task
        under a row in another milestone moves it there, so its milestone,
        its root, and its whole sub-tree have to follow."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()
        host = self._make_task(
            "host",
            milestone_id=self.milestone_b.milestone_id,
            parent_task_id=self.milestone_b.task_id,
        )

        self._put(task_id=task_a.task_id, parent_task_id=host.task_id)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(
                row.milestone_id,
                self.milestone_b.milestone_id,
                f"{row.title} kept the old milestone after reparenting",
            )
            self.assertEqual(
                row.root_task_id,
                self.milestone_b.task_id,
                f"{row.title} kept the old root after reparenting",
            )
        self.assertEqual(task_a.parent_task_id, host.task_id)

    def test_reparent_onto_milestone_backing_task_links_that_milestone(self):
        """Dropping a task directly onto a milestone row is how the table
        and diagram move it into that milestone."""
        task_a, subtask_a, _ = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, parent_task_id=self.milestone_b.task_id)

        task_a.refresh_from_db()
        subtask_a.refresh_from_db()
        self.assertEqual(task_a.milestone_id, self.milestone_b.milestone_id)
        self.assertEqual(task_a.root_task_id, self.milestone_b.task_id)
        self.assertEqual(subtask_a.milestone_id, self.milestone_b.milestone_id)
        self.assertEqual(subtask_a.root_task_id, self.milestone_b.task_id)

    def test_reparent_to_top_level_reroots_on_itself(self):
        """Detaching a task from its parent makes it its own chain top."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()

        self._put(task_id=task_a.task_id, parent_task_id=None)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(
                row.root_task_id,
                task_a.task_id,
                f"{row.title} should be rooted on the detached task",
            )
        self.assertIsNone(task_a.parent_task_id)

    def test_reparent_within_the_same_milestone_leaves_links_alone(self):
        """A plain re-order inside one milestone changes the parent edge
        and nothing else."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()
        sibling = self._make_task(
            "sibling",
            milestone_id=self.milestone_a.milestone_id,
            parent_task_id=self.milestone_a.task_id,
        )

        self._put(task_id=task_a.task_id, parent_task_id=sibling.task_id)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(row.milestone_id, self.milestone_a.milestone_id)
            self.assertEqual(row.root_task_id, self.milestone_a.task_id)
        self.assertEqual(task_a.parent_task_id, sibling.task_id)

    def test_a_task_cannot_be_reparented_under_its_own_descendant(self):
        """Defensive: the payload can name any task id, and a cycle would
        strand the whole sub-tree outside every tree walk."""
        task_a, subtask_a, _ = self._chain_in_milestone_a()

        self.client.put(
            "/api/v2/task/",
            {
                "project": self.project.project_id,
                "task_id": task_a.task_id,
                "parent_task_id": subtask_a.task_id,
            },
            format="json",
        )

        task_a.refresh_from_db()
        self.assertNotEqual(task_a.parent_task_id, subtask_a.task_id)

    # ── Repairing rows the old cascade already broke ───────────────────

    def test_backfill_command_repairs_roots_left_stale_by_the_old_cascade(self):
        """Fixing the cascade doesn't retroactively fix rows it already
        got wrong, and nothing recomputes `root_task_id` on read. The
        existing backfill command is the repair path, so pin that it
        handles this shape."""
        task_a, subtask_a, sub_subtask_a = self._chain_in_milestone_a()
        # Exactly what a pre-fix milestone move left behind: the moved
        # task re-rooted on B, its descendants still on A.
        TaskMaster.objects.filter(task_id=task_a.task_id).update(
            milestone_id=self.milestone_b.milestone_id,
            parent_task_id=self.milestone_b.task_id,
            root_task_id=self.milestone_b.task_id,
        )
        TaskMaster.objects.filter(task_id__in=[subtask_a.task_id, sub_subtask_a.task_id]).update(
            milestone_id=self.milestone_b.milestone_id
        )

        call_command("backfill_root_task_id", verbosity=0)

        for row in (task_a, subtask_a, sub_subtask_a):
            row.refresh_from_db()
            self.assertEqual(
                row.root_task_id,
                self.milestone_b.task_id,
                f"{row.title} was not repaired",
            )
