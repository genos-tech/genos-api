"""MIP-749: the note tools write wherever the note UI lets the user write.

`create_note` / `update_note` (agent and MCP) used to cover My Notes and
task notes only. Now:

  * 'team' creates a note in a Team Notes folder (Editor+ on the folder),
    and update_note moves notes in and out of team folders through the
    REST move's own authorization (`personal_note_move`).
  * 'shared' is an alias for updating someone else's shared note; creating
    one is refused.
  * 'chat' creates and edits chat and thread notes, for members only, with
    the REST create's role fan-out (`chat_note_target`, MIP-838), and an
    edit is pushed into the live `chat-note:<id>` doc.
  * list_note_folders lists the Team Notes folders the user can reach.
"""

from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.search_engine.agent.controller import _friendly_arguments, _ui_sources_from_tool_result
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.search_engine.agent.tools.list_note_folders import _run as list_folders_run
from origin.search_engine.agent.tools.update_note import _run as update_note_run
from origin.services import collab_push
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
)

from .test_base import BaseAPITestCase


class EverySpaceBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        # A public team folder: every team member is an Editor.
        self.handbook = self._team_folder("Handbook", visibility="public")
        # A private team folder where user2 is only a Viewer.
        self.board = self._team_folder("Board", visibility="private")
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.board, user=self.user2, role_id=ROLE_VIEWER
        )
        self.my_folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Drafts"
        )

    def _team_folder(self, name, *, visibility):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user, role_id=ROLE_OWNER
        )
        return folder

    def _note(self, *, owner=None, folder=None, title="n"):
        owner = owner or self.user
        note = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner,
            title=title,
            body=[],
            folder_id=folder.folder_id if folder else None,
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=owner,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_OWNER,
        )
        return note


class CreateTeamNoteTests(EverySpaceBase):
    def test_editor_creates_a_note_in_a_team_folder(self):
        res = create_note_run(
            {
                "note_type": "team",
                "title": "Onboarding",
                "content_text": "hello",
                "folder_id": self.handbook.folder_id,
            },
            self.ctx2,
        )
        note = PersonalNoteMaster.objects.get(note_id=res["note_id"])
        self.assertEqual(note.folder_id, self.handbook.folder_id)
        self.assertEqual(str(note.owner_id), str(self.user2.id))
        self.assertEqual((res["note_type"], res["space"]), ("personal", "team"))
        self.assertTrue(
            NotePermissionMaster.objects.filter(
                note_id=note.note_id, note_type=NOTE_TYPE_PERSONAL, role_id=ROLE_OWNER
            ).exists()
        )
        self.assertEqual(
            NoteVersionMaster.objects.filter(
                note_type=NOTE_TYPE_PERSONAL, note_id=note.note_id
            ).count(),
            1,
        )

    def test_viewer_of_the_folder_cannot_add_a_note(self):
        with self.assertRaises(ToolError):
            create_note_run(
                {"note_type": "team", "title": "x", "folder_id": self.board.folder_id}, self.ctx2
            )
        self.assertFalse(PersonalNoteMaster.objects.filter(folder_id=self.board.folder_id).exists())

    def test_team_note_needs_a_team_folder(self):
        for folder_id in (None, self.my_folder.folder_id, 999999):
            with self.subTest(folder_id=folder_id):
                with self.assertRaises(ToolError):
                    create_note_run(
                        {"note_type": "team", "title": "x", "folder_id": folder_id}, self.ctx
                    )
        # A My-Notes folder is named as the wrong kind, not as a permission gap.
        with self.assertRaisesRegex(ToolError, "not a Team Notes folder"):
            create_note_run(
                {"note_type": "team", "title": "x", "folder_id": self.my_folder.folder_id},
                self.ctx,
            )

    def test_personal_note_cannot_land_in_a_team_folder(self):
        # A wrong id must not turn a private note into a shared one.
        with self.assertRaises(ToolError):
            create_note_run(
                {"note_type": "personal", "title": "x", "folder_id": self.handbook.folder_id},
                self.ctx,
            )
        self.assertFalse(
            PersonalNoteMaster.objects.filter(folder_id=self.handbook.folder_id).exists()
        )

    def test_shared_cannot_be_created(self):
        with self.assertRaises(ToolError):
            create_note_run({"note_type": "shared", "title": "x"}, self.ctx)
        self.assertFalse(PersonalNoteMaster.objects.exists())

    def test_unknown_note_type_is_refused(self):
        with self.assertRaises(ToolError):
            create_note_run({"note_type": "folder", "title": "x"}, self.ctx)


