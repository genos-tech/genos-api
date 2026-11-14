"""MIP-1512 (a): the note tools answer a note you cannot read like a note that is not there.

`get_note` (the in-app `fetch_note` and its MCP twin) and `update_note` used to say "Personal note
12 not found." for an id that is not there, but "Not authorized to read personal note 12." (or
"Not authorized: note is in a different team.") for one that is, so a model, or a person's AI
app, could walk the id space and learn which notes exist in other teams or other people's
private spaces. The words are now the same for "missing", "in another team" and "exists, you
have no role". Only the CLASS differs (`ToolForbidden` for an existing note, which keeps counting
toward the MCP refusal alarm); the text the model sees never does.

A reader who may not WRITE still gets "Not authorized to edit": they can already open the note,
so that answer tells them nothing new.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.fetch_note import _run as fetch_note_run
from origin.search_engine.agent.tools.update_note import _run as update_note_run
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_VIEWER

User = get_user_model()
MISSING = 99999999


class Base(McpBase):
    """`self.user` owns every note and the team; `self.user2` is a teammate with no role on any of
    them; `self.outsider` belongs only to another team."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="mine", body=[]
        )
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.user, project=self.project, task=self.task, title="tn"
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
        self.notes = (
            ("personal", self.personal),
            ("task", self.task_note),
            ("chat", self.chat_note),
        )

    def ctx(self, user, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(user.id))

    def assertSameWords(self, said, note, missing):
        """The same sentence, bar the id the caller named."""
        self.assertEqual(
            said.replace(str(note.note_id), "<id>"), missing.replace(str(MISSING), "<id>")
        )

    def refusal(self, run, note_type, note_id, ctx, **extra):
        with self.assertRaises(ToolError) as caught:
            run({"note_type": note_type, "note_id": note_id, **extra}, ctx)
        return str(caught.exception), caught.exception


class GetNoteTool(Base):
    def run_get(self, note_type, note_id, ctx):
        return self.refusal(fetch_note_run, note_type, note_id, ctx)

    def test_a_note_with_no_role_reads_like_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said, exc = self.run_get(note_type, note.note_id, self.ctx(self.user2))
                missing, _ = self.run_get(note_type, MISSING, self.ctx(self.user2))
                self.assertSameWords(said, note, missing)
                self.assertNotIn("uthorized", said)
                # the class still tells the MCP refusal alarm that it was a refusal
                self.assertIsInstance(exc, ToolForbidden)

    def test_a_note_of_another_team_reads_like_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said, _ = self.run_get(note_type, note.note_id, self.ctx(self.outsider))
                missing, _ = self.run_get(note_type, MISSING, self.ctx(self.outsider))
                self.assertSameWords(said, note, missing)
                self.assertNotIn("team", said.lower().replace("teammate", ""))

    def test_a_member_of_both_teams_running_in_the_other_one_gets_the_same_answer(self):
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said, _ = self.run_get(
                    note_type, note.note_id, self.ctx(self.user, self.other_team)
                )
                missing, _ = self.run_get(note_type, MISSING, self.ctx(self.user, self.other_team))
                self.assertSameWords(said, note, missing)

    def test_a_missing_note_is_still_a_plain_tool_error_not_a_refusal(self):
        for note_type, _note in self.notes:
            with self.subTest(note_type=note_type):
                _said, exc = self.run_get(note_type, MISSING, self.ctx(self.user2))
                self.assertNotIsInstance(exc, ToolForbidden)

    def test_the_answer_is_one_sentence_whatever_the_family(self):
        said = {
            self.run_get(t, MISSING, self.ctx(self.user))[0].replace(str(MISSING), "N")
            for t, _ in self.notes
        }
        self.assertEqual(len(said), 1, said)

    def test_a_reader_still_gets_the_note(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                out = fetch_note_run(
                    {"note_type": note_type, "note_id": note.note_id}, self.ctx(self.user)
                )
                self.assertEqual(out["note_id"], note.note_id)

    def test_a_granted_reader_still_gets_a_personal_note(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.personal.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        out = fetch_note_run(
            {"note_type": "personal", "note_id": self.personal.note_id}, self.ctx(self.user2)
        )
        self.assertEqual(out["note_id"], self.personal.note_id)


class GetNoteOverMcp(Base):
    """The same through the MCP door: the text the client's model reads."""

    def get_note(self, user, note_type, note_id):
        self._key(scope=SCOPE_READ, user=user)
        text, is_error = self._tool_text(
            self._call("get_note", {"note_id": note_id, "note_type": note_type})
        )
        self.assertTrue(is_error, text)
        return text

    def test_an_unreadable_note_reads_like_a_missing_one_over_mcp(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said = self.get_note(self.user2, note_type, note.note_id)
                missing = self.get_note(self.user2, note_type, MISSING)
                self.assertEqual(
                    said.replace(str(note.note_id), "N"), missing.replace(str(MISSING), "N")
                )


class UpdateNoteTool(Base):
    def run_update(self, note_type, note_id, ctx):
        return self.refusal(update_note_run, note_type, note_id, ctx, title="x")

    def test_a_note_with_no_role_reads_like_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said, exc = self.run_update(note_type, note.note_id, self.ctx(self.user2))
                missing, _ = self.run_update(note_type, MISSING, self.ctx(self.user2))
                self.assertSameWords(said, note, missing)
                self.assertNotIn("uthorized", said)
                self.assertIsInstance(exc, ToolForbidden)

    def test_a_note_of_another_team_reads_like_a_missing_one(self):
        for note_type, note in self.notes:
            with self.subTest(note_type=note_type):
                said, _ = self.run_update(note_type, note.note_id, self.ctx(self.outsider))
                missing, _ = self.run_update(note_type, MISSING, self.ctx(self.outsider))
                self.assertSameWords(said, note, missing)

    def test_a_viewer_is_still_told_they_may_not_edit(self):
        """They can already open the note, so the refusal says what it is."""
        for note_type, note in self.notes:
            NotePermissionMaster.objects.update_or_create(
                user=self.user2,
                note_type={"personal": 1, "task": 2, "chat": 3}[note_type],
                note_id=note.note_id,
                defaults={"team": self.team, "role_id": ROLE_VIEWER},
            )
            with self.subTest(note_type=note_type):
                said, exc = self.run_update(note_type, note.note_id, self.ctx(self.user2))
                self.assertIn("Not authorized to edit", said)
                self.assertIsInstance(exc, ToolForbidden)
        self.assertEqual(PersonalNoteMaster.objects.get(pk=self.personal.pk).title, "mine")

    def test_the_owner_still_edits(self):
        with mock.patch("origin.services.collab_push.push_note_body"):
            out = update_note_run(
                {"note_type": "personal", "note_id": self.personal.note_id, "title": "new"},
                self.ctx(self.user),
            )
        self.assertEqual(out["changed_fields"], ["title"])
