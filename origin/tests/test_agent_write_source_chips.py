"""Write tools → source chips, and the `operated` mark.

Every object an approved write tool creates/updates must reach the
frontend as a source chip carrying `operated: true` — that flag is what
lets `citedChipSources` (frontend) keep the chip even when the model's
prose forgets to cite the entity. Read tools must NOT carry the mark:
an operated chip is pinned in the UI unconditionally, so a false mark
would resurrect the retrieved-but-unused chip noise the cited-only rule
exists to drop.

Pure-dict tests — `_ui_sources_from_tool_result` maps a tool's result
payload, and the mark comes from the REGISTRY's `requires_approval`
flag, so no DB is involved.
"""

from django.test import SimpleTestCase

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import (
    NO_CHIP_WRITE_TOOLS,
    _hydrate_source_names,
    _hydrate_task_display_ids,
    _ui_sources_from_tool_result,
    reconstruct_sources_for_run,
)
from origin.search_engine.agent.tools import REGISTRY

from .test_base import BaseAPITestCase


class _StubStep:
    def __init__(self, tool_name, result_json):
        self.tool_name = tool_name
        self.result_json = result_json


class _StubSteps:
    def __init__(self, steps):
        self._steps = steps

    def all(self):
        return self._steps


class _StubRun:
    def __init__(self, steps):
        self.steps = _StubSteps(steps)


class TaskWriteChipTests(SimpleTestCase):
    def test_create_task_yields_an_operated_task_chip(self):
        chips = _ui_sources_from_tool_result(
            "create_task",
            {"task_id": 42, "project_id": 7, "title": "Ship it", "status": "Open"},
        )
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["entity_type"], "task")
        self.assertEqual(chips[0]["entity_id"], "task:42")
        self.assertEqual(chips[0]["project_id"], "7")
        self.assertTrue(chips[0]["operated"])

    def test_update_task_yields_an_operated_task_chip(self):
        chips = _ui_sources_from_tool_result(
            "update_task",
            {"task_id": 42, "changed_fields": ["status"], "status": "WIP", "title": "Ship it"},
        )
        self.assertEqual(chips[0]["entity_id"], "task:42")
        self.assertTrue(chips[0]["operated"])

    def test_assign_task_and_add_comment_chip_the_task(self):
        for call_name, result in (
            ("assign_task", {"task_id": 9, "assignee_id": "u-1", "assignee_username": "bob"}),
            ("add_comment", {"task_id": 9, "comment_id": 3}),
        ):
            chips = _ui_sources_from_tool_result(call_name, result)
            self.assertEqual(chips[0]["entity_id"], "task:9", call_name)
            self.assertTrue(chips[0]["operated"], call_name)

    def test_missing_task_id_yields_no_chip(self):
        self.assertEqual(_ui_sources_from_tool_result("create_task", {"title": "x"}), [])


class TodoWriteChipTests(SimpleTestCase):
    def test_create_todo_item_yields_an_operated_todo_chip(self):
        chips = _ui_sources_from_tool_result(
            "create_todo_item",
            {"item_id": 88, "group_id": 5, "local_date": "2026-07-28", "title": "buy cake"},
        )
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["entity_type"], "todo")
        self.assertEqual(chips[0]["entity_id"], "todo:2026-07-28:item:88")
        self.assertEqual(chips[0]["title"], "buy cake")
        self.assertTrue(chips[0]["operated"])

    def test_update_todo_item_yields_an_operated_todo_chip(self):
        chips = _ui_sources_from_tool_result(
            "update_todo_item",
            {"item_id": 88, "local_date": "2026-07-28", "title": "buy cake", "is_completed": True},
        )
        self.assertEqual(chips[0]["entity_id"], "todo:2026-07-28:item:88")
        self.assertTrue(chips[0]["operated"])

    def test_a_deleted_todo_is_not_chipped_there_is_nothing_to_open(self):
        # The delete result carries no `local_date` beside `item_id`, and the
        # tool is not in the todo branch: a chip would be a dead link.
        result = {"item_id": 88, "title": "buy cake", "deleted": True}
        self.assertEqual(_ui_sources_from_tool_result("delete_todo_item", result), [])
        self.assertEqual(
            _ui_sources_from_tool_result(
                "delete_todo_item", {**result, "local_date": "2026-07-28"}
            ),
            [],
        )


