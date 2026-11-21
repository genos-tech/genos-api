"""What account deletion used to leave behind (MIP-1783).

Accounts are SOFT-deleted, so no foreign key to the user ever cascades: every row of the person
that `delete_account` does not list by hand stays, pointing at an anonymised account. The rows
tested here have no purpose once the person is gone, and several hold text (the email outbox
holds message previews, a reminder holds a preview and the sender's name).

Per table: deleted (the person's own bookmarks, reminders, reactions, notices, quota counters),
or anonymised (a bare "who generated this" id on a summary the team still uses), or kept on
purpose (other people's content, listed in the PR). Every query is keyed on the one user:
another member's rows, and the rows ABOUT the person in other members' data, are never touched.
"""

import json
import uuid
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.utils import timezone

from origin.models.chat.todo_models import ToDoSchedule
from origin.models.chat.unified_models import (
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageMention,
    MessageReaction,
    MessageReminder,
    Pin,
)
from origin.models.common.feature_models import UserFeatureAccess
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.usage_models import ModelUsageCounter
from origin.models.note.favorite_note_models import NoteFolderFavorite
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskMaster,
)
from origin.models.task.task_recent_models import TaskRecent
from origin.search_engine.models import NoteSummary, ThreadSummary
from origin.services import deleted_account_leftovers, message_reminders
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class LeftoversFixture(BaseAPITestCase):
    """`user2` is the leaver; `user` (team owner) and `mate` stay."""

    def setUp(self):
        super().setUp()
        # The search index is not up in unit tests: the prune's index reads see an empty index
        # (MIP-1821). The derived-name tests mock it themselves.
        _index = patch("origin.search_engine.opensearch_client.get_client")
        _client = _index.start()
        self.addCleanup(_index.stop)
        _client.return_value.count.return_value = {"count": 0}
        _client.return_value.search.return_value = {}
        self.leaver = self.user2
        self.mate = self.user
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.mate
        )
        for u in (self.mate, self.leaver):
            ChannelMember.objects.create(channel=self.channel, user=u, role="member")
        self.mates_msg = self._message(self.mate, 1, "mate says hello")
        self.leavers_msg = self._message(self.leaver, 2, "leaver says hello")

    def _message(self, sender, seq, text):
        return Message.objects.create(
            channel=self.channel, sender=sender, seq=seq, body={"text": text}, body_text=text
        )


