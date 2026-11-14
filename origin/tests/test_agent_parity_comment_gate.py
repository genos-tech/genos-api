"""The gate for what MIP-1562 adds to `AGENT_PARITY_TOOLS`: `update_comment` takes `reaction`,
`remove_reaction` and `flag`, and `create_todo_item` takes `task_id` and `comment_id`.

The same contract `test_agent_parity_gate` holds the first three tools to (MIP-1561):

  * off, everything the model or an MCP client is shown is what it was, byte for byte;
  * on, the declarations carry the parameters and the sentences, in the app and over MCP, and the
    sentences that said the tool only edits are swapped and not contradicted;
  * a call that carries a parameter the declaration does not have is ignored, in the app; over MCP
    the endpoint refuses an argument the published schema does not define.
"""

from __future__ import annotations

import copy
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Channel, ChannelKind, Flag, Message
from origin.models.task.task_models import TaskCommentReactionFact
from origin.search_engine.agent import tool_flag_params, tool_flag_text
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
from origin.search_engine.agent.tools.todo_source import PARITY_PROPERTIES as TODO_NEW
from origin.search_engine.agent.tools.update_comment import PARITY_PROPERTIES as COMMENT_NEW
from origin.views.public.mcp import tools as mcp

from .test_agent_parity_gate import canon, declared
from .test_mcp_endpoint import McpBase

ON = {PARITY_FLAG: True}
OFF = {PARITY_FLAG: False}
TOOLS = ("update_comment", "create_todo_item")
NEW = {"update_comment": set(COMMENT_NEW), "create_todo_item": set(TODO_NEW)}


class RegistryIsUntouchedTests(SimpleTestCase):
    def test_the_registry_schemas_carry_none_of_the_parameters(self):
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertFalse(NEW[name] & set(REGISTRY[name].parameters_schema["properties"]))

    def test_body_text_is_still_required_in_the_registry(self):
        self.assertEqual(
            REGISTRY["update_comment"].parameters_schema["required"],
            ["task_id", "comment_id", "body_text"],
        )

    def test_laying_the_parameters_over_mutates_nothing(self):
        before = {n: copy.deepcopy(REGISTRY[n].parameters_schema) for n in TOOLS}
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            for name in TOOLS:
                tool_flag_params.with_flagged_params(name, REGISTRY[name].parameters_schema)
        self.assertEqual({n: REGISTRY[n].parameters_schema for n in TOOLS}, before)

    def test_no_added_name_shadows_a_real_parameter(self):
        for addition in tool_flag_params.ADDITIONS:
            self.assertFalse(
                set(addition.properties)
                & set(REGISTRY[addition.tool].parameters_schema["properties"])
            )
            self.assertLessEqual(
                set(addition.optional), set(REGISTRY[addition.tool].parameters_schema["required"])
            )


