"""`POST /agent/ask/` with `task_context`, and `GET /agent/task-session/`: a conversation per task (MIP-1057).

Who may ask about a task or milestone and who may get its conversation back, one session per (person, task), what the
view hands the loop, and the flag. The model is faked (`run_agent`), so what is asserted is what the VIEW does.

  * A task the asker cannot read reads like one that does not exist: the SAME status and body from `/ask/` and from the
    restore endpoint, and nothing written (no session, no run, no cost rollup). The twin tests run every way of being
    refused against the missing one.
  * The session is bound to the task (`AgentSession.task_id`), reused for that person and that task, never mixed with the
    general session or another task's, never reached by another person.
  * Flag off: `task_context` is read as the unknown field it always was, whatever it holds, and the endpoint is dark.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import task_context
from origin.search_engine.agent.mentions import ResolvedMention
from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.models import AgentRun, AgentSession, AiRequestCost

from .task_ask_fixtures import MISSING_ID, SECRET, TaskWorld, doc

BLOCK = "THE TASK THIS CONVERSATION IS ABOUT"
MILESTONE_BLOCK = "THE MILESTONE THIS CONVERSATION IS ABOUT"
REFERENCES = "USER-PROVIDED REFERENCES"


def rows():
    return (
        AgentSession.objects.count(),
        AgentRun.objects.count(),
        AiRequestCost.objects.count(),
    )


class RefusedAskTests(TaskWorld):
    """A task the asker may not read, against one that is missing."""

    def refused(self):
        refused = self.unreadable()
        refused.pop("missing")
        return refused

    def test_every_refusal_is_the_answer_a_missing_task_gets(self):
        before = rows()
        resp_missing, _ = self.ask(task_context={"task_id": MISSING_ID})
        self.assertEqual(resp_missing.status_code, 404)
        self.assertEqual(resp_missing.json(), {"error": task_context.NOT_FOUND})
        for name, task_id in self.refused().items():
            with self.subTest(name):
                resp, captured = self.ask(task_context={"task_id": task_id})
                self.assertEqual(resp.status_code, resp_missing.status_code)
                self.assertEqual(resp.content, resp_missing.content)
                self.assertNotIn("kwargs", captured, "the loop must not have started")
                self.assertNotIn(SECRET.encode(), resp.content)
        # NOTHING was written by any of them: no session, no run, no cost rollup.
        self.assertEqual(rows(), before)

    def test_a_refused_ask_spends_nothing_even_when_the_loop_would_have_run(self):
        before = rows()
        with mock.patch("origin.search_engine.agent_views.run_agent") as loop:
            for task_id in [*self.refused().values(), MISSING_ID]:
                self.client.post(
                    "/api/v2/agent/ask/",
                    {
                        "query": "what is this?",
                        "team_id": str(self.team_a.team_id),
                        "task_context": {"task_id": task_id},
                    },
                    format="json",
                )
        loop.assert_not_called()
        self.assertEqual(rows(), before)

    def test_a_team_the_asker_is_not_in_is_refused_before_the_task_is_looked_at(self):
        task = self.make_task()
        resp, _ = self.ask(task_context=self.ctx(task), team_id=str(self.team_c.team_id))
        # The ask's own answer for a team nobody may enter: it says nothing about any task.
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "Team not found."})

    def test_a_task_of_another_team_is_not_readable_through_a_team_the_asker_is_in(self):
        # The asker belongs to both teams; the task belongs to B. Asking in A names the wrong tenant.
        TeamMembers.objects.get_or_create(team=self.team_b, attendee=self.a_owner)
        foreign = self.make_task(
            "B's task", project=self.foreign_project, assignee=self.a_owner, reporter=self.a_owner
        )
        # Even as its assignee: it is in team C, not A.
        resp, _ = self.ask(task_context=self.ctx(foreign))
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": task_context.NOT_FOUND})


class WhoMayAskTests(TaskWorld):
    """The rule the model's own `fetch_task` applies, no wider (plus the milestone rule)."""

    def read(self, task):
        resp, captured = self.ask(task_context=self.ctx(task))
        return resp.status_code, captured

    def test_a_project_member_may(self):
        status, captured = self.read(self.make_task())
        self.assertEqual(status, 200)
        self.assertIn(BLOCK, captured["kwargs"]["system_extra"])

    def test_the_assignee_off_the_project_may_while_still_on_the_team(self):
        private = self.private_project()
        task = self.make_task(project=private, assignee=self.a_owner)
        self.assertEqual(self.read(task)[0], 200)

    def test_the_reporter_off_the_project_may_while_still_on_the_team(self):
        private = self.private_project()
        task = self.make_task(project=private, reporter=self.a_owner)
        self.assertEqual(self.read(task)[0], 200)

    def test_someone_who_is_only_a_collaborator_may_not(self):
        # MIP-1567: a collaborator who cannot open the task is not a reader.
        private = self.private_project()
        task = self.make_task(project=private)
        task.collaborators.add(self.a_owner)
        self.assertEqual(self.read(task)[0], 404)

    def test_a_member_removed_from_the_project_may_not_any_more(self):
        task = self.make_task()
        self.assertEqual(self.read(task)[0], 200)
        ProjectMembers.objects.filter(project=self.project, attendee=self.a_owner).delete()
        self.assertEqual(self.read(task)[0], 404)

    def test_a_task_deleted_since_may_not_be_asked_about(self):
        task = self.make_task()
        self.assertEqual(self.read(task)[0], 200)
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(self.read(task)[0], 404)

    def test_a_guest_on_the_project_may_and_one_off_it_may_not(self):
        # A guest holds a project row and no team row: they take part in the team through the project.
        guest = self._user("guest_on_project")
        task = self.make_task()
        self.join(guest)
        self.authenticate(guest)
        self.assertEqual(self.read(task)[0], 200)
        other = self._user("guest_elsewhere")
        self.join(other, self.private_project())
        self.authenticate(other)
        self.assertEqual(self.read(task)[0], 404)

    def test_the_milestone_assignee_off_the_project_may_not_ask_about_a_milestone(self):
        # The milestone endpoints are for the project's members; the row's assignee or reporter may open the
        # ROW as a task, but asking about the milestone is the narrower door.
        private = self.private_project()
        backing, _ = self.make_milestone(project=private, assignee=self.a_owner)
        self.assertEqual(self.read(backing)[0], 404)

    def test_a_milestone_is_asked_about_by_its_backing_task(self):
        backing, milestone = self.make_milestone(assignees=[self.a_editor])
        status, captured = self.read(backing)
        self.assertEqual(status, 200)
        extra = captured["kwargs"]["system_extra"]
        self.assertIn(MILESTONE_BLOCK, extra)
        self.assertIn(f"milestone_id: {milestone.milestone_id}", extra)
        self.assertIn(f"get_milestone_summary(milestone_id={milestone.milestone_id})", extra)
        session = AgentSession.objects.get()
        self.assertEqual(session.task_id, backing.task_id)  # the row id: there is no milestone column

    def test_a_milestone_turned_back_into_a_task_keeps_its_conversation(self):
        backing, milestone = self.make_milestone()
        self.ask(task_context=self.ctx(backing))
        first = AgentSession.objects.get()
        # MIP-1042: the conversion keeps the row and retires the milestone.
        TaskMaster.objects.filter(pk=backing.pk).update(is_milestone=False)
        milestone.is_deleted = True
        milestone.save(update_fields=["is_deleted"])
        resp, captured = self.ask(task_context=self.ctx(backing))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AgentSession.objects.get().pk, first.pk)
        self.assertIn(BLOCK, captured["kwargs"]["system_extra"])  # a task now
        self.assertNotIn(MILESTONE_BLOCK, captured["kwargs"]["system_extra"])


