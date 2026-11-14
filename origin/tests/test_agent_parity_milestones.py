"""Convert a task to a milestone and back, and move a milestone to another project, for the agent and
MCP (MIP-1564, flag `AGENT_PARITY_TOOLS`).

The app has both on a milestone's "..." menu and its project field (MIP-1042 and the milestone PATCH);
`manage_milestone` said "not supported" for the move and had no convert. Now (flag on):

  * `manage_milestone` `action: "convert"` with `task_id` turns a ROOT task into a milestone and with
    `milestone_id` a milestone into a task, on the same row, by the app's own service;
  * `manage_milestone` `update` with `project_id` MOVES the milestone to another project of the team
    through the PATCH's own code (`update_milestone(project_move=...)`).

This file pins the tool: what it does, each refusal (and that a refusal changes nothing), the next step
every refusal sentence names (followed for real), the flag gate (off is what it was, word for word), the
approval card's facts, the approve path and MCP. `test_agent_parity_milestones_twin` runs the same
changes through the app's endpoints and through the tool and compares every channel.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.conf import settings

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneFolder, MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.manage_milestone import (
    ACTIONS,
    ACTIONS_ON,
    MANAGE_MILESTONE,
)
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

from .test_convert_task_milestone import ConversionBase

ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}


class ToolBase(ConversionBase):
    """`ConversionBase`'s team: owner, alice and bob are all on the project CNV; `stranger` is nobody.
    Adds a second project (DST) both owner and alice are on, and the flag, ON unless `flag = False`."""

    flag = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON if self.flag else OFF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dest = ProjectMaster.objects.create(
            team=self.team,
            project_name="Destination",
            code="DST",
            owner=self.owner,
            project_system_user=self.owner,
        )
        for u in (self.owner, self.alice):
            ProjectMembers.objects.create(team=self.team, project=self.dest, attendee=u)
        self.folder = MilestoneFolder.objects.create(
            team=self.team, project=self.project, name="Roadmap", sort_order=0
        )

    def ctx(self, user=None, team=None):
        return ToolContext(
            team_id=str((team or self.team).team_id), user_id=str((user or self.owner).id)
        )

    def call(self, args, user=None, team=None):
        return MANAGE_MILESTONE.run(args, self.ctx(user, team))

    def to_milestone_tool(self, task, **kw):
        return self.call({"action": "convert", "task_id": task.task_id}, **kw)

    def to_task_tool(self, milestone, **kw):
        return self.call({"action": "convert", "milestone_id": milestone.milestone_id}, **kw)

    def move_tool(self, milestone, project=None, **extra):
        return self.call(
            {
                "action": "update",
                "milestone_id": milestone.milestone_id,
                "project_id": (project or self.dest).project_id,
                **extra,
            }
        )

    def roadmap_milestone(self, title="Launch", suffix="", **kw):
        """A milestone in a sprint and a folder with two assignees and two tasks under it, one of
        which has a sub-task: the lot a convert or a move has to take along or let go of. `suffix`
        is added to the tasks' titles, so twin worlds can be told apart."""
        m = self.milestone(
            title,
            assignees=(self.alice, self.bob),
            sprint=self.sprint,
            folder=self.folder,
            **kw,
        )
        first = self.in_milestone(f"First{suffix}", m)
        self.in_milestone(f"Second{suffix}", m)
        self.task(
            f"First's child{suffix}",
            parent_task_id=first.task_id,
            milestone=m,
            sprint=m.sprint,
        )
        return m


# --------------------------------------------------------------------------------------------
# convert: a root task into a milestone
# --------------------------------------------------------------------------------------------


