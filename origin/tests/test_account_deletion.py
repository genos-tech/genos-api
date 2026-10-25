"""Account deletion (GDPR/CCPA erasure).

The properties that matter: the PERSON becomes unidentifiable and every
session dies; the WORKSPACE stays consistent (no orphaned rows, no
ghost memberships, teammates' content keeps its attribution); a team
that still has other members cannot be left ownerless; the AI/search
rows keyed by a bare `user_id` string are swept (nothing cascades to
them — see `AgentHistoryErasureTests`); and the endpoint cannot be fired
by accident or by a leaked token acting on someone else.
"""

import secrets
import shutil
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.core.files.base import ContentFile
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup
from origin.models.common.invite_models import TeamInvite
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.models import (
    AgentRun,
    AgentRunFeedback,
    AgentSession,
    AgentStep,
)
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
        resp = self.client.delete(URL, {"confirm": "DELETE", "password": "nope"}, format="json")
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
        self.assertFalse(TeamMembers.objects.filter(attendee=self.user2, is_deleted=False).exists())
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


class AgentHistoryErasureTests(BaseAPITestCase):
    """The agent tables hold the user's own words, so erasure must reach them.

    `AgentRun.query` is the question as typed and `final_answer_text` the
    answer built from it. Neither table has an FK to `CustomUser` (both
    key the owner with a bare `CharField`), so nothing about anonymizing
    the user row cascades here — only the explicit sweep in
    `_purge_search_engine_rows` removes them.
    """

    def _seed_agent_history(self, user, *, query):
        team_id, user_id = str(self.team.team_id), str(user.id)
        session = AgentSession.objects.create(team_id=team_id, user_id=user_id)
        run = AgentRun.objects.create(
            team_id=team_id,
            user_id=user_id,
            query=query,
            status="done",
            final_answer_text="because of the perf budget",
            session=session,
        )
        step = AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name="search_knowledge_base",
            result_json={"matches": []},
        )
        return session, run, step

    def test_agent_history_is_purged(self):
        session, run, step = self._seed_agent_history(self.user2, query="MY PRIVATE QUESTION")

        delete_account(self.user2)

        self.assertFalse(AgentRun.objects.filter(pk=run.pk).exists())
        self.assertFalse(AgentSession.objects.filter(pk=session.pk).exists())
        # AgentStep cascades from AgentRun, so only the run sweep reaches it.
        self.assertFalse(AgentStep.objects.filter(pk=step.pk).exists())

    def test_runs_are_deleted_not_orphaned(self):
        """Guards against relying on a session cascade that does not exist.

        `AgentRun.session` is SET_NULL, so if the explicit `AgentRun`
        sweep is dropped on the theory that deleting the session takes
        the runs with it, the run survives with `session_id = NULL` —
        still carrying the user's question. The NULL-session assertion is
        the specific one: it fails on a merely-detached run, which a bare
        "does it still exist" check on a different filter could miss.
        """
        self._seed_agent_history(self.user2, query="MY PRIVATE QUESTION")
        user_id = str(self.user2.id)

        delete_account(self.user2)

        self.assertEqual(AgentRun.objects.filter(user_id=user_id).count(), 0)
        self.assertEqual(
            AgentRun.objects.filter(user_id=user_id, session__isnull=True).count(), 0
        )
        # And the raw question text is gone from the table entirely.
        self.assertFalse(AgentRun.objects.filter(query="MY PRIVATE QUESTION").exists())

    def test_another_users_agent_history_is_untouched(self):
        """Erasure is scoped to the person, not to their team.

        `self.user` is a teammate in the same team, so a sweep keyed on
        `team_id` instead of `user_id` would take their history too.
        """
        mine, my_run, _ = self._seed_agent_history(self.user2, query="mine")
        theirs, their_run, their_step = self._seed_agent_history(self.user, query="theirs")

        delete_account(self.user2)

        self.assertFalse(AgentRun.objects.filter(pk=my_run.pk).exists())
        self.assertTrue(AgentRun.objects.filter(pk=their_run.pk).exists())
        self.assertTrue(AgentSession.objects.filter(pk=theirs.pk).exists())
        self.assertTrue(AgentStep.objects.filter(pk=their_step.pk).exists())
        self.assertIsNotNone(mine)

    def test_feedback_left_on_someone_elses_run_is_erased(self):
        """`AgentRunFeedback.user_id` is the RATER, not the run's owner.

        One row per (run, user) by unique constraint, so the deleted
        person's 👎 on a teammate's answer is their own content and has to
        go — while the teammate's run stays. This is why the feedback
        entry in the sweep is not made redundant by its CASCADE from
        `AgentRun`.
        """
        _theirs, their_run, _ = self._seed_agent_history(self.user, query="theirs")
        feedback = AgentRunFeedback.objects.create(
            run=their_run,
            team_id=str(self.team.team_id),
            user_id=str(self.user2.id),
            rating=AgentRunFeedback.RATING_DOWN,
            comment="not useful",
        )

        delete_account(self.user2)

        self.assertFalse(AgentRunFeedback.objects.filter(pk=feedback.pk).exists())
        self.assertTrue(AgentRun.objects.filter(pk=their_run.pk).exists())


