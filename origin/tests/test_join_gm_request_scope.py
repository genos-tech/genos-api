"""A join-GM request names a GM of the team it is filed in, under the GM's own name (MIP-1029).

`POST inbox/joinGMRequest/` checked the caller against the body's `team_id` and then looked the
GM up by id alone, so the GM could be in any team: a member of team A addressed a request to
the owner of a GM in team B, and on Approve (which checks the approver, not the requester's
team) was added to it. The `gm_name` it carries is shown in the approver's push and in the
approval's answer, and was whatever the requester wrote.

The sockets service sends the team the user is working in and the GM it found by searching that
team, with the GM's own title as the name, so an honest request is unchanged.
"""

from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.tests.test_base import BaseAPITestCase, User

JOIN_GM_REQ = "/api/v2/inbox/joinGMRequest/"
GM_APPROVE = "/api/v2/gm/join/fromInbox/"


class JoinGMRequestScopeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Lunch Club", owner=self.user
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.authenticate(self.user2)

    def body(self, gm=None, **optionals):
        gm = gm or self.gm
        return {
            "team_id": str(self.team.team_id),
            "item_body": [{"type": "paragraph", "content": []}],
            "item_type": 3,
            "item_optionals": {"gm_id": str(gm.id), "gm_name": gm.title, **optionals},
        }

    def test_a_gm_of_another_team_is_the_gm_404_and_writes_nothing(self):
        stranger = User.objects.create_user(username="b", email="b@example.com", password="pw")
        other = TeamMaster.objects.create(team_name="B", team_email="b@x.com", owner=stranger)
        TeamMembers.objects.create(team=other, attendee=stranger)
        foreign = Channel.objects.create(
            team=other, kind=ChannelKind.GM, title="Board", owner=stranger
        )
        ChannelMember.objects.create(channel=foreign, user=stranger, role="owner")
        res = self.client.post(JOIN_GM_REQ, self.body(foreign), format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND, res.content)
        self.assertEqual(res.json(), {"error": "GM not found or has no owner."})
        self.assertFalse(InboxItems.objects.exists())

    def test_the_name_in_the_request_is_the_gms_own(self):
        res = self.client.post(JOIN_GM_REQ, self.body(gm_name="Payroll Admins"), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        item = InboxItems.objects.get()
        self.assertEqual(item.item_optionals, {"gm_id": str(self.gm.id), "gm_name": "Lunch Club"})
        # And that is the name the approval reports.
        self.authenticate(self.user)
        res = self.client.post(GM_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["gmName"], "Lunch Club")

    def test_a_request_with_no_name_stays_without_one(self):
        body = self.body()
        del body["item_optionals"]["gm_name"]
        res = self.client.post(JOIN_GM_REQ, body, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(InboxItems.objects.get().item_optionals, {"gm_id": str(self.gm.id)})

    def test_any_spelling_of_the_teams_id_is_the_team(self):
        body = self.body()
        body["team_id"] = str(self.team.team_id).upper()
        res = self.client.post(JOIN_GM_REQ, body, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)

    def test_the_honest_request_is_otherwise_unchanged(self):
        res = self.client.post(JOIN_GM_REQ, self.body(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(res.json()["receiver"], str(self.user.id))
        self.assertEqual(res.json()["data"]["itemType"], 3)
        self.assertFalse(res.json()["alreadyExist"])
        again = self.client.post(JOIN_GM_REQ, self.body(), format="json")
        self.assertTrue(again.json()["alreadyExist"])
        self.assertEqual(InboxItems.objects.count(), 1)
