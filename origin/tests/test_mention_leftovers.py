"""Leftovers of MIP-1598 (MIP-1599): the @mention card of a PERSONAL and a CHAT note, and the
"X commented" row of someone who can open the task but is not in its project chat.

1. A mention in a personal note (surface 6) or a chat note (surface 8) is filed with the note's
   title (`meta.noteTitle`, `firstLineContent`), its id, and for a chat note its chat's routing
   ids, and was rendered from the stored row whoever read it: the live `activity.created`
   payload, the web push, the notification email and the daily digest. The feed, `list_my_unread`
   and the briefing already hold such a row to the note's rule. Now those four show someone who
   cannot open the note the minimal card (#889 did it for a task note):

     * a personal note is private to its owner and its explicit grantees, plus the readers of the
       team folder it is filed in (`get_effective_role`; the owner is its whole ACL, as in the
       feed's gate);
     * a chat note follows its chat (`chat_note_member_q`: a live chat of the kind the note was
       filed under, a member of it by the chat rule, a project chat by the PROJECT's people) and
       its explicit grants. There is no author exception (#766).

   The stored row is untouched and judged when it is read. The pairs are asked in batch.

2. The participant "X commented" row (THREAD_REPLY on the comment mirror) of an assignee or
   reporter who is not on the project was tombstoned by the feed (the project CHAT's rule) and
   pushed with a link into a chat that answers 404. It follows the task's rule now, like the
   comment-mention row (#889).
"""

from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services import briefing, unread_digest, v3_activity
from origin.services.activity_minimal_card import minimal_card_activity_ids
from origin.services.activity_surface_access import unreadable_surface_activity_ids
from origin.tests.test_nonmember_mention_card import (
    COMMENT,
    COMMENT_TEXT,
    FEED,
    SURFACE,
    Cards,
    comment_body,
)
from origin.views.utils.note_role import (
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    get_effective_role,
    note_reader_pairs,
)

User = get_user_model()

PERSONAL_TITLE = "Notes on firing the vendor"
CHAT_TITLE = "Notes from the reorg thread"
MINIMAL_NOTE_URL = "/workspace/notes"
PERSONAL, CHAT = 6, 8


