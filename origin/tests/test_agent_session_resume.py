"""Tests for explicit session resume (`resume: true` on /api/v2/agent/ask/).

A plain (non-entity-scoped) `AgentSession` normally expires after
`SESSION_TTL_MINUTES` of inactivity: an /ask/ carrying its `session_id`
silently mints a fresh session, dropping all prior context. That was a
tolerable artifact for the ephemeral Cmd-K overlay, but the Genos page
keeps the transcript on screen — continuing a visible conversation must
never silently fork. `allow_expired=True` (wired from the request's
`resume` flag) lets an expired plain session be reused, bounded by the
user's history-retention window so resume-ability exactly matches what
the history views show.

The NDJSON *stream* contract is untouched — `resume` is a request-body
field only (see test_agent_event_contract.py, which still pins the
event vocabulary).
"""

import uuid
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from origin.search_engine.agent_views import _get_or_create_session
from origin.search_engine.models import AgentSession


def _age(session: AgentSession, minutes: int = 0, days: int = 0) -> AgentSession:
    """Push `last_active_at` into the past (update() to skip auto_now)."""
    past = timezone.now() - timedelta(minutes=minutes, days=days)
    AgentSession.objects.filter(session_id=session.session_id).update(last_active_at=past)
    session.refresh_from_db()
    return session


class ExplicitResumeTests(TestCase):
    TEAM = "team-1"
    USER = "user-1"

    def _plain_session(self) -> AgentSession:
        return AgentSession.objects.create(team_id=self.TEAM, user_id=self.USER)

    def _resolve(self, session_id, *, allow_expired=False, user=None, team=None):
        return _get_or_create_session(
            str(session_id),
            team or self.TEAM,
            user or self.USER,
            allow_expired=allow_expired,
        )

    def test_expired_plain_session_with_resume_is_reused(self):
        session = _age(self._plain_session(), minutes=60)  # past the 30-min TTL
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days",
            return_value=None,  # unlimited tier
        ):
            got = self._resolve(session.session_id, allow_expired=True)
        self.assertEqual(got.session_id, session.session_id)
        # Reuse touches the activity clock so the follow-up turn keeps
        # the session live for subsequent asks.
        self.assertGreater(got.last_active_at, session.last_active_at)

    def test_expired_plain_session_without_resume_mints_new(self):
        # Regression guard: the implicit-continuation path keeps its TTL.
        session = _age(self._plain_session(), minutes=60)
        got = self._resolve(session.session_id, allow_expired=False)
        self.assertNotEqual(got.session_id, session.session_id)

    def test_resume_outside_retention_window_mints_new(self):
        # The session is older than the tier's history window, so the
        # history views hide it — resume must not reach it either.
        session = _age(self._plain_session(), days=40)
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days",
            return_value=30,
        ):
            got = self._resolve(session.session_id, allow_expired=True)
        self.assertNotEqual(got.session_id, session.session_id)

    def test_resume_inside_retention_window_is_reused(self):
        session = _age(self._plain_session(), days=10)
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days",
            return_value=30,
        ):
            got = self._resolve(session.session_id, allow_expired=True)
        self.assertEqual(got.session_id, session.session_id)

    def test_resume_cannot_reach_foreign_user_session(self):
        session = _age(self._plain_session(), minutes=60)
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days",
            return_value=None,
        ):
            got = self._resolve(session.session_id, allow_expired=True, user="intruder")
        self.assertNotEqual(got.session_id, session.session_id)

    def test_resume_cannot_reach_foreign_team_session(self):
        session = _age(self._plain_session(), minutes=60)
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days",
            return_value=None,
        ):
            got = self._resolve(session.session_id, allow_expired=True, team="team-2")
        self.assertNotEqual(got.session_id, session.session_id)

    def test_fresh_session_with_resume_is_reused_without_retention_lookup(self):
        # `resume` widens acceptance; it never narrows it. And since the
        # flag rides on EVERY continuing ask, the live-session fast path
        # must not pay a tier lookup per turn.
        session = self._plain_session()
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days"
        ) as helper:
            got = self._resolve(session.session_id, allow_expired=True)
        self.assertEqual(got.session_id, session.session_id)
        helper.assert_not_called()

    def test_fresh_session_without_resume_still_reused(self):
        session = self._plain_session()
        got = self._resolve(session.session_id, allow_expired=False)
        self.assertEqual(got.session_id, session.session_id)

    def test_entity_scoped_session_ignores_ttl_regardless_of_flag(self):
        # Thread/note sessions already bypassed the TTL; unchanged.
        session = _age(
            AgentSession.objects.create(
                team_id=self.TEAM,
                user_id=self.USER,
                chat_type=1,
                chat_id=uuid.uuid4(),
                thread_id=uuid.uuid4(),
            ),
            minutes=60,
        )
        got = self._resolve(session.session_id, allow_expired=False)
        self.assertEqual(got.session_id, session.session_id)

    def test_retention_lookup_skipped_when_flag_absent(self):
        # Without `resume`, the retention helper must not even be
        # consulted — the TTL path stays byte-for-byte the old behavior.
        session = _age(self._plain_session(), minutes=60)
        with mock.patch(
            "origin.search_engine.agent_views.get_agent_history_retention_days"
        ) as helper:
            self._resolve(session.session_id, allow_expired=False)
        helper.assert_not_called()
