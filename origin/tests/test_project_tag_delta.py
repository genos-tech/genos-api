"""A tag rename or delete must reach a browser that refreshes by delta (MIP-970).

`getProjectTasks?since=` returns the rows whose `ts_updated_at` is newer than
the browser's checkpoint. `ts_updated_at` is `auto_now`, and Django writes an
`auto_now` column only when the save names it: `task.save(update_fields=["tags"])`
rewrote the tags and left the stamp where it was. A browser that already held
the project therefore kept the old tag name (or the deleted tag) on its tasks
until it did a full load, and the count / id-sum check could not catch it
because the set of task ids had not changed.

Every test ages the tasks to well before the checkpoint, so only a stamp made
by the tag edit itself can put a task back inside the delta window (the
30-second look-back would otherwise hide the bug for a task just created).
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

DELTA = "/api/v2/task/getProjectTasks/"
TAG = "/api/v2/project/tag/"

BUG = {"tagName": "Bug", "tagColor": "#ff0000", "tagTextColor": "#ffffff"}
FEATURE = {"tagName": "Feature", "tagColor": "#00ff00", "tagTextColor": "#000000"}


class TagEditsReachTheDeltaTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Tags", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        for n, tag in enumerate((BUG, FEATURE), start=1):
            ProjectTags.objects.create(
                team=self.team,
                project=self.project,
                tag_id=n,
                tag_name=tag["tagName"],
                tag_color=tag["tagColor"],
                tag_text_color=tag["tagTextColor"],
            )
        self.bug_task = self._task("has Bug", [BUG])
        self.mixed_task = self._task("has both", [FEATURE, BUG])
        self.feature_task = self._task("has Feature", [FEATURE])
        self.plain_task = self._task("no tags", [])

        # The browser synced five minutes ago and every task was last written
        # ten minutes ago: nothing is inside its window until something writes.
        now = timezone.now()
        self.checkpoint = now - timedelta(minutes=5)
        TaskMaster.objects.filter(project=self.project).update(
            ts_updated_at=now - timedelta(minutes=10)
        )

    def _task(self, title, tags):
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open", tags=tags
        )

    def _delta(self):
        res = self.client.get(
            DELTA,
            {
                "team_id": str(self.team.team_id),
                "project_id": str(self.project.project_id),
                "since": self.checkpoint.isoformat(),
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {row["id"]: row for row in res.data["data"]["tasks"]}

    def _id(self, task):
        return str(task.task_id)

    def test_the_fixture_starts_with_an_empty_delta(self):
        """Without this a green test below could just mean everything is "new"."""
        self.assertEqual(self._delta(), {})

    def test_a_renamed_tag_reaches_the_delta_on_every_task_that_carries_it(self):
        res = self.client.put(
            TAG,
            {
                "project_id": self.project.project_id,
                "old_tag_name": "Bug",
                "tag_name": "Defect",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        rows = self._delta()

        self.assertEqual(set(rows), {self._id(self.bug_task), self._id(self.mixed_task)})
        self.assertEqual([t["tagName"] for t in rows[self._id(self.bug_task)]["tags"]], ["Defect"])
        self.assertEqual(
            [t["tagName"] for t in rows[self._id(self.mixed_task)]["tags"]],
            ["Feature", "Defect"],
        )
        self.assertEqual(rows[self._id(self.bug_task)]["concatTags"], "/Defect/")

    def test_a_recoloured_tag_reaches_the_delta(self):
        res = self.client.put(
            TAG,
            {
                "project_id": self.project.project_id,
                "old_tag_name": "Bug",
                "tag_color": "#0000ff",
                "tag_text_color": "#eeeeee",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        rows = self._delta()

        self.assertEqual(set(rows), {self._id(self.bug_task), self._id(self.mixed_task)})
        self.assertEqual(
            rows[self._id(self.bug_task)]["tags"],
            [{"tagName": "Bug", "tagColor": "#0000ff", "tagTextColor": "#eeeeee"}],
        )

    def test_a_deleted_tag_reaches_the_delta(self):
        res = self.client.delete(
            TAG,
            {"project_id": self.project.project_id, "tag_name": "Bug"},
            format="json",
        )
        self.assertEqual(res.status_code, 204)

        rows = self._delta()

        self.assertEqual(set(rows), {self._id(self.bug_task), self._id(self.mixed_task)})
        self.assertEqual(rows[self._id(self.bug_task)]["tags"], [])
        self.assertEqual(
            [t["tagName"] for t in rows[self._id(self.mixed_task)]["tags"]], ["Feature"]
        )

    def test_a_task_the_edit_did_not_touch_stays_out_of_the_delta(self):
        """The stamp is for rows whose tags changed, not the whole project."""
        self.client.put(
            TAG,
            {
                "project_id": self.project.project_id,
                "old_tag_name": "Bug",
                "tag_name": "Defect",
            },
            format="json",
        )

        rows = self._delta()

        self.assertNotIn(self._id(self.feature_task), rows)
        self.assertNotIn(self._id(self.plain_task), rows)