class OwnRowsAreDeletedTests(LeftoversFixture):
    def _outbox(self, user, title="New message"):
        return EmailNotificationEvent.objects.create(
            user=user, category="mention", title=title, body="a message preview", actor_name="Ann"
        )

    def test_the_email_outbox_rows_of_the_person_are_deleted(self):
        mine = self._outbox(self.leaver)
        theirs = self._outbox(self.mate)
        delete_account(self.leaver)
        self.assertFalse(EmailNotificationEvent.objects.filter(pk=mine.pk).exists())
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=theirs.pk).exists())

    def test_every_inbox_item_addressed_to_the_person_is_deleted(self):
        for item_type in (0, 1, 4, 6, 9, 10):
            InboxItems.objects.create(
                team=self.team,
                sender=self.mate,
                receiver=self.leaver,
                item_type=item_type,
                item_body={"title": "t", "text": "a preview"},
            )
        kept = InboxItems.objects.create(
            team=self.team, sender=self.leaver, receiver=self.mate, item_type=0, item_body={}
        )
        delete_account(self.leaver)
        self.assertFalse(InboxItems.objects.filter(receiver=self.leaver).exists())
        self.assertTrue(InboxItems.objects.filter(pk=kept.pk).exists())

    def test_a_request_the_person_sent_and_nobody_answered_is_deleted(self):
        pending = InboxItems.objects.create(
            team=self.team,
            sender=self.leaver,
            receiver=self.mate,
            item_type=2,
            item_body=[],
            request_status="pending",
        )
        answered = InboxItems.objects.create(
            team=self.team,
            sender=self.leaver,
            receiver=self.mate,
            item_type=2,
            item_body=[],
            request_status="approved",
        )
        delete_account(self.leaver)
        self.assertFalse(InboxItems.objects.filter(pk=pending.pk).exists())
        # An answered request is the receiver's history; the sender reads "Deleted user".
        self.assertTrue(InboxItems.objects.filter(pk=answered.pk).exists())

    def test_bookmarks_reminders_reactions_and_mentions_are_deleted(self):
        Flag.objects.create(user=self.leaver, message=self.mates_msg)
        Pin.objects.create(user=self.leaver, channel=self.channel)
        MessageReminder.objects.create(
            user=self.leaver, message=self.mates_msg, remind_at=timezone.now() + timedelta(hours=1)
        )
        MessageReaction.objects.create(user=self.leaver, message=self.mates_msg, emoji="+1")
        MessageMention.objects.create(message=self.mates_msg, mentioned_user=self.leaver)
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P2",
            code="P2",
            owner=self.mate,
            project_system_user=self.mate,
        )
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        TaskCommentReactionFact.objects.create(
            team=self.team,
            task=task,
            comment_id=1,
            reaction_id=1,
            reaction_emoji="+1",
            sender=self.leaver,
        )
        TaskCommentReactionFact.objects.create(
            team=self.team,
            task=task,
            comment_id=1,
            reaction_id=2,
            reaction_emoji="+1",
            sender=self.mate,
        )
        TaskCommentMentionFact.objects.create(
            team=self.team, task=task, comment_id=1, mentioned_user=self.leaver
        )
        # What the other member holds on the same message is not touched.
        Flag.objects.create(user=self.mate, message=self.mates_msg)
        MessageReaction.objects.create(user=self.mate, message=self.mates_msg, emoji="+1")

        delete_account(self.leaver)

        for model, field in (
            (Flag, "user"),
            (Pin, "user"),
            (MessageReminder, "user"),
            (MessageReaction, "user"),
            (MessageMention, "mentioned_user"),
        ):
            self.assertFalse(model.objects.filter(**{field: self.leaver}).exists(), model.__name__)
        self.assertFalse(TaskCommentReactionFact.objects.filter(sender=self.leaver).exists())
        self.assertFalse(TaskCommentMentionFact.objects.filter(mentioned_user=self.leaver).exists())
        self.assertEqual(TaskCommentReactionFact.objects.filter(sender=self.mate).count(), 1)
        self.assertTrue(Flag.objects.filter(user=self.mate).exists())
        self.assertTrue(MessageReaction.objects.filter(user=self.mate).exists())
        # The message itself, and the leaver's own message, stay.
        self.assertTrue(Message.objects.filter(pk=self.leavers_msg.pk).exists())

    def test_recents_schedules_favorites_counters_and_grants_are_deleted(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="P",
            owner=self.mate,
            project_system_user=self.mate,
        )
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        TaskRecent.objects.create(user=self.leaver, task=task, team=self.team)
        ToDoSchedule.objects.create(
            team=self.team,
            user=self.leaver,
            title="Call the clinic",
            rrule="RRULE:FREQ=WEEKLY",
            start_date="2026-11-01",
        )
        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.mate, name="F", scope="team", visibility="public"
        )
        NoteFolderFavorite.objects.create(team=self.team, user=self.leaver, folder=folder)
        ModelUsageCounter.objects.create(
            user=self.leaver, model_name="__llm_ask__", usage_date="2026-11-20", count=3
        )
        UserFeatureAccess.objects.create(
            user=self.leaver, feature=UserFeatureAccess.FEATURE_WEB_SEARCH, note="trial for Ann"
        )
        TaskRecent.objects.create(user=self.mate, task=task, team=self.team)

        delete_account(self.leaver)

        for model in (
            TaskRecent,
            ToDoSchedule,
            NoteFolderFavorite,
            ModelUsageCounter,
            UserFeatureAccess,
        ):
            self.assertFalse(model.objects.filter(user=self.leaver).exists(), model.__name__)
        self.assertTrue(TaskRecent.objects.filter(user=self.mate).exists())
        # The folder itself is the team's.
        self.assertTrue(PersonalNoteFolder.objects.filter(pk=folder.pk).exists())


