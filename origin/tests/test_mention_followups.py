"""Follow-ups of MIP-1593 (MIP-1598): the @mention card of a task NOTE, and the comment-mention
row of someone who can open the task but is not in its project chat.

1. A mention in a task NOTE (surface 7) is filed with the note's title, the task's display id and
   a link naming the note, and was rendered from the stored row whoever read it. The rule for "may
   open it" is the NOTE's (`note_role.get_effective_role`: an explicit grant or a project member
   row), not the task's, so the task's assignee who is not on the project, or the project's owner
   with no member row, can open the task and still gets the minimal card. A reader gets the full
   one. The stored row is untouched and judged when it is read, as in #884.

2. The feed judged a comment-mention row by the project CHAT's rule (`chat_member_q`: a project
   member row), which is narrower than the task's (`can_access_task`: member, owner, assignee,
   reporter). The mention is about the task, so the row follows the task's rule, through the same
   `task_reader_pairs` the card uses. Nothing else of the chat opens: the channel, its messages
   and every row that is not a comment-mention stay as they were.
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import Activity, Message
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing, unread_digest, v3_activity
from origin.services.activity_minimal_card import (
    minimal_card_activity_ids,
    task_reader_pairs,
)
from origin.tests.test_nonmember_mention_card import (
    COMMENT_TEXT,
    FEED,
    SURFACE,
    Cards,
)
from origin.views.utils.note_role import (
    NOTE_TYPE_TASK,
    get_effective_role,
    readable_note_ids,
    task_note_reader_pairs,
)
from origin.views.utils.scope_guards import can_access_task

User = get_user_model()

NOTE_TITLE = "Quarterly layoffs plan"
MINIMAL_NOTE_URL = "/workspace/notes"


class NoteCards(Cards):
    """`self.note` hangs off `self.task`. Readers of it: `self.user` and `self.colleague` (project
    members) and `self.grantee` (an explicit grant, not on the project). Not readers:
    `self.member`, `self.user2`, and `self.assignee` (the assignee of `self.held`, so able to open
    that task but not its note). `self.boss` owns a second project with no member row of theirs:
    they can open its tasks (`can_access_task`) but not its notes."""

    def setUp(self):
        super().setUp()
        self.grantee = User.objects.create_user(
            username="grantee", email="grantee@m1598.test", password="pass12345"
        )
        self.boss = User.objects.create_user(
            username="boss", email="boss@m1598.test", password="pass12345"
        )
        for u in (self.grantee, self.boss):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.note = self.mk_note(self.project, self.task)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.grantee,
            note_type=NOTE_TYPE_TASK,
            note_id=self.note.note_id,
            role_id=3,
        )
        self.held_note = self.mk_note(self.project, self.held)
        self.boss_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Boss project",
            code="BOS",
            owner=self.boss,
            project_system_user=self.boss,
        )
        ProjectMembers.objects.create(team=self.team, project=self.boss_project, attendee=self.user)
        self.boss_task = TaskMaster.objects.create(
            team=self.team,
            project=self.boss_project,
            reporter=self.user,
            assignee=self.user,
            title="Boss task",
            status="WIP",
        )
        self.boss_note = self.mk_note(self.boss_project, self.boss_task)
        self.readers = (self.colleague, self.grantee)
        self.non_readers = (self.member, self.user2)

    def mk_note(self, project, task):
        return TaskNoteMaster.objects.create(
            team=self.team, project=project, owner=self.user, task=task, title=NOTE_TITLE
        )

    def note_meta(self, note):
        """What genos-sockets files for a mention in a task note, plus the display id the writer
        backfills."""
        return {
            "firstLineContent": f"Mentioned in the task note: {note.title}",
            "tsSent": "2026-11-15T00:00:00Z",
            "noteId": note.note_id,
            "noteType": 2,
            "noteTitle": note.title,
            "taskId": note.task_id,
            "projectId": note.project_id,
        }

    def note_mention(self, note, users, *, is_comment=False):
        meta = self.note_meta(note)
        if is_comment:
            meta["isComment"] = True

        def call():
            return self.client.post(
                SURFACE,
                {
                    "surface_type": 7,
                    "team_id": self.team_id,
                    "entity_key": f"note:2:{note.note_id}",
                    "newly_mentioned_user_ids": [str(u.id) for u in users],
                    "meta": meta,
                },
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        wire = {a["recipientUserId"]: a for a in res.json()["activities"]}
        return wire, pushes, emails

    def note_leaks(self, value, note=None):
        """Which details of the note or its task appear anywhere in `value`."""
        note = note or self.note
        text = json.dumps(value, default=str)
        needles = {
            "note title": NOTE_TITLE,
            "display id": note.task.display_id,
            "task title": note.task.title,
            "note link": f"/note/{note.note_id}",
            "task link": f"/task/{note.task_id}",
            "project name": note.project.project_name,
        }
        return sorted(k for k, v in needles.items() if v in text)


class TaskNoteMentionTests(NoteCards):
    def test_someone_who_cannot_open_the_note_gets_the_minimal_card_everywhere(self):
        wire, pushes, emails = self.note_mention(self.note, self.non_readers)

        for stranger in self.non_readers:
            sid = str(stranger.id)
            self.assertEqual(self.note_leaks(wire[sid]), [], wire[sid])
            self.assertEqual(wire[sid]["surfaceType"], 7)
            self.assertTrue(wire[sid]["meta"]["noAccess"])
            for gone in ("noteTitle", "noteId", "taskId", "projectId", "displayId"):
                self.assertNotIn(gone, wire[sid]["meta"])
            self.assertEqual(
                pushes[sid]["title"], f"{self.user.username} mentioned you in a note you can't open"
            )
            self.assertEqual(pushes[sid]["body"], "")
            self.assertEqual(pushes[sid]["url"], MINIMAL_NOTE_URL)
            self.assertEqual(pushes[sid]["category"], "mention_task")
            (mail,) = emails[sid]
            self.assertEqual(
                (mail.title, mail.body, mail.url), (pushes[sid]["title"], "", MINIMAL_NOTE_URL)
            )
            self.assertEqual(self.note_leaks([mail.title, mail.body, mail.url]), [])

    def test_a_reader_gets_the_full_card_a_project_member_and_a_grantee_alike(self):
        wire, pushes, emails = self.note_mention(self.note, self.readers)

        for reader in self.readers:
            rid = str(reader.id)
            self.assertNotIn("noAccess", wire[rid]["meta"])
            self.assertEqual(wire[rid]["meta"]["noteTitle"], NOTE_TITLE)
            self.assertEqual(wire[rid]["meta"]["displayId"], self.task.display_id)
            self.assertEqual(pushes[rid]["title"], f"{self.user.username} mentioned you in a task")
            self.assertEqual(pushes[rid]["body"], self.task.display_id)
            self.assertIn(f"/note/{self.note.note_id}", pushes[rid]["url"])
            self.assertEqual(emails[rid][0].url, pushes[rid]["url"])

    def test_the_note_rule_is_not_the_task_rule(self):
        """The task's assignee, and the owner of the project, can open the TASK but not the note:
        they get the minimal card of the note."""
        for note, holder in ((self.held_note, self.assignee), (self.boss_note, self.boss)):
            self.assertTrue(can_access_task(note.task_id, holder.id))
            self.assertIsNone(get_effective_role(holder.id, NOTE_TYPE_TASK, note.note_id))

            wire, pushes, emails = self.note_mention(note, [holder])

            hid = str(holder.id)
            self.assertEqual(self.note_leaks(wire[hid], note), [], wire[hid])
            self.assertTrue(wire[hid]["meta"]["noAccess"])
            self.assertEqual(pushes[hid]["url"], MINIMAL_NOTE_URL)
            self.assertEqual(self.note_leaks(pushes[hid], note), [])
            self.assertEqual(self.note_leaks(emails[hid][0].url, note), [])

    def test_an_inline_comment_mention_in_the_note_is_judged_the_same_way(self):
        wire, pushes, _ = self.note_mention(
            self.note, [self.member, self.colleague], is_comment=True
        )

        mid, cid = str(self.member.id), str(self.colleague.id)
        self.assertEqual(self.note_leaks(wire[mid]), [])
        self.assertTrue(wire[mid]["meta"]["isComment"])
        self.assertEqual(
            pushes[mid]["title"], f"{self.user.username} mentioned you in a note you can't open"
        )
        self.assertEqual(pushes[cid]["title"], f"{self.user.username} mentioned you in a comment")

    def test_the_notification_the_row_and_the_recipients_are_kept(self):
        wire, pushes, emails = self.note_mention(self.note, self.non_readers + self.readers)

        everyone = {str(u.id) for u in self.non_readers + self.readers}
        self.assertEqual(set(wire), everyone)
        self.assertEqual(set(pushes), everyone)
        self.assertEqual(set(emails), everyone)
        stored = {str(a.recipient_id): a for a in Activity.objects.filter(surface_type=7)}
        self.assertEqual(set(stored), everyone)
        for stranger in self.non_readers:
            meta = stored[str(stranger.id)].meta
            self.assertEqual(meta["noteTitle"], NOTE_TITLE)
            self.assertEqual(meta["displayId"], self.task.display_id)
            self.assertNotIn("noAccess", meta)

    def test_the_card_is_decided_when_it_is_rendered_not_when_it_is_stored(self):
        """Someone given the note later reads the same stored row in full."""
        self.note_mention(self.note, [self.member])
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.member)
        self.authenticate(self.member)

        (row,) = self.client.get(FEED, {"team_id": self.team_id}).json()["activities"]

        self.assertEqual(row["meta"]["noteTitle"], NOTE_TITLE)
        self.assertNotIn("noAccess", row["meta"])

    def test_the_agents_path_goes_through_the_same_fan_out(self):
        from origin.search_engine.agent.tools.body_mentions import notify_note_mentions

        self.note.body = [
            {
                "type": "paragraph",
                "content": [
                    {"type": "mention", "props": {"userId": str(u.id), "userName": "x"}}
                    for u in (self.member, self.colleague)
                ],
            }
        ]
        self.note.save()

        _, pushes, emails = self.fan_out(
            lambda: notify_note_mentions(self.note, note_type=2, actor_id=str(self.user.id))
        )

        mid, cid = str(self.member.id), str(self.colleague.id)
        self.assertEqual(self.note_leaks(pushes[mid]), [])
        self.assertEqual(pushes[mid]["url"], MINIMAL_NOTE_URL)
        self.assertEqual(self.note_leaks([emails[mid][0].title, emails[mid][0].url]), [])
        self.assertEqual(pushes[cid]["body"], self.task.display_id)


class WhatElseANonReaderCanSee(NoteCards):
    """The stored row keeps the note's title and the task's display id, so every place that reads
    it back must hold it to the note's rule: the feed, `list_my_unread` and the briefing."""

    def setUp(self):
        super().setUp()
        self.note_mention(self.note, [self.member, self.colleague])
        self.authenticate(self.member)

    def test_the_feed_gives_no_row_and_lists_it_as_lost(self):
        res = self.client.get(FEED, {"team_id": self.team_id}).json()

        self.assertEqual(res["activities"], [])
        self.assertEqual(len(res["tombstones"]), 1)

    def test_unread_and_the_briefing_say_nothing_of_the_note(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.member.id))

        unread = unread_digest.build_unread(ctx)
        brief = briefing.build_briefing(ctx, now=timezone.now() + timedelta(seconds=1))

        self.assertEqual(unread["items"], [])
        for result in (unread, brief):
            self.assertEqual(self.note_leaks(result), [])

    def test_a_reader_is_told_in_full(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.colleague.id))

        unread = unread_digest.build_unread(ctx)

        (item,) = unread["items"]
        self.assertIn(NOTE_TITLE, json.dumps(item))

    def test_the_daily_digest_line_is_the_minimal_one(self):
        from origin.management.commands.email_digest import Command

        now = timezone.now() + timedelta(seconds=1)

        _, stranger = Command()._collect(str(self.member.id), None, now)
        _, reader = Command()._collect(str(self.colleague.id), None, now)

        self.assertEqual(
            [e["title"] for e in stranger],
            [f"{self.user.username} mentioned you in a note you can't open"],
        )
        self.assertTrue(stranger[0]["url"].endswith(MINIMAL_NOTE_URL))
        self.assertEqual(self.note_leaks(stranger), [])
        self.assertIn(f"/note/{self.note.note_id}", reader[0]["url"])


