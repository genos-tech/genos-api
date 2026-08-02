"""The chat list belongs to one team.

`ChannelListView.get` supported `?team_id=` but the chat sidebar did not
send it, on the documented reasoning that the sidebar "wants every team
at once". A channel belongs to exactly one team — `Channel.team` is
non-null and DMs carry it too — so that reasoning produced a real bug:
switching from team A to team B left team A's chats on screen, because
the refresh that followed the switch returned both teams' channels.

Reproduced against the dev database before the fix: one user in two
teams got 18 channels unnarrowed, 17 for one team and 1 for the other.
Whichever team they switched into, they saw all 18.

The second test covers the crash this newly exposed. `Channel.team`
points at a UUID column, so `filter(team_id="abc")` raises
`ValidationError` out of the ORM — a 500 on request input. That was
unreachable-in-practice while only the webhook scope picker sent the
parameter; it is on every chat-list load now, so it has to be parsed.
"""

from django.contrib.auth import get_user_model

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

CHANNELS = "/api/v3/channels/"


class ChannelListTeamScopeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The same user in a SECOND team — the shape the bug needs. A
        # user in one team could never have observed it.
        self.other_team = TeamMaster.objects.create(
            team_name="CLTS Second",
            team_email="clts-second@example.com",
            owner=self.user,
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)

        self.here = self._group(self.team, "in the first team")
        self.there = self._group(self.other_team, "in the second team")

    def _group(self, team, title):
        channel = Channel.objects.create(team=team, kind=ChannelKind.GM, title=title)
        ChannelMember.objects.create(channel=channel, user=self.user)
        return channel

    def _ids(self, response):
        return {row["id"] for row in response.json()["channels"]}

    def test_team_id_returns_only_that_team(self):
        self.authenticate(self.user)

        first = self.client.get(f"{CHANNELS}?team_id={self.team.team_id}")
        self.assertEqual(first.status_code, 200)
        self.assertIn(str(self.here.id), self._ids(first))
        # The actual bug: the other team's channel came back too.
        self.assertNotIn(str(self.there.id), self._ids(first))

        second = self.client.get(f"{CHANNELS}?team_id={self.other_team.team_id}")
        self.assertEqual(second.status_code, 200)
        self.assertIn(str(self.there.id), self._ids(second))
        self.assertNotIn(str(self.here.id), self._ids(second))

    def test_unnarrowed_still_returns_every_team(self):
        # The compatibility path. Kept deliberately: this is what any
        # caller that hasn't been updated still gets, and asserting it
        # documents that the fix is in the CLIENT sending the parameter,
        # not in the endpoint refusing to answer without it.
        self.authenticate(self.user)
        res = self.client.get(CHANNELS)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            {str(self.here.id), str(self.there.id)} & self._ids(res),
            {str(self.here.id), str(self.there.id)},
        )

    def test_a_team_you_are_not_in_yields_nothing(self):
        stranger = User.objects.create_user(
            username="cltsx", email="cltsx@example.com", password="pw"
        )
        foreign = TeamMaster.objects.create(
            team_name="CLTS Foreign", team_email="cltsx@example.com", owner=stranger
        )
        TeamMembers.objects.create(team=foreign, attendee=stranger)
        theirs = Channel.objects.create(team=foreign, kind=ChannelKind.GM, title="theirs")
        ChannelMember.objects.create(channel=theirs, user=stranger)

        self.authenticate(self.user)
        res = self.client.get(f"{CHANNELS}?team_id={foreign.team_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["channels"], [])

    def test_a_malformed_team_id_is_not_a_500(self):
        self.authenticate(self.user)
        for bad in ("abc", "1", "not-a-uuid", "../../etc/passwd", "%00"):
            with self.subTest(team_id=bad):
                res = self.client.get(f"{CHANNELS}?team_id={bad}")
                self.assertEqual(res.status_code, 200, f"{bad!r} should not crash")
                # Same answer as a team that exists but isn't yours: an
                # unparseable id names nothing.
                self.assertEqual(res.json()["channels"], [])

    def test_an_empty_team_id_falls_back_to_every_team(self):
        # `?team_id=` (present but blank) is falsy, so it must behave
        # like the parameter being absent rather than matching no team —
        # otherwise a client that always appends the key would blank the
        # sidebar whenever the team hasn't resolved yet.
        self.authenticate(self.user)
        res = self.client.get(f"{CHANNELS}?team_id=")
        self.assertEqual(res.status_code, 200)
        self.assertIn(str(self.here.id), self._ids(res))