class OffIsByteIdenticalTests(SimpleTestCase):
    def test_the_declarations_are_the_registrys_own_text(self):
        off = declared(**OFF)
        for name in TOOLS:
            with self.subTest(tool=name):
                self.assertEqual(off[name].description, REGISTRY[name].description)
                self.assertEqual(
                    set(off[name].parameters_schema["properties"]),
                    set(REGISTRY[name].parameters_schema["properties"]),
                )
                self.assertEqual(
                    off[name].parameters_schema.get("required"),
                    REGISTRY[name].parameters_schema.get("required"),
                )

    def test_the_helper_hands_back_the_same_object(self):
        for name in TOOLS:
            schema = REGISTRY[name].parameters_schema
            with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
                self.assertIs(tool_flag_params.with_flagged_params(name, schema), schema)

    def test_switching_it_on_and_off_again_leaves_nothing_behind(self):
        first = {n: canon(d) for n, d in declared(**OFF).items()}
        declared(**ON)
        again = {n: canon(d) for n, d in declared(**OFF).items()}
        self.assertEqual(first, again)

    def test_the_mcp_catalog_with_the_flag_off_is_the_catalogs(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            for tool in mcp.CATALOG:
                if tool.registry_name not in TOOLS:
                    continue
                with self.subTest(tool=tool.name):
                    self.assertEqual(mcp.description_for(tool), tool.description)
                    props = set(mcp.input_schema(tool)["properties"])
                    self.assertFalse(NEW[tool.registry_name] & props)
                    self.assertEqual(
                        mcp.input_schema(tool).get("required"),
                        mcp._to_json_schema(REGISTRY[tool.registry_name].parameters_schema).get(
                            "required"
                        ),
                    )

    def test_every_replacement_is_inert_with_the_flag_off(self):
        by_name = {t.registry_name: t for t in mcp.CATALOG}
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertEqual(
                mcp._replaced(by_name["update_comment"]), by_name["update_comment"].description
            )


class OnPublishesTests(SimpleTestCase):
    def setUp(self):
        self.off, self.on = declared(**OFF), declared(**ON)

    def test_update_comment_gains_three_parameters_and_keeps_the_rest(self):
        off_props = self.off["update_comment"].parameters_schema["properties"]
        on_props = self.on["update_comment"].parameters_schema["properties"]
        self.assertEqual(set(on_props) - set(off_props), {"reaction", "remove_reaction", "flag"})
        self.assertEqual({k: on_props[k] for k in off_props}, off_props)
        self.assertEqual(on_props["flag"]["enum"], ["flag", "unflag", "done"])
        self.assertEqual(on_props["remove_reaction"]["type"], "BOOLEAN")

    def test_body_text_is_no_longer_required_while_it_is_on(self):
        on = self.on["update_comment"].parameters_schema
        self.assertEqual(on["required"], ["task_id", "comment_id"])
        self.assertIn("body_text", on["properties"])

    def test_create_todo_item_gains_task_and_comment_and_still_requires_only_a_title(self):
        off_props = self.off["create_todo_item"].parameters_schema["properties"]
        on = self.on["create_todo_item"].parameters_schema
        self.assertEqual(set(on["properties"]) - set(off_props), {"task_id", "comment_id"})
        self.assertEqual(on["required"], ["title"])
        # A task is named as people name it, as on the other task tools.
        self.assertEqual(on["properties"]["task_id"]["type"], "STRING")
        self.assertIn("PRJ-123", on["properties"]["task_id"]["description"])
        self.assertEqual(on["properties"]["comment_id"]["type"], "INTEGER")

    def test_the_sentences_are_added_and_the_old_ones_swapped(self):
        text = self.on["update_comment"].description
        for old, new in (
            (
                "Required: task_id, comment_id, body_text.",
                "Required: task_id, comment_id and one of body_text, reaction, flag.",
            ),
        ):
            self.assertIn(old, self.off["update_comment"].description)
            self.assertNotIn(old, text)
            self.assertIn(new, text)
        # The rule that only your own comment can be changed is now said for editing only.
        self.assertNotIn("(only comments the user wrote can be changed", text)
        self.assertIn("work on ANY comment of a task the user can open", text)
        self.assertIn("to EDIT with body_text only comments the user wrote", text)
        self.assertIn(
            "link the to-do to a task or comment", self.on["create_todo_item"].description
        )

    def test_every_replacement_names_text_the_tool_really_says(self):
        by_name = {t.registry_name: t for t in mcp.CATALOG}
        for name, flag, old, new in tool_flag_text.REPLACEMENTS:
            if name in TOOLS:
                with self.subTest(tool=name, old=old[:30]):
                    self.assertIn(old, REGISTRY[name].description)
                    self.assertNotEqual(old, new)
                    self.assertEqual(flag, PARITY_FLAG)
        for name, flag, old, new in tool_flag_text.MCP_REPLACEMENTS:
            with self.subTest(tool=name, mcp=old[:30]):
                self.assertIn(old, by_name[name].description)
                self.assertNotEqual(old, new)
                self.assertEqual(flag, PARITY_FLAG)

    def test_the_additions_are_terse(self):
        # Every character is billed on every call; measured whole in `test_agent_prefix_budget`.
        grown = sum(len(canon(self.on[n])) - len(canon(self.off[n])) for n in TOOLS)
        self.assertLess(grown, 1800)

    def test_over_mcp_the_catalog_says_the_same(self):
        by_name = {t.registry_name: t for t in mcp.CATALOG}
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            comment = by_name["update_comment"]
            schema = mcp.input_schema(comment)
            self.assertTrue(set(COMMENT_NEW) <= set(schema["properties"]))
            self.assertEqual(schema["properties"]["remove_reaction"]["type"], "boolean")
            self.assertEqual(schema["required"], ["task_id", "comment_id"])
            text = mcp.description_for(comment)
            self.assertIn("react to or flag any comment", text)
            self.assertIn("to EDIT with `body_text` only comments the caller wrote", text)
            self.assertNotIn("only comments the caller wrote can be changed", text)
            self.assertIn("work on ANY comment of a task the user can open", text)
            todo = by_name["create_todo_item"]
            props = mcp.input_schema(todo)["properties"]
            self.assertTrue(set(TODO_NEW) <= set(props))
            self.assertEqual(props["task_id"]["type"], "string")
            self.assertEqual(mcp.input_schema(todo)["required"], ["title"])
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            self.assertNotIn("reaction", mcp.input_schema(comment)["properties"])
            self.assertEqual(
                mcp.input_schema(comment)["required"], ["task_id", "comment_id", "body_text"]
            )

    def test_nothing_else_moved_with_the_flag_on(self):
        for name in self.off:
            if name in tool_flag_text_names():
                continue
            with self.subTest(tool=name):
                self.assertEqual(canon(self.off[name]), canon(self.on[name]))


def tool_flag_text_names() -> set[str]:
    """Every tool the parity flag changes (this batch and MIP-1561's)."""
    return {a.tool for a in tool_flag_params.ADDITIONS} | {
        n for n, f, _ in tool_flag_text.ADDITIONS if f == PARITY_FLAG
    }


class OverMcpTests(McpBase):
    """The surface MCP clients see: the parameters work end to end with the flag on and are
    refused (not guessed at) with it off."""

    def setUp(self):
        super().setUp()
        self._key()
        channel = Channel.objects.get(project_id=self.project.project_id, kind=ChannelKind.PM)
        Message.objects.create(
            channel=channel,
            sender=self.user,
            seq=1,
            body={"text": "task"},
            body_text="task",
            task_id=self.task.task_id,
        )
        from origin.search_engine.agent.tools import ToolContext  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.comment_id = ADD_COMMENT.run(
            {"task_id": self.task.task_id, "body_text": "Please check the footer."}, ctx
        )["comment_id"]

    def test_with_the_flag_off_the_new_arguments_are_refused_and_nothing_changes(self):
        for arguments in ({"reaction": "👍"}, {"flag": "flag"}):
            with self.subTest(arguments=arguments):
                text, is_error = self._tool_text(
                    self._call(
                        "update_comment",
                        {"task_id": "WRD-7", "comment_id": self.comment_id, **arguments},
                    )
                )
                self.assertTrue(is_error)
                self.assertIn("does not take", text)
        text, is_error = self._tool_text(
            self._call("create_todo_item", {"title": "T", "task_id": "WRD-7"})
        )
        self.assertTrue(is_error)
        self.assertFalse(TaskCommentReactionFact.objects.exists())
        self.assertFalse(Flag.objects.exists())
        self.assertFalse(ToDoItem.objects.exists())

    def test_with_the_flag_on_a_reaction_is_made_by_display_id(self):
        with (
            mock.patch.dict(settings.SEARCH_ENGINE, ON),
            mock.patch("origin.services.realtime_push.push_task_comment"),
        ):
            text, is_error = self._tool_text(
                self._call(
                    "update_comment",
                    {"task_id": "WRD-7", "comment_id": self.comment_id, "reaction": "🎉"},
                )
            )
        self.assertFalse(is_error, text)
        self.assertEqual(
            list(TaskCommentReactionFact.objects.values_list("reaction_emoji", flat=True)), ["🎉"]
        )

    def test_with_the_flag_on_a_flag_is_set_and_marked_done(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            for value in ("flag", "done"):
                text, is_error = self._tool_text(
                    self._call(
                        "update_comment",
                        {"task_id": "WRD-7", "comment_id": self.comment_id, "flag": value},
                    )
                )
                self.assertFalse(is_error, text)
        self.assertIsNotNone(Flag.objects.get().completed_at)

    def test_with_the_flag_on_a_to_do_is_linked_to_the_comment(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._tool_text(
                self._call(
                    "create_todo_item",
                    {
                        "title": "Check the footer",
                        "task_id": "WRD-7",
                        "comment_id": self.comment_id,
                    },
                )
            )
        self.assertFalse(is_error, text)
        notes = ToDoItem.objects.get().notes
        self.assertIn(f"/task/{self.task.task_id}/comment/{self.comment_id}", str(notes))

    def test_a_read_only_key_cannot_use_them(self):
        from origin.models.common.api_key_models import SCOPE_READ  # noqa: PLC0415

        self._key(scope=SCOPE_READ)
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            text, is_error = self._tool_text(
                self._call(
                    "update_comment",
                    {"task_id": "WRD-7", "comment_id": self.comment_id, "reaction": "👍"},
                )
            )
        self.assertTrue(is_error)
        self.assertFalse(TaskCommentReactionFact.objects.exists())

    def test_the_annotations_are_unchanged(self):
        by_name = {t.registry_name: t for t in mcp.CATALOG}
        for name in TOOLS:
            with self.subTest(tool=name):
                with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
                    off = mcp.annotations(by_name[name])
                with mock.patch.dict(settings.SEARCH_ENGINE, ON):
                    on = mcp.annotations(by_name[name])
                self.assertEqual(off, on)


class TheInAppLoopStillDeclaresTheSameTools(SimpleTestCase):
    def test_no_tool_is_added_or_removed(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, OFF):
            off = [d.name for d in _build_tool_declarations()]
        with mock.patch.dict(settings.SEARCH_ENGINE, ON):
            on = [d.name for d in _build_tool_declarations()]
        self.assertEqual(off, on)
