"""Conversations about a thread, note or task: what a refused ask leaves behind, which session an ask may
continue, and who may read a past conversation (MIP-1590, items 2, 3, 4 and 7).

  * 2: an ask from a thread or note the asker may not open is refused BEFORE a cost rollup, a session or a
    run row is written (as an ask about a task always was). The leftover `running` run it used to leave made
    "delete all my conversations" answer 409 for two hours.
  * 3: a session id is continued under a thread or note context only if the session is bound to exactly that
    thread or note. A general session, another object's or a task's is set aside and left as it was.
  * 4: the history list and `/agent/sessions/<id>/` check the object again: a conversation about something the
    person can no longer open is left out of the list and answers like a missing session. An ask continued with
    the session id alone does not hand the earlier turns to the model again. Deleting and pinning stay open.
  * 7: each row of the list says what it is about, as ids only.

Every other behaviour of the ask is the one the neighbouring suites pin; these are only the changes.
"""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent import note_summary, session_scope
from origin.search_engine.agent.thread_summary import ThreadSummaryError
from origin.search_engine.models import AgentRun, AgentSession, AiRequestCost

from .task_ask_fixtures import SECRET, TaskWorld, doc

SESSIONS_URL = "/api/v2/agent/sessions/"
SUMMARY = "A summary of the thread."
MISSING_NOTE = 987654321


def rows():
    return (
        AgentSession.objects.count(),
        AgentRun.objects.count(),
        AiRequestCost.objects.count(),
    )


class World(TaskWorld):
    """The asker is `a_owner`; the model and its summaries are faked (nothing here calls a provider)."""

    def setUp(self):
        super().setUp()
        for target in (
            "origin.search_engine.agent.thread_summary.summarise_thread",
            "origin.search_engine.agent.note_summary.summarise_note",
        ):
            patcher = mock.patch(target, return_value=(SUMMARY, "test:model"))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.team_id = str(self.team_a.team_id)
        self.me = str(self.a_owner.id)

    # -- the objects a conversation can be about -------------------------------

    def thread(self, members=None, replies=True):
        """`(context, channel)` of a thread in a group chat the asker is in (by default)."""
        channel = self.group("Launch", members or [self.a_owner, self.a_editor])
        root = self.say(channel, "when do we launch?")
        if replies:
            self.say(channel, "Friday", reply_to=root)
        return {"chat_type": 2, "chat_id": str(channel.id), "thread_id": str(root.id)}, channel

    def note(self, owner=None, title="Q3 plan", **kw):
        """`(context, note)` of a personal note (the asker's by default)."""
        note = PersonalNoteMaster.objects.create(
            team=self.team_a,
            owner=owner or self.a_owner,
            title=title,
            body=kw.pop("body", doc("migration first")),
            **kw,
        )
        return {"note_type": 1, "note_id": note.note_id}, note

    def task_note(self, project=None):
        project = project or self.project
        task = self.make_task("A task", project=project)
        note = TaskNoteMaster.objects.create(
            team=self.team_a,
            owner=self.a_editor,
            project=project,
            task=task,
            title="Task note",
            body=doc("the plan"),
        )
        return {"note_type": 2, "note_id": note.note_id}, note

    # -- sessions --------------------------------------------------------------

    def past(self, *, thread=None, note=None, task=None, query="What was decided?"):
        """A session with one answered run, bound to the thread, note or task context given (or to none)."""
        scope = {}
        if thread:
            scope = {
                "chat_type": thread["chat_type"],
                "chat_id": thread["chat_id"],
                "thread_id": thread["thread_id"],
            }
        elif note:
            scope = {"note_type": note["note_type"], "note_id": note["note_id"]}
        elif task is not None:
            scope = {"task_id": task.task_id}
        session = AgentSession.objects.create(team_id=self.team_id, user_id=self.me, **scope)
        AgentRun.objects.create(
            team_id=self.team_id,
            user_id=self.me,
            query=query,
            session=session,
            status="done",
            final_answer_text="The answer.",
        )
        return session

    def session_of(self, captured):
        return captured["kwargs"]["session_id"]

    def detail(self, session_id):
        return self.client.get(f"{SESSIONS_URL}{session_id}/", {"team_id": self.team_id})

    def history(self):
        resp = self.client.get(SESSIONS_URL, {"team_id": self.team_id})
        self.assertEqual(resp.status_code, 200)
        return resp.json()["sessions"]

    def delete_all(self):
        return self.client.delete(f"{SESSIONS_URL}?team_id={self.team_id}")

    # -- losing access ---------------------------------------------------------

    def leave(self, channel):
        ChannelMember.objects.filter(channel=channel, user=self.a_owner).update(is_deleted=True)


