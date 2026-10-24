"""Regression: demo teardown must delete a demo team's note versions.

`NoteVersionMaster.team` is `on_delete=SET_NULL` and the row also carries a
bare `note_id`, so nothing ties a version to its note. A demo user who edits
a note gets a version row for the demo team (collab saves one on every
edit), and `delete_demo_environment` then died on:

    IntegrityError: update or delete on table "origin_teammaster" violates
    foreign key constraint "origin_noteversionma_team_id_..._fk_origin_te"

which `cleanup_demo_users` catches per user, so that user — and their whole
workspace — was skipped on every run, forever.

MUST be a TransactionTestCase: the FK is DEFERRABLE INITIALLY DEFERRED, so
it is only checked at a real COMMIT, which TestCase never reaches.
"""

from django.contrib.auth import get_user_model
from django.test import TransactionTestCase
from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment

User = get_user_model()

PERSONAL, TASK, CHAT = 1, 2, 3


class DemoCleanupNoteVersionTests(TransactionTestCase):
    def _seeded_demo_user(self):
        """The product's own path: a seeded demo workspace, no hand-built rows."""
        user = User.objects.create_user(
            username="demo_nv_real",
            email="demo_nv_real@example.com",
            password="x",
            is_demo=True,
        )
        info = create_demo_environment(user)
        return user, info["team_id"]

    def _edit_a_seeded_note(self, user, team_id):
        """Type into a seeded My Note, as the task's steps do: the PUT
        writes the version row through `snapshot_note_version`."""
        note = PersonalNoteMaster.objects.filter(team=team_id, owner=user).first()
        self.assertIsNotNone(note, "the seeder should create a My Note for the user")
        client = APIClient()
        client.force_authenticate(user)
        response = client.put(
            "/api/v2/note/personal/",
            {
                "team_id": str(team_id),
                "note_id": note.note_id,
                "body": note.body,
                "title": "edited",
            },
            format="json",
        )
        self.assertLess(response.status_code, 300, response.content[:300])
        self.assertTrue(
            NoteVersionMaster.objects.filter(note_type=PERSONAL, note_id=note.note_id).exists(),
            "the save should have written a version row",
        )
        return note

    def test_a_demo_user_who_edited_a_note_is_deleted(self):
        user, team_id = self._seeded_demo_user()
        note = self._edit_a_seeded_note(user, team_id)

        delete_demo_environment(user)

        self.assertFalse(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(TeamMaster.objects.filter(team_id=team_id).exists())
        self.assertFalse(
            NoteVersionMaster.objects.filter(note_type=PERSONAL, note_id=note.note_id).exists()
        )

    def test_every_note_type_has_its_versions_removed(self):
        """Personal, task and chat notes all write versions through the same
        helper, and none of them cascade from the team."""
        user, team_id = self._seeded_demo_user()
        pairs = []
        for note_type, model in (
            (PERSONAL, PersonalNoteMaster),
            (TASK, TaskNoteMaster),
            (CHAT, ChatNoteMaster),
        ):
            note = model.objects.filter(team=team_id).first()
            if note is None:
                self.skipTest(f"the seeder made no note of type {note_type}")
            NoteVersionMaster.objects.create(
                team_id=team_id,
                editor=user,
                note_type=note_type,
                note_id=note.note_id,
                version_no=1,
                title="t",
            )
            pairs.append((note_type, note.note_id))

        delete_demo_environment(user)

        for note_type, note_id in pairs:
            self.assertFalse(
                NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id).exists(),
                f"type {note_type} version survived",
            )

    def test_another_teams_versions_are_left_alone(self):
        """The delete is scoped by team: cleaning up one demo user must not
        touch a different team's history, demo or not."""
        user, team_id = self._seeded_demo_user()
        other = User.objects.create_user(
            username="real_owner", email="real_owner@example.com", password="x"
        )
        other_team = TeamMaster.objects.create(
            team_name="Real Team", team_email="real_team@example.com", owner=other
        )
        keep = NoteVersionMaster.objects.create(
            team=other_team,
            editor=other,
            note_type=PERSONAL,
            note_id=987654321,
            version_no=1,
            title="keep me",
        )

        delete_demo_environment(user)

        self.assertTrue(NoteVersionMaster.objects.filter(pk=keep.pk).exists())
