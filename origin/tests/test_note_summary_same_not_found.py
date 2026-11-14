"""The note-summary surfaces answer a note you cannot read like a note that is not there.

`POST /api/v2/agent/note-summary/` (and the note branch of `/agent/ask/`, which shares the loader)
said "Personal note 12 not found." for an id that is not there but "Not authorized to read this
note." (or "Note not found or has no readers.") for one that is, so a signed-in user could walk the
id space and learn which notes exist in other teams and in other people's private spaces. The
words, the status and the body are now the same for "missing", "exists, you have no role" and "in
another team". Nobody gains access: the same people read, the rest hear the same "no".

The loader also had no tenant check of its own: the `team_id` in the body named the team for the
metering and the session lookup, but the note was found by id alone. A note is now found only
inside the team the request names (`fetch_note_for_agent(team_id=...)`; `/ask/`'s `note_context`
carries no object key, so its body team reaches the loader as sent), and the caller must take part
in that team (the gate `/ask/` already has: a member, a guest, or someone admitted to one chat or
folder by a cross-team grant). On `note-summary/` the request's team is first aligned to the
note's own team for a caller who can reach it (`align_team_to_object`), so there the team check is
defence in depth: it cannot be seen over HTTP, only through what the gate does not call.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.note_summary import (
    NoteSummaryError,
    fetch_note_for_agent,
)
from origin.tests.test_agent_non_object_body import _AgentClient

User = get_user_model()
SUMMARY = "/api/v2/agent/note-summary/"
ASK = "/api/v2/agent/ask/"
MISSING = 99999999
ROLE_OWNER = 1
NOT_FOUND = {"error": "Note not found."}


class Base(_AgentClient):
    """`self.user` owns the team and every note; `self.user2` is a teammate with no role on any of
    them; `self.outsider` belongs only to another team."""

    def setUp(self):
        super().setUp()
        # No model is ever reached: a summary is stubbed, and the agent loop of `/ask/` has no
        # client to build (a test that got that far would fail loudly, not call out).
        summarise = mock.patch(
            "origin.search_engine.agent.note_summary.summarise_note",
            return_value=("a summary", "stub:model"),
        )
        client = mock.patch.object(
            controller, "get_model_client", side_effect=RuntimeError("no model")
        )
        summarise.start()
        client.start()
        self.addCleanup(summarise.stop)
        self.addCleanup(client.stop)
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="mine", body=[]
        )
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=project, task=task, title="tn"
        )
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=self.user, role="owner")
        self.chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=channel.kind,
            channel=channel,
            is_thread=False,
            title="cn",
            body=[],
        )
        for code, note in ((1, self.personal), (2, self.task_note), (3, self.chat_note)):
            NotePermissionMaster.objects.create(
                team=self.team,
                user=self.user,
                note_id=note.note_id,
                note_type=code,
                role_id=ROLE_OWNER,
            )
        self.notes = ((1, self.personal), (2, self.task_note), (3, self.chat_note))

    def summary(self, user, team, note_type, note_id, **extra):
        self.authenticate(user)
        return self.client.post(
            SUMMARY,
            {"team_id": str(team.team_id), "note_type": note_type, "note_id": note_id, **extra},
            format="json",
        )

    def assertSameAnswer(self, existing, missing):
        self.assertEqual(missing.status_code, 404, missing.content)
        self.assertEqual(missing.json(), NOT_FOUND)
        self.assertEqual(existing.status_code, 404, existing.content)
        self.assertEqual(existing.json(), missing.json())


class NoteSummaryView(Base):
    def test_a_teammate_with_no_role_gets_what_a_missing_note_gets(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.summary(self.user2, self.team, note_type, note.note_id),
                    self.summary(self.user2, self.team, note_type, MISSING),
                )

    def test_a_stranger_who_knows_the_team_id_gets_the_same_answer(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.summary(self.outsider, self.team, note_type, note.note_id),
                    self.summary(self.outsider, self.team, note_type, MISSING),
                )

    def test_naming_their_own_team_changes_nothing(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.summary(self.outsider, self.other_team, note_type, note.note_id),
                    self.summary(self.outsider, self.other_team, note_type, MISSING),
                )

    def test_a_caller_outside_the_team_never_reaches_the_note_loader(self):
        # Belt and braces: the loader's own ACL would refuse them too, so pin the gate by what it
        # does NOT call. The gate is the one `/ask/` has (`participates_in_team`).
        for force in (False, True):
            with (
                self.subTest(force=force),
                mock.patch("origin.search_engine.agent_views.peek_cached_note_summary") as peek,
                mock.patch("origin.search_engine.agent.note_summary.fetch_note_for_agent") as fetch,
            ):
                res = self.summary(
                    self.outsider, self.team, 1, self.personal.note_id, force_regenerate=force
                )
                self.assertEqual((res.status_code, res.json()), (404, NOT_FOUND))
                peek.assert_not_called()
                fetch.assert_not_called()

    def test_force_regenerate_takes_the_same_path(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.summary(
                        self.user2, self.team, note_type, note.note_id, force_regenerate=True
                    ),
                    self.summary(self.user2, self.team, note_type, MISSING, force_regenerate=True),
                )

    def test_the_reader_still_gets_a_summary(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                res = self.summary(self.user, self.team, note_type, note.note_id)
                self.assertEqual(res.status_code, 200, res.content)
                self.assertEqual(res.json()["summary"], "a summary")
                self.assertEqual(res.json()["note_title"], note.title)

    def test_the_other_refusals_keep_their_answers(self):
        self.authenticate(self.user)
        res = self.client.post(SUMMARY, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            SUMMARY,
            {"team_id": str(self.team.team_id), "note_type": "x", "note_id": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        empty = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="", body=[]
        )
        res = self.summary(self.user, self.team, 1, empty.note_id)
        self.assertEqual(res.status_code, 400, res.content)
        self.assertIn("empty", res.json()["error"])


class NoteBranchOfAsk(Base):
    """`/agent/ask/` with a `note_context` loads the same record; its refusal is the same 404."""

    def ask(self, user, team, note_type, note_id):
        self.authenticate(user)
        return self.client.post(
            ASK,
            {
                "query": "what does it say?",
                "team_id": str(team.team_id),
                "note_context": {"note_type": note_type, "note_id": note_id},
            },
            format="json",
        )

    def test_an_unreadable_note_answers_like_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.ask(self.user2, self.team, note_type, note.note_id),
                    self.ask(self.user2, self.team, note_type, MISSING),
                )

    def test_a_note_in_another_team_than_the_one_named_is_not_found(self):
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                self.assertSameAnswer(
                    self.ask(self.user, self.other_team, note_type, note.note_id),
                    self.ask(self.user, self.other_team, note_type, MISSING),
                )


class TheLoader(Base):
    """`fetch_note_for_agent` raises ONE error for every kind of "no", words included."""

    def test_every_refusal_has_the_same_words(self):
        said = set()
        for note_type, note in self.notes:
            for user in (self.user2, self.outsider):
                for note_id in (note.note_id, MISSING):
                    with self.assertRaises(NoteSummaryError) as raised:
                        fetch_note_for_agent(
                            note_type=note_type, note_id=note_id, user_id=str(user.id)
                        )
                    said.add(str(raised.exception))
        self.assertEqual(said, {"Note not found."})

    def test_the_team_is_checked_when_the_caller_names_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                got = fetch_note_for_agent(
                    note_type=note_type,
                    note_id=note.note_id,
                    user_id=str(self.user.id),
                    team_id=str(self.team.team_id),
                )
                self.assertEqual(got.note_id, note.note_id)
                with self.assertRaises(NoteSummaryError) as raised:
                    fetch_note_for_agent(
                        note_type=note_type,
                        note_id=note.note_id,
                        user_id=str(self.user.id),
                        team_id=str(self.other_team.team_id),
                    )
                self.assertEqual(str(raised.exception), "Note not found.")
