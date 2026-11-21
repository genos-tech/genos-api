"""MIP-1584: force delete (real delete) of a message, all of one's messages in a chat, a task, a
task comment and a note, with who may and what is left.

Rules under test (note "Decision: who can force-delete what (MIP-1584)"): the sender (or a group
owner) for a message; the caller's own rows only for "all my messages"; the task's creator or the
project owner for a task; the author for a comment; the note's owner for a note. Every call needs
the typed confirmation, writes one `data.force_deleted` event with ids and counts only, and removes
files and search chunks after the commit.
"""

from __future__ import annotations

import shutil
import tempfile
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageAttachment,
    MessageReaction,
)
from origin.models.common.inbox_models import InboxItems
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import (
    TaskAttachments,
    TaskComments,
    TaskDependency,
    TaskMaster,
    TaskTags,
)
from origin.models.task.task_recent_models import TaskRecent
from origin.services import force_delete as svc
from origin.services import unified_writer
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL, ROLE_EDITOR, ROLE_OWNER

User = get_user_model()
CONFIRM = {"confirm": "delete-permanently"}
SECRET = "very secret words"
_MEDIA = tempfile.mkdtemp(prefix="mip1584-media-")


def text_body(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


def events():
    return SecurityEvent.objects.filter(action=SecurityAction.DATA_FORCE_DELETED)


@override_settings(MEDIA_ROOT=_MEDIA)
class Base(BaseAPITestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        super().setUp()
        for target in ("origin.search_engine.purge.purge_entities",):
            patcher = mock.patch(target)
            patcher.start()
            self.addCleanup(patcher.stop)
        forget = mock.patch("origin.services.collab_push.forget_document")
        self.forget = forget.start()
        self.addCleanup(forget.stop)
        search = mock.patch("origin.services.project_delete._forget_search")
        self.forget_search = search.start()
        self.addCleanup(search.stop)
        note_purge = mock.patch("origin.services.note_delete.purge_note")
        self.note_purge = note_purge.start()
        self.addCleanup(note_purge.stop)
        self.editor = User.objects.create_user(username="ed1584", email="ed1584@example.com")
        TeamMembers.objects.create(team=self.team, attendee=self.editor)

    def post(self, url, user, body=CONFIRM):
        self.authenticate(user)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(url, body, format="json")


class MessageForceDelete(Base):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="G", owner=self.user
        )
        for user, role in ((self.user, "owner"), (self.user2, "member"), (self.editor, "editor")):
            ChannelMember.objects.create(channel=self.channel, user=user, role=role)
        self.msg = Message.objects.create(
            channel=self.channel,
            sender=self.user2,
            seq=1,
            body=text_body(SECRET),
            body_text=SECRET,
        )
        self.url = reverse("v3_message_force_delete", args=[self.msg.id])

    def fill(self):
        att = MessageAttachment.objects.create(
            message=self.msg,
            uploader=self.user2,
            file=SimpleUploadedFile("a.txt", b"hello"),
            size_bytes=5,
        )
        self.stored = att.file.name
        MessageReaction.objects.create(message=self.msg, user=self.user, emoji="+1")
        Flag.objects.create(message=self.msg, user=self.user)
        Activity.objects.create(
            team=self.team,
            recipient=self.user,
            message=self.msg,
            activity_type=ActivityType.MENTION,
        )

    def test_the_sender_erases_text_attachments_file_reactions_flags_and_inbox_rows(self):
        self.fill()
        self.assertTrue(default_storage.exists(self.stored))
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, "")
        self.assertEqual(self.msg.body, [])
        self.assertIsNotNone(self.msg.deleted_at)
        self.assertFalse(MessageAttachment.objects.filter(message=self.msg).exists())
        self.assertFalse(MessageReaction.objects.filter(message=self.msg).exists())
        self.assertFalse(Flag.objects.filter(message=self.msg).exists())
        self.assertFalse(Activity.objects.filter(message=self.msg).exists())
        self.assertFalse(default_storage.exists(self.stored))
        self.assertEqual(resp.data["removed"]["attachments"], 1)
        self.assertEqual(resp.data["left"]["empty message rows"], 1)

    def test_a_pending_email_and_a_reminder_card_do_not_outlive_the_text(self):
        """MIP-1940: `EmailNotificationEvent.activity` is SET_NULL, so deleting the Activity row
        un-linked a PENDING mention email and the tick sent it with the erased text."""
        from origin.services.message_reminders import ITEM_TYPE_MESSAGE_REMINDER

        self.fill()
        activity = Activity.objects.get(message=self.msg)
        mail = EmailNotificationEvent.objects.create(
            user=self.user, category="mention", title="t", body=SECRET, activity=activity
        )
        card = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_type=ITEM_TYPE_MESSAGE_REMINDER,
            item_body={"title": "Reminder", "text": SECRET},
            item_optionals={"message_id": str(self.msg.id), "preview": SECRET},
            request_status="",
        )
        card_mail = EmailNotificationEvent.objects.create(
            user=self.user, category="reminder", title="t", body=SECRET, inbox_item=card
        )
        other = InboxItems.objects.create(
            team=self.team,
            sender=self.user2,
            receiver=self.user,
            item_type=ITEM_TYPE_MESSAGE_REMINDER,
            item_body={"title": "Reminder", "text": "other"},
            item_optionals={"message_id": "00000000-0000-0000-0000-000000000000"},
            request_status="",
        )
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(EmailNotificationEvent.objects.filter(pk__in=[mail.pk, card_mail.pk]))
        self.assertFalse(InboxItems.objects.filter(pk=card.pk).exists())
        self.assertTrue(InboxItems.objects.filter(pk=other.pk).exists())
        self.assertFalse(
            EmailNotificationEvent.objects.filter(body__contains=SECRET).exists(),
            "no outbox row may still hold the erased text",
        )
        self.assertEqual(resp.data["removed"]["email rows and reminder cards"], 3)

    def _chat_mail(self, *, body=SECRET, url=None, status=None, user=None):
        from origin.services.webpush_dispatch import _chat_url

        return EmailNotificationEvent.objects.create(
            user=user or self.user,
            category="chats",
            title="t",
            body=body,
            url=url if url is not None else _chat_url(self.channel),
            status=EmailNotificationEvent.STATUS_PENDING if status is None else status,
        )

    def test_a_pending_chat_email_with_the_text_is_erased_with_the_message(self):
        """MIP-1950: the `chats` outbox row is written by `enqueue_email_for_message` with the text
        and the chat url, and NO link to the message or an inbox row."""
        mail = self._chat_mail()
        sent = self._chat_mail(status=EmailNotificationEvent.STATUS_SENT)
        other_chat = self._chat_mail(url="/workspace/chat/gm/other")
        other_text = self._chat_mail(body="something else")
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(EmailNotificationEvent.objects.filter(pk=mail.pk).exists())
        # already sent (it left the building), another chat and another text stay
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=sent.pk).exists())
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=other_chat.pk).exists())
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=other_text.pk).exists())
        self.assertEqual(resp.data["removed"]["email rows and reminder cards"], 1)

    def test_a_chat_email_created_before_the_message_is_kept(self):
        from datetime import timedelta

        old = self._chat_mail()
        EmailNotificationEvent.objects.filter(pk=old.pk).update(
            ts_created_at=self.msg.ts_sent_at - timedelta(days=1)
        )
        self.assertEqual(self.post(self.url, self.user2).status_code, 200)
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=old.pk).exists())

    def test_an_attachment_only_message_does_not_delete_a_blank_chat_email(self):
        Message.objects.filter(pk=self.msg.pk).update(body_text="", body=[])
        blank = self._chat_mail(body="")
        self.assertEqual(self.post(self.url, self.user2).status_code, 200)
        self.assertTrue(EmailNotificationEvent.objects.filter(pk=blank.pk).exists())

    def test_no_pending_row_still_holds_the_erased_text(self):
        self._chat_mail()
        self.assertEqual(self.post(self.url, self.user2).status_code, 200)
        self.assertFalse(
            EmailNotificationEvent.objects.filter(
                status=EmailNotificationEvent.STATUS_PENDING, body__contains=SECRET
            ).exists()
        )

    def test_the_group_owner_may_an_editor_and_a_member_may_not(self):
        self.assertEqual(self.post(self.url, self.editor).status_code, 403)
        self.assertEqual(self.post(self.url, self.user).status_code, 200)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, "")

    def test_a_refusal_changes_nothing_and_writes_no_event(self):
        self.post(self.url, self.editor)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, SECRET)
        self.assertEqual(events().count(), 0)

    def test_a_non_member_gets_404(self):
        outsider = User.objects.create_user(username="out1584", email="out1584@example.com")
        self.assertEqual(self.post(self.url, outsider).status_code, 404)

    def test_without_the_typed_confirmation_it_is_a_400(self):
        self.assertEqual(self.post(self.url, self.user2, body={}).status_code, 400)
        self.assertEqual(self.post(self.url, self.user2, body={"confirm": "yes"}).status_code, 400)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, SECRET)

    def test_a_reply_by_somebody_else_survives_and_the_parent_count_goes_down(self):
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=2,
            body=text_body("keep"),
            body_text="keep",
            parent=self.msg,
            thread_root=self.msg,
            is_thread_reply=True,
        )
        Message.objects.filter(pk=self.msg.pk).update(reply_count=1)
        self.post(reverse("v3_message_force_delete", args=[reply.id]), self.user)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.reply_count, 0)
        self.assertEqual(self.msg.body_text, SECRET)  # the parent is another person's

    def test_a_task_linked_message_is_refused(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        task = TaskMaster.objects.create(team=self.team, project=project, title="t", status="Open")
        Message.objects.filter(pk=self.msg.pk).update(task=task)
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "task_linked")

    def test_the_security_event_has_ids_and_counts_and_no_text(self):
        self.fill()
        self.post(self.url, self.user2)
        event = events().get()
        self.assertEqual(event.target_type, "message")
        self.assertEqual(event.target_id, str(self.msg.id))
        self.assertEqual(str(event.actor_id), str(self.user2.id))
        self.assertEqual(event.metadata["removed"]["attachments"], 1)
        dump = f"{event.metadata} {event.target_label} {event.actor_label}"
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("a.txt", dump)

    def test_no_event_when_the_transaction_rolls_back(self):
        with mock.patch.object(svc, "_remove_files_after_commit", side_effect=RuntimeError):
            self.authenticate(self.user2)
            with self.assertRaises(RuntimeError):
                self.client.post(self.url, CONFIRM, format="json")
        self.assertEqual(events().count(), 0)
        self.msg.refresh_from_db()
        self.assertEqual(self.msg.body_text, SECRET)


