"""MIP-1489: deleting a note takes its "recently opened" rows and its @mention feed rows with it.

`services/note_delete.py` hard-deletes the note, its permission rows, its versions and its search
chunks, but left (a) every person's `NoteRecentMaster` row for it, and (b) the `Activity` rows the
note's @mentions and comments wrote for other people (surface types 6/7/8, `meta.noteId`). The
frontend drops the second kind from its own feed on delete (frontend #945), but a full resync
(`GET /api/v3/activities/`) brought them back, pointing at a note that no longer exists.

Both go with the note now, on every delete path: one note, a My Notes folder, a Team Notes folder.
Rows of other notes (and of other note families with the same numeric id) are untouched.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
)
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import note_delete
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.author = self.user
        self.reader = self.user2

    def recent(self, user, code, note_id):
        return NoteRecentMaster.objects.create(
            team=self.team, user=user, note_id=note_id, note_type=code
        )

    def mention(self, recipient, surface, note_id, *, as_text=False):
        return Activity.objects.create(
            team=self.team,
            recipient=recipient,
            actor=self.author,
            activity_type=ActivityType.MENTION,
            surface_type=surface,
            meta={"noteId": str(note_id) if as_text else note_id},
        )

    def feed_note_ids(self, user):
        """What a full resync hands the person: `GET /api/v3/activities/` with no `since`."""
        self.authenticate(user)
        res = self.client.get("/api/v3/activities/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        return {a["meta"].get("noteId") for a in res.json()["activities"] if a.get("meta")}


class DeletingOneNote(Base):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="gone", body=[]
        )
        self.keep = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="kept", body=[]
        )
        for n in (self.note, self.keep):
            NotePermissionMaster.objects.create(
                team=self.team, user=self.author, note_id=n.note_id, note_type=1, role_id=1
            )
            self.recent(self.author, 1, n.note_id)
            self.recent(self.reader, 1, n.note_id)
            self.mention(self.reader, 6, n.note_id)

    def delete_via_rest(self):
        self.authenticate(self.author)
        res = self.client.delete(
            f"/api/v2/note/personal/?team_id={self.team.team_id}&note_id={self.note.note_id}"
        )
        self.assertIn(res.status_code, (200, 204), res.content)

    def test_the_recents_of_everyone_go_with_the_note(self):
        self.delete_via_rest()
        self.assertFalse(
            NoteRecentMaster.objects.filter(note_type=1, note_id=self.note.note_id).exists()
        )

    def test_the_mention_rows_go_and_a_full_resync_no_longer_brings_them_back(self):
        self.assertIn(self.note.note_id, self.feed_note_ids(self.reader))
        self.delete_via_rest()
        self.assertEqual(self.feed_note_ids(self.reader), {self.keep.note_id})

    def test_a_note_id_the_client_sent_as_text_is_matched_too(self):
        self.mention(self.reader, 6, self.note.note_id, as_text=True)
        self.delete_via_rest()
        self.assertFalse(Activity.objects.filter(meta__noteId=str(self.note.note_id)).exists())

    def test_other_notes_are_untouched(self):
        self.delete_via_rest()
        self.assertTrue(
            NoteRecentMaster.objects.filter(note_type=1, note_id=self.keep.note_id).count() == 2
        )
        self.assertEqual(Activity.objects.filter(meta__noteId=self.keep.note_id).count(), 1)

    def test_a_note_of_another_family_with_the_same_numeric_id_is_untouched(self):
        # ids are per family: a task-note row and a task-note mention with the same number stay
        self.recent(self.reader, 2, self.note.note_id)
        self.mention(self.reader, 7, self.note.note_id)
        self.delete_via_rest()
        self.assertTrue(
            NoteRecentMaster.objects.filter(note_type=2, note_id=self.note.note_id).exists()
        )
        self.assertTrue(Activity.objects.filter(surface_type=7).exists())


class DeletingTaskAndChatNotes(Base):
    def test_a_task_note_and_a_chat_note_take_their_rows_with_them(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.author, project_system_user=self.author
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.author)
        task_note = TaskNoteMaster.objects.create(
            team=self.team, owner=self.author, project=project, title="t", body=[]
        )
        channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="gm")
        ChannelMember.objects.create(channel=channel, user=self.author)
        chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.author,
            chat_type=channel.kind,
            channel=channel,
            is_thread=False,
            title="c",
            body=[],
        )
        for family, code, surface, note in (
            ("task", 2, 7, task_note),
            ("chat", 3, 8, chat_note),
        ):
            self.recent(self.reader, code, note.note_id)
            self.mention(self.reader, surface, note.note_id)
            note_delete.delete_note(team_id=self.team.team_id, family=family, note_id=note.note_id)
            self.assertFalse(
                NoteRecentMaster.objects.filter(note_type=code, note_id=note.note_id).exists(),
                family,
            )
            self.assertFalse(Activity.objects.filter(surface_type=surface).exists(), family)


class DeletingAFolder(Base):
    def test_a_my_notes_folder_takes_the_rows_of_every_note_in_it(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.author, name="F", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        notes = [
            PersonalNoteMaster.objects.create(
                team=self.team,
                owner=self.author,
                title=f"n{i}",
                body=[],
                folder_id=folder.folder_id,
            )
            for i in range(2)
        ]
        outside = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="out", body=[]
        )
        for n in (*notes, outside):
            self.recent(self.reader, 1, n.note_id)
            self.mention(self.reader, 6, n.note_id)
        note_delete.delete_personal_folder(
            team_id=self.team.team_id, owner_id=self.author.id, folder_id=folder.folder_id
        )
        left = set(NoteRecentMaster.objects.values_list("note_id", flat=True))
        self.assertEqual(left, {outside.note_id})
        self.assertEqual(
            set(Activity.objects.values_list("meta__noteId", flat=True)), {outside.note_id}
        )

    def test_a_team_notes_folder_takes_them_too(self):
        from origin.models.note.common_note_models import NoteFolderPermission

        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.author, name="T", scope=PersonalNoteFolder.SCOPE_TEAM
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=folder, user=self.author, role_id=1
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.author, title="n", body=[], folder_id=folder.folder_id
        )
        self.recent(self.reader, 1, note.note_id)
        self.mention(self.reader, 6, note.note_id)
        note_delete.delete_team_folder(
            team_id=self.team.team_id, user_id=self.author.id, folder=folder
        )
        self.assertFalse(NoteRecentMaster.objects.exists())
        self.assertFalse(Activity.objects.exists())
