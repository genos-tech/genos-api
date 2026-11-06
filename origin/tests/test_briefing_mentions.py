"""Every @mention that waits on the member shows in their briefing (MIP-1103).

`waiting_on_me.mentions` used to come from `list_my_mentions`, which sees only chat
messages that carry a `MessageMention` row. A mention in a task comment, a task body or a
note writes none: it writes one `Activity(MENTION)` row for the person mentioned, whoever
wrote it and whether it named them or a group they are in (groups are expanded to people
when the text is saved). That row is the source for those, and the briefing reads it, for
what the member can open NOW, and drops it once they have answered.

These tests make each mention through the REAL write path (the chat send, the comment
fan-out, the task-body and note notifiers), not by hand, so they hold what the product
really writes; the edge cases (a deleted task, another team, a note that was removed) are
made by hand on top of those rows.
"""

from __future__ import annotations

import json

from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Message,
)
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import body_mentions
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing
from origin.services.task_comment_fanout import fan_out_task_comment
from origin.tests.test_briefing import BriefingBase
from origin.views.chat import message_views


def mention(user_id):
    return {
        "type": "paragraph",
        "content": [{"type": "mention", "props": {"userId": str(user_id), "userName": "x"}}],
    }


def group(group_id):
    return {
        "type": "paragraph",
        "content": [
            {"type": "mentionGroup", "props": {"groupId": str(group_id), "groupName": "design"}}
        ],
    }


class MentionBase(BriefingBase):
    def setUp(self):
        super().setUp()
        self.design = MentionGroupMaster.objects.create(
            team=self.team, group_name="design", created_by=self.user2
        )
        MentionGroupMembers.objects.create(team=self.team, group=self.design, user=self.user)

    # -- the real write paths --

    def comment_mention(self, task, *, sender=None, body=None, comment_id=None, at=None):
        sender = sender or self.user2
        self._comment_no += 1
        comment = TaskComments.objects.create(
            task=task,
            sender=sender,
            comment_id=comment_id or self._comment_no + 500,
            comment_body=body if body is not None else [mention(self.user.id)],
        )
        fan_out_task_comment(comment)
        if at is not None:
            Activity.objects.filter(
                recipient=self.user, message__metadata__taskCommentId=comment.comment_id
            ).update(ts_created_at=at)
        return comment

    def body_mention(self, task, body, actor=None):
        task.content = body
        task.mentioned_user_ids = []
        task.save()
        body_mentions.notify_task_body_mentions(task, actor_id=str((actor or self.user2).id))

    def note_mention(self, note, kind, actor=None):
        body_mentions.notify_note_mentions(
            note, note_type=kind, actor_id=str((actor or self.user2).id)
        )

    def chat_message(self, channel, body, sender=None):
        return message_views._allocate_seq_and_create_message_with_activities(
            channel=channel,
            sender=sender or self.user2,
            body=body,
            body_text="",
            parent_id=None,
            metadata={},
        )

    def set_time(self, **delta):
        """Move every mention Activity of the member to `now - delta`."""
        Activity.objects.filter(recipient=self.user).update(ts_created_at=self.ago(**delta))

    # -- what the briefing says --

    def mentions(self):
        return self.brief()["waiting_on_me"]["mentions"]

    def sources(self):
        return sorted(m["source"] for m in self.mentions())

    def hidden_task(self, title="Reorg plan"):
        secret = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=secret, attendee=self.user2)
        return self.make_task(title, assignee=self.user2, project=secret)