# --------------------------------------------------------------------------- #
# 2. A refused ask writes nothing                                              #
# --------------------------------------------------------------------------- #


class RefusedThreadAskTests(World):
    def refused(self):
        out = {}
        theirs = self.group("Theirs", [self.a_editor, self.a_viewer])
        root = self.say(theirs, SECRET)
        self.say(theirs, SECRET, reply_to=root)
        out["not a member"] = {
            "chat_type": 2,
            "chat_id": str(theirs.id),
            "thread_id": str(root.id),
        }
        context, channel = self.thread()
        self.leave(channel)
        out["left the chat"] = context
        context, channel = self.thread()
        Channel.objects.filter(pk=channel.pk).update(is_deleted=True)
        out["chat deleted"] = context
        out["missing"] = {
            "chat_type": 2,
            "chat_id": str(uuid.uuid4()),
            "thread_id": str(uuid.uuid4()),
        }
        return out

    def test_every_refusal_is_a_403_in_the_words_of_a_missing_chat_and_writes_nothing(self):
        refused = self.refused()
        before = rows()
        missing_resp, _ = self.ask(thread_context=refused.pop("missing"))
        self.assertEqual(missing_resp.status_code, 403)
        for name, context in refused.items():
            with self.subTest(name):
                resp, captured = self.ask(thread_context=context)
                self.assertEqual(resp.status_code, 403)
                self.assertEqual(resp.content, missing_resp.content)
                self.assertNotIn("kwargs", captured, "the loop must not have started")
                self.assertNotIn(SECRET.encode(), resp.content)
        # No session, no run, no cost rollup.
        self.assertEqual(rows(), before)

    def test_the_conversations_can_still_be_deleted_afterwards(self):
        # The visible symptom: a leftover `running` run made this 409 until it went stale.
        for context in self.refused().values():
            self.ask(thread_context=context)
        self.assertEqual(self.delete_all().status_code, 200)

    def test_an_empty_thread_is_a_400_and_writes_nothing(self):
        context, _ = self.thread(replies=False)
        before = rows()
        resp, captured = self.ask(thread_context=context)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("empty", resp.json()["error"])
        self.assertNotIn("kwargs", captured)
        self.assertEqual(rows(), before)

    def test_a_malformed_context_is_still_a_400_and_writes_nothing(self):
        before = rows()
        for bad in (
            {"thread_context": {"chat_type": "x", "chat_id": "a", "thread_id": "b"}},
            {"note_context": {"note_type": 1}},
            {
                "thread_context": {"chat_type": 2, "chat_id": "a", "thread_id": "b"},
                "note_context": {"note_type": 1, "note_id": 2},
            },
        ):
            with self.subTest(bad):
                resp, _ = self.ask(**bad)
                self.assertEqual(resp.status_code, 400)
        self.assertEqual(rows(), before)

    def test_a_thread_the_asker_may_open_is_asked_as_before(self):
        context, _ = self.thread()
        for flag in (True, False):
            with self.subTest(flag=flag):
                AgentSession.objects.all().delete()
                resp, captured = self.ask(thread_context=context, flag=flag)
                self.assertEqual(resp.status_code, 200)
                session = AgentSession.objects.get()
                self.assertEqual(self.session_of(captured), str(session.session_id))
                self.assertEqual(str(session.thread_id), context["thread_id"])
                self.assertEqual(AgentRun.objects.filter(session=session).count(), 1)
                self.assertIn("<thread_summary>", captured["kwargs"]["system_extra"])

    def test_a_summary_that_fails_closes_its_run_instead_of_leaving_it_running(self):
        context, _ = self.thread()
        with mock.patch(
            "origin.search_engine.agent.thread_summary.summarise_thread",
            side_effect=ThreadSummaryError("LLM call failed: boom"),
        ):
            resp, _ = self.ask(thread_context=context)
        self.assertEqual(resp.status_code, 503)
        run = AgentRun.objects.get()
        self.assertEqual(run.status, "error")
        self.assertIsNotNone(run.finished_at)
        self.assertEqual(self.delete_all().status_code, 200)

    def test_access_lost_between_the_check_and_the_summary_closes_the_run_too(self):
        context, channel = self.thread()
        self.leave(channel)
        with mock.patch("origin.search_engine.agent_views.check_thread_for_ask"):
            resp, _ = self.ask(thread_context=context)
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(AgentRun.objects.get().status, "error")


