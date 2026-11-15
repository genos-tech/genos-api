"""`POST /agent/ask/` aligns the team of a task conversation as the restore route does (follow-up of MIP-1057, api #874).

A task of a project SHARED with another team belongs to the HOST team, and the person asking from the guest team sends
their CURRENT team. `AuthenticatedAPIView` rewrites a top-level `team_id` to the team that owns an object the caller can
reach (`foreign_team_scope.align_team_to_object`), so the restore route `GET /agent/task-session/?team_id=&task_id=` found
the task and returned its history. The ask names the task NESTED in `task_context`, which that rewrite does not read, so
with the current team the ask answered `404 "Task not found."` for a task the person could open: the restore returned the
history and the ask then said the task could not be opened.

What these tests pin:

  * The ask aligns the team of its `task_context` with the SAME helper and the SAME rule (`foreign_team_scope.aligned_team`:
    only for an object the caller reaches on its own terms, a substitution and never a grant), before anything is read or
    spent. The ask and the restore then agree, and the session, the run and the cost rollup sit with the task's own team.
  * It grants nothing. Whoever cannot open the task (a stranger, a member of a team that only shares a chat or a folder, a
    guest off the project, a deleted task, a missing one) gets the one answer a missing task gets, from the ask and from
    the restore alike, and nothing is written.
  * With the flag off `task_context` is the unknown field it always was: the team stays what the client sent.
"""

from __future__ import annotations

from unittest import mock

from django.conf import settings

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import task_context
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.models import AgentRun, AgentSession, AiRequestCost
from origin.views.utils import foreign_team_scope

from .task_ask_fixtures import MISSING_ID, TaskWorld


def rows():
    return (AgentSession.objects.count(), AgentRun.objects.count(), AiRequestCost.objects.count())


class SharedTaskWorld(TaskWorld):
    """Team A hosts a project shared with team B (an accepted grant); `b_owner`, who accepted the grant, is a guest on it, asking from team B."""

    def setUp(self):
        super().setUp()
        self.active_project_grant()
        self.guest = self.b_owner
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.guest).exists(),
            "accepting the grant should have admitted its approver to the project",
        )
        self.task = self.make_task("Shared work")
        self.team_b_id = str(self.team_b.team_id)
        self.team_a_id = str(self.team_a.team_id)

    def ask_as(self, user, task_id, team):
        self.authenticate(user)
        return self.ask(task_context={"task_id": task_id}, team_id=str(team.team_id))


class AGuestAsksFromTheirOwnTeamTests(SharedTaskWorld):
    def test_the_ask_finds_a_task_the_restore_route_finds(self):
        self.authenticate(self.guest)
        restore = self.restore(self.task.task_id, team=self.team_b)
        self.assertEqual(restore.status_code, 200)  # the restore aligns the team already
        resp, captured = self.ask_as(self.guest, self.task.task_id, self.team_b)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("THE TASK THIS CONVERSATION IS ABOUT", captured["kwargs"]["system_extra"])
        self.assertIn("Shared work", captured["kwargs"]["system_extra"])

    def test_the_loop_runs_in_the_tasks_own_team(self):
        _, captured = self.ask_as(self.guest, self.task.task_id, self.team_b)
        self.assertEqual(captured["ctx"].team_id, self.team_a_id)

    def test_the_session_the_run_and_the_cost_sit_with_the_tasks_team(self):
        _, captured = self.ask_as(self.guest, self.task.task_id, self.team_b)
        session = AgentSession.objects.get()
        self.assertEqual((session.team_id, session.user_id), (self.team_a_id, str(self.guest.id)))
        self.assertEqual(session.task_id, self.task.task_id)
        self.assertEqual(AgentRun.objects.get().team_id, self.team_a_id)
        self.assertEqual(captured["stream"]["spend_kwargs"]["team_id"], self.team_a_id)

    def test_the_ask_and_the_restore_then_agree_on_the_conversation(self):
        self.ask_as(self.guest, self.task.task_id, self.team_b)
        restored = self.restore(self.task.task_id, team=self.team_b).json()
        self.assertEqual(restored["agent_session_id"], str(AgentSession.objects.get().session_id))

    def test_the_second_ask_reuses_the_session(self):
        self.ask_as(self.guest, self.task.task_id, self.team_b)
        self.ask_as(self.guest, self.task.task_id, self.team_b)
        self.assertEqual(AgentSession.objects.count(), 1)

    def test_asking_with_the_hosts_team_gives_the_same_session(self):
        # What the client does today as a workaround: it sends the host team. Both end up on one session.
        self.ask_as(self.guest, self.task.task_id, self.team_b)
        resp, _ = self.ask_as(self.guest, self.task.task_id, self.team_a)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AgentSession.objects.count(), 1)

    def test_a_milestone_of_the_shared_project_too(self):
        backing, _ = self.make_milestone("Shared milestone", assignees=[self.guest])
        resp, captured = self.ask_as(self.guest, backing.task_id, self.team_b)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(
            "THE MILESTONE THIS CONVERSATION IS ABOUT", captured["kwargs"]["system_extra"]
        )

    def test_the_history_list_of_the_current_team_does_not_show_it(self):
        # Decision, not a fix: the conversation is recorded with the task's team, as the restore route finds it. The
        # History list of the team the person is working in lists only that team's sessions.
        self.ask_as(self.guest, self.task.task_id, self.team_b)
        listed = self.client.get("/api/v2/agent/sessions/", {"team_id": self.team_b_id})
        self.assertEqual(listed.status_code, 200, listed.content)
        self.assertEqual(listed.json().get("sessions", listed.json()), [])