class OperatedMarkScopeTests(SimpleTestCase):
    def test_read_tools_are_never_marked_operated(self):
        # fetch_task and fetch_note share source-builder branches with
        # write tools — the mark must come from the REGISTRY flag, not
        # the branch.
        for call_name, result in (
            ("fetch_task", {"task_id": 42, "title": "Ship it", "project_id": 7}),
            ("fetch_note", {"note_id": 5, "note_type": "personal", "title": "plan"}),
            ("list_tasks", {"tasks": [{"task_id": 1, "project_id": 2, "title": "t"}]}),
        ):
            for chip in _ui_sources_from_tool_result(call_name, result):
                self.assertNotIn("operated", chip, call_name)

    def test_note_writes_are_marked_operated(self):
        for call_name in ("create_note", "update_note"):
            chips = _ui_sources_from_tool_result(
                call_name, {"note_id": 5, "note_type": "personal", "title": "plan"}
            )
            self.assertTrue(chips[0]["operated"], call_name)


class ReconstructOperatedTests(SimpleTestCase):
    def test_read_then_write_keeps_the_operated_mark(self):
        """First-wins dedup must not strip the mark when the same task
        was fetched before it was updated — History replays depend on
        it just like the live stream does."""
        run = _StubRun(
            [
                _StubStep("fetch_task", {"task_id": 42, "title": "Ship it", "project_id": 7}),
                _StubStep(
                    "update_task",
                    {"task_id": 42, "changed_fields": ["status"], "status": "WIP"},
                ),
            ]
        )
        sources = reconstruct_sources_for_run(run)
        by_id = {s["entity_id"]: s for s in sources}
        self.assertTrue(by_id["task:42"].get("operated"))


# --------------------------------------------------------------------------- #
# Every approval-gated write tool: a chip, or a reason for none               #
# --------------------------------------------------------------------------- #

