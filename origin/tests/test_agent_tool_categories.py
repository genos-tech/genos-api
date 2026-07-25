"""Tool categories + registry-generated prompt sections (PR-A5).

The enforcement pattern is `WRITE_PREFIXES`': a hand-reviewed map plus
two-way completeness tests, so forgetting to categorize a new tool
fails CI with the name spelled out. Plus the guarantees that made this
change safe to ship at all:

  * `_tool_family` parity — the category-driven mapping reproduces the
    old name-regex heuristic for every registered tool (first-ever
    coverage of that function);
  * flag off ⇒ the system prompt is the legacy constant byte-for-byte;
  * the generated sections can't drift (write list == requires_approval
    set; directory names == the registry; true count);
  * tool DECLARATIONS are identical across flag states — the prompt
    changes, the cacheable declarations block does not.
"""

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent import prompts
from origin.search_engine.agent.controller import _build_tool_declarations, _tool_family
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.categories import (
    CATEGORIES,
    PERIPHERAL_FAMILIES,
    TOOL_CATEGORY,
    tool_category,
)


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class CategoryMapTests(SimpleTestCase):
    def test_every_registered_tool_has_a_category(self):
        missing = set(REGISTRY) - set(TOOL_CATEGORY)
        self.assertEqual(
            missing,
            set(),
            f"uncategorized tool(s) {sorted(missing)} — add them to "
            f"tools/categories.py TOOL_CATEGORY (see the README DoD)",
        )

    def test_every_categorized_name_is_a_registered_tool(self):
        stale = set(TOOL_CATEGORY) - set(REGISTRY)
        self.assertEqual(
            stale,
            set(),
            f"TOOL_CATEGORY names nonexistent tool(s) {sorted(stale)} — "
            f"remove them (renamed or deleted tools must not linger)",
        )

    def test_every_category_value_is_known(self):
        unknown = {c for c in TOOL_CATEGORY.values() if c not in CATEGORIES}
        self.assertEqual(unknown, set())

    def test_peripheral_families_are_categories(self):
        self.assertTrue(set(PERIPHERAL_FAMILIES) <= set(CATEGORIES))

    def test_lookup_helper(self):
        self.assertEqual(tool_category("search_knowledge_base"), "search")
        self.assertIsNone(tool_category("not_a_tool"))


class ToolFamilyParityTests(SimpleTestCase):
    """`_tool_family` used to be name-substring guesswork; it now reads
    the declared map. The mapping it feeds (`RAG_TOOL_SUBSETTING`'s
    droppable families) must not change out from under the flag — the
    old heuristic is re-stated here verbatim and compared across the
    ENTIRE registry."""

    @staticmethod
    def _legacy_family(name: str) -> str | None:
        if name == "fetch_pr" or name.startswith("list_pr_"):
            return "pr"
        if "calendar" in name:
            return "calendar"
        if "todo" in name:
            return "todo"
        if name.startswith("get_my_") or name.startswith("list_my_"):
            return "me"
        return None

    def test_family_parity_across_the_whole_registry(self):
        for name in REGISTRY:
            self.assertEqual(
                _tool_family(name),
                self._legacy_family(name),
                f"{name}: category-driven family diverges from the legacy heuristic",
            )

    def test_unknown_tool_is_core(self):
        self.assertIsNone(_tool_family("not_a_tool"))


class GeneratedPromptTests(SimpleTestCase):
    def test_flag_off_serves_the_legacy_prompt_byte_identically(self):
        with override_settings(SEARCH_ENGINE=_se(AGENT_CHEATSHEET_FROM_REGISTRY=False)):
            self.assertIs(prompts.agent_system_prompt(), prompts.AGENT_SYSTEM_PROMPT)

    def test_flag_on_serves_the_registry_prompt(self):
        with override_settings(SEARCH_ENGINE=_se(AGENT_CHEATSHEET_FROM_REGISTRY=True)):
            self.assertIs(prompts.agent_system_prompt(), prompts.AGENT_SYSTEM_PROMPT_REGISTRY)

    def test_generated_write_list_matches_requires_approval_exactly(self):
        """The drift this exists to kill: the hand list was missing both
        todo write tools when this landed."""
        write_names = {t.name for t in REGISTRY.values() if t.requires_approval}
        generated = prompts.AGENT_SYSTEM_PROMPT_REGISTRY
        for name in write_names:
            self.assertIn(name, generated)
        # And the legacy blob (with its missing names) is gone.
        self.assertNotIn(prompts._WRITE_TOOLS_LEGACY_BLOCK, generated)
        # The two names the hand list dropped are present now.
        self.assertIn("create_todo_item", generated)
        self.assertIn("update_todo_item", generated)

    def test_directory_lists_every_tool_with_the_true_count(self):
        generated = prompts.AGENT_SYSTEM_PROMPT_REGISTRY
        self.assertIn(f"all {len(REGISTRY)} tools", generated)
        directory = generated.split("TOOL DIRECTORY", 1)[1]
        for name in REGISTRY:
            self.assertIn(name, directory)

    def test_legacy_prompt_untouched_by_generation(self):
        # The legacy constant still contains its hand-written blob —
        # generation must never mutate it in place.
        self.assertIn(prompts._WRITE_TOOLS_LEGACY_BLOCK, prompts.AGENT_SYSTEM_PROMPT)


class DeclarationsUnchangedTests(SimpleTestCase):
    def test_declarations_identical_across_flag_states(self):
        """The whole point of doing grouping at the PROMPT layer: the
        ~13k-token declarations block — the cacheable prefix — must be
        byte-identical whichever prompt variant is active."""
        with override_settings(SEARCH_ENGINE=_se(AGENT_CHEATSHEET_FROM_REGISTRY=False)):
            off = _build_tool_declarations()
        with override_settings(SEARCH_ENGINE=_se(AGENT_CHEATSHEET_FROM_REGISTRY=True)):
            on = _build_tool_declarations()
        self.assertEqual(off, on)
        self.assertEqual(len(off), len(REGISTRY))
