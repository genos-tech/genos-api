"""Tests for ask-history search + pinning on the agent sessions endpoints.

Both features exist because the sidebar browses a capped slice of recent
sessions (`_HISTORY_LIST_LIMIT`), so anything older is unreachable:

  * `?search=` reaches past that slice, and matches ANY question in a
    session rather than only the first one the row is labelled with;
  * pinning keeps a session at the top, and pinned rows are returned in
    ADDITION to the slice — a pin that could still age out would be
    pointless.

The retention window (test_agent_history_retention.py) still wins over
both: a pin is not a way to read past the plan's history limit.
"""

from datetime import timedelta

from django.test import override_settings
from django.utils import timezone

from origin.search_engine import quota
from origin.search_engine.agent_views import _HISTORY_LIST_LIMIT
from origin.search_engine.models import AgentRun, AgentSession

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas

LIST_URL = "/api/v2/agent/sessions/"

_WINDOWED_QUOTAS = {
    **TEST_QUOTAS,
    "free": {**TEST_QUOTAS["free"], "agent_history_retention_days": 30},
}


class AgentHistorySearchPinTestsBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        quota.invalidate_effective_tier([self.user.id])
        self.scope = {"team_id": str(self.team.pk), "user_id": str(self.user.id)}

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def make_session(self, *queries, pinned_at=None, last_active_at=None, user=None):
        """A session whose turns asked `queries`, oldest first."""
        session = AgentSession.objects.create(
            **{
                **self.scope,
                **({"user_id": str(user.id)} if user else {}),
                **({"pinned_at": pinned_at} if pinned_at else {}),
                **({"last_active_at": last_active_at} if last_active_at else {}),
            }
        )
        for q in queries:
            AgentRun.objects.create(
                team_id=session.team_id,
                user_id=session.user_id,
                query=q,
                session=session,
                status="done",
            )
        return session

    def list_sessions(self, **params):
        res = self.client.get(LIST_URL, {"team_id": str(self.team.pk), **params})
        self.assertEqual(res.status_code, 200)
        return res.data["sessions"]

    def list_ids(self, **params):
        return [s["session_id"] for s in self.list_sessions(**params)]


class SearchTests(AgentHistorySearchPinTestsBase):
    def test_matches_a_question_from_any_turn(self):
        first = self.make_session("how do I deploy the api")
        later = self.make_session("unrelated opener", "what about deploy rollbacks")
        miss = self.make_session("something else entirely")

        found = set(self.list_ids(search="deploy"))

        # The mid-conversation match is the whole point: the row is
        # labelled with its first question, so matching only that would
        # hide `later` from the user who remembers asking it.
        self.assertEqual(found, {str(first.session_id), str(later.session_id)})
        self.assertNotIn(str(miss.session_id), found)

    def test_is_case_insensitive(self):
        session = self.make_session("Where is the Runbook")

        self.assertEqual(self.list_ids(search="runbook"), [str(session.session_id)])
        self.assertEqual(self.list_ids(search="RUNBOOK"), [str(session.session_id)])

    def test_returns_a_session_once_when_several_turns_match(self):
        session = self.make_session("deploy step one", "deploy step two", "deploy step three")

        # Without `distinct()` the join yields one row per matching run.
        self.assertEqual(self.list_ids(search="deploy"), [str(session.session_id)])

    def test_reaches_past_the_browse_cap(self):
        # The needle is older than a full page of recent sessions, so
        # it's invisible to plain browsing — the case search exists for.
        needle = self.make_session(
            "the quarterly forecast question",
            last_active_at=timezone.now() - timedelta(days=5),
        )
        for i in range(_HISTORY_LIST_LIMIT + 5):
            self.make_session(f"filler {i}")

        self.assertNotIn(str(needle.session_id), self.list_ids())
        self.assertEqual(self.list_ids(search="quarterly forecast"), [str(needle.session_id)])

    def test_blank_search_is_ignored(self):
        session = self.make_session("anything")

        self.assertEqual(self.list_ids(search="   "), [str(session.session_id)])

    def test_never_matches_another_users_session(self):
        mine = self.make_session("deploy notes")
        theirs = self.make_session("deploy notes", user=self.user2)

        found = self.list_ids(search="deploy")

        self.assertEqual(found, [str(mine.session_id)])
        self.assertNotIn(str(theirs.session_id), found)