class RefusedNoteAskTests(World):
    def refused(self):
        out = {"missing": {"note_type": 1, "note_id": MISSING_NOTE}}
        out["someone else's"] = self.note(owner=self.a_editor, title=SECRET)[0]
        elsewhere = self.note(title=SECRET)[1]
        PersonalNoteMaster.objects.filter(pk=elsewhere.pk).update(team=self.team_b)
        out["another team's"] = {"note_type": 1, "note_id": elsewhere.note_id}
        out["a task note of a project they are not on"] = self.task_note(
            project=self.private_project()
        )[0]
        return out

    def test_every_refusal_is_the_one_404_of_a_missing_note_and_writes_nothing(self):
        refused = self.refused()
        before = rows()
        missing_resp, _ = self.ask(note_context=refused.pop("missing"))
        self.assertEqual(missing_resp.status_code, 404)
        self.assertEqual(missing_resp.json(), {"error": note_summary.NOTE_NOT_FOUND})
        for name, context in refused.items():
            with self.subTest(name):
                resp, captured = self.ask(note_context=context)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.content, missing_resp.content)
                self.assertNotIn("kwargs", captured)
        self.assertEqual(rows(), before)
        self.assertEqual(self.delete_all().status_code, 200)

    def test_an_empty_note_is_a_400_and_writes_nothing(self):
        context, _ = self.note(title="", body=[])
        before = rows()
        resp, captured = self.ask(note_context=context)
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn("kwargs", captured)
        self.assertEqual(rows(), before)

    def test_a_note_the_asker_may_open_is_asked_as_before(self):
        context, _ = self.note()
        resp, captured = self.ask(note_context=context)
        self.assertEqual(resp.status_code, 200)
        session = AgentSession.objects.get()
        self.assertEqual((session.note_type, session.note_id), (1, context["note_id"]))
        self.assertIn("<note_summary>", captured["kwargs"]["system_extra"])
        self.assertEqual(AgentRun.objects.get().status, "running")

    def test_a_summary_that_fails_closes_its_run(self):
        context, _ = self.note()
        with mock.patch(
            "origin.search_engine.agent.note_summary.summarise_note",
            side_effect=note_summary.NoteSummaryError("LLM call failed: boom"),
        ):
            resp, _ = self.ask(note_context=context)
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(AgentRun.objects.get().status, "error")


# --------------------------------------------------------------------------- #
# 3. Which session an ask may continue                                         #
# --------------------------------------------------------------------------- #


