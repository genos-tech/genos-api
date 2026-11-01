"""MIP-866 — `fetch_note` reads a note the way the UI and `update_note` do.

`fetch_note` used to test `ctx.user_id in personal_note_acl_user_ids(...)`.
That set is built for the search index: a note in a PUBLIC Team Notes folder
contributes one `team:<team_id>` sentinel instead of user ids, so a teammate
who can open the note in the app was told "Not authorized". The personal
family now uses `acl.may_read_note` (the owner, or any role from
`get_effective_role`); task and chat notes keep their id sets, which hold
real user ids, and the tests below pin that they still answer correctly.

Callers: `self.user` owns the notes and the team; `self.user2` is a teammate;
`self.outsider` belongs only to another team.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.acl import may_read_note
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.base import ToolForbidden
from origin.search_engine.agent.tools.fetch_note import _run as fetch_note_run
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_VIEWER,
)

from .test_base import BaseAPITestCase


class FetchNoteAclBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = get_user_model().objects.create_user(
            username="outsider", email="outsider@example.com", password="x"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)

    def ctx(self, user, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(user.id))

    def fetch(self, note_type, note, user, team=None):
        return fetch_note_run(
            {"note_type": note_type, "note_id": note.note_id}, self.ctx(user, team)
        )

    def assertReadable(self, note_type, note, user, team=None):
        self.assertEqual(self.fetch(note_type, note, user, team)["note_id"], note.note_id)

    def assertRefused(self, note_type, note, user, team=None):
        with self.assertRaises(ToolForbidden):
            self.fetch(note_type, note, user, team)


class PersonalFamilyTests(FetchNoteAclBase):
    def folder(self, visibility, *, parent=None, scope=PersonalNoteFolder.SCOPE_TEAM, name="F"):
        return PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name=name,
            scope=scope,
            visibility=visibility,
            parent_folder_id=parent.folder_id if parent else None,
        )

    def note(self, folder=None, owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team,
            owner=owner or self.user,
            title="On-call rota",
            body=[],
            folder_id=folder.folder_id if folder else None,
        )

    # ---- the bug: a public Team Notes folder ---------------------------

    def test_a_teammate_reads_a_note_in_a_public_team_folder(self):
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC))
        self.assertReadable("personal", note, self.user2)

    def test_a_teammate_reads_a_note_in_a_subfolder_that_inherits_public(self):
        parent = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC, name="Handbook")
        child = self.folder(None, parent=parent, name="Runbooks")
        self.assertReadable("personal", self.note(child), self.user2)

    def test_a_non_member_of_the_team_is_refused_a_public_team_note(self):
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC))
        # Someone who left (soft-deleted membership) is no longer a teammate.
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertRefused("personal", note, self.user2)

    def test_a_user_from_another_team_is_refused_even_with_the_notes_team_id(self):
        """A caller whose run context somehow names this team still has no
        role: the folder resolver checks team membership itself."""
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC))
        self.assertRefused("personal", note, self.outsider)

    def test_the_tenant_check_refuses_a_note_from_another_team(self):
        """A member of BOTH teams, running the agent in the other team, would
        otherwise be an Editor through the folder: the run's team decides."""
        TeamMembers.objects.create(team=self.other_team, attendee=self.user2)
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC))
        self.assertRefused("personal", note, self.user2, team=self.other_team)

    # ---- private folders ----------------------------------------------

    def test_a_private_team_folder_refuses_a_teammate_without_a_grant(self):
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE))
        self.assertRefused("personal", note, self.user2)

    def test_a_private_team_folder_admits_a_teammate_holding_a_folder_grant(self):
        folder = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE)
        note = self.note(folder)
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.user2, role_id=ROLE_VIEWER
        )
        self.assertReadable("personal", note, self.user2)

    def test_a_private_subfolder_under_a_public_folder_refuses_a_teammate(self):
        parent = self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC, name="Handbook")
        child = self.folder(PersonalNoteFolder.VISIBILITY_PRIVATE, parent=parent, name="HR")
        self.assertRefused("personal", self.note(child), self.user2)

    # ---- personal notes, owner, shares ----------------------------------

    def test_the_owner_reads_their_own_unfiled_note_without_any_role_row(self):
        """Legacy notes have no `NotePermissionMaster` row and no folder."""
        self.assertReadable("personal", self.note(), self.user)

    def test_a_teammate_cannot_read_an_unshared_personal_note(self):
        personal = self.folder(None, scope=PersonalNoteFolder.SCOPE_PERSONAL)
        self.assertRefused("personal", self.note(personal), self.user2)
        self.assertRefused("personal", self.note(), self.user2)

    def test_a_note_shared_with_a_teammate_is_readable_by_them(self):
        note = self.note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_VIEWER,
        )
        self.assertReadable("personal", note, self.user2)

    # ---- fail closed ----------------------------------------------------

    def test_a_context_with_no_user_is_refused(self):
        note = self.note(self.folder(PersonalNoteFolder.VISIBILITY_PUBLIC))
        with self.assertRaises(ToolForbidden):
            fetch_note_run(
                {"note_type": "personal", "note_id": note.note_id},
                ToolContext(team_id=str(self.team.team_id), user_id=""),
            )

    def test_may_read_note_is_false_for_a_missing_user_even_for_an_owned_note(self):
        note = self.note()
        for user_id in ("", None):
            self.assertFalse(
                may_read_note(
                    user_id=user_id,
                    note_type_code=NOTE_TYPE_PERSONAL,
                    note_id=note.note_id,
                    owner_id=note.owner_id,
                )
            )

    def test_an_unknown_note_is_not_found_not_forbidden(self):
        from origin.search_engine.agent.tools.base import ToolError

        with self.assertRaises(ToolError) as caught:
            fetch_note_run({"note_type": "personal", "note_id": 999999}, self.ctx(self.user))
        self.assertNotIsInstance(caught.exception, ToolForbidden)


