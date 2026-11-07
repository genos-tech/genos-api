"""Every published tool is well-formed, as a scan of `tools/list` would read it (MIP-1110).

OpenAI's plugin portal scans the server's tools before review and rejects malformed schemas.
There is no scanner to run locally, so this reads what a scan reads: a usable name, a
description, an `inputSchema` that is a well-formed object schema, and the three annotations as
booleans. It also fails on the one thing a client cannot recover from: a `required` argument
that the schema never declares.
"""

from __future__ import annotations

import json
import re

from django.test import SimpleTestCase

from origin.views.public.mcp import tools

NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
TYPES = {"string", "integer", "number", "boolean", "array", "object", "null"}


def schema_problems(node, path="$"):
    """What is wrong with one JSON-schema node, recursively."""
    if not isinstance(node, dict):
        return [f"{path}: a schema must be an object"]
    out = []
    kind = node.get("type")
    kinds = kind if isinstance(kind, list) else [kind] if kind is not None else []
    for k in kinds:
        if k not in TYPES:
            out.append(f"{path}: unknown type {k!r}")
    if "description" in node and not isinstance(node["description"], str):
        out.append(f"{path}: description must be a string")
    if "enum" in node and (not isinstance(node["enum"], list) or not node["enum"]):
        out.append(f"{path}: enum must be a non-empty list")
    props = node.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            out.append(f"{path}: properties must be an object")
            props = {}
        for name, sub in props.items():
            out += schema_problems(sub, f"{path}.{name}")
    required = node.get("required")
    if required is not None:
        if not isinstance(required, list) or not all(isinstance(r, str) for r in required):
            out.append(f"{path}: required must be a list of names")
        else:
            for r in required:
                if r not in (props or {}):
                    out.append(f"{path}: required {r!r} is not a declared property")
    if "array" in kinds:
        if "items" not in node:
            out.append(f"{path}: an array needs `items`")
        else:
            out += schema_problems(node["items"], f"{path}[]")
    extra = node.get("additionalProperties")
    if isinstance(extra, dict):
        out += schema_problems(extra, f"{path}.*")
    elif extra is not None and not isinstance(extra, bool):
        out.append(f"{path}: additionalProperties must be a boolean or a schema")
    return out


class TestEveryToolIsWellFormed(SimpleTestCase):
    def described(self):
        for kind in ("api_key", "oauth"):
            for tool in tools.CATALOG:
                yield kind, tools.describe(tool, kind)

    def test_names_are_usable_and_unique(self):
        names = [t.name for t in tools.CATALOG]
        self.assertEqual(len(names), len(set(names)), "duplicate tool name")
        for name in names:
            self.assertIsNotNone(NAME.fullmatch(name), name)

    def test_every_tool_has_a_description(self):
        for kind, tool in self.described():
            with self.subTest(tool=tool["name"], credential=kind):
                self.assertIsInstance(tool["description"], str)
                self.assertGreater(len(tool["description"].strip()), 20)

    def test_every_input_schema_is_a_well_formed_object_schema(self):
        for kind, tool in self.described():
            schema = tool["inputSchema"]
            with self.subTest(tool=tool["name"], credential=kind):
                self.assertEqual(schema.get("type"), "object")
                self.assertEqual(schema_problems(schema), [])

    def test_every_annotation_is_a_boolean(self):
        for kind, tool in self.described():
            with self.subTest(tool=tool["name"], credential=kind):
                self.assertEqual(
                    set(tool["annotations"]), {"readOnlyHint", "destructiveHint", "openWorldHint"}
                )
                self.assertTrue(all(isinstance(v, bool) for v in tool["annotations"].values()))

    def test_the_whole_listing_is_json(self):
        listed = [tools.describe(t) for t in tools.CATALOG]
        self.assertEqual(json.loads(json.dumps(listed)), listed)

    def test_the_checker_itself_catches_what_it_is_for(self):
        """A checker that passes everything is decoration."""
        bad = {
            "type": "object",
            "properties": {"a": {"type": "wat"}, "b": {"type": "array"}, "c": {"enum": []}},
            "required": ["missing"],
        }
        problems = " | ".join(schema_problems(bad))
        for needle in ("unknown type", "needs `items`", "non-empty list", "not a declared"):
            self.assertIn(needle, problems)
