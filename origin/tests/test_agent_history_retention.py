"""Tests for agent-history retention (UX tier model §6.3).

Three enforcement points share one window (`agent_history_retention_days`,
SESSION-anchored on `last_active_at`):

  * the sessions LIST hides out-of-window sessions;
  * the session DETAIL 404s them (indistinguishable from not-found —
    never "exists but locked");
  * the `conversation` recall lane is cut to the same window in
    `_build_filter`, or the agent would still recall asks the user can
    no longer see.

HIDE, never delete: no rows are removed, so an upgrade (or the dark
permissive default) restores everything — pinned by the missing-key
cases.
"""

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from origin.search_engine import quota
from origin.search_engine.models import AgentSession
from origin.search_engine.search import _build_filter

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

LIST_URL = "/api/v2/agent/sessions/"

_WINDOWED_QUOTAS = {
    **TEST_QUOTAS,
    "free": {**TEST_QUOTAS["free"], "agent_history_retention_days": 30},
}


def _conversation_retention_clauses(filt):
    out = []
    for clause in filt:
        shoulds = clause.get("bool", {}).get("should", [])
        for inner in shoulds:
            rng = inner.get("range", {}).get("updated_at")
            if rng and any(
                s.get("bool", {})
                .get("must_not", [{}])[0]
                .get("term", {})
                .get("entity_type")
                == "conversation"
                for s in shoulds
            ):
                out.append(clause)
    return out


class ConversationLaneCutoffFilterTests(BaseAPITestCase):
    def test_no_cutoff_no_clause(self):
        filt = _build_filter("team-1", "user-1", None, None, None)
        self.assertEqual(_conversation_retention_clauses(filt), [])

    def test_cutoff_builds_the_not_or_recent_clause(self):
        cutoff = "2026-07-01T00:00:00+00:00"
        filt = _build_filter(
            "team-1", "user-1", None, None, None, conversation_retention_cutoff=cutoff
        )
        clauses = _conversation_retention_clauses(filt)
        self.assertEqual(len(clauses), 1)
        self.assertEqual(
            clauses[0]["bool"]["should"][1],
            {"range": {"updated_at": {"gte": cutoff}}},
        )
        self.assertEqual(clauses[0]["bool"]["minimum_should_match"], 1)


class SessionWindowViewTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id])
        common = {"team_id": str(self.team.pk), "user_id": str(self.user.id)}
        self.fresh = AgentSession.objects.create(**common)
        self.stale = AgentSession.objects.create(
            **common, last_active_at=timezone.now() - timedelta(days=60)
        )

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def _list_ids(self):
        res = self.client.get(LIST_URL, {"team_id": str(self.team.pk)})
        self.assertEqual(res.status_code, 200)
        return {s["session_id"] for s in res.data["sessions"]}

    def _detail(self, session):
        return self.client.get(
            f"{LIST_URL}{session.session_id}/", {"team_id": str(self.team.pk)}
        )

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_list_hides_out_of_window_sessions(self):
        self.assertEqual(self._list_ids(), {str(self.fresh.session_id)})

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_detail_404s_an_out_of_window_session(self):
        self.assertEqual(self._detail(self.stale).status_code, 404)
        self.assertEqual(self._detail(self.fresh).status_code, 200)

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
    def test_missing_key_shows_everything(self):
        # Dark/permissive — and the hide-never-delete proof: the same
        # stale row a windowed config hid is fully served again.
        self.assertEqual(
            self._list_ids(),
            {str(self.fresh.session_id), str(self.stale.session_id)},
        )
        self.assertEqual(self._detail(self.stale).status_code, 200)
