"""Account deletion (GDPR/CCPA erasure).

The properties that matter: the PERSON becomes unidentifiable and every
session dies; the WORKSPACE stays consistent (no orphaned rows, no
ghost memberships, teammates' content keeps its attribution); a team
that still has other members cannot be left ownerless; and the endpoint
cannot be fired by accident or by a leaked token acting on someone else.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.services.account_deletion import (
    DELETED_USERNAME,
    OwnershipTransferRequired,
    blocking_owned_teams,
    delete_account,
)
from origin.tests.test_base import BaseAPITestCase

URL = "/api/v2/user/account/"


class DeletionGuardTests(BaseAPITestCase):
    def test_unauthenticated_is_rejected(self):
        self.assertEqual(self.client.delete(URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_confirm_string_is_required(self):
        self.authenticate()
        resp = self.client.delete(URL, {"confirm": "yes"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_wrong_password_is_refused(self):
        self.authenticate()
        resp = self.client.delete(
            URL, {"confirm": "DELETE", "password": "nope"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_oauth_only_account_needs_no_password(self):
        # `set_unusable_password` at signup means requiring one would
        # lock OAuth users out of erasure entirely.
        self.user2.set_unusable_password()
        self.user2.save(update_fields=["password"])
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=True)
        self.authenticate(self.user2)
        resp = self.client.delete(URL, {"confirm": "DELETE"}, format="json")
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)

    def test_status_endpoint_reports_the_blocker(self):
        self.authenticate()
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # self.user owns self.team, which still has user2 in it.
        self.assertFalse(resp.data["can_delete"])
        self.assertTrue(resp.data["requires_password"])
        self.assertEqual(len(resp.data["blocking_teams"]), 1)


class OwnershipBlockerTests(BaseAPITestCase):
    def test_owner_of_a_shared_team_is_blocked(self):
        self.assertEqual([t.team_id for t in blocking_owned_teams(self.user)], [self.team.team_id])
        with self.assertRaises(OwnershipTransferRequired):
            delete_account(self.user)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_endpoint_returns_409_with_the_team_list(self):
        self.authenticate()
        resp = self.client.delete(
            URL, {"confirm": "DELETE", "password": "testpass123"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(resp.data["blocking_teams"][0]["teamName"], self.team.team_name)

    def test_sole_member_team_is_not_blocking_and_is_closed(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(is_deleted=True)
        self.assertEqual(blocking_owned_teams(self.user), [])
        delete_account(self.user)
        self.team.refresh_from_db()
        self.assertTrue(self.team.is_deleted)

    def test_non_owner_member_deletes_freely(self):
        self.assertEqual(blocking_owned_teams(self.user2), [])
        delete_account(self.user2)
        self.user2.refresh_from_db()
        self.assertFalse(self.user2.is_active)
        # The team they were in is untouched.
        self.team.refresh_from_db()
        self.assertFalse(self.team.is_deleted)


class ErasureTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # user2 is a plain member — the deletable case.
        NotificationPreference.objects.create(user=self.user2)
        cat = ToDoCategory.objects.create(team=self.team, user=self.user2, name="c")
        ToDoGroup.objects.create(team=self.team, user=self.user2, local_date="2026-08-01")
        self.assertIsNotNone(cat.pk)
        PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="private", body=[]
        )

    def test_identity_is_erased_and_sessions_die(self):
        old_email = self.user2.email
        delete_account(self.user2)
        self.user2.refresh_from_db()
        self.assertNotEqual(self.user2.email, old_email)
        self.assertTrue(self.user2.email.endswith("@deleted.invalid"))
        self.assertEqual(self.user2.username, DELETED_USERNAME)
        self.assertIsNone(self.user2.phone_number)
        self.assertFalse(self.user2.has_usable_password())
        self.assertTrue(self.user2.is_deleted)
        # is_active=False is the session kill-switch: SimpleJWT re-reads
        # this row on every request.
        self.assertFalse(self.user2.is_active)

    def test_personal_data_is_deleted(self):
        delete_account(self.user2)
        self.assertFalse(NotificationPreference.objects.filter(user=self.user2).exists())
        self.assertFalse(ToDoGroup.objects.filter(user=self.user2).exists())
        self.assertFalse(ToDoCategory.objects.filter(user=self.user2).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(owner=self.user2).exists())

    def test_memberships_end_but_the_row_survives(self):
        delete_account(self.user2)
        # Soft — so no ghost membership row and no orphaned FKs.
        self.assertFalse(
            TeamMembers.objects.filter(attendee=self.user2, is_deleted=False).exists()
        )
        self.assertTrue(TeamMembers.objects.filter(attendee=self.user2).exists())
        self.assertTrue(CustomUser.objects.filter(id=self.user2.id).exists())

    def test_team_content_keeps_its_attribution(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="P",
            owner=self.user,
            project_system_user=self.user,
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title="theirs",
            status="Open",
            assignee=self.user2,
            reporter=self.user2,
        )
        delete_account(self.user2)
        task.refresh_from_db()
        # A hard delete would SET_NULL both of these, silently orphaning
        # the teammates' board.
        self.assertEqual(str(task.assignee_id), str(self.user2.id))
        self.assertEqual(str(task.reporter_id), str(self.user2.id))
        task.refresh_from_db()
        self.assertEqual(task.assignee.username, DELETED_USERNAME)

    def test_deleted_user_cannot_use_an_existing_token(self):
        self.authenticate(self.user2)
        delete_account(self.user2)
        # Same client, same token — now rejected because is_active is
        # re-checked from the DB on every request.
        resp = self.client.get(reverse("language_preference"))
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