class TestEachSource(MentionBase):
    def test_a_task_comment_mention_waits_on_me(self):
        task = self.make_task("Fix the login redirect", assignee=self.user2)
        self.comment_mention(task)
        [entry] = self.mentions()
        self.assertEqual(entry["source"], "task_comment")
        self.assertEqual(entry["task_id"], task.task_id)
        self.assertEqual(entry["display_id"], task.display_id)
        self.assertEqual(entry["title"], "Fix the login redirect")
        self.assertEqual(entry["from"], "otheruser")
        self.assertIn("comment_id", entry)
        self.assertEqual(entry["text"], "@x")  # the comment is only the mention chip

    def test_the_comment_text_comes_with_it(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(
            task,
            body=[
                {
                    "type": "paragraph",
                    "content": [
                        {
                            "type": "mention",
                            "props": {"userId": str(self.user.id), "userName": "x"},
                        },
                        {"type": "text", "text": " can you review the redirect?", "styles": {}},
                    ],
                }
            ],
        )
        self.assertEqual(self.mentions()[0]["text"].strip(), "@x can you review the redirect?")

    def test_a_group_mention_in_a_comment_reaches_its_members(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, body=[group(self.design.group_id)])
        self.assertEqual(self.sources(), ["task_comment"])

    def test_a_group_the_member_is_not_in_reaches_them_not(self):
        other = MentionGroupMaster.objects.create(
            team=self.team, group_name="ops", created_by=self.user2
        )
        MentionGroupMembers.objects.create(team=self.team, group=other, user=self.user2)
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, body=[group(other.group_id)])
        self.body_mention(task, [group(other.group_id)])
        self.assertEqual(self.mentions(), [])

    def test_a_task_body_mention_waits_on_me(self):
        task = self.make_task("Spec the export", assignee=self.user2)
        self.body_mention(task, [mention(self.user.id)])
        [entry] = self.mentions()
        self.assertEqual(entry["source"], "task")
        self.assertEqual(entry["display_id"], task.display_id)
        self.assertEqual(entry["title"], "Spec the export")
        self.assertEqual(entry["from"], "otheruser")
        self.assertNotIn("comment_id", entry)

    def test_a_group_mention_in_a_task_body_reaches_its_members(self):
        task = self.make_task(assignee=self.user2)
        self.body_mention(task, [group(self.design.group_id)])
        self.assertEqual(self.sources(), ["task"])

    def test_a_note_mention_waits_on_me_for_each_kind_of_note(self):
        task = self.make_task(assignee=self.user2)
        personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Mine to share", body=[mention(self.user.id)]
        )
        NotePermissionGrant(self, personal)
        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            task=task,
            owner=self.user2,
            title="Spec",
            body=[mention(self.user.id)],
        )
        channel = self.channel([self.user, self.user2], title="Planning")
        chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user2,
            chat_type=2,
            channel=channel,
            is_thread=False,
            title="Meeting notes",
            body=[mention(self.user.id)],
        )
        self.note_mention(personal, 1)
        self.note_mention(task_note, 2)
        self.note_mention(chat_note, 3)
        got = {(m["note_type"], m["title"]) for m in self.mentions()}
        self.assertEqual(
            got, {("personal", "Mine to share"), ("task", "Spec"), ("chat", "Meeting notes")}
        )
        self.assertTrue(all(m["source"] == "note" for m in self.mentions()))

    def test_a_chat_mention_is_still_there_once_and_a_group_in_chat_counts(self):
        channel = self.channel([self.user, self.user2])
        self.chat_message(channel, [mention(self.user.id)])
        self.chat_message(channel, [group(self.design.group_id)])
        entries = self.mentions()
        self.assertEqual([m["source"] for m in entries], ["chat", "chat"])
        self.assertEqual(len({m["message_id"] for m in entries}), 2)

    def test_a_chat_mention_is_not_listed_twice_as_a_comment_mention(self):
        channel = self.channel([self.user, self.user2])
        self.chat_message(channel, [mention(self.user.id)])
        self.assertEqual(self.sources(), ["chat"], "its Activity row is the chat mention itself")

    def test_a_message_that_is_both_a_chat_mention_and_a_comment_mention_is_listed_once(self):
        """Today a comment's thread message carries no `MessageMention`, so the two never
        overlap. If that ever changes, the member must not see the same comment twice."""
        from origin.models.chat.unified_models import MessageMention

        task = self.make_task(assignee=self.user2)
        self.comment_mention(task)
        mirror = Message.objects.get(metadata__taskCommentId=self._comment_no + 500)
        MessageMention.objects.create(message=mirror, mentioned_user=self.user)
        entries = self.mentions()
        self.assertEqual(len(entries), 1)

    def test_the_cap_holds_across_sources(self):
        task = self.make_task(assignee=self.user2)
        channel = self.channel([self.user, self.user2])
        for _ in range(briefing.MAX_LIST + 2):
            self.chat_message(channel, [mention(self.user.id)])
            self.comment_mention(task)
        self.assertEqual(len(self.mentions()), briefing.MAX_LIST)

    def test_every_source_together_is_newest_first(self):
        task = self.make_task(assignee=self.user2)
        channel = self.channel([self.user, self.user2])
        self.chat_message(channel, [mention(self.user.id)])
        self.comment_mention(task)
        self.body_mention(task, [mention(self.user.id)])
        stamps = {"chat": 5, "task_comment": 3, "task": 1}
        for activity in Activity.objects.filter(recipient=self.user):
            if activity.surface_type == 5:
                key = "task"
            elif (activity.message.metadata or {}).get("taskCommentId") is not None:
                key = "task_comment"
            else:
                key = "chat"
            Activity.objects.filter(pk=activity.pk).update(
                ts_created_at=self.ago(hours=stamps[key])
            )
        # the chat message's own time, which the chat entry reports
        Message.objects.filter(channel=channel).update(ts_sent_at=self.ago(hours=5))
        self.assertEqual([m["source"] for m in self.mentions()], ["task", "task_comment", "chat"])


