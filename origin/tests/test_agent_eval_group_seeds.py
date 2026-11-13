"""The seeds of the eval-group cases against the cases that do not list them (MIP-1271 tools stack).

A seed's rows stay in the shared sequence teams for good, so a case that never asked for a seed still runs
against it. This guard fails when a string a case FORBIDS in an answer or in a tool result occurs in the
text of a seed the case's viewer can read, so a seed cannot quietly turn another item's case into a failure
on arm0. `seeds.VISIBLE_TO` says who can read what: the seeds put their tasks and notes in projects of their
own (the story's project is untouched) and the inbox report in one member's own inbox.
"""

from __future__ import annotations

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness, seeds, sequence


def forbidden_strings(case) -> list[str]:
    out: list[str] = []
    expects = [case.raw.get("expect") or {}]
    expects += [t.get("expect") or {} for t in case.raw.get("turns") or []]
    for expect in expects:
        for key in ("answer_does_not_contain", "no_tool_results_contain"):
            out += [str(s) for s in expect.get(key) or []]
    return out


class SeedGuardTests(SimpleTestCase):
    def setUp(self):
        self.cases = [
            c for c in harness.load_cases(include_legacy=False) if c.fixture == "sequence"
        ]

    def test_every_seed_a_case_names_exists(self):
        for case in self.cases:
            for name in case.raw.get("seed") or []:
                self.assertIn(name, seeds.SEEDS, case.id)

    def test_every_seed_says_who_can_read_it_and_has_text(self):
        for name in seeds.SEEDS:
            self.assertIn(name, seeds.VISIBLE_TO)
            for lang in ("en", "ja"):
                self.assertTrue(seeds.seed_texts(name, lang), (name, lang))

    def test_no_case_forbids_a_word_that_a_seed_it_can_read_contains(self):
        clashes = []
        for case in self.cases:
            team, person = sequence.VIEWERS[case.raw.get("viewer", "v1")]
            lang = "ja" if team == "ja" else "en"
            if team == "v2":
                continue  # its own team: no seed is ever applied there
            for name in seeds.SEEDS:
                if name in (case.raw.get("seed") or []) or person not in seeds.VISIBLE_TO[name]:
                    continue
                texts = [t.lower() for t in seeds.seed_texts(name, lang)]
                for word in forbidden_strings(case):
                    if any(word.lower() in text for text in texts):
                        clashes.append(f"{case.id} forbids {word!r}, which seed {name} contains")
        self.assertEqual(clashes, [], "\n".join(clashes))

    def test_the_guard_does_catch_a_clash(self):
        """Not vacuous: the report's heading is a forbidden word of a made-up case."""
        fake = type("C", (), {"raw": {"expect": {"answer_does_not_contain": ["Needs attention"]}}})
        texts = [t.lower() for t in seeds.seed_texts("inbox_report", "en")]
        words = forbidden_strings(fake)
        self.assertTrue(any(w.lower() in t for w in words for t in texts))
