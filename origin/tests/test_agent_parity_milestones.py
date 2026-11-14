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

from origin.models.chat.unified_models import Activity, ActivityType
from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.member_event_models import MemberEvent
from origin.models.common.security_event_models import SecurityEvent
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneFolder, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.manage_milestone import (
    ACTIONS,
    ACTIONS_ON,
    MANAGE_MILESTONE,
)
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services.milestone_service import create_milestone
from origin.views.public.mcp import tools as mcp

from .test_convert_task_milestone import ConversionBase
from .test_mcp_endpoint import McpBase

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
        self.assertIn(
            "converted to the task",
            out["__summary__"].replace("into the task", "converted to the task"),
        )

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
        self.assertEqual(
            sorted(out["assignee_ids"]), sorted([str(self.alice.id), str(self.bob.id)])
        )
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
        self.assertEqual(set(out["changed_fields"]) - {"project"}, {"title", "status", "due_date"})
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
        self.assertEqual(
            sorted(out["assignee_ids"]), sorted([str(self.alice.id), str(self.bob.id)])
        )
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
                self.assertIn(
                    "can't be set in the same call as a project move", str(caught.exception)
                )
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
            self.call({"action": "update", "milestone_id": m.milestone_id, "project_id": 987654})
        with self.assertRaisesRegex(ToolError, "must be an integer"):
            self.call({"action": "update", "milestone_id": m.milestone_id, "project_id": "abc"})
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


# --------------------------------------------------------------------------------------------
# the gate: off is what it was, word for word; on publishes what this task adds
# --------------------------------------------------------------------------------------------

FLAG_OFF_ACTION_REFUSAL = (
    "`action` must be one of ['create', 'update', 'create_folder', 'rename_folder', "
    "'delete_folder'] (got 'convert')."
)
FLAG_OFF_MOVE_REFUSAL = (
    "A milestone can't be moved to another project with this tool, and `project_id` isn't an "
    "argument of `update`. Nothing was changed."
)
FLAG_OFF_TASK_ID_REFUSAL = (
    "`task_id` does not apply to `update`; it takes `assignee_ids`, `description_markdown`, "
    "`due_date`, `effort_level`, `milestone_folder_id`, `milestone_id`, `priority`, `sprint_id`, "
    "`start_date`, `status`, `tags`, `title`. Nothing was changed."
)


def declared(**flags):
    from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: PLC0415

    with mock.patch.dict(settings.SEARCH_ENGINE, flags):
        return {d.name: d for d in _build_tool_declarations()}


class GateOffTests(ToolBase):
    """With the flag off the tool does what it did before this task, and says it in the same words."""

    flag = False

    def test_convert_is_no_action_at_all(self):
        t = self.task("T")
        with self.assertRaises(ToolError) as caught:
            self.call({"action": "convert", "task_id": t.task_id})
        self.assertEqual(str(caught.exception), FLAG_OFF_ACTION_REFUSAL)
        self.assertFalse(self.fresh(t).is_milestone)

    def test_a_project_id_on_update_is_refused_as_it_always_was(self):
        m = self.milestone("Stay")
        with self.assertRaises(ToolError) as caught:
            self.move_tool(m)
        self.assertEqual(str(caught.exception), FLAG_OFF_MOVE_REFUSAL)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_a_task_id_on_update_is_an_argument_that_does_not_apply(self):
        m = self.milestone("Stay")
        with self.assertRaises(ToolError) as caught:
            self.call({"action": "update", "milestone_id": m.milestone_id, "task_id": 5})
        self.assertEqual(str(caught.exception), FLAG_OFF_TASK_ID_REFUSAL)

    def test_a_task_id_that_names_nothing_is_still_the_same_refusal_not_a_lookup(self):
        # With the flag off the catalog does not resolve `task_id`, so a value that names no task
        # does not turn into "No task X in this team".
        from origin.views.public.mcp import tools as mcp  # noqa: PLC0415

        tool = next(t for t in mcp.CATALOG if t.name == "manage_milestone")
        args = mcp.resolve_references(tool, {"task_id": "NOPE-9"}, str(self.team.team_id))
        self.assertEqual(args, {"task_id": "NOPE-9"})

    def test_the_other_actions_are_unchanged(self):
        out = self.call(
            {"action": "create", "project_id": self.project.project_id, "title": "Plain create"}
        )
        self.assertEqual(out["action"], "create")
        m = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        out = self.call({"action": "update", "milestone_id": m.milestone_id, "title": "Renamed"})
        self.assertEqual(out["changed_fields"], ["title"])

    def test_the_declaration_is_the_registrys_own_text_and_schema(self):
        from origin.search_engine.agent.tools import REGISTRY  # noqa: PLC0415

        tool = REGISTRY["manage_milestone"]
        off = declared(**OFF)["manage_milestone"]
        self.assertEqual(off.description, tool.description)
        props = off.parameters_schema["properties"]
        self.assertEqual(props["action"]["enum"], list(ACTIONS))
        self.assertNotIn("task_id", props)
        self.assertEqual(
            props["project_id"]["description"],
            "Project (from list_projects). For create and create_folder.",
        )
        self.assertIn("Moving a milestone to another project is not supported.", off.description)

    def test_the_registry_itself_never_carries_the_flags_additions(self):
        from origin.search_engine.agent.tools import REGISTRY  # noqa: PLC0415

        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            from origin.search_engine.agent import tool_flag_params  # noqa: PLC0415

            tool_flag_params.with_flagged_params(
                "manage_milestone", REGISTRY["manage_milestone"].parameters_schema
            )
        schema = REGISTRY["manage_milestone"].parameters_schema
        self.assertEqual(schema["properties"]["action"]["enum"], list(ACTIONS))
        self.assertNotIn("task_id", schema["properties"])
        self.assertEqual(
            schema["properties"]["project_id"]["description"],
            "Project (from list_projects). For create and create_folder.",
        )

    def test_over_mcp_the_catalog_is_what_it_was(self):
        from origin.views.public.mcp import tools as mcp  # noqa: PLC0415

        tool = next(t for t in mcp.CATALOG if t.name == "manage_milestone")
        schema = mcp.input_schema(tool)
        self.assertEqual(schema["properties"]["action"]["enum"], list(ACTIONS))
        self.assertNotIn("task_id", schema["properties"])
        text = mcp.description_for(tool)
        self.assertEqual(text, mcp._replaced(tool))
        self.assertIn("A milestone can't be moved to another project", text)
        self.assertNotIn("convert", text)


