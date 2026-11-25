"""The dashboard eval cases are well formed and match the tools they test (MIP-2361).

The cases (`evals/group/mcp/t2361-mcp-dashboards.yaml`) are WRITTEN, NOT RUN: Ken decided there is no
paid eval. This file is the free part: it keeps them from rotting by reading them against the real
catalog, the way `test_agent_eval_group_cases` does for the in-app cases. No model is called.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.evals.group import harness
from origin.services.schedule_jobs import JOBS
from origin.views.public.mcp import tools

CASES = Path(harness.__file__).parent / "mcp" / "t2361-mcp-dashboards.yaml"
CASE_KEYS = {"id", "lang", "tags", "plant", "note", "query", "expect", "requires", "plants"}
EXPECT_KEYS = {
    "proposed_write",
    "no_writes",
    "tool_calls_at_most",
    "answer_contains_any",
    "answer_does_not_match",
    "answer_does_not_contain",
    "answer_chars_at_least",
}
WRITE_KEYS = {"tool", "args_match", "args_absent"}


def load() -> list[dict]:
    return yaml.safe_load(CASES.read_text(encoding="utf-8"))


def catalog() -> dict[str, tools.McpTool]:
    with override_settings(GENOS_SKILLS=True, HTML_NOTES=True):
        return {
            t.name: t for t in tools.visible_tools("write", allow_destructive=True, surface=None)
        }


class TestTheCaseFile(SimpleTestCase):
    def test_every_case_is_well_formed(self):
        cases = load()
        ids = [c["id"] for c in cases]
        self.assertEqual(len(ids), len(set(ids)), "duplicate id")
        for case in cases:
            where = case["id"]
            self.assertFalse(set(case) - CASE_KEYS, f"{where}: unknown keys")
            self.assertTrue(where.startswith("t2361."))
            self.assertIn("t2361", case["tags"])
            self.assertIn(case["lang"], {"en", "ja"})
            self.assertTrue(case["query"].strip())
            self.assertFalse(set(case["expect"]) - EXPECT_KEYS, f"{where}: unknown expect keys")
            write = case["expect"].get("proposed_write")
            if write:
                self.assertFalse(set(write) - WRITE_KEYS, f"{where}: unknown proposed_write keys")
                for pattern in (write.get("args_match") or {}).values():
                    re.compile(pattern)
            for pattern in case["expect"].get("answer_does_not_match", []):
                re.compile(pattern)

    def test_the_main_cases_come_in_english_and_japanese(self):
        by_stem: dict[str, set[str]] = {}
        for case in load():
            stem, lang = case["id"].rsplit(".", 1)
            by_stem.setdefault(stem, set()).add(lang)
        for stem in ("t2361.create-mine", "t2361.create-project"):
            self.assertEqual(by_stem[stem], {"en", "ja"}, stem)

    def test_each_kind_of_dashboard_and_each_guard_has_a_case(self):
        text = CASES.read_text(encoding="utf-8")
        for kind in ("mine", "project", "milestone"):
            self.assertIn(f'params.kind: "^{kind}$"', text)
        for tag in ("kind-chat", "kind-refuse", "kind-credits", "kind-injection"):
            self.assertIn(tag, text)

    def test_the_file_is_outside_the_in_app_glob(self):
        # `harness.load_cases` globs `group/*.yaml` and would run these through the in-app agent.
        self.assertNotEqual(CASES.parent, Path(harness.__file__).parent)
        self.assertFalse(CASES.parent.joinpath("..", CASES.name).resolve().exists())


class TestTheCasesMatchTheTools(SimpleTestCase):
    def test_a_proposed_write_names_a_real_write_tool_and_real_arguments(self):
        cat = catalog()
        schema = JOBS["ai_dashboard"].params_schema["properties"]
        for case in load():
            write = case["expect"].get("proposed_write")
            if not write:
                continue
            where = case["id"]
            tool = cat.get(write["tool"])
            self.assertIsNotNone(tool, f"{where}: no tool {write['tool']}")
            self.assertTrue(tool.write, f"{where}: {write['tool']} is not a write tool")
            top = set(tools.describe(tool)["inputSchema"]["properties"])
            keys = list((write.get("args_match") or {})) + list(write.get("args_absent") or [])
            for key in keys:
                if key.startswith("params."):
                    self.assertIn(key.split(".", 1)[1], schema, f"{where}: {key}")
                else:
                    self.assertIn(key, top, f"{where}: {key} is not an argument of the tool")

    def test_the_expected_values_are_allowed_by_the_job_schema(self):
        schema = JOBS["ai_dashboard"].params_schema["properties"]
        for case in load():
            write = case["expect"].get("proposed_write") or {}
            for key, pattern in (write.get("args_match") or {}).items():
                if key == "job":
                    self.assertIn("ai_dashboard", re.sub(r"[\^$]", "", pattern))
                if not key.startswith("params."):
                    continue
                allowed = schema[key.split(".", 1)[1]].get("enum")
                if allowed:
                    self.assertTrue(
                        any(re.search(pattern, value) for value in allowed),
                        f"{case['id']}: {pattern} matches none of {allowed}",
                    )

    def test_the_frequencies_asked_for_exist(self):
        from origin.models.common.schedule_models import FREQUENCY_CHOICES  # noqa: PLC0415

        known = [value for value, _label in FREQUENCY_CHOICES]
        for case in load():
            pattern = ((case["expect"].get("proposed_write") or {}).get("args_match") or {}).get(
                "frequency"
            )
            if pattern:
                self.assertTrue(any(re.search(pattern, f) for f in known), case["id"])

    def test_no_case_waits_for_another_task(self):
        # The chat and place arguments are on the tools (MIP-2358): every case is checked against them.
        self.assertEqual({c["id"] for c in load() if c.get("requires")}, set())
