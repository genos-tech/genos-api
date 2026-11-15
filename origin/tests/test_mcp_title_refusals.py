"""A title that does not fit its column is refused in words (MIP-1367 phase 1,
`agent/tools/field_limits.py`).

Before: `create_task` / `create_note` / `update_task` / `update_note` answered "Failed to create ...
(ref ...)" for a 300-character title (the cause, a `DataError`, went to the log), and
`create_todo_item` with a long title or category answered "this is a bug in Genos".

Now each refuses first, on the value after the tool's own strip, naming the field and the limit and
saying nothing was changed. Failure paths only: what fits is untouched (pinned at the limit exactly,
and with whitespace that strips it under the limit), and no tool declaration moved.
"""

from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY, field_limits, search_kb
from origin.search_engine.agent.tools.base import ToolContext, ToolError

from .test_mcp_endpoint import McpBase

CALL_AGAIN = "Keep it to one short line and put the detail in `{body}`, then send the call again."


def too_long(n, field, limit, nothing, body):
    advice = CALL_AGAIN.format(body=body) if body else "Shorten it and send the call again."
    return (
        f"`{field}` is too long: {n} characters were sent and the limit is {limit}. "
        f"{nothing} {advice}"
    )


class Base(McpBase):
    def setUp(self):
        super().setUp()
        self._key()
        self.pid = self.project.project_id

    def text(self, name, args):
        return self._tool_text(self._call(name, args))

    def refused(self, name, args, message):
        """The call is an `isError` result with exactly `message`, and no ERROR log (an unhandled
        failure logs a traceback with a reference)."""
        with self.assertNoLogs(level="ERROR"):
            text, is_error = self.text(name, args)
        self.assertTrue(is_error, text)
        self.assertEqual(text, message)


class TestTasks(Base):
    def test_create_task_over_255_is_refused_and_nothing_is_created(self):
        before = TaskMaster.objects.count()
        self.refused(
            "create_task",
            {"title": "x" * 256, "project_id": self.pid},
            too_long(256, "title", 255, "Nothing was created.", "content_text"),
        )
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_a_title_of_exactly_255_is_created(self):
        text, is_error = self.text("create_task", {"title": "x" * 255, "project_id": self.pid})
        self.assertFalse(is_error, text)
        self.assertTrue(TaskMaster.objects.filter(title="x" * 255).exists())

    def test_the_limit_is_on_the_stripped_title(self):
        # 300 characters, 255 of them text: the tool strips it, so it fits (and worked before).
        title = "  " * 20 + "y" * 255 + "  " * 5
        self.assertGreater(len(title), 255)
        text, is_error = self.text("create_task", {"title": title, "project_id": self.pid})
        self.assertFalse(is_error, text)
        self.assertTrue(TaskMaster.objects.filter(title="y" * 255).exists())

    def test_update_task_over_255_is_refused_and_the_task_is_unchanged(self):
        self.refused(
            "update_task",
            {"task_id": self.task.task_id, "title": "z" * 300, "status": "WIP"},
            too_long(300, "title", 255, "Nothing was changed.", "content_text"),
        )
        self.task.refresh_from_db()
        self.assertEqual((self.task.title, self.task.status), ("Fix the login redirect", "Open"))

    def test_update_task_at_255_works(self):
        text, is_error = self.text(
            "update_task", {"task_id": self.task.task_id, "title": "z" * 255}
        )
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "z" * 255)

    def test_a_blank_title_keeps_its_old_sentence(self):
        text, is_error = self.text("create_task", {"title": "   ", "project_id": self.pid})
        self.assertEqual((text, is_error), ("`title` is required.", True))

    def test_a_normal_title_still_hears_the_project_refusal(self):
        # A stranger's project with a normal title still hears what it heard before.
        text, is_error = self.text("create_task", {"title": "ok", "project_id": 987654})
        self.assertTrue(is_error)
        self.assertEqual(text, "Project 987654 not found.")


