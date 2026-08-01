"""The Genos Guide (seeded user-manual my-notes, per user per team).

Properties pinned: every member gets their OWN copy on join (that's what
makes the manual retrievable by everyone's Spotlight under note ACLs);
the membership stamp makes seeding once-per-membership — including after
the user deletes the folder (hard recursive delete) — and repeat
/team/join/ calls (every team switch) stay cheap no-ops; a guide failure
never breaks a join; demo users are excluded.
"""

from unittest import mock

from rest_framework import status

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services.guide_notes import GUIDE_FOLDER_NAME, GUIDE_NOTES, seed_guide_notes
from origin.tests.test_base import BaseAPITestCase

JOIN_URL = "/api/v2/team/join/"


def _mock_reindex():
    return mock.patch("origin.services.demo_seeder.kick_off_demo_reindex")


class GuideNotesOnJoinTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # A second team the fixture user is NOT yet a member of.
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other@example.com", owner=self.user2
        )

    def _join(self, user, team, attendee=None):
        self.authenticate(user)
        with _mock_reindex():
            return self.client.post(
                JOIN_URL,
                {"team_id": str(team.team_id), "attendee_id": str((attendee or user).id)},
                format="json",
            )

    def _guide_notes(self, user, team):
        return PersonalNoteMaster.objects.filter(
            team=team, owner=user, folder_id__isnull=False
        ).filter(
            folder_id__in=PersonalNoteFolder.objects.filter(
                team=team, owner=user, name=GUIDE_FOLDER_NAME
            ).values_list("folder_id", flat=True)
        )

    def test_new_membership_seeds_the_guide(self):
        resp = self._join(self.user, self.other_team)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        notes = self._guide_notes(self.user, self.other_team)
        self.assertEqual(notes.count(), len(GUIDE_NOTES))
        # Owner-role rows exist (the note APIs 403 without them).
        self.assertEqual(
            NotePermissionMaster.objects.filter(
                team=self.other_team,
                user=self.user,
                note_id__in=notes.values_list("note_id", flat=True),
            ).count(),
            len(GUIDE_NOTES),
        )
        member = TeamMembers.objects.get(team=self.other_team, attendee=self.user)
        self.assertIsNotNone(member.guide_seeded_at)

    def test_existing_member_picks_the_guide_up_on_next_switch(self):
        # The fixture already made self.user a member of self.team — the
        # every-team-switch join call is where existing users get theirs.
        resp = self._join(self.user, self.team)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._guide_notes(self.user, self.team).count(), len(GUIDE_NOTES))

    def test_repeat_joins_do_not_duplicate(self):
        self._join(self.user, self.team)
        self._join(self.user, self.team)
        self.assertEqual(self._guide_notes(self.user, self.team).count(), len(GUIDE_NOTES))
        self.assertEqual(
            PersonalNoteFolder.objects.filter(
                team=self.team, owner=self.user, name=GUIDE_FOLDER_NAME
            ).count(),
            1,
        )

    def test_deleted_guide_stays_deleted(self):
        # Personal-folder deletion is a HARD recursive delete; the
        # membership stamp is what stops the next team switch from
        # re-ambushing the user with a fresh copy.
        self._join(self.user, self.team)
        PersonalNoteFolder.objects.filter(
            team=self.team, owner=self.user, name=GUIDE_FOLDER_NAME
        ).delete()
        PersonalNoteMaster.objects.filter(team=self.team, owner=self.user).delete()
        self._join(self.user, self.team)
        self.assertEqual(self._guide_notes(self.user, self.team).count(), 0)
        # force=True is the deliberate re-seed lever.
        seed_guide_notes(self.user, self.team, force=True)
        self.assertEqual(self._guide_notes(self.user, self.team).count(), len(GUIDE_NOTES))

    def test_joining_someone_else_seeds_nothing(self):
        # Caller ≠ attendee: notes are personal, so nobody gets seeded.
        resp = self._join(self.user2, self.other_team, attendee=self.user)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._guide_notes(self.user, self.other_team).count(), 0)
        self.assertEqual(self._guide_notes(self.user2, self.other_team).count(), 0)

    def test_each_member_gets_their_own_copy(self):
        self._join(self.user, self.team)
        self._join(self.user2, self.team)
        self.assertEqual(self._guide_notes(self.user, self.team).count(), len(GUIDE_NOTES))
        self.assertEqual(self._guide_notes(self.user2, self.team).count(), len(GUIDE_NOTES))

    def test_demo_users_are_excluded(self):
        self.user.is_demo = True
        self.user.save(update_fields=["is_demo"])
        resp = self._join(self.user, self.other_team)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self._guide_notes(self.user, self.other_team).count(), 0)

    def test_guide_failure_never_breaks_a_join(self):
        with mock.patch(
            "origin.services.guide_notes.seed_guide_notes",
            side_effect=RuntimeError("boom"),
        ):
            resp = self._join(self.user, self.other_team)
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(
            TeamMembers.objects.filter(team=self.other_team, attendee=self.user).exists()
        )