class RestoreTests(TaskWorld):
    def ask_about(self, task, **extra):
        resp, captured = self.ask(task_context=self.ctx(task), **extra)
        self.assertEqual(resp.status_code, 200)
        return captured

    def answer(self, run_id, text="the answer", status="done"):
        AgentRun.objects.filter(run_id=run_id).update(final_answer_text=text, status=status)

    def test_nothing_asked_yet_is_two_empty_values(self):
        resp = self.restore(self.make_task().task_id)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"agent_session_id": None, "turns": []})

    def test_what_was_asked_about_the_task_comes_back(self):
        task = self.make_task()
        captured = self.ask_about(task, query="when is it due?")
        run = captured["stream"]["run"]
        self.answer(run.run_id, "It is due on Friday.")
        body = self.restore(task.task_id).json()
        session = AgentSession.objects.get()
        self.assertEqual(body["agent_session_id"], str(session.session_id))
        self.assertEqual([t["query"] for t in body["turns"]], ["when is it due?"])
        self.assertEqual([t["answer"] for t in body["turns"]], ["It is due on Friday."])
        self.assertEqual(set(body), {"agent_session_id", "turns"})

    def test_the_turns_are_the_ones_the_sessions_endpoint_returns(self):
        task = self.make_task()
        run = self.ask_about(task)["stream"]["run"]
        self.answer(run.run_id)
        session = AgentSession.objects.get()
        detail = self.client.get(
            f"/api/v2/agent/sessions/{session.session_id}/", {"team_id": str(self.team_a.team_id)}
        ).json()
        self.assertEqual(self.restore(task.task_id).json()["turns"], detail["turns"])

    def test_it_is_the_newest_of_several_conversations_on_the_task(self):
        task = self.make_task()
        first = self.ask_about(task)["stream"]["session_id"]
        second = self.ask_about(task, new_conversation=True)["stream"]["session_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.restore(task.task_id).json()["agent_session_id"], str(second))

    def test_another_task_has_its_own_conversation(self):
        one, two = self.make_task("One"), self.make_task("Two")
        self.ask_about(one, query="about one")
        self.assertEqual(self.restore(two.task_id).json(), {"agent_session_id": None, "turns": []})

    def test_the_general_conversation_is_not_a_task_conversation(self):
        task = self.make_task()
        general = AgentSession.objects.create(
            team_id=str(self.team_a.team_id), user_id=str(self.a_owner.id)
        )
        self.assertEqual(self.restore(task.task_id).json()["agent_session_id"], None)
        self.assertEqual(AgentSession.objects.get().pk, general.pk)

    def test_someone_elses_conversation_is_never_handed_over(self):
        task = self.make_task()
        self.join(self.a_editor)
        self.ask_about(task, query="the owner's private question")
        self.authenticate(self.a_editor)
        body = self.restore(task.task_id).json()
        self.assertEqual(body, {"agent_session_id": None, "turns": []})
        self.assertNotIn("private question", json.dumps(body))

    def test_a_session_of_another_team_is_not_returned(self):
        task = self.make_task()
        AgentSession.objects.create(
            team_id=str(self.team_c.team_id), user_id=str(self.a_owner.id), task_id=task.task_id
        )
        self.assertEqual(self.restore(task.task_id).json()["agent_session_id"], None)

    def test_a_task_that_cannot_be_read_gives_back_nothing_of_the_past(self):
        task = self.make_task()
        run = self.ask_about(task)["stream"]["run"]
        self.answer(run.run_id, SECRET)
        ProjectMembers.objects.filter(project=self.project, attendee=self.a_owner).delete()
        resp = self.restore(task.task_id)
        self.assertEqual(resp.status_code, 404)
        self.assertNotIn(SECRET, resp.content.decode())

    def test_every_refusal_is_the_answer_a_missing_task_gets(self):
        missing = self.restore(MISSING_ID)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json(), {"error": task_context.NOT_FOUND})
        refused = self.unreadable()
        refused.pop("missing")
        for name, task_id in refused.items():
            with self.subTest(name):
                resp = self.restore(task_id)
                self.assertEqual((resp.status_code, resp.content), (404, missing.content))

    def test_a_team_the_caller_is_not_in_is_the_same_answer_too(self):
        # A task the caller cannot reach: the platform's team alignment (below) does not apply, so the
        # team they name is the team that is checked, and they are not in it.
        private = self.private_project()
        task = self.make_task(project=private)
        resp = self.restore(task.task_id, team=self.team_c)
        self.assertEqual((resp.status_code, resp.json()), (404, {"error": task_context.NOT_FOUND}))

    def test_the_platforms_team_alignment_applies_to_a_task_the_caller_reaches(self):
        # `AuthenticatedAPIView` rewrites a `team_id` that names the caller's CURRENT team to the team that
        # OWNS an object they can reach (a guest in a shared project's host team). A substitution, never
        # a grant: it happens only for a task the caller may open, and the answer is then that task's own
        # team's conversation, which the caller may read anyway.
        task = self.make_task()
        self.ask(task_context=self.ctx(task))
        body = self.restore(task.task_id, team=self.team_c).json()
        self.assertEqual(body["agent_session_id"], str(AgentSession.objects.get().session_id))

    def test_a_malformed_request_is_a_400(self):
        task = self.make_task()
        team = str(self.team_a.team_id)
        for params in (
            {"task_id": task.task_id},
            {"team_id": team},
            {"team_id": team, "task_id": "abc"},
            {"team_id": team, "task_id": "-3"},
            {"team_id": team, "task_id": "0"},
            {"team_id": team, "task_id": str(2**64)},
            {"team_id": team, "task_id": "HOST-1"},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get("/api/v2/agent/task-session/", params).status_code, 400)

    def test_it_needs_a_signed_in_caller_and_writes_nothing(self):
        task = self.make_task()
        before = rows()
        self.restore(task.task_id)
        self.assertEqual(rows(), before)
        self.client.credentials()
        self.assertIn(self.restore(task.task_id).status_code, (401, 403))

    def test_a_post_is_not_a_restore(self):
        resp = self.client.post("/api/v2/agent/task-session/", {}, format="json")
        self.assertEqual(resp.status_code, 405)


