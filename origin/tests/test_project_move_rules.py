"""Who may change a task's project, and what has to travel with it.

Three bugs shared one cause and one rule.

**The cause.** A move rewrote `project_id` and nothing else that scopes a
task. `GetProjectTasksView` filters on `team` AND `project`, so a task
moved into an externally shared project — owned by the HOST team, while
the row kept the guest's — matched neither project's list and simply
disappeared. Its notes, which carry their own copy of both columns, went
with it.

**The rule.** Only a milestone or a ROOT task — one with no parent task at
all — changes project, and everything filed underneath comes along. Anything
moved alone kept a `parent_task_id` in the project it left, and the
destination's table nests rows under their parent, so it was invisible in
both places. A task living in a milestone counts as filed underneath: it
hangs off the milestone's backing row, so the milestone owns which project
it is in. A milestone could not move at all; now it can, and it lands with
no sprint because sprints are defined per project.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskDependency, TaskMaster

User = get_user_model()


class ProjectMoveTestBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="mover", email="mover@move.test", password="testpass123"
        )
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {RefreshToken.for_user(self.user).access_token}"
        )
        self.team = TeamMaster.objects.create(
            team_name="Home", team_email="home@move.test", owner=self.user
        )
        self.project_a = self._make_project(self.team, "Source")
        self.project_b = self._make_project(self.team, "Destination")

    def _make_project(self, team, name, *, join=True, owner=None):
        project = ProjectMaster.objects.create(
            team=team,
            project_name=name,
            owner=owner or team.owner,
            project_system_user=team.owner,
        )
        if join:
            ProjectMembers.objects.create(team=team, project=project, attendee=self.user)
        return project

    def _make_task(self, project, **kwargs):
        return TaskMaster.objects.create(
            team=project.team,
            project=project,
            title=kwargs.pop("title", "T"),
            status="Open",
            **kwargs,
        )

    def _make_milestone(self, project, title="MS", sprint=None):
        milestone = MilestoneMaster.objects.create(
            team=project.team,
            project=project,
            sprint=sprint,
            title=title,
            reporter=self.user,
        )
        backing = self._make_task(
            project, title=title, is_milestone=True, milestone=milestone, sprint=sprint
        )
        milestone.task = backing
        milestone.save(update_fields=["task"])
        return milestone

    def _put(self, payload):
        return self.client.put("/api/v2/task/", payload, format="json")

    def _move_task(self, task, dest_project, **extra):
        return self._put(
            {
                # What the client actually sends: the team the user is
                # looking at, which a cross-team move has to override.
                "team": self.team.team_id,
                "task_id": task.task_id,
                "project": dest_project.project_id,
                **extra,
            }
        )


class TaskProjectMoveRuleTests(ProjectMoveTestBase):
    """Rule i: only a milestone or a root task may change project."""

    def test_top_level_task_moves(self):
        task = self._make_task(self.project_a)

        res = self._move_task(task, self.project_b)

        self.assertEqual(res.status_code, 200, res.data)
        task.refresh_from_db()
        self.assertEqual(task.project_id, self.project_b.project_id)

    def test_task_inside_a_milestone_cannot_move_on_its_own(self):
        """The milestone owns which project its tasks are in, and rule ii
        says they travel WITH it. This once succeeded, on the reasoning that
        a milestone contains its tasks rather than parenting them — so the
        task moved out and shed the milestone, which is the opposite of
        what the milestone moving would have done to it."""
        milestone = self._make_milestone(self.project_a)
        task = self._make_task(
            self.project_a,
            milestone=milestone,
            parent_task_id=milestone.task_id,
            root_task_id=milestone.task_id,
        )

        res = self._move_task(task, self.project_b)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data.get("code"), "subtask_project_move_forbidden")
        task.refresh_from_db()
        self.assertEqual(task.project_id, self.project_a.project_id)
        self.assertEqual(task.milestone_id, milestone.milestone_id)

    def test_sub_task_of_a_task_inside_a_milestone_cannot_move_either(self):
        """Two levels down, and neither level is allowed to leave alone."""
        milestone = self._make_milestone(self.project_a)
        member = self._make_task(
            self.project_a,
            milestone=milestone,
            parent_task_id=milestone.task_id,
            root_task_id=milestone.task_id,
        )
        sub = self._make_task(
            self.project_a,
            title="sub",
            milestone=milestone,
            parent_task_id=member.task_id,
            root_task_id=milestone.task_id,
        )

        res = self._move_task(sub, self.project_b)

        self.assertEqual(res.status_code, 400, res.data)
        sub.refresh_from_db()
        self.assertEqual(sub.project_id, self.project_a.project_id)

    def test_a_milestones_backing_row_is_not_treated_as_nested(self):
        """It has no parent task, so the rule lets it through. Milestones are
        moved through `milestone_views.patch`, which is also what resets the
        sprint — this only pins that the rule does not stand in the way."""
        milestone = self._make_milestone(self.project_a)
        backing = TaskMaster.objects.get(task_id=milestone.task_id)

        res = self._move_task(backing, self.project_b)

        self.assertEqual(res.status_code, 200, res.data)

    def test_sub_task_cannot_move_on_its_own(self):
        parent = self._make_task(self.project_a, title="parent")
        sub = self._make_task(self.project_a, title="sub", parent_task_id=parent.task_id)

        res = self._move_task(sub, self.project_b)

        self.assertEqual(res.status_code, 400, res.data)
        self.assertEqual(res.data.get("code"), "subtask_project_move_forbidden")
        sub.refresh_from_db()
        self.assertEqual(sub.project_id, self.project_a.project_id)

    def test_sub_task_rejection_does_not_apply_to_the_create_scaffold(self):
        """The create form POSTs its scaffold row under whatever project the
        page was showing, so finalizing a SUB-task in a different project is
        a "move" of a task that doesn't exist to the user yet. `is_init_task`
        is still True until this very PUT clears it."""
        parent = self._make_task(self.project_b, title="parent")
        scaffold = self._make_task(
            self.project_a, title="<new task>", is_init_task=True, parent_task_id=parent.task_id
        )

        res = self._move_task(
            scaffold, self.project_b, parent_task_id=parent.task_id, is_init_task=False
        )

        self.assertEqual(res.status_code, 200, res.data)
        scaffold.refresh_from_db()
        self.assertEqual(scaffold.project_id, self.project_b.project_id)

    def test_sub_task_with_a_hard_deleted_parent_may_still_move(self):
        """A dangling parent leaves the task effectively top-level. Blocking
        the move would strand it in a project with no way out."""
        parent = self._make_task(self.project_a, title="parent")
        sub = self._make_task(self.project_a, title="sub", parent_task_id=parent.task_id)
        parent.delete()

        res = self._move_task(sub, self.project_b)

        self.assertEqual(res.status_code, 200, res.data)
        sub.refresh_from_db()
        self.assertEqual(sub.project_id, self.project_b.project_id)


class TaskMoveDestinationGuardTests(ProjectMoveTestBase):
    def test_move_into_an_unreachable_project_is_refused(self):
        """`can_access_task` authorizes the SOURCE only. Without a check on
        the destination, naming any project id filed the task into a project
        — in any team — the caller has no membership of."""
        stranger = User.objects.create_user(
            username="stranger", email="stranger@move.test", password="testpass123"
        )
        other_team = TeamMaster.objects.create(
            team_name="Theirs", team_email="theirs@move.test", owner=stranger
        )
        off_limits = self._make_project(other_team, "Off limits", join=False, owner=stranger)
        task = self._make_task(self.project_a)

        res = self._move_task(task, off_limits)

        self.assertEqual(res.status_code, 404, res.data)
        task.refresh_from_db()
        self.assertEqual(task.project_id, self.project_a.project_id)

    def test_move_into_a_deleted_project_is_refused(self):
        self.project_b.is_deleted = True
        self.project_b.save(update_fields=["is_deleted"])
        task = self._make_task(self.project_a)

        res = self._move_task(task, self.project_b)

        self.assertEqual(res.status_code, 404, res.data)


class TaskMoveCarriesSubtreeTests(ProjectMoveTestBase):
    """Rule ii: everything filed under the moved task comes along."""

    def test_sub_tasks_follow_and_are_renumbered(self):
        root = self._make_task(self.project_a, title="root")
        child = self._make_task(self.project_a, title="child", parent_task_id=root.task_id)
        grandchild = self._make_task(self.project_a, title="grand", parent_task_id=child.task_id)
        # Occupy the destination's low numbers so a carried-over number
        # would collide with a row already there.
        for i in range(4):
            self._make_task(self.project_b, title=f"squatter-{i}")

        res = self._move_task(root, self.project_b)
        self.assertEqual(res.status_code, 200, res.data)

        moved = [root, child, grandchild]
        for task in moved:
            task.refresh_from_db()
            self.assertEqual(task.project_id, self.project_b.project_id, task.title)
        numbers = [task.project_task_number for task in moved]
        self.assertNotIn(None, numbers)
        self.assertEqual(len(set(numbers)), len(numbers), "numbers must be unique per project")
        # Parent edges INSIDE the moved sub-tree are untouched.
        self.assertEqual(child.parent_task_id, root.task_id)
        self.assertEqual(grandchild.parent_task_id, child.task_id)

    def test_task_notes_follow_the_task(self):
        """Task notes denormalize the owning team and project, and the notes
        tree is read with both — so a note left behind vanishes from every
        project's tree."""
        task = self._make_task(self.project_a)
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project_a, owner=self.user, task=task, title="N"
        )

        self._move_task(task, self.project_b)

        note.refresh_from_db()
        self.assertEqual(note.project_id, self.project_b.project_id)

    def test_response_reports_the_source_project(self):
        """Both projects' lists changed by more than the row the client
        sent, and only the server still knows where the task came from."""
        task = self._make_task(self.project_a)

        res = self._move_task(task, self.project_b)

        self.assertEqual(res.data["movedFromProjectId"], self.project_a.project_id)

    def test_response_reports_no_move_on_an_ordinary_edit(self):
        task = self._make_task(self.project_a)

        res = self._move_task(task, self.project_a, title="renamed")

        self.assertIsNone(res.data["movedFromProjectId"])