class AnonymisedRowsTests(LeftoversFixture):
    def test_summaries_stay_but_forget_who_asked_for_them(self):
        thread = ThreadSummary.objects.create(
            team_id=str(self.team.team_id),
            chat_type=2,
            chat_id=self.channel.id,
            thread_id=uuid.uuid4(),
            summary_text="the team agreed",
            generated_by_user_id=str(self.leaver.id),
        )
        note = NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=1,
            note_id=7,
            summary_text="a note",
            generated_by_user_id=str(self.leaver.id),
        )
        other = NoteSummary.objects.create(
            team_id=str(self.team.team_id),
            note_type=1,
            note_id=8,
            summary_text="b",
            generated_by_user_id=str(self.mate.id),
        )
        delete_account(self.leaver)
        for row in (thread, note, other):
            row.refresh_from_db()
        self.assertEqual(thread.generated_by_user_id, "")
        self.assertEqual(thread.summary_text, "the team agreed")
        self.assertEqual(note.generated_by_user_id, "")
        self.assertEqual(other.generated_by_user_id, str(self.mate.id))

    def test_a_reminder_card_about_the_persons_message_stops_naming_them(self):
        card = InboxItems.objects.create(
            team=self.team,
            sender=self.leaver,
            receiver=self.mate,
            item_type=message_reminders.ITEM_TYPE_MESSAGE_REMINDER,
            item_body={"title": "Reminder about otheruser's message", "text": "leaver says hello"},
            item_optionals={"sender_name": "otheruser", "preview": "leaver says hello"},
            request_status="",
        )
        delete_account(self.leaver)
        card.refresh_from_db()
        self.assertEqual(card.item_optionals["sender_name"], DELETED_USERNAME)
        self.assertNotIn("otheruser", json.dumps([card.item_body, card.item_optionals]))
        # The message preview is the other member's reminder about a message that stays.
        self.assertEqual(card.item_optionals["preview"], "leaver says hello")


class TicksSkipDeletedAccountsTests(LeftoversFixture):
    def test_a_due_reminder_of_a_deleted_account_is_retired_not_delivered(self):
        reminder = MessageReminder.objects.create(
            user=self.leaver, message=self.mates_msg, remind_at=timezone.now() - timedelta(hours=1)
        )
        Flag.objects.create(user=self.leaver, message=self.mates_msg)
        # A leftover from before the rule: the account is gone but the row is not.
        CustomUserModel = type(self.leaver)
        CustomUserModel.objects.filter(pk=self.leaver.pk).update(is_deleted=True, is_active=False)
        with patch("origin.services.message_reminders.schedule_push_for_reminder") as push:
            result = message_reminders.fire_due()
        push.assert_not_called()
        self.assertEqual(result["fired"], 0)
        self.assertFalse(InboxItems.objects.filter(receiver=self.leaver).exists())
        reminder.refresh_from_db()
        self.assertIsNotNone(reminder.cancelled_at)

    def test_the_email_tick_does_not_mail_a_deleted_account(self):
        EmailNotificationEvent.objects.create(
            user=self.leaver, category="mention", title="x", body="y"
        )
        type(self.leaver).objects.filter(pk=self.leaver.pk).update(
            is_deleted=True, email="deleted-x@deleted.invalid"
        )
        with patch("origin.management.commands.email_notify_tick.send_notification_batch") as send:
            call_command("email_notify_tick", stdout=StringIO())
        send.assert_not_called()


class PruneCommandTests(LeftoversFixture):
    """Accounts deleted BEFORE this rule left the rows; a dry run counts, `--apply` deletes."""

    def _leave_rows_behind(self):
        # What an old deletion left: the account is soft-deleted, the rows are still there.
        Flag.objects.create(user=self.leaver, message=self.mates_msg)
        EmailNotificationEvent.objects.create(
            user=self.leaver, category="mention", title="x", body="y"
        )
        InboxItems.objects.create(
            team=self.team, sender=self.mate, receiver=self.leaver, item_type=0, item_body={}
        )
        type(self.leaver).objects.filter(pk=self.leaver.pk).update(is_deleted=True)
        # A live member's rows are never selected.
        Flag.objects.create(user=self.mate, message=self.leavers_msg)

    def test_dry_run_counts_and_writes_nothing(self):
        self._leave_rows_behind()
        out = StringIO()
        call_command("prune_deleted_account_leftovers", stdout=out)
        self.assertIn("DRY RUN", out.getvalue())
        self.assertIn("would delete", out.getvalue())
        self.assertTrue(Flag.objects.filter(user=self.leaver).exists())

    def test_apply_deletes_only_rows_of_deleted_accounts(self):
        self._leave_rows_behind()
        out = StringIO()
        call_command("prune_deleted_account_leftovers", "--apply", stdout=out)
        self.assertFalse(Flag.objects.filter(user=self.leaver).exists())
        self.assertFalse(EmailNotificationEvent.objects.filter(user=self.leaver).exists())
        self.assertFalse(InboxItems.objects.filter(receiver=self.leaver).exists())
        self.assertTrue(Flag.objects.filter(user=self.mate).exists())
        # Counts only: nothing identifying is printed.
        self.assertNotIn(str(self.leaver.id), out.getvalue())

    def test_a_second_run_finds_nothing(self):
        self._leave_rows_behind()
        deleted_account_leftovers.prune(apply=True)
        again = deleted_account_leftovers.prune(apply=False)
        self.assertEqual(again.matched, 0)


