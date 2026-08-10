"""Tests for the read-cursor → activity auto-clear behaviour.

`PUT /api/v3/channels/{id}/read_cursor/` advances the caller's read
pointer AND, as of this change, marks their unread channel activities
read once the cursor sweeps past the backing message — so viewing a
message clears its sidebar activity without a separate click.

The semantics under test (see `read_cursor_views._clear_activities_up_to`):
  - only the caller's own unread rows in THIS channel are touched;
  - only activities whose message `seq <= target.seq` (the cursor's
    forward-only comparator);
  - MAIN-timeline cursor clears only main-timeline activities; a THREAD
    cursor clears only that thread's activities;
  - REACTION activities are NEVER auto-cleared (a reaction to an old
    message points back at a low-seq message the cursor is already past);
  - surface activities (message IS NULL) are never touched;
  - the response echoes the cleared ids as `readActivityIds`.

Reuses the `_ChannelMixin` conventions from `test_views_gaps.py`
(GM channel to avoid the DM pair-uniqueness signal, ORM-inserted
Message rows with explicit seq). No external services — the read-cursor
path and the Activity update are pure DB.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
    ReadCursor,
)
from origin.tests.test_base import BaseAPITestCase


class _ChannelMixin:
    """Build a GM channel + messages + activities for the clear tests."""

    def _make_gm(self, *, members=("user",), owner="user"):
        owner_obj = getattr(self, owner)
        channel = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Clear Test GM",
            owner=owner_obj,
        )
        for name in members:
            user = getattr(self, name)
            ChannelMember.objects.create(
                channel=channel,
                user=user,
                role="owner" if name == owner else "member",
            )
        return channel

    def _make_message(self, channel, *, sender=None, seq=1, text="hello", thread_root=None):
        return Message.objects.create(
            channel=channel,
            sender=sender if sender is not None else self.user2,
            seq=seq,
            body={"text": text},
            body_text=text,
            thread_root=thread_root,
            is_thread_reply=thread_root is not None,
        )

    def _make_activity(
        self,
        *,
        channel,
        message,
        recipient=None,
        actor=None,
        activity_type=ActivityType.MESSAGE,
        is_read=False,
    ):
        return Activity.objects.create(
            team=self.team,
            recipient=recipient if recipient is not None else self.user,
            actor=actor if actor is not None else self.user2,
            activity_type=activity_type,
            channel=channel,
            message=message,
            is_read=is_read,
        )


class ReadCursorClearsActivitiesTests(_ChannelMixin, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # self.user reads; self.user2 is the actor/sender.
        self.channel = self._make_gm(members=("user", "user2"))
        self.m1 = self._make_message(self.channel, seq=1, text="one")
        self.m2 = self._make_message(self.channel, seq=2, text="two")
        self.m3 = self._make_message(self.channel, seq=3, text="three")
        self.url = reverse("v3_read_cursor", args=[self.channel.id])

    # ----- happy path ---------------------------------------------------
    def test_reading_up_to_seq_clears_activities_at_or_below(self):
        """Advancing the cursor to m2 clears the MESSAGE activities for m1
        and m2, but leaves m3 (seq=3 > 2) unread."""
        a1 = self._make_activity(channel=self.channel, message=self.m1)
        a2 = self._make_activity(channel=self.channel, message=self.m2)
        a3 = self._make_activity(channel=self.channel, message=self.m3)

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m2.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)

        # Response echoes exactly the cleared ids.
        self.assertCountEqual(resp.data["readActivityIds"], [str(a1.id), str(a2.id)])

        a1.refresh_from_db()
        a2.refresh_from_db()
        a3.refresh_from_db()
        self.assertTrue(a1.is_read)
        self.assertTrue(a2.is_read)
        self.assertFalse(a3.is_read)

    def test_first_advance_creates_cursor_and_clears(self):
        """The get_or_create first-advance path (no prior cursor row) still
        clears — the clear reads the cursor's final position, not just the
        forward-only branch."""
        a1 = self._make_activity(channel=self.channel, message=self.m1)
        self.assertFalse(
            ReadCursor.objects.filter(user=self.user, channel=self.channel).exists()
        )

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["readActivityIds"], [str(a1.id)])
        a1.refresh_from_db()
        self.assertTrue(a1.is_read)

    # ----- reaction exclusion ------------------------------------------
    def test_reaction_activity_is_never_auto_cleared(self):
        """A reaction to an OLD message mints a NEW activity pointing back
        at that low-seq message. Reading past it must NOT clear it — the
        recipient never saw the reaction notification."""
        reaction = self._make_activity(
            channel=self.channel, message=self.m1, activity_type=ActivityType.REACTION
        )
        mention = self._make_activity(
            channel=self.channel, message=self.m1, activity_type=ActivityType.MENTION
        )

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m3.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["readActivityIds"], [str(mention.id)])

        reaction.refresh_from_db()
        mention.refresh_from_db()
        self.assertFalse(reaction.is_read)  # reaction survives
        self.assertTrue(mention.is_read)  # mention cleared

    # ----- scoping ------------------------------------------------------
    def test_only_own_activities_cleared(self):
        """user2's activity in the same channel is untouched when user reads."""
        mine = self._make_activity(channel=self.channel, message=self.m1)
        theirs = self._make_activity(
            channel=self.channel, message=self.m1, recipient=self.user2, actor=self.user
        )

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m3.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["readActivityIds"], [str(mine.id)])

        mine.refresh_from_db()
        theirs.refresh_from_db()
        self.assertTrue(mine.is_read)
        self.assertFalse(theirs.is_read)

    def test_activity_in_other_channel_untouched(self):
        """Reading channel A leaves an unread activity in channel B alone."""
        other = self._make_gm(members=("user", "user2"))
        other_msg = self._make_message(other, seq=1)
        other_activity = self._make_activity(channel=other, message=other_msg)

        self.authenticate()
        self.client.put(self.url, {"last_read_message_id": str(self.m3.id)}, format="json")

        other_activity.refresh_from_db()
        self.assertFalse(other_activity.is_read)

    def test_already_read_activity_not_reported_again(self):
        """An already-read row is idempotently skipped and not echoed."""
        already = self._make_activity(channel=self.channel, message=self.m1, is_read=True)
        fresh = self._make_activity(channel=self.channel, message=self.m2)

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m3.id)}, format="json"
        )
        self.assertEqual(resp.data["readActivityIds"], [str(fresh.id)])
        already.refresh_from_db()
        self.assertTrue(already.is_read)

    # ----- thread scoping ----------------------------------------------
    def test_main_cursor_does_not_clear_thread_activities(self):
        """A thread reply (thread_root set) is not cleared by a main-timeline
        cursor advance, even though its seq is <= the cursor's seq."""
        reply = self._make_message(self.channel, seq=4, text="reply", thread_root=self.m1)
        main_act = self._make_activity(channel=self.channel, message=self.m1)
        thread_act = self._make_activity(
            channel=self.channel, message=reply, activity_type=ActivityType.THREAD_REPLY
        )

        self.authenticate()
        # Main-timeline cursor advanced past seq 4.
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m3.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        # Only the main-timeline activity is cleared.
        self.assertIn(str(main_act.id), resp.data["readActivityIds"])
        self.assertNotIn(str(thread_act.id), resp.data["readActivityIds"])

        thread_act.refresh_from_db()
        self.assertFalse(thread_act.is_read)

    def test_thread_cursor_clears_only_that_thread(self):
        """A thread cursor advance clears the thread's activities, not the
        main-timeline ones."""
        reply = self._make_message(self.channel, seq=4, text="reply", thread_root=self.m1)
        main_act = self._make_activity(channel=self.channel, message=self.m1)
        thread_act = self._make_activity(
            channel=self.channel, message=reply, activity_type=ActivityType.THREAD_REPLY
        )

        self.authenticate()
        resp = self.client.put(
            self.url,
            {
                "last_read_message_id": str(reply.id),
                "thread_root_id": str(self.m1.id),
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["readActivityIds"], [str(thread_act.id)])

        main_act.refresh_from_db()
        thread_act.refresh_from_db()
        self.assertFalse(main_act.is_read)  # main-timeline row survives
        self.assertTrue(thread_act.is_read)

    # ----- surface activities ------------------------------------------
    def test_surface_activity_without_message_untouched(self):
        """A surface activity (message IS NULL) can't be matched by the
        seq filter and is left unread."""
        surface = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            channel=None,
            message=None,
            surface_type=5,  # task body
            meta={"taskId": "T-1"},
        )

        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m3.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertNotIn(str(surface.id), resp.data["readActivityIds"])
        surface.refresh_from_db()
        self.assertFalse(surface.is_read)

    # ----- no-op --------------------------------------------------------
    def test_no_unread_activities_returns_empty_list(self):
        self.authenticate()
        resp = self.client.put(
            self.url, {"last_read_message_id": str(self.m1.id)}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["readActivityIds"], [])
