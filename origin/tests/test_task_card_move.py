"""A task moved to another project takes its chat card and comment thread along (MIP-2505)."""

from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Flag,
    Message,
    MessageMovedAway,
    MessagePin,
    ReadCursor,
)
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.models import ThreadSummary
from origin.services import realtime_push, task_card_move
from origin.services.demo_seeder import delete_demo_team_data
from origin.services.unified_writer import (
    task_comment_message_uuid,
    write_task_comment_as_thread_reply,
)
from origin.views.common.media_views import _may_read_media
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    ROLE_EDITOR,
    ROLE_OWNER,
    get_effective_role,
)

from .status_reason_support import with_reason
from .test_task_card_post import CardPostBase

User = get_user_model()


def _paragraph(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class MoveBase(CardPostBase):
    """The old project (`self.project`): user and user2. The new one: user and newcomer."""

    def setUp(self):
        super().setUp()
        self.newcomer = User.objects.create_user(
            username="newcomer", email="newcomer@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.newcomer)
        self.destination = ProjectMaster.objects.create(
            team=self.team,
            project_name="Mobile App",
            code="MOB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.destination, attendee=self.user)
        ProjectMembers.objects.create(
            team=self.team, project=self.destination, attendee=self.newcomer
        )
        self.new_channel = Channel.objects.get(
            kind=ChannelKind.PM, legacy_chat_id=self.destination.pk
        )
        for name in ("push_messages", "push_messages_deleted"):
            patcher = mock.patch.object(realtime_push, name)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)

    def _comment(self, task, comment_id, text="a comment"):
        return write_task_comment_as_thread_reply(
            task_id=task.task_id,
            comment_id=comment_id,
            sender_id=str(self.user.id),
            body=_paragraph(text),
            project_id=task.project_id,
            bypass_flag=True,
        )

    def _move(self, task, project=None, **extra):
        args = {"task_id": task.task_id, "project_id": (project or self.destination).pk, **extra}
        with self.captureOnCommitCallbacks(execute=True):
            UPDATE_TASK.run({**with_reason(args), **args}, self.ctx)
        task.refresh_from_db()
        return task

    def _card_of(self, task):
        return Message.objects.get(task_id=task.task_id, is_thread_reply=False)


class AgentMoveTests(MoveBase):
    def setUp(self):
        super().setUp()
        self.task = self._create(assignee_id=str(self.user.id))
        self.card = self._card_of(self.task)
        self.first = self._comment(self.task, 1, "first")
        self.second = self._comment(self.task, 2, "second")
        # A message already in the new chat, so the new seq is after it.
        self.earlier = self._create_in(self.destination, "Already here")

    def _create_in(self, project, title):
        task = self._create(title=title, project_id=project.pk)
        return self._card_of(task)

    def test_the_card_and_its_thread_move_to_the_end_of_the_new_chat(self):
        self._move(self.task)

        card = Message.objects.get(pk=self.card.pk)
        replies = list(Message.objects.filter(thread_root=card).order_by("seq"))
        self.assertEqual(card.channel_id, self.new_channel.id)
        self.assertEqual([m.pk for m in replies], [self.first.pk, self.second.pk])
        self.assertTrue(all(m.channel_id == self.new_channel.id for m in replies))
        self.assertTrue(all(m.parent_id == card.pk for m in replies))
        self.assertEqual(
            [card.seq, *(m.seq for m in replies)],
            [self.earlier.seq + 1, self.earlier.seq + 2, self.earlier.seq + 3],
        )
        self.assertEqual(card.reply_count, 2)
        # The ids the comment mirror derives are unchanged.
        self.assertEqual(self.first.pk, task_comment_message_uuid(self.task.task_id, 1))
        self.assertFalse(
            Message.objects.filter(channel=self.channel, task_id=self.task.task_id).exists()
        )

    def test_the_card_is_rebuilt_and_a_new_comment_joins_the_same_thread(self):
        task = self._move(self.task)

        card = Message.objects.get(pk=self.card.pk)
        self.assertEqual(card.metadata["displayId"], task.display_id)
        self.assertTrue(task.display_id.startswith("MOB-"))
        third = self._comment(task, 3, "third")
        self.assertEqual(third.thread_root_id, card.pk)
        self.assertEqual(
            Message.objects.filter(task_id=task.task_id, is_thread_reply=False).count(), 1
        )

    def test_the_old_chat_is_told_the_rows_are_gone_and_the_new_one_gets_the_card(self):
        self.push_messages.reset_mock()

        self._move(self.task)

        ((channel, gone), _) = self.push_messages_deleted.call_args
        self.assertEqual(channel.id, self.channel.id)
        self.assertEqual({m.pk for m in gone}, {self.card.pk, self.first.pk, self.second.pk})
        events = {(pk, c.args[1]) for c in self.push_messages.call_args_list for pk in c.args[0]}
        self.assertIn((self.card.pk, realtime_push.UPDATED), events)
        self.assertIn((self.second.pk, realtime_push.UPDATED), events)
        self.assertNotIn(realtime_push.CREATED, {event for _, event in events})

    def test_what_people_held_on_the_thread_stays_only_with_the_new_chats_people(self):
        for person in (self.user, self.user2):
            Flag.objects.create(user=person, message=self.first)
            Activity.objects.create(
                team=self.team,
                recipient=person,
                actor=self.user,
                activity_type=ActivityType.THREAD_REPLY,
                channel=self.channel,
                message=self.second,
            )
            ReadCursor.objects.create(
                user=person,
                channel=self.channel,
                thread_root=self.card,
                last_read_message=self.second,
            )

        self._move(self.task)

        self.assertEqual(
            [f.user_id for f in Flag.objects.filter(message=self.first)], [self.user.id]
        )
        (activity,) = Activity.objects.filter(message=self.second)
        self.assertEqual(
            (activity.recipient_id, activity.channel_id), (self.user.id, self.new_channel.id)
        )
        (cursor,) = ReadCursor.objects.filter(thread_root=self.card)
        self.assertEqual((cursor.user_id, cursor.channel_id), (self.user.id, self.new_channel.id))

    def test_the_old_chats_read_cursor_steps_back_to_what_is_left(self):
        other = self._create(title="Stays")
        stays = self._card_of(other)
        # The one that moves is newer than `stays`.
        mover = self._create(title="Leaves")
        cursor = ReadCursor.objects.create(
            user=self.user2, channel=self.channel, last_read_message=self._card_of(mover)
        )

        self._move(mover)

        cursor.refresh_from_db()
        self.assertEqual(cursor.channel_id, self.channel.id)
        self.assertEqual(cursor.last_read_message_id, stays.pk)

    def test_the_threads_cached_summary_is_dropped(self):
        ThreadSummary.objects.create(
            chat_type=ChannelKind.PM,
            chat_id=self.channel.id,
            thread_id=self.card.pk,
            team_id=str(self.team.team_id),
            summary_text="old",
        )
        with mock.patch("origin.search_engine.purge.purge_entities") as purge:
            self._move(self.task)

        self.assertFalse(ThreadSummary.objects.filter(thread_id=self.card.pk).exists())
        purge.assert_any_call([("chat", f"pm:{self.channel.id}:thread:{self.card.pk}")])

    def test_a_task_made_from_the_thread_follows_it(self):
        child = self._create(title="From the thread")
        TaskMaster.objects.filter(pk=child.pk).update(
            chat_type=ChannelKind.PM, chat_id=str(self.channel.id), thread_id=str(self.card.pk)
        )

        self._move(self.task)

        child.refresh_from_db()
        self.assertEqual(child.chat_id, str(self.new_channel.id))
        self.assertEqual(child.thread_id, str(self.card.pk))

    def test_a_file_sent_in_the_thread_follows_the_message(self):
        path = f"chats/{self.channel.id}/messages/{self.first.pk}/report.pdf"
        self.assertTrue(_may_read_media(path, self.user2.id))
        self.assertFalse(_may_read_media(path, self.newcomer.id))

        self._move(self.task)

        self.assertFalse(_may_read_media(path, self.user2.id))
        self.assertTrue(_may_read_media(path, self.newcomer.id))


class LeftBehindTests(MoveBase):
    """MIP-2516: offline devices, pasted images and thread notes."""

    def setUp(self):
        super().setUp()
        self.task = self._create(assignee_id=str(self.user.id))
        self.card = self._card_of(self.task)
        self.path = f"chats/{self.channel.id}/inline/{'a' * 32}.png"
        self.reply = self._comment(self.task, 1)
        Message.objects.filter(pk=self.reply.pk).update(body=self._image(self.path))

    def _image(self, *paths):
        return [
            {"type": "image", "props": {"url": f"https://api.example.com/media/{p}"}} for p in paths
        ]

    def _delta(self, user, channel, kind, since):
        self.client.force_authenticate(user=user)
        res = self.client.get(f"/api/v3/channels/{channel.id}/{kind}/", {"since": since})
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["data"]["deletes"]

    def test_a_later_sync_of_the_old_chat_names_what_left_it(self):
        since = timezone.now().isoformat()

        self._move(self.task)

        self.assertEqual(
            self._delta(self.user2, self.channel, "messages", since), [str(self.card.pk)]
        )
        self.assertEqual(
            self._delta(self.user2, self.channel, "threads", since), [str(self.reply.pk)]
        )
        self.assertEqual(self._delta(self.user, self.new_channel, "messages", since), [])
        # A sync that ran while the move was committing still hears of it.
        just_after = timezone.now().isoformat()
        self.assertEqual(
            self._delta(self.user2, self.channel, "threads", just_after), [str(self.reply.pk)]
        )
        # A device that synced well after the move has nothing to drop.
        later = (timezone.now() + timedelta(minutes=6)).isoformat()
        self.assertEqual(self._delta(self.user2, self.channel, "threads", later), [])

    def test_what_moved_back_is_not_named(self):
        since = timezone.now().isoformat()

        self._move(self._move(self.task), self.project)

        self.assertEqual(self._delta(self.user, self.channel, "messages", since), [])
        self.assertEqual(self._delta(self.user, self.channel, "threads", since), [])
        self.assertEqual(
            self._delta(self.user, self.new_channel, "threads", since), [str(self.reply.pk)]
        )

    def test_a_pasted_image_is_read_by_the_chat_its_message_is_in(self):
        self.assertTrue(_may_read_media(self.path, self.user2.id))
        self.assertFalse(_may_read_media(self.path, self.newcomer.id))

        task = self._move(self.task)

        self.assertFalse(_may_read_media(self.path, self.user2.id))
        self.assertTrue(_may_read_media(self.path, self.newcomer.id))

        self._move(task, self.project)

        self.assertTrue(_may_read_media(self.path, self.user2.id))
        self.assertFalse(_may_read_media(self.path, self.newcomer.id))

    def test_an_image_still_shown_in_the_old_chat_stays_readable_there(self):
        stays = self._card_of(self._create(title="Stays"))
        Message.objects.filter(pk=stays.pk).update(body=self._image(self.path))

        self._move(self.task)

        self.assertTrue(_may_read_media(self.path, self.user2.id))
        self.assertTrue(_may_read_media(self.path, self.newcomer.id))

    def test_a_link_to_another_chats_file_opens_nothing(self):
        other = Channel.objects.create(team=self.team, kind=ChannelKind.GM, owner=self.user2)
        ChannelMember.objects.create(channel=other, user=self.user2)
        theirs = f"chats/{other.id}/inline/{'b' * 32}.png"
        Message.objects.filter(pk=self.reply.pk).update(body=self._image(theirs))

        self._move(self.task)

        self.assertTrue(_may_read_media(theirs, self.user2.id))
        self.assertFalse(_may_read_media(theirs, self.newcomer.id))

    def test_a_name_that_could_be_guessed_is_not_carried(self):
        guessable = f"chats/{self.channel.id}/inline/image.png"
        Message.objects.filter(pk=self.reply.pk).update(body=self._image(guessable))

        self._move(self.task)

        self.assertTrue(_may_read_media(guessable, self.user2.id))
        self.assertFalse(_may_read_media(guessable, self.newcomer.id))

    def test_a_team_teardown_leaves_no_move_record(self):
        self._move(self.task)
        self.assertTrue(MessageMovedAway.objects.exists())

        delete_demo_team_data(self.team.team_id)

        self.assertFalse(MessageMovedAway.objects.exists())

    def test_the_images_stay_readable_when_the_chat_left_behind_is_deleted(self):
        self._move(self.task)
        Message.objects.filter(channel=self.channel).delete()
        Channel.objects.filter(pk=self.channel.pk).delete()

        self.assertTrue(_may_read_media(self.path, self.newcomer.id))

    def test_a_file_of_an_untouched_message_is_still_read_by_its_own_chat(self):
        self._move(self.task)
        untouched = f"chats/{self.channel.id}/inline/{'c' * 32}.png"

        self.assertTrue(_may_read_media(untouched, self.user2.id))
        self.assertFalse(_may_read_media(untouched, self.newcomer.id))

    def _note(self, **fields):
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user2,
            chat_type=ChannelKind.PM,
            channel=self.channel,
            title="n",
            **{"is_thread": True, "thread_root_id": self.card.pk, **fields},
        )

    def test_the_notes_on_the_thread_move_with_it(self):
        note = self._note()
        child = self._note(parent_note_id=note.note_id, is_thread=False, thread_root_id=None)
        chat_wide = self._note(is_thread=False, thread_root_id=None)
        under_it = self._note(parent_note_id=chat_wide.note_id)
        for person in (self.user, self.user2):
            NotePermissionMaster.objects.create(
                team=self.team,
                user=person,
                note_type=NOTE_TYPE_CHAT,
                note_id=note.note_id,
                role_id=ROLE_EDITOR,
            )
        self.assertIsNone(get_effective_role(self.newcomer.id, NOTE_TYPE_CHAT, note.note_id))

        self._move(self.task)

        for row in (note, child, chat_wide, under_it):
            row.refresh_from_db()
        self.assertEqual(
            (note.channel_id, note.thread_root_id), (self.new_channel.id, self.card.pk)
        )
        self.assertEqual(
            (child.channel_id, child.parent_note_id), (self.new_channel.id, note.note_id)
        )
        # Moved out from under a note that stays: it stands on its own.
        self.assertEqual(
            (under_it.channel_id, under_it.parent_note_id), (self.new_channel.id, None)
        )
        self.assertEqual(chat_wide.channel_id, self.channel.id)
        self.assertEqual(
            get_effective_role(self.newcomer.id, NOTE_TYPE_CHAT, note.note_id), ROLE_EDITOR
        )
        for row in (note, child, under_it):
            self.assertIsNone(get_effective_role(self.user2.id, NOTE_TYPE_CHAT, row.note_id))

    def test_moving_back_gives_the_author_their_note_again(self):
        note = self._note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_type=NOTE_TYPE_CHAT,
            note_id=note.note_id,
            role_id=ROLE_OWNER,
        )

        task = self._move(self.task)
        self.assertIsNone(get_effective_role(self.user2.id, NOTE_TYPE_CHAT, note.note_id))
        self._move(task, self.project)

        self.assertEqual(
            get_effective_role(self.user2.id, NOTE_TYPE_CHAT, note.note_id), ROLE_OWNER
        )

    def test_a_teardown_reaches_the_record_of_a_chat_already_deleted(self):
        self._move(self.task)
        Message.objects.filter(channel=self.channel).delete()
        Channel.objects.filter(pk=self.channel.pk).delete()

        delete_demo_team_data(self.team.team_id)

        self.assertFalse(MessageMovedAway.objects.exists())

    def test_a_note_on_a_merged_thread_follows_to_the_card_that_stays(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(project_id=self.destination.pk)
        new_card = self._comment(TaskMaster.objects.get(pk=self.task.pk), 2).thread_root
        note = self._note()

        with self.captureOnCommitCallbacks(execute=True):
            task_card_move.move_task_cards([self.task.task_id], project_id=self.destination.pk)

        note.refresh_from_db()
        self.assertEqual((note.channel_id, note.thread_root_id), (self.new_channel.id, new_card.pk))


class OtherMoveTests(MoveBase):
    def test_a_task_with_no_card_moves_and_posts_nothing(self):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="no card",
            status="Open",
            reporter=self.user,
            assignee=self.user,
        )
        TaskMaster.objects.filter(pk=task.pk).update(root_task_id=task.task_id)

        task = self._move(task)

        self.assertEqual(task.project_id, self.destination.pk)
        self.assertFalse(Message.objects.filter(task_id=task.task_id).exists())
        self.push_messages_deleted.assert_not_called()

    def test_a_new_project_with_no_chat_leaves_the_card_where_it_is(self):
        task = self._create()
        card = self._card_of(task)
        Channel.objects.filter(pk=self.new_channel.pk).delete()

        with self.assertLogs(task_card_move.log, "WARNING") as logs:
            task = self._move(task)

        self.assertEqual(task.project_id, self.destination.pk)
        self.assertEqual(Message.objects.get(pk=card.pk).channel_id, self.channel.id)
        self.assertIn("no PM channel", logs.output[0])
        self.push_messages_deleted.assert_not_called()

    def test_a_second_card_already_in_the_new_chat_takes_the_old_thread(self):
        """A task moved before this change, whose next comment made a card in the new chat."""
        task = self._create()
        old_card = self._card_of(task)
        old_reply = self._comment(task, 1, "before")
        # The state the old code left: the task is in the new project, the card is not.
        TaskMaster.objects.filter(pk=task.pk).update(project_id=self.destination.pk)
        task.refresh_from_db()
        new_reply = self._comment(task, 2, "after")
        new_card = new_reply.thread_root

        with self.captureOnCommitCallbacks(execute=True):
            task_card_move.move_task_cards([task.task_id], project_id=self.destination.pk)

        (card,) = Message.objects.filter(task_id=task.task_id, is_thread_reply=False)
        self.assertEqual(card.pk, new_card.pk)
        (channel, gone), _ = self.push_messages_deleted.call_args
        self.assertEqual(
            (channel.id, {m.pk for m in gone}), (self.channel.id, {old_card.pk, old_reply.pk})
        )
        self.assertEqual(card.reply_count, 2)
        self.assertFalse(Message.objects.filter(pk=old_card.pk).exists())
        old_reply.refresh_from_db()
        self.assertEqual(
            (old_reply.channel_id, old_reply.thread_root_id, old_reply.parent_id),
            (self.new_channel.id, card.pk, card.pk),
        )

    def test_a_failure_in_the_card_move_never_costs_the_task_move(self):
        task = self._create()
        with (
            mock.patch.object(task_card_move, "_move_one", side_effect=RuntimeError("boom")),
            self.assertLogs(task_card_move.log, "WARNING"),
        ):
            task = self._move(task)

        self.assertEqual(task.project_id, self.destination.pk)
        self.assertEqual(self._card_of(task).channel_id, self.channel.id)