class GateOnTests(ToolBase):
    def test_the_declaration_publishes_the_action_the_parameter_and_one_sentence(self):
        from origin.search_engine.agent.tools import REGISTRY  # noqa: PLC0415
        from origin.search_engine.agent.tools import milestone_convert as texts  # noqa: PLC0415

        base = REGISTRY["manage_milestone"].description
        on = declared(**ON)["manage_milestone"]
        props = on.parameters_schema["properties"]
        self.assertEqual(props["action"]["enum"], list(ACTIONS_ON))
        self.assertEqual(props["action"]["enum"][-1], "convert")
        self.assertEqual(props["task_id"]["type"], "STRING")
        self.assertIn("ROOT task", props["task_id"]["description"])
        self.assertEqual(
            props["project_id"]["description"], texts.PARITY_DESCRIPTIONS["project_id"]
        )
        # the sentence that said a move is not supported is gone, and what replaces it sits where it was
        self.assertIn(texts.MOVE_SENTENCE, base)
        self.assertNotIn("not supported", on.description)
        self.assertEqual(on.description, base.replace(texts.MOVE_SENTENCE, texts.MOVE_SENTENCE_ON))
        self.assertEqual(on.description.count(texts.GUIDANCE), 1)
        self.assertLess(
            on.description.index(texts.GUIDANCE), on.description.index("Deleting a milestone")
        )
        # every property it had is kept
        off_props = declared(**OFF)["manage_milestone"].parameters_schema["properties"]
        for name, prop in off_props.items():
            if name not in ("action", "project_id"):
                self.assertEqual(props[name], prop, name)
        self.assertEqual(
            declared(**ON)["manage_milestone"].parameters_schema["required"], ["action"]
        )

    def test_the_additions_are_terse(self):
        off, on = declared(**OFF)["manage_milestone"], declared(**ON)["manage_milestone"]
        grown = len(on.description) + len(str(on.parameters_schema)) - len(off.description)
        grown -= len(str(off.parameters_schema))
        self.assertLess(grown, 1000)

    def test_over_mcp_the_catalog_says_the_same(self):
        from origin.views.public.mcp import tools as mcp  # noqa: PLC0415

        tool = next(t for t in mcp.CATALOG if t.name == "manage_milestone")
        schema = mcp.input_schema(tool)
        self.assertEqual(schema["properties"]["action"]["enum"][-1], "convert")
        self.assertEqual(schema["properties"]["task_id"]["type"], "string")
        text = mcp.description_for(tool)
        self.assertNotIn("A milestone can't be moved", text)
        self.assertNotIn("not supported", text)
        self.assertIn("A milestone's status can't be 'Deleted': use `delete_milestone`.", text)
        self.assertEqual(text.count("'convert'"), 1)
        self.assertEqual(text.count("MOVES the milestone"), 1)
        # in the catalog's own place, before "Everything is checked ..."
        self.assertLess(text.index("'convert'"), text.index("Everything is checked"))

    def test_the_milestone_id_argument_keeps_its_hint_in_both_dialects(self):
        # `milestone_id` is also a reference (the milestone's display id): untouched by the flag.
        on = declared(**ON)["manage_milestone"].parameters_schema["properties"]["milestone_id"]
        off = declared(**OFF)["manage_milestone"].parameters_schema["properties"]["milestone_id"]
        self.assertEqual(on, off)


# --------------------------------------------------------------------------------------------
# the approval card's facts
# --------------------------------------------------------------------------------------------


