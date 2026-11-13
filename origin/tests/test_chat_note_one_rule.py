"""MIP-1474: ONE rule for "may this person open this chat note?".

`get_effective_role`'s chat branch used to read the channel-member row alone, so a STRAY
`ChannelMember` row (someone not on the project, a deleted channel, ...) opened a chat note
while `channel_scope.chat_member_q` (the product's own definition of who may read a chat, and
the rule of the search index's `chat_acl_user_ids`) says such a row opens nothing: a project
chat is read by whoever is on the PROJECT; the channel row only mirrors it.

Every reader below must answer the same way, so each case is asked of all of them:

  * `get_effective_role`         (REST single note, collab gate, inbox, every write gate)
  * `GET /api/v2/note/chat/single/`        (REST)
  * MCP `get_note`               (the in-app agent's `get_note` is the same function)
  * `readable_note_ids`          (favorites, recents, the timeline's batch)
  * `briefing._may_read_note`    (the briefing and `get_item_line`)
  * `timeline.readable_notes`
  * the sidebar's note list (`/api/v2/note/chat/meta/`)
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext
from origin.services import briefing, timeline
from origin.tests.test_mcp_endpoint import McpBase
from origin.views.utils.note_role import (
    ROLE_EDITOR,
    ROLE_VIEWER,
    get_effective_role,
    readable_note_ids,
    require_write_role,
)

User = get_user_model()
CHAT = 3


class OneChatNoteRule(McpBase):
    def setUp(self):
        super().setUp()
        self.reader = User.objects.create_user(
            username="reader", email="reader@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.reader)
        self.owner = self.user2

    # -- builders ----------------------------------------------------------------------------- #

    def project_of(self, code, *members):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=f"P {code}",
            code=code,
            owner=self.owner,
            project_system_user=self.owner,
        )
        for m in (self.owner, *members):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=m)
        return project

    def pm_chat(self, project):
        return Channel.objects.get(kind=ChannelKind.PM, project=project)

    def note_on(self, channel, *, chat_type=None, owner=None, title="The chat note"):
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.owner,
            chat_type=chat_type if chat_type is not None else channel.kind,
            channel=channel,
            is_thread=False,
            title=title,
            body=[{"type": "paragraph", "content": [{"type": "text", "text": "secret chat body"}]}],
        )

    def gm(self, *members, deleted=False):
        channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gm", is_deleted=deleted
        )
        for m in members:
            ChannelMember.objects.create(channel=channel, user=m)
        return channel

    # -- the readers -------------------------------------------------------------------------- #

    def ctx(self):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(self.reader.id))

    def rest_single(self, note):
        self.authenticate(self.reader)
        return self.client.get(
            "/api/v2/note/chat/single/",
            {"team_id": str(self.team.team_id), "note_id": note.note_id},
        )

    def rest_meta_ids(self):
        self.authenticate(self.reader)
        res = self.client.get(
            "/api/v2/note/chat/meta/",
            {"team_id": str(self.team.team_id), "user_id": str(self.reader.id)},
        )
        self.assertEqual(res.status_code, 200, res.content)
        return {n["noteId"] for n in res.json()}

    def mcp_get_note(self, note_id):
        self._key(scope=SCOPE_READ, user=self.reader)
        return self._tool_text(self._call("get_note", {"note_type": "chat", "note_id": note_id}))

    def answers(self, note) -> dict[str, bool]:
        """Every reader's verdict on one note, for the reader."""
        text, is_error = self.mcp_get_note(note.note_id)
        return {
            "get_effective_role": get_effective_role(self.reader.id, CHAT, note.note_id)
            is not None,
            "rest single": self.rest_single(note).status_code == 200,
            "mcp get_note": not is_error and "secret chat body" in text,
            "readable_note_ids": note.note_id
            in readable_note_ids(self.reader.id, CHAT, [note.note_id]),
            "briefing": briefing._may_read_note(self.ctx(), CHAT, note),
            "timeline": (CHAT, note.note_id)
            in timeline.readable_notes(self.ctx(), [(CHAT, note.note_id)]),
            "sidebar list": note.note_id in self.rest_meta_ids(),
        }

    def assertEveryoneSays(self, note, expected: bool):
        got = self.answers(note)
        self.assertEqual(got, {k: expected for k in got}, got)

    # -- the bug: a stray row opens nothing --------------------------------------------------- #

    def test_a_stray_channel_row_on_a_project_chat_opens_nothing(self):
        project = self.project_of("STR")  # the reader is NOT on the project
        chat = self.pm_chat(project)
        ChannelMember.objects.update_or_create(
            channel=chat, user=self.reader, defaults={"is_deleted": False}
        )
        self.assertFalse(ProjectMembers.objects.filter(project=project, attendee=self.reader))
        self.assertEveryoneSays(self.note_on(chat), False)

    def test_a_stray_row_cannot_edit_either(self):
        chat = self.pm_chat(self.project_of("STW"))
        ChannelMember.objects.update_or_create(
            channel=chat, user=self.reader, defaults={"is_deleted": False}
        )
        note = self.note_on(chat)
        self.assertIsNotNone(require_write_role(self.reader.id, CHAT, note.note_id))

    def test_a_stray_row_is_refused_in_the_words_a_plain_stranger_is(self):
        """No new existence oracle: someone refused through a stray row reads the same refusal,
        over REST and over MCP, as someone with no tie to the chat at all."""
        chat = self.pm_chat(self.project_of("ORA"))
        ChannelMember.objects.update_or_create(
            channel=chat, user=self.reader, defaults={"is_deleted": False}
        )
        stray = self.note_on(chat)
        stranger = self.note_on(self.gm(self.owner))
        for note in (stray, stranger):
            self.assertEqual(self.rest_single(note).status_code, 403)
        self.assertEqual(self.rest_single(stray).json(), self.rest_single(stranger).json())
        a, a_err = self.mcp_get_note(stray.note_id)
        b, b_err = self.mcp_get_note(stranger.note_id)
        self.assertTrue(a_err and b_err)
        self.assertEqual(a.replace(str(stray.note_id), "N"), b.replace(str(stranger.note_id), "N"))

    def test_a_channel_that_is_deleted_opens_nothing_even_for_a_live_member_row(self):
        self.assertEveryoneSays(self.note_on(self.gm(self.reader, deleted=True)), False)

    # -- legitimate readers keep their access ------------------------------------------------- #

    def test_a_member_of_a_group_chat_reads_and_edits_its_note(self):
        note = self.note_on(self.gm(self.reader, self.owner))
        self.assertEveryoneSays(note, True)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_EDITOR)

    def test_a_member_of_a_dm_reads_its_note(self):
        dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, title="dm")
        ChannelMember.objects.create(channel=dm, user=self.reader)
        ChannelMember.objects.create(channel=dm, user=self.owner)
        self.assertEveryoneSays(self.note_on(dm), True)

    def test_a_project_member_with_the_mirror_row_reads_the_project_chat_note(self):
        project = self.project_of("MIR", self.reader)
        chat = self.pm_chat(project)
        self.assertTrue(
            ChannelMember.objects.filter(channel=chat, user=self.reader, is_deleted=False)
        )
        note = self.note_on(chat)
        self.assertEveryoneSays(note, True)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_EDITOR)

    def test_a_project_member_with_no_mirror_row_reads_the_project_chat_note(self):
        """The product rule: the PROJECT decides, the channel row only mirrors it. A project
        member whose mirror row is missing (the signal bails when the chat is not there yet)
        still reads, as they do in the briefing, the agent's chat tools and the search index."""
        project = self.project_of("NOM", self.reader)
        chat = self.pm_chat(project)
        ChannelMember.objects.filter(channel=chat, user=self.reader).delete()
        self.assertEveryoneSays(self.note_on(chat), True)

    def test_a_project_member_whose_mirror_row_is_soft_deleted_still_reads(self):
        project = self.project_of("SOF", self.reader)
        chat = self.pm_chat(project)
        ChannelMember.objects.filter(channel=chat, user=self.reader).update(is_deleted=True)
        self.assertEveryoneSays(self.note_on(chat), True)

    def test_someone_removed_from_the_project_loses_the_chat_note_even_if_the_row_lingers(self):
        project = self.project_of("GON", self.reader)
        chat = self.pm_chat(project)
        note = self.note_on(chat)
        self.assertEveryoneSays(note, True)
        ProjectMembers.objects.filter(project=project, attendee=self.reader).delete()
        # the removal signal soft-deletes the mirror row; put it back to model a missed signal
        ChannelMember.objects.filter(channel=chat, user=self.reader).update(is_deleted=False)
        self.assertEveryoneSays(note, False)

    def test_the_note_owner_reads_their_own_note_through_the_briefing(self):
        """Owner and explicit grants are the notes' own ACL, not the chat's."""
        note = self.note_on(self.gm(self.owner), owner=self.reader)
        self.assertTrue(briefing._may_read_note(self.ctx(), CHAT, note))

    def test_an_explicit_grant_opens_a_chat_note_the_reader_is_not_in_the_chat_of(self):
        note = self.note_on(self.gm(self.owner))
        NotePermissionMaster.objects.create(
            team=self.team, user=self.reader, note_type=CHAT, note_id=note.note_id, role_id=3
        )
        got = self.answers(note)
        self.assertEqual(got, {k: True for k in got}, got)
        self.assertEqual(get_effective_role(self.reader.id, CHAT, note.note_id), ROLE_VIEWER)

    def test_a_note_whose_chat_type_differs_from_its_channels_kind_opens_for_no_one_but_acl(self):
        """`chat_type` is the kind the note was filed under; the search index (`chat_acl_user_ids`),
        `get_note` over MCP and the in-app agent all look the channel up BY that kind and find
        nothing when it differs, so such a note opens through its owner and explicit grants only.
        REST used to open it to every member row: it no longer does (one rule)."""
        gm = self.gm(self.reader, self.owner)
        self.assertEveryoneSays(self.note_on(gm, chat_type=ChannelKind.DM), False)

    def test_a_drifted_chat_type_still_opens_to_the_owner_and_to_a_grant(self):
        gm = self.gm(self.owner)
        mine = self.note_on(gm, chat_type=ChannelKind.DM, owner=self.reader)
        self.assertTrue(briefing._may_read_note(self.ctx(), CHAT, mine))
        granted = self.note_on(gm, chat_type=ChannelKind.DM)
        NotePermissionMaster.objects.create(
            team=self.team, user=self.reader, note_type=CHAT, note_id=granted.note_id, role_id=3
        )
        self.assertIsNotNone(get_effective_role(self.reader.id, CHAT, granted.note_id))

    def test_a_chat_type_claiming_a_gm_does_not_turn_a_project_chat_into_a_member_row_chat(self):
        """Someone with a stray row on a project chat, whose note claims `chat_type` GM, is
        still outside the project: the kind that decides is the channel's."""
        chat = self.pm_chat(self.project_of("DRF"))
        ChannelMember.objects.update_or_create(
            channel=chat, user=self.reader, defaults={"is_deleted": False}
        )
        self.assertEveryoneSays(self.note_on(chat, chat_type=ChannelKind.GM), False)