class ChatNoteToolTests(EverySpaceBase):
    def setUp(self):
        super().setUp()
        self.carol = get_user_model().objects.create_user(
            username="carol", email="carol@example.com", password="x"
        )
        # A GM owned by carol: user is a member, user2 is not.
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Design", owner=self.carol
        )
        ChannelMember.objects.create(channel=self.gm, user=self.carol, role="owner")
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="member")
        self.root = Message.objects.create(
            channel=self.gm, sender=self.carol, seq=1, body={}, body_text="kickoff"
        )

    def _create(self, ctx, **extra):
        return create_note_run(
            {"note_type": "chat", "title": "Decisions", "content_text": "We ship", **extra}, ctx
        )

    def test_member_creates_a_chat_note_with_the_chat_roles(self):
        res = self._create(self.ctx, chat_id=str(self.gm.id))
        note = ChatNoteMaster.objects.get(note_id=res["note_id"])
        self.assertEqual(note.chat_type, int(ChannelKind.GM))
        self.assertFalse(note.is_thread)
        roles = dict(
            NotePermissionMaster.objects.filter(
                note_type=NOTE_TYPE_CHAT, note_id=note.note_id
            ).values_list("user_id", "role_id")
        )
        self.assertEqual(roles, {self.carol.id: ROLE_OWNER, self.user.id: ROLE_EDITOR})
        self.assertEqual(
            res["parent_context"],
            {"chat_type": "gm", "chat_id": str(self.gm.id), "is_thread": False},
        )
        self.assertEqual(
            NoteVersionMaster.objects.filter(
                note_type=NOTE_TYPE_CHAT, note_id=note.note_id
            ).count(),
            1,
        )

    def test_thread_note_from_a_pasted_thread_url(self):
        url = f"https://app.genos.chat/workspace/chat/gm/{self.gm.id}/thread/{self.root.id}"
        res = self._create(self.ctx, chat_id=url)
        note = ChatNoteMaster.objects.get(note_id=res["note_id"])
        self.assertTrue(note.is_thread)
        self.assertEqual(str(note.thread_root_id), str(self.root.id))
        self.assertEqual(res["parent_context"]["thread_id"], str(self.root.id))

    def test_non_member_cannot_create(self):
        with self.assertRaises(ToolError):
            self._create(self.ctx2, chat_id=str(self.gm.id))
        self.assertFalse(ChatNoteMaster.objects.exists())

    def test_thread_must_be_in_that_chat(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, owner=self.user)
        ChannelMember.objects.create(channel=other, user=self.user, role="owner")
        with self.assertRaises(ToolError):
            self._create(self.ctx, chat_id=str(other.id), thread_id=str(self.root.id))
        self.assertFalse(ChatNoteMaster.objects.exists())

    def test_chat_id_must_name_a_chat(self):
        for bad in (None, "", "design", "12"):
            with self.subTest(chat_id=bad):
                with self.assertRaises(ToolError):
                    self._create(self.ctx, chat_id=bad)

    def test_member_edits_a_chat_note_and_the_live_doc_gets_it(self):
        note_id = self._create(self.ctx, chat_id=str(self.gm.id))["note_id"]
        carol_ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.carol.id))
        with mock.patch("origin.search_engine.agent.tools.update_note.push_note_body") as push:
            res = update_note_run(
                {"note_type": "chat", "note_id": note_id, "content_text": "We ship Friday"},
                carol_ctx,
            )
        self.assertEqual(res["changed_fields"], ["body"])
        self.assertEqual(res["parent_context"]["chat_id"], str(self.gm.id))
        push.assert_called_once()
        self.assertEqual(push.call_args.args[:2], ("chat", note_id))

    def test_non_member_cannot_edit_a_chat_note(self):
        note_id = self._create(self.ctx, chat_id=str(self.gm.id))["note_id"]
        with self.assertRaises(ToolForbidden):
            update_note_run({"note_type": "chat", "note_id": note_id, "title": "x"}, self.ctx2)
        self.assertEqual(ChatNoteMaster.objects.get(note_id=note_id).title, "Decisions")

    def test_explicit_viewer_cannot_edit_a_chat_note(self):
        note_id = self._create(self.ctx, chat_id=str(self.gm.id))["note_id"]
        NotePermissionMaster.objects.filter(
            note_type=NOTE_TYPE_CHAT, note_id=note_id, user=self.user
        ).update(role_id=ROLE_VIEWER)
        ChatNoteMaster.objects.filter(note_id=note_id).update(owner=self.carol)
        with self.assertRaises(ToolForbidden):
            update_note_run({"note_type": "chat", "note_id": note_id, "title": "x"}, self.ctx)

    def test_chat_doc_name_for_the_body_push(self):
        with mock.patch.object(collab_push, "_push") as push:
            collab_push.push_note_body("chat", 7, [])
        push.assert_called_once_with("chat-note:7", [])

    def test_approval_card_names_the_chat_and_the_note(self):
        out = _friendly_arguments({"note_type": "chat", "chat_id": str(self.gm.id)}, "create_note")
        self.assertEqual(out["chat_name"], "Design")
        note_id = self._create(self.ctx, chat_id=str(self.gm.id))["note_id"]
        out = _friendly_arguments({"note_type": "chat", "note_id": note_id}, "update_note")
        self.assertEqual(out["note_title"], "Decisions")

    def test_written_chat_note_gets_a_chip(self):
        res = self._create(self.ctx, chat_id=str(self.gm.id))
        sources = _ui_sources_from_tool_result("create_note", res)
        self.assertEqual(sources[0]["note_type"], "chat")
        self.assertEqual(sources[0]["chat_id"], str(self.gm.id))