class CardTests(ToolBase):
    def card(self, args, ctx=None):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._friendly_arguments(args, "manage_milestone", ctx=ctx or self.ctx())

    def test_a_task_to_milestone_card_names_the_task_its_sub_tasks_and_assignee(self):
        t = self.task("Plan the launch")
        c1 = self.task("A", parent_task_id=t.task_id, root_task_id=t.task_id)
        self.task("B", parent_task_id=c1.task_id, root_task_id=t.task_id)
        out = self.card({"action": "convert", "task_id": t.task_id})
        self.assertEqual(out["task_id"], t.display_id)
        self.assertEqual(
            out["current"],
            {
                "direction": "task_to_milestone",
                "title": "Plan the launch",
                "display_id": t.display_id,
                "sub_tasks": 2,
                "assignee": "alice",
            },
        )

    def test_a_milestone_to_task_card_says_what_stays_and_what_is_dropped(self):
        m = self.roadmap_milestone()
        out = self.card({"action": "convert", "milestone_id": m.milestone_id})
        self.assertEqual(
            out["current"],
            {
                "direction": "milestone_to_task",
                "title": "Launch",
                "display_id": m.task.display_id,
                "tasks": 3,
                "assignee": "alice",
                "collaborators": ["bob"],
                "folder": "Roadmap",
                "sprint": "S1",
            },
        )

    def test_the_task_count_is_the_live_ones(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(is_deleted=True, status="Deleted")
        out = self.card({"action": "convert", "milestone_id": m.milestone_id})
        self.assertEqual(out["current"]["tasks"], 2)

    def test_a_milestone_with_nobody_on_it_has_no_assignee_and_no_collaborators(self):
        m = self.milestone("Empty")
        cur = self.card({"action": "convert", "milestone_id": m.milestone_id})["current"]
        self.assertEqual(
            (cur["assignee"], cur["collaborators"], cur["folder"], cur["sprint"]),
            (None, [], None, None),
        )

    def test_a_move_card_names_the_project_it_leaves_the_tasks_that_go_and_what_is_cleared(self):
        tag = [{"tagName": "Feature"}]
        m = self.roadmap_milestone(tags=tag)
        out = self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": self.dest.project_id}
        )
        self.assertEqual(out["project_id"], "Destination")
        self.assertEqual(out["current"]["project_id"], "Conv")
        self.assertEqual(
            out["current"]["move"],
            {"tasks": 3, "sprint": "S1", "folder": "Roadmap", "tags": ["Feature"]},
        )

    def test_no_move_facts_when_the_project_is_the_same_or_the_flag_is_off_or_no_project(self):
        m = self.roadmap_milestone()
        same = self.card(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.project.project_id,
                "title": "X",
            }
        )
        self.assertNotIn("move", same["current"])
        plain = self.card({"action": "update", "milestone_id": m.milestone_id, "title": "X"})
        self.assertNotIn("move", plain["current"])
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            off = self.card(
                {
                    "action": "update",
                    "milestone_id": m.milestone_id,
                    "project_id": self.dest.project_id,
                    "title": "X",
                }
            )
        self.assertNotIn("move", off["current"])
        self.assertNotIn("project_id", off["current"])

    def test_a_destination_the_person_is_not_on_shows_no_move_facts(self):
        m = self.roadmap_milestone()
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.bob,
            project_system_user=self.bob,
        )
        out = self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": hidden.project_id}
        )
        self.assertNotIn("move", out.get("current", {}))
        self.assertEqual(out["project_id"], hidden.project_id)  # raw: the name is not for them

    def test_an_item_the_person_cannot_read_shows_nothing(self):
        # a task and a milestone of a project bob is not on, shown to bob
        hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Hidden",
            code="HID",
            owner=self.owner,
            project_system_user=self.owner,
        )
        t = self.task("Theirs", project=hidden, assignee=self.owner)
        bob = self.ctx(self.bob)
        self.assertNotIn("current", self.card({"action": "convert", "task_id": t.task_id}, ctx=bob))
        from origin.services.milestone_service import create_milestone  # noqa: PLC0415

        m = create_milestone(hidden, reporter_id=self.owner.id, title="Theirs too")
        self.assertNotIn(
            "current", self.card({"action": "convert", "milestone_id": m.milestone_id}, ctx=bob)
        )

    def test_both_ids_or_neither_shows_nothing_and_does_not_break_the_card(self):
        t = self.task("T")
        m = self.milestone("M")
        for args in (
            {"action": "convert"},
            {"action": "convert", "task_id": t.task_id, "milestone_id": m.milestone_id},
            {"action": "convert", "task_id": "x", "milestone_id": ""},
            {"action": "convert", "task_id": True},
        ):
            with self.subTest(args=args):
                self.assertNotIn("current", self.card(args))

    def test_the_card_changes_no_row(self):
        m = self.roadmap_milestone()
        before = list(
            TaskMaster.objects.order_by("pk").values_list("pk", "ts_updated_at", "project_id")
        )
        self.card({"action": "convert", "milestone_id": m.milestone_id})
        self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": self.dest.project_id}
        )
        after = list(
            TaskMaster.objects.order_by("pk").values_list("pk", "ts_updated_at", "project_id")
        )
        self.assertEqual(before, after)


class ChipsTests(ToolBase):
    def sources(self, result):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._ui_sources_from_tool_result("manage_milestone", dict(result))

    def test_a_milestone_turned_into_a_task_chips_the_task_not_a_dead_milestone(self):
        m = self.roadmap_milestone()
        out = self.to_task_tool(m)
        (src,) = self.sources(out)
        self.assertEqual(src["entity_type"], "task")
        self.assertEqual(src["entity_id"], f"task:{out['task_id']}")

    def test_a_task_turned_into_a_milestone_and_a_moved_milestone_chip_the_milestone(self):
        t = self.task("T")
        out = self.to_milestone_tool(t)
        (src,) = self.sources(out)
        self.assertEqual(src["entity_type"], "milestone")
        self.assertEqual(src["entity_id"], f"milestone:{out['milestone_id']}")
        moved = self.move_tool(MilestoneMaster.objects.get(milestone_id=out["milestone_id"]))
        (src,) = self.sources(moved)
        self.assertEqual(
            (src["entity_type"], src["entity_id"]),
            ("milestone", f"milestone:{out['milestone_id']}"),
        )


