"""Regression: demo teardown must sweep the agent-history tables.

`AgentSession` / `AgentRun` key their owner with bare `CharField`
team_id / user_id columns — no FK to TeamMaster or to the user — so
nothing about deleting a demo team or its users cascades to them. Before
the fix, every demo visitor who asked Spotlight a single question left
AgentSession + AgentRun + AgentStep rows behind permanently, on a table
that grows with every LP visit.

The subtle half is that deleting the sessions is NOT enough on its own.
`AgentRun.session` is `on_delete=SET_NULL`, so a session delete does not
cascade to its runs — it NULLs their `session_id` and leaves them (plus
their steps and LLM-call telemetry) alive and unreachable. Verified
directly: after deleting only the session, the run row still exists with
`session_id = None`. So the runs need their own explicit delete, which is
what `test_runs_are_deleted_not_orphaned` pins — it fails if the
`AgentRun` sweep is ever dropped on the theory that the session cascade
covers it.

(Deleting runs before sessions, as `delete_demo_team_data` does, is the
clearer order because it never transiently orphans anything. It is not
load-bearing here though: both sweeps filter on the same `team_id`, so
either order removes both sets of rows.)

(`account_deletion.py::_purge_search_engine_rows` had this exact bug —
it deleted AgentSession under a comment claiming the runs cascaded from
it, leaving the deleted account's raw question text behind. Fixed, and
pinned by `AgentHistoryErasureTests` in `test_account_deletion.py`.)
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.services.demo_seeder import delete_demo_environment

User = get_user_model()


class DemoCleanupAgentHistoryTests(TestCase):
    def _demo_env_with_agent_history(self):
        user = User.objects.create_user(
            username="demo_agent_hist_user",
            email="demo_agent_hist@example.com",
            password="x",
            is_demo=True,
        )
        team = TeamMaster.objects.create(
            team_name="Demo Agent History Team",
            team_email="demo_agent_hist_team@example.com",
            owner=user,
            is_demo=True,
        )
        TeamMembers.objects.create(team=team, attendee=user)

        team_id, user_id = str(team.team_id), str(user.id)
        session = AgentSession.objects.create(team_id=team_id, user_id=user_id)
        run = AgentRun.objects.create(
            team_id=team_id,
            user_id=user_id,
            query="why did we decide that?",
            status="done",
            final_answer_text="because of the perf budget",
            session=session,
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="search_knowledge_base",
            result_json={"matches": []},
        )
        return user, team, session, run

    def test_teardown_removes_sessions_runs_and_steps(self):
        user, team, session, run = self._demo_env_with_agent_history()
        team_id = str(team.team_id)

        delete_demo_environment(user)

        self.assertFalse(AgentRun.objects.filter(team_id=team_id).exists())
        self.assertFalse(AgentSession.objects.filter(team_id=team_id).exists())
        # AgentStep cascades from AgentRun, so deleting the run sweeps it.
        self.assertFalse(AgentStep.objects.filter(run_id=run.run_id).exists())
        self.assertFalse(AgentSession.objects.filter(pk=session.pk).exists())

    def test_runs_are_deleted_not_orphaned(self):
        """Guards against relying on a session cascade that does not exist.

        `AgentRun.session` is SET_NULL, so if the explicit AgentRun sweep
        is ever removed on the theory that deleting the session takes the
        runs with it, the run survives with `session_id = NULL`. The
        second assertion is the specific one: zero runs for this team that
        have a NULL session — i.e. nothing was merely detached.
        """
        user, team, _session, _run = self._demo_env_with_agent_history()
        team_id = str(team.team_id)

        delete_demo_environment(user)

        self.assertEqual(AgentRun.objects.filter(team_id=team_id).count(), 0)
        self.assertEqual(
            AgentRun.objects.filter(team_id=team_id, session__isnull=True).count(), 0
        )

    def test_teardown_leaves_other_teams_agent_history_alone(self):
        """The sweep is team-scoped: one demo logout must not touch another
        visitor's rows (both are is_demo, both live in the same tables)."""
        user, team, _s, _r = self._demo_env_with_agent_history()

        other = User.objects.create_user(
            username="demo_agent_hist_other",
            email="demo_agent_hist_other@example.com",
            password="x",
            is_demo=True,
        )
        other_team = TeamMaster.objects.create(
            team_name="Other Demo Team",
            team_email="demo_agent_hist_other_team@example.com",
            owner=other,
            is_demo=True,
        )
        other_run = AgentRun.objects.create(
            team_id=str(other_team.team_id),
            user_id=str(other.id),
            query="unrelated",
            status="done",
            final_answer_text="unrelated",
        )

        delete_demo_environment(user)

        self.assertFalse(AgentRun.objects.filter(team_id=str(team.team_id)).exists())
        self.assertTrue(AgentRun.objects.filter(pk=other_run.pk).exists())
