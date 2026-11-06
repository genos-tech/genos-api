"""A request item has the type of the endpoint that made it, never the body's (MIP-1029).

`POST inbox/joinGMRequest/` built the item with `"item_type": request.data["item_type"]`, where
the team and project requests hard-code theirs (MIP-940). Nothing checked it was 3, so a caller
could file an item of ANY type addressed to the owner of any GM they named, wearing whatever
`item_body` they liked. The approvals check the item's type (MIP-1014), which limits what a
fabricated "project request" or "note access request" can do, but two types were still live:

* type 5, a team-ownership claim, which the CLAIMANT may finalize on their own once the
  `deadline` in `item_optionals` has passed. The deadline is a body field here, so a team
  editor could file a claim with a deadline of last year and take the team at once, without
  the owner's 30 days to answer or the owner ever being told;
* type 6, the Genos digest, which is system-authored (`sender` is NULL) and renders as such.

Each test checks the item that was written, not just the status.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import EDITOR
from origin.tests.test_base import BaseAPITestCase

JOIN_GM_REQ = "/api/v2/inbox/joinGMRequest/"
FINALIZE = "/api/v2/team/ownership-claim/finalize/"
PROJECT_APPROVE = "/api/v2/project/join/fromInbox/"

# What a body could say. Text, a list, an object and `null` are 500s or 400s before this fix; the
# numbers are the types that exist (0 notice, 1 team, 2 project, 4 note access, 5 ownership claim,
# 6 digest, 7 team connection, 8 external share) and some that do not.
FORGED_TYPES = (0, 1, 2, 4, 5, 6, 7, 8, 99, -5, "1", "5", True, "abc", [1], {"a": 1}, None)


class JoinGMRequestTypeTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # The caller is an EDITOR of the team (the lowest role that may claim ownership),
        # and owns a GM of their own, so the item is addressed to them.
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(member_role=EDITOR)
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Mine", owner=self.user2
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="owner")
        self.authenticate(self.user2)

    def body(self, **over):
        body = {
            "team_id": str(self.team.team_id),
            "item_body": [{"type": "paragraph", "content": []}],
            "item_type": 3,
            "item_optionals": {"gm_id": str(self.gm.id), "gm_name": "Mine"},
        }
        body.update(over)
        return {k: v for k, v in body.items() if v is not ...}

    def test_the_item_is_a_join_gm_request_whatever_the_body_says(self):
        for forged in FORGED_TYPES:
            with self.subTest(item_type=forged):
                InboxItems.objects.all().delete()
                res = self.client.post(JOIN_GM_REQ, self.body(item_type=forged), format="json")
                self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
                self.assertEqual(res.json()["data"]["itemType"], 3)
                item = InboxItems.objects.get()
                self.assertEqual(item.item_type, 3)
                self.assertEqual(item.sender_id, self.user2.id)

    def test_a_body_without_an_item_type_is_the_same_request(self):
        res = self.client.post(JOIN_GM_REQ, self.body(item_type=...), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        self.assertEqual(InboxItems.objects.get().item_type, 3)

    def test_the_documented_request_is_unchanged(self):
        res = self.client.post(JOIN_GM_REQ, self.body(), format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        item = InboxItems.objects.get()
        self.assertEqual(
            (item.item_type, item.receiver_id, item.team_id, item.request_status),
            (3, self.user2.id, self.team.team_id, "pending"),
        )
        self.assertEqual(item.item_optionals, {"gm_id": str(self.gm.id), "gm_name": "Mine"})
        self.assertFalse(res.json()["alreadyExist"])

    def test_a_forged_ownership_claim_cannot_take_the_team(self):
        # The attack: an editor files "item_type 5" with a deadline in the past, then finalizes it.
        res = self.client.post(
            JOIN_GM_REQ,
            self.body(
                item_type=5,
                item_optionals={
                    "gm_id": str(self.gm.id),
                    "kind": "team_ownership_claim",
                    "deadline": "2000-01-01T00:00:00+00:00",
                },
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        item = InboxItems.objects.get()
        res = self.client.post(FINALIZE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND, res.content)
        self.assertEqual(res.json(), {"error": "No open claim with that id."})
        self.team.refresh_from_db()
        self.assertEqual(self.team.owner_id, self.user.id)

    def test_a_forged_project_request_cannot_ask_the_gm_owner_to_approve_a_project_join(self):
        # The caller addresses the item to a GM owner who also owns a project, and names that
        # project in `item_optionals`: the owner's Approve would add the caller to it.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Secret", owner=self.user2, project_system_user=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        self.authenticate(self.user)
        gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Theirs", owner=self.user2
        )
        ChannelMember.objects.create(channel=gm, user=self.user2, role="owner")
        res = self.client.post(
            JOIN_GM_REQ,
            self.body(
                item_type=2,
                item_optionals={"gm_id": str(gm.id), "project_id": project.project_id},
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.content)
        item = InboxItems.objects.get()
        self.authenticate(self.user2)
        res = self.client.post(PROJECT_APPROVE, {"item_id": item.item_id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND, res.content)
        self.assertFalse(
            ProjectMembers.objects.filter(project=project, attendee=self.user).exists()
        )

    def test_no_item_is_written_when_the_gm_is_not_found(self):
        res = self.client.post(
            JOIN_GM_REQ,
            self.body(
                item_type=5, item_optionals={"gm_id": "00000000-0000-0000-0000-000000000000"}
            ),
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(InboxItems.objects.exists())

    def test_the_url_is_the_one_the_sockets_service_calls(self):
        self.assertEqual(reverse("inbox_join_gm_request_item"), JOIN_GM_REQ)
