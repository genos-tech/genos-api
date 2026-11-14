"""MIP-1551 (2): undoing an agent-created note deletes it only for someone who still has a role.

`undo_apply._may_delete_note` let "the author" delete when their effective role was `None`
(`wrote_it and (role is None or role <= ROLE_EDITOR)`). That is right for a PERSONAL note (a legacy
note of a private folder has no role row and no implicit access: its author is its owner), and
wrong for the two shared families: a CHAT note's author who left the chat (MIP-1492, api #766) or a
TASK note's author who left the project (MIP-1511, api #774) holds no role on the note on every
other gate, and could still delete it by pressing Undo on the agent step that made it.

Now, as `has_write_permission` already does for a write: the author exception is personal-only;
for a chat or task note the effective role decides (an explicit row, else membership by the one
rule), so an author who left needs a role again (a deliberate share, a row of a soft-deleted chat,
Ken's MIP-1505 decision) to delete it.

What is pinned:
  * the decision itself, as a matrix over the three families (`_may_delete_note`);
  * the real endpoint: a leaver's Undo is refused `permission_lost` and the note stays. The state
    guard (`note_access_hash`) already refuses most leavers with `changed_since`, because the leave
    ends the author's row; the tests realign the stored access hash to what it is now, so that
    ONLY the permission gate is under test (a leave that leaves the access set as it was, e.g. a
    legacy note whose rows were never written, reaches it).
"""

from __future__ import annotations

import copy

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent import undo, undo_apply
from origin.search_engine.models import AgentStep
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER

from .test_agent_undo_capture import PERSONAL, blocks
from .test_agent_undo_endpoint import UndoBase

CHAT = 3
TASK = 2


class Builders(UndoBase):
    """`self.user` is the author under test, `self.other` the other person of every chat/project."""

    def gm(self, *, deleted=False):
        channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="gm",
            owner=self.other,
            is_deleted=deleted,
        )
        for m in (self.user, self.other):
            ChannelMember.objects.create(
                channel=channel, user=m, role="owner" if m == self.other else "member"
            )
        return channel

    def their_project(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            code="THR",
            owner=self.other,
            project_system_user=self.other,
        )
        for m in (self.user, self.other):
            ProjectMembers.objects.create(team=self.team, project=project, attendee=m)
        return project

    def leave_chat(self, channel):
        res = self.client.delete(f"/api/v3/channels/{channel.id}/members/{self.user.id}/")
        self.assertEqual(res.status_code, 204, res.content)

    def row(self, family_code, note, role, user=None):
        NotePermissionMaster.objects.update_or_create(
            team=self.team,
            user=user or self.user,
            note_type=family_code,
            note_id=note.note_id,
            defaults={"role_id": role},
        )

    def clear_rows(self, family_code, note):
        NotePermissionMaster.objects.filter(note_type=family_code, note_id=note.note_id).delete()

    def realign_access(self, step, family, note):
        """What the state guard compares, made equal to now: only the permission gate is left."""
        body = copy.deepcopy(step.undo_json)
        body["image"]["access"] = undo.note_access_hash(family, note.note_id)
        AgentStep.objects.filter(pk=step.pk).update(undo_json=body)

    def may_delete(self, code, note, user=None):
        return undo_apply._may_delete_note(str((user or self.user).id), code, note)


class TheDecisionForAChatNote(Builders):
    def setUp(self):
        super().setUp()
        self.chat = self.gm()
        self.note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=ChannelKind.GM,
            channel=self.chat,
            is_thread=False,
            title="Chat note",
            body=blocks("x"),
        )

    def test_a_member_author_with_no_row_may_delete(self):
        self.assertTrue(self.may_delete(CHAT, self.note))

    def test_an_author_with_an_editor_or_owner_row_may_delete(self):
        for role in (ROLE_EDITOR, ROLE_OWNER):
            with self.subTest(role=role):
                self.row(CHAT, self.note, role)
                self.assertTrue(self.may_delete(CHAT, self.note))

    def test_an_author_made_a_reader_of_their_own_note_may_not(self):
        self.row(CHAT, self.note, ROLE_VIEWER)
        self.assertFalse(self.may_delete(CHAT, self.note))

    def test_an_author_who_left_with_no_row_may_not(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.user).update(is_deleted=True)
        self.assertFalse(self.may_delete(CHAT, self.note))

    def test_an_author_who_left_and_holds_a_viewer_share_may_not(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.user).update(is_deleted=True)
        self.row(CHAT, self.note, ROLE_VIEWER)
        self.assertFalse(self.may_delete(CHAT, self.note))

    def test_an_author_who_left_and_was_given_an_editor_or_owner_row_may(self):
        ChannelMember.objects.filter(channel=self.chat, user=self.user).update(is_deleted=True)
        for role in (ROLE_EDITOR, ROLE_OWNER):
            with self.subTest(role=role):
                self.row(CHAT, self.note, role)
                self.assertTrue(self.may_delete(CHAT, self.note))

    def test_a_soft_deleted_chat_the_author_keeps_their_row_but_not_the_bare_fk(self):
        """MIP-1505: the row wins, the channel's liveness is not asked; with no row, membership
        counts only while the channel is live."""
        Channel.objects.filter(id=self.chat.id).update(is_deleted=True)
        self.assertFalse(self.may_delete(CHAT, self.note))
        self.row(CHAT, self.note, ROLE_OWNER)
        self.assertTrue(self.may_delete(CHAT, self.note))

    def test_an_owner_who_is_not_the_author_may_and_an_editor_who_is_not_may_not(self):
        self.clear_rows(CHAT, self.note)
        self.row(CHAT, self.note, ROLE_OWNER, user=self.other)
        self.assertTrue(self.may_delete(CHAT, self.note, user=self.other))
        self.row(CHAT, self.note, ROLE_EDITOR, user=self.other)
        self.assertFalse(self.may_delete(CHAT, self.note, user=self.other))


