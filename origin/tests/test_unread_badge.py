"""The number on the installed app's icon (MIP-929).

With the app closed the service worker writes `badge_count` from a Web
Push payload to the home-screen icon, so the server has to count the same
three things the open app counts (`App.tsx`: `useAppBadge(unReadInboxItemCount
+ unReadChatAndActivityCounts)`):

  * unread REQUESTS in the inbox (not the Activities half: item_type 0, 6
    Genos digest, 9 reminders),
  * chats (DM, GM, PM, NOT MDM) with something new, ONE per chat,
  * unread Activity feed topics, ONE per topic.

All for the one team the app is showing, which for a push is the team the
push is about. Before this the server added up every unread inbox item of
every team and every unread message of every chat of every team, so an
unread pile of daily digests kept the icon at 121 while the app said 0.

Each test sets up ONE term, so the total is that term. The two `Parity`
classes check the rule against what the endpoints the app reads actually
return, with the app's own rule re-written from the wire shape.
"""

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.models.common.inbox_models import ACTIVITY_INBOX_ITEM_TYPES, InboxItems
from origin.models.common.notification_models import PushSubscription
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.management.commands import agent_digest
from origin.services import unread_badge, webpush_dispatch
from origin.services.message_reminders import ITEM_TYPE_MESSAGE_REMINDER
from origin.services.todo_reminders import ITEM_TYPE_REMINDER
from origin.services.unread_badge import unread_badge_total
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# item_type vocabulary (origin/models/common/inbox_models.py)
ACTIVITY_NOTICE, JOIN_TEAM, JOIN_PROJECT, JOIN_GM, NOTE_ACCESS = 0, 1, 2, 3, 4
OWNERSHIP_CLAIM, DIGEST, TEAM_CONNECTION, EXTERNAL_SHARE, MSG_REMINDER = 5, 6, 7, 8, 9
REQUEST_TYPES = (JOIN_TEAM, JOIN_PROJECT, JOIN_GM, NOTE_ACCESS, OWNERSHIP_CLAIM)
REQUEST_TYPES += (TEAM_CONNECTION, EXTERNAL_SHARE)