class MyMessagesForceDelete(Base):
    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM)
        ChannelMember.objects.create(channel=self.dm, user=self.user, role="member")
        ChannelMember.objects.create(channel=self.dm, user=self.user2, role="member")
        self.mine = [
            Message.objects.create(
                channel=self.dm, sender=self.user, seq=i, body=text_body(SECRET), body_text=SECRET
            )
            for i in (1, 2)
        ]
        self.theirs = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=3, body=text_body("theirs"), body_text="theirs"
        )
        self.url = reverse("v3_my_messages_force_delete", args=[self.dm.id])

    def test_only_my_rows_go_and_the_partners_copy_and_the_chat_stay(self):
        resp = self.post(self.url, self.user)
        self.assertEqual(resp.status_code, 200, resp.data)
        for m in self.mine:
            m.refresh_from_db()
            self.assertEqual(m.body_text, "")
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.body_text, "theirs")
        self.assertTrue(Channel.objects.filter(pk=self.dm.pk).exists())
        self.assertEqual(resp.data["remaining"], 0)
        self.assertEqual(resp.data["removed"]["messages"], 2)

    def test_the_batch_is_small_enough_for_one_request(self):
        self.assertLessEqual(svc.MY_MESSAGES_BATCH, 200)

    def test_the_open_chats_are_told_once_per_erased_message(self):
        """MIP-1943: no push meant the text stayed on screen until the next sync."""
        with mock.patch("origin.services.realtime_push.push_messages") as push:
            self.post(self.url, self.user)
        push.assert_called_once()
        pushed = {m.pk for m in push.call_args.args[0]}
        self.assertEqual(pushed, {m.pk for m in self.mine})

    def test_queries_per_message_stay_bounded(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        for i in range(10, 20):
            Message.objects.create(
                channel=self.dm, sender=self.user, seq=i, body=text_body("x"), body_text="x"
            )
        self.authenticate(self.user)
        with CaptureQueriesContext(connection) as ctx:
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self.url, CONFIRM, format="json")
        n = resp.data["removed"]["messages"]
        self.assertEqual(n, 12)
        self.assertLess(len(ctx) / n, 30, f"{len(ctx)} queries for {n} messages")

    def test_the_partner_cannot_delete_my_messages_through_it(self):
        self.post(self.url, self.user2)
        for m in self.mine:
            m.refresh_from_db()
            self.assertEqual(m.body_text, SECRET)

    def test_a_non_member_gets_404(self):
        self.assertEqual(self.post(self.url, self.editor).status_code, 404)

    def test_the_batch_limit_reports_what_remains(self):
        with mock.patch.object(svc, "MY_MESSAGES_BATCH", 1):
            self.authenticate(self.user)
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(self.url, CONFIRM, format="json")
        self.assertEqual(resp.data["remaining"], 1)
        resp = self.post(self.url, self.user)
        self.assertEqual(resp.data["remaining"], 0)