class CrossTeamTaskMoveTests(ProjectMoveTestBase):
    """Bug 1: moving into (or out of) an externally shared project.

    The destination belongs to the HOST team while the caller works from
    their own. The row's team has to follow its project, or the task list —
    filtered on team AND project — finds it under neither.
    """

    def setUp(self):
        super().setUp()
        self.host = User.objects.create_user(
            username="host", email="host@move.test", password="testpass123"
        )
        self.host_team = TeamMaster.objects.create(
            team_name="Host", team_email="host-team@move.test", owner=self.host
        )
        self.shared_project = ProjectMaster.objects.create(
            team=self.host_team,
            project_name="Shared",
            owner=self.host,
            project_system_user=self.host,
        )
        # A guest IS an ordinary ProjectMembers row in the host team with no
        # TeamMembers row — that absence is the whole guest model.
        ProjectMembers.objects.create(
            team=self.host_team, project=self.shared_project, attendee=self.user
        )

    def _project_task_ids(self, project):
        """The ids the project's table would actually draw — the whole point
        of the bug is that a 200 here can come back without the moved row."""
        res = self.client.get(
            "/api/v2/task/getProjectTasks/",
            {"team_id": project.team_id, "project_id": project.project_id},
        )
        self.assertEqual(res.status_code, 200, res.data)
        return {row["id"] for row in res.data["data"]["tasks"]}

    def test_move_into_a_shared_project_takes_the_team_with_it(self):
        task = self._make_task(self.project_a)

        res = self._move_task(task, self.shared_project)

        self.assertEqual(res.status_code, 200, res.data)
        task.refresh_from_db()
        self.assertEqual(task.project_id, self.shared_project.project_id)
        self.assertEqual(str(task.team_id), str(self.host_team.team_id))

    def test_moved_task_is_listed_by_the_destination_project(self):
        """The regression itself: a 200 that left the task in neither list."""
        task = self._make_task(self.project_a)

        self._move_task(task, self.shared_project)

        self.assertIn(str(task.task_id), self._project_task_ids(self.shared_project))
        self.assertNotIn(str(task.task_id), self._project_task_ids(self.project_a))

    def test_move_out_of_a_shared_project_takes_the_team_back(self):
        task = self._make_task(self.shared_project)

        res = self._move_task(task, self.project_a)

        self.assertEqual(res.status_code, 200, res.data)
        task.refresh_from_db()
        self.assertEqual(str(task.team_id), str(self.team.team_id))
        self.assertIn(str(task.task_id), self._project_task_ids(self.project_a))

    def test_sub_tasks_change_team_too(self):
        root = self._make_task(self.project_a, title="root")
        child = self._make_task(self.project_a, title="child", parent_task_id=root.task_id)

        self._move_task(root, self.shared_project)

        child.refresh_from_db()
        self.assertEqual(str(child.team_id), str(self.host_team.team_id))
        self.assertIn(str(child.task_id), self._project_task_ids(self.shared_project))

    def test_dependency_edges_that_would_straddle_teams_are_dropped(self):
        """`TaskDependency` allows cross-project edges but not cross-team
        ones. An edge left behind would render a "blocked by" chip pointing
        at a task the other side can never load."""
        task = self._make_task(self.project_a, title="moving")
        blocker_left_behind = self._make_task(self.project_a, title="blocker")
        already_shared = self._make_task(self.shared_project, title="neighbour")
        straddling = TaskDependency.objects.create(
            blocker_task=blocker_left_behind, blocked_task=task, team=self.team
        )
        survives = TaskDependency.objects.create(
            blocker_task=already_shared, blocked_task=task, team=self.host_team
        )

        self._move_task(task, self.shared_project)

        self.assertFalse(TaskDependency.objects.filter(id=straddling.id).exists())
        survives.refresh_from_db()
        self.assertEqual(str(survives.team_id), str(self.host_team.team_id))

    def test_same_team_move_keeps_its_dependency_edges(self):
        task = self._make_task(self.project_a, title="moving")
        blocker = self._make_task(self.project_a, title="blocker")
        edge = TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=task, team=self.team
        )

        self._move_task(task, self.project_b)

        self.assertTrue(TaskDependency.objects.filter(id=edge.id).exists())


