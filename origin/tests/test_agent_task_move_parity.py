"""MIP-1609 item 3: `update_task`'s own project move judges the people already assigned and says what
the move did, as the milestone move does (MIP-1583, api #881/#882) and as the app's own task PUT does
(MIP-1597). Behind the flag `AGENT_TASK_MOVE_PARITY` (default off): it adds a refusal and fields to a
tool result, so it is model-visible.

The tool only moves a task inside the caller's team (`project_move_error`), so what it can refuse is
an assignee who is on neither the destination project nor the team: someone who LEFT the team (or a
guest of the source project). The app's PUT refuses the same case with the same sentence, except for
the way out (the PUT takes an `assignee`, `update_task` does not, so the tool points at
`assign_task`).

What the flag does NOT gate: the Owner rows a move ends on task notes were already recorded (and
restored on a move back) on this path, through `relocate_task_satellites` (MIP-1583); only telling
the model about them is new.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import TaskNoteRoleRevoke
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.parity import TASK_MOVE_FLAG
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.services import task_project_move
from origin.tests.test_task_personal_note_roles_revoked import TaskNoteBase
from origin.views.utils.note_role import NOTE_TYPE_TASK, ROLE_OWNER

User = get_user_model()


def flag(on: bool):
    return mock.patch.dict(settings.SEARCH_ENGINE, {TASK_MOVE_FLAG: on})


class MoveBase(TaskNoteBase):
    """`owner` moves. `author` is on the team and on Alpha, not on Beta. `leaver` left the team (a
    soft-deleted team row, no project row). Alpha: owner + author; Beta (same team): owner only."""

    def setUp(self):
        super().setUp()
        self.alpha = self.project_of("Alpha", self.author)
        self.alpha.code = "ALP"
        self.alpha.save(update_fields=["code"])
        self.beta = self.project_of("Beta")
        self.beta.code = "BET"
        self.beta.save(update_fields=["code"])
        self.leaver = User.objects.create_user(
            username="leaver", email="leaver@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.leaver, is_deleted=True)
        self.task, self.child = self.tree()

    def tree(self):
        """A root task in Alpha with one sub-task, both assigned to and filed by the owner."""
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.alpha,
            title="a task",
            status="Open",
            assignee=self.owner,
            reporter=self.owner,
        )
        TaskMaster.objects.filter(pk=task.pk).update(root_task_id=task.task_id)
        child = TaskMaster.objects.create(
            team=self.team,
            project=self.alpha,
            title="a sub-task",
            status="Open",
            parent_task_id=task.task_id,
            root_task_id=task.task_id,
            assignee=self.owner,
            reporter=self.owner,
        )
        for row in (task, child):
            row.refresh_from_db()
        return task, child

    def ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.owner.id))

    def tool_move(self, task, project, **extra):
        return UPDATE_TASK.run(
            {"task_id": task.task_id, "project_id": project.project_id, **extra}, self.ctx()
        )

    def put_move(self, task, project, **extra):
        self.authenticate(self.owner)
        return self.client.put(
            "/api/v2/task/",
            {
                "team": str(self.team.team_id),
                "task_id": task.task_id,
                "project": project.project_id,
                **extra,
            },
            format="json",
        )

    def assert_in_alpha(self, *rows):
        for row in rows:
            row.refresh_from_db()
            self.assertEqual(row.project_id, self.alpha.project_id, row.title)


# --------------------------------------------------------------------------------------------
# (a) the people already assigned are judged against the destination
# --------------------------------------------------------------------------------------------


class ToolRefusesAnAssigneeOutsideTheDestinationTests(MoveBase):
    def test_an_assignee_who_left_the_team_refuses_the_move_and_nothing_changes(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        with flag(True), self.assertRaises(ToolError) as caught:
            self.tool_move(self.task, self.beta)
        message = str(caught.exception)
        self.assertIn(self.task.display_id, message)  # named by its display id
        self.assertIn("This task can't move to Beta", message)
        self.assertIn("is not on that project or its team", message)
        self.assertTrue(message.endswith("Nothing was changed."), message)
        self.assert_in_alpha(self.task, self.child)
        self.task.refresh_from_db()
        self.assertEqual(self.task.assignee_id, self.leaver.id)

    def test_the_way_out_is_a_tool_the_model_has_not_a_parameter_update_task_lacks(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        with flag(True), self.assertRaises(ToolError) as caught:
            self.tool_move(self.task, self.beta)
        message = str(caught.exception)
        self.assertIn("`assign_task`", message)
        self.assertNotIn("send `assignee`", message)
        self.assertNotIn("assignee", UPDATE_TASK.parameters_schema["properties"])

    def test_the_assignee_of_a_sub_task_that_goes_along_is_judged_too(self):
        TaskMaster.objects.filter(pk=self.child.pk).update(assignee=self.leaver)
        with flag(True), self.assertRaises(ToolError) as caught:
            self.tool_move(self.task, self.beta)
        self.assertIn(self.child.display_id, str(caught.exception))
        self.assertNotIn(self.task.display_id + ",", str(caught.exception))
        self.assert_in_alpha(self.task, self.child)

    def test_a_deleted_sub_task_does_not_block_the_move(self):
        TaskMaster.objects.filter(pk=self.child.pk).update(assignee=self.leaver, status="Deleted")
        with flag(True):
            out = self.tool_move(self.task, self.beta)
        self.assertIn("project", out["changed_fields"])
        self.child.refresh_from_db()
        self.assertEqual(self.child.project_id, self.beta.project_id)

    def test_a_teammate_who_is_not_on_the_destination_is_not_newly_refused(self):
        # the author is on the team, not on Beta: a new task there could be given to them too
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.author)
        with flag(True):
            out = self.tool_move(self.task, self.beta)
        self.assertEqual(out["project_id"], self.beta.project_id)

    def test_the_refusal_comes_before_any_field_is_written(self):
        # a title sent with the move is not saved when the move is refused
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        with flag(True), self.assertRaises(ToolError):
            self.tool_move(self.task, self.beta, title="renamed", priority="High")
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.priority), ("a task", None))

    def test_a_destination_of_another_team_is_a_not_found_whoever_is_assigned(self):
        # no oracle: the existing not-found answers first, the same with or without a leaver
        other_team = TeamMaster.objects.create(
            team_name="Theirs", team_email="theirs@example.com", owner=self.owner
        )
        TeamMembers.objects.create(team=other_team, attendee=self.owner)
        theirs = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", owner=self.owner, project_system_user=self.owner
        )
        ProjectMembers.objects.create(team=other_team, project=theirs, attendee=self.owner)
        answers = []
        for assignee in (self.owner, self.leaver):
            TaskMaster.objects.filter(pk=self.task.pk).update(assignee=assignee)
            with flag(True), self.assertRaises(ToolError) as caught:
                self.tool_move(self.task, theirs)
            answers.append(str(caught.exception))
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0], f"Project {theirs.project_id} not found.")

    def test_a_call_that_does_not_move_judges_nobody(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        with flag(True):
            same = self.tool_move(self.task, self.alpha)  # already there
            renamed = UPDATE_TASK.run({"task_id": self.task.task_id, "title": "x"}, self.ctx())
        self.assertEqual(same["changed_fields"], [])
        self.assertEqual(renamed["changed_fields"], ["title"])


# --------------------------------------------------------------------------------------------
# (b) what the move did: the notes it narrowed, the reporters outside the destination
# --------------------------------------------------------------------------------------------


class ToolSaysWhatTheMoveDidTests(MoveBase):
    def test_the_notes_it_narrowed_are_said_counted_over_the_task_and_its_sub_tasks(self):
        self.create_task_note(self.author, self.task)
        self.create_task_note(self.author, self.child)
        with flag(True):
            out = self.tool_move(self.task, self.beta)  # the author is not on Beta
        self.assertEqual(out["moved"], {"notes_narrowed": 2})
        self.assertIn(
            "; 2 task note(s) lost their author's Owner role, as the author is not on Beta",
            out["__summary__"],
        )
        # recorded, so a move back gives them back (MIP-1583), whatever the flag
        self.assertEqual(TaskNoteRoleRevoke.objects.filter(user=self.author).count(), 2)

    def test_a_move_back_narrows_nothing_and_writes_the_rows_back(self):
        note = self.create_task_note(self.author, self.task)
        with flag(True):
            self.tool_move(self.task, self.beta)
            self.assertIsNone(self.explicit(self.author, NOTE_TYPE_TASK, note))
            back = self.tool_move(self.task, self.alpha)
        self.assertNotIn("moved", back)
        self.assertEqual(self.explicit(self.author, NOTE_TYPE_TASK, note), ROLE_OWNER)
        self.assertFalse(TaskNoteRoleRevoke.objects.filter(user=self.author).exists())

    def test_a_reporter_outside_the_destination_is_said_and_the_task_still_moves(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(reporter=self.leaver)
        TaskMaster.objects.filter(pk=self.child.pk).update(reporter=self.leaver)
        with flag(True):
            out = self.tool_move(self.task, self.beta)
        self.assertEqual(out["moved"], {"reporters_outside_destination": 1})  # one person
        self.assertIn(
            "; 1 reporter(s) of moved tasks are not on Beta and keep reading what they filed",
            out["__summary__"],
        )
        self.assertEqual(out["project_id"], self.beta.project_id)

    def test_both_are_said_together(self):
        self.create_task_note(self.author, self.task)
        TaskMaster.objects.filter(pk=self.child.pk).update(reporter=self.leaver)
        with flag(True):
            out = self.tool_move(self.task, self.beta)
        self.assertEqual(out["moved"], {"notes_narrowed": 1, "reporters_outside_destination": 1})

    def test_a_move_with_nothing_to_say_is_what_it_always_was(self):
        with flag(False):
            off = self.tool_move(self.task, self.beta)
        task, child = self.tree()
        with flag(True):
            on = self.tool_move(task, self.beta)
        self.assertNotIn("moved", on)
        self.assertEqual(set(on), set(off))
        self.assertEqual(
            re.sub(r"BET-\d+|ALP-\d+", "#", on["__summary__"]),
            re.sub(r"BET-\d+|ALP-\d+", "#", off["__summary__"]),
        )


# --------------------------------------------------------------------------------------------
# flag off: byte-identical to before (the gap stays, as the rollback)
# --------------------------------------------------------------------------------------------


class FlagOffIsWhatItWasTests(MoveBase):
    def test_an_assignee_who_left_the_team_does_not_stop_the_move(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        with flag(False):
            out = self.tool_move(self.task, self.beta)
        self.assertEqual(out["project_id"], self.beta.project_id)
        self.assertEqual(
            out["__summary__"],
            f"Updated task {out['display_id']}: project (was {self.task.display_id})",
        )

    def test_the_result_says_nothing_about_notes_or_reporters(self):
        self.create_task_note(self.author, self.task)
        TaskMaster.objects.filter(pk=self.task.pk).update(reporter=self.leaver)
        with flag(False):
            out = self.tool_move(self.task, self.beta)
        self.assertEqual(
            set(out),
            {
                "task_id",
                "changed_fields",
                "status",
                "title",
                "display_id",
                "milestone_id",
                "parent_task_id",
                "project_id",
                "__summary__",
            },
        )
        self.assertNotIn("note", out["__summary__"])
        self.assertNotIn("reporter", out["__summary__"])
        # the record is kept whatever the flag (it is what a move back restores)
        self.assertEqual(TaskNoteRoleRevoke.objects.filter(user=self.author).count(), 1)

    def test_with_the_flag_off_neither_check_runs(self):
        # (no extra query for a member who never sees the result: the report is not computed)
        with (
            flag(False),
            mock.patch(
                "origin.search_engine.agent.tools.update_task.task_move_assignee_problem"
            ) as judged,
            mock.patch("origin.services.task_reparent.reporters_outside_destination") as counted,
        ):
            self.tool_move(self.task, self.beta)
        judged.assert_not_called()
        counted.assert_not_called()

    def test_with_the_flag_on_both_run(self):
        with (
            flag(True),
            mock.patch(
                "origin.search_engine.agent.tools.update_task.task_move_assignee_problem",
                wraps=task_project_move.task_move_assignee_problem,
            ) as judged,
            mock.patch(
                "origin.services.task_reparent.reporters_outside_destination",
                wraps=task_project_move.reporters_outside_destination,
            ) as counted,
        ):
            self.tool_move(self.task, self.beta)
        judged.assert_called_once()
        counted.assert_called_once()


# --------------------------------------------------------------------------------------------
# twins: the app's PUT and the tool (flag on) decide and say the same
# --------------------------------------------------------------------------------------------


class AppAndToolTwinTests(MoveBase):
    """Each scenario runs on two identical task trees: one moved by the PUT, one by the tool."""

    SCENARIOS = (
        "leaver on the task",
        "leaver on a sub-task",
        "leaver on a deleted sub-task",
        "teammate not on the destination",
        "notes by an author not on the destination",
        "reporter who left the team",
        "nothing special",
    )

    def arrange(self, scenario, task, child):
        if scenario == "leaver on the task":
            TaskMaster.objects.filter(pk=task.pk).update(assignee=self.leaver)
        elif scenario == "leaver on a sub-task":
            TaskMaster.objects.filter(pk=child.pk).update(assignee=self.leaver)
        elif scenario == "leaver on a deleted sub-task":
            TaskMaster.objects.filter(pk=child.pk).update(assignee=self.leaver, status="Deleted")
        elif scenario == "teammate not on the destination":
            TaskMaster.objects.filter(pk=task.pk).update(assignee=self.author)
        elif scenario == "notes by an author not on the destination":
            self.create_task_note(self.author, task)
            self.create_task_note(self.author, child)
        elif scenario == "reporter who left the team":
            TaskMaster.objects.filter(pk=task.pk).update(reporter=self.leaver)

    @staticmethod
    def shape(text, task, child):
        """The sentence without the ids of its own tree and without the way out (which differs on
        purpose: the PUT takes `assignee`, the tool does not)."""
        text = text.replace(child.display_id, "<child>").replace(task.display_id, "<task>")
        return text.split(" Reassign")[0].split(" Ask ")[0]

    def test_the_app_and_the_tool_refuse_the_same_moves_and_report_the_same_counts(self):
        for scenario in self.SCENARIOS:
            with self.subTest(scenario=scenario):
                app_task, app_child = self.tree()
                tool_task, tool_child = self.tree()
                self.arrange(scenario, app_task, app_child)
                self.arrange(scenario, tool_task, tool_child)
                res = self.put_move(app_task, self.beta)
                try:
                    with flag(True):
                        out = self.tool_move(tool_task, self.beta)
                    refused = None
                except ToolError as exc:
                    out, refused = None, str(exc)
                if res.status_code == 400:
                    self.assertEqual(res.data["code"], "assignee_not_on_destination")
                    self.assertIsNotNone(refused, "the app refused, the tool moved")
                    self.assertEqual(
                        self.shape(res.data["error"], app_task, app_child),
                        self.shape(refused, tool_task, tool_child),
                    )
                    self.assertTrue(refused.endswith("Nothing was changed."))
                    self.assert_in_alpha(app_task, tool_task)
                    continue
                self.assertEqual(res.status_code, 200, res.data)
                self.assertIsNone(refused, f"the app moved, the tool refused: {refused}")
                said = out.get("moved", {})
                self.assertEqual(res.data["notesNarrowed"], said.get("notes_narrowed", 0))
                self.assertEqual(
                    res.data["reportersOutsideDestination"],
                    said.get("reporters_outside_destination", 0),
                )
                app_task.refresh_from_db()
                tool_task.refresh_from_db()
                self.assertEqual(app_task.project_id, tool_task.project_id)

    def test_the_put_keeps_its_own_way_out(self):
        # the app's sentence is unchanged by the tool's keyword
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=self.leaver)
        res = self.put_move(self.task, self.beta)
        self.assertEqual(res.status_code, 400, res.data)
        self.assertIn("Reassign them first (or send `assignee` with the move).", res.data["error"])
        self.assertNotIn("assign_task", res.data["error"])