class FailClosedAndCost(NoteCards):
    def rows(self, users, key):
        return v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=7,
            entity_key=f"note:2:{self.note.note_id}:{key}",
            newly_mentioned_user_ids=[str(u.id) for u in users],
            meta=self.note_meta(self.note),
        )

    def test_a_failing_check_shows_the_minimal_card_not_the_full_one(self):
        with mock.patch(
            "origin.services.activity_minimal_card.minimal_card_activity_ids",
            side_effect=RuntimeError("db down"),
        ):
            wire, pushes, _ = self.note_mention(self.note, [self.member, self.colleague])

        for who in (self.member, self.colleague):
            self.assertEqual(self.note_leaks(wire[str(who.id)]), [])
            self.assertEqual(pushes[str(who.id)]["url"], MINIMAL_NOTE_URL)

    def test_a_row_naming_no_note_is_a_strangers(self):
        row = v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=7,
            entity_key="note:2:broken",
            newly_mentioned_user_ids=[str(self.colleague.id)],
            meta={"firstLineContent": "x", "noteId": "not a number"},
        )

        self.assertEqual(minimal_card_activity_ids(row), {r.id for r in row})

    def test_the_check_costs_the_same_for_one_recipient_as_for_many(self):
        extra = []
        for i in range(6):
            u = User.objects.create_user(
                username=f"x{i}", email=f"x{i}@m1598.test", password="pass12345"
            )
            TeamMembers.objects.create(team=self.team, attendee=u)
            extra.append(u)
        one = self.rows([self.member], "one")
        many = self.rows(extra + [self.user2, self.colleague], "many")

        with CaptureQueriesContext(connection) as small:
            got_one = minimal_card_activity_ids(one)
        with CaptureQueriesContext(connection) as large:
            got_many = minimal_card_activity_ids(many)

        self.assertEqual(got_one, {a.id for a in one})
        self.assertEqual(
            got_many, {a.id for a in many if str(a.recipient_id) != str(self.colleague.id)}
        )
        self.assertEqual(len(small), len(large))
        self.assertLessEqual(len(large), 3)

    def test_a_task_mention_and_a_note_mention_together_stay_bounded(self):
        rows = self.rows(
            [self.member, self.colleague], "mixed"
        ) + v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=5,
            entity_key=f"task:{self.task.task_id}",
            newly_mentioned_user_ids=[str(self.member.id), str(self.colleague.id)],
            meta=self.meta(self.task),
        )

        with CaptureQueriesContext(connection) as ctx:
            got = minimal_card_activity_ids(rows)

        self.assertEqual(len(got), 2)
        self.assertLessEqual(len(ctx), 6)

    def test_a_row_of_a_surface_this_does_not_cover_is_not_judged_here(self):
        # Personal and chat notes (6, 8) were left out here by #889 and are covered since MIP-1599
        # (`test_mention_leftovers`); what stays out is any other surface, such as the AI-app notice.
        row = v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=10,
            entity_key="note:1:1",
            newly_mentioned_user_ids=[str(self.user2.id)],
            meta={"noteId": 1, "noteType": 1},
        )

        with CaptureQueriesContext(connection) as ctx:
            got = minimal_card_activity_ids(row)

        self.assertEqual(got, set())
        self.assertEqual(len(ctx), 0)