class ExportStaysConsistentTests(LeftoversFixture):
    def test_what_is_deleted_is_no_longer_exported(self):
        """The export lists the person's inbox reports and reminders-type data by recipient: after
        deletion none of the rows it would read is left."""
        from origin.services.account_export import build_export

        InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.leaver,
            item_type=10,
            item_body={"title": "r"},
        )
        self.assertEqual(build_export(self.leaver)["counts"]["schedule_reports"], 1)
        delete_account(self.leaver)
        self.assertEqual(build_export(self.leaver)["counts"]["schedule_reports"], 0)


class SecurityNetworkFieldsTests(LeftoversFixture):
    """MIP-1789: a deleted person's IP and user agent leave the security log. Every other column
    stays: the action, the time, the team and the actor id are the record a breach needs."""

    def _row(
        self, actor_id, action=SecurityAction.API_KEY_USED, ip="203.0.113.9", ua="Mozilla/5.0"
    ):
        return SecurityEvent.objects.create(
            action=action,
            source="api_key",
            actor_id=actor_id,
            actor_label="",
            team_id=self.team.team_id,
            target_type="api_key",
            target_id="k1",
            ip=ip,
            user_agent=ua,
            metadata={"n": 1},
        )

    def _leave_rows_behind(self):
        """An account deleted BEFORE this rule: soft-deleted, its rows still hold the network."""
        self.theirs = self._row(self.leaver.pk)
        self.theirs_null = self._row(self.leaver.pk, ip=None, ua="")
        self.mates = self._row(self.mate.pk)
        self.no_actor = self._row(None, action=SecurityAction.SIGNIN_FAILED)
        type(self.leaver).objects.filter(pk=self.leaver.pk).update(is_deleted=True)

    def test_deleting_an_account_blanks_its_network_fields_and_keeps_the_rest(self):
        mine = self._row(self.leaver.pk, action=SecurityAction.SIGNIN_SUCCEEDED)
        before = SecurityEvent.objects.get(pk=mine.pk)
        delete_account(self.leaver)
        after = SecurityEvent.objects.get(pk=mine.pk)
        self.assertIsNone(after.ip)
        self.assertEqual(after.user_agent, "")
        for field in ("action", "ts_created_at", "team_id", "actor_id", "source", "metadata"):
            self.assertEqual(getattr(after, field), getattr(before, field), field)

    def test_the_deletion_record_itself_loses_the_requesters_network_fields(self):
        from django.test import RequestFactory

        from origin.middleware import current_user

        request = RequestFactory().post(
            "/", REMOTE_ADDR="198.51.100.4", HTTP_USER_AGENT="Requester/1.0"
        )
        with patch.object(current_user._state, "request", request, create=True):
            delete_account(self.leaver)
        rows = SecurityEvent.objects.filter(actor_id=self.leaver.pk)
        self.assertTrue(rows.filter(action=SecurityAction.ACCOUNT_DELETED).exists())
        self.assertFalse(rows.filter(ip__isnull=False).exists())
        self.assertFalse(rows.exclude(user_agent="").exists())

    def test_another_members_rows_and_rows_with_no_actor_keep_their_network_fields(self):
        self._leave_rows_behind()
        deleted_account_leftovers.prune(apply=True)
        self.mates.refresh_from_db()
        self.no_actor.refresh_from_db()
        self.assertEqual(self.mates.ip, "203.0.113.9")
        self.assertEqual(self.no_actor.ip, "203.0.113.9")

    def test_the_prune_counts_a_dry_run_and_blanks_only_the_deleted_accounts_rows(self):
        self._leave_rows_behind()
        dry = deleted_account_leftovers.prune(apply=False)
        self.assertEqual(dry.by_table["security_network_fields"], 1)
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.ip, "203.0.113.9")

        applied = deleted_account_leftovers.prune(apply=True)
        self.assertEqual(applied.by_table["security_network_fields"], 1)
        self.theirs.refresh_from_db()
        self.assertIsNone(self.theirs.ip)
        self.assertEqual(self.theirs.user_agent, "")
        self.assertEqual(self.theirs.action, SecurityAction.API_KEY_USED)
        self.assertEqual(
            deleted_account_leftovers.prune(apply=False).by_table.get("security_network_fields", 0),
            0,
        )

    def test_the_command_counts_without_printing_ids(self):
        self._leave_rows_behind()
        out = StringIO()
        call_command("prune_deleted_account_leftovers", stdout=out)
        self.assertIn("security_network_fields=1", out.getvalue())
        self.assertNotIn(str(self.leaver.pk), out.getvalue())
        self.assertNotIn("203.0.113.9", out.getvalue())