# --------------------------------------------------------------------------------------------
# the undo map, the notices, the use markers
# --------------------------------------------------------------------------------------------


class UndoTests(ToolBase):
    def test_the_tool_is_not_undoable_and_says_why_for_every_action(self):
        from origin.search_engine.agent import undo  # noqa: PLC0415

        self.assertEqual(undo.UNDO_SUPPORT["manage_milestone"], undo.NONE)
        code, line = undo.UNDO_REASONS["manage_milestone"]
        self.assertEqual(code, undo.NO_SNAPSHOT)
        self.assertIn("convert drops its folder and sprint", line)
        self.assertIn("move to another project clears", line)

    def test_a_proposal_for_a_convert_or_a_move_is_not_undoable(self):
        from origin.search_engine.agent import undo  # noqa: PLC0415

        for args in (
            {"action": "convert", "task_id": 1},
            {"action": "convert", "milestone_id": 1},
            {"action": "update", "milestone_id": 1, "project_id": 2},
        ):
            proposal = undo.proposal("manage_milestone", args)
            self.assertFalse(proposal.undoable, args)
            self.assertEqual(proposal.reason, undo.NO_SNAPSHOT)


class NoticeAndMarkerTests(ToolBase):
    def test_a_convert_or_a_move_sends_no_ai_app_notice(self):
        # MIP-1432 keeps small edits silent; a convert and a move are edits (decision for the
        # maintainer: no new verb, no new switch).
        from origin.services import mcp_notice  # noqa: PLC0415

        t = self.task("T")
        for args, payload in (
            ({"action": "convert", "task_id": t.task_id}, self.to_milestone_tool(t)),
            (
                {"action": "update", "milestone_id": 1, "project_id": self.dest.project_id},
                self.move_tool(self.milestone("M")),
            ),
        ):
            self.assertIsNone(mcp_notice.build_notice("manage_milestone", args, payload))

    def test_the_use_marker_names_the_converted_task_or_milestone(self):
        from origin.services import member_events  # noqa: PLC0415

        self.assertEqual(
            member_events.subjects("manage_milestone", {"action": "convert", "task_id": 7}),
            [("task", "7")],
        )
        self.assertEqual(
            member_events.subjects("manage_milestone", {"action": "convert", "milestone_id": 9}),
            [("milestone", "9")],
        )
        self.assertEqual(
            member_events.subjects(
                "manage_milestone", {"action": "update", "milestone_id": 9, "project_id": 2}
            ),
            [("milestone", "9")],
        )
        self.assertEqual(
            member_events.subjects("manage_milestone", {"action": "create", "project_id": 2}), []
        )


# --------------------------------------------------------------------------------------------
# the in-app loop: the pause for approval, the card, the approved run
# --------------------------------------------------------------------------------------------


class LoopBase(ToolBase):
    def paused(self, args):
        import uuid  # noqa: PLC0415

        from origin.search_engine.models import AgentRun, AgentStep  # noqa: PLC0415

        run = AgentRun.objects.create(
            team_id=self.ctx().team_id,
            user_id=self.ctx().user_id,
            query="convert it",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        from origin.search_engine.agent import controller  # noqa: PLC0415

        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="manage_milestone",
            arguments_json=args,
            summary=controller.PENDING_APPROVAL_MARKER,
        )
        return run

    def approve(self, args, **flags):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        events = []
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, flags),
            mock.patch.object(controller, "_drive_loop", return_value=None),
        ):
            controller.resume_agent(self.paused(args), "approve", self.ctx(), events.append)
        return events