class PrivateNotes(Cards):
    """Two notes, both written by `self.user`.

    `self.personal` is theirs and shared with `self.grantee` only. `self.chat_note` hangs off a
    group chat whose members are `self.user` and `self.colleague`; `self.leaver` was in it and
    left. Not readers of either: `self.member`, `self.user2` (and `self.colleague` of the
    personal one, `self.grantee` of the chat one)."""

    def setUp(self):
        super().setUp()
        self.grantee = User.objects.create_user(
            username="grantee", email="grantee@m1599.test", password="pass12345"
        )
        self.leaver = User.objects.create_user(
            username="leaver", email="leaver@m1599.test", password="pass12345"
        )
        for u in (self.grantee, self.leaver):
            TeamMembers.objects.create(team=self.team, attendee=u)
        self.personal = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title=PERSONAL_TITLE
        )
        self.grant(self.personal, 1, self.user, role=1)
        self.grant(self.personal, 1, self.grantee, role=3)
        self.chat = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="reorg")
        for u in (self.user, self.colleague):
            ChannelMember.objects.create(channel=self.chat, user=u)
        ChannelMember.objects.create(channel=self.chat, user=self.leaver, is_deleted=True)
        self.chat_note = self.chat_note_on(self.chat)

    def grant(self, note, note_type, user, role=3):
        return NotePermissionMaster.objects.create(
            team=self.team, user=user, note_type=note_type, note_id=note.note_id, role_id=role
        )

    def chat_note_on(self, channel, **kw):
        return ChatNoteMaster.objects.create(
            team=self.team,
            owner=kw.pop("owner", self.user),
            chat_type=kw.pop("chat_type", channel.kind),
            channel=channel,
            is_thread=False,
            title=CHAT_TITLE,
            body=[],
        )

    # -- what genos-sockets files (utils/activity_emitter.py) --

    def personal_meta(self, note=None):
        note = note or self.personal
        return {
            "firstLineContent": f"Mentioned in the note: {note.title}",
            "tsSent": "2026-11-15T00:00:00Z",
            "noteId": note.note_id,
            "noteType": 1,
            "noteTitle": note.title,
        }

    def chat_meta(self, note=None):
        note = note or self.chat_note
        return {
            "firstLineContent": f"Mentioned in the note: {note.title}",
            "tsSent": "2026-11-15T00:00:00Z",
            "noteId": note.note_id,
            "noteType": 3,
            "noteTitle": note.title,
            "chatType": int(ChannelKind.GM),
            "chatId": str(self.chat.id),
            "threadId": "9f1c5b7e-0000-4000-8000-000000000042",
        }

    def meta_of(self, surface, note=None):
        return self.personal_meta(note) if surface == PERSONAL else self.chat_meta(note)

    def note_mention(self, surface, users, *, note=None, is_comment=False):
        note = note or (self.personal if surface == PERSONAL else self.chat_note)
        meta = self.meta_of(surface, note)
        if is_comment:
            meta["isComment"] = True

        def call():
            return self.client.post(
                SURFACE,
                {
                    "surface_type": surface,
                    "team_id": self.team_id,
                    "entity_key": f"note:{meta['noteType']}:{note.note_id}",
                    "newly_mentioned_user_ids": [str(u.id) for u in users],
                    "meta": meta,
                },
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        wire = {a["recipientUserId"]: a for a in res.json()["activities"]}
        return wire, pushes, emails

    def rows(self, surface, users, key="k", note=None):
        note = note or (self.personal if surface == PERSONAL else self.chat_note)
        return v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=surface,
            entity_key=f"note:{note.note_id}:{key}",
            newly_mentioned_user_ids=[str(u.id) for u in users],
            meta=self.meta_of(surface, note),
        )

    def leaks(self, value, surface, note=None):
        """Which details of the note, or of the chat it hangs off, appear anywhere in `value`."""
        note = note or (self.personal if surface == PERSONAL else self.chat_note)
        text = json.dumps(value, default=str)
        needles = {
            "note title": note.title,
            "note id key": '"noteId"',
            "note title key": '"noteTitle"',
            "chat id": str(self.chat.id),
            "thread id": "9f1c5b7e",
            "personal note link": f"/notes/my/{note.note_id}",
        }
        return sorted(k for k, v in needles.items() if v in text)


