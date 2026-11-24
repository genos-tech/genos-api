"""What a connected AI app is sent for Genos Skills, pinned (MIP-2046).

The MCP tool list is model-visible: a client's model reads each tool's name, description and schema
to choose a tool, and `run_skill` hands it a block of instructions to follow. A change to any of
those words changes what that model does, so each is pinned here as a sha256 in the one golden file
(`origin/tests/golden/agent_pins.json`; re-record with `python manage.py record_agent_goldens`, see
`origin/services/agent_pins.py`) and `git diff origin/tests/golden` shows the change a reviewer owes
the grouped paid run for.

Two things are pinned:

  * the published declaration of each of the three tools, exactly as `tools/list` sends it;
  * the instructions `run_skill` returns, for a fixed skill, in each of its four shapes (the member's
    own read skill, an act skill, a teammate's skill, an outside author's skill).

The switch is pinned by assertion, not by hash: with `GENOS_SKILLS` off none of the three is in any
list a client can ask for, so the catalog it sees is what it was before this change.

These tests need no database: the instructions are built from a hand-made `ResolvedSkill`.
"""

from __future__ import annotations

import hashlib
import json

from django.test import SimpleTestCase, override_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.services import agent_pins
from origin.services.skills import mcp as skills_mcp
from origin.services.skills import run
from origin.tests.skill_fence_helpers import strip_tag
from origin.views.public.mcp import surfaces, tools

PINS = agent_pins.Pins(__name__)

SKILL_TOOLS = ("list_my_skills", "get_skill", "run_skill")

BODY = "# Weekly report\n\n## Steps\n1. Read the open tasks of the project.\n2. Write three lines."


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _resolved(author: str, level: str, *, draft: bool = False) -> run.ResolvedSkill:
    return run.ResolvedSkill(
        skill_id="00000000-0000-4000-8000-000000000001",
        team_id="t",
        slug="report",
        version_no=0 if draft else 3,
        draft=draft,
        author=author,
        tool_level=level,
        body_markdown=BODY,
    )


class DeclarationPins(SimpleTestCase):
    """The three tools as a client's `tools/list` shows them, with the switch on."""

    @override_settings(GENOS_SKILLS=True)
    def test_each_declaration(self):
        for name in SKILL_TOOLS:
            with self.subTest(tool=name):
                PINS.check(
                    self, f"declaration:{name}", _digest(tools.describe(tools.BY_NAME[name]))
                )

    @override_settings(GENOS_SKILLS=True)
    def test_the_skills_endpoint_lists_the_three_in_this_order(self):
        listed = [t.name for t in tools.visible_tools(SCOPE_WRITE, surface="skills")]
        self.assertEqual(listed, list(SKILL_TOOLS))
        PINS.check(self, "skills_surface:names", _digest(listed))


class SwitchOffPins(SimpleTestCase):
    """With the switch off (the default) a client is offered what it was: none of the three."""

    def test_no_list_a_client_can_ask_for_has_them(self):
        for surface in (None, "core", *surfaces.DOMAIN_TOOLS):
            with self.subTest(surface=surface):
                names = {t.name for t in tools.visible_tools(SCOPE_WRITE, surface=surface)}
                self.assertEqual(names & set(SKILL_TOOLS), set())
        for name in SKILL_TOOLS:
            self.assertFalse(tools.published(tools.BY_NAME[name]), name)


class InstructionPins(SimpleTestCase):
    """What `run_skill` tells the client's model to do, for each shape of skill."""

    def _instructions(self, author, level, **kw) -> str:
        # The fence tag is `skill_text_` + 8 random hex digits per run (MIP-2192): the pin is of the
        # words around it, with `skill_text` standing in for the tag, so it stays one value.
        return strip_tag(
            skills_mcp.build_instructions(_resolved(author, level, **kw), "for project WRD")
        )

    def test_the_members_own_read_skill(self):
        PINS.check(
            self,
            "instructions:you:read",
            _digest(self._instructions(run.AUTHOR_YOU, "read", draft=True)),
        )

    def test_the_members_own_act_skill(self):
        PINS.check(
            self,
            "instructions:you:act",
            _digest(self._instructions(run.AUTHOR_YOU, "act", draft=True)),
        )

    def test_a_teammates_act_skill(self):
        PINS.check(
            self,
            "instructions:teammate:act",
            _digest(self._instructions(run.AUTHOR_TEAMMATE, "act")),
        )

    def test_an_outside_authors_skill_may_only_propose_a_task_note_or_todo(self):
        text = self._instructions(run.AUTHOR_OUTSIDE, "act")
        self.assertNotIn("may only read", text)
        self.assertIn("a new task, note or to-do", text)
        PINS.check(self, "instructions:outside:act", _digest(text))

    def test_an_outside_authors_read_skill_is_not_given_a_licence_to_create(self):
        text = self._instructions(run.AUTHOR_OUTSIDE, "read")
        self.assertIn("may only read", text)
        self.assertNotIn("a new task, note or to-do", text)
        PINS.check(self, "instructions:outside:read", _digest(text))