def NotePermissionGrant(test, note):
    """A personal note is private to its owner; share it with the member as the app would."""
    from origin.models.note.common_note_models import NotePermissionMaster

    NotePermissionMaster.objects.create(
        team=test.team, user=test.user, note_id=note.note_id, note_type=1, role_id=2
    )


class TestWhatTheMemberCanOpen(MentionBase):
    def test_a_task_they_cannot_open_is_left_out_whatever_it_says(self):
        task = self.hidden_task("Reorg plan")
        self.comment_mention(task, body=[mention(self.user.id)])
        self.body_mention(task, [mention(self.user.id)])
        out = json.dumps(self.brief())
        self.assertEqual(self.mentions(), [])
        self.assertNotIn("Reorg plan", out)

    def test_a_task_they_are_assigned_is_theirs_even_outside_the_project(self):
        task = self.hidden_task("Mine anyway")
        TaskMaster.objects.filter(pk=task.pk).update(assignee=self.user)
        self.comment_mention(task)
        self.assertEqual(self.sources(), ["task_comment"])

    def test_another_teams_mention_is_left_out(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        task = self.make_task(assignee=self.user2)
        self.body_mention(task, [mention(self.user.id)])
        Activity.objects.filter(recipient=self.user).update(team=other)
        self.assertEqual(self.mentions(), [])

    def test_a_deleted_task_a_deleted_comment_and_a_deleted_message_are_left_out(self):
        task = self.make_task(assignee=self.user2)
        comment = self.comment_mention(task)
        TaskComments.objects.filter(pk=comment.pk).update(is_deleted=True)
        self.assertEqual(self.mentions(), [], "the comment was deleted")
        TaskComments.objects.filter(pk=comment.pk).update(is_deleted=False)
        Message.objects.filter(metadata__taskCommentId=comment.comment_id).update(
            deleted_at=self.now
        )
        self.assertEqual(self.mentions(), [], "its thread message was deleted")
        Message.objects.filter(metadata__taskCommentId=comment.comment_id).update(deleted_at=None)
        self.assertEqual(len(self.mentions()), 1)
        TaskMaster.objects.filter(pk=task.pk).update(is_deleted=True)
        self.assertEqual(self.mentions(), [], "the task was deleted")

    def test_a_note_they_cannot_open_is_left_out(self):
        task = self.make_task(assignee=self.user2)
        private = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user2,
            title="Their private note",
            body=[mention(self.user.id)],
        )
        self.note_mention(private, 1)  # no share: the member has no role on it
        closed = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=closed, attendee=self.user2)
        hidden_task = self.make_task("Hidden", assignee=self.user2, project=closed)
        task_note = TaskNoteMaster.objects.create(
            team=self.team,
            project=closed,
            task=hidden_task,
            owner=self.user2,
            title="Secret spec",
            body=[mention(self.user.id)],
        )
        self.note_mention(task_note, 2)
        elsewhere = self.channel([self.user2], title="Others only")
        chat_note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user2,
            chat_type=2,
            channel=elsewhere,
            is_thread=False,
            title="Secret meeting",
            body=[mention(self.user.id)],
        )
        self.note_mention(chat_note, 3)
        out = json.dumps(self.brief())
        self.assertEqual(self.mentions(), [])
        for secret in ("Their private note", "Secret spec", "Secret meeting"):
            self.assertNotIn(secret, out)
        self.assertIsNotNone(task)

    def test_a_mention_row_for_a_note_they_cannot_open_is_left_out_whoever_filed_it(self):
        """The agent's notifier will not notify someone who can't read a note, but the app's own
        endpoint (`ActivitySurfaceView`) checks only team membership: so a row can exist for a
        note the member has no access to, and the briefing must not name it."""
        private = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Their private note", body=[]
        )
        for surface, note_id in ((6, private.note_id),):
            Activity.objects.create(
                team=self.team,
                recipient=self.user,
                actor=self.user2,
                activity_type=ActivityType.MENTION,
                surface_type=surface,
                meta={"noteId": note_id, "noteType": 1},
            )
        self.assertEqual(self.mentions(), [])
        self.assertNotIn("Their private note", json.dumps(self.brief()))
        NotePermissionGrant(self, private)
        self.assertEqual(self.sources(), ["note"], "and it shows once the note is shared with them")

    def test_a_note_in_another_team_or_that_was_removed_is_left_out(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=[mention(self.user.id)]
        )
        self.note_mention(note, 1)
        self.assertEqual(self.sources(), ["note"])
        PersonalNoteMaster.objects.filter(pk=note.pk).update(team=other)
        self.assertEqual(self.mentions(), [], "the note is in another team")
        PersonalNoteMaster.objects.filter(pk=note.pk).delete()
        self.assertEqual(self.mentions(), [], "the note is gone")

    def test_a_note_of_another_team_is_left_out_even_when_the_row_says_this_team(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        note = PersonalNoteMaster.objects.create(
            team=other, owner=self.user, title="Elsewhere", body=[]
        )
        Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            surface_type=6,
            meta={"noteId": note.note_id, "noteType": 1},
        )
        self.assertEqual(self.mentions(), [])
        self.assertNotIn("Elsewhere", json.dumps(self.brief()))

    def test_someone_elses_mentions_are_not_mine(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, body=[mention(self.user2.id)])
        self.body_mention(task, [mention(self.user2.id)], actor=self.user)
        self.assertEqual(self.mentions(), [])

    def test_only_mentions_count_not_other_activity(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task)
        Activity.objects.filter(recipient=self.user).update(activity_type=ActivityType.THREAD_REPLY)
        self.assertEqual(self.mentions(), [])