class TaskToMilestoneToolTests(ToolBase):
    def test_a_root_task_becomes_a_milestone_on_the_same_row(self):
        t = self.task("Plan the launch", status="WIP", priority="High", priority_code=3)
        child = self.task("Step one", parent_task_id=t.task_id, root_task_id=t.task_id)
        display_id = t.display_id

        out = self.to_milestone_tool(t)

        row = self.fresh(t)
        m = MilestoneMaster.objects.get(task=row)
        self.assertTrue(row.is_milestone)
        self.assertEqual(row.display_id, display_id)
        self.assertEqual(out["action"], "convert")
        self.assertEqual(out["direction"], "task_to_milestone")
        self.assertEqual(out["milestone_id"], m.milestone_id)
        self.assertEqual(out["task_id"], t.task_id)
        self.assertEqual(out["display_id"], display_id)
        self.assertEqual(out["status"], "WIP")
        self.assertEqual(out["assignee_ids"], [str(self.alice.id)])
        # the sub-task is in the milestone now, still under the same row
        self.assertEqual(out["tasks_in_milestone"], 1)
        child = self.fresh(child)
        self.assertEqual(child.milestone_id, m.milestone_id)
        self.assertEqual(child.parent_task_id, t.task_id)
        self.assertIn(display_id, out["__summary__"])
        self.assertIn("milestone", out["__summary__"])

    def test_the_task_can_be_named_by_a_display_id_after_the_catalog_resolves_it(self):
        # `parity_task_ref_args` is the catalog's: the tool itself takes the number.
        t = self.task("By number")
        self.assertEqual(self.to_milestone_tool(t)["task_id"], t.task_id)

    def test_it_is_not_undoable_and_returns_no_pre_image(self):
        t = self.task("No undo")
        out = MANAGE_MILESTONE.run(
            {"action": "convert", "task_id": t.task_id},
            ToolContext(team_id=self.ctx().team_id, user_id=self.ctx().user_id, capture_undo=True),
        )
        self.assertNotIn("__undo__", out)

    def test_a_sub_task_is_refused_with_what_to_do_next_and_nothing_changes(self):
        parent = self.task("Parent")
        sub = self.task("Sub", parent_task_id=parent.task_id, root_task_id=parent.task_id)
        with self.assertRaises(ToolError) as caught:
            self.to_milestone_tool(sub)
        text = str(caught.exception)
        self.assertIn("Only a task with no parent can become a milestone", text)
        self.assertIn("`update_task` with `parent_task_id` ''", text)
        self.assertTrue(text.endswith("Nothing was changed."))
        self.assertFalse(self.fresh(sub).is_milestone)
        self.assertFalse(MilestoneMaster.objects.filter(title="Sub").exists())

    def test_a_task_in_a_milestone_is_refused_with_what_to_do_next(self):
        m = self.milestone("Home")
        # filed in the milestone with no parent edge: the second refusal, `task_in_milestone`
        filed = self.task("Filed", milestone=m)
        with self.assertRaises(ToolError) as caught:
            self.to_milestone_tool(filed)
        text = str(caught.exception)
        self.assertIn("A task that is in a milestone can't become a milestone", text)
        self.assertIn("`update_task` with `milestone_id` ''", text)
        self.assertFalse(self.fresh(filed).is_milestone)

    def test_a_milestones_own_row_points_at_the_other_direction(self):
        m = self.milestone("Already")
        with self.assertRaises(ToolError) as caught:
            self.to_milestone_tool(m.task)
        text = str(caught.exception)
        self.assertIn("already a milestone", text)
        self.assertIn("`convert` with its `milestone_id`", text)
        self.assertEqual(MilestoneMaster.objects.filter(task=m.task, is_deleted=False).count(), 1)

    def test_a_deleted_task_and_a_task_that_is_not_there_are_refused(self):
        gone = self.task("Gone", is_deleted=True, status="Deleted")
        with self.assertRaisesRegex(ToolError, "has been deleted"):
            self.to_milestone_tool(gone)
        with self.assertRaisesRegex(ToolError, "not found"):
            self.call({"action": "convert", "task_id": 987654})
        self.assertFalse(self.fresh(gone).is_milestone)

    def test_an_unsaved_draft_is_refused_in_a_sentence_with_a_next_step(self):
        draft = self.task("Draft", is_init_task=True)
        with self.assertRaises(ToolError) as caught:
            self.to_milestone_tool(draft)
        self.assertIn("hasn't been saved yet", str(caught.exception))
        self.assertIn("`list_tasks`", str(caught.exception))
        self.assertFalse(self.fresh(draft).is_milestone)

    def test_the_refusal_next_steps_really_work(self):
        # Each sentence names an `update_task` call. Make it, then convert: the whole path works.
        # (a) a sub-task of a root task: take it out from under its parent
        parent = self.task("Parent")
        sub = self.task("Sub", parent_task_id=parent.task_id, root_task_id=parent.task_id)
        with self.assertRaises(ToolError):
            self.to_milestone_tool(sub)
        UPDATE_TASK.run({"task_id": sub.task_id, "parent_task_id": ""}, self.ctx())
        self.assertEqual(self.to_milestone_tool(sub)["direction"], "task_to_milestone")
        # (b) a task in a milestone: take it out of the milestone
        m = self.milestone("Home")
        filed = self.in_milestone("Filed", m)
        with self.assertRaises(ToolError):
            self.to_milestone_tool(filed)
        UPDATE_TASK.run({"task_id": filed.task_id, "milestone_id": ""}, self.ctx())
        self.assertEqual(self.to_milestone_tool(filed)["direction"], "task_to_milestone")

    def test_exactly_one_of_the_two_ids(self):
        t = self.task("T")
        m = self.milestone("M")
        for args in (
            {"action": "convert"},
            {"action": "convert", "task_id": t.task_id, "milestone_id": m.milestone_id},
            {"action": "convert", "task_id": "", "milestone_id": ""},
        ):
            with self.subTest(args=args), self.assertRaisesRegex(ToolError, "exactly one of"):
                self.call(args)
        self.assertFalse(self.fresh(t).is_milestone)
        self.assertFalse(MilestoneMaster.objects.get(pk=m.pk).is_deleted)

    def test_nothing_else_is_accepted_with_a_convert(self):
        t = self.task("T")
        with self.assertRaisesRegex(ToolError, "`title` does not apply to `convert`"):
            self.call({"action": "convert", "task_id": t.task_id, "title": "New title"})
        self.assertFalse(self.fresh(t).is_milestone)

    def test_it_is_refused_for_a_project_the_person_is_not_on_even_if_they_can_open_the_task(self):
        # bob is on CNV but not on DST; he is the assignee of a task there
        t = self.task("Bob's", project=self.dest, assignee=self.bob)
        with self.assertRaises(ToolForbidden) as caught:
            self.to_milestone_tool(t, user=self.bob)
        self.assertIn("Only a member of", str(caught.exception))
        self.assertFalse(self.fresh(t).is_milestone)