class TeamAndSharedUpdateTests(EverySpaceBase):
    def test_team_alias_edits_a_team_note_as_folder_editor(self):
        note = self._note(folder=self.handbook)
        res = update_note_run(
            {"note_type": "team", "note_id": note.note_id, "title": "Renamed"}, self.ctx2
        )
        self.assertEqual(res["changed_fields"], ["title"])

    def test_shared_alias_edits_a_note_shared_with_editor_rights(self):
        note = self._note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_EDITOR,
        )
        res = update_note_run(
            {"note_type": "shared", "note_id": note.note_id, "title": "Edited"}, self.ctx2
        )
        self.assertEqual(res["changed_fields"], ["title"])

    def test_viewer_of_a_team_folder_cannot_edit_its_notes(self):
        note = self._note(folder=self.board)
        with self.assertRaises(ToolForbidden):
            update_note_run({"note_type": "team", "note_id": note.note_id, "title": "x"}, self.ctx2)


class FolderMoveTests(EverySpaceBase):
    def test_owner_files_a_my_note_into_a_team_folder(self):
        note = self._note()
        res = update_note_run(
            {
                "note_type": "personal",
                "note_id": note.note_id,
                "folder_id": self.handbook.folder_id,
            },
            self.ctx,
        )
        self.assertEqual(res["changed_fields"], ["folder"])
        note.refresh_from_db()
        self.assertEqual(note.folder_id, self.handbook.folder_id)

    def test_folder_editor_rearranges_a_team_note_between_team_folders(self):
        other = self._team_folder("Other", visibility="public")
        note = self._note(folder=self.handbook)
        update_note_run(
            {"note_type": "team", "note_id": note.note_id, "folder_id": other.folder_id},
            self.ctx2,
        )
        note.refresh_from_db()
        self.assertEqual(note.folder_id, other.folder_id)

    def test_only_the_owner_moves_a_note_out_of_team_notes_and_nothing_half_applies(self):
        note = self._note(folder=self.handbook)
        with self.assertRaises(ToolError):
            update_note_run(
                {
                    "note_type": "team",
                    "note_id": note.note_id,
                    "title": "Should not stick",
                    "folder_id": None,
                },
                self.ctx2,
            )
        note.refresh_from_db()
        self.assertEqual(note.folder_id, self.handbook.folder_id)
        self.assertEqual(note.title, "n")

    def test_moving_out_of_team_notes_purges_it_from_search(self):
        note = self._note(folder=self.handbook)
        with mock.patch("origin.views.utils.personal_note_move.purge_notes_from_index") as purge:
            update_note_run(
                {"note_type": "personal", "note_id": note.note_id, "folder_id": None}, self.ctx
            )
        note.refresh_from_db()
        self.assertIsNone(note.folder_id)
        purge.assert_called_once()
        self.assertIn(note.note_id, purge.call_args.args[0])

    def test_cannot_file_into_a_team_folder_without_editor_rights(self):
        note = self._note(owner=self.user2)
        with self.assertRaises(ToolError):
            update_note_run(
                {
                    "note_type": "personal",
                    "note_id": note.note_id,
                    "folder_id": self.board.folder_id,
                },
                self.ctx2,
            )
        note.refresh_from_db()
        self.assertIsNone(note.folder_id)

    def test_cannot_file_into_someone_elses_my_notes_folder(self):
        note = self._note(owner=self.user2)
        with self.assertRaises(ToolError):
            update_note_run(
                {
                    "note_type": "personal",
                    "note_id": note.note_id,
                    "folder_id": self.my_folder.folder_id,
                },
                self.ctx2,
            )


class ListFoldersTests(EverySpaceBase):
    def test_team_folders_are_listed_apart_with_the_callers_role(self):
        self._note(folder=self.handbook, title="Guide")
        self._note(folder=self.my_folder, title="Mine")
        res = list_folders_run({}, self.ctx2)
        team = {f["name"]: f for f in res["team_folders"]}
        self.assertEqual(team["Handbook"]["your_role"], "editor")
        self.assertTrue(team["Handbook"]["can_add_notes"])
        self.assertEqual(team["Board"]["your_role"], "viewer")
        self.assertFalse(team["Board"]["can_add_notes"])
        self.assertEqual([n["title"] for n in team["Handbook"]["notes"]], ["Guide"])
        # user2 has no My-Notes folders; user's "Drafts" is not theirs.
        self.assertEqual(res["folders"], [])

    def test_my_team_folders_no_longer_pass_as_my_notes_folders(self):
        self._note(folder=self.handbook, title="Guide")
        res = list_folders_run({}, self.ctx)
        self.assertEqual([f["name"] for f in res["folders"]], ["Drafts"])
        self.assertNotIn("Guide", [n["title"] for n in res["top_level_notes"]])
        self.assertIn("Handbook", [f["name"] for f in res["team_folders"]])

    def test_a_private_team_folder_you_lost_access_to_is_not_listed(self):
        NoteFolderPermission.objects.filter(folder=self.board, user=self.user2).delete()
        res = list_folders_run({}, self.ctx2)
        self.assertNotIn("Board", [f["name"] for f in res["team_folders"]])
