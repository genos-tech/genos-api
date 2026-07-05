"""Generic contract tests over the agent tool REGISTRY.

Change-safety gate for new tools (genos-docs
`spotlight/SPOTLIGHT_AGENT_CHANGE_SAFETY.md` §4.1): every tool — current
and future — is covered automatically by iterating `REGISTRY`, so adding
a tool requires no new authoring here. Per-tool ACL / behavior tests
still live in their own files; the per-tool Definition of Done is
`origin/search_engine/agent/tools/README.md`.

Two deterministic gates:

  * Structural — the declaration is well-formed: registry key matches
    the tool name, the name is a lowercase identifier (Gemini
    function-calling rejects anything else), the description is
    non-empty (the model picks tools BY the description), and the
    parameters schema is a Gemini-style JSON Schema whose `required`
    entries actually exist in `properties`. A malformed schema otherwise
    only surfaces at request time against the live model.

  * Write-flag — write-shaped tools (`create_` / `update_` / `delete_` /
    `assign_` / `add_`) MUST set `requires_approval=True`. The
    controller's pause/approve protocol is keyed on that single flag
    ("Phase 7: write tools pause the loop" in controller.py), so a
    mis-flagged write tool executes WITHOUT user approval — silent data
    mutation, the highest-blast-radius mistake a new tool can ship with.

No DB, no LLM, no network — pure declaration checks, safe to hard-gate.
"""

import re

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import REGISTRY

# Verb prefixes that mark a tool as write-shaped. A new write VERB (e.g.
# `send_`, `mark_`) must be added here — test_write_prefixes_are_complete
# fails until it is, so the vocabulary can't silently rot.
WRITE_PREFIXES = ("create_", "update_", "delete_", "assign_", "add_")

# Schema `type` values both provider adapters accept. Tools are authored
# in Gemini's UPPERCASE form; `claude_client._normalize_schema` maps
# UPPERCASE → lowercase and passes standard lowercase through, so both
# spellings are wire-safe. Anything else (a typo like "STIRNG") only
# blows up at request time against the live model.
SCHEMA_TYPES = {
    "OBJECT",
    "STRING",
    "INTEGER",
    "NUMBER",
    "BOOLEAN",
    "ARRAY",
    "NULL",
    "object",
    "string",
    "integer",
    "number",
    "boolean",
    "array",
    "null",
}

TOOL_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\Z")


def _walk(node, path="$"):
    """Yield (path, dict) for every dict node in a schema tree."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _walk(value, f"{path}[{i}]")


class ToolRegistryContractTests(SimpleTestCase):
    def test_registry_is_populated(self):
        """Guard the guards: the per-tool loops below pass vacuously on an
        empty REGISTRY, so a broken import chain in tools/__init__.py must
        fail here first. Floor sits safely under the current 53 tools."""
        self.assertGreater(len(REGISTRY), 40, "REGISTRY suspiciously small — import wiring broken?")

    def test_declarations_are_well_formed(self):
        for name, tool in sorted(REGISTRY.items()):
            with self.subTest(tool=name):
                self.assertEqual(name, tool.name, "REGISTRY key must equal tool.name")
                self.assertRegex(
                    tool.name,
                    TOOL_NAME_RE,
                    "tool names must be lowercase snake_case identifiers "
                    "(Gemini function-calling rejects anything else)",
                )
                self.assertIsInstance(tool.description, str)
                self.assertTrue(
                    tool.description.strip(),
                    "description is empty — the model chooses tools BY the "
                    "description; say WHEN to use it, not just what it does",
                )
                self.assertIsInstance(tool.parameters_schema, dict)
                self.assertIn(
                    tool.parameters_schema.get("type"),
                    ("OBJECT", "object"),
                    "top-level parameters_schema must be an object schema",
                )
                self.assertIsInstance(tool.requires_approval, bool)
                self.assertTrue(callable(tool.run))

    def test_schema_types_and_required_are_valid(self):
        for name, tool in sorted(REGISTRY.items()):
            with self.subTest(tool=name):
                for path, node in _walk(tool.parameters_schema):
                    type_value = node.get("type")
                    if isinstance(type_value, str):
                        self.assertIn(
                            type_value,
                            SCHEMA_TYPES,
                            f"{name}: unknown schema type {type_value!r} at {path}",
                        )
                    if "required" in node:
                        required = node["required"]
                        self.assertIsInstance(required, list, f"{name}: `required` at {path}")
                        properties = node.get("properties")
                        self.assertIsInstance(
                            properties,
                            dict,
                            f"{name}: `required` without `properties` at {path}",
                        )
                        missing = set(required) - set(properties)
                        self.assertFalse(
                            missing,
                            f"{name}: `required` names undeclared properties "
                            f"{sorted(missing)} at {path}",
                        )

    def test_write_shaped_tools_require_approval(self):
        """THE silent-corruption guard. The controller auto-executes any
        tool with requires_approval=False; for a write tool that means
        data mutation with no user approval step. If a write-shaped tool
        is genuinely read-only, rename it — don't weaken this test."""
        for name, tool in sorted(REGISTRY.items()):
            if not name.startswith(WRITE_PREFIXES):
                continue
            with self.subTest(tool=name):
                self.assertTrue(
                    tool.requires_approval,
                    f"{name} is write-shaped (prefix) but has "
                    "requires_approval=False — it would mutate data without "
                    "the pause/approve step. Set requires_approval=True.",
                )

    def test_write_prefixes_are_complete(self):
        """Keep WRITE_PREFIXES an exhaustive vocabulary: every tool the
        author flagged requires_approval=True must match a known write
        prefix. When a new write verb appears (send_, mark_, ...), add it
        to WRITE_PREFIXES so future tools using that verb are guarded by
        test_write_shaped_tools_require_approval too."""
        unmatched = sorted(
            name
            for name, tool in REGISTRY.items()
            if tool.requires_approval and not name.startswith(WRITE_PREFIXES)
        )
        self.assertFalse(
            unmatched,
            f"approval-required tools with an unknown write verb: {unmatched} "
            "— add the verb prefix to WRITE_PREFIXES in this file",
        )