class SubTaskAndAppMoveTests(MoveBase):
    def test_a_move_in_the_app_takes_the_cards_of_the_sub_tasks_too(self):
        task = self._create()
        child = self._create(title="Child", parent_task_id=task.task_id)
        child_reply = self._comment(child, 1)  # a sub-task's card is made by its first comment
        self.authenticate()

        with self.captureOnCommitCallbacks(execute=True):
            res = self.client.put(
                "/api/v2/task/",
                {"task_id": task.task_id, "project": self.destination.pk},
                format="json",
            )

        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self._card_of(task).channel_id, self.new_channel.id)
        child_reply.refresh_from_db()
        self.assertEqual(child_reply.channel_id, self.new_channel.id)
        self.assertEqual(self._card_of(child).channel_id, self.new_channel.id)
        self.assertFalse(
            Message.objects.filter(
                channel=self.channel, task_id__in=[task.task_id, child.task_id]
            ).exists()
        )

    def test_a_moved_milestone_takes_the_cards_of_its_own_row_and_its_tasks(self):
        flags = {**settings.SEARCH_ENGINE, "AGENT_PARITY_TOOLS": True}
        out = MANAGE_MILESTONE.run(
            {"action": "create", "project_id": self.project.pk, "title": "Launch"}, self.ctx
        )
        milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        task = self._create(milestone_id=milestone.milestone_id)
        backing = TaskMaster.objects.get(task_id=milestone.task_id)
        self._comment(backing, 1)
        args = {
            "action": "update",
            "milestone_id": milestone.milestone_id,
            "project_id": self.destination.pk,
        }

        with mock.patch.object(settings, "SEARCH_ENGINE", flags):
            with self.captureOnCommitCallbacks(execute=True):
                MANAGE_MILESTONE.run({**with_reason(args), **args}, self.ctx)

        for row in (backing, task):
            self.assertEqual(self._card_of(row).channel_id, self.new_channel.id)
        self.assertFalse(Message.objects.filter(channel=self.channel).exists())
        text = self._card_of(backing).body
        self.assertTrue(text[0]["content"][0]["text"].startswith("🚩"))


class MovedCardPinTests(MoveBase):
    """A pin belongs to the chat it was made in (MIP-2487)."""

    def test_moving_the_task_drops_the_cards_pin(self):
        task = self._create(assignee_id=str(self.user.id))
        card = self._card_of(task)
        MessagePin.objects.create(channel=card.channel, message=card, pinned_by=self.user)

        self._move(task)

        self.assertFalse(MessagePin.objects.exists())
