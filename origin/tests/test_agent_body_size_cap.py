"""A size cap on every body an agent tool writes (MIP-822).

`create_note`, `update_note`, `add_comment`, `create_task`, `update_task`,
`create_task_plan`, `create_todo_item` and `update_todo_item` take a markdown
or text body from a model or, over MCP, from an API key. None was bounded; the
regexes behind them cost about a second per quarter-megabyte.

What is pinned here:

  * the helper: under / exactly at / one over the limit, bytes not characters,
    the message, the settings override and its fallback;
  * every write path refuses an over-limit body BEFORE doing anything (nothing
    is written), and accepts one exactly at the limit;
  * MCP reaches the same guard and returns it as an `isError` result;
  * a drift test: a new approval-gated tool that takes a body-shaped argument
    fails until it is either guarded or listed as deliberately left out.

The per-path tests run with a tiny `AGENT_MAX_BODY_BYTES` so they are fast and
independent of the default; the default itself is pinned once.
"""

from __future__ import annotations

from datetime import date

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import (
    DEFAULT_MAX_BODY_BYTES,
    check_body_size,
    check_body_sizes,
    max_body_bytes,
)

from .test_base import BaseAPITestCase

LIMIT = 200  # bytes, for the per-path tests


class TheDefault(SimpleTestCase):
    def test_the_default_is_256_kb(self):
        self.assertEqual(DEFAULT_MAX_BODY_BYTES, 262_144)
        self.assertEqual(settings.AGENT_MAX_BODY_BYTES, 262_144)
        self.assertEqual(max_body_bytes(), 262_144)

    def test_a_quarter_megabyte_body_passes_and_one_byte_more_does_not(self):
        check_body_size("a" * 262_144, "content_text")
        with self.assertRaises(ToolError):
            check_body_size("a" * 262_145, "content_text")


@override_settings(AGENT_MAX_BODY_BYTES=LIMIT)
class TheHelper(SimpleTestCase):
    def test_under_the_limit_passes(self):
        check_body_size("a" * (LIMIT - 1), "content_text")

    def test_exactly_at_the_limit_passes(self):
        check_body_size("a" * LIMIT, "content_text")

    def test_one_byte_over_fails(self):
        with self.assertRaises(ToolError):
            check_body_size("a" * (LIMIT + 1), "content_text")

    def test_the_unit_is_bytes_not_characters(self):
        # 66 three-byte characters = 198 bytes: fine.
        check_body_size("あ" * 66, "content_text")
        # 67 of them is 201 bytes from 67 characters: over, though 67 << 200.
        with self.assertRaises(ToolError) as caught:
            check_body_size("あ" * 67, "content_text")
        self.assertIn("201 bytes", str(caught.exception))

    def test_a_four_byte_character_counts_four(self):
        check_body_size("😀" * 50, "content_text")  # exactly 200
        with self.assertRaises(ToolError):
            check_body_size("😀" * 51, "content_text")  # 204

    def test_the_raw_text_is_counted_not_the_stripped_text(self):
        with self.assertRaises(ToolError):
            check_body_size(" " * LIMIT + "x", "content_text")

    def test_a_lone_surrogate_is_counted_not_a_crash(self):
        # JSON can carry "\ud800"; a strict UTF-8 encode raises on it, which
        # would be a 500 instead of a refusal.
        check_body_size("\ud800" * 66, "content_text")  # 3 bytes each = 198
        with self.assertRaises(ToolError):
            check_body_size("\ud800" * 67, "content_text")

    def test_the_error_names_the_field_the_size_and_the_limit_and_says_what_to_do(self):
        with self.assertRaises(ToolError) as caught:
            check_body_size("a" * 1234, "body_text")
        message = str(caught.exception)
        self.assertIn("`body_text`", message)
        self.assertIn("1,234 bytes", message)
        self.assertIn(f"{LIMIT:,} bytes", message)
        self.assertIn("Nothing was saved", message)
        self.assertIn("split", message)

    def test_non_strings_and_empty_values_are_not_this_helpers_business(self):
        for value in (None, "", 5, ["a" * 1000], {"a": "b" * 1000}):
            with self.subTest(value=repr(value)[:20]):
                check_body_size(value, "content_text")

    def test_several_bodies_must_each_fit_and_fit_together(self):
        check_body_sizes([("a", "x" * 100), ("b", "y" * 100), ("c", None)])  # exactly 200
        with self.assertRaises(ToolError) as caught:
            check_body_sizes([("a", "x" * 100), ("b", "y" * 101)])
        self.assertIn("201 bytes of body text in total", str(caught.exception))
        with self.assertRaises(ToolError) as caught:
            check_body_sizes([("tasks[3].content_markdown", "x" * 201)])
        self.assertIn("`tasks[3].content_markdown`", str(caught.exception))


