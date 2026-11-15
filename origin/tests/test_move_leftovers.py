"""MIP-1597: the leftovers of the milestone convert / project move work (MIP-1583, api #881 + #882).

One class per item:

1. the single-task PUT move answers `notesNarrowed` as the milestone PATCH does;
2. a reporter outside the destination does not stop a move and is counted
   (`reportersOutsideDestination`); the reporter keeps reading the task;
3. the single-task PUT move refuses an assignee who is not on the destination, as the milestone
   PATCH does (`assignee_not_on_destination`);
4. the move counts (`tasks_moved` and the approval cards) are the live tasks only;
5. a convert makes no "changed in your AI app" notice (decided, pinned).
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.mcp_notice import RULES, build_notice
from origin.tests.test_agent_parity_milestones import ToolBase
from origin.tests.test_task_personal_note_roles_revoked import TaskNoteBase
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()
MILESTONE_URL = "/api/v2/milestone/"


class PutMoveBase(TaskNoteBase):
    """`owner` (the one who moves) and `author` are on the team; `stranger` is on the team and on no
    project. Alpha: owner + author. Beta (same team): owner only. `abroad`: a project of another
    team that only the owner is on, so author and stranger are outside it."""

    def setUp(self):
        super().setUp()
        self.alpha = self.project_of("Alpha", self.author)
        self.beta = self.project_of("Beta")
        other = TeamMaster.objects.create(
            team_name="Abroad", team_email="abroad@example.com", owner=self.owner
        )
        TeamMembers.objects.get_or_create(team=other, attendee=self.owner)
        self.abroad = ProjectMaster.objects.create(
            team=other, project_name="Abroad", owner=self.owner, project_system_user=self.owner
        )
        ProjectMembers.objects.create(team=other, project=self.abroad, attendee=self.owner)
        self.task = self.task_in(self.alpha)
        self.child = self.sub_task(self.task)

    def sub_task(self, parent, **kw):
        return TaskMaster.objects.create(
            team=self.team,
            project=parent.project,
            title="a sub-task",
            status="Open",
            parent_task_id=parent.task_id,
            root_task_id=parent.task_id,
            **kw,
        )

    def put(self, task, **extra):
        self.authenticate(self.owner)
        return self.client.put(
            "/api/v2/task/",
            {"team": str(self.team.team_id), "task_id": task.task_id, **extra},
            format="json",
        )

    def move(self, task, project, **extra):
        return self.put(task, project=project.project_id, **extra)

    def assert_not_moved(self):
        for row in (self.task, self.child):
            row.refresh_from_db()
            self.assertEqual(row.project_id, self.alpha.project_id)


# --------------------------------------------------------------------------------------------
# item 3: the same assignee check as the milestone PATCH
# --------------------------------------------------------------------------------------------


class PutMoveRechecksAssigneesTests(PutMoveBase):
    def test_an_assignee_who_is_not_on_the_destination_refuses_the_move(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        res = self.move(self.task, self.abroad)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["code"], "assignee_not_on_destination")
        self.assertIn("Nothing was changed", res.data["error"])
        self.task.refresh_from_db()
        self.assertIn(self.task.display_id, res.data["error"])  # named by its id
        self.assert_not_moved()
        self.assertEqual(self.task.assignee_id, self.author.id)

    def test_the_assignee_of_a_sub_task_that_goes_along_is_judged_too(self):
        TaskMaster.objects.filter(pk=self.child.pk).update(assignee=self.author)
        res = self.move(self.task, self.abroad)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["code"], "assignee_not_on_destination")
        self.child.refresh_from_db()
        self.assertIn(self.child.display_id, res.data["error"])
        self.assert_not_moved()

    def test_a_deleted_sub_task_does_not_block_the_move(self):
        TaskMaster.objects.filter(pk=self.child.pk).update(assignee=self.author, status="Deleted")
        gone = self.sub_task(self.task, assignee=self.author, is_deleted=True)
        res = self.move(self.task, self.abroad)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(TaskMaster.objects.get(pk=gone.pk).project_id, self.abroad.project_id)

    def test_the_assignee_the_same_save_sets_is_the_one_judged(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        res = self.move(self.task, self.abroad, assignee=str(self.owner.id))
        self.assertEqual(res.status_code, 200, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.owner.id)
        self.assertEqual(self.task.project_id, self.abroad.project_id)

    def test_a_null_assignee_in_the_payload_is_the_stored_one(self):
        # a null is dropped from the save (it clears nothing), so the stored person stays on it
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        res = self.move(self.task, self.abroad, assignee=None)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data["code"], "assignee_not_on_destination")

    def test_a_move_inside_a_team_is_not_newly_refused(self):
        # the author is on the team, not on Beta: assigned there is allowed, as a new task there is
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        res = self.move(self.task, self.beta)
        self.assertEqual(res.status_code, 200, res.data)
        self.task.refresh_from_db()
        self.assertEqual(self.task.project_id, self.beta.project_id)
        self.assertEqual(self.task.assignee_id, self.author.id)

    def test_a_save_that_is_not_a_move_judges_nobody(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        res = self.put(self.task, project=self.alpha.project_id, title="renamed")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertNotIn("notesNarrowed", res.data)
        self.assertNotIn("reportersOutsideDestination", res.data)

    def test_a_destination_the_caller_is_not_on_is_a_not_found_before_anything_is_judged(self):
        # no oracle: a project the caller cannot reach answers the same whoever is assigned
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        theirs = TeamMaster.objects.create(
            team_name="Theirs", team_email="theirs@example.com", owner=self.author
        )
        hidden = ProjectMaster.objects.create(
            team=theirs, project_name="Hidden", owner=self.author, project_system_user=self.author
        )
        res = self.move(self.task, hidden)
        self.assertEqual(res.status_code, 404, res.data)
        self.assertNotIn("code", res.data)
        self.assert_not_moved()


# --------------------------------------------------------------------------------------------
# item 4: the counts are the live tasks
# --------------------------------------------------------------------------------------------


class MoveCountsLiveTasksTests(ToolBase):
    """A task deleted in the app has the status "Deleted" and the row stays: a person does not see
    it, so the count of "tasks that go with it" does not include it (as `subTasksKept`)."""

    def test_the_tool_does_not_count_a_task_whose_status_is_deleted(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")  # not is_deleted
        out = self.move_tool(m)
        self.assertEqual(out["moved"]["tasks_moved"], 2)
        self.assertIn("with 2 task(s)", out["__summary__"])
        # it still moved: the count is what a person sees, not what is stored
        self.assertEqual(TaskMaster.objects.get(title="Second").project_id, self.dest.project_id)

    def test_the_tool_counts_every_task_when_none_is_deleted(self):
        m = self.roadmap_milestone()
        self.assertEqual(self.move_tool(m)["moved"]["tasks_moved"], 3)

    def card(self, args):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._friendly_arguments(args, "manage_milestone", ctx=self.ctx())

    def test_the_move_card_says_the_same(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")
        out = self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": self.dest.project_id}
        )
        self.assertEqual(out["current"]["move"]["tasks"], 2)

    def test_the_convert_card_says_the_same(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(status="Deleted")
        out = self.card({"action": "convert", "milestone_id": m.milestone_id})
        self.assertEqual(out["current"]["tasks"], 2)


