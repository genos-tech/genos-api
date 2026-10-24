"""Tests for `GET /api/v3/activities/` — the activity-feed list endpoint.

The case that matters here is the two-team recipient: someone who
belongs to team A and team B has entries in both, and the sidebar shows
one team at a time, so the endpoint has to be narrowable. Before
`?team_id=` existed the feed returned every team's entries at once and
team A's mentions showed up while team B was on screen.
"""

import uuid

from origin.models.chat.unified_models import Activity, ActivityType, Channel, ChannelKind
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.test_base import BaseAPITestCase


class ActivityListTeamScopeTests(BaseAPITestCase):
    """`self.user` is in `self.team` (from the base fixtures) and in a
    second team; both teams have an activity addressed to them."""

    URL = "/api/v3/activities/"

    def setUp(self):
        super().setUp()
        self.other_team = TeamMaster.objects.create(
            team_name="Other Team",
            team_email="other-team@example.com",
            owner=self.user2,
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        TeamMembers.objects.create(team=self.other_team, attendee=self.user2)

        self.here = self._activity(self.team)
        self.there = self._activity(self.other_team)
        self.authenticate()

    def _activity(self, team):
        channel = Channel.objects.create(
            team=team,
            kind=ChannelKind.GM,
            title=f"{team.team_name} chat",
            owner=self.user2,
        )
        return Activity.objects.create(
            team=team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            channel=channel,
        )

    def _ids(self, response):
        return {row["id"] for row in response.json()["activities"]}

    def test_narrows_to_the_named_team(self):
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._ids(res), {str(self.here.id)})

    def test_the_other_team_gets_its_own_entries(self):
        res = self.client.get(self.URL, {"team_id": str(self.other_team.team_id)})
        self.assertEqual(self._ids(res), {str(self.there.id)})

    def test_every_row_carries_its_team(self):
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id)})
        row = res.json()["activities"][0]
        self.assertEqual(row["teamId"], str(self.team.team_id))

    def test_a_team_you_have_no_entries_in_returns_nothing(self):
        stranger_team = TeamMaster.objects.create(
            team_name="Stranger Team",
            team_email="stranger@example.com",
            owner=self.user2,
        )
        res = self.client.get(self.URL, {"team_id": str(stranger_team.team_id)})
        self.assertEqual(self._ids(res), set())

    def test_an_unparseable_team_id_is_empty_not_a_500(self):
        res = self.client.get(self.URL, {"team_id": "not-a-uuid"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._ids(res), set())

    def test_an_unknown_but_valid_team_id_is_empty(self):
        res = self.client.get(self.URL, {"team_id": str(uuid.uuid4())})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._ids(res), set())

    def test_omitting_team_id_still_returns_everything(self):
        # The compatibility path: a client that hasn't been updated yet
        # keeps working, it just isn't narrowed.
        res = self.client.get(self.URL)
        self.assertEqual(self._ids(res), {str(self.here.id), str(self.there.id)})

    def test_someone_elses_entries_are_never_returned(self):
        theirs = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
        )
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id)})
        self.assertNotIn(str(theirs.id), self._ids(res))