class TheLimitIsOneSetting(SimpleTestCase):
    @override_settings(AGENT_MAX_BODY_BYTES=10)
    def test_the_setting_is_read_on_every_call(self):
        self.assertEqual(max_body_bytes(), 10)
        with self.assertRaises(ToolError):
            check_body_size("a" * 11, "content_text")

    def test_zero_negative_and_junk_fall_back_to_the_default(self):
        for bad in (0, -5, "nope", None):
            with self.subTest(value=bad), override_settings(AGENT_MAX_BODY_BYTES=bad):
                self.assertEqual(max_body_bytes(), DEFAULT_MAX_BODY_BYTES)


@override_settings(AGENT_MAX_BODY_BYTES=LIMIT)
class EveryWritePathRefusesAnOversizeBody(BaseAPITestCase):
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
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Fix the login redirect",
            status="Open",
            project_task_number=1,
        )
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ok = "a" * LIMIT
        self.big = "a" * (LIMIT + 1)

    def _run(self, name, args):
        return REGISTRY[name].run(args, self.ctx)

    def _refused(self, name, args, field):
        with self.assertRaises(ToolError) as caught:
            self._run(name, args)
        message = str(caught.exception)
        self.assertIn("too large", message)
        self.assertIn(f"`{field}`", message)
        self.assertIn(f"{LIMIT + 1} bytes", message)

    # -- create_note / update_note --

    def _note_args(self, body):
        return {"note_type": "personal", "title": "Spec", "content_text": body}

    def test_create_note(self):
        self._refused("create_note", self._note_args(self.big), "content_text")
        self.assertFalse(PersonalNoteMaster.objects.filter(owner=self.user).exists())
        self._run("create_note", self._note_args(self.ok))
        self.assertTrue(PersonalNoteMaster.objects.filter(owner=self.user).exists())

    def test_create_note_counts_bytes(self):
        self._refused("create_note", self._note_args("あ" * 67), "content_text")
        self.assertFalse(PersonalNoteMaster.objects.filter(owner=self.user).exists())

    def test_update_note(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="mine", body=[]
        )
        args = {"note_type": "personal", "note_id": note.note_id}
        self._refused("update_note", {**args, "content_text": self.big}, "content_text")
        note.refresh_from_db()
        self.assertEqual(note.body, [])
        self._run("update_note", {**args, "content_text": self.ok})
        note.refresh_from_db()
        self.assertNotEqual(note.body, [])

    # -- add_comment --

    def test_add_comment(self):
        before = TaskComments.objects.count()
        args = {"task_id": self.task.task_id}
        self._refused("add_comment", {**args, "body_text": self.big}, "body_text")
        self.assertEqual(TaskComments.objects.count(), before)
        self._run("add_comment", {**args, "body_text": self.ok})
        self.assertEqual(TaskComments.objects.count(), before + 1)

    # -- update_comment --

    def test_update_comment(self):
        args = {"task_id": self.task.task_id}
        comment_id = self._run("add_comment", {**args, "body_text": "first draft"})["comment_id"]
        row = TaskComments.objects.get(task=self.task, comment_id=comment_id)
        before = row.comment_body
        update = {**args, "comment_id": comment_id}
        self._refused("update_comment", {**update, "body_text": self.big}, "body_text")
        row.refresh_from_db()
        self.assertEqual(row.comment_body, before, "a refused edit changes nothing")
        self._run("update_comment", {**update, "body_text": self.ok})
        row.refresh_from_db()
        self.assertNotEqual(row.comment_body, before)

    # -- create_task / update_task --

    def test_create_task(self):
        args = {"title": "New thing", "project_id": self.project.project_id}
        self._refused("create_task", {**args, "content_text": self.big}, "content_text")
        self.assertFalse(TaskMaster.objects.filter(title="New thing").exists())
        self._run("create_task", {**args, "content_text": self.ok})
        self.assertTrue(TaskMaster.objects.filter(title="New thing").exists())

    def test_update_task(self):
        args = {"task_id": self.task.task_id}
        self._refused("update_task", {**args, "content_text": self.big}, "content_text")
        self.task.refresh_from_db()
        self.assertFalse(self.task.content)
        self._run("update_task", {**args, "content_text": self.ok})
        self.task.refresh_from_db()
        self.assertTrue(self.task.content)

    def test_update_task_partial_edits(self):
        """MIP-824's partial edits write to the same body, so the same cap applies."""
        args = {"task_id": self.task.task_id}
        self._refused("update_task", {**args, "body_append": self.big}, "body_append")
        self._refused("update_task", {**args, "body_prepend": self.big}, "body_prepend")
        self._refused(
            "update_task",
            {**args, "body_replace": {"old_text": "x", "new_text": self.big}},
            "body_replace.new_text",
        )
        self._refused(
            "update_task",
            {**args, "body_replace": {"old_text": self.big, "new_text": "x"}},
            "body_replace.old_text",
        )

    # -- create_task_plan --

    def _plan(self, *bodies, milestone_body=None):
        plan = {
            "project_id": self.project.project_id,
            "tasks": [{"title": f"Step {i}", "content_markdown": b} for i, b in enumerate(bodies)],
        }
        if milestone_body is not None:
            plan["milestone"] = {"title": "Launch", "description_markdown": milestone_body}
        return plan

    def test_create_task_plan_refuses_one_oversize_task_body(self):
        self._refused(
            "create_task_plan", self._plan("short", self.big), "tasks[1].content_markdown"
        )
        self.assertFalse(TaskMaster.objects.filter(title__startswith="Step ").exists())

    def test_create_task_plan_refuses_an_oversize_milestone_description(self):
        self._refused(
            "create_task_plan",
            self._plan("short", milestone_body=self.big),
            "milestone.description_markdown",
        )
        self.assertFalse(TaskMaster.objects.filter(title__startswith="Step ").exists())

    def test_create_task_plan_accepts_one_body_exactly_at_the_limit(self):
        self._run("create_task_plan", self._plan(self.ok))
        self.assertTrue(TaskMaster.objects.filter(title="Step 0").exists())

    def test_create_task_plan_caps_the_whole_call_not_only_each_body(self):
        half = "a" * (LIMIT // 2 + 1)
        with self.assertRaises(ToolError) as caught:
            self._run("create_task_plan", self._plan(half, half))
        self.assertIn("in total", str(caught.exception))
        self.assertFalse(TaskMaster.objects.filter(title__startswith="Step ").exists())

    # -- to-do notes --

    def test_create_todo_item(self):
        args = {"title": "Call the lawyer"}
        self._refused("create_todo_item", {**args, "notes_text": self.big}, "notes_text")
        self.assertFalse(ToDoItem.objects.filter(title="Call the lawyer").exists())
        self._run("create_todo_item", {**args, "notes_text": self.ok})
        self.assertTrue(ToDoItem.objects.filter(title="Call the lawyer").exists())

    def test_update_todo_item(self):
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 11, 1)
        )
        item = ToDoItem.objects.create(group=group, title="x", notes=[])
        args = {"item_id": item.item_id}
        self._refused("update_todo_item", {**args, "notes_text": self.big}, "notes_text")
        item.refresh_from_db()
        self.assertEqual(item.notes, [])
        self._run("update_todo_item", {**args, "notes_text": self.ok})
        item.refresh_from_db()
        self.assertNotEqual(item.notes, [])