# One realistic result per chipped write tool, with the keys the tool's own `return` carries
# (read from each tool module), and the chip it must produce: (entity_type, entity_id).
CHIPPED_WRITE_TOOLS = {
    "create_task": ({"task_id": 42, "project_id": 7, "title": "t"}, "task", "task:42"),
    "update_task": ({"task_id": 42, "project_id": 7, "title": "t"}, "task", "task:42"),
    "assign_task": ({"task_id": 9, "assignee_id": "u-1"}, "task", "task:9"),
    "add_comment": ({"task_id": 9, "comment_id": 3}, "task", "task:9"),
    "update_comment": (
        {"task_id": 9, "display_id": "P-9", "comment_id": 3, "newly_mentioned": []},
        "task",
        "task:9",
    ),
    "delete_comment": (
        {
            "task_id": 9,
            "display_id": "P-9",
            "comment_id": 3,
            "deleted": True,
            "already_deleted": False,
            "comments_left": 2,
        },
        "task",
        "task:9",
    ),
    "restore_task_body": (
        {"task_id": 9, "display_id": "P-9", "restored_from": 12, "replaced_body_kept": True},
        "task",
        "task:9",
    ),
    "add_task_delivery": (
        {
            "task_id": 9,
            "display_id": "P-9",
            "outcome": "linked",
            "state_checked_on_github": True,
            "webhook_registered": True,
            "delivery": {},
        },
        "task",
        "task:9",
    ),
    "remove_task_delivery": (
        {"task_id": 9, "display_id": "P-9", "removed": {}},
        "task",
        "task:9",
    ),
    "create_task_plan": (
        {
            "project_id": 7,
            "project_name": "P",
            "milestone": {"milestone_id": 3, "title": "M", "task_id": 50},
            "tasks": [{"task_id": 51, "title": "a", "display_id": "P-2"}],
        },
        "milestone",
        "milestone:3",
    ),
    "update_tasks_bulk": (
        {"updated": [{"task_id": 5, "title": "t", "project_id": 7, "display_id": "P-5"}]},
        "task",
        "task:5",
    ),
    "update_task_dependencies": (
        {"added": [{"blocker_task_id": 1, "blocked_task_id": 2}], "removed": []},
        "task",
        "task:1",
    ),
    "manage_milestone": (
        {"milestone_id": 3, "title": "M", "project_id": 7, "task_id": 50},
        "milestone",
        "milestone:3",
    ),
    "delete_milestone": ({"project_id": 7, "project_name": "P"}, "project", "project:7"),
    "create_todo_item": (
        {"item_id": 88, "local_date": "2026-07-28", "title": "x"},
        "todo",
        "todo:2026-07-28:item:88",
    ),
    "update_todo_item": (
        {"item_id": 88, "local_date": "2026-07-28", "title": "x"},
        "todo",
        "todo:2026-07-28:item:88",
    ),
    "create_note": (
        {"note_id": 5, "note_type": "personal", "title": "p"},
        "note",
        "note:personal:5",
    ),
    "update_note": (
        {"note_id": 5, "note_type": "personal", "title": "p"},
        "note",
        "note:personal:5",
    ),
    "restore_note_version": (
        {
            "note_type": "personal",
            "note_id": 5,
            "restored_from_version_no": 1,
            "new_version_no": 3,
            "title": "p",
            "changed_fields": ["title", "body"],
            "live_editor": "pushed",
        },
        "note",
        "note:personal:5",
    ),
    "manage_note_sharing": (
        {"note_type": "personal", "note_id": 5, "user_id": "u-2", "action": "share"},
        "note",
        "note:personal:5",
    ),
    "manage_project": (
        {"project_id": 7, "name": "Website", "code": "WEB", "is_private": False},
        "project",
        "project:7",
    ),
    "manage_sprint": (
        {
            "sprint_id": 1,
            "project_id": 7,
            "name": "Sprint 1",
            "start_date": "2026-11-01",
            "end_date": "2026-11-14",
            "status": "active",
        },
        "project",
        "project:7",
    ),
    "manage_project_members": (
        {"project_id": 7, "user_id": "u-2", "already_member": False},
        "project",
        "project:7",
    ),
    "add_project_tag": (
        {"project_id": 7, "tag_name": "ux", "color": "#fff"},
        "project",
        "project:7",
    ),
    "manage_project_labels": (
        {"project_id": 7, "changed_label_ids": [1], "labels": []},
        "project",
        "project:7",
    ),
    "approve_join_request": (
        {
            "inbox_item_id": 1,
            "project_id": 7,
            "project_name": "Website",
            "user_id": "u-2",
            "username": "bob",
            "request_status": "approved",
            "already_member": False,
        },
        "project",
        "project:7",
    ),
    "decline_join_request": (
        {
            "inbox_item_id": 1,
            "project_id": 7,
            "project_name": "Website",
            "user_id": "u-2",
            "username": "bob",
            "request_status": "declined",
        },
        "project",
        "project:7",
    ),
}


def _approval_tools() -> set[str]:
    return {name for name, tool in REGISTRY.items() if tool.requires_approval}


class WriteToolChipCompletenessTests(SimpleTestCase):
    def test_every_approval_tool_has_a_chip_or_a_reason_for_none_and_nothing_else_is_listed(self):
        gated = _approval_tools()
        listed = set(CHIPPED_WRITE_TOOLS) | set(NO_CHIP_WRITE_TOOLS)
        self.assertFalse(
            gated - listed,
            f"approval-gated tools with no chip and no reason for none: {sorted(gated - listed)}. "
            "Give it a branch in `_sources_from_tool_result` and a row in CHIPPED_WRITE_TOOLS, "
            "or a reason in NO_CHIP_WRITE_TOOLS (origin/search_engine/agent/controller.py).",
        )
        self.assertFalse(
            listed - gated, f"listed but not approval-gated tools: {sorted(listed - gated)}"
        )
        self.assertFalse(
            set(CHIPPED_WRITE_TOOLS) & set(NO_CHIP_WRITE_TOOLS),
            "a tool is both chipped and listed as having no chip",
        )

    def test_a_tool_with_no_chip_says_why(self):
        for name, reason in NO_CHIP_WRITE_TOOLS.items():
            with self.subTest(tool=name):
                self.assertTrue(reason.strip())

    def test_every_chipped_write_tool_yields_one_operated_chip_to_the_right_place(self):
        for name, (result, etype, eid) in CHIPPED_WRITE_TOOLS.items():
            with self.subTest(tool=name):
                chips = _ui_sources_from_tool_result(name, result)
                self.assertTrue(chips, f"{name} produced no chip")
                self.assertEqual((chips[0]["entity_type"], chips[0]["entity_id"]), (etype, eid))
                self.assertTrue(all(c.get("operated") for c in chips))

    def test_a_write_tool_without_a_chip_yields_none_even_when_its_result_carries_ids(self):
        # Every id key a branch looks at, so a no-chip tool cannot slip into one by accident.
        result = {
            "task_id": 1,
            "project_id": 7,
            "note_id": 5,
            "note_type": "personal",
            "item_id": 3,
            "local_date": "2026-07-28",
            "folder_id": 2,
            "sprint_id": 1,
            "milestone_id": 4,
        }
        for name in NO_CHIP_WRITE_TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(_ui_sources_from_tool_result(name, result), [])


