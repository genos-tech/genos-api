"""What OpenAI's plugin scan reads from every tool, and a delete that hid inside an argument
(MIP-1110).

OpenAI lists `readOnlyHint`, `destructiveHint` and `openWorldHint` as REQUIRED on every tool,
and defines the last as "the tool accesses the public internet or open-ended external
entities", adding that a bounded private account is not open-world just because someone else
hosts it. So: all three on every tool (a read says `destructiveHint: false`), and
`openWorldHint` only for the calendar writes, whose attendees are e-mailed by Google.

The second half is about the member's "Deleting needs its own yes": `update_task` with status
`Deleted` removes a task from every list, and used to run for a connection that was only
allowed to make changes. It is now held to the same opt-in as the delete tools.
"""

from __future__ import annotations

import re

from origin.tests.test_mcp_endpoint import SCOPE_READ, SCOPE_WRITE, McpBase
from origin.tests.test_mcp_oauth_flow import FlowBase
from origin.views.public.mcp import tools

DELETE_WORDS = re.compile(r"delet|remov|archiv|purge|revoke|unshare", re.I)


def enum_values(node):
    """Every value of every `enum` anywhere in a JSON schema."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "enum":
                yield from (v for v in value if isinstance(v, str))
            else:
                yield from enum_values(value)
    elif isinstance(node, list):
        for item in node:
            yield from enum_values(item)


class TestEveryToolSaysAllThree(McpBase):
    def listed(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        return {t["name"]: t["annotations"] for t in self._result(self._rpc("tools/list"))["tools"]}

    def test_all_three_hints_are_on_every_tool_and_are_booleans(self):
        for name, hints in self.listed().items():
            with self.subTest(tool=name):
                self.assertEqual(set(hints), {"readOnlyHint", "destructiveHint", "openWorldHint"})
                self.assertTrue(all(isinstance(v, bool) for v in hints.values()), hints)

    def test_the_hints_say_what_each_tool_is(self):
        by_name = {t.name: t for t in tools.visible_tools(SCOPE_WRITE, allow_destructive=True)}
        for name, hints in self.listed().items():
            tool = by_name[name]
            with self.subTest(tool=name):
                self.assertIs(hints["readOnlyHint"], not tool.write)
                # A read is never destructive; a write is exactly as destructive as its flag.
                self.assertIs(hints["destructiveHint"], tool.write and tool.destructive)

    def test_only_the_calendar_writes_reach_outside_people(self):
        open_world = {n for n, h in self.listed().items() if h["openWorldHint"]}
        self.assertEqual(
            open_world,
            {"create_calendar_event", "update_calendar_event", "delete_calendar_event"},
        )

    def test_the_open_world_names_are_real_tools(self):
        """A renamed tool must not silently stop being open-world."""
        self.assertLessEqual(tools.OPEN_WORLD_TOOLS, {t.name for t in tools.CATALOG})
        self.assertTrue(all(t.write for t in tools.CATALOG if t.name in tools.OPEN_WORLD_TOOLS))


class TestADeleteInsideAnArgument(McpBase):
    def update(self, **args):
        return self._tool_text(self._call("update_task", {"task_id": "WRD-7", **args}))

    def test_a_write_key_cannot_delete_a_task_through_its_status(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.update(status="Deleted")
        self.assertTrue(is_error)
        self.assertIn("status=Deleted", text)
        self.assertIn("deletes", text)
        self.assertIn("Settings", text, "the refusal must say how to fix it")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Open")

    def test_the_same_key_can_still_change_the_status_to_anything_else(self):
        self._key(scope=SCOPE_WRITE)
        text, is_error = self.update(status="Closed")
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Closed")

    def test_a_key_that_was_opted_into_deleting_may(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        text, is_error = self.update(status="Deleted")
        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "Deleted")

    def test_a_milestone_folder_may_be_deleted_with_write_scope_alone(self):
        """On purpose: a folder is only a grouping (see `ARGUMENT_DELETES_WITHOUT_THE_OPT_IN`).
        Here it must not be held by the opt-in; the tool then answers for itself (this folder
        does not exist)."""
        self._key(scope=SCOPE_WRITE)
        text, _ = self._tool_text(
            self._call(
                "manage_milestone",
                {"action": "delete_folder", "milestone_folder_id": 999999},
            )
        )
        self.assertNotIn("destructive tools", text)

    def test_a_read_key_is_still_refused_as_read_only_first(self):
        self._key(scope=SCOPE_READ, allow_destructive=True)
        text, is_error = self.update(status="Deleted")
        self.assertTrue(is_error)
        self.assertIn("read-only", text)

    def test_no_other_argument_deletes(self):
        """A write tool that is not a delete tool must not take a delete-looking value
        unless it is in `DELETING_ARGUMENTS` (it then needs the opt-in) or was decided to be fine
        without it (`ARGUMENT_DELETES_WITHOUT_THE_OPT_IN`)."""
        known = {(tool, value) for tool, (_field, value) in tools.DELETING_ARGUMENTS.items()}
        known |= tools.ARGUMENT_DELETES_WITHOUT_THE_OPT_IN
        found = set()
        for tool in tools.CATALOG:
            if tool.write and not tool.destructive:
                for value in enum_values(tools.input_schema(tool)):
                    if DELETE_WORDS.search(value):
                        found.add((tool.name, value))
        self.assertEqual(found, known)


class TestDescriptionsNameTheRightCredential(FlowBase):
    """Forty descriptions say "Requires a write-scoped API key". A person who connected an app
    has no key, and OpenAI asks that descriptions match behaviour: they are told "write access"
    (MIP-1110). A holder of a real API key reads the words that were always there."""

    def test_a_connected_app_is_never_told_about_an_api_key(self):
        tokens = self.tokens()
        listed = self._result(self.mcp(tokens["access_token"]))["tools"]
        writes = [t for t in listed if not t["annotations"]["readOnlyHint"]]
        self.assertTrue(writes)
        for tool in listed:
            with self.subTest(tool=tool["name"]):
                self.assertNotIn("API key", tool["description"])
                self.assertNotIn("write-scoped", tool["description"])
        for tool in writes:
            with self.subTest(write=tool["name"]):
                self.assertIn("write access", tool["description"])
        self.assertIn(
            "Requires write access.",
            {t["name"]: t["description"] for t in listed}["update_task"],
        )

    def test_a_key_holder_reads_what_was_always_there(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        listed = {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}
        self.assertIn("a write-scoped API key", listed["update_task"]["description"])