class TestAnswered(MentionBase):
    def test_commenting_on_the_task_afterwards_answers_a_comment_mention(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, at=self.ago(hours=5))
        self.assertEqual(len(self.mentions()), 1)
        self.comment(task, self.user, "On it", at=self.ago(hours=2))
        self.assertEqual(self.mentions(), [])

    def test_a_comment_from_before_the_mention_does_not_answer_it(self):
        task = self.make_task(assignee=self.user2)
        self.comment(task, self.user, "earlier", at=self.ago(hours=9))
        self.comment_mention(task, at=self.ago(hours=5))
        self.assertEqual(len(self.mentions()), 1)

    def test_changing_the_task_afterwards_answers_a_task_body_mention(self):
        task = self.make_task(assignee=self.user2)
        self.body_mention(task, [mention(self.user.id)])
        self.set_time(hours=5)
        self.assertEqual(len(self.mentions()), 1)
        self.activity(task, self.user, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        self.assertEqual(self.mentions(), [])

    def test_a_change_by_someone_else_does_not_answer_it(self):
        task = self.make_task(assignee=self.user2)
        self.body_mention(task, [mention(self.user.id)])
        self.set_time(hours=5)
        self.activity(task, self.user2, "status_changed", "Open", "WIP", at=self.ago(hours=2))
        self.comment(task, self.user2, "ping", at=self.ago(hours=2))
        self.assertEqual(len(self.mentions()), 1)

    def test_editing_the_note_afterwards_answers_a_note_mention(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=[mention(self.user.id)]
        )
        self.note_mention(note, 1)
        self.set_time(hours=5)
        self.assertEqual(len(self.mentions()), 1)
        early = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=1,
            editor=self.user,
            title="Mine",
        )
        NoteVersionMaster.objects.filter(pk=early.pk).update(ts_updated_at=self.ago(hours=8))
        self.assertEqual(
            len(self.mentions()), 1, "an edit from before the mention does not answer it"
        )
        late = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=2,
            editor=self.user,
            title="Mine",
        )
        NoteVersionMaster.objects.filter(pk=late.pk).update(ts_updated_at=self.ago(hours=2))
        self.assertEqual(self.mentions(), [])

    def test_someone_elses_edit_does_not_answer_a_note_mention(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=[mention(self.user.id)]
        )
        self.note_mention(note, 1)
        self.set_time(hours=5)
        v = NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=1,
            editor=self.user2,
            title="Mine",
        )
        NoteVersionMaster.objects.filter(pk=v.pk).update(ts_updated_at=self.ago(hours=2))
        self.assertEqual(len(self.mentions()), 1)

    def test_opening_the_feed_entry_answers_a_note_mention(self):
        """A reply in a note's own comment thread can't be seen from here, so for a note the
        entry having been opened counts."""
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Mine", body=[mention(self.user.id)]
        )
        self.note_mention(note, 1)
        self.assertEqual(len(self.mentions()), 1)
        Activity.objects.filter(recipient=self.user).update(is_read=True)
        self.assertEqual(self.mentions(), [])

    def test_a_read_comment_mention_still_waits_until_they_reply(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task)
        Activity.objects.filter(recipient=self.user).update(is_read=True)
        self.assertEqual(len(self.mentions()), 1, "seen is not answered")

    def test_a_mention_older_than_a_week_is_let_go(self):
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, at=self.ago(days=8))
        self.assertEqual(self.mentions(), [])
        self.set_time(days=6)
        self.assertEqual(len(self.mentions()), 1)

    def test_tagging_yourself_is_a_reminder_that_waits(self):
        task = self.make_task(assignee=self.user)
        self.body_mention(task, [mention(self.user.id)], actor=self.user)
        [entry] = self.mentions()
        self.assertEqual(entry["from"], "testuser")