class SessionReuseTests(World):
    def setUp(self):
        super().setUp()
        self.thread_a, _ = self.thread()
        self.thread_b, _ = self.thread()
        self.note_a, _ = self.note()
        self.note_b, _ = self.note(title="Other")

    def set_aside(self, stored, **context):
        """Ask under `context` naming `stored`'s id: a fresh session, bound to the context, and
        `stored` exactly as it was."""
        stamp = AgentSession.objects.get(pk=stored.pk).last_active_at
        runs = AgentRun.objects.filter(session=stored).count()
        resp, captured = self.ask(session_id=str(stored.session_id), **context)
        self.assertEqual(resp.status_code, 200)
        used = self.session_of(captured)
        self.assertNotEqual(used, str(stored.session_id))
        stored.refresh_from_db()
        self.assertEqual(stored.last_active_at, stamp)
        self.assertEqual(AgentRun.objects.filter(session=stored).count(), runs)
        return AgentSession.objects.get(session_id=used)

    def test_a_general_session_is_set_aside_under_a_thread_context(self):
        fresh = self.set_aside(self.past(), thread_context=self.thread_a)
        self.assertEqual(str(fresh.thread_id), self.thread_a["thread_id"])

    def test_a_general_session_is_set_aside_under_a_note_context(self):
        fresh = self.set_aside(self.past(), note_context=self.note_a)
        self.assertEqual(fresh.note_id, self.note_a["note_id"])

    def test_another_threads_session_is_set_aside(self):
        fresh = self.set_aside(self.past(thread=self.thread_b), thread_context=self.thread_a)
        self.assertEqual(str(fresh.thread_id), self.thread_a["thread_id"])

    def test_another_notes_session_is_set_aside(self):
        fresh = self.set_aside(self.past(note=self.note_b), note_context=self.note_a)
        self.assertEqual(fresh.note_id, self.note_a["note_id"])

    def test_a_note_session_is_set_aside_under_a_thread_context_and_the_reverse(self):
        self.set_aside(self.past(note=self.note_a), thread_context=self.thread_a)
        self.set_aside(self.past(thread=self.thread_a), note_context=self.note_a)

    def test_a_task_session_is_set_aside_under_a_thread_or_note_context(self):
        task = self.make_task()
        self.set_aside(self.past(task=task), thread_context=self.thread_a)
        self.set_aside(self.past(task=task), note_context=self.note_a)

    def test_the_thread_session_is_found_by_the_context_when_the_id_is_set_aside(self):
        mine = self.past(thread=self.thread_a)
        resp, captured = self.ask(
            session_id=str(self.past().session_id), thread_context=self.thread_a
        )
        self.assertEqual(self.session_of(captured), str(mine.session_id))

    def test_the_sessions_own_thread_or_note_continues_it(self):
        mine = self.past(thread=self.thread_a)
        resp, captured = self.ask(session_id=str(mine.session_id), thread_context=self.thread_a)
        self.assertEqual(self.session_of(captured), str(mine.session_id))
        note = self.past(note=self.note_a)
        resp, captured = self.ask(session_id=str(note.session_id), note_context=self.note_a)
        self.assertEqual(self.session_of(captured), str(note.session_id))
        self.assertEqual(AgentSession.objects.count(), 2)

    def test_ids_are_compared_as_uuids_not_as_text(self):
        # Two conversations about the thread: the one the client names is the OLDER one, and the context
        # spells the ids in capitals. A text comparison would set it aside and the per-thread lookup would
        # then pick the newest instead.
        older = self.past(thread=self.thread_a)
        newest = self.past(thread=self.thread_a)
        AgentSession.objects.filter(pk=newest.pk).update(
            last_active_at=older.last_active_at + timedelta(hours=1)
        )
        shouting = {
            **self.thread_a,
            "chat_id": self.thread_a["chat_id"].upper(),
            "thread_id": self.thread_a["thread_id"].upper(),
        }
        resp, captured = self.ask(session_id=str(older.session_id), thread_context=shouting)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.session_of(captured), str(older.session_id))

    def test_the_session_id_alone_still_continues_a_conversation_about_a_readable_object(self):
        thread = self.past(thread=self.thread_a)
        note = self.past(note=self.note_a)
        for session in (thread, note):
            resp, captured = self.ask(session_id=str(session.session_id))
            self.assertEqual(self.session_of(captured), str(session.session_id))
        self.assertEqual(AgentSession.objects.count(), 2)


class SessionIdAloneAfterAccessLostTests(World):
    def test_a_thread_the_asker_left_is_not_continued(self):
        context, channel = self.thread()
        session = self.past(thread=context)
        self.leave(channel)
        resp, captured = self.ask(session_id=str(session.session_id))
        self.assertEqual(resp.status_code, 200)
        fresh = AgentSession.objects.get(session_id=self.session_of(captured))
        self.assertNotEqual(fresh.session_id, session.session_id)
        self.assertIsNone(fresh.chat_id)
        # The conversation is still theirs to delete.
        self.assertTrue(AgentSession.objects.filter(pk=session.pk).exists())

    def test_a_note_that_is_gone_is_not_continued(self):
        context, note = self.note()
        session = self.past(note=context)
        PersonalNoteMaster.objects.filter(pk=note.pk).delete()
        resp, captured = self.ask(session_id=str(session.session_id))
        self.assertNotEqual(self.session_of(captured), str(session.session_id))

    def test_a_task_the_asker_lost_is_not_continued(self):
        task = self.make_task()
        session = self.past(task=task)
        resp, captured = self.ask(session_id=str(session.session_id))
        self.assertEqual(self.session_of(captured), str(session.session_id))
        self.remove_asker()
        resp, captured = self.ask(session_id=str(session.session_id))
        self.assertNotEqual(self.session_of(captured), str(session.session_id))

    def test_a_task_session_is_left_alone_while_the_parity_flag_is_off(self):
        # Unchanged: a task's conversation is only ever read while the flag is on.
        task = self.make_task()
        session = self.past(task=task)
        self.remove_asker()
        resp, captured = self.ask(session_id=str(session.session_id), flag=False)
        self.assertEqual(self.session_of(captured), str(session.session_id))