class WriteChipCapTests(SimpleTestCase):
    """The tools' own limits bound these lists: 20 plan tasks, 30 bulk rows, 30 edges."""

    def test_a_20_task_plan_chips_every_task(self):
        tasks = [{"task_id": 100 + i, "title": f"t{i}", "display_id": f"P-{i}"} for i in range(20)]
        chips = _ui_sources_from_tool_result(
            "create_task_plan",
            {
                "project_id": 7,
                "project_name": "P",
                "milestone": {"milestone_id": 3, "title": "M", "task_id": 99},
                "tasks": tasks,
            },
        )
        ids = [c["entity_id"] for c in chips]
        self.assertEqual(sum(1 for i in ids if i.startswith("task:")), 20)
        self.assertEqual(ids[0], "milestone:3")
        self.assertEqual(ids[-1], "project:7")
        self.assertTrue(all(c["operated"] for c in chips))

    def test_a_30_row_bulk_update_chips_every_row(self):
        rows = [{"task_id": i, "title": f"t{i}", "project_id": 7} for i in range(1, 31)]
        chips = _ui_sources_from_tool_result("update_tasks_bulk", {"updated": rows})
        self.assertEqual([c["entity_id"] for c in chips], [f"task:{i}" for i in range(1, 31)])

    def test_30_dependency_edges_chip_both_ends_of_each(self):
        edges = [{"blocker_task_id": 2 * i + 1, "blocked_task_id": 2 * i + 2} for i in range(30)]
        chips = _ui_sources_from_tool_result(
            "update_task_dependencies", {"added": edges, "removed": []}
        )
        self.assertEqual(len(chips), 60)

    def test_a_task_in_two_edges_is_one_chip(self):
        edges = [
            {"blocker_task_id": 1, "blocked_task_id": 2},
            {"blocker_task_id": 1, "blocked_task_id": 3},
        ]
        chips = _ui_sources_from_tool_result(
            "update_task_dependencies", {"added": edges, "removed": []}
        )
        self.assertEqual([c["entity_id"] for c in chips], ["task:1", "task:2", "task:3"])


class ProjectWriteChipTests(SimpleTestCase):
    def test_manage_project_names_the_project_with_its_own_name(self):
        chips = _ui_sources_from_tool_result(
            "manage_project", {"project_id": 7, "name": "Website", "code": "WEB"}
        )
        self.assertEqual(chips[0]["title"], "Website")

    def test_a_sprints_name_is_not_taken_for_the_projects(self):
        chips = _ui_sources_from_tool_result(
            "manage_sprint", {"sprint_id": 1, "project_id": 7, "name": "Sprint 1"}
        )
        self.assertEqual(chips[0]["entity_id"], "project:7")
        self.assertEqual(chips[0]["title"], "")

    def test_a_join_request_answer_names_the_project_it_carries(self):
        for name in ("approve_join_request", "decline_join_request"):
            chips = _ui_sources_from_tool_result(
                name, {"inbox_item_id": 1, "project_id": 7, "project_name": "Website"}
            )
            self.assertEqual(chips[0]["title"], "Website", name)

    def test_a_label_that_was_only_created_has_no_project_so_no_chip(self):
        # `manage_project_labels` create returns the team's label, not a project.
        result = {"label_id": 4, "label_name": "ux", "color": "#fff"}
        self.assertEqual(_ui_sources_from_tool_result("manage_project_labels", result), [])

    def test_a_deleted_comment_chips_its_task_which_is_still_there(self):
        chips = _ui_sources_from_tool_result(
            "delete_comment", {"task_id": 9, "display_id": "P-9", "comment_id": 3, "deleted": True}
        )
        self.assertEqual(chips[0]["entity_id"], "task:9")
        self.assertEqual(chips[0]["task_display_id"], "P-9")

    def test_the_operated_mark_survives_a_reconstructed_run_for_the_new_tools(self):
        run = _StubRun(
            [
                _StubStep("manage_project_members", {"project_id": 7, "user_id": "u-2"}),
                _StubStep("delete_comment", {"task_id": 9, "display_id": "P-9", "comment_id": 3}),
            ]
        )
        by_id = {s["entity_id"]: s for s in reconstruct_sources_for_run(run)}
        self.assertTrue(by_id["project:7"]["operated"])
        self.assertTrue(by_id["task:9"]["operated"])


