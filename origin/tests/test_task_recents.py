"""MIP-1063: the task sidebar's Recents are the tasks YOU opened, newest open first.

It used to be the most recently edited tasks of everyone on the team, so a
teammate's edit moved a task up and a task you only read never showed. The open is
recorded by the client when a task is shown; it is private to the user, and the list
re-checks project access on every read.
"""

from datetime import timedelta
from importlib import import_module
from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_recent_models import TaskRecent
from origin.services import task_recents
from origin.services.task_recents import RECENT_TASKS_CAP, RECENT_TASKS_PER_PROJECT
from origin.tests.cross_team_fixtures import CrossTeamTestCase

from .test_base import BaseAPITestCase

User = get_user_model()

OPEN = "/api/v2/task/recent/open/"
LIST = "/api/v2/task/recent/"


class RecentsBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project(self.team, "Genos", "GEN", self.user)
        # user2 owns a project `user` is not in.
        self.other = self.make_project(self.team, "Secret", "SEC", self.user2)

    def make_project(self, team, name, code, owner, members=()):
        project = ProjectMaster.objects.create(
            team=team, project_name=name, code=code, owner=owner, project_system_user=owner
        )
        for person in (owner, *members):
            ProjectMembers.objects.create(team=team, project=project, attendee=person)
        return project

    def make_task(self, project=None, title="T", **extra):
        project = project or self.project
        return TaskMaster.objects.create(
            team=project.team, project=project, title=title, status="Open", **extra
        )

    def open(self, task_id, user=None):
        self.authenticate(user or self.user)
        return self.client.post(OPEN, {"task_id": task_id}, format="json")

    def recents(self, user=None, team=None, expect=200):
        self.authenticate(user or self.user)
        res = self.client.get(LIST, {"team_id": str((team or self.team).team_id)})
        self.assertEqual(res.status_code, expect, res.content)
        return res.data

    def titles(self, **kw):
        return [r["title"] for r in self.recents(**kw)]


class RecordTests(RecentsBase):
    def test_opening_records_one_row_and_reopening_moves_it_to_the_top(self):
        a, b = self.make_task(title="A"), self.make_task(title="B")
        self.assertEqual(self.open(a.task_id).status_code, 200)
        self.assertEqual(self.open(b.task_id).status_code, 200)
        self.assertEqual(self.titles(), ["B", "A"])
        self.assertEqual(self.open(a.task_id).status_code, 200)
        self.assertEqual(self.titles(), ["A", "B"])
        self.assertEqual(TaskRecent.objects.filter(user=self.user).count(), 2)

    def test_it_is_the_callers_own_list(self):
        mine, theirs = self.make_task(title="Mine"), self.make_task(title="Theirs")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.open(mine.task_id)
        self.open(theirs.task_id, user=self.user2)
        self.assertEqual(self.titles(), ["Mine"])
        self.assertEqual(self.titles(user=self.user2), ["Theirs"])

    def test_an_edit_by_someone_else_does_not_move_a_task(self):
        a, b = self.make_task(title="A"), self.make_task(title="B")
        self.open(a.task_id)
        self.open(b.task_id)
        a.title = "A edited"
        a.save()
        self.assertEqual(self.titles(), ["B", "A edited"])

    def test_a_task_in_a_project_the_caller_is_not_in_is_not_found(self):
        secret = self.make_task(self.other, "Secret")
        self.assertEqual(self.open(secret.task_id).status_code, 404)
        self.assertEqual(self.open(10**9).status_code, 404)
        self.assertFalse(TaskRecent.objects.exists())

    def test_drafts_and_deleted_tasks_cannot_be_recorded(self):
        draft = self.make_task(title="draft", is_init_task=True)
        gone = self.make_task(title="gone", is_deleted=True)
        self.assertEqual(self.open(draft.task_id).status_code, 404)
        self.assertEqual(self.open(gone.task_id).status_code, 404)

    def test_malformed_ids_are_a_400_not_a_500(self):
        self.authenticate()
        for bad in ("abc", "", None, True, 1.5, [1], {"a": 1}):
            res = self.client.post(OPEN, {"task_id": bad}, format="json")
            self.assertEqual(res.status_code, 400, (bad, res.content))
        self.assertEqual(self.client.post(OPEN, {}, format="json").status_code, 400)

    def test_it_needs_a_login(self):
        self.unauthenticate()
        self.assertEqual(self.client.post(OPEN, {"task_id": 1}, format="json").status_code, 401)
        self.assertEqual(self.client.get(LIST, {"team_id": "x"}).status_code, 401)

    def test_a_milestone_backing_row_is_recorded_and_flagged(self):
        ms = self.make_task(title="Launch", is_milestone=True)
        self.open(ms.task_id)
        self.assertTrue(self.recents()[0]["isMilestone"])


