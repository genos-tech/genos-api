"""Favorites and recent notes stored, and then listed, any note id (MIP-713).

`NoteFavoriteView.post` and `RecordNoteOpenView.post` wrote whatever
`note_id` / `note_type` the body named, and the two `meta` reads looked the
ids up by `note_id__in` with no access filter at all. Note ids are
`BigAutoField`s, so favoriting 1..N of each type and reading the sidebar
listed the title of every note on the install: other users' personal notes,
task notes from projects the caller is not on (with the project name, task
title and display id), and chat notes with their channel ids.

The rule now is the note editor's own, `note_role.get_effective_role`: a
note may be stored only if the caller can open it, and a listing keeps only
the notes the caller can still open, since access can end after the row was
written. A refusal is the 404 a missing note gets.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import ROLE_OWNER, ROLE_VIEWER, readable_note_ids

FAVORITE = "/api/v2/note/favorite/"
FAVORITE_META = "/api/v2/note/favorite/meta/"
RECENT = "/api/v2/note/recent/"
RECENT_META = "/api/v2/note/recent/meta/"

PERSONAL, TASK, CHAT = 1, 2, 3


class NoteRefAccessBase(BaseAPITestCase):
    """user owns one note of each kind; user2 is on the team but on none of
    them; the outsider is in another team altogether."""

    def setUp(self):
        super().setUp()
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="My diary", body=[]
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.personal.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Leadership",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Layoff plan", status="Open"
        )
        self.task_note = TaskNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            project=self.project,
            task=self.task,
            title="Who goes",
            body=[],
        )

        self.channel = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="Board")
        ChannelMember.objects.create(channel=self.channel, user=self.user)
        self.chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="Board minutes",
            body=[],
            chat_type=1,
            channel=self.channel,
            is_thread=False,
        )

        self.outsider = self._make_user("noteout")
        self.outsider_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="noteout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    def _make_user(self, name):
        from django.contrib.auth import get_user_model

        return get_user_model().objects.create_user(
            username=name, email=f"{name}@example.com", password="pw12345678"
        )

    def _refs(self):
        return (
            (self.personal.note_id, PERSONAL),
            (self.task_note.note_id, TASK),
            (self.chat_note.note_id, CHAT),
        )

    def _body(self, user, team, note_id, note_type):
        return {
            "team_id": str(team.team_id),
            "user_id": str(user.id),
            "note_id": note_id,
            "note_type": note_type,
        }

    def _meta(self, url, user, team):
        return self.client.get(url, {"team_id": str(team.team_id), "user_id": str(user.id)})


class FavoriteAccessTests(NoteRefAccessBase):
    def test_a_teammate_cannot_favorite_notes_they_cannot_open(self):
        self.authenticate(self.user2)
        for note_id, note_type in self._refs():
            res = self.client.post(
                FAVORITE, self._body(self.user2, self.team, note_id, note_type), format="json"
            )
            self.assertEqual(res.status_code, 404, (note_type, res.data))
        self.assertFalse(NoteFavoriteMaster.objects.exists())

    def test_an_outsider_cannot_favorite_them_either(self):
        self.authenticate(self.outsider)
        for note_id, note_type in self._refs():
            res = self.client.post(
                FAVORITE,
                self._body(self.outsider, self.outsider_team, note_id, note_type),
                format="json",
            )
            self.assertEqual(res.status_code, 404, (note_type, res.data))
        self.assertFalse(NoteFavoriteMaster.objects.exists())

    def test_a_missing_note_and_a_bad_type_are_the_same_404(self):
        self.authenticate(self.user)
        for note_id, note_type in ((999999, PERSONAL), (self.personal.note_id, 7), ("x", 1)):
            res = self.client.post(
                FAVORITE, self._body(self.user, self.team, note_id, note_type), format="json"
            )
            self.assertEqual(res.status_code, 404, (note_id, note_type))

    def test_a_grant_left_behind_by_a_deleted_note_is_not_enough(self):
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=999999, note_type=PERSONAL, role_id=ROLE_VIEWER
        )
        self.authenticate(self.user2)
        res = self.client.post(
            FAVORITE, self._body(self.user2, self.team, 999999, PERSONAL), format="json"
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(NoteFavoriteMaster.objects.exists())

    def test_the_owner_can_favorite_each_kind(self):
        self.authenticate(self.user)
        for note_id, note_type in self._refs():
            res = self.client.post(
                FAVORITE, self._body(self.user, self.team, note_id, note_type), format="json"
            )
            self.assertEqual(res.status_code, 201, (note_type, res.data))
        self.assertEqual(NoteFavoriteMaster.objects.filter(user=self.user).count(), 3)

    def test_a_string_note_type_still_resolves(self):
        """A JSON body may send the type as "2"; the role check compares to
        ints, so an uncoerced string read as no access at all."""
        self.authenticate(self.user)
        res = self.client.post(
            FAVORITE,
            self._body(self.user, self.team, str(self.task_note.note_id), "2"),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        # The client types both as numbers and compares ids with ===.
        self.assertEqual((res.data["noteId"], res.data["noteType"]), (self.task_note.note_id, 2))

    def test_the_listing_leaves_out_rows_the_caller_cannot_open(self):
        """Rows written before this fix, or whose access has since ended."""
        for note_id, note_type in self._refs():
            NoteFavoriteMaster.objects.create(
                team=self.team, user=self.user2, note_id=note_id, note_type=note_type
            )
        self.authenticate(self.user2)
        res = self._meta(FAVORITE_META, self.user2, self.team)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"personalNotes": [], "taskNotes": [], "chatNotes": []})
        for secret in ("My diary", "Who goes", "Layoff plan", "Leadership", "Board minutes"):
            self.assertNotIn(secret, str(res.data))

    def test_the_owner_still_sees_all_three(self):
        for note_id, note_type in self._refs():
            NoteFavoriteMaster.objects.create(
                team=self.team, user=self.user, note_id=note_id, note_type=note_type
            )
        self.authenticate(self.user)
        res = self._meta(FAVORITE_META, self.user, self.team)
        self.assertEqual(
            [len(res.data[k]) for k in ("personalNotes", "taskNotes", "chatNotes")], [1, 1, 1]
        )

    def test_leaving_the_project_takes_the_favorite_out_of_the_list(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate(self.user2)
        res = self.client.post(
            FAVORITE,
            self._body(self.user2, self.team, self.task_note.note_id, TASK),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        res = self._meta(FAVORITE_META, self.user2, self.team)
        self.assertEqual(res.data["taskNotes"], [])

    def test_unfavoriting_a_note_you_lost_access_to_still_works(self):
        """Clearing a stale row must not need the access that went away."""
        NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user2, note_id=self.task_note.note_id, note_type=TASK
        )
        self.authenticate(self.user2)
        res = self.client.delete(
            f"{FAVORITE}?team_id={self.team.team_id}&user_id={self.user2.id}"
            f"&note_id={self.task_note.note_id}&note_type={TASK}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(NoteFavoriteMaster.objects.exists())


class RecentAccessTests(NoteRefAccessBase):
    def test_a_teammate_cannot_record_opening_notes_they_cannot_open(self):
        self.authenticate(self.user2)
        for note_id, note_type in self._refs():
            res = self.client.post(
                RECENT, self._body(self.user2, self.team, note_id, note_type), format="json"
            )
            self.assertEqual(res.status_code, 404, (note_type, res.data))
        self.assertFalse(NoteRecentMaster.objects.exists())

    def test_the_owner_can_record_each_kind(self):
        self.authenticate(self.user)
        for note_id, note_type in self._refs():
            res = self.client.post(
                RECENT, self._body(self.user, self.team, note_id, note_type), format="json"
            )
            self.assertEqual(res.status_code, 200, (note_type, res.data))
        self.assertEqual(NoteRecentMaster.objects.filter(user=self.user).count(), 3)

    def test_the_listing_leaves_out_rows_the_caller_cannot_open(self):
        for note_id, note_type in self._refs():
            NoteRecentMaster.objects.create(
                team=self.team, user=self.user2, note_id=note_id, note_type=note_type
            )
        self.authenticate(self.user2)
        res = self._meta(RECENT_META, self.user2, self.team)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data, {"personalNotes": [], "taskNotes": [], "chatNotes": []})

    def test_the_owner_still_sees_all_three(self):
        for note_id, note_type in self._refs():
            NoteRecentMaster.objects.create(
                team=self.team, user=self.user, note_id=note_id, note_type=note_type
            )
        self.authenticate(self.user)
        res = self._meta(RECENT_META, self.user, self.team)
        self.assertEqual(
            [len(res.data[k]) for k in ("personalNotes", "taskNotes", "chatNotes")], [1, 1, 1]
        )


class ReadableNoteIdsTests(NoteRefAccessBase):
    """The batched rule has to agree with `get_effective_role` on every
    path it can grant by, and on the boundaries either side."""

    def test_nothing_for_a_stranger(self):
        for note_id, note_type in self._refs():
            self.assertEqual(readable_note_ids(self.user2.id, note_type, [note_id]), set())

    def test_an_explicit_grant_reaches_any_kind(self):
        for note_id, note_type in self._refs():
            NotePermissionMaster.objects.create(
                team=self.team,
                user=self.user2,
                note_id=note_id,
                note_type=note_type,
                role_id=ROLE_VIEWER,
            )
            self.assertEqual(readable_note_ids(self.user2.id, note_type, [note_id]), {note_id})

    def test_project_membership_reaches_task_notes(self):
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertEqual(
            readable_note_ids(self.user2.id, TASK, [self.task_note.note_id]),
            {self.task_note.note_id},
        )

    def test_chat_membership_reaches_chat_notes_but_not_once_left(self):
        member = ChannelMember.objects.create(channel=self.channel, user=self.user2)
        self.assertEqual(
            readable_note_ids(self.user2.id, CHAT, [self.chat_note.note_id]),
            {self.chat_note.note_id},
        )
        member.is_deleted = True
        member.save()
        self.assertEqual(readable_note_ids(self.user2.id, CHAT, [self.chat_note.note_id]), set())

    def test_a_public_team_folder_reaches_the_personal_notes_in_it(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        self.personal.folder_id = folder.folder_id
        self.personal.save()
        self.assertEqual(
            readable_note_ids(self.user2.id, PERSONAL, [self.personal.note_id]),
            {self.personal.note_id},
        )
        # The team folder is open to the team, not to everyone.
        self.assertEqual(
            readable_note_ids(self.outsider.id, PERSONAL, [self.personal.note_id]), set()
        )

    def test_a_private_team_folder_does_not(self):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="HR",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE,
        )
        self.personal.folder_id = folder.folder_id
        self.personal.save()
        self.assertEqual(readable_note_ids(self.user2.id, PERSONAL, [self.personal.note_id]), set())

    def test_one_type_never_answers_for_another(self):
        """Note ids collide across the three tables; a grant on one kind
        must not read as a grant on the same number of another."""
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        shared_id = self.task_note.note_id
        for model in (PersonalNoteMaster, ChatNoteMaster):
            model.objects.filter(note_id=shared_id).delete()
        PersonalNoteMaster.objects.create(
            note_id=shared_id, team=self.team, owner=self.user, title="Same number", body=[]
        )
        ChatNoteMaster.objects.create(
            note_id=shared_id,
            team=self.team,
            owner=self.user,
            title="Same number",
            body=[],
            chat_type=1,
            channel=self.channel,
            is_thread=False,
        )
        self.assertEqual(readable_note_ids(self.user2.id, TASK, [shared_id]), {shared_id})
        self.assertEqual(readable_note_ids(self.user2.id, PERSONAL, [shared_id]), set())
        self.assertEqual(readable_note_ids(self.user2.id, CHAT, [shared_id]), set())

    def test_an_explicit_grant_names_its_type_too(self):
        note_id = self.task_note.note_id
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user2, note_id=note_id, note_type=TASK, role_id=ROLE_VIEWER
        )
        self.assertEqual(readable_note_ids(self.user2.id, TASK, [note_id]), {note_id})
        self.assertEqual(readable_note_ids(self.user2.id, PERSONAL, [note_id]), set())
        self.assertEqual(readable_note_ids(self.user2.id, CHAT, [note_id]), set())