class TestTheOutput(MentionBase):
    def test_people_are_named_once_and_a_stranger_stays_someone(self):
        from django.contrib.auth import get_user_model

        stranger = get_user_model().objects.create_user(
            username="stranger", email="s@example.com", password="x"
        )
        task = self.make_task(assignee=self.user2)
        self.comment_mention(task, sender=self.user2)
        self.body_mention(task, [mention(self.user.id)], actor=stranger)
        by = {m["source"]: m["from"] for m in self.mentions()}
        self.assertEqual(by, {"task_comment": "otheruser", "task": "someone"})
        self.assertNotIn("stranger", json.dumps(self.brief()))

    def test_it_is_capped_and_newest_first(self):
        task = self.make_task(assignee=self.user2)
        for n in range(briefing.MAX_LIST + 6):
            self.comment_mention(task, at=self.ago(minutes=n + 1))
        entries = self.mentions()
        self.assertEqual(len(entries), briefing.MAX_LIST)
        stamps = [e["at"] for e in entries]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_the_number_of_queries_does_not_grow_with_the_number_of_mentions(self):
        task = self.make_task(assignee=self.user2)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

        def count():
            names = briefing._Names(ctx)
            with CaptureQueriesContext(connection) as captured:
                briefing._activity_mentions(ctx, self.now, names, set())
            return len(captured)

        for n in range(3):
            self.comment_mention(task, at=self.ago(minutes=n + 1))
        few = count()
        for n in range(30):
            self.comment_mention(task, at=self.ago(minutes=n + 10))
        self.assertEqual(count(), few, "the same queries for 33 mentions as for 3")

    def test_the_catch_me_up_prompt_still_names_only_real_fields(self):
        from origin.views.public.mcp import prompts

        text = prompts.get_prompt("catch_me_up", {}, frozenset({"get_my_briefing"}))["messages"][0][
            "content"
        ]["text"]
        self.assertIn("`waiting_on_me`", text)
        self.assertIn("waiting_on_me", self.brief())

    def test_it_stays_small_with_a_lot_of_mentions(self):
        """The briefing's whole budget (about 8k tokens, 32,000 characters) holds when the
        member is mentioned a lot, in every way."""
        today = self.now.astimezone(briefing.zone_for_user_id(self.user.id)).date()
        for n in range(40):
            task = self.make_task(
                f"Task number {n} with a reasonably long descriptive title",
                assignee=self.user2,
                due_date=today,
            )
            self.comment_mention(
                task,
                body=[
                    {
                        "type": "paragraph",
                        "content": [
                            {
                                "type": "mention",
                                "props": {"userId": str(self.user.id), "userName": "x"},
                            },
                            {"type": "text", "text": " please look at this " * 30, "styles": {}},
                        ],
                    }
                ],
                at=self.ago(minutes=n + 1),
            )
            self.body_mention(task, [mention(self.user.id)])
        size = len(json.dumps(self.brief()))
        self.assertLess(size, 32_000, f"the briefing is {size} characters")
        self.assertLessEqual(len(self.mentions()), briefing.MAX_LIST)