class LoopTests(LoopBase):
    def drive(self, args, **flags):
        from origin.search_engine.agent import controller  # noqa: PLC0415
        from origin.search_engine.llm.types import AgentMessage, FunctionCall  # noqa: PLC0415

        class Client:
            def __init__(self):
                self.script = [
                    [(None, FunctionCall(name="manage_milestone", args=args))],
                    [("done", None)],
                ]

            def generate_step(self, messages, tools, system_instruction, **kwargs):
                yield from self.script.pop(0)

        events = []
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, flags),
            mock.patch.object(controller, "get_model_client", return_value=Client()),
        ):
            pause = controller._drive_loop(
                messages=[AgentMessage(role="user", text="convert it")],
                ctx=self.ctx(),
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
                disabled_tools=None,
            )
        return events, pause

    def card(self, args, **flags):
        events, pause = self.drive(args, **flags)
        self.assertIsNotNone(pause)
        (card,) = [e for e in events if e["type"] == "tool_call_pending_approval"]
        return card

    def test_a_convert_pauses_for_approval_with_the_card_and_runs_nothing(self):
        t = self.task("Plan the launch")
        card = self.card({"action": "convert", "task_id": t.task_id}, **ON)
        self.assertEqual(card["tool_name"], "manage_milestone")
        self.assertEqual(card["arguments"]["current"]["direction"], "task_to_milestone")
        self.assertEqual(card["arguments"]["current"]["title"], "Plan the launch")
        self.assertIs(card["undoable"], False)
        self.assertEqual(card["undo_reason"], "no_snapshot")
        self.assertFalse(self.fresh(t).is_milestone)

    def test_a_display_id_resolves_before_the_card_and_the_tool_see_it(self):
        t = self.task("Plan the launch")
        card = self.card({"action": "convert", "task_id": t.display_id}, **ON)
        self.assertEqual(card["arguments"]["current"]["display_id"], t.display_id)

    def test_a_move_pauses_with_what_it_does(self):
        m = self.roadmap_milestone()
        card = self.card(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
            },
            **ON,
        )
        self.assertEqual(card["arguments"]["project_id"], "Destination")
        self.assertEqual(card["arguments"]["current"]["move"]["tasks"], 3)
        self.assertIs(card["undoable"], False)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_with_the_flag_off_the_card_promises_nothing_about_a_move(self):
        m = self.roadmap_milestone()
        card = self.card(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
            },
            **OFF,
        )
        self.assertNotIn("move", card["arguments"].get("current", {}))

    def test_the_declared_tool_carries_the_action_only_while_the_flag_is_on(self):
        from origin.search_engine.agent.controller import _build_tool_declarations  # noqa: PLC0415

        for flags, expect in ((ON, True), (OFF, False)):
            with mock.patch.dict(settings.SEARCH_ENGINE, flags):
                decl = {d.name: d for d in _build_tool_declarations()}["manage_milestone"]
            self.assertEqual(
                "convert" in decl.parameters_schema["properties"]["action"]["enum"], expect
            )


class ApproveTests(LoopBase):
    def test_approving_a_convert_runs_it_and_tells_the_browser_what_to_drop(self):
        t = self.task("Plan the launch")
        events = self.approve({"action": "convert", "task_id": t.task_id}, **ON)
        (result,) = [e for e in events if e["type"] == "tool_call_result"]
        row = self.fresh(t)
        self.assertTrue(row.is_milestone)
        self.assertEqual(
            result["milestone_change"],
            {
                "kind": "convert",
                "direction": "task_to_milestone",
                "task_id": t.task_id,
                "milestone_id": row.milestone_id,
                "project_id": self.project.project_id,
            },
        )
        # no Undo is offered: the answer is stored with no pre-image
        self.assertFalse(result.get("undo"))

    def test_approving_a_milestone_to_task_names_the_retired_milestone(self):
        m = self.roadmap_milestone()
        events = self.approve({"action": "convert", "milestone_id": m.milestone_id}, **ON)
        (result,) = [e for e in events if e["type"] == "tool_call_result"]
        self.assertEqual(
            result["milestone_change"],
            {
                "kind": "convert",
                "direction": "milestone_to_task",
                "task_id": m.task_id,
                "milestone_id": m.milestone_id,
                "project_id": self.project.project_id,
            },
        )
        self.assertTrue(MilestoneMaster.objects.get(pk=m.pk).is_deleted)

    def test_approving_a_move_says_which_project_to_re_read_too(self):
        m = self.roadmap_milestone()
        events = self.approve(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
            },
            **ON,
        )
        (result,) = [e for e in events if e["type"] == "tool_call_result"]
        self.assertEqual(
            result["milestone_change"],
            {
                "kind": "move",
                "task_id": m.task_id,
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
                "from_project_id": self.project.project_id,
            },
        )

    def test_an_ordinary_milestone_write_carries_no_such_ref(self):
        m = self.milestone("Plain")
        events = self.approve(
            {"action": "update", "milestone_id": m.milestone_id, "title": "Renamed"}, **ON
        )
        for event in events:
            self.assertNotIn("milestone_change", event)

    def test_approving_after_the_flag_went_off_changes_nothing(self):
        t = self.task("Plan the launch")
        events = self.approve({"action": "convert", "task_id": t.task_id}, **OFF)
        errors = [e["error"] for e in events if e["type"] == "tool_call_error"]
        self.assertEqual(len(errors), 1)
        self.assertIn("`action` must be one of", errors[0])
        self.assertNotIn("convert'", errors[0].split("(got")[0])
        self.assertFalse(self.fresh(t).is_milestone)
        m = self.milestone("Stay")
        self.approve(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
            },
            **OFF,
        )
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_a_member_on_the_read_tier_cannot_get_it_approved_even_by_name(self):
        from origin.search_engine.agent import controller, tool_tiers  # noqa: PLC0415

        t = self.task("Plan the launch")
        events = []
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, ON),
            mock.patch.object(controller, "_drive_loop", return_value=None),
        ):
            controller.resume_agent(
                self.paused({"action": "convert", "task_id": t.task_id}),
                "approve",
                self.ctx(),
                events.append,
                disabled_tools=tool_tiers.disabled_tools_for_level("read"),
            )
        self.assertIn(
            "no longer available",
            [e["error"] for e in events if e["type"] == "tool_call_error"][0],
        )
        self.assertFalse(self.fresh(t).is_milestone)

    def test_a_refused_convert_is_an_error_the_model_can_act_on_and_changes_nothing(self):
        parent = self.task("Parent")
        sub = self.task("Sub", parent_task_id=parent.task_id, root_task_id=parent.task_id)
        events = self.approve({"action": "convert", "task_id": sub.task_id}, **ON)
        error = [e["error"] for e in events if e["type"] == "tool_call_error"][0]
        self.assertIn("`update_task` with `parent_task_id` ''", error)
        self.assertFalse(self.fresh(sub).is_milestone)


