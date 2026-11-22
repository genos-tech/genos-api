"""A team with no live member left has ALL its data deleted (MIP-1958).

Real commits are needed (the team FKs are DEFERRABLE INITIALLY DEFERRED, checked only at COMMIT),
hence a TransactionTestCase. The workspace is the product's own demo seed (tasks, notes, chats,
projects, files) flipped to a real team, plus one hand-made row per kind the seed does not make.
"""

import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from origin.models.chat.unified_models import Channel, Message, MessageAttachment
from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamConnection, TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.models import AgentRun, AgentSession, NoteSummary, RagChunk
from origin.services import crypto, team_purge
from origin.services.account_deletion import delete_account
from origin.services.demo_seeder import create_demo_environment, delete_demo_environment
from origin.services.team_connection import normalize_team_pair
from origin.services.team_membership import remove_team_member
from origin.tests.transaction_support import CheapFlushTransactionTestCase

User = get_user_model()
FORGET = "origin.services.collab_push.forget_document"


class _Base(CheapFlushTransactionTestCase):
    def _team(self, name, *, closed=True):
        """A seeded workspace made a real team. `closed`: every membership ended and the team closed."""
        user = User.objects.create_user(
            username=name, email=f"{name}@example.com", password="x", is_demo=True
        )
        team = TeamMaster.objects.get(team_id=create_demo_environment(user)["team_id"])
        TeamMaster.objects.filter(pk=team.pk).update(is_demo=False)
        team.refresh_from_db()
        if closed:
            self._close(team)
        return user, team

    def _close(self, team):
        TeamMembers.objects.filter(team=team).update(is_deleted=True)
        TeamMaster.objects.filter(pk=team.pk).update(is_deleted=True)

    def _purge(self, team):
        with mock.patch(FORGET, return_value=True) as forget:
            outcome = team_purge.purge_team(team.team_id)
        return outcome, forget

    def _gone(self, team):
        return not TeamMaster.objects.filter(team_id=team.team_id).exists()


