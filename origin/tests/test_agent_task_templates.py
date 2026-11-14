"""The server's copy of the app's built-in task templates (MIP-1561).

The templates live in the browser (`genos-frontend` `src/features/tasks/utils/taskTemplates.ts`).
The server keeps only their section HEADINGS (`plan_task_bodies.BUILT_IN_TEMPLATES`), so a task made
through the agent or MCP with `template: "bug"` has the sections a person picking "Bug report" in the
app gets. This is a mirror of client text, so it is pinned in both directions: this file spells the
four templates out (a change to either side is a visible change here), and the system prompt's own
list of house headings, which the model is told to write, must be a subset of them.

No database, no model.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT
from origin.search_engine.agent.tools.plan_task_bodies import (
    BUILT_IN_HEADINGS,
    BUILT_IN_TEMPLATES,
    _built_in_id,
    built_in_body,
)

# `TASK_TEMPLATES` in taskTemplates.ts: id, label, the heading of each section in order.
FRONTEND = {
    "default": (
        "Standard task",
        ["🧾 Summary", "🪜 Motivation", "✅ Acceptance criteria", "🎯 Notes & links"],
    ),
    "bug": (
        "Bug report",
        [
            "🐞 Summary",
            "🔁 Steps to reproduce",
            "🎯 Expected behavior",
            "💥 Actual behavior",
            "🧪 Environment",
        ],
    ),
    "spike": (
        "Research / spike",
        [
            "❓ Question",
            "💡 Hypothesis",
            "🧭 Approach",
            "⏱ Timebox",
            "📌 Findings",
            "🚧 Out of scope",
        ],
    ),
    "milestone": (
        "Milestone",
        [
            "🎯 Goal",
            "✅ Success criteria",
            "📦 In scope",
            "🚫 Out of scope",
            "⚠️ Risks & dependencies",
            "🧭 Order of work",
        ],
    ),
}


class BuiltInTemplateMirrorTests(SimpleTestCase):
    def test_the_four_templates_are_the_frontends_ids_labels_and_headings(self):
        self.assertEqual(
            {tid: (label, list(headings)) for tid, (label, headings) in BUILT_IN_TEMPLATES.items()},
            FRONTEND,
        )

    def test_headings_are_unique_within_a_template_and_never_blank(self):
        for tid, headings in BUILT_IN_HEADINGS.items():
            with self.subTest(template=tid):
                self.assertEqual(len(headings), len(set(headings)))
                self.assertTrue(all(h.strip() for h in headings))

    def test_the_prompts_house_headings_for_tasks_are_in_the_matching_template(self):
        # The prompt tells the model to write a plan task's body with these sections.
        for heading in ("🧾 Summary", "🪜 Motivation", "✅ Acceptance criteria"):
            self.assertIn(f'"### {heading}"', AGENT_SYSTEM_PROMPT)
            self.assertIn(heading, BUILT_IN_HEADINGS["default"])
        for heading in (
            "🐞 Summary",
            "🔁 Steps to reproduce",
            "🎯 Expected behavior",
            "💥 Actual behavior",
        ):
            self.assertIn(f'"### {heading}"', AGENT_SYSTEM_PROMPT)
            self.assertIn(heading, BUILT_IN_HEADINGS["bug"])

    def test_a_name_is_an_id_or_a_label_in_any_case_and_nothing_else(self):
        self.assertEqual(_built_in_id("BUG"), "bug")
        self.assertEqual(_built_in_id(" research / SPIKE "), "spike")
        self.assertEqual(_built_in_id("standard task"), "default")
        self.assertIsNone(_built_in_id("bugs"))
        self.assertIsNone(_built_in_id("custom:3"))

    def test_the_body_is_the_headings_with_the_title_under_the_first(self):
        self.assertEqual(
            built_in_body("default", "Ship it"),
            "### 🧾 Summary\n\nShip it\n\n### 🪜 Motivation\n\n### ✅ Acceptance criteria\n\n"
            "### 🎯 Notes & links",
        )
