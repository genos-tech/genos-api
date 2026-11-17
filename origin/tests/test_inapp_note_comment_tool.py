"""`comment_on_note` for the in-app agent, behind `AGENT_INAPP_NOTE_COMMENT_TOOL` (MIP-1671).

The MCP tool (MIP-1644) starts a comment thread on a passage of a note. This is its in-app twin, shipped DARK:
a new model-visible tool adds a declaration to every request and can take an intent from a tool the model
already uses (`add_comment` for a task, `update_note` for a note's own text), and that needs an eval nobody has
run. What is pinned is everything that does not need one:

- OFF (the default): the tool is neither declared nor runnable in the app, and MCP's own tool is the one MCP runs;
- ON: it is declared, asks for approval, is a single write that a read-only member does not get, reads the note
  first and says what its neighbours are for, and takes no block id (the in-app model has none);
- ON or OFF, MCP is untouched: the same tool, the same published schema;
- running it is the MCP tool's work: collab is asked as the caller, a refusal is a sentence, nothing the model
  invents (a block id) reaches collab, and the people the comment reaches are told (which is what the undo
  map's "notified people" claim for this tool rests on);
- the approval card has nothing editable and the call cannot be undone, and both are declared.
"""

from __future__ import annotations

import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.chat.unified_models import Activity, ActivityType
from origin.search_engine.agent import approval_edits, undo
from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.tool_tiers import SINGLE_WRITE_TOOLS, disabled_tools_for_level
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import (
    FLAGGED_REGISTRY,
    app_write_tools,
    get_app_tool,
    get_mcp_tool,
)
from origin.search_engine.agent.tools.comment_on_note import COMMENT_ON_NOTE_INAPP
from origin.views.public.mcp import tools as mcp_tools

from .test_mcp_note_comment_start import CREATED, StartBase
from .test_mcp_note_thread_writes import EDITOR_ROLE

FLAG = "AGENT_INAPP_NOTE_COMMENT_TOOL"
NAME = "comment_on_note"


def flag(on: bool):
    return mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: on})


def declared():
    return {d.name: d for d in _build_tool_declarations()}


def declared_chars() -> int:
    return sum(
        len(d.name) + len(d.description) + len(json.dumps(d.parameters_schema, ensure_ascii=False))
        for d in _build_tool_declarations()
    )


class DarkWhileOffTests(SimpleTestCase):
    def test_the_flag_is_off_by_default_in_production(self):
        from apis import settings as production  # noqa: PLC0415

        self.assertIs(production.SEARCH_ENGINE[FLAG], False)

    def test_off_the_tool_is_not_declared_not_runnable_and_not_in_the_registry(self):
        with flag(False):
            self.assertNotIn(NAME, declared())
            self.assertIsNone(get_app_tool(NAME))
            self.assertNotIn(NAME, {t.name for t in app_write_tools()})
        self.assertNotIn(NAME, REGISTRY)
        self.assertIn(NAME, FLAGGED_REGISTRY)

    def test_it_is_filed_under_its_flag(self):
        flag_name, tool = FLAGGED_REGISTRY[NAME]
        self.assertEqual(flag_name, FLAG)
        self.assertIs(tool, COMMENT_ON_NOTE_INAPP)


class DeclaredWhenOnTests(SimpleTestCase):
    def test_on_it_is_declared_runnable_and_a_write(self):
        with flag(True):
            self.assertIn(NAME, declared())
            self.assertIs(get_app_tool(NAME), COMMENT_ON_NOTE_INAPP)
            self.assertIn(NAME, {t.name for t in app_write_tools()})
        self.assertTrue(COMMENT_ON_NOTE_INAPP.requires_approval)
        self.assertFalse(COMMENT_ON_NOTE_INAPP.mcp_only)

    def test_turning_it_on_adds_only_this_tool(self):
        with flag(False):
            before = [d.name for d in _build_tool_declarations()]
        with flag(True):
            after = [d.name for d in _build_tool_declarations()]
        self.assertEqual([n for n in after if n not in before], [NAME])
        self.assertEqual(
            [n for n in after if n != NAME], before, "everything else, in the same order"
        )

    def test_it_is_a_single_write_a_read_only_member_does_not_get(self):
        self.assertIn(NAME, SINGLE_WRITE_TOOLS)
        with flag(True):
            self.assertIn(NAME, disabled_tools_for_level("read"))
            self.assertNotIn(NAME, disabled_tools_for_level("act"))
            self.assertNotIn(NAME, disabled_tools_for_level("organize"))
        with flag(False):
            self.assertNotIn(NAME, disabled_tools_for_level("read"))

    def test_the_description_tells_the_model_what_the_neighbouring_tools_are_for(self):
        text = COMMENT_ON_NOTE_INAPP.description
        for needle in (
            "fetch_note",  # the read it needs first (AGENT_TOOL_SURFACE §6.6)
            "REQUIRES USER APPROVAL",
            "add_comment",  # a comment on a TASK
            "update_note",  # the note's own text
            "EXACTLY",
            "occurrence",
        ):
            self.assertIn(needle, text)
        self.assertLess(text.index("fetch_note"), text.index("occurrence"), "the read comes first")

    def test_it_takes_no_block_id_because_the_in_app_model_has_none(self):
        props = COMMENT_ON_NOTE_INAPP.parameters_schema["properties"]
        self.assertEqual(
            sorted(props), ["anchor_text", "note_id", "note_type", "occurrence", "text"]
        )
        self.assertEqual(
            COMMENT_ON_NOTE_INAPP.parameters_schema["required"],
            ["note_type", "note_id", "anchor_text", "text"],
        )

    def test_what_it_costs_the_prompt_when_on(self):
        with flag(False):
            off = declared_chars()
        with flag(True):
            on = declared_chars()
        # Measured at 1,3xx characters; a number to notice a casual doubling, not a budget (the prompt
        # budget decision belongs to the eval that flips this flag, see `apis/settings.py`).
        self.assertGreater(on - off, 900)
        self.assertLess(on - off, 1_700)