class TrimTests(RecentsBase):
    def test_older_rows_beyond_the_cap_are_trimmed(self):
        tasks = [self.make_task(title=f"t{i}") for i in range(RECENT_TASKS_CAP + 3)]
        for t in tasks:
            task_recents.record_open(user_id=self.user.id, task=t)
        rows = TaskRecent.objects.filter(user=self.user)
        self.assertEqual(rows.count(), RECENT_TASKS_CAP)
        self.assertFalse(rows.filter(task=tasks[0]).exists())
        self.assertTrue(rows.filter(task=tasks[-1]).exists())

    def test_the_trim_never_touches_another_user(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        keep = self.make_task(title="theirs")
        task_recents.record_open(user_id=self.user2.id, task=keep)
        for i in range(RECENT_TASKS_CAP + 1):
            task_recents.record_open(user_id=self.user.id, task=self.make_task(title=f"t{i}"))
        self.assertTrue(TaskRecent.objects.filter(user=self.user2, task=keep).exists())


class ReadTests(RecentsBase):
    def test_at_most_twenty_per_project_newest_open_first(self):
        mine = [self.make_task(title=f"a{i}") for i in range(RECENT_TASKS_PER_PROJECT + 5)]
        other = self.make_project(self.team, "Second", "SEC2", self.user)
        second = self.make_task(other, "b0")
        for t in mine:
            self.open(t.task_id)
        self.open(second.task_id)
        got = self.recents()
        self.assertEqual(
            sum(1 for r in got if r["projectId"] == self.project.project_id),
            RECENT_TASKS_PER_PROJECT,
        )
        self.assertEqual(got[0]["title"], "b0")
        self.assertNotIn("a0", [r["title"] for r in got])

    def test_finished_tasks_stay_in_the_list(self):
        t = self.make_task(title="done")
        self.open(t.task_id)
        TaskMaster.objects.filter(pk=t.pk).update(status="Closed")
        got = self.recents()
        self.assertEqual([r["status"]["status"] for r in got], ["Closed"])

    def test_leaving_the_project_drops_it_and_rejoining_brings_it_back(self):
        t = self.make_task(title="T")
        self.open(t.task_id)
        # user2 owns the project in the base fixture of `other`; make `user` a plain member here.
        proj = self.make_project(self.team, "Joined", "JOI", self.user2, members=(self.user,))
        joined = self.make_task(proj, "J")
        self.open(joined.task_id)
        self.assertEqual(set(self.titles()), {"T", "J"})
        ProjectMembers.objects.filter(project=proj, attendee=self.user).delete()
        self.assertEqual(self.titles(), ["T"])
        ProjectMembers.objects.create(team=self.team, project=proj, attendee=self.user)
        self.assertEqual(set(self.titles()), {"T", "J"})

    def test_a_deleted_project_or_task_drops_out(self):
        t = self.make_task(title="T")
        self.open(t.task_id)
        TaskMaster.objects.filter(pk=t.pk).update(is_deleted=True)
        self.assertEqual(self.titles(), [])
        TaskMaster.objects.filter(pk=t.pk).update(is_deleted=False)
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        self.assertEqual(self.titles(), [])

    def test_the_row_has_the_search_endpoints_shape_plus_ts_opened(self):
        t = self.make_task(title="Shape")
        self.open(t.task_id)
        row = self.recents()[0]
        self.authenticate()
        search = self.client.get(
            "/api/v2/search/teamTasks/",
            {
                "team_id": str(self.team.team_id),
                "project_id": -1,
                "statuses": "open",
                "top_n": -1,
                "include_all": "true",
            },
        ).data[0]
        self.assertEqual(set(row), set(search) | {"tsOpened"})
        for key in set(search) - {"tsUpdated"}:
            self.assertEqual(row[key], search[key], key)

    def test_the_team_id_is_required_and_must_be_a_uuid(self):
        self.authenticate()
        res = self.client.get(LIST)
        self.assertEqual((res.status_code, res.data["error"]), (400, "team_id is required."))
        for bad in ("abc", "", "1"):
            res = self.client.get(LIST, {"team_id": bad})
            self.assertEqual(
                (res.status_code, res.data["error"]), (400, "team_id must be a valid id."), bad
            )

    def test_another_teams_workspace_shows_only_its_own_projects(self):
        other_team = self.make_other_team()
        t = self.make_task(title="Here")
        self.open(t.task_id)
        self.assertEqual(self.recents(team=other_team), [])
        self.assertEqual(self.titles(), ["Here"])

    def make_other_team(self):
        from origin.models.common.team_models import TeamMaster, TeamMembers

        team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        return team


class SharedProjectTests(CrossTeamTestCase):
    def test_a_guest_sees_a_shared_project_task_they_opened_from_their_own_workspace(self):
        self.active_project_grant()
        task = TaskMaster.objects.create(
            team=self.team_a, project=self.project, title="Shared", status="Open"
        )
        self.assertTrue(ProjectMembers.objects.filter(project=self.project, attendee=self.b_owner))
        self.authenticate(self.b_owner)
        # `team_id` here is the guest's own workspace; the task is team A's.
        res = self.client.post(OPEN, {"task_id": task.task_id, "team_id": str(self.team_b.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.get(LIST, {"team_id": str(self.team_b.team_id)})
        self.assertEqual([r["title"] for r in res.data], ["Shared"])

    def test_a_stranger_team_member_cannot_record_it(self):
        task = TaskMaster.objects.create(
            team=self.team_a, project=self.project, title="Host", status="Open"
        )
        self.authenticate(self.c_owner)
        self.assertEqual(self.client.post(OPEN, {"task_id": task.task_id}).status_code, 404)


class DeletionTests(RecentsBase):
    def test_deleting_the_task_removes_its_rows(self):
        t = self.make_task(title="T")
        self.open(t.task_id)
        TaskMaster.objects.filter(pk=t.pk).delete()
        self.assertFalse(TaskRecent.objects.exists())

    def test_deleting_the_user_removes_their_rows_and_only_theirs(self):
        leaver = User.objects.create_user(username="leaver", email="l@e.com", password="pass12345")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=leaver)
        t = self.make_task(title="T")
        self.open(t.task_id, user=leaver)
        self.open(t.task_id)
        leaver.delete()
        self.assertEqual(list(TaskRecent.objects.values_list("user_id", flat=True)), [self.user.id])


class SeedMigrationTests(RecentsBase):
    """The one-off backfill from a person's own edits in the last 30 days."""

    def seed(self):
        mod = import_module("origin.migrations.0220_task_recent")
        mod.seed_from_own_edits(apps, connection.schema_editor)

    def activity(self, task, actor, days_ago):
        row = TaskActivity.objects.create(
            team=self.team,
            project=task.project,
            task=task,
            actor=actor,
            action_type=TaskActivityActionType.TITLE,
        )
        TaskActivity.objects.filter(pk=row.pk).update(
            ts_created_at=timezone.now() - timedelta(days=days_ago)
        )
        return row

    def test_seeds_from_own_recent_edits_newest_first_and_one_row_per_task(self):
        old, mid, new = (self.make_task(title=t) for t in ("old", "mid", "new"))
        self.activity(old, self.user, 40)
        self.activity(mid, self.user, 10)
        self.activity(mid, self.user, 20)
        self.activity(new, self.user, 1)
        TaskRecent.objects.all().delete()
        self.seed()
        self.assertEqual(self.titles(), ["new", "mid"])
        self.assertEqual(TaskRecent.objects.filter(task=mid).count(), 1)

    def test_other_peoples_edits_are_not_mine_and_nothing_is_seeded_twice(self):
        t = self.make_task(title="T")
        self.activity(t, self.user2, 1)
        TaskRecent.objects.all().delete()
        self.seed()
        self.assertEqual(self.titles(), [])
        self.assertEqual(TaskRecent.objects.count(), 1)  # user2's own row
        self.seed()
        self.assertEqual(TaskRecent.objects.count(), 1)

    def test_at_most_the_cap_per_person_and_team_newest_first(self):
        mod = import_module("origin.migrations.0220_task_recent")
        tasks = [self.make_task(title=f"t{i}") for i in range(4)]
        for days_ago, task in zip((4, 3, 2, 1), tasks):
            self.activity(task, self.user, days_ago)
        TaskRecent.objects.all().delete()
        with mock.patch.object(mod, "SEED_CAP", 2):
            self.seed()
        self.assertEqual(self.titles(), ["t3", "t2"])

    def test_the_cap_is_per_team_so_a_busy_team_does_not_crowd_out_another(self):
        from origin.models.common.team_models import TeamMaster, TeamMembers

        mod = import_module("origin.migrations.0220_task_recent")
        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=elsewhere, attendee=self.user)
        there = self.make_project(elsewhere, "There", "THE", self.user)
        here_tasks = [self.make_task(title=f"here{i}") for i in range(3)]
        there_task = self.make_task(there, "there")
        for days_ago, task in zip((1, 2, 3), here_tasks):
            self.activity(task, self.user, days_ago)
        self.activity(there_task, self.user, 25)  # the oldest of all, inside the window
        TaskRecent.objects.all().delete()
        with mock.patch.object(mod, "SEED_CAP", 2):
            self.seed()
        kept = sorted(TaskRecent.objects.values_list("task__title", flat=True))
        self.assertEqual(kept, ["here0", "here1", "there"])

    def test_drafts_deleted_tasks_and_deleted_projects_are_not_seeded(self):
        draft = self.make_task(title="draft", is_init_task=True)
        gone = self.make_task(title="gone", is_deleted=True)
        dead_project = self.make_project(self.team, "Dead", "DEA", self.user)
        in_dead = self.make_task(dead_project, "in dead")
        live = self.make_task(title="live")
        for task in (draft, gone, in_dead, live):
            self.activity(task, self.user, 1)
        ProjectMaster.objects.filter(pk=dead_project.pk).update(is_deleted=True)
        TaskRecent.objects.all().delete()
        self.seed()
        self.assertEqual(sorted(TaskRecent.objects.values_list("task__title", flat=True)), ["live"])

    def test_a_task_created_since_is_not_in_the_past(self):
        t = self.make_task(title="T")
        TaskRecent.objects.all().delete()
        TaskActivity.objects.filter(task=t).delete()
        self.seed()
        self.assertFalse(TaskRecent.objects.exists())
