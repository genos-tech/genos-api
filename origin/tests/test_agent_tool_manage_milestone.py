"""`manage_milestone` (MIP-879): milestones end to end for the agent and MCP.

Before this a milestone could only be created inside `create_task_plan` (which
needs a task), and nothing could set its assignees, folder, sprint or start
date, or manage the folders. The milestone is two rows (`MilestoneMaster` and
a mirrored backing `TaskMaster`) and the sprint reaches the milestone's tasks,
so "the tool wrote the field" is not the contract: it has to leave what the
UI's endpoints leave. The core tests drive the same change through the UI's
POST / PATCH / folder endpoints and through the tool on twin milestones, and
compare every column that says where a milestone sits and what it says.

Every refusal is also pinned to change nothing, even when the call carried
other valid changes.
"""

from __future__ import annotations

from datetime import date
from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.milestone_models import (
    MilestoneAssignees,
    MilestoneFolder,
    MilestoneMaster,
)
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.blocknote_md import task_body_blocks
from origin.search_engine.agent.tools.list_milestones import LIST_MILESTONES
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.quota import TASK_CREATE_KEY

from .test_agent_tool_task_milestone_move import MilestoneMoveTestBase

User = get_user_model()

MILESTONE_URL = "/api/v2/milestone/"
FOLDER_URL = "/api/v2/milestone/folder/"
TOOL = "origin.search_engine.agent.tools.manage_milestone"


def _strip_ids(node):
    """Block ids are minted per body; two bodies are the same when only those differ."""
    if isinstance(node, dict):
        return {k: _strip_ids(v) for k, v in node.items() if k != "id"}
    if isinstance(node, list):
        return [_strip_ids(v) for v in node]
    return node


def _state(milestone):
    """Everything that says what a milestone is and where it sits, with the
    ids that differ between twins left out."""
    m = MilestoneMaster.objects.select_related("task").get(pk=milestone.pk)
    b = m.task
    kids = list(TaskMaster.objects.filter(milestone=m).exclude(task_id=b.task_id).order_by("title"))
    return {
        "milestone": {
            "title": m.title,
            "description": _strip_ids(m.description),
            "status": m.status,
            "priority": m.priority,
            "effort_level": m.effort_level,
            "start_date": m.start_date,
            "due_date": m.due_date,
            "tags": m.tags,
            "project_id": m.project_id,
            "team_id": m.team_id,
            "sprint_id": m.sprint_id,
            "folder_id": m.folder_id,
            "reporter_id": m.reporter_id,
            "is_deleted": m.is_deleted,
        },
        "assignees": sorted(
            str(u) for u in m.milestone_assignees.values_list("user_id", flat=True)
        ),
        "backing": {
            "title": b.title,
            "content": _strip_ids(b.content),
            "status": b.status,
            "priority": b.priority,
            "effort_level": b.effort_level,
            "start_date": b.start_date,
            "due_date": b.due_date,
            "tags": b.tags,
            "assignee_id": b.assignee_id,
            "reporter_id": b.reporter_id,
            "sprint_id": b.sprint_id,
            "project_id": b.project_id,
            "team_id": b.team_id,
            "is_milestone": b.is_milestone,
            "is_deleted": b.is_deleted,
            "parent_task_id": b.parent_task_id,
            "milestone_is_this_one": b.milestone_id == m.milestone_id,
        },
        "history": sorted(
            (a.action_type, str(a.old_value), str(a.new_value))
            for a in TaskActivity.objects.filter(task=b)
        ),
        "kids": [
            (
                k.title,
                k.sprint_id,
                k.milestone_id == m.milestone_id,
                sorted(
                    (a.action_type, str(a.old_value), str(a.new_value))
                    for a in TaskActivity.objects.filter(task=k)
                    if a.action_type != "created"
                ),
            )
            for k in kids
        ],
    }