class NoteMentionCards(PrivateNotes):
    def test_a_stranger_to_a_personal_note_gets_the_minimal_card_everywhere(self):
        wire, pushes, emails = self.note_mention(PERSONAL, [self.member, self.user2, self.grantee])

        for stranger in (self.member, self.user2):
            sid = str(stranger.id)
            self.assertEqual(self.leaks(wire[sid], PERSONAL), [], wire[sid])
            self.assertTrue(wire[sid]["meta"]["noAccess"])
            self.assertEqual(wire[sid]["surfaceType"], PERSONAL)
            self.assertEqual(
                wire[sid]["meta"]["firstLineContent"], "Mentioned in a note you can't open"
            )
            self.assertEqual(
                pushes[sid]["title"], f"{self.user.username} mentioned you in a note you can't open"
            )
            self.assertEqual((pushes[sid]["body"], pushes[sid]["url"]), ("", MINIMAL_NOTE_URL))
            self.assertEqual(pushes[sid]["category"], "mention_note")
            (mail,) = emails[sid]
            self.assertEqual(
                (mail.title, mail.body, mail.url),
                (pushes[sid]["title"], "", MINIMAL_NOTE_URL),
            )
            self.assertEqual(self.leaks([mail.title, mail.body, mail.url], PERSONAL), [])

        gid = str(self.grantee.id)
        self.assertEqual(wire[gid]["meta"]["noteTitle"], PERSONAL_TITLE)
        self.assertNotIn("noAccess", wire[gid]["meta"])
        self.assertEqual(pushes[gid]["title"], f"{self.user.username} mentioned you in a note")
        self.assertEqual(pushes[gid]["url"], f"/workspace/notes/my/{self.personal.note_id}")
        self.assertIn(f"/notes/my/{self.personal.note_id}", emails[gid][0].url)

    def test_the_owner_who_mentions_themselves_and_a_grantee_get_the_full_card(self):
        wire, pushes, _ = self.note_mention(PERSONAL, [self.user, self.grantee])

        for who in (self.user, self.grantee):
            self.assertNotIn("noAccess", wire[str(who.id)]["meta"])
            self.assertEqual(wire[str(who.id)]["meta"]["noteTitle"], PERSONAL_TITLE)
            self.assertEqual(
                pushes[str(who.id)]["title"], f"{self.user.username} mentioned you in a note"
            )

    def test_a_stranger_to_a_chat_note_gets_the_minimal_card_everywhere(self):
        wire, pushes, emails = self.note_mention(
            CHAT, [self.member, self.leaver, self.grantee, self.colleague]
        )

        for stranger in (self.member, self.leaver, self.grantee):
            sid = str(stranger.id)
            self.assertEqual(self.leaks(wire[sid], CHAT), [], wire[sid])
            self.assertTrue(wire[sid]["meta"]["noAccess"])
            self.assertEqual(wire[sid]["surfaceType"], CHAT)
            self.assertEqual(
                pushes[sid]["title"], f"{self.user.username} mentioned you in a note you can't open"
            )
            self.assertEqual((pushes[sid]["body"], pushes[sid]["url"]), ("", MINIMAL_NOTE_URL))
            (mail,) = emails[sid]
            self.assertEqual(
                (mail.title, mail.body, mail.url), (pushes[sid]["title"], "", MINIMAL_NOTE_URL)
            )
            self.assertEqual(self.leaks([mail.title, mail.body, mail.url], CHAT), [])

        cid = str(self.colleague.id)
        self.assertEqual(wire[cid]["meta"]["noteTitle"], CHAT_TITLE)
        self.assertEqual(wire[cid]["meta"]["chatId"], str(self.chat.id))
        self.assertNotIn("noAccess", wire[cid]["meta"])
        self.assertEqual(pushes[cid]["title"], f"{self.user.username} mentioned you in a note")

    def test_a_chat_note_of_a_project_chat_follows_the_project_not_the_channel_row(self):
        pm = self.channel
        note = self.chat_note_on(pm)
        # a stray channel-member row opens nothing; the project's people do
        ChannelMember.objects.get_or_create(channel=pm, user=self.member)
        self.assertFalse(ProjectMembers.objects.filter(attendee=self.member).exists())
        meta = {**self.chat_meta(note), "chatType": int(ChannelKind.PM), "chatId": str(pm.id)}
        got = v3_activity.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=CHAT,
            entity_key=f"note:3:{note.note_id}",
            newly_mentioned_user_ids=[str(self.member.id), str(self.colleague.id)],
            meta=meta,
        )

        by_user = {str(a.recipient_id): a.id for a in got}
        self.assertEqual(
            minimal_card_activity_ids(got),
            {by_user[str(self.member.id)]},
        )

    def test_a_chat_note_of_a_chat_of_another_kind_or_a_deleted_chat_is_closed(self):
        wrong_kind = self.chat_note_on(self.chat, chat_type=int(ChannelKind.DM))
        gone = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="gone", is_deleted=True
        )
        ChannelMember.objects.create(channel=gone, user=self.colleague)
        deleted = self.chat_note_on(gone)

        for note in (wrong_kind, deleted):
            got = self.rows(CHAT, [self.colleague], key=f"n{note.note_id}", note=note)
            self.assertEqual(minimal_card_activity_ids(got), {a.id for a in got}, note.note_id)

    def test_the_notification_the_row_and_the_recipients_are_kept(self):
        self.note_mention(PERSONAL, [self.member, self.grantee])

        stored = {
            str(a.recipient_id): a
            for a in Activity.objects.filter(surface_type=PERSONAL, meta__noteId__isnull=False)
        }
        self.assertEqual(set(stored), {str(self.member.id), str(self.grantee.id)})
        for row in stored.values():
            self.assertEqual(row.meta["noteTitle"], PERSONAL_TITLE)
            self.assertNotIn("noAccess", row.meta)

    def test_the_card_is_decided_when_it_is_rendered_not_when_it_is_stored(self):
        self.note_mention(PERSONAL, [self.member])
        self.grant(self.personal, 1, self.member)
        self.authenticate(self.member)

        (row,) = self.client.get(FEED, {"team_id": self.team_id}).json()["activities"]

        self.assertEqual(row["meta"]["noteTitle"], PERSONAL_TITLE)
        self.assertNotIn("noAccess", row["meta"])

    def test_an_inline_comment_mention_is_judged_the_same_way(self):
        wire, pushes, _ = self.note_mention(
            CHAT, [self.member, self.colleague], note=self.chat_note, is_comment=True
        )

        mid, cid = str(self.member.id), str(self.colleague.id)
        self.assertEqual(self.leaks(wire[mid], CHAT), [])
        self.assertEqual(
            pushes[mid]["title"], f"{self.user.username} mentioned you in a note you can't open"
        )
        self.assertEqual(pushes[cid]["title"], f"{self.user.username} mentioned you in a comment")