class MilestoneProjectMoveTests(ProjectMoveTestBase):
    """Bug 3 + rules ii/iii: a milestone can move, takes its tasks, and
    lands with no sprint."""

    def setUp(self):
        super().setUp()
        self.sprint_a = Sprint.objects.create(
            team=self.team,
            project=self.project_a,
            name="Sprint 1",
            sequence_number=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
        )
        self.milestone = self._make_milestone(self.project_a, sprint=self.sprint_a)
        self.milestone.tags = [{"tagId": 1, "tagName": "source-only"}]
        self.milestone.save(update_fields=["tags"])
        self.member = self._make_task(
            self.project_a,
            title="member",
            milestone=self.milestone,
            sprint=self.sprint_a,
            parent_task_id=self.milestone.task_id,
            root_task_id=self.milestone.task_id,
        )
        self.member_child = self._make_task(
            self.project_a,
            title="member child",
            milestone=self.milestone,
            sprint=self.sprint_a,
            parent_task_id=self.member.task_id,
            root_task_id=self.milestone.task_id,
        )

    def _patch_milestone(self, **body):
        return self.client.patch(
            f"/api/v2/milestone/{self.milestone.milestone_id}/", body, format="json"
        )

    def test_milestone_changes_project(self):
        res = self._patch_milestone(project_id=self.project_b.project_id)

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["milestone"]["projectId"], self.project_b.project_id)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.project_id, self.project_b.project_id)

    def test_backing_task_and_member_tasks_follow(self):
        self._patch_milestone(project_id=self.project_b.project_id)

        moved_ids = [self.milestone.task_id, self.member.task_id, self.member_child.task_id]
        for task in TaskMaster.objects.filter(task_id__in=moved_ids):
            self.assertEqual(task.project_id, self.project_b.project_id, task.title)
            self.assertIsNotNone(task.project_task_number, task.title)

    def test_sprint_is_cleared_for_the_milestone_and_everything_in_it(self):
        """Sprints are defined per project, so the one the milestone sat in
        does not exist in the destination and no mapping is meaningful."""
        self._patch_milestone(project_id=self.project_b.project_id)

        self.milestone.refresh_from_db()
        self.assertIsNone(self.milestone.sprint_id)
        for task in (self.member, self.member_child):
            task.refresh_from_db()
            self.assertIsNone(task.sprint_id, task.title)
        self.assertIsNone(TaskMaster.objects.get(task_id=self.milestone.task_id).sprint_id)

    def test_a_sprint_sent_alongside_a_project_move_is_ignored(self):
        """It names a sprint in the project being left or the one being
        entered, and the milestone must land unplanned either way."""
        res = self._patch_milestone(
            project_id=self.project_b.project_id, sprint_id=self.sprint_a.sprint_id
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.milestone.refresh_from_db()
        self.assertIsNone(self.milestone.sprint_id)

    def test_project_scoped_tags_are_dropped(self):
        self._patch_milestone(project_id=self.project_b.project_id)

        self.milestone.refresh_from_db()
        self.assertIsNone(self.milestone.tags)

    def test_move_into_an_unreachable_project_is_refused(self):
        stranger = User.objects.create_user(
            username="stranger2", email="stranger2@move.test", password="testpass123"
        )
        other_team = TeamMaster.objects.create(
            team_name="Theirs2", team_email="theirs2@move.test", owner=stranger
        )
        off_limits = self._make_project(other_team, "Off limits", join=False, owner=stranger)

        res = self._patch_milestone(project_id=off_limits.project_id)

        self.assertEqual(res.status_code, 404, res.data)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.project_id, self.project_a.project_id)

    def test_an_ordinary_patch_still_leaves_the_project_alone(self):
        res = self._patch_milestone(status="WIP")

        self.assertEqual(res.status_code, 200, res.data)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.project_id, self.project_a.project_id)
        self.assertEqual(self.milestone.sprint_id, self.sprint_a.sprint_id)

    def test_a_same_project_patch_is_not_treated_as_a_move(self):
        res = self._patch_milestone(project_id=self.project_a.project_id)

        self.assertEqual(res.status_code, 200, res.data)
        self.milestone.refresh_from_db()
        self.assertEqual(self.milestone.sprint_id, self.sprint_a.sprint_id)