class NoteReaderPairsAreTheNoteRule(NoteCards):
    """`task_note_reader_pairs` is `get_effective_role(...) is not None` for many people and notes
    at once: pinned over every kind of person, so the two cannot drift."""

    def test_it_equals_the_per_person_rule_and_the_batch_for_one_person(self):
        gone = User.objects.create_user(
            username="gone", email="gone@m1598.test", password="pass12345"
        )
        people = [
            self.user,
            self.colleague,
            self.grantee,
            self.member,
            self.user2,
            self.assignee,
            self.boss,
            gone,
        ]
        notes = [self.note, self.held_note, self.boss_note]
        ids = [n.note_id for n in notes] + [999_999]

        got = task_note_reader_pairs([p.id for p in people], ids)

        for person in people:
            for note_id in ids:
                want = get_effective_role(person.id, NOTE_TYPE_TASK, note_id) is not None
                self.assertEqual((str(person.id), note_id) in got, want, (person.username, note_id))
            self.assertEqual(
                {n for p, n in got if p == str(person.id)},
                readable_note_ids(person.id, NOTE_TYPE_TASK, ids),
            )

    def test_it_is_three_queries_however_many_people_and_notes(self):
        with CaptureQueriesContext(connection) as ctx:
            task_note_reader_pairs(
                [self.user.id, self.member.id, self.grantee.id, self.boss.id],
                [self.note.note_id, self.held_note.note_id, self.boss_note.note_id],
            )

        self.assertLessEqual(len(ctx), 3)

    def test_nobody_and_nothing_cost_nothing(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(task_note_reader_pairs([], [self.note.note_id]), set())
            self.assertEqual(task_note_reader_pairs([self.user.id], []), set())
        self.assertEqual(len(ctx), 0)


# -- item 2 ----------------------------------------------------------------------------------- #


class CommentMentionOfAHolder(Cards):
    """`self.assignee` is the assignee of `self.held` and is not on the project: they can open
    the task, not its project chat. `self.member` is on the team and can open neither."""

    def setUp(self):
        super().setUp()
        self.authenticate(self.user)
        self.report, self.wire, self.pushes, self.emails = self.comment_mention(
            self.held, [self.assignee, self.member]
        )
        self.mirror = Message.objects.get(thread_root__isnull=False, task_id=self.held.task_id)
        self.aid, self.mid = str(self.assignee.id), str(self.member.id)

    def feed(self, user):
        self.authenticate(user)
        res = self.client.get(FEED, {"team_id": self.team_id})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_the_feed_row_follows_the_task_rule(self):
        mine = self.feed(self.assignee)
        theirs = self.feed(self.member)

        (row,) = mine["activities"]
        self.assertEqual(row["messageId"], str(self.mirror.id))
        self.assertIn(COMMENT_TEXT, row["message"]["bodyText"])
        self.assertEqual(row["message"]["taskId"], self.held.task_id)
        self.assertEqual(mine["tombstones"], [])
        # Someone who cannot open the task keeps the row out and listed as lost.
        self.assertEqual(theirs["activities"], [])
        self.assertEqual(len(theirs["tombstones"]), 1)

    def test_the_feed_the_push_and_unread_agree(self):
        mine = self.feed(self.assignee)
        ctx = ToolContext(team_id=self.team_id, user_id=self.aid)

        unread = unread_digest.build_unread(ctx)

        self.assertEqual(len(mine["activities"]), 1)
        self.assertEqual(self.pushes[self.aid]["title"], f"{self.user.username} mentioned you")
        (item,) = unread["items"]
        self.assertEqual(item["task_id"], self.held.task_id)

    def test_the_push_and_the_email_link_to_the_task_not_to_a_chat_they_cannot_open(self):
        url = f"/workspace/tasks/project/{self.project.project_id}/task/{self.held.task_id}"

        self.assertEqual(self.pushes[self.aid]["url"], url)
        self.assertEqual(self.emails[self.aid][0].url, url)
        self.assertIn(COMMENT_TEXT, self.pushes[self.aid]["body"])
        self.assertEqual(self.pushes[self.aid]["category"], "mention_chat")

    def test_a_project_member_keeps_the_link_to_the_chat(self):
        _, _, pushes, _ = self.comment_mention(self.task, [self.colleague])

        self.assertIn("/workspace/chat/pm/", pushes[str(self.colleague.id)]["url"])

    def test_the_chat_itself_stays_closed_to_them(self):
        self.authenticate(self.assignee)

        chat = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/")
        one = self.client.get(f"/api/v3/messages/{self.mirror.id}/")

        self.assertIn(chat.status_code, (403, 404))
        self.assertIn(one.status_code, (403, 404))
        for res in (chat, one):
            self.assertNotIn(COMMENT_TEXT, res.content.decode())

    def test_the_row_carries_the_comment_and_not_the_task_card_message(self):
        (row,) = self.feed(self.assignee)["activities"]

        text = json.dumps(row, default=str)
        self.assertIn(COMMENT_TEXT, text)
        # The task-card message the comment hangs under is only named by its opaque id.
        card = Message.objects.get(
            channel=self.channel, task_id=self.held.task_id, thread_root__isnull=True
        )
        self.assertNotIn(card.body_text, text.replace(str(card.id), ""))

    def test_a_plain_chat_mention_in_the_project_chat_stays_out(self):
        """A mention in the project chat that is not a task comment is the chat's content: the
        task rule does not reach it, however the person relates to a task."""
        plain = Message.objects.create(
            channel=self.channel, sender=self.user, seq=50, body=[], body_text="hello @x"
        )
        about_task = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=51,
            body=[],
            body_text="a reply on the card",
            task_id=self.held.task_id,
        )
        for message in (plain, about_task):
            v3_activity.create_mention_activities(
                message=message,
                mentioned_user_ids=[self.aid],
                actor=self.user,
                skip_actor=False,
            )

        got = self.feed(self.assignee)

        self.assertEqual({a["messageId"] for a in got["activities"]}, {str(self.mirror.id)})
        self.assertEqual(len(got["tombstones"]), 2)

    def test_it_is_judged_when_the_feed_is_read(self):
        TaskMaster.objects.filter(task_id=self.held.task_id).update(assignee=self.user)

        got = self.feed(self.assignee)

        self.assertEqual(got["activities"], [])
        self.assertEqual(len(got["tombstones"]), 1)

    def test_a_deleted_task_gives_no_row(self):
        TaskMaster.objects.filter(task_id=self.held.task_id).update(is_deleted=True)

        got = self.feed(self.assignee)

        self.assertEqual(got["activities"], [])
        self.assertEqual(len(got["tombstones"]), 1)

    def test_a_chat_rule_row_is_unchanged_for_a_project_member(self):
        self.comment_mention(self.task, [self.colleague])

        got = self.feed(self.colleague)

        self.assertEqual(len(got["activities"]), 1)
        self.assertEqual(got["tombstones"], [])

    def test_the_senders_report_is_unchanged(self):
        self.assertEqual(
            {u["userId"] for u in self.report["nonMemberMentions"]["users"]},
            {self.aid, self.mid},
        )

    def test_the_feed_costs_the_same_for_one_such_row_as_for_many(self):
        def feed_queries():
            self.authenticate(self.assignee)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(FEED, {"team_id": self.team_id}).status_code, 200)
            return len(ctx)

        before = feed_queries()
        self.authenticate(self.user)
        for _ in range(4):
            self.comment_mention(self.held, [self.assignee])

        self.assertEqual(len(self.feed(self.assignee)["activities"]), 5)
        self.assertEqual(feed_queries(), before)


class TaskReaderPairsAreCanAccessTask(Cards):
    """The feed and the card ask `task_reader_pairs`; this pins it equal to `can_access_task` over
    every kind of person, so a change to either (MIP-1592 narrows `can_access_task` for someone
    who left the team) cannot go unnoticed here."""

    def test_it_equals_can_access_task_for_every_kind_of_person(self):
        left = User.objects.create_user(
            username="left", email="left@m1598.test", password="pass12345"
        )
        TaskMaster.objects.filter(task_id=self.held.task_id).update(reporter=left)
        deleted = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Gone",
            status="WIP",
            is_deleted=True,
        )
        people = [self.user, self.colleague, self.assignee, self.member, self.user2, left]
        tasks = [self.task, self.held, deleted]

        got = task_reader_pairs([p.id for p in people], [t.task_id for t in tasks] + [999_999])

        for person in people:
            for task in tasks:
                self.assertEqual(
                    (str(person.id), task.task_id) in got,
                    can_access_task(task.task_id, person.id),
                    (person.username, task.title),
                )
        self.assertEqual({t for _, t in got if t == 999_999}, set())