class WhatElseANonReaderCanSee(PrivateNotes):
    """The stored row keeps the note's title, so every place that reads it back holds it to the
    note's rule: the feed, `list_my_unread`, the briefing and the daily digest."""

    def setUp(self):
        super().setUp()
        self.note_mention(PERSONAL, [self.member, self.grantee])
        self.note_mention(CHAT, [self.member, self.colleague])

    def test_the_feed_gives_no_row_and_lists_them_as_lost(self):
        self.authenticate(self.member)
        res = self.client.get(FEED, {"team_id": self.team_id}).json()

        self.assertEqual(res["activities"], [])
        self.assertEqual(len(res["tombstones"]), 2)

    def test_unread_and_the_briefing_say_nothing_of_the_notes(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.member.id))

        unread = unread_digest.build_unread(ctx)
        brief = briefing.build_briefing(ctx, now=timezone.now() + timedelta(seconds=1))

        self.assertEqual(unread["items"], [])
        for result in (unread, brief):
            text = json.dumps(result, default=str)
            self.assertNotIn(PERSONAL_TITLE, text)
            self.assertNotIn(CHAT_TITLE, text)

    def test_a_reader_is_told_in_full(self):
        ctx = ToolContext(team_id=self.team_id, user_id=str(self.colleague.id))

        (item,) = unread_digest.build_unread(ctx)["items"]

        self.assertIn(CHAT_TITLE, json.dumps(item))


