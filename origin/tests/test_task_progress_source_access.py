"""MIP-2550: the progress feed leaves out the changes of a task the reader cannot open.

A `TaskProgressFact` stays with the old parent when its source task later moves, so without
this the feed (REST and MCP) would show the moved task's CURRENT title and display id.
"""

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp.progress.get_progress_history import GET_PROGRESS_HISTORY
from origin.views.utils.scope_guards import can_access_task, readable_tasks_q

URL = "/api/v2/task/progress/"
SECRET = "Quiet acquisition"


class ProgressSourceAccessTests(BaseAPITestCase):
    """`user` is on both projects, `user2` only on the parent's."""

    def setUp(self):
        super().setUp()
        for member in (self.user, self.user2):
            TeamMembers.objects.get_or_create(team=self.team, attendee=member)
        self.open = self._project("Open", [self.user, self.user2])
        self.vault = self._project("Vault", [self.user])
        with self.captureOnCommitCallbacks(execute=True):
            self.parent = self._task("Parent", self.open)
            self.child = self._task("Child", self.open, parent_task_id=self.parent.task_id)
            self.stays = self._task("Stays", self.open, parent_task_id=self.parent.task_id)
        with self.captureOnCommitCallbacks(execute=True):
            for task in (self.child, self.stays):
                task.status = "WIP"
                task.save()
        # The child leaves for a project `user2` is not on, and is renamed there.
        with self.captureOnCommitCallbacks(execute=True):
            self.child.project = self.vault
            self.child.title = SECRET
            self.child.parent_task_id = None
            self.child.save()
        self.child.refresh_from_db()

    def _project(self, name, members):
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, owner=self.user, project_system_user=self.user
        )
        for member in members:
            ProjectMembers.objects.create(team=self.team, project=project, attendee=member)
        return project

    def _task(self, title, project, **extra):
        return TaskMaster.objects.create(
            team=self.team,
            project=project,
            title=title,
            reporter=self.user,
            status="Open",
            **extra,
        )

    def _rest(self, viewer, **params):
        self.authenticate(viewer)
        res = self.client.get(URL, {"task_id": self.parent.task_id, "limit": 100, **params})
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def _mcp(self, viewer, **args):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(viewer.id))
        return GET_PROGRESS_HISTORY.run({"task_id": self.parent.task_id, "limit": 100, **args}, ctx)

    def test_the_app_feed_leaves_out_a_task_the_reader_cannot_open(self):
        entries = self._rest(self.user2)["entries"]
        self.assertIn(str(self.stays.task_id), {e["sourceTask"]["id"] for e in entries})
        self.assertNotIn(str(self.child.task_id), {e["sourceTask"]["id"] for e in entries})
        self.assertNotIn(SECRET, repr(entries))
        self.assertNotIn(self.child.display_id, repr(entries))

    def test_the_tool_leaves_out_a_task_the_reader_cannot_open(self):
        result = self._mcp(self.user2)
        ids = {e["task_id"] for e in result["entries"]}
        self.assertIn(self.stays.task_id, ids)
        self.assertNotIn(self.child.task_id, ids)
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn(self.child.display_id, repr(result))

    def test_a_reader_of_both_projects_sees_it_as_before(self):
        rest = self._rest(self.user)["entries"]
        self.assertIn(SECRET, {e["sourceTask"]["title"] for e in rest})
        self.assertIn(self.child.task_id, {e["task_id"] for e in self._mcp(self.user)["entries"]})

    def test_the_assignee_of_the_moved_task_still_reads_it(self):
        TaskMaster.objects.filter(pk=self.child.pk).update(assignee=self.user2)
        self.assertIn(SECRET, repr(self._rest(self.user2)))
        self.assertIn(SECRET, repr(self._mcp(self.user2)))

    def test_a_deleted_task_the_reader_could_open_stays(self):
        TaskMaster.objects.filter(pk=self.stays.pk).update(is_deleted=True)
        ids = {e["sourceTask"]["id"] for e in self._rest(self.user2)["entries"]}
        self.assertIn(str(self.stays.task_id), ids)

    def test_pages_stay_full_and_the_cursor_walks_what_is_left(self):
        everything = [e["factId"] for e in self._rest(self.user2)["entries"]]
        self.assertGreater(len(everything), 1)
        walked, cursor = [], None
        while True:
            page = self._rest(self.user2, limit=1, **({"cursor": cursor} if cursor else {}))
            self.assertEqual(len(page["entries"]), 1)
            walked += [e["factId"] for e in page["entries"]]
            cursor = page["nextCursor"]
            if cursor is None:
                break
        self.assertEqual(walked, everything)
        one = self._mcp(self.user2, limit=len(everything))
        self.assertEqual(len(one["entries"]), len(everything))
        self.assertFalse(one["has_more"])

    def test_the_query_count_does_not_grow_with_the_page(self):
        def count(limit):
            self.authenticate(self.user2)
            with CaptureQueriesContext(connection) as seen:
                self.client.get(URL, {"task_id": self.parent.task_id, "limit": limit})
            return len(seen)

        before = count(100)
        with self.captureOnCommitCallbacks(execute=True):
            for i in range(5):
                task = self._task(f"more {i}", self.open, parent_task_id=self.parent.task_id)
                task.status = "WIP"
                task.save()
        self.assertEqual(count(100), before)

    def test_the_query_form_agrees_with_can_access_task(self):
        def person(name, **team):
            user = CustomUser.objects.create_user(
                email=f"{name}@example.com", password="x", username=name
            )
            if team:
                TeamMembers.objects.create(team=self.team, attendee=user, **team)
            return user

        stranger = person("stranger")
        teammate = person("teammate", is_deleted=False)
        removed = person("removed", is_deleted=True)
        guest = person("guest")
        ProjectMembers.objects.create(team=self.team, project=self.open, attendee=guest)
        team_owner = TeamMaster.objects.get(pk=self.team.pk).owner
        loose = self._task("no project", None, assignee=teammate)
        gone = self._project("Gone", [self.user, teammate])
        in_gone = self._task("in gone", gone, assignee=teammate)
        ProjectMaster.objects.filter(pk=gone.pk).update(is_deleted=True)
        held = [
            self._task(f"held by {p.username}", self.vault, assignee=p)
            for p in (teammate, removed, guest, stranger)
        ]
        tasks = [self.parent, self.child, loose, in_gone, *held]
        people = [self.user, self.user2, stranger, teammate, removed, guest, team_owner]
        for who in people:
            got = set(
                TaskMaster.objects.filter(
                    readable_tasks_q(who.id), pk__in=[t.pk for t in tasks]
                ).values_list("pk", flat=True)
            )
            want = {t.pk for t in tasks if can_access_task(t.pk, who.id)}
            self.assertEqual(got, want, who.username)