# --------------------------------------------------------------------------------------------
# convert: a milestone into a task
# --------------------------------------------------------------------------------------------


class MilestoneToTaskToolTests(ToolBase):
    def test_a_milestone_becomes_a_task_on_the_same_row(self):
        m = self.roadmap_milestone()
        row_id, display_id = m.task_id, m.task.display_id

        out = self.to_task_tool(m)

        row = TaskMaster.objects.get(task_id=row_id)
        self.assertFalse(row.is_milestone)
        self.assertIsNone(row.milestone_id)
        self.assertIsNone(row.sprint_id)
        self.assertEqual(row.display_id, display_id)
        self.assertEqual(out["action"], "convert")
        self.assertEqual(out["direction"], "milestone_to_task")
        self.assertEqual(out["task_id"], row_id)
        self.assertEqual(out["display_id"], display_id)
        self.assertEqual(out["converted_milestone_id"], m.milestone_id)
        self.assertEqual(out["sub_tasks_kept"], 3)
        # the first assignee stays the assignee, the other becomes a collaborator
        self.assertEqual(out["assignee_id"], str(self.alice.id))
        self.assertEqual(out["collaborator_ids"], [str(self.bob.id)])
        self.assertEqual(row.assignee_id, self.alice.id)
        self.assertEqual([c.id for c in row.collaborators.all()], [self.bob.id])
        # a retired milestone: no folder, no sprint, no people, remembers the task
        retired = MilestoneMaster.objects.get(pk=m.pk)
        self.assertTrue(retired.is_deleted)
        self.assertIsNone(retired.folder_id)
        self.assertIsNone(retired.sprint_id)
        self.assertEqual(retired.converted_to_task_id, row_id)
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=retired).exists())
        # its tasks are sub-tasks of the row, out of the sprint
        for title in ("First", "Second", "First's child"):
            child = TaskMaster.objects.get(title=title)
            self.assertIsNone(child.milestone_id)
            self.assertIsNone(child.sprint_id)
        self.assertEqual(TaskMaster.objects.get(title="First").parent_task_id, row_id)

    def test_the_result_has_no_milestone_id_so_nothing_reads_it_as_a_live_milestone(self):
        m = self.roadmap_milestone()
        out = self.to_task_tool(m)
        self.assertNotIn("milestone_id", out)
        self.assertIn("converted to the task", out["__summary__"].replace("into the task", "converted to the task"))

    def test_an_already_converted_milestone_says_where_it_went(self):
        m = self.milestone("Once")
        self.to_task_tool(m)
        row = TaskMaster.objects.get(task_id=m.task_id)
        with self.assertRaises(ToolError) as caught:
            self.to_task_tool(m)
        text = str(caught.exception)
        self.assertIn(f"Milestone {m.milestone_id} was already converted to the task", text)
        self.assertIn(row.display_id, text)
        self.assertIn(f"task_id {row.task_id}", text)

    def test_a_task_that_became_a_milestone_again_names_the_new_milestone(self):
        m = self.milestone("Round trip")
        self.to_task_tool(m)
        again = self.to_milestone_tool(TaskMaster.objects.get(task_id=m.task_id))
        with self.assertRaises(ToolError) as caught:
            self.to_task_tool(m)
        self.assertIn(f"milestone {again['milestone_id']}", str(caught.exception))

    def test_a_deleted_milestone_is_said_to_be_deleted_not_converted(self):
        m = self.milestone("Deleted")
        MilestoneMaster.objects.filter(pk=m.pk).update(is_deleted=True)
        with self.assertRaisesRegex(ToolError, "has been deleted"):
            self.to_task_tool(m)

    def test_a_milestone_that_is_not_there_is_not_found(self):
        with self.assertRaisesRegex(ToolError, "not found"):
            self.call({"action": "convert", "milestone_id": 987654})

    def test_the_converted_task_can_be_converted_back_and_keeps_its_sub_tasks(self):
        m = self.roadmap_milestone()
        self.to_task_tool(m)
        row = TaskMaster.objects.get(task_id=m.task_id)
        out = self.to_milestone_tool(row)
        # all three tasks under the row join the new milestone; the folder and sprint are not back
        self.assertEqual(out["tasks_in_milestone"], 3)
        self.assertIsNone(out["folder_id"])
        self.assertIsNone(out["sprint_id"])
        self.assertEqual(out["assignee_ids"], [str(self.alice.id)])