class TestTaskPlans(Base):
    def test_a_task_title_over_the_limit_names_the_task_and_creates_nothing(self):
        before = TaskMaster.objects.count()
        text, is_error = self.text(
            "create_task_plan",
            {"project_id": self.pid, "tasks": [{"title": "fine"}, {"title": "t" * 256}]},
        )
        self.assertTrue(is_error)
        self.assertEqual(
            text,
            "Plan validation failed:\n- tasks[1]: "
            + too_long(256, "title", 255, "Nothing was created.", "content_text"),
        )
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_a_milestone_title_over_the_limit_creates_nothing(self):
        before = MilestoneMaster.objects.count()
        text, is_error = self.text(
            "create_task_plan",
            {"project_id": self.pid, "milestone": {"title": "m" * 300}, "tasks": [{"title": "a"}]},
        )
        self.assertTrue(is_error)
        self.assertIn("milestone: `title` is too long: 300 characters were sent", text)
        self.assertEqual(MilestoneMaster.objects.count(), before)

    def test_titles_at_the_limit_still_make_a_plan(self):
        text, is_error = self.text(
            "create_task_plan",
            {
                "project_id": self.pid,
                "milestone": {"title": "m" * 255},
                "tasks": [{"title": "a" * 255}],
            },
        )
        self.assertFalse(is_error, text)
        self.assertTrue(MilestoneMaster.objects.filter(title="m" * 255).exists())

    def test_every_problem_is_still_listed_together(self):
        text, _ = self.text(
            "create_task_plan",
            {"project_id": self.pid, "tasks": [{"title": "t" * 256}, {"title": ""}]},
        )
        self.assertIn("tasks[0]: `title` is too long", text)
        self.assertIn("tasks[1]: `title` is required.", text)


class TestNotes(Base):
    def test_create_note_over_255_is_refused(self):
        before = PersonalNoteMaster.objects.count()
        self.refused(
            "create_note",
            {"note_type": "personal", "title": "n" * 300},
            too_long(300, "title", 255, "Nothing was created.", "content_text"),
        )
        self.assertEqual(PersonalNoteMaster.objects.count(), before)

    def test_create_note_at_255_works(self):
        text, is_error = self.text("create_note", {"note_type": "personal", "title": "n" * 255})
        self.assertFalse(is_error, text)

    def test_update_note_over_255_is_refused_and_the_note_is_unchanged(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Keep me", body=[]
        )
        self.refused(
            "update_note",
            {"note_id": note.note_id, "note_type": "personal", "title": "n" * 300},
            too_long(300, "title", 255, "Nothing was changed.", "content_text"),
        )
        note.refresh_from_db()
        self.assertEqual(note.title, "Keep me")

    def test_update_note_at_255_works(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="a", body=[]
        )
        text, is_error = self.text(
            "update_note", {"note_id": note.note_id, "note_type": "personal", "title": "n" * 255}
        )
        self.assertFalse(is_error, text)


class TestTodos(Base):
    def test_create_todo_title_over_512_is_refused(self):
        self.refused(
            "create_todo_item",
            {"title": "d" * 513},
            too_long(513, "title", 512, "Nothing was created.", "notes_text"),
        )
        self.assertFalse(ToDoItem.objects.filter(title__startswith="d" * 20).exists())

    def test_create_todo_title_of_512_works_and_300_still_works(self):
        for n in (512, 300):
            text, is_error = self.text("create_todo_item", {"title": "d" * n})
            self.assertFalse(is_error, text)

    def test_create_todo_category_over_64_is_refused(self):
        self.refused(
            "create_todo_item",
            {"title": "t", "category": "c" * 65},
            too_long(65, "category", 64, "Nothing was created.", None),
        )
        self.assertFalse(ToDoCategory.objects.exists())
        self.assertFalse(ToDoItem.objects.filter(title="t").exists())

    def test_a_category_of_64_works(self):
        text, is_error = self.text("create_todo_item", {"title": "t", "category": "c" * 64})
        self.assertFalse(is_error, text)

    def _item(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=__import__("datetime").date.today()
        )
        return ToDoItem.objects.create(group=group, title="Original")

    def test_update_todo_title_over_512_is_refused_and_unchanged(self):
        item = self._item()
        self.refused(
            "update_todo_item",
            {"item_id": item.item_id, "title": "u" * 600, "is_completed": True},
            too_long(600, "title", 512, "Nothing was changed.", "notes_text"),
        )
        item.refresh_from_db()
        self.assertEqual((item.title, item.is_completed), ("Original", False))

    def test_update_todo_category_over_64_is_refused_and_unchanged(self):
        item = self._item()
        self.refused(
            "update_todo_item",
            {"item_id": item.item_id, "category": "c" * 70},
            too_long(70, "category", 64, "Nothing was changed.", None),
        )

    def test_update_todo_at_the_limits_works(self):
        item = self._item()
        text, is_error = self.text(
            "update_todo_item", {"item_id": item.item_id, "title": "u" * 512, "category": "c" * 64}
        )
        self.assertFalse(is_error, text)


