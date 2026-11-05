"""A milestone's backing row carries the milestone's own id (MIP-1049).

A milestone is a `MilestoneMaster` plus a backing `TaskMaster` row
(`is_milestone=True`). Milestones made through the API give that row
`milestone=<the milestone>` and the code is written for it: the board and the
table route a click on a milestone row to the milestone preview only when the
row has it. The demo seeder (and `starter_seeder`, which seeds real customer
workspaces through the same helper) created the row first and never linked it,
so a milestone card in a seeded workspace opened the task preview instead.

Two halves: the seeders stop producing unlinked rows, and migration 0218 repairs
the rows already there.
"""

import importlib
from datetime import timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services.demo_seeder import create_demo_environment
from origin.services.milestone_rollup import milestone_work_q
from origin.services.milestone_service import create_milestone, ensure_backing_task
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

_migration = importlib.import_module("origin.migrations.0218_backfill_milestone_backing_link")


def assert_backing_rows_linked(test, team_id):
    """Every milestone of the team points at a backing row that points back."""
    milestones = list(MilestoneMaster.objects.filter(team_id=team_id).exclude(task=None))
    test.assertTrue(milestones, "the workspace has no milestones to check")
    for m in milestones:
        row = TaskMaster.objects.get(pk=m.task_id)
        test.assertTrue(row.is_milestone)
        test.assertEqual(
            row.milestone_id, m.milestone_id, f"backing row of {m.title!r} is not linked"
        )
    return milestones