class FailClosedAndCost(PrivateNotes):
    def test_a_failing_check_shows_the_minimal_card_not_the_full_one(self):
        with mock.patch(
            "origin.services.activity_minimal_card.minimal_card_activity_ids",
            side_effect=RuntimeError("db down"),
        ):
            wire, pushes, _ = self.note_mention(PERSONAL, [self.member, self.grantee])

        for who in (self.member, self.grantee):
            self.assertEqual(self.leaks(wire[str(who.id)], PERSONAL), [])
            self.assertEqual(pushes[str(who.id)]["url"], MINIMAL_NOTE_URL)

    def test_a_row_naming_no_note_is_a_strangers(self):
        for surface in (PERSONAL, CHAT):
            with self.subTest(surface=surface):
                got = v3_activity.create_surface_mention_activities(
                    team_id=self.team.team_id,
                    actor=self.user,
                    surface_type=surface,
                    entity_key=f"note:broken:{surface}",
                    newly_mentioned_user_ids=[str(self.user.id)],
                    meta={"firstLineContent": "x", "noteId": "not a number"},
                )
                self.assertEqual(minimal_card_activity_ids(got), {r.id for r in got})

    def _many_users(self, n):
        out = []
        for i in range(n):
            u = User.objects.create_user(
                username=f"x{i}", email=f"x{i}@m1599.test", password="pass12345"
            )
            TeamMembers.objects.create(team=self.team, attendee=u)
            out.append(u)
        return out

    def test_the_check_costs_the_same_for_one_recipient_as_for_many(self):
        extra = self._many_users(6)
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        filed = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Filed", folder_id=folder.folder_id
        )
        for surface, note in (
            (PERSONAL, self.personal),
            (PERSONAL, filed),
            (CHAT, self.chat_note),
        ):
            with self.subTest(surface=surface, note=note.note_id):
                one = self.rows(surface, [self.member], "one", note)
                many = self.rows(
                    surface, extra + [self.user2, self.grantee, self.colleague], "many", note
                )

                with CaptureQueriesContext(connection) as small:
                    minimal_card_activity_ids(one)
                with CaptureQueriesContext(connection) as large:
                    minimal_card_activity_ids(many)

                self.assertEqual(len(small), len(large))
                # Seven, plus the one question that splits the skill bodies off (MIP-2190).
                self.assertLessEqual(len(large), 8)

    def test_many_notes_cost_the_same_as_one(self):
        """The digest asks about one person's mentions of many notes at once."""
        extra = self._many_users(1)

        def mentions(count):
            rows = []
            for i in range(count):
                personal = PersonalNoteMaster.objects.create(
                    team=self.team, owner=self.user, title=f"p{i}"
                )
                chat = self.chat_note_on(self.chat)
                rows += self.rows(PERSONAL, extra, f"p{i}", personal)
                rows += self.rows(CHAT, extra, f"c{i}", chat)
            return rows

        few, many = mentions(1), mentions(8)

        with CaptureQueriesContext(connection) as small:
            got_few = minimal_card_activity_ids(few)
        with CaptureQueriesContext(connection) as large:
            got_many = minimal_card_activity_ids(many)

        self.assertEqual((len(got_few), len(got_many)), (2, 16))
        self.assertEqual(len(small), len(large))

    def test_task_task_note_personal_and_chat_note_rows_together_stay_bounded(self):
        rows = (
            self.rows(PERSONAL, [self.member, self.grantee])
            + self.rows(CHAT, [self.member, self.colleague])
            + v3_activity.create_surface_mention_activities(
                team_id=self.team.team_id,
                actor=self.user,
                surface_type=5,
                entity_key=f"task:{self.task.task_id}",
                newly_mentioned_user_ids=[str(self.member.id), str(self.colleague.id)],
                meta=self.meta(self.task),
            )
        )

        with CaptureQueriesContext(connection) as ctx:
            got = minimal_card_activity_ids(rows)

        self.assertEqual(len(got), 3)
        self.assertLessEqual(len(ctx), 12)


