"""Tests for the viewer-tier retention clause in RAG retrieval.

`search._build_filter(chat_retention_cutoff=...)` must hide
chat-family chunks (`chat`, `thread_summary`) older than the viewer's
message-history window while leaving every other entity type
untouched — otherwise Spotlight would surface messages the chat UI
hides. Pure filter-shape tests (no OpenSearch round-trip), same style
as test_mention_search's `_build_filter` coverage, plus a resolution
test that `search()` derives the cutoff from the effective tier.
"""

from django.test import SimpleTestCase, override_settings

from origin.search_engine import quota
from origin.search_engine.search import _build_filter

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

CUTOFF = "2026-04-18T00:00:00+00:00"


def _retention_clauses(filt):
    """The retention bool-should clauses in a built filter."""
    out = []
    for clause in filt:
        should = clause.get("bool", {}).get("should")
        if not should:
            continue
        for branch in should:
            rng = branch.get("range", {}).get("created_at")
            if rng:
                out.append(clause)
                break
    return out


class BuildFilterRetentionShapeTests(SimpleTestCase):
    def test_no_cutoff_no_clause(self):
        filt = _build_filter("team-1", "user-1", None, None, None)
        self.assertEqual(_retention_clauses(filt), [])

    def test_cutoff_adds_chat_family_clause(self):
        filt = _build_filter("team-1", "user-1", None, None, None, chat_retention_cutoff=CUTOFF)
        clauses = _retention_clauses(filt)
        self.assertEqual(len(clauses), 1)
        should = clauses[0]["bool"]["should"]
        self.assertEqual(clauses[0]["bool"]["minimum_should_match"], 1)

        # Branch 1: NOT a chat-family chunk (other entity types pass).
        must_not = should[0]["bool"]["must_not"][0]["terms"]["entity_type"]
        self.assertEqual(set(must_not), {"chat", "thread_summary"})
        # Branch 2: chat-family chunks must be recent enough.
        self.assertEqual(should[1]["range"]["created_at"], {"gte": CUTOFF})

    def test_acl_and_tenant_clauses_still_first(self):
        filt = _build_filter("team-1", "user-1", None, None, None, chat_retention_cutoff=CUTOFF)
        self.assertEqual(filt[0], {"term": {"team_id": "team-1"}})
        self.assertEqual(filt[1], {"term": {"acl_user_ids": "user-1"}})

    def test_composes_with_entity_types_and_dates(self):
        filt = _build_filter(
            "team-1",
            "user-1",
            ["chat"],
            "2026-01-01",
            None,
            chat_retention_cutoff=CUTOFF,
        )
        self.assertIn({"terms": {"entity_type": ["chat"]}}, filt)
        self.assertIn({"range": {"updated_at": {"gte": "2026-01-01"}}}, filt)
        self.assertEqual(len(_retention_clauses(filt)), 1)


@override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
class RetentionResolutionTests(BaseAPITestCase):
    """`get_message_retention_days` (the value `search()` feeds the
    cutoff from) follows the effective tier."""

    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def test_free_viewer_gets_window_paid_viewer_does_not(self):
        self.assertEqual(quota.get_message_retention_days(self.user.id), 90)
        self.team.plan = "pro"
        self.team.save(update_fields=["plan"])
        quota.invalidate_effective_tier([self.user.id])
        self.assertIsNone(quota.get_message_retention_days(self.user.id))