class _Fixture(BaseAPITestCase):
    """`self.user` is the person whose icon it is; `self.user2` is everyone else.

    Team A is `self.team`; team B is a second team both belong to.
    """

    def setUp(self):
        super().setUp()
        self.me = self.user
        self.other = self.user2
        self.team_a = self.team
        self.team_b = TeamMaster.objects.create(
            team_name="Team B", team_email="b@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team_b, attendee=self.user)
        TeamMembers.objects.create(team=self.team_b, attendee=self.user2)

    # -- the number under test ------------------------------------------
    def badge(self, team="a", user=None):
        team_id = {"a": self.team_a.team_id, "b": self.team_b.team_id, None: None}.get(team, team)
        return unread_badge_total((user or self.me).id, team_id=team_id)

    # -- inbox ------------------------------------------------------------
    def inbox(self, item_type, *, team=None, receiver=None, **kw):
        kw.setdefault("is_read", False)
        return InboxItems.objects.create(
            team=team or self.team_a,
            sender=self.other,
            receiver=receiver or self.me,
            item_type=item_type,
            item_body={"title": "t", "text": "x"},
            **kw,
        )

    # -- chats ------------------------------------------------------------
    def chat(self, kind=ChannelKind.GM, *, team=None, me_in=True, **kw):
        channel = Channel.objects.create(
            team=team or self.team_a, kind=kind, title=f"chat-{uuid.uuid4().hex[:6]}", **kw
        )
        ChannelMember.objects.create(channel=channel, user=self.other)
        if me_in:
            ChannelMember.objects.create(channel=channel, user=self.me)
        return channel

    def say(self, channel, seq, *, sender=None, thread_root=None, deleted=False, **kw):
        return Message.objects.create(
            channel=channel,
            sender=sender or self.other,
            seq=seq,
            body={"text": f"m{seq}"},
            body_text=f"m{seq}",
            thread_root=thread_root,
            parent=thread_root,
            is_thread_reply=thread_root is not None,
            deleted_at=timezone.now() if deleted else None,
            **kw,
        )

    def read_up_to(self, channel, message):
        ReadCursor.objects.update_or_create(
            user=self.me,
            channel=channel,
            thread_root=None,
            defaults={"last_read_message": message},
        )

    # -- activity ---------------------------------------------------------
    def activity(
        self,
        activity_type=ActivityType.MESSAGE,
        *,
        channel=None,
        message=None,
        team=None,
        recipient=None,
        is_read=False,
        age_days=0,
        **kw,
    ):
        row = Activity.objects.create(
            team=team or self.team_a,
            recipient=recipient or self.me,
            actor=self.other,
            activity_type=activity_type,
            channel=channel,
            message=message,
            is_read=is_read,
            **kw,
        )
        if age_days:
            Activity.objects.filter(pk=row.pk).update(
                ts_created_at=timezone.now() - timedelta(days=age_days)
            )
        return row

    def feed(self, channel, n, activity_type=ActivityType.MESSAGE, **kw):
        """`n` unread activities, each on its own new top-level message."""
        top = Message.objects.filter(channel=channel).count()
        return [
            self.activity(
                activity_type, channel=channel, message=self.say(channel, top + i + 1), **kw
            )
            for i in range(n)
        ]


# ----------------------------------------------------------------------
# Term 1: unread Requests
# ----------------------------------------------------------------------
class InboxTermTests(_Fixture):
    def test_activity_half_of_the_inbox_does_not_count(self):
        # A digest, a reminder and a plain notice are things that HAPPENED.
        # The app puts them on the Activities tab and keeps them out of the
        # Requests badge.
        for item_type in (ACTIVITY_NOTICE, DIGEST, MSG_REMINDER):
            self.inbox(item_type)
        self.assertEqual(self.badge(), 0)

    def test_every_request_type_counts_once(self):
        for item_type in REQUEST_TYPES:
            self.inbox(item_type)
        self.assertEqual(self.badge(), len(REQUEST_TYPES))

    def test_the_production_shape(self):
        # Ken's account on 2026-11-02: 92 unread digests, 17 message
        # reminders, 12 activity notices, no requests. The icon said 121;
        # the open app said 0.
        InboxItems.objects.bulk_create(
            [
                InboxItems(
                    team=self.team_a,
                    sender=None if t == DIGEST else self.other,
                    receiver=self.me,
                    item_type=t,
                    item_body={"title": "t", "text": "x"},
                )
                for t, n in ((DIGEST, 92), (MSG_REMINDER, 17), (ACTIVITY_NOTICE, 12))
                for _ in range(n)
            ]
        )
        self.assertEqual(InboxItems.objects.filter(receiver=self.me, is_read=False).count(), 121)
        self.assertEqual(self.badge(), 0)
        self.assertEqual(self.badge(team=None), 0)

    def test_a_request_among_the_digests_is_the_whole_count(self):
        for _ in range(5):
            self.inbox(DIGEST)
        self.inbox(JOIN_TEAM)
        self.assertEqual(self.badge(), 1)

    def test_a_request_in_the_other_team_does_not_count(self):
        self.inbox(JOIN_TEAM, team=self.team_b)
        self.assertEqual(self.badge("a"), 0)
        self.assertEqual(self.badge("b"), 1)

    def test_read_deleted_and_other_peoples_requests_do_not_count(self):
        self.inbox(JOIN_TEAM, is_read=True)
        self.inbox(JOIN_TEAM, is_deleted=True)
        self.inbox(JOIN_TEAM, receiver=self.other)
        self.assertEqual(self.badge(), 0)

    def test_the_activity_types_include_what_the_server_files_there(self):
        # The sets the writers use must stay inside the one the badge
        # excludes, or a new digest/reminder would count as a request.
        self.assertTrue(
            {agent_digest.ITEM_TYPE_DIGEST, ITEM_TYPE_MESSAGE_REMINDER, ITEM_TYPE_REMINDER, 0}
            <= set(ACTIVITY_INBOX_ITEM_TYPES)
        )
        self.assertFalse(set(REQUEST_TYPES) & set(ACTIVITY_INBOX_ITEM_TYPES))


# ----------------------------------------------------------------------
# Term 2: chats with something new
# ----------------------------------------------------------------------
class ChatTermTests(_Fixture):
    def test_dm_gm_and_pm_count_but_mdm_does_not(self):
        for kind in (ChannelKind.DM, ChannelKind.GM, ChannelKind.PM, ChannelKind.MDM):
            self.say(self.chat(kind), 1)
        self.assertEqual(self.badge(), 3)

    def test_it_counts_chats_not_messages(self):
        busy = self.chat()
        for seq in range(1, 6):
            self.say(busy, seq)
        self.assertEqual(self.badge(), 1)
        self.say(self.chat(), 1)
        self.assertEqual(self.badge(), 2)

    def test_deleted_channels_memberships_and_strangers_do_not_count(self):
        self.say(self.chat(is_deleted=True), 1)

        gone = self.chat()
        self.say(gone, 1)
        ChannelMember.objects.filter(channel=gone, user=self.me).update(is_deleted=True)

        self.say(self.chat(me_in=False), 1)
        self.assertEqual(self.badge(), 0)

    def test_a_chat_in_the_other_team_does_not_count(self):
        self.say(self.chat(team=self.team_b), 1)
        self.assertEqual(self.badge("a"), 0)
        self.assertEqual(self.badge("b"), 1)

    def test_the_read_cursor_is_respected(self):
        channel = self.chat()
        m1, _m2, m3 = (self.say(channel, seq) for seq in (1, 2, 3))
        self.assertEqual(self.badge(), 1)  # never read anything
        self.read_up_to(channel, m1)
        self.assertEqual(self.badge(), 1)  # two still to read
        self.read_up_to(channel, m3)
        self.assertEqual(self.badge(), 0)
        self.say(channel, 4)
        self.assertEqual(self.badge(), 1)

    def test_my_own_latest_message_means_nothing_is_new(self):
        # The app reads a chat whose newest message is the viewer's own as
        # read, even when the cursor is behind (`channelToLegacyChat`).
        channel = self.chat()
        self.say(channel, 1)
        self.say(channel, 2, sender=self.me)
        self.assertEqual(self.badge(), 0)

    def test_someone_replying_after_my_message_is_new(self):
        channel = self.chat()
        self.say(channel, 1, sender=self.me)
        self.say(channel, 2)
        self.assertEqual(self.badge(), 1)

    def test_only_my_own_messages_after_the_cursor_is_not_new(self):
        channel = self.chat()
        m1 = self.say(channel, 1)
        self.read_up_to(channel, m1)
        self.say(channel, 2, sender=self.me)
        self.assertEqual(self.badge(), 0)

    def test_thread_replies_and_deleted_messages_are_not_new(self):
        channel = self.chat()
        root = self.say(channel, 1)
        self.read_up_to(channel, root)
        self.say(channel, 2, thread_root=root)
        self.say(channel, 3, deleted=True)
        self.assertEqual(self.badge(), 0)

    def test_a_message_whose_sender_is_gone_is_still_new(self):
        channel = self.chat()
        msg = self.say(channel, 1)
        Message.objects.filter(pk=msg.pk).update(sender=None)
        self.assertEqual(self.badge(), 1)

    def test_a_chat_shared_into_the_team_counts_there(self):
        # The chat list adds chats another team shared with this one
        # (`team_scope_q`); the badge has to use the same scope or a guest's
        # icon would miss exactly the chats the app lists for them.
        hosted = self.chat(team=self.team_b)
        self.say(hosted, 1)
        self.assertEqual(self.badge("a"), 0)
        shared = {str(hosted.id): object()}
        with mock.patch(
            "origin.services.channel_scope.external_objects_for_member",
            side_effect=lambda kind, team, user: shared if kind == "channel" else {},
        ):
            self.assertEqual(self.badge("a"), 1)


class ChatParityTests(_Fixture):
    """The server count against the endpoint the app builds its own from."""

    def test_agrees_with_the_chat_list_the_app_receives(self):
        read = self.chat(ChannelKind.DM)
        self.read_up_to(read, self.say(read, 1))
        self.say(self.chat(ChannelKind.GM), 1)
        mine = self.chat(ChannelKind.GM)
        self.say(mine, 1)
        self.say(mine, 2, sender=self.me)
        self.say(self.chat(ChannelKind.MDM), 1)
        self.say(self.chat(ChannelKind.PM), 1)
        self.say(self.chat(ChannelKind.GM, team=self.team_b), 1)
        threaded = self.chat(ChannelKind.GM)
        root = self.say(threaded, 1)
        self.read_up_to(threaded, root)
        self.say(threaded, 2, thread_root=root)

        self.authenticate(self.me)
        for team in (self.team_a, self.team_b):
            resp = self.client.get("/api/v3/channels/", {"team_id": str(team.team_id)})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                self.badge(team.team_id), _app_chat_count(resp.data["channels"], str(self.me.id))
            )
        self.assertEqual(self.badge("a"), 2)  # the new GM and the PM
        self.assertEqual(self.badge("b"), 1)


