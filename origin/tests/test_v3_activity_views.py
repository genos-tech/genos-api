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