# --------------------------------------------------------------------------------------------
# MCP
# --------------------------------------------------------------------------------------------


class McpMilestoneBase(McpBase):
    """McpBase's task is WRD-7 (a root task; `self.user` is its assignee and a member)."""

    flag = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, ON if self.flag else OFF)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.milestone = create_milestone(
            self.project, reporter_id=self.user.id, title="Launch the redesign"
        )
        self.other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.other, attendee=self.user)

    def text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class McpFlagOnTests(McpMilestoneBase):
    def test_a_write_key_is_offered_the_action_and_the_parameter(self):
        self._key(scope=SCOPE_WRITE)
        tool = self.listed()["manage_milestone"]
        props = tool["inputSchema"]["properties"]
        self.assertIn("convert", props["action"]["enum"])
        self.assertEqual(props["task_id"]["type"], "string")
        self.assertIn("convert", tool["description"])

    def test_a_task_is_turned_into_a_milestone_by_its_display_id(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "task_id": self.task.display_id}
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_milestone)
        self.assertIn(self.task.display_id, text)
        self.assertIn("task_to_milestone", text)

    def test_a_milestone_is_turned_into_a_task_by_its_display_id_or_its_number(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone",
            {"action": "convert", "milestone_id": self.milestone.task.display_id},
        )
        self.assertFalse(is_error, text)
        self.assertIn("milestone_to_task", text)
        self.assertTrue(MilestoneMaster.objects.get(pk=self.milestone.pk).is_deleted)
        again = create_milestone(self.project, reporter_id=self.user.id, title="Second")
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "milestone_id": again.milestone_id}
        )
        self.assertFalse(is_error, text)

    def test_a_milestone_is_moved_by_a_number_and_the_result_says_what_happened(self):
        self._key(scope=SCOPE_WRITE)
        result = self._result(
            self._call(
                "manage_milestone",
                {
                    "action": "update",
                    "milestone_id": self.milestone.milestone_id,
                    "project_id": self.other.project_id,
                },
            )
        )
        self.assertFalse(result.get("isError"), result["content"][0]["text"])
        moved = result["structuredContent"]["moved"]
        self.assertEqual(moved["from_project_name"], "Website Redesign")
        self.assertEqual(moved["cleared"], ["sprint", "folder", "tags"])
        self.assertEqual(
            MilestoneMaster.objects.get(pk=self.milestone.pk).project_id, self.other.project_id
        )

    def test_a_refusal_is_a_sentence_with_the_next_step_and_changes_nothing(self):
        self._key(scope=SCOPE_WRITE)
        sub = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Sub",
            status="Open",
            parent_task_id=self.task.task_id,
            root_task_id=self.task.task_id,
            reporter=self.user,
        )
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "task_id": sub.task_id}
        )
        self.assertTrue(is_error)
        self.assertIn("`update_task` with `parent_task_id` ''", text)
        sub.refresh_from_db()
        self.assertFalse(sub.is_milestone)

    def test_a_read_only_key_cannot_use_it_and_nothing_changes(self):
        self._key(scope=SCOPE_READ)
        self.assertNotIn("manage_milestone", self.listed())
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "task_id": self.task.task_id}
        )
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_milestone)

    def test_neither_is_destructive_so_a_write_key_without_the_opt_in_may_do_both(self):
        # Decision: nothing is deleted for good (see the PR); a key that may change things may
        # convert and move, and the announcement stays `destructiveHint: true` as for every overwrite.
        self._key(scope=SCOPE_WRITE, allow_destructive=False)
        tool = next(t for t in mcp.CATALOG if t.name == "manage_milestone")
        self.assertFalse(tool.destructive)
        self.assertIsNone(
            mcp.deleting_argument("manage_milestone", {"action": "convert", "task_id": 1})
        )
        self.assertIsNone(
            mcp.deleting_argument(
                "manage_milestone", {"action": "update", "milestone_id": 1, "project_id": 2}
            )
        )
        self.assertIs(self.listed()["manage_milestone"]["annotations"]["destructiveHint"], True)
        before = SecurityEvent.objects.count()
        _text, is_error = self.text(
            "manage_milestone", {"action": "convert", "task_id": self.task.task_id}
        )
        self.assertFalse(is_error)
        # not a delete, so it leaves no entry in the destructive-call log
        self.assertEqual(SecurityEvent.objects.count(), before)

    def test_it_leaves_a_marker_on_the_converted_task_and_no_notice(self):
        self._key(scope=SCOPE_WRITE)
        notices = Activity.objects.filter(activity_type=ActivityType.AI_APP).count()
        self.text("manage_milestone", {"action": "convert", "task_id": self.task.task_id})
        self.assertTrue(
            MemberEvent.objects.filter(
                user=self.user, object_type="task", object_id=str(self.task.task_id)
            ).exists()
        )
        self.assertEqual(
            Activity.objects.filter(activity_type=ActivityType.AI_APP).count(), notices
        )

    def test_a_project_of_another_team_is_not_found_over_mcp_too(self):
        self._key(scope=SCOPE_WRITE)
        from origin.models.common.team_models import TeamMaster, TeamMembers  # noqa: PLC0415

        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=elsewhere, attendee=self.user)
        foreign = ProjectMaster.objects.create(
            team=elsewhere,
            project_name="Foreign",
            code="FOR",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=elsewhere, project=foreign, attendee=self.user)
        text, is_error = self.text(
            "manage_milestone",
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "project_id": foreign.project_id,
            },
        )
        self.assertTrue(is_error)
        self.assertIn(f"Project {foreign.project_id} not found", text)
        self.assertEqual(
            MilestoneMaster.objects.get(pk=self.milestone.pk).project_id, self.project.project_id
        )