class EveryKindOfData(_Base):
    def test_seeded_content_is_deleted(self):
        user, team = self._team("tp_seed")
        tid = team.team_id
        self.assertTrue(TaskMaster.objects.filter(team=tid).exists())
        self.assertTrue(Message.objects.filter(channel__team=tid).exists())
        self.assertTrue(ProjectMaster.objects.filter(team=tid).exists())
        self.assertTrue(
            PersonalNoteMaster.objects.filter(team=tid).exists()
            or TaskNoteMaster.objects.filter(team=tid).exists()
            or ChatNoteMaster.objects.filter(team=tid).exists()
        )

        outcome, _ = self._purge(team)

        self.assertTrue(outcome.purged)
        self.assertTrue(self._gone(team))
        self.assertFalse(TaskMaster.objects.filter(team=tid).exists())
        self.assertFalse(Channel.objects.filter(team=tid).exists())
        self.assertFalse(Message.objects.filter(channel__team=tid).exists())
        self.assertFalse(ProjectMaster.objects.filter(team=tid).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(team=tid).exists())
        self.assertFalse(TaskNoteMaster.objects.filter(team=tid).exists())
        self.assertFalse(ChatNoteMaster.objects.filter(team=tid).exists())

    def test_set_null_rows_do_not_survive(self):
        """Mention groups, invites: `team` is SET_NULL, so `TeamMaster.delete()` leaves them behind."""
        user, team = self._team("tp_setnull")
        MentionGroupMaster.objects.create(team=team, group_name="design", created_by=user)
        TeamInvite.objects.create(
            team=team,
            invited_email="friend@example.com",
            invited_by=user,
            token_hash="0" * 64,
            expires_at=timezone.now() + timedelta(days=7),
        )
        self._purge(team)
        self.assertEqual(MentionGroupMaster.objects.filter(created_by=user).count(), 0)
        self.assertEqual(TeamInvite.objects.filter(invited_by=user).count(), 0)

    def test_stored_files_are_removed(self):
        user, team = self._team("tp_files")
        channel = Channel.objects.filter(team=team).first()
        message = Message.objects.filter(channel=channel).first()
        attachment = MessageAttachment.objects.create(
            message=message, uploader=user, file=ContentFile(b"x", name="a.txt")
        )
        emoji = TeamEmojiMaster.objects.create(
            team=team,
            name="party",
            image=SimpleUploadedFile("party.png", b"\x89PNG\r\n\x1a\n", content_type="image/png"),
            created_by=user,
        )
        team.profile_image_file = ContentFile(b"y", name="team.png")
        team.save(update_fields=["profile_image_file"])
        names = [attachment.file.name, emoji.image.name, team.profile_image_file.name]
        for n in names:
            self.assertTrue(default_storage.exists(n), n)

        outcome, _ = self._purge(team)

        self.assertGreaterEqual(outcome.files, 3)
        for n in names:
            self.assertFalse(default_storage.exists(n), n)

    def test_search_rows_and_summaries_are_deleted(self):
        user, team = self._team("tp_search")
        RagChunk.objects.create(
            chunk_id="tp-chunk-1",
            entity_type="task",
            entity_id="1",
            chunk_type="body",
            team_id=team.team_id,
            text_hash="h",
            embedding_model="m",
        )
        NoteSummary.objects.create(
            team_id=str(team.team_id), note_type=1, note_id=1, summary_text="s"
        )
        AgentSession.objects.create(team_id=str(team.team_id), user_id=str(user.id))
        self._purge(team)
        # RagChunk rows are kept when OpenSearch cannot be reached (so `sweep_orphans` can find the
        # leftovers): the deletion of the index documents is checked in the helper's own test.
        self.assertFalse(NoteSummary.objects.filter(team_id=str(team.team_id)).exists())
        self.assertFalse(AgentSession.objects.filter(team_id=str(team.team_id)).exists())
        self.assertFalse(AgentRun.objects.filter(team_id=str(team.team_id)).exists())

    def test_opensearch_chunks_are_deleted_through_the_existing_helper(self):
        user, team = self._team("tp_os")
        with (
            mock.patch("origin.services.demo_seeder._delete_demo_team_search_chunks") as sweep,
            mock.patch(FORGET, return_value=True),
        ):
            team_purge.purge_team(team.team_id)
        sweep.assert_called_once_with(team.team_id)

    def test_collab_documents_are_forgotten(self):
        user, team = self._team("tp_collab")
        task_id = TaskMaster.objects.filter(team=team).values_list("task_id", flat=True).first()
        _, forget = self._purge(team)
        names = {c.args[0] for c in forget.call_args_list}
        self.assertIn(f"task-body:{task_id}", names)

    def test_a_connection_to_another_team_does_not_block_and_is_removed(self):
        user, team = self._team("tp_conn")
        other_user, other = self._team("tp_conn_other", closed=False)
        lo, hi = normalize_team_pair(team.team_id, other.team_id)
        TeamConnection.objects.create(
            team_lo_id=lo, team_hi_id=hi, requested_by_team=team, status="accepted"
        )
        outcome, _ = self._purge(team)
        self.assertTrue(outcome.purged)
        self.assertFalse(TeamConnection.objects.exists())
        # the other team is untouched
        self.assertTrue(TeamMaster.objects.filter(team_id=other.team_id, is_deleted=False).exists())
        self.assertTrue(TaskMaster.objects.filter(team=other).exists())

    def test_failures_in_search_and_collab_still_finish_the_database_purge(self):
        user, team = self._team("tp_besteffort")
        with mock.patch(FORGET, side_effect=RuntimeError("collab down")):
            # a raising after-commit step must not undo the committed delete
            try:
                team_purge.purge_team(team.team_id)
            except RuntimeError:
                pass
        self.assertTrue(self._gone(team))