def _app_chat_count(channels, my_id):
    """`countUnreadChats` + `channelToLegacyChat`, from the wire shape."""
    count = 0
    for ch in channels:
        if ch["kind"] not in (1, 2, 3):
            continue
        latest = ch["latestMessage"]
        self_sent = bool(latest) and (latest["sender"] or {}).get("userId") == my_id
        if ch["unreadCount"] > 0 and not self_sent:
            count += 1
    return count


# ----------------------------------------------------------------------
# Term 3: Activity feed topics
# ----------------------------------------------------------------------
class _ActivityFixture(_Fixture):
    """Activities are filed for chats the person is not in, so that the chat
    term stays out of the way; the three terms together are in `TotalTests`."""

    def chat(self, kind=ChannelKind.GM, *, team=None, me_in=False, **kw):
        return super().chat(kind, team=team, me_in=me_in, **kw)


class ActivityTermTests(_ActivityFixture):
    def test_an_unread_activity_counts(self):
        self.feed(self.chat(), 1)
        self.assertEqual(self.badge(), 1)

    def test_a_burst_in_one_chat_is_one_topic(self):
        # The feed collapses a run into one row and the badge counts rows.
        a, b = self.chat(), self.chat()
        self.feed(a, 5)
        self.assertEqual(self.badge(), 1)
        self.feed(b, 2)
        self.assertEqual(self.badge(), 2)

    def test_the_kind_of_activity_is_part_of_the_topic(self):
        channel = self.chat()
        self.feed(channel, 2, ActivityType.MESSAGE)
        self.feed(channel, 2, ActivityType.MENTION)
        self.assertEqual(self.badge(), 2)

    def test_replies_collapse_per_thread(self):
        channel = self.chat()
        root_x, root_y = self.say(channel, 1), self.say(channel, 2)
        for seq, root in ((3, root_x), (4, root_x), (5, root_x)):
            self.activity(
                ActivityType.THREAD_REPLY,
                channel=channel,
                message=self.say(channel, seq, thread_root=root),
            )
        self.assertEqual(self.badge(), 1)
        self.activity(
            ActivityType.THREAD_REPLY,
            channel=channel,
            message=self.say(channel, 6, thread_root=root_y),
        )
        self.assertEqual(self.badge(), 2)

    def test_task_comments_collapse_per_task_not_per_thread(self):
        channel = self.chat(ChannelKind.PM)
        for n in (1, 2):
            root = self.say(channel, n * 10)
            self.activity(
                ActivityType.THREAD_REPLY,
                channel=channel,
                message=self.say(
                    channel, n * 10 + 1, thread_root=root, metadata={"taskCommentId": n}
                ),
            )
        self.assertEqual(self.badge(), 1)

    def test_replies_that_are_not_task_comments_stay_per_thread(self):
        channel = self.chat(ChannelKind.PM)
        for n in (1, 2):
            root = self.say(channel, n * 10)
            self.activity(
                ActivityType.THREAD_REPLY,
                channel=channel,
                message=self.say(channel, n * 10 + 1, thread_root=root),
            )
        self.assertEqual(self.badge(), 2)

    def test_reactions_are_one_topic_per_reacted_message(self):
        channel = self.chat()
        mine = [self.say(channel, seq, sender=self.me) for seq in (1, 2)]
        for _ in range(3):
            self.activity(ActivityType.REACTION, channel=channel, message=mine[0])
        self.assertEqual(self.badge(), 1)
        self.activity(ActivityType.REACTION, channel=channel, message=mine[1])
        self.assertEqual(self.badge(), 2)

    def test_a_reaction_to_someone_elses_message_is_not_counted(self):
        # The app only shows a reaction to the person who was reacted to.
        channel = self.chat()
        self.activity(ActivityType.REACTION, channel=channel, message=self.say(channel, 1))
        self.assertEqual(self.badge(), 0)

    def test_note_and_task_body_mentions_are_topics_of_their_own(self):
        for note_id in (1, 1, 2):
            self.activity(ActivityType.MENTION, surface_type=6, meta={"noteId": note_id})
        self.assertEqual(self.badge(), 2)
        self.activity(ActivityType.MENTION, surface_type=7, meta={"noteId": 1})  # task note 1
        self.assertEqual(self.badge(), 3)
        for _ in range(2):
            self.activity(ActivityType.MENTION, surface_type=5, meta={"taskId": 7})
        self.assertEqual(self.badge(), 4)

    def test_read_activities_and_other_peoples_do_not_count(self):
        channel = self.chat()
        self.feed(channel, 2, is_read=True)
        self.feed(channel, 2, recipient=self.other)
        self.assertEqual(self.badge(), 0)

    def test_an_activity_in_the_other_team_does_not_count(self):
        self.feed(self.chat(team=self.team_b), 1, team=self.team_b)
        self.assertEqual(self.badge("a"), 0)
        self.assertEqual(self.badge("b"), 1)

    def test_the_feed_only_holds_thirty_days(self):
        channel = self.chat()
        self.feed(channel, 1, age_days=31)
        self.assertEqual(self.badge(), 0)
        self.feed(self.chat(), 1, age_days=29)
        self.assertEqual(self.badge(), 1)

    def test_the_synthetic_thread_first_message_is_dropped(self):
        # `countUnreadActivityMessages` drops `isThread && messageId === 1`.
        channel = self.chat()
        root = self.say(channel, 0)
        self.activity(
            ActivityType.THREAD_REPLY,
            channel=channel,
            message=self.say(channel, 1, thread_root=root),
        )
        self.assertEqual(self.badge(), 0)
        self.activity(
            ActivityType.THREAD_REPLY,
            channel=channel,
            message=self.say(channel, 2, thread_root=root),
        )
        self.assertEqual(self.badge(), 1)