class TaskAndChatBranchesStayCorrectTests(FetchNoteAclBase):
    """Task and chat notes keep their id sets (real user ids, no sentinel):
    nothing was wrong there, so these pin the behaviour rather than change it."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, title="Research", body=[]
        )

    def test_a_project_member_reads_a_task_note(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertReadable("task", self.task_note, self.user2)

    def test_a_teammate_outside_the_project_is_refused_a_task_note(self):
        self.assertRefused("task", self.task_note, self.user2)

    def test_a_granted_teammate_reads_a_task_note(self):
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=self.task_note.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_VIEWER,
        )
        self.assertReadable("task", self.task_note, self.user2)

    def test_a_task_note_is_refused_across_teams(self):
        self.assertRefused("task", self.task_note, self.outsider, team=self.other_team)

    def _chat_note(self):
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Board", owner=self.user
        )
        ChannelMember.objects.create(channel=gm, user=self.user, role="owner")
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=int(ChannelKind.GM),
            channel=gm,
            is_thread=False,
            title="Decisions",
            body=[],
        )
        return gm, note

    def test_a_channel_member_reads_a_chat_note_and_a_non_member_does_not(self):
        gm, note = self._chat_note()
        self.assertRefused("chat", note, self.user2)
        ChannelMember.objects.create(channel=gm, user=self.user2, role="member")
        self.assertReadable("chat", note, self.user2)

    def test_a_chat_note_is_refused_across_teams(self):
        _gm, note = self._chat_note()
        self.assertRefused("chat", note, self.outsider, team=self.other_team)

    def test_the_chunker_note_codes_match_the_role_resolvers(self):
        # `fetch_note` feeds `may_read_note` the chunker constant;
        # `get_effective_role` compares to its own. They must be the same number.
        from origin.search_engine.chunkers import base

        self.assertEqual(base.NOTE_TYPE_PERSONAL, NOTE_TYPE_PERSONAL)
        self.assertEqual(base.NOTE_TYPE_TASK, NOTE_TYPE_TASK)
        self.assertEqual(base.NOTE_TYPE_CHAT, NOTE_TYPE_CHAT)