class McpFlagOffTests(McpMilestoneBase):
    flag = False

    def test_the_tool_a_client_sees_is_what_it_was(self):
        self._key(scope=SCOPE_WRITE)
        tool = self.listed()["manage_milestone"]
        props = tool["inputSchema"]["properties"]
        self.assertNotIn("convert", props["action"]["enum"])
        self.assertNotIn("task_id", props)
        self.assertNotIn("convert", tool["description"])
        self.assertIn("A milestone can't be moved to another project", tool["description"])

    def test_the_new_argument_is_refused_and_nothing_changes(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "task_id": self.task.task_id}
        )
        self.assertTrue(is_error)
        self.assertIn("does not take `task_id`", text)
        self.task.refresh_from_db()
        self.assertFalse(self.task.is_milestone)

    def test_the_action_alone_is_refused_in_the_words_of_an_unknown_action(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone", {"action": "convert", "milestone_id": self.milestone.milestone_id}
        )
        self.assertTrue(is_error)
        self.assertEqual(text, FLAG_OFF_ACTION_REFUSAL)
        self.assertFalse(MilestoneMaster.objects.get(pk=self.milestone.pk).is_deleted)

    def test_a_move_is_refused_as_it_always_was(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone",
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "project_id": self.other.project_id,
            },
        )
        self.assertTrue(is_error)
        self.assertEqual(text, FLAG_OFF_MOVE_REFUSAL)
        self.assertEqual(
            MilestoneMaster.objects.get(pk=self.milestone.pk).project_id, self.project.project_id
        )


# --------------------------------------------------------------------------------------------
# update_task points a milestone's own row at the tool that moves it
# --------------------------------------------------------------------------------------------

OLD_ROW_REFUSAL = "is a milestone; move it from the milestone's own menu in the app."  # the sentence it always was


class UpdateTaskRedirectTests(ToolBase):
    def refusal(self, m):
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASK.run({"task_id": m.task_id, "project_id": self.dest.project_id}, self.ctx())
        return str(caught.exception)

    def test_with_the_flag_on_the_refusal_names_the_tool_and_the_ids_that_move_it(self):
        m = self.milestone("Moves with its own tool")
        text = self.refusal(m)
        self.assertIn("`manage_milestone`", text)
        self.assertIn(f"`milestone_id` {m.milestone_id}", text)
        self.assertIn("`project_id`", text)
        self.assertNotIn("in the app", text)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_the_sentence_it_names_really_moves_it(self):
        m = self.milestone("Follow the sentence")
        self.refusal(m)
        out = self.move_tool(m)
        self.assertEqual(out["project_id"], self.dest.project_id)

    def test_a_plain_task_is_not_redirected(self):
        t = self.task("Plain")
        out = UPDATE_TASK.run(
            {"task_id": t.task_id, "project_id": self.dest.project_id}, self.ctx()
        )
        self.assertEqual(out["project_id"], self.dest.project_id)


class UpdateTaskRedirectFlagOffTests(ToolBase):
    flag = False

    def test_with_the_flag_off_it_is_the_sentence_it_always_was(self):
        m = self.milestone("Old sentence")
        with self.assertRaises(ToolError) as caught:
            UPDATE_TASK.run({"task_id": m.task_id, "project_id": self.dest.project_id}, self.ctx())
        self.assertEqual(
            str(caught.exception),
            f"Task {m.task.display_id} {OLD_ROW_REFUSAL}",
        )


# --------------------------------------------------------------------------------------------
# from the review: ids that are not ids, the lock, the live counts, the cards' mirror of the tool
# --------------------------------------------------------------------------------------------


class IdHygieneTests(ToolBase):
    def test_a_boolean_or_a_fraction_is_not_an_id_and_changes_nothing(self):
        first = self.task("Task one")  # what `true` would have meant
        m = self.milestone("Milestone")
        for args in (
            {"action": "convert", "task_id": True},
            {"action": "convert", "task_id": 1.5},
            {"action": "convert", "milestone_id": True},
            {"action": "convert", "milestone_id": 2.5},
            {"action": "update", "milestone_id": m.milestone_id, "project_id": True},
            {"action": "update", "milestone_id": m.milestone_id, "project_id": 1.5},
        ):
            with self.subTest(args=args), self.assertRaisesRegex(ToolError, "must be an id"):
                self.call(args)
        self.assertFalse(self.fresh(first).is_milestone)
        self.assertFalse(MilestoneMaster.objects.get(pk=m.pk).is_deleted)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, self.project.project_id)

    def test_a_whole_number_as_a_float_or_a_string_is_still_an_id(self):
        t = self.task("Plain")
        out = self.call({"action": "convert", "task_id": float(t.task_id)})
        self.assertEqual(out["task_id"], t.task_id)
        m = self.milestone("By string")
        out = self.call({"action": "convert", "milestone_id": str(m.milestone_id)})
        self.assertEqual(out["converted_milestone_id"], m.milestone_id)