# Approval-gated tools with a body-shaped string argument, and what was done
# about each. A tool added to the registry that is not in one of these two
# sets fails the test below, which is the point: a new body-taking write has
# to be a decision, not an omission.
GUARDED = {
    "create_note": {"content_text"},
    "update_note": {"content_text"},
    "add_comment": {"body_text"},
    "update_comment": {"body_text"},
    "create_task": {"content_text"},
    "update_task": {"content_text", "body_append", "body_prepend", "old_text", "new_text"},
    "create_task_plan": {"content_markdown", "description_markdown"},
    "create_todo_item": {"notes_text"},
    "update_todo_item": {"notes_text"},
    "manage_milestone": {"description_markdown"},
}
# Deliberately not capped here: the text is forwarded to Google Calendar, which
# enforces its own limit, and it is not a note, comment or task body.
LEFT_OUT = {
    "create_calendar_event": {"description"},
    "update_calendar_event": {"description"},
    # A hash of the body, not a body: fetch_task's short `body_hash` string.
    "update_task": {"base_body_hash"},
}
_BODY_WORDS = ("text", "markdown", "body", "content", "notes", "description")


def _string_property_names(schema, found=None):
    found = set() if found is None else found
    if isinstance(schema, dict):
        for name, sub in (schema.get("properties") or {}).items():
            if isinstance(sub, dict) and sub.get("type") == "STRING":
                found.add(name)
            _string_property_names(sub, found)
        _string_property_names(schema.get("items"), found)
    return found


class NoBodyTakingWriteIsLeftUnconsidered(SimpleTestCase):
    def test_every_approval_gated_tool_with_a_body_argument_is_guarded_or_listed(self):
        unaccounted = {}
        for name, tool in REGISTRY.items():
            if not tool.requires_approval:
                continue
            bodyish = {
                p
                for p in _string_property_names(tool.parameters_schema)
                if any(word in p for word in _BODY_WORDS)
            }
            known = GUARDED.get(name, set()) | LEFT_OUT.get(name, set())
            if bodyish - known:
                unaccounted[name] = sorted(bodyish - known)
        self.assertEqual(
            unaccounted,
            {},
            "These write tools take a body-shaped argument that is neither capped "
            "(origin/search_engine/agent/tools/body_limits.py) nor listed as left out.",
        )

    def test_every_guarded_tool_still_exists(self):
        for name in GUARDED:
            self.assertIn(name, REGISTRY)
