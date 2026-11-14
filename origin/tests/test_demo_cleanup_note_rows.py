"""MIP-1506: what a demo teardown leaves behind about the team's notes: nothing.

`delete_demo_team_data` deletes `ProjectMembers` and `ProjectMaster` for a team without calling
the chat-note role revoke (`note_role_revoke`, the `ProjectMembers` post_delete receiver of
#763). That is fine only if every row that revoke would have removed dies anyway. This pins it,
over the product's own seeded demo workspace plus the rows a visitor's use writes:

  * `NotePermissionMaster`: the roles a project, group or direct chat's membership wrote on its
    chat notes, including a row whose `team` is NULL (the FK is SET_NULL, so a team-scoped
    delete would never find it), and the row of a user who is not a member of the demo team;
  * `NoteVersionMaster`, `NoteSummary` (no FK to the note), `NoteFavoriteMaster`,
    `NoteRecentMaster`, and the comment / mention `Activity` rows (surface 6/7/8, `meta.noteId`).

The teardown deletes the notes with an explicit permission delete, then `forget_deleted_notes`,
and every note delete also fires the satellite `post_delete` receiver (#781), so the rows go by
the note's id and not by the team's. Dropping the revoke from the teardown loses nothing.

MUST be a TransactionTestCase, like the other demo-cleanup tests: the teardown's deferred FKs are
only checked at a real COMMIT.
"""

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
)
from origin.models.common.team_models import TeamMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMembers
from origin.search_engine.models import NoteSummary
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

User = get_user_model()

PERSONAL, TASK, CHAT = 1, 2, 3
SURFACE = {PERSONAL: 6, TASK: 7, CHAT: 8}


class DemoTeardownNoteRows(TransactionTestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="demo_nr", email="demo_nr@example.com", password="x", is_demo=True
        )
        self.stranger = User.objects.create_user(
            username="nr_stranger", email="nr_stranger@example.com", password="x"
        )
        self.team_id = create_demo_environment(self.user)["team_id"]

    def chat_notes_on_every_kind(self):
        """A chat note on a project chat, a group chat and a direct message of the demo team."""
        notes = []
        for kind in (ChannelKind.PM, ChannelKind.GM, ChannelKind.DM):
            channel = Channel.objects.filter(team=self.team_id, kind=kind).first()
            if channel is None:
                continue
            note = ChatNoteMaster.objects.create(
                team_id=self.team_id,
                owner=self.user,
                chat_type=int(kind),
                channel=channel,
                is_thread=False,
                title=f"note on kind {kind}",
                body=[],
            )
            notes.append(note)
        self.assertIn(int(ChannelKind.PM), {n.chat_type for n in notes}, "no project chat note")
        return notes

    def decorate(self, code, note_id):
        """Every row that points at a note by number only."""
        NoteVersionMaster.objects.create(
            team_id=self.team_id,
            editor=self.user,
            note_type=code,
            note_id=note_id,
            version_no=1,
            title="t",
        )
        NoteSummary.objects.create(
            team_id=str(self.team_id), note_type=code, note_id=note_id, summary_text="clear text"
        )
        NoteFavoriteMaster.objects.create(
            team_id=self.team_id, user=self.user, note_type=code, note_id=note_id
        )
        NoteRecentMaster.objects.create(
            team_id=self.team_id, user=self.user, note_type=code, note_id=note_id
        )
        Activity.objects.create(
            team_id=self.team_id,
            recipient=self.user,
            actor=self.user,
            activity_type=ActivityType.THREAD_REPLY,
            surface_type=SURFACE[code],
            meta={"noteId": note_id, "isComment": True},
        )

    def test_nothing_about_the_teams_notes_survives_the_teardown(self):
        pairs = []
        for note in self.chat_notes_on_every_kind():
            pairs.append((CHAT, note.note_id))
            # the owner's role, and a role the chat's membership wrote for a person who is not
            # on the demo team at all, with the team FK nulled (SET_NULL left it so)
            NotePermissionMaster.objects.create(
                team_id=self.team_id,
                user=self.user,
                note_type=CHAT,
                note_id=note.note_id,
                role_id=ROLE_OWNER,
            )
            NotePermissionMaster.objects.create(
                team=None,
                user=self.stranger,
                note_type=CHAT,
                note_id=note.note_id,
                role_id=ROLE_EDITOR,
            )
        for code, model in ((PERSONAL, PersonalNoteMaster), (TASK, TaskNoteMaster)):
            note = model.objects.filter(team=self.team_id).first()
            if note is not None:
                pairs.append((code, note.note_id))
        self.assertTrue(any(code == CHAT for code, _ in pairs), "no chat note was built")
        self.assertTrue(any(code != CHAT for code, _ in pairs), "the seeder made no other note")
        for code, note_id in pairs:
            self.decorate(code, note_id)
        # the seeded chat members and the project members are the rows the revoke would walk
        self.assertTrue(ProjectMembers.objects.filter(team=self.team_id).exists())
        self.assertTrue(ChannelMember.objects.filter(channel__team=self.team_id).exists())
        keep = NoteSummary.objects.create(
            team_id="someone-else", note_type=CHAT, note_id=987_654, summary_text="keep me"
        )

        delete_demo_environment(self.user)

        self.assertFalse(TeamMaster.objects.filter(team_id=self.team_id).exists())
        for code, note_id in pairs:
            for model in (
                NotePermissionMaster,
                NoteVersionMaster,
                NoteSummary,
                NoteFavoriteMaster,
                NoteRecentMaster,
            ):
                self.assertFalse(
                    model.objects.filter(note_type=code, note_id=note_id).exists(),
                    f"{model.__name__} of note {code}:{note_id} survived the teardown",
                )
            self.assertFalse(
                Activity.objects.filter(surface_type=SURFACE[code], meta__noteId=note_id).exists(),
                f"the activity rows of note {code}:{note_id} survived the teardown",
            )
        # only the demo team's notes are touched
        self.assertTrue(NoteSummary.objects.filter(pk=keep.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.stranger.pk).exists())