class PinListTests(AgentHistorySearchPinTestsBase):
    def test_pinned_sessions_come_first(self):
        self.make_session("newest ask")
        pinned = self.make_session(
            "pinned ask",
            pinned_at=timezone.now(),
            last_active_at=timezone.now() - timedelta(days=2),
        )

        rows = self.list_sessions()

        # Older by last_active_at, but pinned — so it leads.
        self.assertEqual(rows[0]["session_id"], str(pinned.session_id))
        self.assertTrue(rows[0]["is_pinned"])
        self.assertFalse(rows[1]["is_pinned"])

    def test_most_recently_pinned_leads(self):
        older_pin = self.make_session("pinned first", pinned_at=timezone.now() - timedelta(days=1))
        newer_pin = self.make_session("pinned second", pinned_at=timezone.now())

        self.assertEqual(
            self.list_ids()[:2],
            [str(newer_pin.session_id), str(older_pin.session_id)],
        )

    def test_a_pin_survives_the_browse_cap(self):
        pinned = self.make_session(
            "pinned long ago",
            pinned_at=timezone.now(),
            last_active_at=timezone.now() - timedelta(days=10),
        )
        for i in range(_HISTORY_LIST_LIMIT + 5):
            self.make_session(f"filler {i}")

        ids = self.list_ids()

        # Returned ON TOP of the recent slice, not competing for its rows.
        self.assertEqual(ids[0], str(pinned.session_id))
        self.assertEqual(len(ids), _HISTORY_LIST_LIMIT + 1)

    def test_search_still_applies_to_pinned_rows(self):
        pinned_miss = self.make_session("pinned but unrelated", pinned_at=timezone.now())
        pinned_hit = self.make_session("pinned deploy notes", pinned_at=timezone.now())

        found = self.list_ids(search="deploy")

        self.assertEqual(found, [str(pinned_hit.session_id)])
        self.assertNotIn(str(pinned_miss.session_id), found)

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_a_pin_does_not_reach_past_the_retention_window(self):
        stale_pin = self.make_session(
            "pinned but out of window",
            pinned_at=timezone.now(),
            last_active_at=timezone.now() - timedelta(days=60),
        )
        fresh = self.make_session("in window")

        # Pinning must not become a way to read history the plan
        # doesn't cover. Hidden, not deleted — an upgrade brings it back.
        self.assertEqual(self.list_ids(), [str(fresh.session_id)])
        self.assertNotIn(str(stale_pin.session_id), self.list_ids(search="pinned"))
        self.assertIsNotNone(AgentSession.objects.get(pk=stale_pin.pk).pinned_at)


class PinToggleTests(AgentHistorySearchPinTestsBase):
    def pin_url(self, session_id):
        return f"{LIST_URL}{session_id}/pin/"

    def test_post_pins_and_delete_unpins(self):
        session = self.make_session("ask")

        res = self.client.post(self.pin_url(session.session_id))
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["is_pinned"])
        self.assertIsNotNone(AgentSession.objects.get(pk=session.pk).pinned_at)

        res = self.client.delete(self.pin_url(session.session_id))
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["is_pinned"])
        self.assertIsNone(AgentSession.objects.get(pk=session.pk).pinned_at)

    def test_pinning_twice_keeps_the_original_pin_time(self):
        session = self.make_session("ask")

        self.client.post(self.pin_url(session.session_id))
        first = AgentSession.objects.get(pk=session.pk).pinned_at
        self.client.post(self.pin_url(session.session_id))

        # Pin order is `-pinned_at`, so a re-pin that moved the timestamp
        # would reshuffle the sidebar under a double-click.
        self.assertEqual(AgentSession.objects.get(pk=session.pk).pinned_at, first)

    def test_unpinning_an_unpinned_session_is_a_no_op(self):
        session = self.make_session("ask")

        res = self.client.delete(self.pin_url(session.session_id))

        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["is_pinned"])

    def test_cannot_pin_another_users_session(self):
        theirs = self.make_session("their ask", user=self.user2)

        res = self.client.post(self.pin_url(theirs.session_id))

        # 404, not 403: never confirm that an id someone else owns exists.
        self.assertEqual(res.status_code, 404)
        self.assertIsNone(AgentSession.objects.get(pk=theirs.pk).pinned_at)

    def test_unknown_and_malformed_ids_are_404(self):
        self.assertEqual(
            self.client.post(self.pin_url("11111111-2222-4333-8444-555555555555")).status_code,
            404,
        )
        self.assertEqual(self.client.post(self.pin_url("not-a-uuid")).status_code, 404)

    def test_requires_auth(self):
        session = self.make_session("ask")
        self.unauthenticate()

        self.assertEqual(self.client.post(self.pin_url(session.session_id)).status_code, 401)

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(_WINDOWED_QUOTAS))
    def test_can_still_unpin_a_session_the_window_now_hides(self):
        session = self.make_session(
            "pinned before the downgrade",
            pinned_at=timezone.now(),
            last_active_at=timezone.now() - timedelta(days=60),
        )

        res = self.client.delete(self.pin_url(session.session_id))

        # The window governs reading, not pin state. Gating this verb on
        # it would strand the pin after a downgrade.
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(AgentSession.objects.get(pk=session.pk).pinned_at)