class AlreadyAMilestoneTests(ToolBase):
    def test_the_refusal_names_the_milestone_a_task_row_stands_for(self):
        m = self.milestone("Named")
        with self.assertRaises(ToolError) as caught:
            self.to_milestone_tool(m.task)
        self.assertIn(
            f"`milestone_id` instead (it is milestone {m.milestone_id})", str(caught.exception)
        )


class LiveCountTests(ToolBase):
    def test_a_deleted_task_that_goes_with_a_move_is_not_counted(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(is_deleted=True, status="Deleted")
        out = self.move_tool(m)
        self.assertEqual(out["moved"]["tasks_moved"], 2)
        # it still moved: the count is what a person sees, not what is stored
        self.assertEqual(TaskMaster.objects.get(title="Second").project_id, self.dest.project_id)


class MoveLockTests(ToolBase):
    def test_a_milestone_converted_while_the_move_was_being_applied_is_not_revived(self):
        from origin.search_engine.agent.tools import manage_milestone as module  # noqa: PLC0415
        from origin.services.task_milestone_conversion import convert_milestone_to_task  # noqa: PLC0415

        m = self.roadmap_milestone()
        real = module._destination

        def converted_in_between(*args, **kwargs):
            destination = real(*args, **kwargs)
            convert_milestone_to_task(m.milestone_id)  # someone else, after the lookup
            return destination

        with mock.patch.object(module, "_destination", side_effect=converted_in_between):
            with self.assertRaisesRegex(ToolError, "was changed while this was being applied"):
                self.move_tool(m)
        retired = MilestoneMaster.objects.get(pk=m.pk)
        self.assertTrue(retired.is_deleted)  # not written back as a live milestone
        self.assertEqual(retired.converted_to_task_id, m.task_id)
        self.assertEqual(
            TaskMaster.objects.get(task_id=m.task_id).project_id, self.project.project_id
        )

    def test_a_milestone_moved_by_someone_else_in_between_is_not_moved_again(self):
        from origin.search_engine.agent.tools import manage_milestone as module  # noqa: PLC0415

        m = self.roadmap_milestone()
        real = module._destination
        third = ProjectMaster.objects.create(
            team=self.team,
            project_name="Third",
            code="THR",
            owner=self.owner,
            project_system_user=self.owner,
        )

        def moved_in_between(*args, **kwargs):
            destination = real(*args, **kwargs)
            MilestoneMaster.objects.filter(pk=m.pk).update(project=third)
            return destination

        with mock.patch.object(module, "_destination", side_effect=moved_in_between):
            with self.assertRaisesRegex(ToolError, "was changed while this was being applied"):
                self.move_tool(m)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).project_id, third.project_id)


class CardMirrorTests(ToolBase):
    def card(self, args, ctx=None):
        from origin.search_engine.agent import controller  # noqa: PLC0415

        return controller._friendly_arguments(args, "manage_milestone", ctx=ctx or self.ctx())

    def test_a_convert_card_says_nothing_with_the_flag_off(self):
        t = self.task("T")
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            out = self.card({"action": "convert", "task_id": t.task_id})
        self.assertNotIn("current", out)

    def test_a_call_that_also_sets_what_a_move_clears_is_not_promised_as_a_move(self):
        m = self.roadmap_milestone()
        for extra in ({"sprint_id": "5"}, {"milestone_folder_id": "3"}, {"tags": ["x"]}):
            with self.subTest(extra=extra):
                out = self.card(
                    {
                        "action": "update",
                        "milestone_id": m.milestone_id,
                        "project_id": self.dest.project_id,
                        "title": "X",
                        **extra,
                    }
                )
                self.assertNotIn("move", out["current"])

    def test_a_person_who_is_not_on_the_milestones_project_gets_no_move_card(self):
        m = self.roadmap_milestone()
        ProjectMembers.objects.create(team=self.team, project=self.dest, attendee=self.bob)
        # bob is on both projects of the team here; take him off the source and he may not move it
        ProjectMembers.objects.filter(project=self.project, attendee=self.bob).delete()
        out = self.card(
            {
                "action": "update",
                "milestone_id": m.milestone_id,
                "project_id": self.dest.project_id,
            },
            ctx=self.ctx(self.bob),
        )
        self.assertNotIn("move", out.get("current", {}))

    def test_the_move_card_counts_the_tasks_a_person_still_sees(self):
        m = self.roadmap_milestone()
        TaskMaster.objects.filter(title="Second").update(is_deleted=True, status="Deleted")
        out = self.card(
            {"action": "update", "milestone_id": m.milestone_id, "project_id": self.dest.project_id}
        )
        self.assertEqual(out["current"]["move"]["tasks"], 2)


class McpEmptyTaskIdTests(McpMilestoneBase):
    def test_an_empty_task_id_beside_a_milestone_id_is_an_unsent_one(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.text(
            "manage_milestone",
            {"action": "convert", "milestone_id": self.milestone.milestone_id, "task_id": ""},
        )
        self.assertFalse(is_error, text)
        self.assertIn("milestone_to_task", text)