class TheDecisionForATaskNote(Builders):
    def setUp(self):
        super().setUp()
        self.project = self.their_project()
        self.note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, title="Plan", body=blocks("x")
        )

    def test_a_member_author_may_delete(self):
        self.assertTrue(self.may_delete(TASK, self.note))

    def test_an_author_taken_off_the_project_with_no_row_may_not(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        self.assertFalse(self.may_delete(TASK, self.note))

    def test_an_author_taken_off_the_project_who_holds_an_owner_row_may(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user).delete()
        self.row(TASK, self.note, ROLE_OWNER)
        self.assertTrue(self.may_delete(TASK, self.note))

    def test_an_author_made_a_reader_of_their_own_note_may_not(self):
        self.row(TASK, self.note, ROLE_VIEWER)
        self.assertFalse(self.may_delete(TASK, self.note))


class ThePersonalNoteIsUnchanged(Builders):
    """A private note has no implicit access: its author is its owner, row or no row."""

    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=blocks("x")
        )

    def test_the_author_with_no_row_may_delete(self):
        self.assertTrue(self.may_delete(PERSONAL, self.note))

    def test_the_author_with_an_owner_row_may_delete(self):
        self.row(PERSONAL, self.note, ROLE_OWNER)
        self.assertTrue(self.may_delete(PERSONAL, self.note))

    def test_the_author_made_a_reader_of_their_own_note_may_not(self):
        self.row(PERSONAL, self.note, ROLE_VIEWER)
        self.assertFalse(self.may_delete(PERSONAL, self.note))

    def test_someone_else_with_no_row_may_not(self):
        self.assertFalse(self.may_delete(PERSONAL, self.note, user=self.other))


class UndoingTheNoteTheAgentMade(Builders):
    def made_chat_note(self):
        chat = self.gm()
        run, step = self.go(
            "create_note",
            {
                "note_type": "chat",
                "chat_id": str(chat.id),
                "title": "Chat made",
                "content_text": "hi",
            },
        )
        return chat, ChatNoteMaster.objects.get(title="Chat made"), run, step

    def test_a_member_author_undoes_it(self):
        _chat, note, run, step = self.made_chat_note()
        self.undone(run, step)
        self.assertFalse(ChatNoteMaster.objects.filter(pk=note.pk).exists())

    def test_an_author_who_left_the_chat_may_not_undo_it(self):
        chat, note, run, step = self.made_chat_note()
        self.leave_chat(chat)
        self.refused(run, step, "permission_lost", 403)  # the permission is asked first
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk).exists())

    def test_an_author_who_left_is_refused_on_permission_alone(self):
        chat, note, run, step = self.made_chat_note()
        self.leave_chat(chat)
        self.realign_access(step, "chat", note)
        self.refused(run, step, "permission_lost", 403)
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk).exists())

    def test_an_author_who_left_a_note_without_rows_is_refused_too(self):
        """The legacy shape: no row for anyone, so the leave changes no access set at all."""
        chat, note, run, step = self.made_chat_note()
        self.clear_rows(CHAT, note)
        self.realign_access(step, "chat", note)
        ChannelMember.objects.filter(channel=chat, user=self.user).update(is_deleted=True)
        self.refused(run, step, "permission_lost", 403)
        self.assertTrue(ChatNoteMaster.objects.filter(pk=note.pk).exists())

    def test_a_task_note_author_who_left_the_project_is_refused_on_permission_alone(self):
        project = self.their_project()
        run, step = self.go(
            "create_note",
            {
                "note_type": "task",
                "title": "Plan made",
                "content_text": "text",
                "project_id": project.project_id,
            },
        )
        note = TaskNoteMaster.objects.get(title="Plan made")
        ProjectMembers.objects.filter(project=project, attendee=self.user).delete()
        self.realign_access(step, "task", note)
        self.refused(run, step, "permission_lost", 403)
        self.assertTrue(TaskNoteMaster.objects.filter(pk=note.pk).exists())

    def test_a_task_note_author_who_is_still_a_member_undoes_it(self):
        project = self.their_project()
        run, step = self.go(
            "create_note",
            {
                "note_type": "task",
                "title": "Plan made",
                "content_text": "text",
                "project_id": project.project_id,
            },
        )
        note = TaskNoteMaster.objects.get(title="Plan made")
        self.undone(run, step)
        self.assertFalse(TaskNoteMaster.objects.filter(pk=note.pk).exists())