class NoteReaderPairsAreTheNoteRule(PrivateNotes):
    """`note_reader_pairs` is `get_effective_role` (plus the owner of a personal note) for many
    people and notes at once, and so the feed's gate: three rules that cannot drift."""

    def folder(self, *, visibility=None, parent=None, name="F", granted=()):
        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user2,
            name=name,
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=visibility,
            parent_folder_id=parent.folder_id if parent else None,
        )
        for u in granted:
            NoteFolderPermission.objects.create(team=self.team, folder=folder, user=u, role_id=3)
        return folder

    def filed(self, folder):
        return PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user2, title="Filed", folder_id=folder.folder_id
        )

    def test_a_personal_note_equals_the_per_person_rule_and_the_feed(self):
        public = self.folder(visibility=PersonalNoteFolder.VISIBILITY_PUBLIC, name="Pub")
        private = self.folder(
            visibility=PersonalNoteFolder.VISIBILITY_PRIVATE, name="Priv", granted=[self.member]
        )
        inheriting = self.folder(parent=public, name="Inherit")
        narrowed = self.folder(
            parent=public, name="Narrow", visibility=PersonalNoteFolder.VISIBILITY_PRIVATE
        )
        personal_folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user2, name="Mine", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        notes = [
            self.personal,
            self.filed(public),
            self.filed(private),
            self.filed(inheriting),
            self.filed(narrowed),
            self.filed(personal_folder),
            PersonalNoteMaster.objects.create(team=self.team, owner=self.member, title="Theirs"),
        ]
        # the team's owner with no member row of theirs is still on the team
        TeamMembers.objects.filter(attendee=self.user).delete()
        people = [
            self.user,
            self.user2,
            self.member,
            self.colleague,
            self.grantee,
            self.leaver,
        ]
        TeamMembers.objects.filter(attendee=self.leaver).update(is_deleted=True)

        got = note_reader_pairs(
            [p.id for p in people], NOTE_TYPE_PERSONAL, [n.note_id for n in notes]
        )

        for person in people:
            lost = unreadable_surface_activity_ids(
                person.id,
                [(note.note_id, PERSONAL, {"noteId": note.note_id}) for note in notes],
            )
            for note in notes:
                expected = (
                    get_effective_role(person.id, NOTE_TYPE_PERSONAL, note.note_id) is not None
                    or note.owner_id == person.id
                )
                self.assertEqual(
                    (str(person.id), note.note_id) in got,
                    expected,
                    (person.username, note.note_id, note.folder_id),
                )
                self.assertEqual(
                    note.note_id not in lost, expected, (person.username, note.note_id)
                )

    def test_a_chat_note_equals_the_per_person_rule_and_the_feed(self):
        project = self.project
        notes = [
            self.chat_note,
            self.chat_note_on(self.chat, chat_type=int(ChannelKind.DM)),
            self.chat_note_on(self.channel),  # the project's chat
            self.chat_note_on(Channel.objects.create(team=self.team, kind=ChannelKind.GM)),
            self.chat_note_on(
                Channel.objects.create(team=self.team, kind=ChannelKind.GM, is_deleted=True)
            ),
        ]
        ChannelMember.objects.create(channel=notes[4].channel, user=self.colleague)
        # a stray channel row on the project's chat, a grant after leaving, and an author who left
        ChannelMember.objects.get_or_create(channel=self.channel, user=self.member)
        self.grant(self.chat_note, 3, self.leaver)
        left_author = self.chat_note_on(self.chat, owner=self.leaver)
        notes.append(left_author)
        people = [
            self.user,
            self.user2,
            self.member,
            self.colleague,
            self.grantee,
            self.leaver,
        ]
        self.assertTrue(ProjectMembers.objects.filter(project=project, attendee=self.user).exists())

        got = note_reader_pairs([p.id for p in people], NOTE_TYPE_CHAT, [n.note_id for n in notes])

        for person in people:
            lost = unreadable_surface_activity_ids(
                person.id,
                [(n.note_id, CHAT, {"noteId": n.note_id}) for n in notes],
            )
            for note in notes:
                expected = get_effective_role(person.id, NOTE_TYPE_CHAT, note.note_id) is not None
                self.assertEqual(
                    (str(person.id), note.note_id) in got,
                    expected,
                    (person.username, note.note_id),
                )
                self.assertEqual(
                    note.note_id not in lost, expected, (person.username, note.note_id)
                )

    def test_it_is_a_fixed_number_of_queries_however_many_people_and_notes(self):
        def cost(note_type, people, notes):
            with CaptureQueriesContext(connection) as ctx:
                note_reader_pairs([p.id for p in people], note_type, [n.note_id for n in notes])
            return len(ctx)

        folder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="Handbook",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility=PersonalNoteFolder.VISIBILITY_PUBLIC,
        )
        personal = [self.personal] + [self.filed(folder) for _ in range(5)]
        chats = [self.chat_note] + [self.chat_note_on(self.chat) for _ in range(5)]
        few = [self.member]
        many = [self.member, self.user2, self.colleague, self.grantee, self.leaver, self.user]

        self.assertEqual(
            cost(NOTE_TYPE_PERSONAL, few, personal[:1]),
            cost(NOTE_TYPE_PERSONAL, many, personal[:1]),
        )
        self.assertEqual(
            cost(NOTE_TYPE_PERSONAL, many, personal[:1] + personal[1:]),
            cost(NOTE_TYPE_PERSONAL, many, personal[:1] + personal[1:2]),
        )
        self.assertEqual(cost(NOTE_TYPE_CHAT, few, chats[:1]), cost(NOTE_TYPE_CHAT, many, chats))

    def test_nobody_and_nothing_cost_nothing(self):
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(note_reader_pairs([], NOTE_TYPE_PERSONAL, [1]), set())
            self.assertEqual(note_reader_pairs([self.user.id], NOTE_TYPE_CHAT, []), set())
            self.assertEqual(note_reader_pairs([self.user.id], NOTE_TYPE_CHAT, ["x"]), set())
        self.assertEqual(len(ctx), 0)


