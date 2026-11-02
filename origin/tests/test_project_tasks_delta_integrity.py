"""What a browser needs to trust (or distrust) its cached project tasks (MIP-945).

`getProjectTasks?since=` answers "what changed", and a browser that took a
checkpoint without the rows behind it can never find that out from the delta
itself. Two things were missing:

  * a number that catches a wrong set even when the COUNT balances (one ghost
    row plus one missing row is still the right count);
  * a way back for a row whose `ts_updated_at` is stamped BEFORE its
    transaction commits: a sync that snapshots in between moves its
    checkpoint past a row it never saw, and no later delta sends it again.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/task/getProjectTasks/"


class ProjectTasksDeltaIntegrityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Delta", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _task(self, title, **extra):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open", **extra
        )

    def _get(self, **extra):
        params = {"team_id": str(self.team.team_id), "project_id": str(self.project.project_id)}
        params.update(extra)
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]

    def _stamp(self, task, when):
        """`.update()` skips `auto_now`, which is how a test sets the clock."""
        TaskMaster.objects.filter(pk=task.pk).update(ts_updated_at=when)

    # ---- idSum -----------------------------------------------------

    def test_a_delta_answer_carries_the_sum_of_the_ids_it_counts(self):
        a, b, c = self._task("a"), self._task("b"), self._task("c")
        gone = self._task("gone")
        TaskMaster.objects.filter(pk=gone.pk).update(is_deleted=True)
        self._task("bootstrap", is_init_task=True)

        data = self._get(since="2999-01-01T00:00:00Z")

        self.assertEqual(data["totalCount"], 3)
        self.assertEqual(data["idSum"], a.task_id + b.task_id + c.task_id)

    def test_the_sum_moves_when_the_count_does_not(self):
        """One ghost plus one missing row balances the count. The sum does not."""
        a, b = self._task("a"), self._task("b")
        before = self._get(since="2999-01-01T00:00:00Z")

        TaskMaster.objects.filter(pk=b.pk).update(is_deleted=True)
        c = self._task("c")
        after = self._get(since="2999-01-01T00:00:00Z")

        self.assertEqual(before["totalCount"], after["totalCount"])
        self.assertNotEqual(before["idSum"], after["idSum"])
        self.assertEqual(after["idSum"], a.task_id + c.task_id)

    def test_an_empty_project_sums_to_zero_not_null(self):
        data = self._get(since="2999-01-01T00:00:00Z")
        self.assertEqual(data["totalCount"], 0)
        self.assertEqual(data["idSum"], 0)

    def test_a_full_answer_is_its_own_proof(self):
        self._task("a")
        data = self._get()
        self.assertEqual(len(data["tasks"]), 1)
        self.assertNotIn("idSum", data)
        self.assertNotIn("totalCount", data)

    # ---- look-back --------------------------------------------------

    def test_a_row_stamped_just_before_the_checkpoint_is_sent_again(self):
        """Stamped 10 s before the client's checkpoint, committed after it."""
        since = timezone.now()
        late = self._task("committed late")
        self._stamp(late, since - timedelta(seconds=10))

        ids = [t["id"] for t in self._get(since=since.isoformat())["tasks"]]

        self.assertIn(str(late.task_id), ids)

    def test_a_row_long_before_the_checkpoint_is_not(self):
        since = timezone.now()
        old = self._task("old")
        self._stamp(old, since - timedelta(minutes=5))

        ids = [t["id"] for t in self._get(since=since.isoformat())["tasks"]]

        self.assertNotIn(str(old.task_id), ids)

    def test_a_row_after_the_checkpoint_is_sent(self):
        since = timezone.now() - timedelta(minutes=1)
        new = self._task("new")

        ids = [t["id"] for t in self._get(since=since.isoformat())["tasks"]]

        self.assertIn(str(new.task_id), ids)

    def test_a_tombstone_inside_the_look_back_is_sent_as_a_tombstone(self):
        since = timezone.now()
        gone = self._task("gone")
        TaskMaster.objects.filter(pk=gone.pk).update(
            is_deleted=True, ts_updated_at=since - timedelta(seconds=5)
        )

        rows = {t["id"]: t for t in self._get(since=since.isoformat())["tasks"]}

        self.assertTrue(rows[str(gone.task_id)]["isDeleted"])

    def test_the_checkpoint_the_client_stores_is_not_moved_back(self):
        """Only the QUERY looks back; `server_time` stays the moment of the
        snapshot, or every sync would drag the next one's window further."""
        before = timezone.now()
        res = self.client.get(
            URL,
            {
                "team_id": str(self.team.team_id),
                "project_id": str(self.project.project_id),
                "since": (before - timedelta(minutes=1)).isoformat(),
            },
        )
        self.assertGreaterEqual(
            timezone.datetime.fromisoformat(res.data["server_time"]).timestamp(),
            before.timestamp(),
        )