class WhoIsPurged(_Base):
    def test_a_team_with_one_live_member_left_is_not_purged(self):
        user, team = self._team("tp_live", closed=False)
        TeamMaster.objects.filter(pk=team.pk).update(is_deleted=True)  # closed by mistake
        outcome, _ = self._purge(team)
        self.assertFalse(outcome.purged)
        self.assertFalse(self._gone(team))
        self.assertTrue(TaskMaster.objects.filter(team=team).exists())

    def test_an_open_team_is_not_purged(self):
        user, team = self._team("tp_open", closed=False)
        TeamMembers.objects.filter(team=team).update(is_deleted=True)
        outcome, _ = self._purge(team)  # members gone but the team was never closed
        self.assertFalse(outcome.purged)
        self.assertFalse(self._gone(team))

    def test_a_demo_team_goes_through_its_own_teardown(self):
        user = User.objects.create_user(
            username="tp_demo", email="tp_demo@example.com", password="x", is_demo=True
        )
        team = TeamMaster.objects.get(team_id=create_demo_environment(user)["team_id"])
        self.assertTrue(team.is_demo)
        TeamMaster.objects.filter(pk=team.pk).update(is_deleted=True)
        TeamMembers.objects.filter(team=team).update(is_deleted=True)
        self.assertEqual(team_purge.pending_purge_team_ids(), [])
        self.assertFalse(team_purge.purge_team(team.team_id).purged)
        self.assertFalse(team_purge.close_team_if_empty(team.team_id))
        self.assertFalse(self._gone(team))
        delete_demo_environment(user)
        self.assertTrue(self._gone(team))

    def test_a_paid_stripe_team_is_held(self):
        user, team = self._team("tp_paid")
        TeamMaster.objects.filter(pk=team.pk).update(
            plan="pro", plan_set_by="stripe", stripe_customer_id="cus_x"
        )
        outcome, _ = self._purge(team)
        self.assertFalse(outcome.purged)
        self.assertEqual(outcome.held, "billing")
        self.assertFalse(self._gone(team))
        # held teams stay out of the batch, so they cannot starve the others
        self.assertEqual(team_purge.pending_purge_team_ids(), [])
        self.assertEqual(team_purge.held_team_count(), 1)

    def test_a_person_cut_off_by_the_operator_still_keeps_the_team_alive(self):
        """`operator_cutoff` clears is_active reversibly: the purge cannot be undone."""
        user, team = self._team("tp_cutoff", closed=False)
        TeamMembers.objects.filter(team=team).exclude(attendee=user).update(is_deleted=True)
        User.objects.filter(pk=user.pk).update(is_active=False, is_demo=False)
        self.assertTrue(team_purge.has_live_member(team.team_id))
        self.assertFalse(team_purge.close_team_if_empty(team.team_id))

    def test_an_mcp_connection_of_a_live_person_moves_to_their_other_team(self):
        from oauth2_provider.models import get_application_model

        from origin.models.common.mcp_connection_models import McpConnection

        user, team = self._team("tp_mcp")
        _, other = self._team("tp_mcp_other", closed=False)
        member = TeamMembers.objects.filter(team=other, is_deleted=False).first().attendee
        app = get_application_model().objects.create(
            name="c",
            client_type="public",
            authorization_grant_type="authorization-code",
            redirect_uris="https://example.com/cb",
        )
        conn = McpConnection.objects.create(
            user=member,
            application=app,
            default_team=team,
            team_ids=[str(team.team_id), str(other.team_id)],
        )
        self._purge(team)
        conn.refresh_from_db()
        self.assertEqual(conn.default_team_id, other.team_id)
        self.assertEqual(conn.team_ids, [str(other.team_id)])

    def test_a_member_who_rejoined_before_the_purge_blocks_it(self):
        user, team = self._team("tp_rejoin")
        TeamMembers.objects.filter(team=team).update(is_deleted=False)
        self.assertEqual(team_purge.pending_purge_team_ids(), [])
        self.assertFalse(self._purge(team)[0].purged)
        self.assertFalse(self._gone(team))

    def test_another_team_is_left_alone(self):
        _, gone = self._team("tp_gone")
        _, kept = self._team("tp_kept", closed=False)
        self._purge(gone)
        self.assertTrue(self._gone(gone))
        self.assertTrue(TaskMaster.objects.filter(team=kept).exists())
        self.assertTrue(Message.objects.filter(channel__team=kept).exists())

    def test_purging_twice_is_a_no_op(self):
        _, team = self._team("tp_twice")
        self.assertTrue(self._purge(team)[0].purged)
        self.assertFalse(self._purge(team)[0].purged)


class TheTriggers(_Base):
    def test_the_last_live_member_deleting_their_account_closes_the_team(self):
        user, team = self._team("tp_trig_acct", closed=False)
        # leave exactly one live member: the user. The seed's bot peers go.
        TeamMembers.objects.filter(team=team).exclude(attendee=user).update(is_deleted=True)
        TeamMaster.objects.filter(pk=team.pk).update(owner=None)  # a plain member, owner gone
        user.is_demo = False
        user.save()
        delete_account(user)
        team.refresh_from_db()
        self.assertTrue(team.is_deleted)
        self.assertEqual(team_purge.pending_purge_team_ids(), [team.team_id])

    def test_a_team_with_another_live_member_stays_open(self):
        user, team = self._team("tp_trig_open", closed=False)
        other = User.objects.create_user(username="tp_other", email="tp_other@example.com")
        TeamMembers.objects.create(team=team, attendee=other)
        TeamMembers.objects.filter(team=team).exclude(attendee__in=[user, other]).update(
            is_deleted=True
        )
        remove_team_member(team.team_id, user.id)
        team.refresh_from_db()
        self.assertFalse(team.is_deleted)

    def test_removing_the_last_live_member_closes_the_team(self):
        user, team = self._team("tp_trig_rm", closed=False)
        TeamMembers.objects.filter(team=team).exclude(attendee=user).update(is_deleted=True)
        remove_team_member(team.team_id, user.id)
        team.refresh_from_db()
        self.assertTrue(team.is_deleted)