class TestAlreadyClear(Base):
    """The tools that already refused in words. Pinned so they stay that way."""

    def test_milestone_title(self):
        text, is_error = self.text(
            "manage_milestone", {"action": "create", "project_id": self.pid, "title": "m" * 300}
        )
        self.assertTrue(is_error)
        self.assertIn("`title` is too long (300 characters; the limit is 255).", text)
        self.assertIn("Nothing was created", text)

    def test_sprint_name(self):
        text, is_error = self.text(
            "manage_sprint",
            {
                "action": "create",
                "project_id": self.pid,
                "name": "s" * 200,
                "start_date": "2026-01-01",
                "end_date": "2026-01-14",
            },
        )
        self.assertEqual(
            (text, is_error), ("`name` is too long (200 characters; the limit is 120).", True)
        )

    def test_note_folder_name(self):
        text, is_error = self.text(
            "manage_note_folder", {"action": "create", "name": "f" * 300, "space": "personal"}
        )
        self.assertEqual(
            (text, is_error), ("Folder name must be 1-255 characters. Nothing was created.", True)
        )


class TestSearchQuery(Base):
    RESULTS = {"results": []}

    def search(self, query, *, mcp=True):
        ctx = ToolContext(team_id="t", user_id="u", surface="mcp" if mcp else "")
        with mock.patch.object(search_kb, "search", return_value=self.RESULTS) as s:
            out = search_kb._run({"query": query}, ctx)
        return out, s

    def test_the_default_is_1000_characters(self):
        self.assertEqual(field_limits.MCP_SEARCH_QUERY_MAX_CHARS, 1000)

    def test_over_the_limit_over_mcp_is_refused_and_nothing_is_searched(self):
        with mock.patch.object(search_kb, "search") as s:
            with self.assertRaises(ToolError) as caught:
                search_kb._run(
                    {"query": "q" * 1001}, ToolContext(team_id="t", user_id="u", surface="mcp")
                )
        s.assert_not_called()
        self.assertEqual(
            str(caught.exception),
            "`query` is too long: 1001 characters were sent and the limit is 1000. "
            "Nothing was searched. Shorten it and send the call again.",
        )

    def test_at_the_limit_it_is_searched(self):
        out, s = self.search("q" * 1000)
        s.assert_called_once()
        self.assertEqual(out["matches"], [])

    def test_the_limit_is_on_the_stripped_query(self):
        out, s = self.search("  " + "q" * 1000 + "   ")
        s.assert_called_once()

    def test_the_in_app_agent_is_not_limited(self):
        out, s = self.search("q" * 5000, mcp=False)
        s.assert_called_once()

    def test_through_the_endpoint_it_is_a_tool_error_the_model_can_read(self):
        with mock.patch.object(search_kb, "search", return_value=self.RESULTS):
            text, is_error = self.text("search_workspace", {"query": "q" * 2000})
        self.assertTrue(is_error)
        self.assertTrue(text.startswith("`query` is too long: 2000 characters"))

    def test_a_normal_search_through_the_endpoint_is_untouched(self):
        with mock.patch.object(search_kb, "search", return_value=self.RESULTS) as s:
            text, is_error = self.text("search_workspace", {"query": "login redirect"})
        self.assertFalse(is_error, text)
        s.assert_called_once()


class TestNothingModelVisibleMoved(Base):
    def test_the_in_app_agent_hears_the_same_sentence(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolError) as caught:
            REGISTRY["create_task"].run({"title": "x" * 256, "project_id": self.pid}, ctx)
        self.assertEqual(
            str(caught.exception),
            too_long(256, "title", 255, "Nothing was created.", "content_text"),
        )

    def test_no_published_schema_gained_a_length(self):
        listed = self._result(self._rpc("tools/list"))["tools"]
        for tool in listed:
            for name, spec in (tool["inputSchema"].get("properties") or {}).items():
                self.assertNotIn("maxLength", spec, f"{tool['name']}.{name}")


class TestLimitsComeFromTheColumns(SimpleTestCase):
    def test_values(self):
        self.assertEqual(
            (
                field_limits.TASK_TITLE_MAX,
                field_limits.MILESTONE_TITLE_MAX,
                field_limits.NOTE_TITLE_MAX,
                field_limits.TODO_TITLE_MAX,
                field_limits.TODO_CATEGORY_MAX,
            ),
            (255, 255, 255, 512, 64),
        )

    def test_a_non_string_is_not_this_checks_to_judge(self):
        for value in (None, 5, ["x" * 999], {"a": 1}):
            field_limits.check_text_length(value, "title", 3)

    def test_the_length_is_in_characters(self):
        field_limits.check_text_length("日本語", "title", 3)
        with self.assertRaises(ToolError):
            field_limits.check_text_length("日本語x", "title", 3)

    def test_env_for_the_query_cap(self):
        for raw, want in (("", 1000), ("x", 1000), ("0", 1000), ("-4", 1000), ("250", 250)):
            with mock.patch.dict("os.environ", {"MCP_SEARCH_QUERY_MAX_CHARS": raw}):
                self.assertEqual(field_limits._env_int("MCP_SEARCH_QUERY_MAX_CHARS", 1000), want)