class ActivityParityTests(_ActivityFixture):
    """The server count against the feed the app receives, using the app's
    own grouping rule (`activityTopicKey`) written out from the wire shape."""

    def test_agrees_with_the_activity_feed_the_app_receives(self):
        a, b, pm = self.chat(), self.chat(ChannelKind.MDM), self.chat(ChannelKind.PM)
        self.feed(a, 3)
        self.feed(a, 2, ActivityType.MENTION)
        self.feed(b, 2)
        root = self.say(a, 50)
        for seq in (51, 52):
            self.activity(
                ActivityType.THREAD_REPLY, channel=a, message=self.say(a, seq, thread_root=root)
            )
        mine = self.say(a, 60, sender=self.me)
        for _ in range(2):
            self.activity(ActivityType.REACTION, channel=a, message=mine)
        self.activity(ActivityType.REACTION, channel=a, message=self.say(a, 61))
        for n in (1, 2):
            troot = self.say(pm, n * 10)
            self.activity(
                ActivityType.THREAD_REPLY,
                channel=pm,
                message=self.say(pm, n * 10 + 1, thread_root=troot, metadata={"taskCommentId": n}),
            )
        for note_id in (3, 3, 4):
            self.activity(ActivityType.MENTION, surface_type=8, meta={"noteId": note_id})
        self.activity(ActivityType.MENTION, surface_type=5, meta={"taskId": 9, "projectId": 2})
        self.feed(a, 2, is_read=True)
        self.feed(self.chat(team=self.team_b), 2, team=self.team_b)
        self.feed(a, 1, age_days=40)

        self.authenticate(self.me)
        for team in (self.team_a, self.team_b):
            resp = self.client.get(
                "/api/v3/activities/",
                {
                    "team_id": str(team.team_id),
                    "since": (timezone.now() - timedelta(days=30)).isoformat(),
                },
            )
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                self.badge(team.team_id),
                _app_activity_count(resp.data["activities"], str(self.me.id)),
            )
        self.assertGreater(self.badge("a"), 5)


