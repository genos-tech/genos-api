"""Tests for `_load_prior_turns` — session history that a follow-up turn
sees. Regression guard for the bug where a long prior answer was truncated
to 400 chars, so "save that answer to my note" / "include ALL of it" only
ever had the first paragraph in context and couldn't reproduce the rest.
"""

from django.conf import settings
from django.test import TestCase, override_settings

from origin.search_engine.agent_views import _load_prior_turns
from origin.search_engine.models import AgentRun, AgentSession


class TestLoadPriorTurns(TestCase):
    def _make_run(self, session, query, answer):
        return AgentRun.objects.create(
            session=session,
            team_id=session.team_id,
            user_id=session.user_id,
            query=query,
            final_answer_text=answer,
            status="done",
        )

    def test_long_prior_answer_is_carried_in_full(self):
        session = AgentSession.objects.create(team_id="t1", user_id="u1")
        long_answer = "Section A\n\n" + ("x" * 3600)  # ~3610 chars, well past 400
        self._make_run(session, "explain pglogical", long_answer)

        turns = _load_prior_turns(session, max_turns=3)

        self.assertEqual(len(turns), 1)
        _q, a = turns[0]
        self.assertEqual(a, long_answer)  # full, untruncated
        self.assertGreater(len(a), 400)

    def test_two_turns_back_answer_also_preserved(self):
        # The "no, include ALL of it" retry: the target answer sits two
        # turns back, so every turn in the verbatim window must carry the
        # full text — not just the most-recent one.
        session = AgentSession.objects.create(team_id="t1", user_id="u1")
        big = "B" * 3000
        self._make_run(session, "q1 big answer", big)
        self._make_run(session, "save it", "ok, saved")
        self._make_run(session, "no, all of it", "here you go")

        turns = _load_prior_turns(session, max_turns=3)

        self.assertEqual(len(turns), 3)
        by_q = {q: a for q, a in turns}
        self.assertEqual(by_q["q1 big answer"], big)  # full 3000, not 400

    @override_settings(
        SEARCH_ENGINE={**settings.SEARCH_ENGINE, "SESSION_PRIOR_ANSWER_MAX_CHARS": 50}
    )
    def test_cap_is_settings_overridable(self):
        session = AgentSession.objects.create(team_id="t1", user_id="u1")
        self._make_run(session, "q", "y" * 500)

        turns = _load_prior_turns(session, max_turns=3)

        self.assertEqual(len(turns[0][1]), 50)
