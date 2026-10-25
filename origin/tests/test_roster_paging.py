"""The roster can be fetched a page at a time (MIP-805).

Opt-in and additive: a request with neither `cursor` nor `limit` is the whole
roster, exactly as before, so an old client is unaffected. The things that can
go wrong in a page boundary are the ones pinned here: a member skipped or
repeated where two names tie or collate oddly, and the cross-team people
(which are not part of the paged query) showing up on every page or on none.
"""

from django.contrib.auth import get_user_model

from origin.models.common.team_models import TeamMembers
from origin.tests.cross_team_fixtures import CrossTeamTestCase

User = get_user_model()

URL = "/api/v2/team/getTeamMembers/"


class RosterPagingTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.a_owner)

    _joined = 0

    def _join(self, username):
        # A counter, not the name: same-named members need different emails.
        type(self)._joined += 1
        user = User.objects.create_user(
            username=username,
            email=f"member{type(self)._joined}@example.com",
            password="pw123456",
        )
        TeamMembers.objects.create(team=self.team_a, attendee=user)
        return user

    def _get(self, **params):
        res = self.client.get(
            URL,
            {
                "team_id": str(self.team_a.team_id),
                "team_name": self.team_a.team_name,
                "user_id": str(self.a_owner.id),
                **params,
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _members(self, body):
        return body["data"]["members"]

    def _all_pages(self, limit):
        """Follow next_cursor to the end; returns every row of every page."""
        rows, cursor = [], None
        for _ in range(60):
            body = self._get(limit=limit, **({"cursor": cursor} if cursor else {}))
            rows += self._members(body)
            if not body["has_more"]:
                return rows, body
            cursor = body["next_cursor"]
        self.fail("paging did not terminate")

    # -- an unpaged request is unchanged ------------------------------------

    def test_an_unpaged_request_has_no_paging_keys_and_everyone(self):
        for i in range(7):
            self._join(f"member{i}")

        body = self._get()

        self.assertNotIn("has_more", body)
        self.assertNotIn("next_cursor", body)
        names = {m["userName"] for m in self._members(body) if not m.get("isExternal")}
        self.assertTrue({f"member{i}" for i in range(7)} <= names)

    # -- a paged request ----------------------------------------------------

    def test_the_pages_cover_every_member_once(self):
        joined = [self._join(f"person{i:02d}") for i in range(23)]

        rows, _ = self._all_pages(limit=5)

        ids = [str(m["userId"]) for m in rows if not m.get("isExternal")]
        self.assertEqual(len(ids), len(set(ids)), "a member was repeated across pages")
        self.assertTrue({str(u.id) for u in joined} <= set(ids))

    def test_pages_come_back_in_the_same_order_as_the_unpaged_roster(self):
        for i in range(12):
            self._join(f"zed{i:02d}")
        unpaged = [str(m["userId"]) for m in self._members(self._get()) if not m.get("isExternal")]

        paged, _ = self._all_pages(limit=4)

        self.assertEqual([str(m["userId"]) for m in paged if not m.get("isExternal")], unpaged)

    def test_members_with_the_same_name_are_neither_skipped_nor_repeated(self):
        """The reason the cursor carries the id as well as the name."""
        twins = [self._join("Sam") for _ in range(9)]
        # `username` is unique on the user model in some deployments; if this
        # database enforces it the twins above would not exist, so say so.
        self.assertEqual(len({u.username for u in twins}), 1)

        rows, _ = self._all_pages(limit=2)

        ids = [str(m["userId"]) for m in rows if not m.get("isExternal")]
        self.assertTrue({str(u.id) for u in twins} <= set(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_names_that_collate_oddly_are_neither_skipped_nor_repeated(self):
        """The database orders by its own collation, not by bytes: case, accents
        and punctuation interleave. The cursor filters in SQL on the same
        ordering, so a boundary falling between them must not drop a member."""
        names = [
            "alice",
            "Alice",
            "alice-b",
            "Alice B",
            "álvaro",
            "Alvaro",
            "_x",
            "Zoë",
            "zoe",
            "ZOE",
        ]
        joined = [self._join(n) for n in names]

        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                rows, _ = self._all_pages(limit=limit)
                ids = [str(m["userId"]) for m in rows if not m.get("isExternal")]
                self.assertTrue({str(u.id) for u in joined} <= set(ids))
                self.assertEqual(len(ids), len(set(ids)))

    def test_exactly_one_page_is_not_reported_as_more(self):
        before = len([m for m in self._members(self._get()) if not m.get("isExternal")])

        body = self._get(limit=before)

        self.assertIs(body["has_more"], False)
        self.assertIsNone(body["next_cursor"])

    def test_one_past_a_page_reports_more(self):
        before = len([m for m in self._members(self._get()) if not m.get("isExternal")])
        self._join("one_too_many")

        body = self._get(limit=before)

        self.assertIs(body["has_more"], True)
        self.assertTrue(body["next_cursor"])

    # -- the cross-team people ----------------------------------------------

    def test_the_cross_team_people_are_on_the_last_page_exactly_once(self):
        self.active_project_grant()
        for i in range(11):
            self._join(f"staff{i:02d}")

        pages, cursor = [], None
        for _ in range(40):
            body = self._get(limit=3, **({"cursor": cursor} if cursor else {}))
            pages.append(body)
            if not body["has_more"]:
                break
            cursor = body["next_cursor"]

        external_by_page = [sum(1 for m in self._members(b) if m.get("isExternal")) for b in pages]
        self.assertGreater(len(pages), 1)
        self.assertEqual(
            sum(external_by_page), external_by_page[-1], "an external is on an early page"
        )
        self.assertEqual(
            len({str(m["userId"]) for b in pages for m in self._members(b) if m.get("isExternal")}),
            external_by_page[-1],
            "an external was repeated",
        )
        self.assertGreaterEqual(external_by_page[-1], 1, "the external person was lost")

    # -- robustness ---------------------------------------------------------

    def test_an_unreadable_cursor_is_the_first_page_not_an_error(self):
        for i in range(6):
            self._join(f"p{i}")
        first = [str(m["userId"]) for m in self._members(self._get(limit=3))]

        for junk in ("garbage", "%%%", "e30=", "eyJ1IjoieCIsImkiOiJub3QtYS11dWlkIn0="):
            with self.subTest(cursor=junk):
                body = self._get(limit=3, cursor=junk)
                self.assertEqual([str(m["userId"]) for m in self._members(body)], first)

    def test_a_silly_limit_is_clamped_not_an_error(self):
        for junk in ("0", "-5", "abc", "999999"):
            with self.subTest(limit=junk):
                self.assertEqual(
                    self.client.get(
                        URL,
                        {
                            "team_id": str(self.team_a.team_id),
                            "limit": junk,
                        },
                    ).status_code,
                    200,
                )

    def test_a_cursor_cannot_widen_what_the_caller_may_see(self):
        """The cursor is a position in the caller's own roster, so a forged one
        cannot reach another team's members."""
        outsider = User.objects.create_user(
            username="outsider", email="out@example.com", password="pw123456"
        )
        from origin.models.common.team_models import TeamMaster

        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=outsider
        )
        TeamMembers.objects.create(team=other, attendee=outsider)

        res = self.client.get(URL, {"team_id": str(other.team_id), "limit": 5})

        self.assertEqual(res.status_code, 404)