def _num(value):
    """`num()` in `v3ActivityToLegacy`: numbers only, else 0."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def _app_activity_count(rows, my_id):
    """`popActivityMessages` + `countUnreadActivityTopics`, from the wire shape."""
    keys = set()
    for row in rows:
        if row["isRead"]:
            continue
        msg = row["message"]
        t = row["activityType"]
        # popActivityMessages: a reaction only matters to who was reacted to.
        sender = (msg or {}).get("sender") or {}
        if t == 2 and sender.get("userId") != my_id:
            continue
        # countUnreadActivityTopics: the synthetic thread first message.
        if msg and msg["isThreadReply"] and msg["seq"] == 1:
            continue
        surface = row["surfaceType"]
        meta = row["meta"] or {}
        if surface is not None:
            if 6 <= surface <= 8:
                keys.add((t, "note", surface, _num(meta.get("noteId"))))
            elif surface == 5:
                keys.add((t, "taskbody", _num(meta.get("taskId"))))
            else:
                keys.add((t, "channel", _num(meta.get("noteId"))))
            continue
        kind, channel_id = row["channelKind"], row["channelId"]
        task_id = msg.get("taskId") or 0
        is_task_comment = bool((msg.get("metadata") or {}).get("taskCommentId")) or (
            kind == 4 and bool(task_id)
        )
        if is_task_comment:
            keys.add((t, "taskcomment", task_id))
        elif t == 2:
            keys.add((2, "reaction", channel_id, msg["id"]))
        elif msg["isThreadReply"]:
            keys.add((t, "thread", channel_id, msg["parentId"] or ""))
        else:
            keys.add((t, "channel", channel_id))
    return len(keys)


# ----------------------------------------------------------------------
# The total
# ----------------------------------------------------------------------
class TotalTests(_Fixture):
    def test_the_icon_is_the_sum_of_the_three_terms(self):
        self.inbox(JOIN_TEAM)
        self.inbox(DIGEST)
        self.say(self.chat(), 1)
        self.feed(self.chat(), 1)
        # one request + (the new chat) + (the other chat) + its activity topic
        self.assertEqual(self.badge(), 1 + 2 + 1)

    def test_no_team_counts_every_team_of_the_user(self):
        # The fallback for a push that cannot name a team: an upper bound,
        # corrected the moment the app opens.
        self.inbox(JOIN_TEAM)
        self.inbox(JOIN_TEAM, team=self.team_b)
        self.say(self.chat(team=self.team_b), 1)
        self.assertEqual(self.badge("a"), 1)
        self.assertEqual(self.badge(None), 3)

    def test_an_unreadable_team_id_means_no_team_rather_than_an_error(self):
        self.inbox(JOIN_TEAM)
        self.inbox(JOIN_TEAM, team=self.team_b)
        self.assertEqual(unread_badge_total(self.me.id, team_id="not-a-uuid"), 2)
        self.assertEqual(unread_badge_total(self.me.id, team_id=""), 2)

    def test_a_failure_is_none_not_an_exception(self):
        with mock.patch.object(unread_badge, "_unread_requests", side_effect=RuntimeError("db")):
            self.assertIsNone(self.badge())

    def test_a_few_queries_whatever_the_size(self):
        def queries(team):
            with CaptureQueriesContext(connection) as ctx:
                self.badge(team)
            return len(ctx)

        self.inbox(JOIN_TEAM)
        self.feed(self.chat(), 1)
        small = (queries("a"), queries(None))
        for _ in range(8):
            self.feed(self.chat(), 2)
            self.inbox(JOIN_TEAM)
        big = (queries("a"), queries(None))
        self.assertEqual(small, big)  # no per-chat or per-row queries
        # requests 1 + activity 1 + chats 1 + the team's two share lookups
        self.assertEqual(big, (5, 3))


# ----------------------------------------------------------------------
# The push carries the team it is about
# ----------------------------------------------------------------------
@override_settings(CACHES=LOCMEM)
class PushCarriesTheTeamTests(_Fixture):
    def setUp(self):
        super().setUp()
        cache.clear()
        PushSubscription.objects.create(
            user=self.other,
            endpoint=f"https://example.com/{uuid.uuid4()}",
            p256dh="p256",
            auth="auth",
        )

    def _send(self, fn, *args, **kwargs):
        """Run a dispatcher with the transport mocked; returns the send mock."""
        with (
            mock.patch.object(webpush_dispatch, "vapid_configured", return_value=True),
            mock.patch.object(webpush_dispatch, "send_web_push") as send,
            mock.patch.object(
                webpush_dispatch._executor, "submit", side_effect=lambda f, **kw: f(**kw)
            ),
            mock.patch("origin.services.email_enqueue.enqueue_email_to_user"),
            self.captureOnCommitCallbacks(execute=True),
        ):
            fn(*args, **kwargs)
        return send

    def _badge_of(self, send):
        send.assert_called_once()
        return send.call_args.kwargs["payload"].get("badge_count")

    def _requests_for_other(self):
        # Team A: 1 request. Team B: 2 requests. Digests everywhere.
        self.inbox(JOIN_TEAM, receiver=self.other)
        for _ in range(2):
            self.inbox(JOIN_TEAM, receiver=self.other, team=self.team_b)
        for team in (self.team_a, self.team_b):
            for _ in range(4):
                self.inbox(DIGEST, receiver=self.other, team=team)

    def test_the_badge_follows_the_team_of_the_push(self):
        self._requests_for_other()
        for team_id, expected in (
            (self.team_a.team_id, 1),
            (self.team_b.team_id, 2),
            (None, 3),
        ):
            send = self._send(
                webpush_dispatch._queue_push,
                recipient_id=self.other.id,
                category="inbox",
                title="t",
                body="",
                url="/x",
                tag=f"t:{team_id}",
                team_id=team_id,
            )
            self.assertEqual(self._badge_of(send), expected, team_id)

    def test_an_activity_push_is_about_the_activity_team(self):
        self._requests_for_other()
        act = SimpleNamespace(
            id=uuid.uuid4(),
            activity_type=ActivityType.MENTION,
            recipient_id=self.other.id,
            team_id=self.team_b.team_id,
            actor=SimpleNamespace(username="Alice", profile_image_file_name=""),
            channel=SimpleNamespace(kind=ChannelKind.DM, id="chan-uuid"),
            message_id=uuid.uuid4(),
            message=SimpleNamespace(
                body_text="hi", metadata={}, thread_root_id=None, parent_id=None
            ),
            surface_type=None,
            meta={},
        )
        send = self._send(webpush_dispatch.dispatch_push_for_activities, [act])
        self.assertEqual(self._badge_of(send), 2)

    def test_an_inbox_item_push_is_about_the_item_team(self):
        self._requests_for_other()
        item = SimpleNamespace(
            item_id=1,
            item_type=JOIN_TEAM,
            receiver_id=self.other.id,
            team_id=self.team_b.team_id,
            sender=SimpleNamespace(username="Alice", profile_image_file_name=""),
        )
        send = self._send(webpush_dispatch.dispatch_push_for_inbox_item, item)
        self.assertEqual(self._badge_of(send), 2)

    def test_an_inbox_item_without_a_team_counts_every_team(self):
        self._requests_for_other()
        item = SimpleNamespace(
            item_id=1,
            item_type=JOIN_TEAM,
            receiver_id=self.other.id,
            team_id=None,
            sender=SimpleNamespace(username="Alice", profile_image_file_name=""),
        )
        send = self._send(webpush_dispatch.dispatch_push_for_inbox_item, item)
        self.assertEqual(self._badge_of(send), 3)

    def test_a_message_push_is_about_the_chat_team(self):
        self._requests_for_other()
        message = SimpleNamespace(
            channel=SimpleNamespace(
                kind=ChannelKind.DM, id="chan-uuid", title="", team_id=self.team_b.team_id
            ),
            sender=SimpleNamespace(username="Alice", profile_image_file_name=""),
            body_text="hello",
        )
        send = self._send(webpush_dispatch.dispatch_push_for_message, message, [self.other.id])
        self.assertEqual(self._badge_of(send), 2)

    def test_a_reminder_push_is_about_the_reminder_team(self):
        self._requests_for_other()
        send = self._send(
            webpush_dispatch.schedule_push_for_reminder,
            recipient_id=self.other.id,
            title="t",
            body="b",
            url="/x",
            tag="r:1",
            team_id=self.team_b.team_id,
        )
        self.assertEqual(self._badge_of(send), 2)

    def test_a_one_off_user_push_is_about_the_team_it_names(self):
        self._requests_for_other()
        send = self._send(
            webpush_dispatch.schedule_push_to_user,
            recipient_id=self.other.id,
            category="inbox",
            title="t",
            url="/x",
            team_id=self.team_a.team_id,
        )
        self.assertEqual(self._badge_of(send), 1)

    def test_callers_that_name_no_team_still_get_a_number(self):
        self._requests_for_other()
        send = self._send(
            webpush_dispatch.schedule_push_to_user,
            recipient_id=self.other.id,
            category="inbox",
            title="t",
            url="/x",
        )
        self.assertEqual(self._badge_of(send), 3)