class TheCommand(_Base):
    def _run(self, *args):
        out = io.StringIO()
        with mock.patch(FORGET, return_value=True):
            call_command("purge_closed_teams", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_deletes_nothing_and_prints_numbers_only(self):
        user, team = self._team("tp_cmd_dry")
        text = self._run()
        self.assertIn("DRY RUN", text)
        self.assertIn("1 closed team(s)", text)
        self.assertFalse(self._gone(team))
        for secret in (str(team.team_id), team.team_name, user.email, "tp_cmd_dry"):
            self.assertNotIn(str(secret), text)

    def test_a_failing_team_does_not_stop_the_others_and_fails_the_run(self):
        _, bad = self._team("tp_cmd_bad")
        _, good = self._team("tp_cmd_good")
        real = team_purge.purge_team

        def flaky(team_id):
            if team_id == bad.team_id:
                raise RuntimeError("boom")
            return real(team_id)

        out = io.StringIO()
        with (
            mock.patch(FORGET, return_value=True),
            mock.patch.object(team_purge, "purge_team", flaky),
        ):
            with self.assertRaises(CommandError):  # CronCommand fails the run on an ERROR log
                call_command("purge_closed_teams", "--apply", "--yes", stdout=out)
        self.assertIn("1 team(s) failed", out.getvalue())
        self.assertTrue(self._gone(good))
        self.assertFalse(self._gone(bad))

    def test_apply_needs_yes(self):
        self._team("tp_cmd_yes")
        with self.assertRaises(CommandError):
            call_command("purge_closed_teams", "--apply", stdout=io.StringIO())

    def test_apply_purges_and_a_second_run_reports_nothing(self):
        _, team = self._team("tp_cmd_apply")
        self.assertIn("1 team(s) purged", self._run("--apply", "--yes"))
        self.assertTrue(self._gone(team))
        self.assertIn("0 closed team(s)", self._run("--apply", "--yes"))


class StoredOAuthTokens(_Base):
    """Genos must not keep a Google or GitHub token once the connection or the account is gone."""

    def _connect(self, user):
        for provider, token in (("google", "g-secret"), ("github", "gh-secret")):
            ConnectedAccount.objects.create(
                user=user,
                provider=provider,
                provider_user_id=f"{provider}-sub",
                access_token_encrypted=crypto.encrypt(token),
                refresh_token_encrypted=crypto.encrypt(token + "-r"),
            )

    def test_account_deletion_leaves_no_token_row(self):
        user = User.objects.create_user(username="tp_tok", email="tp_tok@example.com")
        self._connect(user)
        delete_account(user)
        self.assertFalse(ConnectedAccount.objects.filter(user_id=user.id).exists())
        self.assertEqual(ConnectedAccount.objects.count(), 0)

    def test_disconnecting_deletes_the_token_row(self):
        from rest_framework.test import APIClient

        user = User.objects.create_user(username="tp_tok2", email="tp_tok2@example.com")
        self._connect(user)
        client = APIClient()
        client.force_authenticate(user=user)
        for acc in list(ConnectedAccount.objects.filter(user=user)):
            resp = client.delete(f"/api/v2/integrations/account/{acc.id}/")
            self.assertEqual(resp.status_code, 204, resp.content)
        self.assertEqual(ConnectedAccount.objects.count(), 0)


class EveryFileFieldIsCovered(_Base):
    """A new model with a FileField must be added to the purge (or here, with the reason)."""

    COVERED = {
        # `team_purge._stored_files`
        "TeamMaster.profile_image_file",
        "ProjectMaster.profile_image_url",
        "Channel.profile_image_file",
        "MessageAttachment.file",
        "TeamEmojiMaster.image",
        # removed by `delete_demo_team_data` (task_files, note_delete.forget_note_attachments)
        "TaskAttachments.attached_file",
        "TaskBodyAttachmentFact.body_attachment_url",
        "ChatNoteAttachmentFact.note_attachment_url",
        "PersonalNoteAttachmentFact.note_attachment_url",
        "TaskNoteAttachmentFact.note_attachment_url",
        # a person's own avatar: removed by delete_account, not part of a team
        "CustomUser.profile_image_url",
    }

    def test_no_file_field_is_forgotten(self):
        from django.apps import apps
        from django.db import models

        found = {
            f"{m.__name__}.{f.name}"
            for m in apps.get_app_config("origin").get_models()
            for f in m._meta.get_fields()
            if isinstance(f, models.FileField)
        }
        self.assertEqual(found - self.COVERED, set(), "a FileField the team purge does not know")
