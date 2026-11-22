"""`edit_note_block` for the in-app agent, behind `AGENT_INAPP_NOTE_BLOCK_TOOL` (MIP-1656).

The MCP tool (MIP-1646) changes ONE block of a note. This is its in-app twin, shipped DARK (a new model-visible
tool needs a routing and behaviour eval, see the module docstring of `agent/tools/edit_note_block.py`). Pinned:

- OFF (the default): the tool is neither declared nor runnable, `fetch_note` is the registry's own declaration and
  ignores `include_blocks`;
- ON: the tool is declared, asks for approval, is a single write a read-only member does not get, and says when
  to use it instead of `update_note`; `fetch_note` gains exactly the two block parameters;
- ON or OFF, MCP is untouched (same tool, same published schemas, same key order);
- running it is the MCP tool's work with the MCP tool's checks: a viewer, a stranger and an archived project are
  refused before collab is asked; the sentences name `fetch_note`, never `get_note`;
- the blocks the model reads are fenced;
- the approval card has nothing editable and the call cannot be undone, and both are declared.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent import approval_edits, undo
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS, disabled_tools_for_level
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import (
    FLAGGED_REGISTRY,
    ToolError,
    ToolForbidden,
    app_write_tools,
    get_app_tool,
    get_mcp_tool,
)
from origin.search_engine.agent.tools.edit_note_block import EDIT_NOTE_BLOCK_INAPP
from origin.search_engine.agent.tools.fetch_note import FETCH_NOTE
from origin.views.public.mcp import tools as mcp_tools

from .test_mcp_note_block_edit import VIEWER_ROLE, BlockBase, body

FLAG = "AGENT_INAPP_NOTE_BLOCK_TOOL"
NAME = "edit_note_block"
BLOCK_PARAMS = {"include_blocks", "blocks_matching"}


def flag(on: bool):
    return mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: on})


def declared():
    return {d.name: d for d in _build_tool_declarations()}


class DarkWhileOffTests(SimpleTestCase):
    def test_the_flag_is_off_by_default_in_production(self):
        from apis import settings as production  # noqa: PLC0415

        self.assertIs(production.SEARCH_ENGINE.get(FLAG, False), False)

    def test_off_the_tool_is_not_declared_and_not_runnable(self):
        with flag(False):
            self.assertNotIn(NAME, declared())
            self.assertIsNone(get_app_tool(NAME))
            self.assertNotIn(NAME, {t.name for t in app_write_tools()})
        self.assertNotIn(NAME, REGISTRY)

    def test_off_fetch_note_is_the_registry_declaration_untouched(self):
        with flag(False):
            props = declared()["fetch_note"].parameters_schema["properties"]
        self.assertFalse(BLOCK_PARAMS & set(props))
        self.assertEqual(sorted(props), ["note_id", "note_type"])

    def test_it_is_filed_under_its_flag(self):
        flag_name, tool = FLAGGED_REGISTRY[NAME]
        self.assertEqual(flag_name, FLAG)
        self.assertIs(tool, EDIT_NOTE_BLOCK_INAPP)


class DeclaredWhenOnTests(SimpleTestCase):
    def test_on_it_is_declared_runnable_and_a_write(self):
        with flag(True):
            self.assertIn(NAME, declared())
            self.assertIs(get_app_tool(NAME), EDIT_NOTE_BLOCK_INAPP)
            self.assertIn(NAME, {t.name for t in app_write_tools()})
        self.assertTrue(EDIT_NOTE_BLOCK_INAPP.requires_approval)
        self.assertFalse(EDIT_NOTE_BLOCK_INAPP.mcp_only)

    def test_turning_it_on_adds_this_tool_and_two_fetch_note_parameters_only(self):
        with flag(False):
            before = list(_build_tool_declarations())
        with flag(True):
            after = list(_build_tool_declarations())
        self.assertEqual([d.name for d in after if d.name not in {b.name for b in before}], [NAME])
        by_name_before = {d.name: d for d in before}
        for d in after:
            if d.name in (NAME, "fetch_note"):
                continue
            self.assertEqual(d, by_name_before[d.name], f"{d.name} moved")
        fetch = next(d for d in after if d.name == "fetch_note")
        self.assertEqual(
            set(fetch.parameters_schema["properties"]), {"note_id", "note_type"} | BLOCK_PARAMS
        )
        self.assertEqual(fetch.parameters_schema["required"], ["note_type", "note_id"])

    def test_it_is_a_single_write_a_read_only_member_does_not_get(self):
        self.assertIn(NAME, SINGLE_WRITE_TOOLS)
        with flag(True):
            self.assertIn(NAME, disabled_tools_for_level("read"))
            self.assertNotIn(NAME, disabled_tools_for_level("act"))

    def test_the_description_says_when_to_use_it_instead_of_update_note(self):
        text = EDIT_NOTE_BLOCK_INAPP.description
        for needle in ("fetch_note", "include_blocks", "update_note", "REQUIRES USER APPROVAL"):
            self.assertIn(needle, text)
        self.assertLess(text.index("fetch_note"), text.index("replace"), "the read comes first")

    def test_the_shape_matches_the_mcp_tool(self):
        mcp = MCP_ONLY_REGISTRY[NAME]
        self.assertEqual(
            sorted(EDIT_NOTE_BLOCK_INAPP.parameters_schema["properties"]),
            sorted(mcp.parameters_schema["properties"]),
        )
        self.assertEqual(
            EDIT_NOTE_BLOCK_INAPP.parameters_schema["required"],
            mcp.parameters_schema["required"],
        )

    def test_what_it_costs_the_prompt_when_on(self):
        def chars():
            return sum(
                len(d.name)
                + len(d.description)
                + len(json.dumps(d.parameters_schema, ensure_ascii=False))
                for d in _build_tool_declarations()
            )

        with flag(False):
            off = chars()
        with flag(True):
            on = chars()
        self.assertGreater(on - off, 600)
        self.assertLess(on - off, 2500)


class McpUntouchedTests(SimpleTestCase):
    def test_mcp_still_gets_its_own_tool(self):
        for on in (False, True):
            with flag(on):
                self.assertIs(get_mcp_tool(NAME), MCP_ONLY_REGISTRY[NAME])

    def test_published_schemas_do_not_move_with_the_flag(self):
        def published():
            out = {}
            for tool in mcp_tools.CATALOG:
                if tool.name in (NAME, "get_note"):
                    out[tool.name] = json.dumps(
                        [tool.description, mcp_tools.input_schema(tool)], sort_keys=False
                    )
            return out

        with flag(False):
            off = published()
        with flag(True):
            on = published()
        self.assertEqual(set(off), {NAME, "get_note"})
        self.assertEqual(off, on)


class ApprovalAndUndoTests(SimpleTestCase):
    def test_nothing_is_editable_on_the_card(self):
        self.assertEqual(approval_edits.EDITABLE_FIELDS[NAME], {})

    def test_it_cannot_be_undone_and_says_why(self):
        self.assertEqual(undo.UNDO_SUPPORT[NAME], undo.NONE)
        code, line = undo.UNDO_REASONS[NAME]
        self.assertEqual(code, undo.NOT_YET)
        self.assertIn("version history", line)


class RunTests(BlockBase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def run_tool(self, ctx=None, **extra):
        return EDIT_NOTE_BLOCK_INAPP.run(self.args(**extra), ctx or self.ctx)

    def test_replace_reaches_collab_with_the_block_and_the_hash(self):
        self.collab_ok(new_ids=["n1"])
        out = self.run_tool(
            block_id="b-para", op="replace", content_text="new words", base_block_hash="a" * 16
        )
        self.assertEqual(out["op"], "replace")
        self.assertEqual(out["new_block_ids"], ["n1"])
        self.assertEqual(self.sent["blockId"], "b-para")
        self.assertEqual(self.sent["op"], "replace")
        self.assertEqual(self.sent["baseBlockHash"], "a" * 16)

    def test_delete_reaches_collab(self):
        self.collab_ok()
        out = self.run_tool(block_id="b-para", op="delete")
        self.assertEqual(out["op"], "delete")
        self.assertEqual(self.sent["op"], "delete")

    def test_a_note_nobody_opened_is_edited_in_its_stored_body(self):
        self.no_doc()
        out = self.run_tool(block_id="b-para", op="delete")
        self.assertEqual(out["live_editor"], "not_open")
        self.assertNotIn("b-para", json.dumps(self.fresh().body))

    def test_a_viewer_is_refused_and_collab_is_not_asked(self):
        from origin.models.note.common_note_models import NotePermissionMaster  # noqa: PLC0415

        NotePermissionMaster.objects.filter(user=self.user2).delete()
        self._grant(self.user2, VIEWER_ROLE)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolForbidden):
            self.run_tool(ctx, block_id="b-para", op="delete")
        self.post.assert_not_called()
        self.assertEqual(self.fresh().body, body())

    def test_a_stranger_gets_not_found_and_collab_is_not_asked(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolError) as caught:
            self.run_tool(ctx, block_id="b-para", op="delete")
        self.assertIn("not found", str(caught.exception))
        self.post.assert_not_called()

    def test_an_archived_projects_note_is_refused_before_collab(self):
        from origin.services import archive  # noqa: PLC0415

        with mock.patch.object(archive, "archived_note_refusal", return_value="Archived."):
            with mock.patch(
                "origin.views.public.mcp.note_blocks.edit_note_block.archived_note_refusal",
                return_value="Archived.",
            ):
                with self.assertRaises(ToolError) as caught:
                    self.run_tool(block_id="b-para", op="delete")
        self.assertIn("Nothing was changed", str(caught.exception))
        self.post.assert_not_called()

    def test_sentences_name_fetch_note_never_get_note(self):
        for bad in ({"block_id": "bad id!", "op": "delete"},):
            with self.assertRaises(ToolError) as caught:
                self.run_tool(**bad)
            self.assertIn("fetch_note", str(caught.exception))
            self.assertNotIn("get_note", str(caught.exception))
        self.answer({"error": "block not found", "kind": "block_not_found"}, 409)
        self.post.side_effect = None
        with mock.patch(
            "origin.views.public.mcp.note_blocks.edit_note_block.collab_push.edit_note_block",
            side_effect=__import__(
                "origin.services.collab_push", fromlist=["BlockEditError"]
            ).BlockEditError("block_not_found"),
        ):
            with self.assertRaises(ToolError) as caught:
                self.run_tool(block_id="gone", op="delete")
        self.assertIn("fetch_note", str(caught.exception))
        self.assertNotIn("get_note", str(caught.exception))


class FetchNoteBlocksTests(BlockBase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def fetch(self, **extra):
        return FETCH_NOTE.run(
            {"note_type": "personal", "note_id": self.note.note_id, **extra}, self.ctx
        )

    def test_off_include_blocks_is_ignored_and_collab_not_asked(self):
        with flag(False):
            out = self.fetch(include_blocks=True)
        self.assertNotIn("blocks", out)
        self.post.assert_not_called()

    def test_on_blocks_are_listed_and_their_text_is_fenced(self):
        self.answer({"exists": False, "blocks": []})
        with flag(True):
            out = self.fetch(include_blocks=True)
        self.assertEqual(out["blocks"][0]["block_id"], "b-intro")
        for block in out["blocks"]:
            self.assertIn("workspace_content", block["text"])
        self.assertEqual(list(out)[-1], "__summary__")

    def test_on_without_include_blocks_nothing_is_added(self):
        with flag(True):
            out = self.fetch()
        self.assertNotIn("blocks", out)
        self.post.assert_not_called()

    def test_on_a_long_note_is_cut(self):
        from origin.search_engine.agent.tools import fetch_note  # noqa: PLC0415

        with flag(True), mock.patch.object(fetch_note, "IN_APP_MAX_BLOCKS", 2):
            self.answer({"exists": False, "blocks": []})
            out = self.fetch(include_blocks=True)
        self.assertEqual(len(out["blocks"]), 2)
        self.assertIn("blocks_matching", out["blocks_note"])

    def test_on_collab_down_is_blocks_null_with_a_note(self):
        import requests  # noqa: PLC0415

        self.post.side_effect = requests.ConnectionError("down")
        with flag(True):
            out = self.fetch(include_blocks=True)
        self.assertIsNone(out["blocks"])
        self.assertIn("blocks_note", out)


class ProjectNameWordingTests(SimpleTestCase):
    """MIP-1957: a project name is no longer "unique in the team" (a hidden project's name must not be
    probed). The wording is true before and after: a name a project the caller can see already has is refused."""

    def test_manage_project_does_not_promise_team_wide_unique_names(self):
        manage = REGISTRY["manage_project"].description
        published = next(t for t in mcp_tools.CATALOG if t.name == "manage_project").description
        for text in (manage, published):
            self.assertNotIn("unique in the team", text)
            self.assertIn("a project you can see already has it", text)