class AnOrdinaryMemberIsUnchangedTests(TaskWorld):
    def test_a_member_of_the_tasks_own_team_asks_as_before(self):
        task = self.make_task()
        resp, captured = self.ask(task_context=self.ctx(task))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].team_id, str(self.team_a.team_id))
        self.assertEqual(AgentSession.objects.get().team_id, str(self.team_a.team_id))

    def test_the_helper_leaves_a_request_that_already_agrees_alone(self):
        task = self.make_task()
        team = str(self.team_a.team_id)
        got = foreign_team_scope.aligned_team("task", task.task_id, team, self.a_owner.id)
        self.assertEqual(got, team)


class ANamedTeamIsReplacedOnlyForATaskThePersonReachesTests(TaskWorld):
    def test_a_team_the_person_is_not_in_is_replaced_by_the_tasks_own_team(self):
        # As the restore route does (`test_agent_task_context_ask`): a substitution, never a grant, for a task they can open.
        task = self.make_task()
        resp, captured = self.ask(task_context=self.ctx(task), team_id=str(self.team_c.team_id))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].team_id, str(self.team_a.team_id))

    def test_a_substituted_team_the_person_may_not_enter_reads_as_a_missing_task(self):
        # They can reach the task (they are its assignee) but are no part of its team: the team was never named by
        # them, so the answer is the task's own words, as the restore route's, and not "Team not found.".
        foreign = self.make_task(
            "C's task", project=self.foreign_project, assignee=self.a_owner, reporter=self.c_owner
        )
        resp, _ = self.ask(task_context=self.ctx(foreign))
        missing, _ = self.ask(task_context={"task_id": MISSING_ID})
        self.assertEqual((resp.status_code, resp.json()), (404, missing.json()))
        self.assertEqual(missing.json(), {"error": task_context.NOT_FOUND})
        restored = self.restore(foreign.task_id)
        self.assertEqual((restored.status_code, restored.json()), (404, missing.json()))
        self.assertEqual(rows(), (0, 0, 0))

    def test_a_team_they_name_but_are_not_in_is_still_refused_by_name_for_a_task_they_cannot_reach(
        self,
    ):
        private = self.make_task(project=self.private_project())
        resp, _ = self.ask(task_context=self.ctx(private), team_id=str(self.team_c.team_id))
        self.assertEqual((resp.status_code, resp.json()), (404, {"error": "Team not found."}))