class CrossTeamMilestoneMoveTests(ProjectMoveTestBase):
    def setUp(self):
        super().setUp()
        self.host = User.objects.create_user(
            username="host2", email="host2@move.test", password="testpass123"
        )
        self.host_team = TeamMaster.objects.create(
            team_name="Host2", team_email="host-team2@move.test", owner=self.host
        )
        self.shared_project = ProjectMaster.objects.create(
            team=self.host_team,
            project_name="Shared",
            owner=self.host,
            project_system_user=self.host,
        )
        ProjectMembers.objects.create(
            team=self.host_team, project=self.shared_project, attendee=self.user
        )
        self.milestone = self._make_milestone(self.project_a)
        self.member = self._make_task(
            self.project_a,
            title="member",
            milestone=self.milestone,
            parent_task_id=self.milestone.task_id,
        )

    def test_milestone_and_its_tasks_take_the_host_team(self):
        res = self.client.patch(
            f"/api/v2/milestone/{self.milestone.milestone_id}/",
            {"project_id": self.shared_project.project_id},
            format="json",
        )

        self.assertEqual(res.status_code, 200, res.data)
        self.milestone.refresh_from_db()
        self.assertEqual(str(self.milestone.team_id), str(self.host_team.team_id))
        self.member.refresh_from_db()
        self.assertEqual(str(self.member.team_id), str(self.host_team.team_id))

        listed = self.client.get(
            "/api/v2/task/getProjectTasks/",
            {
                "team_id": self.host_team.team_id,
                "project_id": self.shared_project.project_id,
            },
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        ids = {row["id"] for row in listed.data["data"]["tasks"]}
        self.assertIn(str(self.member.task_id), ids)
        self.assertIn(str(self.milestone.task_id), ids)