class TaskBase(Base):
    def setUp(self):
        super().setUp()
        # self.user owns the project; self.user2 creates tasks; self.editor is an editor.
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        for user, role in ((self.user2, "viewer"), (self.editor, "editor")):
            ProjectMembers.objects.create(
                project=self.project, attendee=user, member_role=role, team=self.team
            )
        self.anchor = self.make_task(self.user, "anchor")
        self.task = self.make_task(self.user2, "Secret plan")
        # keep `anchor` as the highest number so `task` is a plain row delete
        top = self.task.project_task_number or 0
        TaskMaster.objects.filter(pk=self.anchor.pk).update(project_task_number=top + 10)
        self.url = reverse("v3_task_force_delete", args=[self.task.task_id])

    def make_task(self, creator, title, **extra):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open", **extra
        )
        TaskActivity.objects.filter(task=task, action_type=TaskActivityActionType.CREATED).update(
            actor=creator
        )
        return task

    def attach(self, task):
        row = TaskAttachments(task=task, attachment_id=1, original_filename="a.txt")
        row.attached_file.save("a.txt", ContentFile(b"bytes"), save=True)
        return row


class TaskForceDelete(TaskBase):
    def test_the_creator_removes_the_row_comments_files_history_and_search_chunks(self):
        att = self.attach(self.task)
        stored = att.attached_file.name
        TaskComments.objects.create(
            task=self.task, sender=self.user2, comment_id=1, comment_body=text_body(SECRET)
        )
        task_id = self.task.task_id
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(TaskMaster.objects.filter(task_id=task_id).exists())
        self.assertFalse(TaskComments.objects.filter(task_id=task_id).exists())
        self.assertFalse(TaskAttachments.objects.filter(task_id=task_id).exists())
        self.assertFalse(TaskActivity.objects.filter(task_id=task_id).exists())
        self.assertFalse(default_storage.exists(stored))
        self.forget_search.assert_called_once()
        self.assertEqual(self.forget_search.call_args.args[0], [task_id])
        self.forget.assert_any_call(f"task-body:{task_id}")
        self.assertFalse(resp.data["scrubbed"])

    def test_the_project_owner_may_even_without_creating_it(self):
        self.assertEqual(self.post(self.url, self.user).status_code, 200)

    def test_an_editor_a_reporter_set_by_an_editor_and_an_outsider_may_not(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(reporter=self.editor)
        self.assertEqual(self.post(self.url, self.editor).status_code, 403)
        outsider = User.objects.create_user(username="out1584b", email="out1584b@example.com")
        self.assertEqual(self.post(self.url, outsider).status_code, 404)
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())

    def test_a_task_with_sub_tasks_is_refused_with_a_count(self):
        TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="child",
            status="Open",
            parent_task_id=self.task.task_id,
        )
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data["code"], "has_subtasks")
        self.assertEqual(resp.data["subtasks"], 1)
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())

    def test_the_highest_numbered_task_is_emptied_not_deleted(self):
        top = self.make_task(self.user2, "Top secret", content=text_body(SECRET))
        TaskMaster.objects.filter(pk=top.pk).update(project_task_number=999)
        TaskComments.objects.create(
            task=top, sender=self.user2, comment_id=1, comment_body=text_body(SECRET)
        )
        resp = self.post(reverse("v3_task_force_delete", args=[top.task_id]), self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["scrubbed"])
        top.refresh_from_db()
        self.assertEqual(top.title, "(deleted)")
        self.assertIsNone(top.content)
        self.assertEqual(top.status, "Deleted")
        self.assertEqual(top.project_task_number, 999)
        self.assertFalse(TaskComments.objects.filter(task_id=top.task_id).exists())
        # the next new task still gets a fresh number
        nxt = TaskMaster.objects.create(
            team=self.team, project=self.project, title="n", status="Open"
        )
        self.assertGreater(nxt.project_task_number, 999)

    def test_the_emptied_task_has_the_shape_collab_relies_on(self):
        """MIP-1950: genos-collab `documentForget` answers 409 (and keeps the body) unless the task
        row is `(deleted)`, has no content, is deleted and has exactly ONE history row with no
        actor. A later change that adds a history row would silently break that."""
        top = self.make_task(self.user2, "Top secret", content=text_body(SECRET))
        TaskMaster.objects.filter(pk=top.pk).update(project_task_number=999)
        resp = self.post(reverse("v3_task_force_delete", args=[top.task_id]), self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["scrubbed"])
        top.refresh_from_db()
        self.assertEqual(top.title, "(deleted)")
        self.assertIsNone(top.content)
        self.assertTrue(top.is_deleted)
        rows = list(TaskActivity.objects.filter(task=top))
        self.assertEqual(len(rows), 1, [r.action_type for r in rows])
        self.assertIsNone(rows[0].actor_id)

    def test_the_scrubbed_task_keeps_no_pr_links_tags_recents_or_calendar_link(self):
        """MIP-1942: the row stays, so the cascades a plain delete relies on do not run."""
        top = self.make_task(self.user2, "Top secret", content=text_body(SECRET))
        TaskMaster.objects.filter(pk=top.pk).update(
            project_task_number=999,
            linked_calendar_event_id="evt-secret",
            linked_calendar_id="cal-secret",
        )
        TaskDelivery.objects.create(
            team=self.team,
            task=top,
            repo_owner="o",
            repo_name="r",
            pr_title=SECRET,
            branch="secret-branch",
            state="open",
            source="webhook",
        )
        TaskTags.objects.create(
            project=self.project,
            task=top,
            tag_id=1,
            tag_name="t",
            tag_color="a",
            tag_text_color="b",
        )
        TaskRecent.objects.create(user=self.user2, task=top, team=self.team)
        resp = self.post(reverse("v3_task_force_delete", args=[top.task_id]), self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["scrubbed"])
        top.refresh_from_db()
        self.assertFalse(TaskDelivery.objects.filter(task_id=top.task_id).exists())
        self.assertFalse(TaskTags.objects.filter(task_id=top.task_id).exists())
        self.assertFalse(TaskRecent.objects.filter(task_id=top.task_id).exists())
        self.assertIsNone(top.linked_calendar_event_id)
        self.assertIsNone(top.linked_calendar_id)

    def test_a_pending_email_for_a_task_comment_does_not_outlive_it(self):
        """MIP-1940 for tasks: the Activity rows of the card and comment mirrors."""
        from origin.models.chat.unified_models import Channel as Ch

        chan = Ch.objects.create(team=self.team, kind=ChannelKind.GM, title="T", owner=self.user)
        msg = Message.objects.create(
            channel=chan, sender=self.user2, seq=1, body=text_body(SECRET), body_text=SECRET
        )
        Message.objects.filter(pk=msg.pk).update(task=self.task)
        activity = Activity.objects.create(
            team=self.team, recipient=self.user, message=msg, activity_type=ActivityType.MENTION
        )
        mail = EmailNotificationEvent.objects.create(
            user=self.user, category="mention", title="t", body=SECRET, activity=activity
        )
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(EmailNotificationEvent.objects.filter(pk=mail.pk).exists())

    def test_a_dependency_to_a_live_task_is_removed(self):
        TaskDependency.objects.create(
            team=self.team, blocker_task_id=self.task.task_id, blocked_task_id=self.anchor.task_id
        )
        self.assertEqual(self.post(self.url, self.user2).status_code, 200)
        self.assertFalse(TaskDependency.objects.exists())
        self.anchor.refresh_from_db()

    def test_the_event_has_the_counts_and_not_the_title(self):
        self.post(self.url, self.user2)
        event = events().get()
        self.assertEqual(event.target_type, "task")
        self.assertNotIn("Secret plan", f"{event.metadata} {event.target_label}")

    def test_a_task_without_confirmation_is_untouched(self):
        self.assertEqual(self.post(self.url, self.user2, body={}).status_code, 400)
        self.assertTrue(TaskMaster.objects.filter(pk=self.task.pk).exists())