class MilestoneToolBase(MilestoneMoveTestBase):
    def setUp(self):
        super().setUp()
        self.sprint2 = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S2",
            sequence_number=2,
            start_date="2026-10-15",
            end_date="2026-10-28",
        )
        self.folder = MilestoneFolder.objects.create(
            team=self.team, project=self.project, name="Launch"
        )
        self.folder2 = MilestoneFolder.objects.create(
            team=self.team, project=self.project, name="Growth", sort_order=1
        )
        for tag_id, name in enumerate(("Feature", "Bug"), start=1):
            ProjectTags.objects.create(
                team=self.team,
                project=self.project,
                tag_id=tag_id,
                tag_name=name,
                tag_color="#00FF00",
                tag_text_color="#000000",
            )
        self.authenticate()

    # -- driving --

    def _tool(self, **args):
        return MANAGE_MILESTONE.run(args, self.ctx)

    def _post(self, **body):
        res = self.client.post(MILESTONE_URL, body, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return MilestoneMaster.objects.get(milestone_id=res.data["milestone"]["milestoneId"])

    def _patch(self, milestone, **body):
        res = self.client.patch(f"{MILESTONE_URL}{milestone.milestone_id}/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _twins(self, with_kid=True, **kwargs):
        """Two identical milestones, each with one task in it."""
        pair = []
        for _ in range(2):
            m = self._milestone("Twin", **kwargs)
            if with_kid:
                self._task("kid", milestone=m, parent_task_id=m.task_id, sprint=m.sprint)
            pair.append(m)
        return pair

    def _same_as_the_ui(self, tool_args, rest_body, **twin_kwargs):
        """The same change through the tool and the UI's PATCH lands two identical
        milestones. Returns the tool's result."""
        by_tool, by_ui = self._twins(**twin_kwargs)
        out = self._tool(action="update", milestone_id=by_tool.milestone_id, **tool_args)
        self._patch(by_ui, **rest_body)
        self.assertEqual(_state(by_tool), _state(by_ui))
        return out

    # -- fixtures --

    def _other_team_project(self):
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        project = ProjectMaster.objects.create(
            team=other_team,
            project_name="Elsewhere",
            code="ELS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=other_team, project=project, attendee=self.user2)
        return other_team, project

    def _project_i_am_not_in(self, name="Closed room", code="CLS"):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        return project

    def _unchanged(self, *milestones):
        """Assert nothing about these milestones, or the milestone and folder
        counts, moves between now and the returned call."""
        before = [_state(m) for m in milestones]
        counts = (MilestoneMaster.objects.count(), MilestoneFolder.objects.count())

        def check():
            self.assertEqual([_state(m) for m in milestones], before)
            self.assertEqual(
                (MilestoneMaster.objects.count(), MilestoneFolder.objects.count()), counts
            )

        return check


# --------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------


class CreateMilestoneTests(MilestoneToolBase):
    def _full_args(self):
        return {
            "title": "Ship the thing",
            "description_markdown": "### Goal\nShip it.\n\n- one\n- two",
            "status": "WIP",
            "priority": "High",
            "effort_level": "Moderate",
            "start_date": "2026-11-01",
            "due_date": "2026-12-01",
            "tags": ["Feature"],
            "assignee_ids": [str(self.user2.id)],
            "milestone_folder_id": str(self.folder.folder_id),
            "sprint_id": str(self.sprint2.sprint_id),
        }

    def test_create_matches_the_uis_post(self):
        args = self._full_args()
        out = self._tool(action="create", project_id=self.project.project_id, **args)
        by_tool = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        by_ui = self._post(
            project_id=self.project.project_id,
            title=args["title"],
            description=task_body_blocks(args["description_markdown"]),
            status="WIP",
            priority="High",
            effort_level="Moderate",
            start_date="2026-11-01",
            due_date="2026-12-01",
            tags=[{"tagName": "Feature", "tagColor": "#00FF00", "tagTextColor": "#000000"}],
            assignee_ids=[str(self.user2.id)],
            folder_id=self.folder.folder_id,
            sprint_id=self.sprint2.sprint_id,
        )

        self.assertEqual(_state(by_tool), _state(by_ui))
        # And it is a whole milestone: a backing row that nests under it, the
        # sprint, the folder, the assignee, the reporter.
        state = _state(by_tool)
        self.assertEqual(state["milestone"]["sprint_id"], self.sprint2.sprint_id)
        self.assertEqual(state["milestone"]["folder_id"], self.folder.folder_id)
        self.assertEqual(state["assignees"], [str(self.user2.id)])
        self.assertEqual(state["milestone"]["reporter_id"], self.user.id)
        self.assertTrue(state["backing"]["milestone_is_this_one"])
        self.assertTrue(state["backing"]["is_milestone"])

    def test_only_a_title_is_needed(self):
        out = self._tool(action="create", project_id=self.project.project_id, title="Bare")
        by_tool = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        by_ui = self._post(project_id=self.project.project_id, title="Bare")

        self.assertEqual(_state(by_tool), _state(by_ui))
        self.assertEqual(out["status"], "Open")
        self.assertEqual(out["tags"], [])
        self.assertEqual(out["assignee_ids"], [])

    def test_the_result_names_the_milestone_and_its_backing_task(self):
        out = self._tool(action="create", project_id=self.project.project_id, title="Named")

        m = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        self.assertEqual(out["action"], "create")
        self.assertEqual(out["task_id"], m.task_id)
        self.assertEqual(out["display_id"], m.task.display_id)
        self.assertEqual(out["project_id"], self.project.project_id)
        self.assertIn("Named", out["__summary__"])

    def test_a_blank_enum_on_create_means_unset(self):
        out = self._tool(
            action="create",
            project_id=self.project.project_id,
            title="Blank",
            priority="",
            effort_level="",
            status="",
            start_date="",
        )
        self.assertEqual(
            (out["priority"], out["effort_level"], out["status"]), (None, None, "Open")
        )

    def test_the_project_task_cache_is_dropped(self):
        with mock.patch(f"{TOOL}.invalidate_project_tasks_cache") as dropped:
            self._tool(action="create", project_id=self.project.project_id, title="Cache")
        dropped.assert_called_once_with(self.team.team_id, self.project.project_id)

    def test_it_counts_as_one_task_creation(self):
        with mock.patch(f"{TOOL}.increment_usage") as charged:
            self._tool(action="create", project_id=self.project.project_id, title="Charged")
        charged.assert_called_once_with(str(self.user.id), TASK_CREATE_KEY)

    def test_the_monthly_task_cap_stops_it(self):
        unchanged = self._unchanged()
        with mock.patch(f"{TOOL}.check_remaining_monthly", return_value=(False, 5, 5)):
            with self.assertRaises(ToolError) as caught:
                self._tool(action="create", project_id=self.project.project_id, title="Over")
        self.assertIn("5 tasks", str(caught.exception))
        unchanged()

    def test_a_body_with_a_mention_pings_the_person(self):
        with mock.patch(f"{TOOL}.notify_task_body_mentions") as pinged:
            out = self._tool(
                action="create",
                project_id=self.project.project_id,
                title="Mentions",
                description_markdown="Goal",
            )
        backing = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertEqual(pinged.call_args.args[0].task_id, backing.task_id)
        self.assertEqual(pinged.call_args.kwargs, {"actor_id": str(self.user.id)})

    def test_a_milestone_without_tasks_is_listed(self):
        out = self._tool(action="create", project_id=self.project.project_id, title="Empty")
        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        row = next(r for r in listed["milestones"] if r["milestone_id"] == out["milestone_id"])
        self.assertEqual(row["tasks_total"], 0)


class CreateRefusalTests(MilestoneToolBase):
    def _refused(self, message, **args):
        unchanged = self._unchanged()
        args = {"project_id": self.project.project_id, "title": "Refused", **args}
        with self.assertRaises(ToolError) as caught:
            self._tool(action="create", **args)
        self.assertIn(message, str(caught.exception))
        self.assertIn("Nothing was created", str(caught.exception))
        unchanged()

    def test_a_bad_value_creates_nothing_even_beside_good_ones(self):
        self._refused("`priority` must be one of", priority="Urgent", tags=["Feature"])
        self._refused("`effort_level` must be one of", effort_level="Huge")
        self._refused("`status` must be one of", status="Done")
        self._refused("ISO 8601 date", due_date="next friday")
        self._refused("ISO 8601 date", start_date="2026-13-45")

    def test_deleted_is_not_a_status_here(self):
        self._refused("delete_milestone", status="Deleted")

    def test_an_empty_or_long_title_is_refused(self):
        unchanged = self._unchanged()
        for title in ("", "   "):
            with self.assertRaises(ToolError):
                self._tool(action="create", project_id=self.project.project_id, title=title)
        with self.assertRaises(ToolError) as caught:
            self._tool(action="create", project_id=self.project.project_id, title="x" * 256)
        self.assertIn("too long", str(caught.exception))
        unchanged()

    def test_an_unknown_tag_is_refused_and_listed(self):
        self._refused("unknown tag", tags=["Nope"])

    def test_assignees_must_be_people_on_the_team_or_project(self):
        stranger = User.objects.create_user(username="stranger", email="s@x.com", password="pw")
        self._refused("not an active member of this team", assignee_ids=[str(stranger.id)])
        self._refused("is not a user id", assignee_ids=["not-a-uuid"])

    def test_a_folder_or_sprint_of_another_project_is_refused(self):
        elsewhere = self._project_i_am_not_in("Other", "OTH")
        ProjectMembers.objects.create(team=self.team, project=elsewhere, attendee=self.user)
        foreign_folder = MilestoneFolder.objects.create(
            team=self.team, project=elsewhere, name="Theirs"
        )
        foreign_sprint = Sprint.objects.create(
            team=self.team,
            project=elsewhere,
            name="X",
            sequence_number=1,
            start_date="2026-10-01",
            end_date="2026-10-14",
        )
        self._refused(
            "not found in this project", milestone_folder_id=str(foreign_folder.folder_id)
        )
        self._refused("not found in this project", sprint_id=str(foreign_sprint.sprint_id))
        self._refused("not found in this project", milestone_folder_id="987654")
        self._refused("not found in this project", sprint_id="987654")

    def test_a_deleted_sprint_is_refused(self):
        Sprint.objects.filter(pk=self.sprint2.pk).update(is_deleted=True)
        self._refused("not found in this project", sprint_id=str(self.sprint2.sprint_id))

    def test_a_body_over_the_size_cap_is_refused(self):
        unchanged = self._unchanged()
        with self.settings(AGENT_MAX_BODY_BYTES=100):
            with self.assertRaises(ToolError) as caught:
                self._tool(
                    action="create",
                    project_id=self.project.project_id,
                    title="Big",
                    description_markdown="x" * 200,
                )
        self.assertIn("too large", str(caught.exception))
        unchanged()

    def test_every_problem_is_named_in_one_refusal(self):
        unchanged = self._unchanged()
        with self.assertRaises(ToolError) as caught:
            self._tool(
                action="create",
                project_id=self.project.project_id,
                title="Many",
                priority="Urgent",
                effort_level="Huge",
            )
        self.assertIn("priority", str(caught.exception))
        self.assertIn("effort_level", str(caught.exception))
        unchanged()

    def test_a_failure_while_writing_rolls_the_milestone_back(self):
        unchanged = self._unchanged()
        with mock.patch(
            f"{TOOL}.create_milestone", side_effect=RuntimeError("db went away")
        ) as boom:
            with self.assertRaises(ToolError) as caught:
                self._tool(action="create", project_id=self.project.project_id, title="Boom")
        boom.assert_called_once()
        self.assertIn("nothing was created", str(caught.exception))
        self.assertNotIn("db went away", str(caught.exception))
        unchanged()


# --------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------


class UpdateMilestoneTests(MilestoneToolBase):
    def test_title(self):
        self._same_as_the_ui({"title": "Renamed"}, {"title": "Renamed"})

    def test_status(self):
        out = self._same_as_the_ui({"status": "WIP"}, {"status": "WIP"})
        self.assertEqual(out["changed_fields"], ["status"])
        self.assertEqual(out["status"], "WIP")

    def test_priority_and_effort(self):
        self._same_as_the_ui(
            {"priority": "Critical", "effort_level": "Extensive"},
            {"priority": "Critical", "effort_level": "Extensive"},
        )

    def test_the_start_date_is_settable(self):
        """The field nothing could set before."""
        out = self._same_as_the_ui({"start_date": "2026-11-03"}, {"start_date": "2026-11-03"})
        self.assertEqual(out["start_date"], "2026-11-03")
        self.assertEqual(out["changed_fields"], ["start_date"])

    def test_the_due_date(self):
        self._same_as_the_ui({"due_date": "2026-12-24"}, {"due_date": "2026-12-24"})

    def test_a_date_is_cleared_with_an_empty_string(self):
        out = self._same_as_the_ui(
            {"start_date": "", "due_date": ""},
            {"start_date": None, "due_date": None},
            start_date=date(2026, 11, 1),
            due_date=date(2026, 12, 1),
        )
        self.assertEqual(out["changed_fields"], ["start_date(cleared)", "due_date(cleared)"])
        self.assertIsNone(out["due_date"])

    def test_tags_replace_and_clear(self):
        tag = {"tagName": "Bug", "tagColor": "#00FF00", "tagTextColor": "#000000"}
        out = self._same_as_the_ui({"tags": ["Bug"]}, {"tags": [tag]})
        self.assertEqual(out["tags"], ["Bug"])
        self._same_as_the_ui({"tags": []}, {"tags": None}, tags=[tag])

    def test_a_tag_reorder_is_not_a_change(self):
        both = [
            {"tagName": "Feature", "tagColor": "#00FF00", "tagTextColor": "#000000"},
            {"tagName": "Bug", "tagColor": "#00FF00", "tagTextColor": "#000000"},
        ]
        m = self._milestone("Tagged", tags=both)
        out = self._tool(action="update", milestone_id=m.milestone_id, tags=["Bug", "Feature"])
        self.assertEqual(out["changed_fields"], [])

    def test_description(self):
        blocks = task_body_blocks("### Goal\nMore")
        out = self._same_as_the_ui(
            {"description_markdown": "### Goal\nMore"}, {"description": blocks}
        )
        self.assertEqual(out["changed_fields"], ["description"])

    def test_the_description_is_pushed_to_the_editor_and_mentions_filed(self):
        m = self._milestone("Body")
        with (
            mock.patch(f"{TOOL}.push_task_body") as pushed,
            mock.patch(f"{TOOL}.notify_task_body_mentions") as pinged,
        ):
            self._tool(action="update", milestone_id=m.milestone_id, description_markdown="New")
        backing = TaskMaster.objects.get(task_id=m.task_id)
        pushed.assert_called_once()
        self.assertEqual(pushed.call_args.args[0], backing.task_id)
        self.assertEqual(_strip_ids(pushed.call_args.args[1]), _strip_ids(backing.content))
        self.assertEqual(pinged.call_args.args[0].task_id, backing.task_id)

    def test_no_editor_push_when_the_description_is_not_sent(self):
        m = self._milestone("NoBody")
        with mock.patch(f"{TOOL}.push_task_body") as pushed:
            self._tool(action="update", milestone_id=m.milestone_id, title="Other")
        pushed.assert_not_called()

    def test_folder_in_and_out(self):
        out = self._same_as_the_ui(
            {"milestone_folder_id": str(self.folder.folder_id)},
            {"folder_id": self.folder.folder_id},
        )
        self.assertEqual(out["folder_id"], self.folder.folder_id)
        self.assertEqual(out["changed_fields"], ["folder"])
        out = self._same_as_the_ui(
            {"milestone_folder_id": ""}, {"folder_id": None}, folder=self.folder
        )
        self.assertIsNone(out["folder_id"])
        self.assertEqual(out["changed_fields"], ["folder(cleared)"])

    def test_moving_between_folders(self):
        self._same_as_the_ui(
            {"milestone_folder_id": self.folder2.folder_id},
            {"folder_id": self.folder2.folder_id},
            folder=self.folder,
        )

    def test_sprint_link_carries_the_milestones_tasks(self):
        """The sprint reaches the tasks in the milestone and their history."""
        out = self._same_as_the_ui(
            {"sprint_id": str(self.sprint2.sprint_id)}, {"sprint_id": self.sprint2.sprint_id}
        )
        self.assertEqual(out["sprint_id"], self.sprint2.sprint_id)
        self.assertEqual(out["changed_fields"], ["sprint"])

    def test_sprint_unlink_carries_the_milestones_tasks(self):
        by_tool, by_ui = self._twins(sprint=self.sprint)
        self._tool(action="update", milestone_id=by_tool.milestone_id, sprint_id="")
        self._patch(by_ui, sprint_id=None)

        self.assertEqual(_state(by_tool), _state(by_ui))
        kids = _state(by_tool)["kids"]
        self.assertEqual([k[1] for k in kids], [None])
        # and the move is in the kid's history, marked as carried along
        kid = TaskMaster.objects.get(milestone=by_tool, title="kid")
        self.assertTrue(
            TaskActivity.objects.filter(
                task=kid, action_type="sprint_changed", metadata__cascade="milestone_sprint"
            ).exists()
        )

    def test_the_same_sprint_is_a_no_op(self):
        m = self._milestone("In sprint", sprint=self.sprint)
        out = self._tool(
            action="update", milestone_id=m.milestone_id, sprint_id=self.sprint.sprint_id
        )
        self.assertEqual(out["changed_fields"], [])

    def test_assignees_replace_the_set(self):
        """Both ways the UI sets them: the join rows match the PATCH's, and the
        per-person endpoint leaves the same join rows as the tool."""
        by_tool, by_ui = self._twins()
        out = self._tool(
            action="update", milestone_id=by_tool.milestone_id, assignee_ids=[str(self.user2.id)]
        )
        self._patch(by_ui, assignee_ids=[str(self.user2.id)])

        self.assertEqual(_state(by_tool), _state(by_ui))
        self.assertEqual(out["assignee_ids"], [str(self.user2.id)])
        self.assertEqual(out["changed_fields"], ["assignees"])

    def test_assignees_match_the_per_person_endpoint(self):
        by_tool, by_ui = self._twins()
        self._tool(
            action="update", milestone_id=by_tool.milestone_id, assignee_ids=[str(self.user2.id)]
        )
        res = self.client.post(
            f"{MILESTONE_URL}{by_ui.milestone_id}/assignees/",
            {"user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(_state(by_tool)["assignees"], _state(by_ui)["assignees"])

    def test_assignees_are_a_full_replacement_and_can_be_cleared(self):
        m = self._milestone("People")
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        out = self._tool(
            action="update", milestone_id=m.milestone_id, assignee_ids=[str(self.user2.id)]
        )
        self.assertEqual(out["assignee_ids"], [str(self.user2.id)])
        self.assertEqual(
            list(MilestoneAssignees.objects.filter(milestone=m).values_list("user_id", flat=True)),
            [self.user2.id],
        )

        out = self._tool(action="update", milestone_id=m.milestone_id, assignee_ids=[])
        self.assertEqual(out["assignee_ids"], [])
        self.assertEqual(out["changed_fields"], ["assignees(cleared)"])
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())

    def test_an_empty_string_clears_the_assignees_too(self):
        m = self._milestone("People")
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        self._tool(action="update", milestone_id=m.milestone_id, assignee_ids="")
        self.assertFalse(MilestoneAssignees.objects.filter(milestone=m).exists())

    def test_the_same_assignees_are_a_no_op(self):
        m = self._milestone("People")
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)
        before = _state(m)
        out = self._tool(
            action="update", milestone_id=m.milestone_id, assignee_ids=[str(self.user2.id)]
        )
        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(_state(m), before)

    def test_everything_at_once_matches_the_ui(self):
        tag = {"tagName": "Feature", "tagColor": "#00FF00", "tagTextColor": "#000000"}
        blocks = task_body_blocks("### Goal\nAll of it")
        self._same_as_the_ui(
            {
                "title": "All",
                "description_markdown": "### Goal\nAll of it",
                "status": "Blocked",
                "priority": "Low",
                "effort_level": "Minimal",
                "start_date": "2026-11-02",
                "due_date": "2026-11-30",
                "tags": ["Feature"],
                "assignee_ids": [str(self.user2.id)],
                "milestone_folder_id": self.folder.folder_id,
                "sprint_id": self.sprint2.sprint_id,
            },
            {
                "title": "All",
                "description": blocks,
                "status": "Blocked",
                "priority": "Low",
                "effort_level": "Minimal",
                "start_date": "2026-11-02",
                "due_date": "2026-11-30",
                "tags": [tag],
                "assignee_ids": [str(self.user2.id)],
                "folder_id": self.folder.folder_id,
                "sprint_id": self.sprint2.sprint_id,
            },
        )

    def test_a_numeric_string_is_accepted_for_the_id(self):
        m = self._milestone("By string")
        out = self._tool(action="update", milestone_id=str(m.milestone_id), title="Via string")
        self.assertEqual(out["title"], "Via string")

    def test_only_the_changed_fields_are_written_and_named(self):
        m = self._milestone("Mixed", priority="High", status="Open")
        out = self._tool(
            action="update", milestone_id=m.milestone_id, priority="High", status="Closed"
        )
        self.assertEqual(out["changed_fields"], ["status"])
        self.assertIn("status", out["__summary__"])

    def test_a_no_op_writes_nothing_and_records_nothing(self):
        m = self._milestone("Still", priority="High")
        before = _state(m)
        stamp = MilestoneMaster.objects.get(pk=m.pk).ts_updated_at
        with mock.patch(f"{TOOL}.invalidate_project_tasks_cache") as dropped:
            out = self._tool(
                action="update", milestone_id=m.milestone_id, title="Still", priority="High"
            )
        self.assertEqual(out["changed_fields"], [])
        self.assertIn("No changes", out["__summary__"])
        self.assertEqual(_state(m), before)
        self.assertEqual(MilestoneMaster.objects.get(pk=m.pk).ts_updated_at, stamp)
        dropped.assert_not_called()

    def test_a_change_drops_the_project_task_cache(self):
        m = self._milestone("Cache")
        with mock.patch(f"{TOOL}.invalidate_project_tasks_cache") as dropped:
            self._tool(action="update", milestone_id=m.milestone_id, priority="High")
        dropped.assert_called_once_with(self.team.team_id, self.project.project_id)

    def test_a_legacy_milestone_without_a_backing_task_gets_one(self):
        legacy = self._milestone("Legacy", backed=False)
        self.assertIsNone(legacy.task_id)

        out = self._tool(action="update", milestone_id=legacy.milestone_id, priority="High")

        legacy.refresh_from_db()
        self.assertIsNotNone(legacy.task_id)
        self.assertEqual(legacy.task.priority, "High")
        self.assertEqual(out["task_id"], legacy.task_id)

    def test_a_later_milestone_patch_does_not_revert_the_tool(self):
        """The backing row is a mirror the next PATCH rewrites from the milestone,
        so the tool has to have written the milestone, not just the mirror."""
        m = self._milestone("Mirror")
        self._tool(action="update", milestone_id=m.milestone_id, status="Closed", priority="High")
        self._patch(m, title="Retitled")

        state = _state(m)
        self.assertEqual(state["milestone"]["status"], "Closed")
        self.assertEqual(state["backing"]["status"], "Closed")
        self.assertEqual(state["backing"]["priority"], "High")

    def test_the_change_is_in_the_backing_tasks_history(self):
        m = self._milestone("History")
        self._tool(
            action="update", milestone_id=m.milestone_id, status="WIP", due_date="2026-12-01"
        )
        kinds = set(
            TaskActivity.objects.filter(task_id=m.task_id).values_list("action_type", flat=True)
        )
        self.assertTrue({"status_changed", "due_date_changed"} <= kinds, kinds)

    def test_a_milestone_cannot_be_deleted_by_status(self):
        m = self._milestone("Keep me")
        unchanged = self._unchanged(m)
        with self.assertRaises(ToolError) as caught:
            self._tool(
                action="update", milestone_id=m.milestone_id, status="Deleted", title="Renamed"
            )
        self.assertIn("delete_milestone", str(caught.exception))
        unchanged()


class UpdateRefusalTests(MilestoneToolBase):
    def setUp(self):
        super().setUp()
        self.m = self._milestone("Target", priority="Normal", sprint=self.sprint)
        self._task("kid", milestone=self.m, parent_task_id=self.m.task_id, sprint=self.sprint)

    def _refused(self, message, **args):
        unchanged = self._unchanged(self.m)
        with self.assertRaises(ToolError) as caught:
            self._tool(action="update", milestone_id=self.m.milestone_id, **args)
        self.assertIn(message, str(caught.exception))
        unchanged()

    def test_a_bad_value_changes_nothing_even_beside_good_ones(self):
        good = {"title": "Would rename", "sprint_id": str(self.sprint2.sprint_id)}
        self._refused("`priority` must be one of", priority="Urgent", **good)
        self._refused("`effort_level` must be one of", effort_level="Huge", **good)
        self._refused("`status` must be one of", status="Done", **good)
        self._refused("delete_milestone", status="Deleted", **good)
        self._refused("ISO 8601 date", due_date="soon", **good)
        self._refused("ISO 8601 date", start_date="2026-02-30", **good)
        self._refused("unknown tag", tags=["Nope"], **good)
        self._refused("not found in this project", milestone_folder_id="987654", **good)
        self._refused("not found in this project", sprint_id="987654", title="Would rename")
        self._refused("must be a folder id", milestone_folder_id="abc", **good)
        self._refused("must be a sprint id", sprint_id="abc", title="Would rename")

    def test_a_blank_enum_is_not_a_way_to_clear_it_on_update(self):
        self._refused("`priority` must be one of", priority="", title="Would rename")

    def test_a_date_typo_does_not_silently_clear_the_date(self):
        """The UI's PATCH turns an unparseable date into NULL; the tool refuses."""
        m = self._milestone("Dated", due_date=date(2026, 12, 1))
        with self.assertRaises(ToolError):
            self._tool(action="update", milestone_id=m.milestone_id, due_date="2026/12/01")
        m.refresh_from_db()
        self.assertEqual(m.due_date, date(2026, 12, 1))

    def test_an_empty_title_is_refused_though_the_ui_would_accept_it(self):
        self._refused("`title` must be non-empty", title="  ", priority="High")
        self._refused("too long", title="x" * 256, priority="High")

    def test_assignees_are_validated(self):
        stranger = User.objects.create_user(username="stranger", email="s@x.com", password="pw")
        self._refused(
            "not an active member of this team",
            assignee_ids=[str(stranger.id)],
            title="Would rename",
        )
        self._refused("is not a user id", assignee_ids=["1"], title="Would rename")
        self._refused("must be a list", assignee_ids=5, title="Would rename")

    def test_a_deleted_user_cannot_be_assigned(self):
        gone = User.objects.create_user(username="gone", email="g@x.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=gone)
        User.objects.filter(pk=gone.pk).update(is_deleted=True)
        self._refused("has been deleted", assignee_ids=[str(gone.id)], title="Would rename")

    def test_a_folder_or_sprint_of_another_project_is_refused(self):
        elsewhere = self._project_i_am_not_in("Other", "OTH")
        ProjectMembers.objects.create(team=self.team, project=elsewhere, attendee=self.user)
        foreign = MilestoneFolder.objects.create(team=self.team, project=elsewhere, name="Theirs")
        self._refused(
            "not found in this project",
            milestone_folder_id=str(foreign.folder_id),
            title="Would rename",
        )

    def test_a_milestone_cannot_be_moved_to_another_project(self):
        other = self._project_i_am_not_in("Other", "OTH")
        ProjectMembers.objects.create(team=self.team, project=other, attendee=self.user)
        self._refused("can't be moved to another project", project_id=other.project_id)

    def test_a_deleted_milestone_cannot_be_edited(self):
        MilestoneMaster.objects.filter(pk=self.m.pk).update(is_deleted=True)
        with self.assertRaises(ToolError) as caught:
            self._tool(action="update", milestone_id=self.m.milestone_id, title="Zombie")
        self.assertIn("has been deleted", str(caught.exception))
        self.m.refresh_from_db()
        self.assertEqual(self.m.title, "Target")

    def test_a_missing_milestone_id(self):
        for bad in ("abc", None, ""):
            with self.assertRaises(ToolError):
                self._tool(action="update", milestone_id=bad, title="x")
        with self.assertRaises(ToolError) as caught:
            self._tool(action="update", milestone_id=987654, title="x")
        self.assertIn("not found", str(caught.exception))

    def test_a_failure_while_writing_rolls_back_everything(self):
        """The assignees are the last write; if they fail, the title and the
        sprint push before it are not left behind."""
        unchanged = self._unchanged(self.m)
        with mock.patch(
            "origin.services.milestone_manage.sync_milestone_assignees",
            side_effect=RuntimeError("db went away"),
        ):
            with self.assertRaises(ToolError) as caught:
                self._tool(
                    action="update",
                    milestone_id=self.m.milestone_id,
                    title="Would rename",
                    sprint_id=str(self.sprint2.sprint_id),
                    assignee_ids=[str(self.user2.id)],
                )
        self.assertNotIn("db went away", str(caught.exception))
        unchanged()


# --------------------------------------------------------------------------
# which arguments belong to which action
# --------------------------------------------------------------------------


class ActionArgumentTests(MilestoneToolBase):
    def test_an_unknown_action_is_refused(self):
        for action in ("frobnicate", "", None, "delete"):
            with self.assertRaises(ToolError) as caught:
                self._tool(action=action)
            self.assertIn("`action` must be one of", str(caught.exception))

    def test_an_argument_that_does_not_apply_is_refused_not_ignored(self):
        """A field that would be dropped silently reads as success."""
        m = self._milestone("Folded", folder=self.folder)
        unchanged = self._unchanged(m)
        cases = [
            ("delete_folder", {"milestone_folder_id": self.folder.folder_id, "title": "Oops"}),
            (
                "rename_folder",
                {
                    "milestone_folder_id": self.folder.folder_id,
                    "folder_name": "n",
                    "priority": "High",
                },
            ),
            (
                "create_folder",
                {
                    "project_id": self.project.project_id,
                    "folder_name": "n",
                    "milestone_id": m.milestone_id,
                },
            ),
            ("update", {"milestone_id": m.milestone_id, "folder_name": "n"}),
            (
                "create",
                {
                    "project_id": self.project.project_id,
                    "title": "t",
                    "milestone_id": m.milestone_id,
                },
            ),
        ]
        for action, args in cases:
            with self.subTest(action=action):
                with self.assertRaises(ToolError) as caught:
                    self._tool(action=action, **args)
                self.assertIn("does not apply", str(caught.exception))
        unchanged()

    def test_the_required_arguments_are_named(self):
        cases = [
            ("create", {"project_id": self.project.project_id}, "`title`"),
            ("create", {"title": "t"}, "`project_id`"),
            ("update", {"title": "t"}, "`milestone_id`"),
            ("create_folder", {"project_id": self.project.project_id}, "`folder_name`"),
            ("rename_folder", {"folder_name": "n"}, "`milestone_folder_id`"),
            ("rename_folder", {"milestone_folder_id": self.folder.folder_id}, "`folder_name`"),
            ("delete_folder", {}, "`milestone_folder_id`"),
        ]
        for action, args, missing in cases:
            with self.subTest(action=action, missing=missing):
                with self.assertRaises(ToolError) as caught:
                    self._tool(action=action, **args)
                self.assertIn(missing, str(caught.exception))
                self.assertIn("Nothing was changed", str(caught.exception))

    def test_none_is_the_same_as_leaving_an_argument_out(self):
        """Models send explicit nulls for optional fields."""
        out = self._tool(
            action="create",
            project_id=self.project.project_id,
            title="Nulls",
            description_markdown=None,
            priority=None,
            tags=None,
            milestone_id=None,
            folder_name=None,
        )
        self.assertEqual(out["title"], "Nulls")


# --------------------------------------------------------------------------
# folders
# --------------------------------------------------------------------------


class FolderTests(MilestoneToolBase):
    def setUp(self):
        super().setUp()
        # A second project holding the same folders, so the UI and the tool can
        # each act on their own copy.
        self.twin_project = self._project(self.team, "Twin", "TWN")
        for f in MilestoneFolder.objects.filter(project=self.project).order_by("folder_id"):
            MilestoneFolder.objects.create(
                team=self.team, project=self.twin_project, name=f.name, sort_order=f.sort_order
            )

    def _folder_rows(self, project):
        return list(
            MilestoneFolder.objects.filter(project=project)
            .order_by("sort_order", "folder_id")
            .values_list("name", "sort_order", "team_id")
        )

    def test_create_folder_matches_the_uis_post(self):
        out = self._tool(
            action="create_folder", project_id=self.twin_project.project_id, folder_name="Q4"
        )
        res = self.client.post(
            FOLDER_URL, {"project_id": self.project.project_id, "name": "Q4"}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)

        self.assertEqual(self._folder_rows(self.twin_project), self._folder_rows(self.project))
        self.assertEqual(out["name"], "Q4")
        self.assertEqual(out["sort_order"], res.data["folder"]["sortOrder"])
        self.assertEqual(out["project_id"], self.twin_project.project_id)

    def test_the_first_folder_of_a_project_starts_the_order_at_zero(self):
        fresh = self._project(self.team, "Fresh", "FRS")
        out = self._tool(action="create_folder", project_id=fresh.project_id, folder_name="One")
        self.assertEqual(out["sort_order"], 0)

    def test_the_name_is_trimmed(self):
        out = self._tool(
            action="create_folder", project_id=self.project.project_id, folder_name="  Q4  "
        )
        self.assertEqual(out["name"], "Q4")

    def test_a_bad_name_creates_nothing(self):
        unchanged = self._unchanged()
        for name in ("   ", "x" * 101, 5):
            with self.subTest(name=name):
                with self.assertRaises(ToolError) as caught:
                    self._tool(
                        action="create_folder",
                        project_id=self.project.project_id,
                        folder_name=name,
                    )
                self.assertIn("at most 100 characters", str(caught.exception))
        unchanged()

    def test_a_duplicate_name_is_refused_but_another_projects_name_is_fine(self):
        unchanged = self._unchanged()
        with self.assertRaises(ToolError) as caught:
            self._tool(
                action="create_folder", project_id=self.project.project_id, folder_name="Launch"
            )
        self.assertIn("already has a folder", str(caught.exception))
        unchanged()
        fresh = self._project(self.team, "Fresh", "FRS")
        self._tool(action="create_folder", project_id=fresh.project_id, folder_name="Launch")

    def test_rename_matches_the_uis_patch(self):
        by_tool = MilestoneFolder.objects.get(project=self.twin_project, name="Launch")
        by_ui = MilestoneFolder.objects.get(project=self.project, name="Launch")

        out = self._tool(
            action="rename_folder", milestone_folder_id=by_tool.folder_id, folder_name="Q4"
        )
        res = self.client.patch(f"{FOLDER_URL}{by_ui.folder_id}/", {"name": "Q4"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(self._folder_rows(self.twin_project), self._folder_rows(self.project))
        self.assertEqual(out["changed_fields"], ["name"])

    def test_renaming_to_the_same_name_is_a_no_op(self):
        out = self._tool(
            action="rename_folder", milestone_folder_id=self.folder.folder_id, folder_name="Launch"
        )
        self.assertEqual(out["changed_fields"], [])

    def test_renaming_onto_another_folder_is_refused(self):
        unchanged = self._unchanged()
        with self.assertRaises(ToolError) as caught:
            self._tool(
                action="rename_folder",
                milestone_folder_id=self.folder.folder_id,
                folder_name="Growth",
            )
        self.assertIn("already has a folder", str(caught.exception))
        unchanged()
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Launch")

    def test_a_bad_new_name_is_refused(self):
        with self.assertRaises(ToolError):
            self._tool(
                action="rename_folder", milestone_folder_id=self.folder.folder_id, folder_name=" "
            )
        self.folder.refresh_from_db()
        self.assertEqual(self.folder.name, "Launch")

    def test_delete_matches_the_uis_delete_and_deletes_no_milestone(self):
        tool_folder = MilestoneFolder.objects.get(project=self.twin_project, name="Launch")
        ui_folder = MilestoneFolder.objects.get(project=self.project, name="Launch")
        in_tool = self._milestone("In tool", project=self.twin_project, folder=tool_folder)
        in_ui = self._milestone("In ui", folder=ui_folder)
        kid = self._task("kid", milestone=in_ui, parent_task_id=in_ui.task_id)

        out = self._tool(action="delete_folder", milestone_folder_id=tool_folder.folder_id)
        res = self.client.delete(f"{FOLDER_URL}{ui_folder.folder_id}/")
        self.assertEqual(res.status_code, 204)

        self.assertEqual(self._folder_rows(self.twin_project), self._folder_rows(self.project))
        for m in (in_tool, in_ui):
            m.refresh_from_db()
            self.assertIsNone(m.folder_id)
            self.assertFalse(m.is_deleted)
            self.assertFalse(m.task.is_deleted)
        kid.refresh_from_db()
        self.assertFalse(kid.is_deleted)
        self.assertEqual(kid.milestone_id, in_ui.milestone_id)
        self.assertEqual(out["milestones_unfiled"], 1)
        self.assertTrue(out["deleted"])
        self.assertIn("unfiled", out["__summary__"])

    def test_deleting_a_folder_counts_only_live_milestones(self):
        live = self._milestone("Live", folder=self.folder)
        gone = self._milestone("Gone", folder=self.folder)
        MilestoneMaster.objects.filter(pk=gone.pk).update(is_deleted=True)
        out = self._tool(action="delete_folder", milestone_folder_id=self.folder.folder_id)
        self.assertEqual(out["milestones_unfiled"], 1)
        live.refresh_from_db()
        self.assertIsNone(live.folder_id)

    def test_a_missing_folder_is_not_found(self):
        for action, extra in (
            ("rename_folder", {"folder_name": "n"}),
            ("delete_folder", {}),
        ):
            with self.assertRaises(ToolError) as caught:
                self._tool(action=action, milestone_folder_id=987654, **extra)
            self.assertIn("not found", str(caught.exception))
        with self.assertRaises(ToolError):
            self._tool(action="delete_folder", milestone_folder_id="abc")


# --------------------------------------------------------------------------
# who may do it
# --------------------------------------------------------------------------


class AccessTests(MilestoneToolBase):
    """The UI lets any project member do all of it and answers a stranger with a
    404. The tool also holds the run's team, and words every refusal like a miss
    so a caller learns nothing about an object it can't see."""

    def setUp(self):
        super().setUp()
        self.m = self._milestone("Target")
        self.hidden_project = self._project_i_am_not_in()
        self.hidden = MilestoneMaster.objects.create(
            team=self.team, project=self.hidden_project, title="Hidden", reporter=self.user2
        )
        self.hidden_folder = MilestoneFolder.objects.create(
            team=self.team, project=self.hidden_project, name="Secret"
        )
        self.other_team, self.other_project = self._other_team_project()
        self.foreign = MilestoneMaster.objects.create(
            team=self.other_team, project=self.other_project, title="Foreign", reporter=self.user2
        )
        self.foreign_folder = MilestoneFolder.objects.create(
            team=self.other_team, project=self.other_project, name="Foreign folder"
        )

    def _refusal_text(self, **call):
        with self.assertRaises(ToolError) as caught:
            self._tool(**call)
        return caught.exception

    def test_a_milestone_in_a_project_the_caller_is_not_in_looks_like_a_miss(self):
        refused = self._refusal_text(
            action="update", milestone_id=self.hidden.milestone_id, title="Mine now"
        )
        missing = self._refusal_text(action="update", milestone_id=987654, title="Mine now")
        self.assertIsInstance(refused, ToolForbidden)
        self.assertEqual(
            str(refused).replace(str(self.hidden.milestone_id), "N"),
            str(missing).replace("987654", "N"),
        )
        self.hidden.refresh_from_db()
        self.assertEqual(self.hidden.title, "Hidden")

    def test_another_teams_milestone_looks_like_a_miss(self):
        refused = self._refusal_text(
            action="update", milestone_id=self.foreign.milestone_id, title="Mine now"
        )
        self.assertIsInstance(refused, ToolForbidden)
        self.assertIn("not found", str(refused))
        self.foreign.refresh_from_db()
        self.assertEqual(self.foreign.title, "Foreign")

    def test_a_project_the_caller_is_not_in_cannot_be_created_in_or_given_folders(self):
        unchanged = self._unchanged()
        for project in (self.hidden_project, self.other_project):
            for call in (
                {"action": "create", "title": "Sneak"},
                {"action": "create_folder", "folder_name": "Sneak"},
            ):
                with self.subTest(project=project.project_name, call=call["action"]):
                    refused = self._refusal_text(project_id=project.project_id, **call)
                    self.assertIsInstance(refused, ToolForbidden)
                    self.assertIn("not found", str(refused))
        unchanged()

    def test_a_folder_in_a_project_the_caller_is_not_in_cannot_be_touched(self):
        unchanged = self._unchanged()
        for folder in (self.hidden_folder, self.foreign_folder):
            for call in (
                {"action": "rename_folder", "folder_name": "Mine"},
                {"action": "delete_folder"},
            ):
                with self.subTest(folder=folder.name, call=call["action"]):
                    refused = self._refusal_text(milestone_folder_id=folder.folder_id, **call)
                    self.assertIsInstance(refused, ToolForbidden)
                    self.assertIn("not found", str(refused))
        unchanged()

    def test_a_milestone_cannot_be_filed_in_a_folder_the_caller_cannot_see(self):
        unchanged = self._unchanged(self.m)
        for folder in (self.hidden_folder, self.foreign_folder):
            with self.assertRaises(ToolError):
                self._tool(
                    action="update",
                    milestone_id=self.m.milestone_id,
                    milestone_folder_id=str(folder.folder_id),
                )
        unchanged()

    def test_an_assignee_of_a_milestone_who_is_not_in_the_project_cannot_edit_it(self):
        MilestoneAssignees.objects.create(team=self.team, milestone=self.hidden, user=self.user)
        refused = self._refusal_text(
            action="update", milestone_id=self.hidden.milestone_id, title="Mine now"
        )
        self.assertIsInstance(refused, ToolForbidden)

    def test_a_member_who_is_not_the_owner_may_do_everything(self):
        member = User.objects.create_user(username="mem", email="mem@x.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=member)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(member.id))
        out = MANAGE_MILESTONE.run(
            {"action": "update", "milestone_id": self.m.milestone_id, "title": "By member"}, ctx
        )
        self.assertEqual(out["title"], "By member")
        folder = MANAGE_MILESTONE.run(
            {"action": "create_folder", "project_id": self.project.project_id, "folder_name": "M"},
            ctx,
        )
        self.assertEqual(folder["name"], "M")

    def test_the_run_must_be_in_the_milestones_team(self):
        """A caller who is a member of a project in another team, but whose run
        is for this team, can't use that project's milestones."""
        ProjectMembers.objects.create(
            team=self.other_team, project=self.other_project, attendee=self.user
        )
        refused = self._refusal_text(
            action="update", milestone_id=self.foreign.milestone_id, title="Mine now"
        )
        self.assertIsInstance(refused, ToolForbidden)

    def test_a_project_of_another_team_is_refused_even_for_one_of_its_members(self):
        """Creating in it, or touching its folders, needs the run to be in its team
        too: membership alone is not enough."""
        ProjectMembers.objects.create(
            team=self.other_team, project=self.other_project, attendee=self.user
        )
        unchanged = self._unchanged()
        calls = (
            {"action": "create", "project_id": self.other_project.project_id, "title": "Sneak"},
            {
                "action": "create_folder",
                "project_id": self.other_project.project_id,
                "folder_name": "Sneak",
            },
            {
                "action": "rename_folder",
                "milestone_folder_id": self.foreign_folder.folder_id,
                "folder_name": "Sneak",
            },
            {"action": "delete_folder", "milestone_folder_id": self.foreign_folder.folder_id},
        )
        for call in calls:
            with self.subTest(call=call["action"]):
                refused = self._refusal_text(**call)
                self.assertIsInstance(refused, ToolForbidden)
        unchanged()

    def test_a_deleted_project_is_not_found(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self._tool(action="create", project_id=self.project.project_id, title="Zombie")
        with self.assertRaises(ToolError):
            self._tool(action="update", milestone_id=self.m.milestone_id, title="Zombie")


# --------------------------------------------------------------------------
# list_milestones: the ids the write takes
# --------------------------------------------------------------------------


class ListMilestonesSourcesTests(MilestoneToolBase):
    def test_folders_and_each_milestones_folder_sprint_and_assignees_are_listed(self):
        m = self._milestone("Filed", folder=self.folder, sprint=self.sprint)
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)

        out = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)

        self.assertEqual(
            out["folders"],
            [
                {
                    "folder_id": self.folder.folder_id,
                    "project_id": self.project.project_id,
                    "name": "Launch",
                },
                {
                    "folder_id": self.folder2.folder_id,
                    "project_id": self.project.project_id,
                    "name": "Growth",
                },
            ],
        )
        row = next(r for r in out["milestones"] if r["milestone_id"] == m.milestone_id)
        self.assertEqual(row["folder_id"], self.folder.folder_id)
        self.assertEqual(row["sprint_id"], self.sprint.sprint_id)
        self.assertEqual(row["assignee_ids"], [str(self.user2.id)])
        self.assertEqual(row["assignee_count"], 1)

    def test_folders_of_projects_the_caller_is_not_in_are_not_listed(self):
        hidden_project = self._project_i_am_not_in()
        MilestoneFolder.objects.create(team=self.team, project=hidden_project, name="Secret")
        out = LIST_MILESTONES.run({}, self.ctx)
        self.assertNotIn("Secret", [f["name"] for f in out["folders"]])

    def test_a_listed_folder_id_can_be_filed_into_and_a_milestone_id_edited(self):
        """The read produces what the write consumes."""
        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        folder_id = listed["folders"][0]["folder_id"]
        milestone_id = self._milestone("Round trip").milestone_id
        listed = LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        self.assertIn(milestone_id, [r["milestone_id"] for r in listed["milestones"]])

        self._tool(action="update", milestone_id=milestone_id, milestone_folder_id=folder_id)

        row = next(
            r
            for r in LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)[
                "milestones"
            ]
            if r["milestone_id"] == milestone_id
        )
        self.assertEqual(row["folder_id"], folder_id)

    def test_assignees_cost_no_extra_queries(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        milestones = [self._milestone(f"M{i}") for i in range(3)]
        with CaptureQueriesContext(connection) as before:
            LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        for m in milestones:
            MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
            MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user2)
        with CaptureQueriesContext(connection) as after:
            LIST_MILESTONES.run({"project_id": self.project.project_id}, self.ctx)
        self.assertEqual(len(after), len(before))


# --------------------------------------------------------------------------
# registration
# --------------------------------------------------------------------------


class RegistrationTests(MilestoneToolBase):
    def test_both_tools_are_registered_as_single_writes(self):
        from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS

        for name in ("manage_milestone", "delete_milestone"):
            self.assertIn(name, REGISTRY)
            self.assertTrue(REGISTRY[name].requires_approval)
            self.assertIn(name, SINGLE_WRITE_TOOLS)

    def test_the_milestone_folder_argument_does_not_share_the_note_folders_name(self):
        """`folder_id` labels as a note folder on the approval card."""
        props = MANAGE_MILESTONE.parameters_schema["properties"]
        self.assertNotIn("folder_id", props)
        self.assertIn("milestone_folder_id", props)

    def test_every_action_is_documented_in_the_schema(self):
        from origin.search_engine.agent.tools.manage_milestone import ACTIONS

        schema = MANAGE_MILESTONE.parameters_schema["properties"]["action"]
        self.assertEqual(schema["enum"], list(ACTIONS))
        self.assertEqual(MANAGE_MILESTONE.parameters_schema["required"], ["action"])
