"""Quality round 2 — retrieval assembly lever + eval environment guard.

Contract under test:

  * `_collapse_mirror_entities` (flag-gated, DEFAULT OFF per the
    measured retrieval A/B): cross-type same-title mirrors collapse to
    the highest-ranked row; chat surfaces of the same channel collapse
    per (chat_id, title); same-type equal titles and untitled rows
    survive.
  * `agent_eval`'s `_index_mapping_drift` preflight: reports text
    subfields the canonical mapping expects but the live index lacks
    (the stale-local-index failure mode that skewed round 2's first
    measurements), stays quiet on a healthy or richer-than-expected
    index, and treats connectivity errors as not-drift.
"""

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from origin.search_engine.management.commands.agent_eval import Command
from origin.search_engine.search import _collapse_mirror_entities


def _row(title, etype, chat_id=None, score=1.0):
    return {"title": title, "entity_type": etype, "chat_id": chat_id, "score": score}


class CollapseMirrorEntitiesTests(SimpleTestCase):
    def test_cross_type_mirror_drops_lower_ranked(self):
        rows = [
            _row("Q2 Roadmap", "project"),
            _row("Q2 Roadmap", "chat", chat_id="c1"),
            _row("Customer interviews", "task"),
        ]
        out = _collapse_mirror_entities(rows)
        self.assertEqual(
            [(r["title"], r["entity_type"]) for r in out],
            [("Q2 Roadmap", "project"), ("Customer interviews", "task")],
        )

    def test_same_type_equal_titles_survive(self):
        rows = [_row("Fix flaky test", "task"), _row("Fix flaky test", "task")]
        self.assertEqual(len(_collapse_mirror_entities(rows)), 2)

    def test_chat_surfaces_of_same_channel_collapse(self):
        rows = [
            _row("Q2 Roadmap", "chat", chat_id="c1"),  # thread window
            _row("Q2 Roadmap", "chat", chat_id="c1"),  # another thread
            _row("Q2 Roadmap", "chat", chat_id="c2"),  # DIFFERENT channel, same name
        ]
        out = _collapse_mirror_entities(rows)
        self.assertEqual([r["chat_id"] for r in out], ["c1", "c2"])

    def test_thread_with_own_title_survives(self):
        rows = [
            _row("Q2 Roadmap", "chat", chat_id="c1"),
            _row("Perf budget discussion", "chat", chat_id="c1"),
        ]
        self.assertEqual(len(_collapse_mirror_entities(rows)), 2)

    def test_untitled_rows_pass_through(self):
        rows = [_row("", "chat", chat_id="c1"), _row(None, "task"), _row("X", "task")]
        self.assertEqual(len(_collapse_mirror_entities(rows)), 3)


class IndexMappingDriftTests(SimpleTestCase):
    def _drift_with_live_fields(self, live_title_fields):
        live = {
            "knowledge_chunks_v1": {
                "mappings": {
                    "properties": {
                        "title": {"type": "text", "fields": live_title_fields},
                        # Other text fields present WITH their expected
                        # subfields so only `title` can drift in this
                        # fixture.
                    }
                }
            }
        }
        client = MagicMock()
        client.indices.get_mapping.return_value = live
        with patch(
            "origin.search_engine.opensearch_client.get_client", return_value=client
        ):
            return Command()._index_mapping_drift()

    def test_missing_subfields_reported(self):
        drift = self._drift_with_live_fields({})
        title_lines = [d for d in drift if d.startswith("title:")]
        self.assertEqual(len(title_lines), 1)
        self.assertIn("icu", title_lines[0])
        self.assertIn("prefix", title_lines[0])

    def test_healthy_title_not_reported(self):
        from origin.search_engine.index_config import build_mappings

        expected = build_mappings()["properties"]["title"]["fields"]
        drift = self._drift_with_live_fields(dict(expected))
        self.assertEqual([d for d in drift if d.startswith("title:")], [])

    def test_connectivity_error_is_not_drift(self):
        client = MagicMock()
        client.indices.get_mapping.side_effect = ConnectionError("opensearch down")
        with patch(
            "origin.search_engine.opensearch_client.get_client", return_value=client
        ):
            self.assertEqual(Command()._index_mapping_drift(), [])