class CommentForceDelete(TaskBase):
    def setUp(self):
        super().setUp()
        self.comment = TaskComments.objects.create(
            task=self.task, sender=self.user2, comment_id=1, comment_body=text_body(SECRET)
        )
        self.mirror_id = unified_writer.task_comment_message_uuid(self.task.task_id, 1)
        self.url = reverse("v3_task_comment_force_delete", args=[self.task.task_id, 1])

    def test_the_author_blanks_the_comment_and_the_row_stays_so_ids_are_not_reused(self):
        resp = self.post(self.url, self.user2)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.comment.refresh_from_db()
        self.assertTrue(self.comment.is_deleted)
        self.assertEqual(self.comment.comment_body, [])
        self.assertEqual(resp.data["left"]["empty comment rows"], 1)

    def test_somebody_else_even_the_project_owner_may_not(self):
        self.assertEqual(self.post(self.url, self.user).status_code, 403)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.comment_body, text_body(SECRET))

    def test_an_outsider_gets_404(self):
        outsider = User.objects.create_user(username="out1584c", email="out1584c@example.com")
        self.assertEqual(self.post(self.url, outsider).status_code, 404)


class NoteForceDelete(Base):
    def setUp(self):
        super().setUp()
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan", body=text_body(SECRET)
        )
        for user, role in ((self.user, ROLE_OWNER), (self.user2, ROLE_EDITOR)):
            NotePermissionMaster.objects.create(
                team=self.team,
                user=user,
                note_id=self.note.note_id,
                note_type=NOTE_TYPE_PERSONAL,
                role_id=role,
            )
        self.url = reverse("v3_note_force_delete", args=["personal", self.note.note_id])

    def test_the_owner_removes_the_note_and_asks_collab_to_forget_the_document(self):
        resp = self.post(self.url, self.user)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(PersonalNoteMaster.objects.filter(pk=self.note.pk).exists())
        self.assertFalse(NotePermissionMaster.objects.filter(note_id=self.note.note_id).exists())
        self.note_purge.assert_called_once_with("personal", self.note.note_id)
        self.forget.assert_called_once_with(f"my-note:{self.note.note_id}")
        event = events().get()
        self.assertNotIn("Plan", f"{event.metadata} {event.target_label}")

    def test_a_note_with_sub_pages_is_refused_with_a_count(self):
        """MIP-1941: the same rule as the agent's delete tool and as tasks with sub-tasks."""
        for owner in (self.user, self.user2):
            PersonalNoteMaster.objects.create(
                team=self.team,
                owner=owner,
                title="child",
                body=text_body(SECRET),
                parent_note_id=self.note.note_id,
            )
        resp = self.post(self.url, self.user)
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data["code"], "has_children")
        self.assertEqual(resp.data["children"], 2)
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=self.note.pk).exists())
        self.assertEqual(events().count(), 0)

    def test_an_editor_may_not_and_nothing_is_touched(self):
        self.assertEqual(self.post(self.url, self.user2).status_code, 403)
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=self.note.pk).exists())
        self.forget.assert_not_called()

    def test_a_stranger_gets_404(self):
        self.assertEqual(self.post(self.url, self.editor).status_code, 404)

    def test_a_rolled_back_delete_never_reaches_collab(self):
        with mock.patch.object(svc, "record_security_event", side_effect=RuntimeError):
            self.authenticate(self.user)
            with self.assertRaises(RuntimeError):
                with self.captureOnCommitCallbacks(execute=True):
                    self.client.post(self.url, CONFIRM, format="json")
        self.forget.assert_not_called()
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=self.note.pk).exists())