class SurfaceActivityActorGuardTests(BaseAPITestCase):
    """`POST /api/v3/activities/surface/` and `/surface-comment/` refuse a
    caller outside the team — and, since MIP-716, let in a team owner who
    has no `TeamMembers` row. The old bare `TeamMembers` query refused
    them in their own team; `is_team_member` counts ownership."""

    URLS = ("/api/v3/activities/surface/", "/api/v3/activities/surface-comment/")

    def _body(self, team):
        return {
            "surface_type": 5,
            "team_id": str(team.team_id),
            "entity_key": "task:1",
            "newly_mentioned_user_ids": [],
            "meta": {},
        }

    def test_owner_without_a_member_row_is_let_in(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        self.authenticate(self.user)
        for url in self.URLS:
            with self.subTest(url=url):
                res = self.client.post(url, self._body(self.team), format="json")
                self.assertEqual(res.status_code, 201)

    def test_outsider_is_refused(self):
        stranger_team = TeamMaster.objects.create(
            team_name="Stranger", team_email="s@example.com", owner=self.user2
        )
        self.authenticate(self.user)
        for url in self.URLS:
            with self.subTest(url=url):
                res = self.client.post(url, self._body(stranger_team), format="json")
                self.assertEqual(res.status_code, 403)


class ActivityListPagingTests(BaseAPITestCase):
    """A page is the newest `_MAX_ROWS`, and the response says whether there is
    more (MIP-740). It used to cut silently: the client stores `server_time` as
    its next `since`, so everything past the cut was lost for good."""

    URL = "/api/v3/activities/"

    def setUp(self):
        super().setUp()
        from origin.views.chat import activity_views_v3

        self.limit = activity_views_v3._MAX_ROWS
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="chat", owner=self.user2
        )
        self.authenticate()

    def _make(self, count, *, same_instant=False):
        """`count` activities for self.user. With `same_instant` they all share one
        timestamp, which is what a cursor on the timestamp alone gets wrong."""
        from datetime import timedelta

        from django.utils import timezone

        base = timezone.now() - timedelta(hours=1)
        rows = [
            Activity.objects.create(
                team=self.team,
                recipient=self.user,
                actor=self.user2,
                activity_type=ActivityType.MENTION,
                channel=self.channel,
            )
            for _ in range(count)
        ]
        for i, row in enumerate(rows):
            when = base if same_instant else base + timedelta(seconds=i)
            Activity.objects.filter(pk=row.pk).update(ts_created_at=when)
        return rows

    def _get(self, **params):
        res = self.client.get(self.URL, {"team_id": str(self.team.team_id), **params})
        self.assertEqual(res.status_code, 200)
        return res.json()

    def _all_pages(self, **params):
        """Follow next_cursor to the end; returns every id in order."""
        seen, cursor = [], None
        for _ in range(20):
            body = self._get(**params, **({"cursor": cursor} if cursor else {}))
            seen += [row["id"] for row in body["activities"]]
            if not body["has_more"]:
                return seen, body
            cursor = body["next_cursor"]
        self.fail("paging did not terminate")

    def test_a_short_list_says_there_is_no_more(self):
        self._make(5)

        body = self._get()

        self.assertEqual(len(body["activities"]), 5)
        self.assertIs(body["has_more"], False)
        self.assertIsNone(body["next_cursor"])

    def test_exactly_one_page_is_not_reported_as_more(self):
        """The off-by-one that `_MAX_ROWS + 1` exists to get right."""
        self._make(self.limit)

        body = self._get()

        self.assertEqual(len(body["activities"]), self.limit)
        self.assertIs(body["has_more"], False)

    def test_one_past_a_page_reports_more_and_returns_a_full_page(self):
        self._make(self.limit + 1)

        body = self._get()

        self.assertEqual(len(body["activities"]), self.limit)
        self.assertIs(body["has_more"], True)
        self.assertTrue(body["next_cursor"])

    def test_the_pages_cover_every_row_once_in_newest_first_order(self):
        rows = self._make(self.limit + 37)

        seen, _ = self._all_pages()

        self.assertEqual(len(seen), len(rows))
        self.assertEqual(len(set(seen)), len(rows), "a row was repeated across pages")
        self.assertEqual(set(seen), {str(r.id) for r in rows})
        self.assertEqual(seen, [str(r.id) for r in reversed(rows)], "not newest first")

    def test_rows_sharing_a_timestamp_are_neither_skipped_nor_repeated(self):
        """The reason the cursor carries the id as well as the time."""
        rows = self._make(self.limit + 25, same_instant=True)

        seen, _ = self._all_pages()

        self.assertEqual(len(seen), len(rows))
        self.assertEqual(set(seen), {str(r.id) for r in rows})

    def test_a_request_without_a_cursor_is_the_first_page_as_before(self):
        """An old client sends no cursor and must keep getting the newest page."""
        rows = self._make(self.limit + 10)

        body = self._get()

        newest = [str(r.id) for r in reversed(rows)][: self.limit]
        self.assertEqual([row["id"] for row in body["activities"]], newest)
        self.assertEqual(
            {"activities", "since", "server_time"} - set(body), set(), "an old key went missing"
        )

    def test_an_unreadable_cursor_is_the_first_page_not_an_error(self):
        self._make(self.limit + 3)

        for junk in ("garbage", "%%%", "e30=", "eyJ0IjoiPyIsImkiOiI/In0="):
            with self.subTest(cursor=junk):
                body = self._get(cursor=junk)
                self.assertEqual(len(body["activities"]), self.limit)

    def test_a_cursor_cannot_reach_another_users_rows(self):
        """The cursor is a position, not an id list: it filters the caller's own
        rows, so a forged one can't read anyone else's."""
        self._make(3)
        other = Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=self.channel,
        )

        seen, _ = self._all_pages()

        self.assertNotIn(str(other.id), seen)
