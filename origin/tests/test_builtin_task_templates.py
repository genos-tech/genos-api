"""The built-in task templates have ONE source file (MIP-1606).

`origin/search_engine/agent/tools/builtin_task_templates.json` lists the four templates the
app offers (id, English label, sections with their grey prompts). The browser builds its
BlockNote blocks from a vendored copy of the same file (`genos-frontend`
`src/features/tasks/utils/builtinTaskTemplates.json`); this side derives from it what the agent
needs: the labels and section headings (`plan_task_bodies.BUILT_IN_TEMPLATES`) and the set of
prompt texts a reader skips (`template_prompts.PROMPTS`). Before this file both were typed out
by hand next to the browser's constants.

The expectations below are LITERALS captured from the hand-written lists before the move, on
purpose: a test that read them from the JSON would agree with whatever the JSON said. Nothing
here touches a database or a model.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import builtin_templates, template_prompts
from origin.search_engine.agent.tools.plan_task_bodies import (
    BUILT_IN_HEADINGS,
    BUILT_IN_TEMPLATES,
    built_in_body,
)

# What `built_in_body(<id>, "T")` returned before the move.
BODY_OF_T = {
    "default": "### 🧾 Summary\n\nT\n\n### 🪜 Motivation\n\n### ✅ Acceptance criteria\n\n### 🎯 Notes & links",
    "bug": "### 🐞 Summary\n\nT\n\n### 🔁 Steps to reproduce\n\n### 🎯 Expected behavior\n\n### 💥 Actual behavior\n\n### 🧪 Environment",
    "spike": "### ❓ Question\n\nT\n\n### 💡 Hypothesis\n\n### 🧭 Approach\n\n### ⏱ Timebox\n\n### 📌 Findings\n\n### 🚧 Out of scope",
    "milestone": "### 🎯 Goal\n\nT\n\n### ✅ Success criteria\n\n### 📦 In scope\n\n### 🚫 Out of scope\n\n### ⚠️ Risks & dependencies\n\n### 🧭 Order of work",
}

# `template_prompts.PROMPTS` before the move: the text of every grey prompt in the four templates.
PROMPTS_BEFORE = frozenset(
    {
        "Anything else worth pinning here.",
        "Browser, OS, app version, user, team, anything that narrows it down.",
        "Click on …",
        "Experiments / prototypes to try…",
        "Explicitly things we are NOT answering here.",
        "Fill in as you learn — link out to docs, PRs, threads.",
        "First condition that must be true when this is done.",
        "Gates, when each parent task closes, and the critical path.",
        "Go to …",
        "How long are we willing to spend on this?",
        "Measurable signal #1 that we hit the goal.",
        "Measurable signal #2.",
        "Measurable signal #3.",
        "Observe that …",
        "One or two lines on what this task delivers.",
        "One-line description of the bug.",
        "People to talk to…",
        "Second condition…",
        "Step 1: the first task.",
        "Step 2: what it unblocks. Note tasks that can run in parallel.",
        "Thing we are explicitly NOT doing.",
        "Third condition…",
        "What actually happens? Include error messages, screenshots.",
        "What are we trying to learn or decide?",
        "What could derail this? Who/what are we waiting on?",
        "What do we currently believe is true?",
        "What outcome does this milestone deliver, and for whom?",
        "What should happen?",
        "Where to look first…",
        "Which task do we take first, and what comes next?",
        "Why does this matter? What problem are we solving?",
        "Workstream / feature 1",
        "Workstream / feature 2",
    }
)


class DerivedFromTheSharedFileTests(SimpleTestCase):
    """What the agent reads is what it read before the move."""

    def test_the_templates_keep_their_order(self):
        # The tool error lists them in this order and the app's picker shows them in it.
        self.assertEqual(list(BUILT_IN_TEMPLATES), ["default", "bug", "spike", "milestone"])
        self.assertEqual(list(BUILT_IN_HEADINGS), ["default", "bug", "spike", "milestone"])

    def test_a_template_body_is_what_it_was(self):
        for tid, expected in BODY_OF_T.items():
            with self.subTest(template=tid):
                self.assertEqual(built_in_body(tid, "T"), expected)

    def test_the_prompts_a_reader_skips_are_what_they_were(self):
        self.assertEqual(template_prompts.PROMPTS, PROMPTS_BEFORE)
        self.assertIsInstance(template_prompts.PROMPTS, frozenset)


# The hash of the parsed content of `builtin_task_templates.json`. The SAME value is pinned in
# `genos-frontend` `src/__tests__/builtinTaskTemplates.test.ts`, against its vendored copy of the file.
PINNED_SHA256 = "7cbb3cae38bae98e63f653558ab4d9a43fa17da2f0163112ae1614913e55c62e"


class SharedFileTests(SimpleTestCase):
    def _raw(self):
        return json.loads(builtin_templates.SOURCE_PATH.read_text(encoding="utf-8"))

    def test_the_file_is_the_one_whose_hash_the_frontend_pins_too(self):
        self.assertEqual(
            builtin_templates.canonical_sha256(self._raw()),
            PINNED_SHA256,
            "builtin_task_templates.json changed. The browser builds the task form from a vendored "
            "copy of this file (genos-frontend src/features/tasks/utils/builtinTaskTemplates.json): "
            "copy it there, and set the new hash in BOTH repos' tests "
            "(here PINNED_SHA256, there PINNED_SHA256 in builtinTaskTemplates.test.ts). Two copies "
            "that differ are a task form and an agent that disagree.",
        )

    def test_the_module_hashes_what_it_loaded(self):
        self.assertEqual(builtin_templates.CANONICAL_SHA256, PINNED_SHA256)

    def test_the_hash_ignores_formatting_but_not_content_or_order(self):
        raw = self._raw()
        reindented = json.loads(json.dumps(raw, indent=2))
        self.assertEqual(
            builtin_templates.canonical_sha256(reindented), builtin_templates.canonical_sha256(raw)
        )
        edited = json.loads(json.dumps(raw))
        edited["templates"][1]["sections"][0]["heading"] += "!"
        self.assertNotEqual(
            builtin_templates.canonical_sha256(edited), builtin_templates.canonical_sha256(raw)
        )
        reordered = json.loads(json.dumps(raw))
        reordered["templates"].reverse()
        self.assertNotEqual(
            builtin_templates.canonical_sha256(reordered), builtin_templates.canonical_sha256(raw)
        )

    def test_a_malformed_file_is_refused_at_load(self):
        good = self._raw()["templates"]
        for name, mutate in {
            "no templates": lambda t: t.clear(),
            "repeated id": lambda t: t.append(dict(t[0])),
            "blank label": lambda t: t[0].update(label=" "),
            "no sections": lambda t: t[0].update(sections=[]),
            "blank heading": lambda t: t[0]["sections"][0].update(heading=""),
            "unknown kind": lambda t: t[0]["sections"][0]["items"][0].update(kind="para"),
            "blank prompt": lambda t: t[0]["sections"][0]["items"][0].update(text=" "),
        }.items():
            with self.subTest(case=name):
                bad = json.loads(json.dumps(good))
                mutate(bad)
                with self.assertRaises(ValueError):
                    builtin_templates._validated({"templates": bad})
        with self.assertRaises(ValueError):
            builtin_templates._validated({"templates": "x"})

    def test_labels_headings_and_prompts_come_straight_from_the_file(self):
        raw = self._raw()["templates"]
        self.assertEqual(
            {t["id"]: (t["label"], [s["heading"] for s in t["sections"]]) for t in raw},
            {k: (label, list(h)) for k, (label, h) in BUILT_IN_TEMPLATES.items()},
        )
        every_item = {i["text"] for t in raw for s in t["sections"] for i in s["items"]}
        self.assertEqual(template_prompts.PROMPTS, every_item)