# -- item 2: the participant "X commented" row ------------------------------------------------ #


class ParticipantRowOfAHolder(Cards):
    """`self.assignee` is the assignee of `self.held` and `self.rep` the reporter of
    `self.rep_task` (who commented on it before): neither is on the project, so both can open
    their task and neither can open the project chat the comment is mirrored into. A plain
    comment (no @mention) gives each of them the participant row."""

    def setUp(self):
        super().setUp()
        self.rep = User.objects.create_user(
            username="rep", email="rep@m1599.test", password="pass12345"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.rep)
        self.rep_task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.rep,
            assignee=self.user,
            title="Reported elsewhere",
            status="WIP",
        )
        TaskComments.objects.create(
            task=self.rep_task, sender=self.rep, comment_id=1, comment_body=comment_body()
        )
        self.aid, self.rid, self.mid = (str(u.id) for u in (self.assignee, self.rep, self.member))
        self.authenticate(self.user)
        self.wire, self.pushes, self.emails = self.plain_comment(self.held)
        self.rep_wire, self.rep_pushes, self.rep_emails = self.plain_comment(self.rep_task)
        self.mirror = Message.objects.get(
            task_id=self.held.task_id, metadata__has_key="taskCommentId"
        )
        self.url = f"/workspace/tasks/project/{self.project.project_id}/task/{self.held.task_id}"

    def plain_comment(self, task):
        EmailNotificationEvent.objects.all().delete()

        def call():
            return self.client.post(
                COMMENT,
                {"task_id": str(task.task_id), "comment_body": comment_body()},
                format="json",
            )

        res, pushes, emails = self.fan_out(call)
        self.assertEqual(res.status_code, 201, res.content)
        wire = {a["recipientUserId"]: a for a in res.json()["_v3_activities"]}
        return wire, pushes, emails

    def feed(self, user):
        self.authenticate(user)
        res = self.client.get(FEED, {"team_id": self.team_id})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_the_holders_get_the_participant_row_in_their_feed(self):
        for user, task in ((self.assignee, self.held), (self.rep, self.rep_task)):
            with self.subTest(user=user.username):
                got = self.feed(user)

                (row,) = got["activities"]
                self.assertEqual(row["activityType"], int(ActivityType.THREAD_REPLY))
                self.assertEqual(row["message"]["taskId"], task.task_id)
                self.assertIn(COMMENT_TEXT, row["message"]["bodyText"])
                self.assertEqual(got["tombstones"], [])

    def test_the_row_is_written_for_them_as_before(self):
        self.assertEqual(set(self.wire), {self.aid})
        self.assertEqual(set(self.rep_wire), {self.rid})

    def test_the_push_and_the_email_link_to_the_task_not_to_a_chat_they_cannot_open(self):
        rep_url = f"/workspace/tasks/project/{self.project.project_id}/task/{self.rep_task.task_id}"

        self.assertEqual(self.pushes[self.aid]["url"], self.url)
        self.assertEqual(self.emails[self.aid][0].url, self.url)
        self.assertEqual(self.rep_pushes[self.rid]["url"], rep_url)
        self.assertEqual(self.rep_emails[self.rid][0].url, rep_url)
        # what else the push says is as before: its group, its title, the comment's own text
        self.assertEqual(self.pushes[self.aid]["category"], "task_comments")
        self.assertEqual(
            self.pushes[self.aid]["title"], f"{self.user.username} commented on a task"
        )
        self.assertIn(COMMENT_TEXT, self.pushes[self.aid]["body"])

    def test_a_project_member_keeps_the_link_to_the_chat(self):
        mine = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.colleague,
            title="Mine",
            status="WIP",
        )

        _, pushes, emails = self.plain_comment(mine)

        cid = str(self.colleague.id)
        self.assertIn("/workspace/chat/pm/", pushes[cid]["url"])
        self.assertIn("/workspace/chat/pm/", emails[cid][0].url)

    def test_the_chat_and_its_messages_stay_closed_to_them(self):
        self.authenticate(self.assignee)

        chat = self.client.get(f"/api/v3/channels/{self.channel.id}/messages/")
        one = self.client.get(f"/api/v3/messages/{self.mirror.id}/")

        for res in (chat, one):
            self.assertIn(res.status_code, (403, 404))
            self.assertNotIn(COMMENT_TEXT, res.content.decode())

    def test_a_reply_that_is_not_a_task_comment_stays_out(self):
        """The task rule reaches the comment mirror and nothing else of the project chat: a
        thread reply under the task's card is the chat's content."""
        card = Message.objects.get(
            channel=self.channel, task_id=self.held.task_id, thread_root__isnull=True
        )
        reply = Message.objects.create(
            channel=self.channel,
            sender=self.user,
            seq=60,
            body=[],
            body_text="a reply on the card",
            task_id=self.held.task_id,
            thread_root=card,
            is_thread_reply=True,
        )
        row = Activity.objects.create(
            team=self.team,
            recipient=self.assignee,
            actor=self.user,
            activity_type=ActivityType.THREAD_REPLY,
            channel=self.channel,
            message=reply,
            meta={},
        )
        from origin.services.activity_minimal_card import task_links

        # nor is it given a link into the task in the push or the email
        self.assertEqual(task_links([row]), {})

        got = self.feed(self.assignee)

        self.assertEqual({a["messageId"] for a in got["activities"]}, {str(self.mirror.id)})
        self.assertEqual(len(got["tombstones"]), 1)

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

    def test_someone_who_lost_the_task_is_given_no_link_to_it(self):
        from origin.services.activity_minimal_card import task_links

        row = Activity.objects.get(recipient=self.assignee, message=self.mirror)
        self.assertEqual(task_links([row]), {row.id: self.url})
        TaskMaster.objects.filter(task_id=self.held.task_id).update(assignee=self.user)

        self.assertEqual(task_links([row]), {})

    def test_unread_and_the_feed_agree(self):
        ctx = ToolContext(team_id=self.team_id, user_id=self.aid)

        (item,) = unread_digest.build_unread(ctx)["items"]

        self.assertEqual(item["task_id"], self.held.task_id)
        self.assertEqual(len(self.feed(self.assignee)["activities"]), 1)

    def test_the_feed_costs_the_same_for_one_such_row_as_for_many(self):
        def feed_queries():
            self.authenticate(self.assignee)
            with CaptureQueriesContext(connection) as ctx:
                self.assertEqual(self.client.get(FEED, {"team_id": self.team_id}).status_code, 200)
            return len(ctx)

        before = feed_queries()
        self.authenticate(self.user)
        for _ in range(4):
            self.plain_comment(self.held)

        self.assertEqual(len(self.feed(self.assignee)["activities"]), 5)
        self.assertEqual(feed_queries(), before)

    def test_the_links_cost_the_same_for_one_row_as_for_many(self):
        from origin.services.activity_minimal_card import task_links

        one = list(Activity.objects.filter(recipient=self.assignee).select_related("message"))
        for _ in range(4):
            self.plain_comment(self.held)
        many = list(Activity.objects.filter(recipient=self.assignee).select_related("message"))

        with CaptureQueriesContext(connection) as small:
            task_links(one)
        with CaptureQueriesContext(connection) as large:
            got = task_links(many)

        self.assertEqual((len(one), len(many), len(got)), (1, 5, 5))
        self.assertEqual(len(small), len(large))