class ItGrantsNothingTests(SharedTaskWorld):
    """The substitution happens only for a task the caller reaches on its own terms; everything else is the one answer."""

    def missing(self, user, team):
        resp, _ = self.ask_as(user, MISSING_ID, team)
        self.assertEqual(resp.status_code, 404)
        return resp.json()

    def test_a_stranger_gets_the_answer_a_missing_task_gets(self):
        before = rows()
        missing = self.missing(self.c_owner, self.team_c)
        self.assertEqual(missing, {"error": task_context.NOT_FOUND})
        resp, _ = self.ask_as(self.c_owner, self.task.task_id, self.team_c)
        self.assertEqual((resp.status_code, resp.json()), (404, missing))
        self.assertEqual(rows(), before)

    def test_a_guest_off_the_project_gets_it_too(self):
        outsider = self.b_editor  # team B, but not the approver: no project row
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=outsider).exists()
        )
        missing = self.missing(outsider, self.team_b)
        resp, _ = self.ask_as(outsider, self.task.task_id, self.team_b)
        self.assertEqual((resp.status_code, resp.json()), (404, missing))
        self.assertEqual(rows(), (0, 0, 0))

    def test_a_person_of_a_team_that_only_shares_a_folder_gets_it_too(self):
        # A grant on a note folder or a chat is not the project: no `ProjectMembers` row, so no task.
        self.active_folder_grant()
        grantee = self._user("folder_grantee")
        TeamMembers.objects.create(team=self.team_b, attendee=grantee)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=grantee).exists()
        )
        missing = self.missing(grantee, self.team_b)
        resp, _ = self.ask_as(grantee, self.task.task_id, self.team_b)
        self.assertEqual((resp.status_code, resp.json()), (404, missing))
        self.assertEqual(rows(), (0, 0, 0))

    def test_a_deleted_task_and_one_in_a_private_project_are_that_answer_too(self):
        missing = self.missing(self.guest, self.team_b)
        TaskMaster.objects.filter(pk=self.task.pk).update(is_deleted=True)
        private = self.make_task("Private", project=self.private_project())
        for task_id in (self.task.task_id, private.task_id):
            with self.subTest(task=task_id):
                resp, _ = self.ask_as(self.guest, task_id, self.team_b)
                self.assertEqual((resp.status_code, resp.json()), (404, missing))
        self.assertEqual(rows(), (0, 0, 0))

    def test_the_restore_route_says_the_same_for_each(self):
        self.authenticate(self.c_owner)
        from_stranger = self.restore(self.task.task_id, team=self.team_c)
        self.authenticate(self.guest)
        from_missing = self.restore(MISSING_ID, team=self.team_b)
        self.assertEqual(
            (from_stranger.status_code, from_stranger.json()),
            (from_missing.status_code, from_missing.json()),
        )

    def test_the_helper_names_the_owner_only_for_a_task_the_person_reaches(self):
        team_b = self.team_b_id
        reached = foreign_team_scope.aligned_team("task", self.task.task_id, team_b, self.guest.id)
        self.assertEqual(reached, self.team_a_id)
        kept = foreign_team_scope.aligned_team("task", self.task.task_id, team_b, self.c_owner.id)
        self.assertEqual(kept, team_b)
        for not_a_task in (MISSING_ID, "not an id"):
            got = foreign_team_scope.aligned_team("task", not_a_task, team_b, self.guest.id)
            self.assertEqual(got, team_b, not_a_task)


class WithTheFlagOffTheTeamIsWhatTheClientSentTests(SharedTaskWorld):
    flag = False

    def test_the_task_context_is_ignored_and_so_is_the_alignment(self):
        resp, captured = self.ask_as(self.guest, self.task.task_id, self.team_b)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["ctx"].team_id, self.team_b_id)
        self.assertNotIn("THE TASK THIS CONVERSATION", captured["kwargs"].get("system_extra") or "")
        session = AgentSession.objects.get()
        self.assertEqual(session.team_id, self.team_b_id)
        self.assertIsNone(session.task_id)

    def test_the_same_whole_request_with_or_without_the_field(self):
        self.authenticate(self.guest)
        plain = self.ask(team_id=self.team_b_id)[1]
        with_field = self.ask(task_context={"task_id": self.task.task_id}, team_id=self.team_b_id)[
            1
        ]
        for key in ("system_extra", "seed_sources", "prior_turns"):
            self.assertEqual(plain["kwargs"].get(key), with_field["kwargs"].get(key), key)
        self.assertEqual(plain["ctx"].team_id, with_field["ctx"].team_id)

    def test_a_malformed_task_context_is_still_not_a_400(self):
        resp, _ = self.ask_as(self.guest, "not an id", self.team_b)
        self.assertEqual(resp.status_code, 200)


class TheFlagIsTheOnlySwitchTests(SharedTaskWorld):
    def test_flipping_it_off_and_on_changes_only_the_team(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: False}):
            off = self.ask_as(self.guest, self.task.task_id, self.team_b)[1]
        on = self.ask_as(self.guest, self.task.task_id, self.team_b)[1]
        self.assertEqual(off["ctx"].team_id, self.team_b_id)
        self.assertEqual(on["ctx"].team_id, self.team_a_id)