class HydrateSourceNamesTests(BaseAPITestCase):
    """Names a write tool's result did not carry are read back from the row, in one pass
    through the same funnel every emit path already uses."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def _chips(self, name, result):
        return _hydrate_task_display_ids(_ui_sources_from_tool_result(name, result))

    def test_a_project_chip_is_named_from_the_row(self):
        (chip,) = self._chips(
            "add_project_tag", {"project_id": self.project.project_id, "tag_name": "ux"}
        )
        self.assertEqual(chip["title"], "Website Redesign")

    def test_a_name_the_result_carried_is_left_alone(self):
        (chip,) = self._chips(
            "approve_join_request",
            {"project_id": self.project.project_id, "project_name": "As told"},
        )
        self.assertEqual(chip["title"], "As told")

    def test_several_unnamed_project_chips_cost_one_query(self):
        other = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other",
            code="OTH",
            owner=self.user,
            project_system_user=self.user,
        )
        chips = _ui_sources_from_tool_result(
            "add_project_tag", {"project_id": self.project.project_id}
        ) + _ui_sources_from_tool_result("add_project_tag", {"project_id": other.project_id})
        with self.assertNumQueries(1):
            _hydrate_source_names(chips)
        self.assertEqual([c["title"] for c in chips], ["Website Redesign", "Other"])

    def test_a_shared_note_chip_gets_its_title(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Research", body=[]
        )
        (chip,) = self._chips(
            "manage_note_sharing",
            {"note_type": "personal", "note_id": note.note_id, "user_id": "u", "action": "share"},
        )
        self.assertEqual(chip["title"], "Research")

    def test_a_restored_task_note_gets_the_refs_its_link_needs(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, task=task, title="Plan", body=[]
        )
        (chip,) = self._chips(
            "restore_note_version",
            {"note_type": "task", "note_id": note.note_id, "title": "Plan"},
        )
        self.assertEqual(chip["project_id"], str(self.project.project_id))
        self.assertEqual(chip["task_id"], str(task.task_id))
        self.assertEqual(chip["title"], "Plan")

    def test_a_restored_chat_note_gets_its_chat_refs(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="DM")
        ChannelMember.objects.create(channel=dm, user=self.user)
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Chat",
            body=[],
            chat_type=1,
            channel=dm,
            is_thread=False,
        )
        (chip,) = self._chips(
            "restore_note_version",
            {"note_type": "chat", "note_id": note.note_id, "title": "Chat"},
        )
        self.assertEqual(chip["chat_id"], str(dm.pk))
        self.assertEqual(chip["chat_type"], "dm")

    def test_a_chip_that_already_has_everything_costs_no_query(self):
        chips = _ui_sources_from_tool_result(
            "update_note",
            {
                "note_id": 5,
                "note_type": "task",
                "title": "Plan",
                "parent_context": {"project_id": 7, "task_id": "9"},
            },
        )
        with self.assertNumQueries(0):
            _hydrate_source_names(chips)

    def test_an_unknown_id_leaves_the_chip_as_it_was_and_does_not_raise(self):
        chips = _ui_sources_from_tool_result(
            "manage_note_sharing", {"note_type": "personal", "note_id": 999999}
        ) + _ui_sources_from_tool_result("add_project_tag", {"project_id": 999999})
        _hydrate_source_names(chips)
        self.assertEqual([c["title"] for c in chips], ["", ""])
