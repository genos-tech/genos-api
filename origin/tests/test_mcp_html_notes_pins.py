"""What a connected AI app is sent for HTML notes, pinned (MIP-2214).

The MCP tool list and the prompts are model-visible: a client's model reads each tool's name,
description and schema to choose a tool and write its arguments, and `make_html_note` hands it a
block of instructions. The words of `save_html_note` carry the generation guide, so a change to
them changes the pages a model writes. Each is pinned as a sha256 in the one golden file
(`origin/tests/golden/agent_pins.json`; re-record with `python manage.py record_agent_goldens`, see
`origin/services/agent_pins.py`), and `git diff origin/tests/golden` shows the change a reviewer owes
the grouped paid run for.

Pinned, with `HTML_NOTES` ON:

  * the declaration of `save_html_note`, exactly as `tools/list` sends it;
  * the declaration of `get_note` (it gains `include_html` and one closing sentence);
  * the declarations `update_note` and `edit_note_block` keep (they are NOT changed: the guard is in
    what they answer, not in what they declare);
  * the `make_html_note` prompt for a key that can save and for one that cannot;
  * what a guarded `update_note` / `edit_note_block` answers.

Pinned, with the switch OFF (the default): `get_note`, `update_note`, `edit_note_block` and
`create_note` as a client sees them, which must equal what they were before this change. That is also
checked by assertion against the words the switch adds. No database is needed.
"""

from __future__ import annotations

import hashlib
import json

from django.test import SimpleTestCase, override_settings

from origin.models.common.api_key_models import SCOPE_WRITE
from origin.search_engine.agent.tools.base import ToolError
from origin.search_engine.agent.tools.html_note_guard import refuse_html_text_edit
from origin.services import agent_pins
from origin.views.public.mcp import prompts, tools

PINS = agent_pins.Pins(__name__)

ALWAYS = ("get_note", "update_note", "edit_note_block", "create_note")


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class _Html:
    body_format = "html"


class DeclarationPins(SimpleTestCase):
    """The tools as a client's `tools/list` shows them, with the switch on."""

    @override_settings(HTML_NOTES=True)
    def test_save_html_note(self):
        PINS.check(
            self,
            "declaration:save_html_note",
            _digest(tools.describe(tools.BY_NAME["save_html_note"])),
        )

    @override_settings(HTML_NOTES=True)
    def test_the_note_tools_with_the_switch_on(self):
        for name in ALWAYS:
            with self.subTest(tool=name):
                PINS.check(
                    self, f"declaration:{name}:on", _digest(tools.describe(tools.BY_NAME[name]))
                )

    @override_settings(HTML_NOTES=True)
    def test_only_get_note_changes_its_declaration_when_the_switch_goes_on(self):
        on = {n: tools.describe(tools.BY_NAME[n]) for n in ALWAYS}
        with override_settings(HTML_NOTES=False):
            off = {n: tools.describe(tools.BY_NAME[n]) for n in ALWAYS}
        for name in ALWAYS:
            with self.subTest(tool=name):
                if name == "get_note":
                    self.assertNotEqual(on[name], off[name])
                else:
                    self.assertEqual(on[name], off[name])

    @override_settings(HTML_NOTES=True)
    def test_the_published_description_carries_the_whole_generation_guide(self):
        from origin.views.public.mcp.html_notes.guide import GENERATION_RULES

        text = tools.describe(tools.BY_NAME["save_html_note"])["description"]
        self.assertEqual(len(GENERATION_RULES), 10)
        for number, rule in enumerate(GENERATION_RULES, start=1):
            self.assertIn(f"{number}. {rule}", text)


class SwitchOffPins(SimpleTestCase):
    """With the switch off a client is offered what it was."""

    @override_settings(HTML_NOTES=False)
    def test_the_note_tools_as_they_were(self):
        for name in ALWAYS:
            with self.subTest(tool=name):
                PINS.check(
                    self, f"declaration:{name}:off", _digest(tools.describe(tools.BY_NAME[name]))
                )

    @override_settings(HTML_NOTES=False)
    def test_nothing_of_html_notes_is_in_what_a_client_can_read(self):
        listed = tools.visible_tools(SCOPE_WRITE, allow_destructive=True)
        blob = json.dumps([tools.describe(t) for t in listed]) + json.dumps(prompts.list_prompts())
        for word in (
            "save_html_note",
            "include_html",
            "body_format",
            "make_html_note",
            "HTML note",
        ):
            with self.subTest(word=word):
                self.assertNotIn(word, blob)


class PromptPins(SimpleTestCase):
    @override_settings(HTML_NOTES=True)
    def test_the_listing(self):
        PINS.check(self, "prompts:list", _digest(prompts.list_prompts()))

    @override_settings(HTML_NOTES=True)
    def test_the_text_for_a_key_that_can_save(self):
        out = prompts.get_prompt(
            "make_html_note", {"request": "a burndown chart"}, frozenset({"save_html_note"})
        )
        PINS.check(self, "prompt:make_html_note:can_save", _digest(out))

    @override_settings(HTML_NOTES=True)
    def test_the_text_for_a_key_that_cannot(self):
        out = prompts.get_prompt("make_html_note", {"request": "a burndown chart"}, frozenset())
        PINS.check(self, "prompt:make_html_note:read_only", _digest(out))


class GuardPins(SimpleTestCase):
    def test_what_a_guarded_edit_answers(self):
        with self.assertRaises(ToolError) as caught:
            refuse_html_text_edit(_Html(), 42)
        PINS.check(self, "guard:html_text_edit", _digest(str(caught.exception)))

    def test_an_ordinary_note_passes_the_guard(self):
        class Plain:
            body_format = "blocknote"

        self.assertIsNone(refuse_html_text_edit(Plain(), 1))
        self.assertIsNone(refuse_html_text_edit(object(), 1))
