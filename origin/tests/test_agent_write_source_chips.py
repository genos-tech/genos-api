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

from origin.search_engine.agent.controller import (
    _ui_sources_from_tool_result,
    reconstruct_sources_for_run,
)


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
