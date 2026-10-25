"""The inbox can be fetched a page at a time (MIP-806).

Opt-in and additive: a request with neither `cursor` nor `limit` is the whole
inbox exactly as before, with no new keys. `item_id` is an auto-increment
primary key, so the cursor is exact: no ties to break and no collation to
match. The things worth pinning are the page boundary, that an incremental
sync still returns tombstones across pages, and that nobody's cursor reaches
anyone else's items.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

URL = "/api/v2/inbox/"


class InboxPagingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.sender = User.objects.create_user(
            username="inboxsender", email="inboxsender@example.com", password="pw123456"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.sender)
        self.authenticate(self.user)

    def _item(self, receiver=None, **kw):
        return InboxItems.objects.create(
            team=self.team,
            sender=self.sender,
            receiver=receiver or self.user,
            item_type=1,
            item_body="hello",
            **kw,
        )

    def _get(self, **params):
        res = self.client.get(
            URL, {"team_id": str(self.team.team_id), "user_id": str(self.user.id), **params}
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _items(self, body):
        return body["data"]["items"]

    def _all_pages(self, limit, **params):
        rows, cursor = [], None
        for _ in range(60):
            body = self._get(limit=limit, **params, **({"cursor": cursor} if cursor else {}))
            rows += self._items(body)
            if not body["has_more"]:
                return rows, body
            cursor = body["next_cursor"]
        self.fail("paging did not terminate")

    # -- an unpaged request is unchanged ------------------------------------

    def test_an_unpaged_request_has_no_paging_keys_and_everything(self):
        made = [self._item() for _ in range(7)]

        body = self._get()

        self.assertNotIn("has_more", body)
        self.assertNotIn("next_cursor", body)
        self.assertEqual({i["itemId"] for i in self._items(body)}, {m.item_id for m in made})

    # -- a paged request ----------------------------------------------------

    def test_the_pages_cover_every_item_once_in_order(self):
        made = [self._item() for _ in range(23)]

        rows, _ = self._all_pages(limit=5)

        ids = [r["itemId"] for r in rows]
        self.assertEqual(ids, [m.item_id for m in made])
        self.assertEqual(len(ids), len(set(ids)))

    def test_exactly_one_page_is_not_reported_as_more(self):
        for _ in range(5):
            self._item()

        body = self._get(limit=5)

        self.assertEqual(len(self._items(body)), 5)
        self.assertIs(body["has_more"], False)
        self.assertIsNone(body["next_cursor"])

    def test_one_past_a_page_reports_more_and_returns_a_full_page(self):
        for _ in range(6):
            self._item()

        body = self._get(limit=5)

        self.assertEqual(len(self._items(body)), 5)
        self.assertIs(body["has_more"], True)
        self.assertTrue(body["next_cursor"])

    def test_the_cursor_is_the_last_item_returned(self):
        made = [self._item() for _ in range(6)]

        body = self._get(limit=4)

        self.assertEqual(body["next_cursor"], str(made[3].item_id))
        self.assertEqual(
            self._items(self._get(limit=4, cursor=body["next_cursor"]))[0]["itemId"],
            made[4].item_id,
        )

    def test_a_paged_read_is_ordered_by_the_primary_key(self):
        """The cursor is `item_id`, so the page must be read in `item_id` order:
        without an ORDER BY a page is whatever the database returns first, and a
        boundary can skip or repeat rows once the table is big enough to be read
        out of insertion order.

        That cannot be forced to differ on a small test table (a bare scan here
        returns insertion order even after UPDATEs, which I measured), so this
        checks the query itself, as the MIP-802 test does."""
        from unittest import mock

        from django.db.models.query import QuerySet

        seen = []
        real = QuerySet.__getitem__

        def spy(qs, key):
            if getattr(qs, "model", None) is InboxItems and isinstance(key, slice):
                seen.append(tuple(qs.query.order_by))
            return real(qs, key)

        for _ in range(3):
            self._item()
        with mock.patch.object(QuerySet, "__getitem__", spy):
            self._get(limit=2)

        self.assertIn(("item_id",), seen)

    # -- delta behaviour is preserved across pages --------------------------

    def test_a_full_load_still_hides_deleted_items_on_every_page(self):
        keep = [self._item() for _ in range(6)]
        gone = self._item(is_deleted=True)

        rows, _ = self._all_pages(limit=2)

        ids = {r["itemId"] for r in rows}
        self.assertNotIn(gone.item_id, ids)
        self.assertEqual(ids, {k.item_id for k in keep})

    def test_an_incremental_sync_still_returns_tombstones_across_pages(self):
        since = (timezone.now() - timedelta(minutes=5)).isoformat()
        live = [self._item() for _ in range(4)]
        dead = [self._item(is_deleted=True) for _ in range(3)]

        rows, _ = self._all_pages(limit=2, since=since)

        by_id = {r["itemId"]: r for r in rows}
        self.assertEqual(set(by_id), {i.item_id for i in live + dead})
        for item in dead:
            self.assertTrue(by_id[item.item_id]["isDeleted"])

    def test_every_page_carries_the_same_force_full_flag(self):
        for _ in range(5):
            self._item()
        stale = (timezone.now() - timedelta(days=400)).isoformat()

        flags = []
        cursor = None
        for _ in range(10):
            body = self._get(limit=2, since=stale, **({"cursor": cursor} if cursor else {}))
            flags.append(bool(body.get("force_full_reload")))
            if not body["has_more"]:
                break
            cursor = body["next_cursor"]

        self.assertGreater(len(flags), 1)
        self.assertEqual(len(set(flags)), 1, "force_full_reload differed between pages")

    # -- isolation ----------------------------------------------------------

    def test_another_users_items_never_appear(self):
        mine = [self._item() for _ in range(3)]
        theirs = [self._item(receiver=self.user2) for _ in range(3)]

        rows, _ = self._all_pages(limit=2)

        ids = {r["itemId"] for r in rows}
        self.assertEqual(ids, {m.item_id for m in mine})
        self.assertFalse(ids & {t.item_id for t in theirs})

    def test_a_forged_cursor_cannot_reach_another_users_items(self):
        for _ in range(2):
            self._item()
        theirs = self._item(receiver=self.user2)

        body = self._get(limit=1, cursor=str(theirs.item_id - 1))

        self.assertNotIn(theirs.item_id, {i["itemId"] for i in self._items(body)})

    # -- robustness ---------------------------------------------------------

    def test_an_unreadable_cursor_is_the_first_page_not_an_error(self):
        made = [self._item() for _ in range(5)]

        for junk in ("garbage", "-3", "1.5", "e30=", "%%%"):
            with self.subTest(cursor=junk):
                body = self._get(limit=3, cursor=junk)
                self.assertEqual(
                    [i["itemId"] for i in self._items(body)], [m.item_id for m in made[:3]]
                )

    def test_a_silly_limit_is_clamped_not_an_error(self):
        for _ in range(3):
            self._item()
        for junk in ("0", "-5", "abc", "999999"):
            with self.subTest(limit=junk):
                res = self.client.get(URL, {"team_id": str(self.team.team_id), "limit": junk})
                self.assertEqual(res.status_code, 200)
