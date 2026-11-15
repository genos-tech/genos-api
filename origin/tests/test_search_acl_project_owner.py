"""The project's owner is on a task's and a milestone's search ACL (MIP-1591).

`can_access_task` (REST) has always let the project's owner in with no `ProjectMembers` row; the
search ACL (`acl.task_acl_user_ids` at fetch time, the chunkers' `acl_user_ids` on the index) did
not, so an owner who is not also a listed member could open a task by id but never found it in
search. Creating a project writes the owner's member row, and the owner cannot leave, so the gap
is latent (it needs a row deleted by hand or a team removal), but the two rules should agree.

The owner is added only while still on the team (`active_team_member_ids`, which counts the team's
own owner), exactly like an assignee or reporter: a project owner who left the team has their
member row dropped by `drop_in_team_access` and stays `ProjectMaster.owner`, and search must not
bring them back (MIP-1371).

Chat and task-note ACLs are NOT changed: their REST rule (`chat_member_q`, `get_effective_role`)
asks `ProjectMembers` only, so the owner is not a reader there and the two rules already agree.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.acl_dirty import claim
from origin.search_engine.agent.acl import milestone_acl_user_ids, task_acl_user_ids
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks
from origin.search_engine.models import SearchAclDirty
from origin.services.member_removal import mark_search_acl_dirty
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()


class OwnerBase(BaseAPITestCase):
    """`powner` owns `project` (a team member, NOT the team's owner) and has no member row;
    `member` (= user2) is on it; `assignee` / `reporter` hold the task from outside the project."""

    def setUp(self):
        super().setUp()
        self.powner = User.objects.create_user(
            username="powner", email="powner@example.com", password="pw12345!"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.powner)
        self.assignee = User.objects.create_user(
            username="assignee", email="assignee@example.com", password="pw12345!"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.assignee)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Owned", owner=self.powner, project_system_user=self.powner
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="T",
            status="Open",
            assignee=self.assignee,
            reporter=self.user2,
        )

    def leave_team(self, person):
        TeamMembers.objects.filter(team=self.team, attendee=person).update(is_deleted=True)

    def acl(self):
        t = self.task
        return task_acl_user_ids(t.project_id, t.assignee_id, t.reporter_id)

    def chunk_acl(self):
        for entity in iter_task_chunks():
            if entity.entity_id == f"task:{self.task.task_id}":
                return set(entity.chunks[0].acl_user_ids)
        self.fail("task not chunked")


class TaskAclBuilderTests(OwnerBase):
    def test_the_owner_without_a_member_row_is_on_the_task_acl(self):
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.powner).exists()
        )
        self.assertIn(str(self.powner.id), self.acl())
        # ... and that is the REST answer too: the two rules agree for this person.
        self.assertTrue(can_access_task(self.task.task_id, self.powner.id))

    def test_only_the_owner_gains(self):
        # `teammate` is on the team but not on the project: still nobody.
        teammate = User.objects.create_user(
            username="mate", email="mate@example.com", password="pw12345!"
        )
        TeamMembers.objects.create(team=self.team, attendee=teammate)
        self.assertEqual(
            self.acl(),
            {str(self.powner.id), str(self.user2.id), str(self.assignee.id)},
        )

    def test_the_owner_is_there_with_no_assignee_and_no_reporter(self):
        # The agent's create_task / create_note guards ask exactly this.
        self.assertEqual(
            task_acl_user_ids(self.project.project_id, None, None),
            {str(self.powner.id), str(self.user2.id)},
        )

    def test_an_owner_who_left_the_team_stays_off_it(self):
        self.leave_team(self.powner)
        self.assertNotIn(str(self.powner.id), self.acl())

    def test_the_teams_own_owner_counts_even_without_a_team_row(self):
        # A project owned by the TEAM's owner (self.user), who has no TeamMembers row.
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Mine", owner=self.user, project_system_user=self.user
        )
        self.assertEqual(task_acl_user_ids(project.project_id, None, None), {str(self.user.id)})

    def test_a_project_with_no_owner_and_no_project_are_unchanged(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(owner=None)
        self.assertEqual(self.acl(), {str(self.user2.id), str(self.assignee.id)})
        self.assertEqual(task_acl_user_ids(None, self.assignee.id, None), {str(self.assignee.id)})

    def test_the_queries_do_not_depend_on_who_holds_the_task(self):
        with CaptureQueriesContext(connection) as holders:
            task_acl_user_ids(self.project.project_id, self.assignee.id, self.user2.id)
        with CaptureQueriesContext(connection) as nobody:
            task_acl_user_ids(self.project.project_id, None, None)
        self.assertEqual(len(holders), len(nobody))


class TaskChunkerTests(OwnerBase):
    def test_the_owner_without_a_member_row_is_on_the_indexed_acl(self):
        self.assertIn(str(self.powner.id), self.chunk_acl())

    def test_the_index_and_the_builder_say_the_same(self):
        self.assertEqual(self.chunk_acl(), self.acl())

    def test_an_owner_who_left_the_team_is_not_indexed(self):
        self.leave_team(self.powner)
        self.assertNotIn(str(self.powner.id), self.chunk_acl())
        self.assertEqual(self.chunk_acl(), self.acl())

    def test_a_task_outside_any_project_gets_no_owner(self):
        loose = TaskMaster.objects.create(
            team=self.team, title="L", status="Open", assignee=self.assignee, reporter=self.user2
        )
        for entity in iter_task_chunks():
            if entity.entity_id == f"task:{loose.task_id}":
                self.assertEqual(
                    set(entity.chunks[0].acl_user_ids), {str(self.assignee.id), str(self.user2.id)}
                )
                return
        self.fail("task not chunked")


class MilestoneTests(OwnerBase):
    def setUp(self):
        super().setUp()
        self.milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Q4 launch"
        )

    def chunk_acl(self):
        for entity in iter_milestone_chunks():
            if entity.entity_id == f"milestone:{self.milestone.milestone_id}":
                return set(entity.chunks[0].acl_user_ids)
        self.fail("milestone not chunked")

    def test_the_owner_is_on_the_milestone_acl(self):
        acl = milestone_acl_user_ids(self.project.project_id, self.milestone.milestone_id)
        self.assertEqual(acl, {str(self.powner.id), str(self.user2.id)})

    def test_the_owner_is_on_the_indexed_milestone_acl(self):
        self.assertIn(str(self.powner.id), self.chunk_acl())

    def test_an_owner_who_left_the_team_is_off_both(self):
        self.leave_team(self.powner)
        acl = milestone_acl_user_ids(self.project.project_id, self.milestone.milestone_id)
        self.assertNotIn(str(self.powner.id), acl)
        self.assertNotIn(str(self.powner.id), self.chunk_acl())


class RefreshMarksTests(OwnerBase):
    """The owner is on the index with no member row, so no membership write marks the project
    when the owner changes: the two places that change it, and the team removal, mark it."""

    def setUp(self):
        super().setUp()
        SearchAclDirty.objects.all().delete()

    def test_removing_the_owner_from_the_team_marks_the_projects_they_own(self):
        mark_search_acl_dirty(self.team.team_id, self.powner.id)
        self.assertIn(self.project.project_id, claim().project_ids)

    def test_it_does_not_mark_a_project_that_is_nothing_to_them(self):
        # The team's owner owns no project here and holds none of its tasks.
        mark_search_acl_dirty(self.team.team_id, self.user.id)
        self.assertNotIn(self.project.project_id, claim().project_ids)

    def test_handing_the_project_over_marks_it(self):
        self.authenticate(self.powner)
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "owner_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.project.refresh_from_db()
        self.assertEqual(self.project.owner_id, self.user2.id)
        self.assertIn(self.project.project_id, claim().project_ids)
        # The old owner has no member row and is no longer the owner: off both rules.
        self.assertNotIn(str(self.powner.id), self.acl())
        self.assertNotIn(str(self.powner.id), self.chunk_acl())