class MCPIsUntouchedTests(SimpleTestCase):
    def test_mcp_runs_its_own_tool_whatever_the_flag_says(self):
        for on in (False, True):
            with self.subTest(flag=on), flag(on):
                self.assertIs(get_mcp_tool(NAME), MCP_ONLY_REGISTRY[NAME])
                self.assertIsNot(get_mcp_tool(NAME), COMMENT_ON_NOTE_INAPP)

    def test_the_published_mcp_schema_and_description_do_not_move_with_the_flag(self):
        tool = next(t for t in mcp_tools.CATALOG if t.name == NAME)
        with flag(False):
            off = (mcp_tools.input_schema(tool), tool.description)
        with flag(True):
            on = (mcp_tools.input_schema(tool), tool.description)
        self.assertEqual(off, on)
        self.assertIn(
            "block_id", off[0]["properties"], "MCP still has the block id the app does not"
        )

    def test_the_mcp_tool_is_still_mcp_only(self):
        self.assertTrue(MCP_ONLY_REGISTRY[NAME].mcp_only)


class ApprovalDeclarationsTests(SimpleTestCase):
    def test_nothing_is_editable_on_the_card(self):
        self.assertEqual(approval_edits.EDITABLE_FIELDS[NAME], {})

    def test_it_cannot_be_undone_and_the_reason_is_the_people_it_tells(self):
        self.assertEqual(undo.UNDO_SUPPORT[NAME], undo.NONE)
        self.assertEqual(undo.UNDO_REASONS[NAME][0], undo.NOTIFIED_PEOPLE)


class RunningItTests(StartBase):
    """The twin runs the MCP tool's work. Collab is stubbed at `requests.post` as for the MCP tests."""

    def setUp(self):
        super().setUp()
        self._grant(self.user2, EDITOR_ROLE)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        patcher = flag(True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tool(self, **args):
        return COMMENT_ON_NOTE_INAPP.run(
            {
                "note_type": "personal",
                "note_id": self.note.note_id,
                "anchor_text": "180 days",
                "text": "Why 180?",
                **args,
            },
            self.ctx,
        )

    def test_it_asks_collab_as_the_caller_and_returns_the_thread(self):
        out = self.run_tool()
        self.assertEqual(out["thread_id"], CREATED["threadId"])
        self.assertEqual(self.sent["userId"], str(self.user.id))
        self.assertEqual(self.sent["documentName"], f"my-note:{self.note.note_id}")
        self.assertEqual(self.sent["anchorText"], "180 days")
        self.assertEqual(self.post.call_count, 1)

    def test_an_occurrence_goes_through(self):
        self.run_tool(occurrence=2)
        self.assertEqual(self.sent["occurrence"], 2)

    def test_a_block_id_the_model_invents_never_reaches_collab(self):
        self.run_tool(block_id="made-up-by-the-model")
        self.assertNotIn("blockId", self.sent)

    def test_a_refusal_is_a_sentence_for_the_model_and_nothing_is_posted(self):
        from origin.search_engine.agent.tools.base import ToolError  # noqa: PLC0415

        self.answer({"error": "x", "code": "anchor_not_found", "blockedBlocks": 0}, 404)
        with self.assertRaises(ToolError) as ctx:
            self.run_tool()
        self.assertIn("not found in the note", str(ctx.exception))
        self.assertEqual(Activity.objects.filter(recipient=self.user2, surface_type=6).count(), 0)

    def test_the_people_the_comment_reaches_are_told(self):
        # The undo map files this tool under "notified people": running it must show the notice.
        self.run_tool()
        rows = list(Activity.objects.filter(recipient=self.user2, surface_type=6))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].actor_id, self.user.id)
        self.assertEqual(rows[0].meta["noteId"], self.note.note_id)
        self.assertTrue(rows[0].meta["isComment"])

    def test_the_member_is_not_told_their_own_comment(self):
        self.run_tool()
        self.assertEqual(Activity.objects.filter(recipient=self.user, surface_type=6).count(), 0)
        # ...and the in-app agent makes no AI-app notice: its member answers a card for every write.
        self.assertEqual(
            Activity.objects.filter(recipient=self.user, activity_type=ActivityType.AI_APP).count(),
            0,
        )

    def test_a_note_the_member_cannot_read_is_not_found_and_collab_is_not_asked(self):
        from origin.search_engine.agent.tools.base import ToolError  # noqa: PLC0415

        stranger = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        from origin.models.note.common_note_models import NotePermissionMaster  # noqa: PLC0415

        NotePermissionMaster.objects.filter(user=self.user2).delete()
        with self.assertRaises(ToolError):
            COMMENT_ON_NOTE_INAPP.run(
                {
                    "note_type": "personal",
                    "note_id": self.note.note_id,
                    "anchor_text": "x",
                    "text": "y",
                },
                stranger,
            )
        self.post.assert_not_called()