class SessionScopeTests(TaskWorld):
    def ask_about(self, task, **extra):
        resp, captured = self.ask(task_context=self.ctx(task), **extra)
        self.assertEqual(resp.status_code, 200)
        return captured

    def test_the_first_ask_makes_a_session_bound_to_the_task(self):
        task = self.make_task()
        captured = self.ask_about(task)
        session = AgentSession.objects.get()
        self.assertEqual(
            (session.task_id, session.team_id, session.user_id),
            (task.task_id, str(self.team_a.team_id), str(self.a_owner.id)),
        )
        self.assertEqual(captured["kwargs"]["session_id"], str(session.session_id))
        self.assertEqual(AgentRun.objects.get().session_id, session.session_id)
        # And no other scope column.
        self.assertIsNone(session.chat_id)
        self.assertIsNone(session.note_id)

    def test_asking_again_about_the_same_task_is_the_same_session_without_a_session_id(self):
        task = self.make_task()
        self.ask_about(task)
        self.ask_about(task, query="and who owns it?")
        self.assertEqual(AgentSession.objects.count(), 1)
        self.assertEqual(AgentRun.objects.filter(session=AgentSession.objects.get()).count(), 2)

    def test_a_second_task_does_not_share_the_first_ones_conversation(self):
        one, two = self.make_task("One"), self.make_task("Two")
        self.ask_about(one)
        self.ask_about(two)
        self.assertEqual(
            sorted(AgentSession.objects.values_list("task_id", flat=True)),
            sorted([one.task_id, two.task_id]),
        )

    def test_the_general_session_is_set_aside_and_left_untouched(self):
        task = self.make_task()
        general = AgentSession.objects.create(
            team_id=str(self.team_a.team_id), user_id=str(self.a_owner.id)
        )
        stamp = AgentSession.objects.get(pk=general.pk).last_active_at
        captured = self.ask_about(task, session_id=str(general.session_id))
        self.assertNotEqual(captured["kwargs"]["session_id"], str(general.session_id))
        general.refresh_from_db()
        self.assertEqual(general.last_active_at, stamp)
        self.assertIsNone(general.task_id)
        self.assertFalse(AgentRun.objects.filter(session=general).exists())
        self.assertEqual(AgentSession.objects.filter(task_id=task.task_id).count(), 1)

    def test_another_tasks_session_id_is_set_aside_too(self):
        one, two = self.make_task("One"), self.make_task("Two")
        first = self.ask_about(one)["kwargs"]["session_id"]
        second = self.ask_about(two, session_id=first)["kwargs"]["session_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(AgentSession.objects.get(session_id=second).task_id, two.task_id)

    def test_a_thread_or_note_session_id_is_set_aside_too(self):
        task = self.make_task()
        thread = AgentSession.objects.create(
            team_id=str(self.team_a.team_id),
            user_id=str(self.a_owner.id),
            chat_type=2,
            chat_id=uuid.uuid4(),
            thread_id=uuid.uuid4(),
        )
        captured = self.ask_about(task, session_id=str(thread.session_id))
        self.assertNotEqual(captured["kwargs"]["session_id"], str(thread.session_id))

    def test_its_own_session_id_is_reused(self):
        task = self.make_task()
        first = self.ask_about(task)["kwargs"]["session_id"]
        again = self.ask_about(task, session_id=first)["kwargs"]["session_id"]
        self.assertEqual(first, again)
        self.assertEqual(AgentSession.objects.count(), 1)

    def test_a_new_conversation_starts_another_session_on_the_same_task(self):
        task = self.make_task()
        first = self.ask_about(task)["kwargs"]["session_id"]
        second = self.ask_about(task, new_conversation=True)["kwargs"]["session_id"]
        self.assertNotEqual(first, second)
        self.assertEqual(AgentSession.objects.filter(task_id=task.task_id).count(), 2)
        # The next ask without a session id continues the NEWEST, the one `restore` returns.
        self.assertEqual(self.ask_about(task)["kwargs"]["session_id"], second)

    def test_a_conversation_about_a_task_does_not_expire_like_a_plain_one(self):
        task = self.make_task()
        first = self.ask_about(task)["kwargs"]["session_id"]
        AgentSession.objects.filter(pk=first).update(
            last_active_at=timezone.now() - timedelta(days=3)
        )
        # By id (the TTL bypass) and by the task (the scope lookup).
        self.assertEqual(self.ask_about(task, session_id=first)["kwargs"]["session_id"], first)
        AgentSession.objects.filter(pk=first).update(
            last_active_at=timezone.now() - timedelta(days=3)
        )
        self.assertEqual(self.ask_about(task)["kwargs"]["session_id"], first)

    def test_another_member_asking_about_the_same_task_gets_their_own_conversation(self):
        task = self.make_task()
        self.join(self.a_editor)
        mine = self.ask_about(task)["kwargs"]["session_id"]
        self.authenticate(self.a_editor)
        theirs = self.ask_about(task)["kwargs"]["session_id"]
        self.assertNotEqual(mine, theirs)
        # Even with the other person's session id in hand.
        stolen = self.ask_about(task, session_id=mine)["kwargs"]["session_id"]
        self.assertEqual(stolen, theirs)
        self.assertEqual(AgentSession.objects.get(session_id=mine).user_id, str(self.a_owner.id))
        self.assertFalse(AgentRun.objects.filter(session_id=mine, user_id=str(self.a_editor.id)).exists())

    def test_a_session_of_another_team_is_not_reused(self):
        task = self.make_task()
        elsewhere = AgentSession.objects.create(
            team_id=str(self.team_c.team_id), user_id=str(self.a_owner.id), task_id=task.task_id
        )
        captured = self.ask_about(task)
        self.assertNotEqual(captured["kwargs"]["session_id"], str(elsewhere.session_id))

    def test_the_session_is_not_created_for_a_task_that_cannot_be_read(self):
        before = AgentSession.objects.count()
        self.ask(task_context={"task_id": MISSING_ID})
        self.assertEqual(AgentSession.objects.count(), before)

    # -- continuing a task's conversation without naming the task ------------

    def test_a_session_id_alone_keeps_the_conversation_about_the_task(self):
        task = self.make_task("Fix the login flow")
        first = self.ask_about(task)["kwargs"]["session_id"]
        resp, captured = self.ask(session_id=first, resume=True, query="and the owner?")
        self.assertEqual(resp.status_code, 200)
        extra = captured["kwargs"]["system_extra"]
        self.assertIn(BLOCK, extra)
        self.assertIn("Fix the login flow", extra)
        self.assertEqual(captured["kwargs"]["session_id"], first)

    def test_a_task_that_cannot_be_read_any_more_is_dropped_from_such_a_continuation_quietly(self):
        task = self.make_task()
        first = self.ask_about(task)["kwargs"]["session_id"]
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        resp, captured = self.ask(session_id=first, query="and the owner?")
        self.assertEqual(resp.status_code, 200)  # nothing was named, so nothing is refused
        self.assertIsNone(captured["kwargs"]["system_extra"])

    def test_a_plain_session_continued_by_id_gets_no_block(self):
        general = AgentSession.objects.create(
            team_id=str(self.team_a.team_id), user_id=str(self.a_owner.id)
        )
        _, captured = self.ask(session_id=str(general.session_id))
        self.assertIsNone(captured["kwargs"]["system_extra"])


class AskShapeTests(TaskWorld):
    def test_the_block_reaches_the_model_in_the_slot_of_a_summary_and_the_task_is_the_chip(self):
        task = self.make_task("Fix the login flow")
        resp, captured = self.ask(task_context=self.ctx(task))
        self.assertEqual(resp.status_code, 200)
        extra = captured["kwargs"]["system_extra"]
        self.assertTrue(extra.startswith(BLOCK))
        self.assertIn("Title: Fix the login flow", extra)
        chip = captured["kwargs"]["seed_sources"][0]
        self.assertEqual((chip["entity_type"], chip["task_id"]), ("task", str(task.task_id)))

    def test_the_run_row_holds_nothing_of_the_task(self):
        task = self.make_task("Fix the login flow", content=doc(SECRET))
        self.ask(task_context=self.ctx(task))
        run = AgentRun.objects.get()
        self.assertNotIn(SECRET, json.dumps([run.query, run.mentions, run.resume_context]))
        self.assertNotIn("Fix the login flow", json.dumps([run.mentions, run.resume_context]))

    def test_a_mention_of_the_same_task_adds_no_second_bullet_but_keeps_everything_else(self):
        task = self.make_task("Fix the login flow")
        other = self.make_task("Another task")
        same = ResolvedMention(
            kind="task",
            label="Fix the login flow",
            task_id=task.task_id,
            display_id=task.display_id,
            project_id=str(self.project.project_id),
        )
        different = ResolvedMention(
            kind="task",
            label="Another task",
            task_id=other.task_id,
            display_id=other.display_id,
            project_id=str(self.project.project_id),
        )
        mentions = [
            {"type": "task", "task_id": task.task_id, "label": "x"},
            {"type": "task", "task_id": other.task_id, "label": "y"},
        ]
        with mock.patch(
            "origin.search_engine.agent_views.resolve_mentions", return_value=[same, different]
        ):
            _, captured = self.ask(task_context=self.ctx(task), mentions=mentions)
        extra = captured["kwargs"]["system_extra"]
        bullets = extra.split(REFERENCES)[1]
        self.assertIn(f"task:{other.task_id}", bullets)
        self.assertNotIn(f"task:{task.task_id}", bullets)
        # The boost still sees both, and the task is ONE chip (the loop dedupes by entity id).
        self.assertEqual(len(captured["ctx"].resolved_mentions), 2)

    def test_a_mention_of_the_task_alone_leaves_no_empty_references_block(self):
        task = self.make_task()
        same = ResolvedMention(
            kind="task",
            label=task.title,
            task_id=task.task_id,
            display_id=task.display_id,
            project_id=str(self.project.project_id),
        )
        with mock.patch("origin.search_engine.agent_views.resolve_mentions", return_value=[same]):
            _, captured = self.ask(
                task_context=self.ctx(task), mentions=[{"type": "task", "task_id": task.task_id}]
            )
        self.assertNotIn(REFERENCES, captured["kwargs"]["system_extra"])

    def test_a_mention_without_a_task_context_is_what_it_was(self):
        task = self.make_task()
        same = ResolvedMention(
            kind="task",
            label=task.title,
            task_id=task.task_id,
            display_id=task.display_id,
            project_id=str(self.project.project_id),
        )
        with mock.patch("origin.search_engine.agent_views.resolve_mentions", return_value=[same]):
            _, captured = self.ask(mentions=[{"type": "task", "task_id": task.task_id}])
        extra = captured["kwargs"]["system_extra"]
        self.assertIn(REFERENCES, extra)
        self.assertNotIn(BLOCK, extra)

    def test_the_block_is_followed_by_the_blocks_of_the_ask_as_before(self):
        task = self.make_task()
        _, captured = self.ask(task_context=self.ctx(task), selected_text="the passage")
        extra = captured["kwargs"]["system_extra"]
        self.assertLess(extra.index(BLOCK), extra.index("TEXT THE USER SELECTED"))

    def test_the_context_is_exclusive_with_a_thread_or_a_note(self):
        task = self.make_task()
        thread = {"chat_type": 2, "chat_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4())}
        note = {"note_type": 1, "note_id": 1}
        before = rows()
        for extra in ({"thread_context": thread}, {"note_context": note}):
            with self.subTest(extra=list(extra)):
                resp, _ = self.ask(task_context=self.ctx(task), **extra)
                self.assertEqual(resp.status_code, 400)
                self.assertIn("task_context", resp.json()["error"])
        self.assertEqual(rows(), before)

    def test_a_thread_and_a_note_together_are_refused_in_the_words_they_always_were(self):
        thread = {"chat_type": 2, "chat_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4())}
        resp, _ = self.ask(thread_context=thread, note_context={"note_type": 1, "note_id": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(
            resp.json(), {"error": "thread_context and note_context are mutually exclusive."}
        )

    def test_a_malformed_context_is_a_400_before_anything_is_spent(self):
        before = rows()
        for bad in (
            "5",
            [1],
            {"task_id": "abc"},
            {"task_id": None, "x": 1},
            {"task_id": True},
            {"task_id": 0},
            {"task_id": -4},
            {"task_id": 2**63},
            {"task_id": 1.5},
            {"task_id": {"a": 1}},
            {"task_id": [3]},
            {"display_id": "HOST-1"},
            {"milestone_id": 3},
        ):
            with self.subTest(bad=bad):
                resp, _ = self.ask(task_context=bad)
                self.assertEqual(resp.status_code, 400)
                self.assertEqual(resp.json(), {"error": task_context.MALFORMED})
        self.assertEqual(rows(), before)

    def test_a_numeric_string_is_a_task_id_and_an_empty_context_is_none(self):
        task = self.make_task()
        resp, captured = self.ask(task_context={"task_id": str(task.task_id)})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(BLOCK, captured["kwargs"]["system_extra"])
        for empty in (None, {}, 0, False, ""):
            with self.subTest(empty=empty):
                resp, captured = self.ask(task_context=empty)
                self.assertEqual(resp.status_code, 200)
                self.assertIsNone(captured["kwargs"]["system_extra"])

    def test_the_client_cannot_make_a_task_a_milestone_or_name_its_title(self):
        task = self.make_task("The real title")
        _, captured = self.ask(
            task_context={"task_id": task.task_id, "kind": "milestone", "title": "FORGED TITLE"}
        )
        extra = captured["kwargs"]["system_extra"]
        self.assertIn(BLOCK, extra)
        self.assertNotIn(MILESTONE_BLOCK, extra)
        self.assertNotIn("FORGED TITLE", extra)
        self.assertNotIn("FORGED TITLE", json.dumps(captured["kwargs"]["seed_sources"]))

    def test_the_stream_the_view_builds_has_no_new_field(self):
        task = self.make_task()
        _, with_ctx = self.ask(task_context=self.ctx(task))
        _, without = self.ask()
        self.assertEqual(set(with_ctx["stream"]), set(without["stream"]))


class FlagOffTests(TaskWorld):
    flag = False

    def test_the_field_is_read_as_the_unknown_one_it_always_was(self):
        task = self.make_task()
        before = AgentSession.objects.count()
        for value in (
            self.ctx(task),
            {"task_id": MISSING_ID},  # not even looked up
            {"task_id": "abc"},  # not even type-checked
            "not an object",
            [1, 2],
        ):
            with self.subTest(value=value):
                resp, captured = self.ask(task_context=value)
                self.assertEqual(resp.status_code, 200)
                self.assertIsNone(captured["kwargs"]["system_extra"])
                self.assertIsNone(captured["kwargs"].get("seed_sources"))
        # Plain sessions, bound to nothing.
        self.assertEqual(AgentSession.objects.filter(task_id__isnull=False).count(), 0)
        self.assertGreater(AgentSession.objects.count(), before)

    def test_it_does_not_even_clash_with_a_thread_or_a_note(self):
        task = self.make_task()
        thread = {"chat_type": 2, "chat_id": str(uuid.uuid4()), "thread_id": str(uuid.uuid4())}
        with mock.patch(
            "origin.search_engine.agent_views.load_or_generate_for_ask", return_value="a summary"
        ):
            resp, captured = self.ask(task_context=self.ctx(task), thread_context=thread)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("<thread_summary>", captured["kwargs"]["system_extra"])
        self.assertNotIn(BLOCK, captured["kwargs"]["system_extra"])

    def test_the_request_is_the_same_with_and_without_it(self):
        task = self.make_task()
        _, plain = self.ask()
        _, with_ctx = self.ask(task_context=self.ctx(task))
        for key in ("system_extra", "seed_sources", "prior_turns", "disabled_tools"):
            self.assertEqual(plain["kwargs"].get(key), with_ctx["kwargs"].get(key), key)

    def test_a_session_bound_to_a_task_is_just_a_session_while_the_flag_is_off(self):
        task = self.make_task()
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            first = self.ask(task_context=self.ctx(task))[1]["kwargs"]["session_id"]
        resp, captured = self.ask(session_id=first, query="and the owner?")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(captured["kwargs"]["system_extra"])  # no block from a session

    def test_the_endpoint_is_dark_and_the_same_for_every_task(self):
        task = self.make_task()
        with mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True}):
            self.ask(task_context=self.ctx(task))
        empty = {"agent_session_id": None, "turns": []}
        for task_id in (task.task_id, MISSING_ID, *self.unreadable().values()):
            resp = self.restore(task_id)
            self.assertEqual((resp.status_code, resp.json()), (200, empty), task_id)
        # A malformed request is still a 400, flag or no flag.
        resp = self.client.get(
            "/api/v2/agent/task-session/", {"team_id": str(self.team_a.team_id), "task_id": "x"}
        )
        self.assertEqual(resp.status_code, 400)


class ReadTaskRuleTests(TaskWorld):
    """`task_context.read_task` itself: the one rule, called with every kind of caller."""

    def test_the_assignee_who_left_the_team_is_not_a_reader(self):
        # MIP-1371: the assignee outside the project counts only while still on the team.
        leaver = self._user("leaver")
        TeamMembers.objects.create(team=self.team_a, attendee=leaver, is_deleted=False)
        private = self.private_project()
        task = self.make_task(project=private, assignee=leaver)
        args = (task.task_id, str(self.team_a.team_id), str(leaver.id))
        self.assertIsNotNone(task_context.read_task(*args))
        TeamMembers.objects.filter(team=self.team_a, attendee=leaver).update(is_deleted=True)
        self.assertIsNone(task_context.read_task(*args))
        self.assertFalse(task_context.can_read(*args))

    def test_can_read_is_the_rule_of_read_task(self):
        good = self.make_task()
        names = self.unreadable()
        team, user = str(self.team_a.team_id), str(self.a_owner.id)
        for name, task_id in [("readable", good.task_id), *names.items()]:
            with self.subTest(name):
                self.assertEqual(
                    task_context.can_read(task_id, team, user),
                    task_context.read_task(task_id, team, user) is not None,
                )

    def test_junk_ids_and_empty_callers_read_nothing(self):
        task = self.make_task()
        team, user = str(self.team_a.team_id), str(self.a_owner.id)
        for junk in (None, "", "abc", True, False, 0, -1, 2**63, 1.5, [], {}):
            with self.subTest(task_id=junk):
                self.assertIsNone(task_context.read_task(junk, team, user))
        self.assertIsNone(task_context.read_task(task.task_id, "", user))
        self.assertIsNone(task_context.read_task(task.task_id, team, ""))
        self.assertIsNone(task_context.read_task(task.task_id, None, None))

    def test_the_team_must_be_the_tasks_own(self):
        task = self.make_task()
        user = str(self.a_owner.id)
        self.assertIsNotNone(task_context.read_task(task.task_id, str(self.team_a.team_id), user))
        self.assertIsNone(task_context.read_task(task.task_id, str(self.team_c.team_id), user))
        self.assertIsNone(task_context.read_task(task.task_id, "no-such-team", user))
