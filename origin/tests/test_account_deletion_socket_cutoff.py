"""Deleting an account takes the person's open sockets out of their rooms (MIP-1550).

Ending a membership does not end a socket that already joined a room: the deleted person's open
tab kept receiving the team's chats, and kept broadcasting `presence.ping` into its roster (the
sockets service only checks that the socket is already in the room), until it reconnected, which
it cannot do because the token dies with the account. `push_membership_removed` is the cut-off
team removal already uses; this pins that deleting the account uses it too, for the person's own
rooms only.
"""

from unittest import mock

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.project.prj_models import ProjectMembers
from origin.services.account_deletion import delete_account
from origin.tests.cross_team_fixtures import CrossTeamTestCase

PUSH = "origin.services.realtime_push.push_membership_removed"


class DeleteAccountCutsOffOpenSocketsTests(CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.leaver = self.a_viewer
        self.gm = Channel.objects.create(
            team=self.team_a, kind=ChannelKind.GM, title="Room", owner=self.a_owner
        )
        for user in (self.leaver, self.a_owner):
            ChannelMember.objects.create(channel=self.gm, user=user)
        ProjectMembers.objects.create(
            team=self.team_a, project=self.project, attendee=self.leaver, member_role="viewer"
        )

    def _calls(self, user):
        with mock.patch(PUSH) as push:
            delete_account(user)
        return [c.kwargs for c in push.call_args_list]

    def test_a_team_member_is_cut_off_from_the_team_its_projects_and_its_chats(self):
        calls = self._calls(self.leaver)
        mine = [c for c in calls if str(c["team_id"]) == str(self.team_a.team_id)]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["scope"], "team")
        self.assertEqual(str(mine[0]["user_id"]), str(self.leaver.id))
        self.assertIn(self.project.project_id, mine[0]["project_ids"])
        self.assertIn((int(ChannelKind.GM), str(self.gm.id)), mine[0]["channels"])

    def test_only_the_deleted_persons_rooms_are_named(self):
        calls = self._calls(self.leaver)
        self.assertTrue(calls)
        self.assertEqual({str(c["user_id"]) for c in calls}, {str(self.leaver.id)})
        for call in calls:
            self.assertNotIn(str(self.a_owner.id), str(call))

    def test_a_guest_of_another_team_is_cut_off_from_what_they_held_there(self):
        """`b_viewer` is on team B and holds a project and a chat of team A as a guest."""
        ProjectMembers.objects.create(
            team=self.team_a, project=self.project, attendee=self.b_viewer, member_role="guest"
        )
        ChannelMember.objects.create(channel=self.gm, user=self.b_viewer)
        calls = self._calls(self.b_viewer)
        by_team = {str(c["team_id"]): c for c in calls}
        self.assertEqual(by_team[str(self.team_b.team_id)]["scope"], "team")
        host = by_team[str(self.team_a.team_id)]
        self.assertEqual(host["scope"], "channel")  # not a member of team A: no team room
        self.assertIn(self.project.project_id, host["project_ids"])
        self.assertIn((int(ChannelKind.GM), str(self.gm.id)), host["channels"])

    def test_a_failed_erasure_pushes_nothing(self):
        """The push is scheduled on commit: a rolled-back deletion cuts nobody off."""
        with mock.patch(
            "origin.services.account_deletion._delete_invites_to", side_effect=ValueError
        ):
            with mock.patch("origin.services.realtime_push._post") as post:
                with self.settings(
                    SOCKETS_INTERNAL_URL="http://sockets.invalid", SOCKETS_INTERNAL_SECRET="s"
                ):
                    with self.assertRaises(ValueError):
                        with self.captureOnCommitCallbacks(execute=True):
                            delete_account(self.leaver)
        post.assert_not_called()