class DemoSeederLinksBackingRows(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(
            username="demo_backing_link",
            email="demo_backing_link@example.com",
            password="x",
            is_demo=True,
        )
        cls.env = create_demo_environment(cls.user, short="backing")
        cls.team_id = cls.env["team_id"]

    def test_every_demo_milestone_row_carries_its_milestone(self):
        assert_backing_rows_linked(self, self.team_id)

    def test_the_row_is_not_counted_as_the_milestones_own_work(self):
        # The link puts the row inside the milestone's FK set; the progress count
        # has to cut it out, or every seeded milestone would read one task too many.
        for m in MilestoneMaster.objects.filter(team_id=self.team_id).exclude(task=None):
            work = TaskMaster.objects.filter(milestone_work_q(m))
            self.assertFalse(work.filter(pk=m.task_id).exists())
            self.assertTrue(work.exists())

    def test_linking_the_row_writes_no_activity(self):
        # A `.save()` would add a "milestone changed" entry to the history the
        # demo seeds for the milestone's own row.
        rows = TaskMaster.objects.filter(team_id=self.team_id, is_milestone=True)
        self.assertTrue(rows.exists())
        self.assertFalse(
            TaskActivity.objects.filter(task__in=rows, field_name="milestone_id").exists()
        )


class StarterSeederLinksBackingRows(BaseAPITestCase):
    def test_starter_workspace_milestones_are_linked(self):
        from unittest import mock

        self.authenticate()
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            resp = self.client.post(
                "/api/v2/team/create/",
                {
                    "team_name": "Acme",
                    "team_email": "acme@example.com",
                    "owner_id": str(self.user.id),
                    "with_starter": True,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, 201, resp.data)
        assert_backing_rows_linked(self, resp.data["teamDetails"]["teamId"])


class BackfillMigration(BaseAPITestCase):
    """0218: link the rows that are already there, and only those."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Genos-Alpha",
            code="MIP",
            owner=self.user,
            project_system_user=self.user,
        )

    def _milestone(self, title):
        return create_milestone(self.project, reporter_id=self.user.id, title=title)

    def _unlink(self, milestone):
        """Put a milestone back in the state the seeders left it in."""
        TaskMaster.objects.filter(pk=milestone.task_id).update(milestone=None)

    def _run(self):
        _migration.backfill_milestone_backing_link(apps, None)

    def _milestone_id_of(self, milestone):
        return TaskMaster.objects.get(pk=milestone.task_id).milestone_id

    def test_links_an_unlinked_backing_row(self):
        m = self._milestone("Launch")
        self._unlink(m)
        self.assertIsNone(self._milestone_id_of(m))

        self._run()

        self.assertEqual(self._milestone_id_of(m), m.milestone_id)

    def test_links_each_row_to_its_own_milestone(self):
        a, b = self._milestone("A"), self._milestone("B")
        self._unlink(a)
        self._unlink(b)

        self._run()

        self.assertEqual(self._milestone_id_of(a), a.milestone_id)
        self.assertEqual(self._milestone_id_of(b), b.milestone_id)

    def test_a_row_that_is_already_linked_is_left_alone(self):
        m = self._milestone("Launch")
        before = TaskMaster.objects.get(pk=m.task_id)

        self._run()

        after = TaskMaster.objects.get(pk=m.task_id)
        self.assertEqual(after.milestone_id, m.milestone_id)
        self.assertEqual(after.ts_updated_at, before.ts_updated_at)

    def test_does_not_touch_the_updated_time_or_write_history(self):
        m = self._milestone("Launch")
        self._unlink(m)
        stamp = timezone.now() - timedelta(days=30)
        TaskMaster.objects.filter(pk=m.task_id).update(ts_updated_at=stamp)
        history = TaskActivity.objects.filter(task_id=m.task_id).count()

        self._run()

        row = TaskMaster.objects.get(pk=m.task_id)
        self.assertEqual(row.milestone_id, m.milestone_id)
        self.assertEqual(row.ts_updated_at, stamp)
        self.assertEqual(TaskActivity.objects.filter(task_id=m.task_id).count(), history)

    def test_a_deleted_milestone_stays_detached(self):
        # Deleting a milestone detaches its tasks on purpose.
        m = self._milestone("Gone")
        self._unlink(m)
        MilestoneMaster.objects.filter(pk=m.pk).update(is_deleted=True)

        self._run()

        self.assertIsNone(self._milestone_id_of(m))

    def test_a_row_that_stopped_being_a_milestone_is_not_relinked(self):
        # A milestone turned back into a task (MIP-1042) is an ordinary task.
        m = self._milestone("Became a task")
        TaskMaster.objects.filter(pk=m.task_id).update(milestone=None, is_milestone=False)

        self._run()

        self.assertIsNone(self._milestone_id_of(m))

    def test_a_milestone_row_with_no_milestone_pointing_at_it_is_left_alone(self):
        orphan = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Orphan",
            content=[],
            is_milestone=True,
        )

        self._run()

        orphan.refresh_from_db()
        self.assertIsNone(orphan.milestone_id)

    def test_an_ordinary_task_is_never_given_a_milestone(self):
        self._milestone("Launch")
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Plain", content=[]
        )

        self._run()

        task.refresh_from_db()
        self.assertIsNone(task.milestone_id)

    def test_picks_the_live_milestone_when_a_deleted_one_also_points_at_the_row(self):
        live = self._milestone("Live")
        self._unlink(live)
        stale = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Stale",
            task_id=live.task_id,
            is_deleted=True,
        )
        # `stale` has the higher id: the live one must still win.
        self.assertGreater(stale.milestone_id, live.milestone_id)

        self._run()

        self.assertEqual(self._milestone_id_of(live), live.milestone_id)

    def test_an_existing_link_is_never_overwritten(self):
        # Scoped to NULL rows: whatever a row already points at stays.
        first, other = self._milestone("First"), self._milestone("Other")
        TaskMaster.objects.filter(pk=first.task_id).update(milestone=other)

        self._run()

        self.assertEqual(self._milestone_id_of(first), other.milestone_id)

    def test_two_live_milestones_on_one_row_resolve_to_the_newest(self):
        # Not expected to happen; the answer should still be deterministic.
        older = self._milestone("Older")
        self._unlink(older)
        newer = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Newer",
            task_id=older.task_id,
        )
        self.assertGreater(newer.milestone_id, older.milestone_id)

        self._run()

        self.assertEqual(self._milestone_id_of(older), newer.milestone_id)

    def test_running_it_twice_changes_nothing_more(self):
        m = self._milestone("Launch")
        self._unlink(m)
        self._run()
        first = TaskMaster.objects.get(pk=m.task_id)

        self._run()

        second = TaskMaster.objects.get(pk=m.task_id)
        self.assertEqual(second.milestone_id, first.milestone_id)
        self.assertEqual(second.ts_updated_at, first.ts_updated_at)

    def test_matches_what_the_api_creates(self):
        # The state the migration produces is the state `ensure_backing_task`
        # builds for a milestone made through the API.
        seeded = self._milestone("Seeded")
        self._unlink(seeded)
        self._run()
        api = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Api"
        )
        backing = ensure_backing_task(api)

        self.assertEqual(self._milestone_id_of(seeded), seeded.milestone_id)
        self.assertEqual(backing.milestone_id, api.milestone_id)
