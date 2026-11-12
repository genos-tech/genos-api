"""Opening a task or a note marks its unread activity read (MIP-1421).

The rows are made through the real producers (the comment fan-out, the body-mention notifier,
the surface comment/mention producers, the chat send), then the caller opens the task or note
through the real endpoint the client calls when it SHOWS it. Pinned:

  * what the task preview shows is cleared, and the ids come back as `readActivityIds`;
  * what it does not show stays unread: a plain reply in the task's project-chat thread, the
    task NOTE's rows, another task's rows, another person's rows;
  * the same for a note: only that note's rows, of that note type;
  * an open that is refused (no access) clears nothing;
  * a failure of the clear never fails the open.
"""

from __future__ import annotations

from unittest import mock

from origin.models.chat.unified_models import Activity
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster
from origin.services import activity_seen, v3_activity
from origin.tests.test_briefing_mentions import mention
from origin.tests.test_unread_digest import UnreadBase, para
from origin.views.chat import message_views

TASK_OPEN = "/api/v2/task/recent/open/"
NOTE_OPEN = "/api/v2/note/recent/"


class SeenBase(UnreadBase):
    def setUp(self):
        super().setUp()
        self.client.credentials()
        self.client.force_authenticate(user=self.user)

    def unread_ids(self, user=None):
        return set(
            str(i)
            for i in Activity.objects.filter(
                recipient=user or self.user, is_read=False
            ).values_list("id", flat=True)
        )

    def open_task(self, task):
        response = self.client.post(TASK_OPEN, {"task_id": task.task_id}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        return set(response.json()["readActivityIds"])

    def open_note(self, note, note_type):
        response = self.client.post(
            NOTE_OPEN,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "note_id": note.note_id,
                "note_type": note_type,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.content)
        return set(response.json()["readActivityIds"])

    def body_comment(self, task, text="is this in scope?"):
        return v3_activity.create_surface_comment_activities(
            team_id=self.team.team_id,
            actor=self.user2,
            surface_type=5,
            entity_key=f"comment-participant:{task.task_id}-{text}",
            meta={"taskId": task.task_id, "firstLineContent": text},
        )


class OpeningATaskTests(SeenBase):
    def test_comments_mentions_and_inline_comments_on_the_task_are_cleared(self):
        task = self.make_task("Fix the footer")
        self.task_comment(task, "I pushed a fix")
        self.task_comment(task, body=[mention(self.user.id)])
        self.body_comment(task)
        before = self.unread_ids()
        self.assertEqual(len(before), 3)
        cleared = self.open_task(task)
        self.assertEqual(cleared, before)
        self.assertEqual(self.unread_ids(), set())

    def test_a_body_mention_stored_with_a_string_task_id_is_cleared_too(self):
        task = self.make_task("Spec")
        v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user2,
            surface_type=5,
            entity_key=f"task:{task.task_id}",
            newly_mentioned_user_ids=[self.me],
            meta={"taskId": str(task.task_id)},
        )
        self.assertEqual(len(self.open_task(task)), 1)

    def test_a_plain_reply_in_the_tasks_project_chat_thread_stays_unread(self):
        """It shows in chat, not in the task preview: the chat read cursor clears it."""
        task = self.make_task("Thread task")
        self.task_comment(task, "a comment")  # makes the task's card and its thread
        mirror = Activity.objects.get(recipient=self.user, message__task_id=task.task_id)
        card = mirror.message.thread_root
        channel = mirror.message.channel
        # The caller takes part in the thread, so the next reply is addressed to them.
        self.send(channel, "I'll look", sender=self.user, parent=card)
        # A client may send `taskId` with a reply in a task's thread, which links the reply to
        # the task (`message.task_id`); it is still a chat reply, not a comment (no
        # `taskCommentId`), and the task preview does not show it.
        reply, _ = message_views._allocate_seq_and_create_message_with_activities(
            channel=channel,
            sender=self.user2,
            body=[para("chatting in the thread")],
            body_text="chatting in the thread",
            parent_id=card.id,
            metadata={"taskId": task.task_id},
        )
        self.assertEqual(reply.task_id, task.task_id)
        reply_row = Activity.objects.get(recipient=self.user, message=reply, is_read=False)
        cleared = self.open_task(task)
        self.assertEqual(cleared, {str(mirror.id)})
        reply_row.refresh_from_db()
        self.assertFalse(reply_row.is_read)

    def test_another_tasks_rows_and_the_task_notes_rows_stay_unread(self):
        task = self.make_task("Mine")
        other = self.make_task("Other")
        self.task_comment(other, "elsewhere")
        note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user2, title="Notes"
        )
        self.note_row(note, 2, "see the note")
        Activity.objects.filter(meta__noteId=note.note_id).update(
            meta={**{"noteId": note.note_id, "noteType": 2, "taskId": task.task_id}}
        )
        before = self.unread_ids()
        self.assertEqual(self.open_task(task), set())
        self.assertEqual(self.unread_ids(), before)

    def test_someone_elses_rows_are_untouched(self):
        task = self.make_task("Shared", assignee=self.user2, reporter=self.user)
        self.task_comment(task, "for user2", sender=self.user)
        theirs = self.unread_ids(self.user2)
        self.assertTrue(theirs)
        self.open_task(task)
        self.assertEqual(self.unread_ids(self.user2), theirs)

    def test_a_refused_open_clears_nothing(self):
        closed = ProjectMaster.objects.create(
            team=self.team,
            project_name="Board only",
            code="BRD",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = self.make_task("Not mine", assignee=self.user2, project=closed)
        self.body_comment(task)
        before = self.unread_ids()
        response = self.client.post(TASK_OPEN, {"task_id": task.task_id}, format="json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.unread_ids(), before)

    def test_a_failing_clear_never_fails_the_open(self):
        task = self.make_task("Mine")
        self.task_comment(task)
        with mock.patch.object(
            activity_seen.Activity.objects, "filter", side_effect=RuntimeError("db down")
        ):
            response = self.client.post(TASK_OPEN, {"task_id": task.task_id}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["readActivityIds"], [])

    def test_opening_again_clears_nothing_new(self):
        task = self.make_task("Mine")
        self.task_comment(task)
        self.assertEqual(len(self.open_task(task)), 1)
        self.assertEqual(self.open_task(task), set())


class OpeningANoteTests(SeenBase):
    def shared_note(self, title="Shared plan"):
        """The caller's own note, with the owner row the app writes on create (the open
        endpoint reads roles from `NotePermissionMaster`, not from `owner`)."""
        note = super().shared_note(title)
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=note.note_id, note_type=1, role_id=1
        )
        return note

    def test_the_notes_mentions_and_inline_comments_are_cleared(self):
        note = self.shared_note("Launch plan")
        self.note_row(note, 1, "check section 2")
        with mock.patch.object(v3_activity, "_comment_recipients", return_value={self.me}):
            self.note_row(note, 1, "typo", is_comment=True)
        before = self.unread_ids()
        self.assertEqual(len(before), 2)
        self.assertEqual(self.open_note(note, 1), before)
        self.assertEqual(self.unread_ids(), set())

    def test_only_that_note_and_that_note_type(self):
        note = self.shared_note("Mine")
        other = self.shared_note("Other")
        self.note_row(other, 1, "elsewhere")
        # a TASK note with the same numeric id rides a different surface: not this note
        task = self.make_task("T")
        task_note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, task=task, owner=self.user2, title="TN"
        )
        self.note_row(task_note, 2, "same number, other family")
        Activity.objects.filter(surface_type=7).update(meta={"noteId": note.note_id, "noteType": 2})
        before = self.unread_ids()
        self.assertEqual(self.open_note(note, 1), set())
        self.assertEqual(self.unread_ids(), before)

    def test_a_note_the_caller_cannot_open_clears_nothing(self):
        private = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Their diary", body=[]
        )
        self.note_row(private, 1, "hello")
        before = self.unread_ids()
        response = self.client.post(
            NOTE_OPEN,
            {
                "team_id": str(self.team.team_id),
                "user_id": str(self.user.id),
                "note_id": private.note_id,
                "note_type": 1,
            },
            format="json",
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.unread_ids(), before)


class HelperTests(SeenBase):
    def test_an_unknown_note_type_clears_nothing(self):
        self.assertEqual(activity_seen.mark_note_activity_read(self.me, 9, 1), [])

    def test_a_chat_message_is_never_cleared_by_a_task_open(self):
        channel = self.channel([self.user, self.user2])
        self.send(channel, "hi", body=[para("hi")])
        task = self.make_task("T")
        before = self.unread_ids()
        self.assertEqual(self.open_task(task), set())
        self.assertEqual(self.unread_ids(), before)
