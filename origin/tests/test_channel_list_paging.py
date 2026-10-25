"""The chat list can be fetched a page at a time (MIP-808).

Opt-in and additive: a request with neither `cursor` nor `limit` is every
channel exactly as before, with no new keys. The chat list reconciles against
this response, so the failure that matters is a channel going missing or being
repeated at a page boundary. This view also has parts the others lack, each
pinned here: a DISTINCT over a membership join (a channel must not repeat),
a multi-part scope (the team's own, chats shared with it, and shared projects'
PM chats), and per-channel unread counts and latest messages that must be the
same on a page as they are in the full list.
"""

import uuid

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.cross_team_fixtures import CrossTeamTestCase
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

CHANNELS = "/api/v3/channels/"


class ChannelListPagingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.user)

    def _group(self, title="chat", team=None, member=None):
        channel = Channel.objects.create(
            team=team or self.team, kind=ChannelKind.GM, title=title, owner=self.user
        )
        ChannelMember.objects.create(channel=channel, user=member or self.user)
        return channel

    def _get(self, **params):
        res = self.client.get(CHANNELS, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200, res.content[:300])
        return res.json()

    def _all_pages(self, limit):
        rows, cursor = [], None
        for _ in range(60):
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}))
            rows += body["channels"]
            if not body["has_more"]:
                return rows, body
            cursor = body["next_cursor"]
        self.fail("paging did not terminate")

    # -- an unpaged request is unchanged ------------------------------------

    def test_an_unpaged_request_has_no_paging_keys_and_every_channel(self):
        made = [self._group(f"c{i}") for i in range(7)]

        body = self._get()

        self.assertNotIn("has_more", body)
        self.assertNotIn("next_cursor", body)
        self.assertEqual({c["id"] for c in body["channels"]}, {str(m.id) for m in made})

    def test_an_unpaged_empty_list_is_unchanged(self):
        body = self._get()

        self.assertEqual(body, {"channels": []})

    # -- a paged request ----------------------------------------------------

    def test_the_pages_cover_every_channel_once(self):
        made = [self._group(f"c{i:02d}") for i in range(23)]

        rows, _ = self._all_pages(limit=5)

        ids = [r["id"] for r in rows]
        self.assertEqual(len(ids), len(set(ids)), "a channel was repeated across pages")
        self.assertEqual(set(ids), {str(m.id) for m in made})

    def test_the_pages_are_in_id_order(self):
        for i in range(12):
            self._group(f"c{i}")

        rows, _ = self._all_pages(limit=4)

        ids = [r["id"] for r in rows]
        self.assertEqual(ids, sorted(ids))

    def test_exactly_one_page_is_not_reported_as_more(self):
        for i in range(5):
            self._group(f"c{i}")

        body = self._get(limit=5)

        self.assertEqual(len(body["channels"]), 5)
        self.assertIs(body["has_more"], False)
        self.assertIsNone(body["next_cursor"])

    def test_one_past_a_page_reports_more(self):
        for i in range(6):
            self._group(f"c{i}")

        body = self._get(limit=5)

        self.assertEqual(len(body["channels"]), 5)
        self.assertIs(body["has_more"], True)
        self.assertTrue(body["next_cursor"])

    def test_a_cursor_past_the_end_is_an_empty_final_page(self):
        self._group("only")
        last = "ffffffff-ffff-ffff-ffff-ffffffffffff"

        body = self._get(limit=5, cursor=last)

        self.assertEqual(body, {"channels": [], "has_more": False, "next_cursor": None})

    def test_a_paged_read_is_ordered_by_id(self):
        """The cursor is the channel id, so the page must be read in id order:
        without an ORDER BY a page is whatever the database returns first, and a
        boundary can skip or repeat channels once the list is big enough to be
        read out of order. This view's DISTINCT makes it worse, since the
        planner may sort or hash for it and neither is the order the cursor needs.

        A bare read on a small test table returns a stable order anyway (the
        same was measured for the inbox), so a behavioural test cannot expose
        the missing ORDER BY. This checks the query itself, as MIP-802 does."""
        from unittest import mock

        from django.db.models.query import QuerySet

        seen = []
        real = QuerySet.__getitem__

        def spy(qs, key):
            if getattr(qs, "model", None) is Channel and isinstance(key, slice):
                seen.append(tuple(qs.query.order_by))
            return real(qs, key)

        for i in range(3):
            self._group(f"c{i}")
        with mock.patch.object(QuerySet, "__getitem__", spy):
            self._get(limit=2)

        self.assertIn(("id",), seen)

    # -- the parts of this view that the others do not have -----------------

    def test_channels_I_am_not_in_never_appear(self):
        mine = [self._group(f"mine{i}") for i in range(3)]
        theirs = [self._group(f"theirs{i}", member=self.user2) for i in range(3)]
        for t in theirs:
            ChannelMember.objects.filter(channel=t, user=self.user).delete()

        rows, _ = self._all_pages(limit=2)

        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {str(m.id) for m in mine})
        self.assertFalse(ids & {str(t.id) for t in theirs})

    def test_the_team_narrowing_still_applies_to_every_page(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        here = [self._group(f"h{i}") for i in range(5)]
        there = [self._group(f"t{i}", team=other) for i in range(5)]

        rows, _ = self._all_pages(limit=2)

        ids = {r["id"] for r in rows}
        self.assertEqual(ids, {str(h.id) for h in here})
        self.assertFalse(ids & {str(t.id) for t in there})

    def test_each_channel_on_a_page_equals_the_same_channel_in_the_full_list(self):
        """The unread count and the latest message are computed per channel. A
        page must carry the same values the unpaged list does."""
        channels = [self._group(f"c{i}") for i in range(6)]
        for n, channel in enumerate(channels):
            for seq in range(1, n + 2):
                Message.objects.create(
                    channel=channel,
                    sender=self.user2,
                    seq=seq,
                    body=[{"type": "paragraph", "content": f"m{seq}"}],
                    body_text=f"m{seq}",
                )

        full = {c["id"]: c for c in self._get()["channels"]}
        paged, _ = self._all_pages(limit=2)

        self.assertEqual(len(paged), len(full))
        for row in paged:
            self.assertEqual(row, full[row["id"]], f"{row['id']} differs on a page")

    # -- robustness ---------------------------------------------------------

    def test_an_unreadable_cursor_is_the_first_page_not_an_error(self):
        for i in range(6):
            self._group(f"c{i}")
        first = [c["id"] for c in self._get(limit=3)["channels"]]

        for junk in ("garbage", "%%%", "123", "not-a-uuid", str(uuid.uuid4())[:-1]):
            with self.subTest(cursor=junk):
                self.assertEqual(
                    [c["id"] for c in self._get(limit=3, cursor=junk)["channels"]], first
                )

    def test_a_silly_limit_is_clamped_not_an_error(self):
        self._group("one")
        for junk in ("0", "-5", "abc", "999999"):
            with self.subTest(limit=junk):
                res = self.client.get(CHANNELS, {"team_id": str(self.team.team_id), "limit": junk})
                self.assertEqual(res.status_code, 200)

    def test_a_cursor_cannot_reach_a_channel_I_am_not_in(self):
        mine = self._group("mine")
        theirs = self._group("theirs", member=self.user2)
        ChannelMember.objects.filter(channel=theirs, user=self.user).delete()
        just_below = str(uuid.UUID(int=max(0, min(mine.id.int, theirs.id.int) - 1)))

        rows = self._get(limit=10, cursor=just_below)["channels"]

        self.assertNotIn(str(theirs.id), {r["id"] for r in rows})


class SharedChatPagingTests(CrossTeamTestCase):
    """A guest's chat list holds a channel that does not belong to the team
    being viewed: the PM chat of a project another team shared with it. That
    arrives through the scope's OR-branches, not the plain `team_id` filter, so
    paging has to keep it exactly once and keep the host's other chats out."""

    def setUp(self):
        super().setUp()
        self.grant = self.active_project_grant()

    def _guest_pages(self, limit):
        self.authenticate(self.b_owner)
        rows, cursor = [], None
        for _ in range(40):
            res = self.client.get(
                CHANNELS,
                {
                    "team_id": str(self.team_b.team_id),
                    "limit": limit,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            self.assertEqual(res.status_code, 200, res.content[:300])
            body = res.json()
            rows += body["channels"]
            if not body["has_more"]:
                return rows
            cursor = body["next_cursor"]
        self.fail("paging did not terminate")

    def _own_chats(self, count):
        made = []
        for i in range(count):
            channel = Channel.objects.create(
                team=self.team_b, kind=ChannelKind.GM, title=f"b{i}", owner=self.b_owner
            )
            ChannelMember.objects.create(channel=channel, user=self.b_owner)
            made.append(channel)
        return made

    def test_the_shared_projects_chat_appears_exactly_once_whatever_the_page_size(self):
        own = self._own_chats(7)
        self.authenticate(self.b_owner)
        full = {
            c["id"]
            for c in self.client.get(CHANNELS, {"team_id": str(self.team_b.team_id)}).json()[
                "channels"
            ]
        }
        shared = full - {str(c.id) for c in own}
        self.assertTrue(shared, "the fixture should put a shared chat in the guest's list")

        for limit in (1, 2, 3, 100):
            with self.subTest(limit=limit):
                ids = [r["id"] for r in self._guest_pages(limit)]
                self.assertEqual(len(ids), len(set(ids)))
                self.assertEqual(set(ids), full)

    def test_the_hosts_other_chats_stay_out_of_every_page(self):
        self._own_chats(3)
        host_only = Channel.objects.create(
            team=self.team_a, kind=ChannelKind.GM, title="host only", owner=self.a_owner
        )
        ChannelMember.objects.create(channel=host_only, user=self.a_owner)

        for limit in (1, 2, 50):
            with self.subTest(limit=limit):
                ids = {r["id"] for r in self._guest_pages(limit)}
                self.assertNotIn(str(host_only.id), ids)