# --------------------------------------------------------------------------------------------
# move: a milestone to another project
# --------------------------------------------------------------------------------------------


class MoveToolTests(ToolBase):
    def test_the_milestone_and_its_tasks_move_and_get_new_display_ids(self):
        m = self.roadmap_milestone()
        old_display = m.task.display_id
        tasks = list(TaskMaster.objects.filter(title__in=["First", "Second", "First's child"]))

        out = self.move_tool(m)

        m.refresh_from_db()
        self.assertEqual(m.project_id, self.dest.project_id)
        self.assertEqual(out["project_id"], self.dest.project_id)
        self.assertEqual(out["project_name"], "Destination")
        self.assertIn("project", out["changed_fields"])
        self.assertTrue(out["display_id"].startswith("DST-"))
        self.assertNotEqual(out["display_id"], old_display)
        for t in [m.task, *tasks]:
            t.refresh_from_db()
            self.assertEqual(t.project_id, self.dest.project_id, t.title)
            self.assertTrue(t.display_id.startswith("DST-"), t.title)
            self.assertIsNone(t.sprint_id, t.title)
        # what is cleared on the milestone
        self.assertIsNone(m.sprint_id)
        self.assertIsNone(m.folder_id)
        self.assertEqual(out["sprint_id"], None)
        self.assertEqual(out["folder_id"], None)
        # and what the result tells the model about it
        self.assertEqual(
            out["moved"],
            {
                "from_project_id": self.project.project_id,
                "from_project_name": "Conv",
                "tasks_moved": 3,
                "cleared": ["sprint", "folder", "tags"],
                "display_ids_changed": True,
            },
        )
        self.assertIn("Conv to Destination", out["__summary__"])

    def test_tags_are_cleared_on_the_milestone_and_on_every_task_that_moves(self):
        tag = [{"tagName": "Feature"}]
        m = self.roadmap_milestone(tags=tag)
        TaskMaster.objects.filter(milestone=m).update(tags=tag)
        TaskMaster.objects.filter(pk=m.task_id).update(tags=tag)

        self.move_tool(m)

        m.refresh_from_db()
        self.assertFalse(m.tags)
        for row in TaskMaster.objects.filter(milestone=m):
            self.assertFalse(row.tags, row.title)

    def test_the_assignees_stay(self):
        m = self.roadmap_milestone()
        out = self.move_tool(m)
        self.assertEqual(sorted(out["assignee_ids"]), sorted([str(self.alice.id), str(self.bob.id)]))
        self.assertEqual(
            MilestoneAssignees.objects.filter(milestone=m, team=self.dest.team).count(), 2
        )

    def test_other_fields_may_ride_along_with_a_move(self):
        m = self.roadmap_milestone()
        out = self.move_tool(m, title="Launch v2", status="WIP", due_date="2026-12-24")
        m.refresh_from_db()
        self.assertEqual((m.title, m.status, m.due_date), ("Launch v2", "WIP", date(2026, 12, 24)))
        self.assertEqual(m.project_id, self.dest.project_id)
        self.assertEqual(out["changed_fields"][0], "project")
        self.assertEqual(
            set(out["changed_fields"]) - {"project"}, {"title", "status", "due_date"}
        )
        self.assertIn("also changed", out["__summary__"])

    def test_people_are_judged_as_the_patch_judges_them_a_team_member_or_a_project_member(self):
        # bob is on the team and on CNV, not on DST: the rule is "the project or its team", so he may
        # be named. `stranger` is on neither: refused, and nothing moves.
        m = self.milestone("People", assignees=(self.alice,))
        with self.assertRaises(ToolError):
            self.move_tool(m, assignee_ids=[str(self.alice.id), str(self.stranger.id)])
        m.refresh_from_db()
        self.assertEqual(m.project_id, self.project.project_id)
        out = self.move_tool(m, assignee_ids=[str(self.alice.id), str(self.bob.id)])
        self.assertEqual(sorted(out["assignee_ids"]), sorted([str(self.alice.id), str(self.bob.id)]))
        self.assertEqual(out["project_id"], self.dest.project_id)

    def test_a_sprint_a_folder_or_tags_in_the_same_call_are_refused_and_nothing_moves(self):
        m = self.roadmap_milestone()
        for extra in (
            {"sprint_id": str(self.sprint.sprint_id)},
            {"sprint_id": ""},
            {"milestone_folder_id": str(self.folder.folder_id)},
            {"milestone_folder_id": ""},
            {"tags": []},
            {"tags": ["Feature"]},
        ):
            with self.subTest(extra=extra):
                with self.assertRaises(ToolError) as caught:
                    self.move_tool(m, title="Changed", **extra)
                self.assertIn("can't be set in the same call as a project move", str(caught.exception))
                self.assertIn("a second `update` in Destination", str(caught.exception))
                m.refresh_from_db()
                self.assertEqual(m.project_id, self.project.project_id)
                self.assertEqual(m.title, "Launch")
                self.assertEqual(m.sprint_id, self.sprint.sprint_id)

    def test_the_project_it_is_already_in_is_no_move(self):
        m = self.roadmap_milestone()
        out = self.move_tool(m, project=self.project, title="Same project")
        self.assertEqual(out["changed_fields"], ["title"])
        self.assertNotIn("moved", out)
        m.refresh_from_db()
        # sprint and folder untouched: nothing moved
        self.assertEqual(m.sprint_id, self.sprint.sprint_id)
        self.assertEqual(m.folder_id, self.folder.folder_id)
        self.assertTrue(self.fresh(m.task).display_id.startswith("CNV-"))

    def test_a_move_to_the_same_project_alone_changes_nothing(self):
        m = self.roadmap_milestone()
        out = self.move_tool(m, project=self.project)
        self.assertEqual(out["changed_fields"], [])
        self.assertIn("No changes", out["__summary__"])

    def test_a_project_the_caller_is_not_on_is_not_found_and_nothing_moves(self):
        m = self.milestone("Stay", assignees=(self.alice,))
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.bob,
            project_system_user=self.bob,
        )
        with self.assertRaises(ToolForbidden) as caught:
            self.move_tool(m, project=hidden)
        self.assertIn(f"Project {hidden.project_id} not found", str(caught.exception))
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_a_deleted_project_and_a_project_that_is_not_there_are_not_found(self):
        m = self.milestone("Stay")
        ProjectMaster.objects.filter(pk=self.dest.pk).update(is_deleted=True)
        with self.assertRaisesRegex(ToolError, "not found"):
            self.move_tool(m)
        with self.assertRaisesRegex(ToolError, "not found"):
            self.call(
                {"action": "update", "milestone_id": m.milestone_id, "project_id": 987654}
            )
        with self.assertRaisesRegex(ToolError, "must be an integer"):
            self.call(
                {"action": "update", "milestone_id": m.milestone_id, "project_id": "abc"}
            )
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_a_milestone_of_a_project_the_caller_is_not_on_is_not_found(self):
        m = self.milestone("Theirs")
        # bob is on CNV: take him off and he can no longer move its milestones
        ProjectMembers.objects.filter(project=self.project, attendee=self.bob).delete()
        with self.assertRaises(ToolForbidden):
            self.call(
                {
                    "action": "update",
                    "milestone_id": m.milestone_id,
                    "project_id": self.dest.project_id,
                },
                user=self.bob,
            )

    def test_the_move_is_back_to_where_it_came_from_by_the_same_call(self):
        m = self.roadmap_milestone()
        self.move_tool(m)
        out = self.move_tool(m, project=self.project)
        self.assertEqual(out["project_id"], self.project.project_id)
        self.assertTrue(out["display_id"].startswith("CNV-"))
        self.assertIsNone(out["sprint_id"])  # not back: the sprint was cleared by the first move