# --------------------------------------------------------------------------- #
# 4. Reading a past conversation, and 7. what it is about                      #
# --------------------------------------------------------------------------- #


class ReadAgainTests(World):
    def lose_thread(self):
        context, channel = self.thread()
        session = self.past(thread=context)
        return (
            session,
            lambda: self.leave(channel),
            lambda: ChannelMember.objects.filter(channel=channel, user=self.a_owner).update(
                is_deleted=False
            ),
        )

    def lose_note(self):
        context, note = self.note()
        session = self.past(note=context)
        return (
            session,
            lambda: PersonalNoteMaster.objects.filter(pk=note.pk).update(team=self.team_b),
            lambda: PersonalNoteMaster.objects.filter(pk=note.pk).update(team=self.team_a),
        )

    def lose_task_note(self):
        context, _ = self.task_note()
        session = self.past(note=context)
        return session, self.remove_asker, lambda: self.join(self.a_owner)

    def lose_task(self):
        task = self.make_task()
        session = self.past(task=task)
        return session, self.remove_asker, lambda: self.join(self.a_owner)

    def cases(self):
        return {
            "thread": self.lose_thread,
            "note": self.lose_note,
            "task note": self.lose_task_note,
            "task": self.lose_task,
        }

    def test_the_session_opens_while_the_object_is_readable_and_answers_404_after(self):
        missing = self.detail(uuid.uuid4())
        self.assertEqual(missing.status_code, 404)
        for name, make in self.cases().items():
            with self.subTest(name):
                session, lose, regain = make()
                opened = self.detail(session.session_id)
                self.assertEqual(opened.status_code, 200)
                self.assertEqual(opened.json()["turns"][0]["answer"], "The answer.")
                lose()
                resp = self.detail(session.session_id)
                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.content, missing.content)
                self.assertNotIn(b"The answer.", resp.content)
                # Hidden, never deleted: access coming back brings it straight back.
                self.assertTrue(AgentSession.objects.filter(pk=session.pk).exists())
                regain()
                self.assertEqual(self.detail(session.session_id).status_code, 200)

    def test_a_general_session_and_its_neighbours_are_not_affected(self):
        general = self.past()
        self.assertEqual(self.detail(general.session_id).status_code, 200)
        self.assertEqual([s["session_id"] for s in self.history()], [str(general.session_id)])

    def test_the_list_leaves_out_what_the_person_can_no_longer_open_and_nothing_else(self):
        general = self.past()
        lost = {}
        for name, make in self.cases().items():
            session, lose, regain = make()
            lost[name] = (session, lose, regain)
        self.assertEqual(len(self.history()), 1 + len(lost))
        for _, lose, _ in lost.values():
            lose()
        self.assertEqual([s["session_id"] for s in self.history()], [str(general.session_id)])
        for session, _, regain in lost.values():
            regain()
        self.assertEqual(len(self.history()), 1 + len(lost))

    def test_what_is_left_out_can_still_be_pinned_and_deleted(self):
        session, lose, _ = self.lose_thread()
        lose()
        self.assertEqual(
            self.client.post(f"{SESSIONS_URL}{session.session_id}/pin/").status_code, 200
        )
        self.assertEqual(
            self.client.delete(f"{SESSIONS_URL}{session.session_id}/").status_code, 200
        )
        self.assertFalse(AgentSession.objects.filter(pk=session.pk).exists())
        again, lose, _ = self.lose_note()
        lose()
        self.assertEqual(self.delete_all().status_code, 200)
        self.assertEqual(AgentSession.objects.count(), 0)

    def test_an_object_shared_by_several_sessions_is_checked_once_per_list(self):
        context, _ = self.thread()
        for _ in range(3):
            self.past(thread=context)
        with mock.patch.object(
            session_scope.thread_summary,
            "chat_readable",
            wraps=session_scope.thread_summary.chat_readable,
        ) as check:
            self.assertEqual(len(self.history()), 3)
        self.assertEqual(check.call_count, 1)

    def test_threads_of_one_chat_share_one_check(self):
        channel = self.group("Busy", [self.a_owner, self.a_editor])
        for n in range(3):
            root = self.say(channel, f"topic {n}")
            self.say(channel, "reply", reply_to=root)
            self.past(
                thread={
                    "chat_type": 2,
                    "chat_id": str(channel.id),
                    "thread_id": str(root.id),
                }
            )
        with mock.patch.object(
            session_scope.thread_summary,
            "chat_readable",
            wraps=session_scope.thread_summary.chat_readable,
        ) as check:
            self.assertEqual(len(self.history()), 3)
        self.assertEqual(check.call_count, 1)

    def test_a_lost_chat_does_not_shorten_the_list_below_its_cap(self):
        context, channel = self.thread()
        for _ in range(3):
            self.past(thread=context)  # the newest ones, all about the chat that is lost
        for n in range(2):
            self.past(query=f"general {n}")
        AgentSession.objects.filter(chat_id__isnull=True).update(
            last_active_at=timezone.now() - timedelta(days=1)
        )
        self.leave(channel)
        with mock.patch("origin.search_engine.agent_views._HISTORY_LIST_LIMIT", 2):
            self.assertEqual(len(self.history()), 2)

    def test_the_check_of_a_note_does_not_load_its_text(self):
        context, note = self.note(title=SECRET)
        self.past(note=context)
        table = PersonalNoteMaster._meta.db_table
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(len(self.history()), 1)
        note_reads = [q["sql"] for q in queries if f'FROM "{table}"' in q["sql"]]
        self.assertTrue(note_reads)
        for sql in note_reads:
            self.assertNotIn('"body"', sql)

    def test_each_row_says_what_it_is_about_as_ids_only(self):
        thread, _ = self.thread()
        note, _ = self.note(title=SECRET)
        task = self.make_task(SECRET)
        self.past(thread=thread)
        self.past(note=note)
        self.past(task=task)
        self.past()
        by_kind = {(s["scope"] or {}).get("kind"): s["scope"] for s in self.history()}
        self.assertEqual(
            by_kind,
            {
                "thread": {"kind": "thread", **thread},
                "note": {"kind": "note", **note},
                "task": {"kind": "task", "task_id": task.task_id},
                None: None,
            },
        )
        self.assertNotIn(SECRET, json.dumps(self.history()))

    def test_the_other_keys_of_a_row_are_unchanged(self):
        self.past()
        row = self.history()[0]
        self.assertEqual(
            set(row),
            {
                "session_id",
                "created_at",
                "last_active_at",
                "first_query",
                "turn_count",
                "is_pinned",
                "scope",
            },
        )


class NoteReadableTests(World):
    """`note_readable` is the rule of `fetch_note_for_agent` without the body: the same answer for every case."""

    def test_it_agrees_with_the_fetch_for_every_kind_of_note(self):
        mine, _ = self.note()
        theirs, _ = self.note(owner=self.a_editor)
        task_note, _ = self.task_note()
        private, _ = self.task_note(project=self.private_project())
        gone = {"note_type": 1, "note_id": MISSING_NOTE}
        unknown_type = {"note_type": 9, "note_id": 1}
        for name, context in {
            "mine": mine,
            "theirs": theirs,
            "task note": task_note,
            "private task note": private,
            "missing": gone,
            "unknown type": unknown_type,
        }.items():
            for team in (self.team_id, str(self.team_b.team_id)):
                with self.subTest(name, team=team):
                    try:
                        note_summary.fetch_note_for_agent(user_id=self.me, team_id=team, **context)
                        expected = True
                    except note_summary.NoteSummaryError:
                        expected = False
                    self.assertEqual(
                        note_summary.note_readable(user_id=self.me, team_id=team, **context),
                        expected,
                    )