class AvatarAndInviteErasureTests(BaseAPITestCase):
    """The photo on disk and the invitations addressed to the person (MIP-810).

    Clearing the fields on the user row forgot the avatar's path but left the
    file, and an invite keeps the address it was sent to after the account row
    has forgotten it.
    """

    def setUp(self):
        super().setUp()
        self._media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self._media, ignore_errors=True)
        override = override_settings(MEDIA_ROOT=self._media)
        override.enable()
        self.addCleanup(override.disable)

    def _give_avatar(self, user):
        user.profile_image_url.save("me.png", ContentFile(b"png-bytes"), save=True)
        return Path(self._media) / user.profile_image_url.name

    def _invite(self, email, **extra):
        return TeamInvite.objects.create(
            team=self.team,
            invited_email=email,
            token_hash=secrets.token_hex(32),
            expires_at=timezone.now() + timedelta(days=1),
            **extra,
        )

    # -- the avatar ----------------------------------------------------

    def test_the_avatar_file_is_removed_from_storage(self):
        on_disk = self._give_avatar(self.user2)
        self.assertTrue(on_disk.exists())

        delete_account(self.user2)

        self.assertFalse(on_disk.exists())

    def test_a_user_with_no_avatar_is_deleted_normally(self):
        result = delete_account(self.user2)

        self.assertEqual(result["user_id"], str(self.user2.id))
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)

    def test_a_file_already_gone_does_not_block_the_erasure(self):
        on_disk = self._give_avatar(self.user2)
        on_disk.unlink()

        delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)

    def test_a_storage_error_does_not_block_the_erasure(self):
        self._give_avatar(self.user2)

        with mock.patch(
            "origin.services.account_deletion.default_storage.delete",
            side_effect=OSError("volume unavailable"),
        ):
            delete_account(self.user2)

        self.user2.refresh_from_db()
        self.assertTrue(self.user2.is_deleted)
        self.assertFalse(self.user2.is_active)

    def test_the_file_is_kept_when_the_erasure_itself_fails(self):
        """A file delete cannot be rolled back, so it must not run before the
        transaction has committed."""
        on_disk = self._give_avatar(self.user2)

        # Fail at the LAST step inside the transaction (the row save), after every
        # other step has run, so a file removed anywhere before the commit
        # would already be gone.
        with mock.patch.object(type(self.user2), "save", side_effect=RuntimeError("database down")):
            with self.assertRaises(RuntimeError):
                delete_account(self.user2)

        self.assertTrue(on_disk.exists())
        self.user2.refresh_from_db()
        self.assertFalse(self.user2.is_deleted)

    def test_another_users_avatar_is_untouched(self):
        mine = self._give_avatar(self.user2)
        theirs = self._give_avatar(self.user)

        delete_account(self.user2)

        self.assertFalse(mine.exists())
        self.assertTrue(theirs.exists())

    # -- the invites ---------------------------------------------------

    def test_invites_addressed_to_the_person_are_removed_in_every_status(self):
        address = self.user2.email
        pending = self._invite(address)
        accepted = self._invite(address, status="accepted")
        revoked = self._invite(address, status="revoked")

        result = delete_account(self.user2)

        for invite in (pending, accepted, revoked):
            self.assertFalse(TeamInvite.objects.filter(pk=invite.pk).exists())
        self.assertEqual(result["invites_removed"], 3)

    def test_the_address_is_matched_without_regard_to_case(self):
        invite = self._invite(self.user2.email.upper())

        delete_account(self.user2)

        self.assertFalse(TeamInvite.objects.filter(pk=invite.pk).exists())

    def test_invites_to_other_people_are_kept(self):
        other = self._invite("someone.else@example.com")
        mine = self._invite(self.user2.email)

        delete_account(self.user2)

        self.assertTrue(TeamInvite.objects.filter(pk=other.pk).exists())
        self.assertFalse(TeamInvite.objects.filter(pk=mine.pk).exists())

    def test_invites_the_person_sent_are_kept(self):
        """Team history: the row stays, attributed to the anonymised user like
        the rest of their past work (the account row is anonymised, not
        deleted, so nothing nulls the link)."""
        sent = self._invite("newcomer@example.com", invited_by=self.user2)

        delete_account(self.user2)

        sent.refresh_from_db()
        self.assertEqual(sent.invited_by_id, self.user2.id)
        self.assertEqual(sent.invited_by.username, DELETED_USERNAME)

    def test_no_invites_is_zero_removed(self):
        self.assertEqual(delete_account(self.user2)["invites_removed"], 0)
