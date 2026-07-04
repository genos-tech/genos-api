"""D5 natural-prose citation measurement (§4.6) — pure/mocked tests.

Covers the three new measurement pieces:
  - `extract_prose_citations` / the link + bare regexes (deterministic
    extraction that both the adoption metric and the judge prompt use);
  - `_citation_style_metric` (prose_citation_rate, skip-when-immovable);
  - `judge_answer`'s nullable `prose_faithfulness` axis — most
    importantly the force-to-None rule: an answer with NO link-form
    citations must never carry a numeric prose score, even when the
    judge model returns one (0.0 there would silently punish bare-form
    answers in the aggregate).

No DB, no network: the judge's model client is stubbed.
"""

import json
from unittest.mock import patch

from django.test import SimpleTestCase

from origin.search_engine.agent.evals.judge import (
    _build_user_prompt,
    _error_scores,
    extract_prose_citations,
    judge_answer,
)
from origin.search_engine.agent.evals.runner import (
    _CITATION_BARE_RE,
    _citation_style_metric,
)


def _delta_events(answer: str) -> list[dict]:
    return [{"type": "answer_delta", "text": answer}]


class _StubClient:
    """Minimal ModelClient double: generate_step yields one text chunk."""

    def __init__(self, payload: dict):
        self._raw = json.dumps(payload)

    def generate_step(self, **_kwargs):
        yield (self._raw, None)


class TestExtractProseCitations(SimpleTestCase):
    def test_extracts_label_id_pairs_in_order(self):
        ans = (
            "The team [ruled out framer-motion](task:42) over bundle size; "
            "see [the follow-up thread](chat:pm:1:thread:3)."
        )
        self.assertEqual(
            extract_prose_citations(ans),
            [
                ("ruled out framer-motion", "task:42"),
                ("the follow-up thread", "chat:pm:1:thread:3"),
            ],
        )

    def test_ignores_ordinary_markdown_links_and_bare_tokens(self):
        ans = (
            "See [the docs](https://example.com), mail [me](mailto:x@y.z), "
            "note [sic], and the bare token [task:42]."
        )
        self.assertEqual(extract_prose_citations(ans), [])

    def test_todo_and_milestone_ids_match(self):
        ans = "[the launch item](todo:2026-07-03:item:117) and [the beta](milestone:12)"
        self.assertEqual(
            extract_prose_citations(ans),
            [("the launch item", "todo:2026-07-03:item:117"), ("the beta", "milestone:12")],
        )

    def test_bare_regex_skips_link_labels_and_prose_brackets(self):
        ans = "[ruled it out](task:42) then [note:personal:7] but not [sic]"
        self.assertEqual(_CITATION_BARE_RE.findall(ans), ["note:personal:7"])


class TestCitationStyleMetric(SimpleTestCase):
    def test_skipped_when_case_has_no_citation_expectation(self):
        events = _delta_events("All link form [x](task:1).")
        self.assertEqual(_citation_style_metric(events, {"no_errors": True}), {})

    def test_skipped_for_no_citations_cases(self):
        events = _delta_events("plain answer")
        self.assertEqual(_citation_style_metric(events, {"no_citations": True}), {})

    def test_all_link_form_scores_one(self):
        events = _delta_events("A [x](task:1) and B [y](note:personal:2).")
        m = _citation_style_metric(events, {"has_citations": True})
        self.assertEqual(m, {"prose_citation_rate": 1.0})

    def test_mixed_forms_score_fraction(self):
        events = _delta_events("A [x](task:1) and the bare [task:2].")
        m = _citation_style_metric(events, {"citations_count_at_least": 2})
        self.assertEqual(m, {"prose_citation_rate": 0.5})

    def test_expected_but_absent_scores_zero(self):
        events = _delta_events("no citations at all")
        m = _citation_style_metric(events, {"has_citations": True})
        self.assertEqual(m, {"prose_citation_rate": 0.0})


class TestJudgeProseFaithfulness(SimpleTestCase):
    _SOURCES = [{"entity_type": "task", "entity_id": "task:42", "title": "Spike: framer-motion"}]

    def _judge(self, answer: str, payload: dict) -> dict:
        with patch(
            "origin.search_engine.agent.evals.judge.get_model_client",
            return_value=_StubClient(payload),
        ):
            return judge_answer(query="q", sources=self._SOURCES, answer=answer)

    def test_numeric_score_kept_when_link_citations_exist(self):
        scores = self._judge(
            "The team [ruled out framer-motion](task:42).",
            {
                "faithfulness": 1.0,
                "citation_precision": 1.0,
                "completeness": 1.0,
                "prose_faithfulness": 0.85,
                "notes": "",
            },
        )
        self.assertEqual(scores["prose_faithfulness"], 0.85)

    def test_forced_none_when_answer_has_no_link_citations(self):
        # Judge (wrongly) returns a number for a bare-token answer —
        # the deterministic extractor overrides it to None.
        scores = self._judge(
            "The spike decided it [task:42].",
            {
                "faithfulness": 1.0,
                "citation_precision": 1.0,
                "completeness": 1.0,
                "prose_faithfulness": 0.0,
                "notes": "",
            },
        )
        self.assertIsNone(scores["prose_faithfulness"])

    def test_none_when_judge_omits_the_axis(self):
        scores = self._judge(
            "The team [ruled out framer-motion](task:42).",
            {"faithfulness": 1.0, "citation_precision": 1.0, "completeness": 1.0, "notes": ""},
        )
        self.assertIsNone(scores["prose_faithfulness"])

    def test_error_scores_carry_none_not_zero(self):
        self.assertIsNone(_error_scores("boom")["prose_faithfulness"])

    def test_prompt_lists_pairs_with_resolved_titles(self):
        prompt = _build_user_prompt(
            "q",
            self._SOURCES,
            "x",
            [],
            [("ruled out framer-motion", "task:42"), ("ghost", "task:999")],
        )
        self.assertIn('link text: "ruled out framer-motion" -> cites: task:42', prompt)
        self.assertIn('source title: "Spike: framer-motion"', prompt)
        self.assertIn("(id not among retrieved sources)", prompt)

    def test_prompt_normalises_unprefixed_chat_entity_ids(self):
        sources = [{"entity_type": "chat", "entity_id": "pm:1:thread:3", "title": "Design sync"}]
        prompt = _build_user_prompt(
            "q", sources, "x", [], [("the thread", "chat:pm:1:thread:3")]
        )
        self.assertIn('source title: "Design sync"', prompt)
