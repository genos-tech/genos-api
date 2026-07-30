"""Break-glass team-ownership recovery.

The gap: transfer is owner-INITIATED, so an absent owner leaves a team with
no path to being administered. This is the only flow that can move
ownership without the owner's consent, so most of what follows is about
what it must REFUSE.

See `origin/services/ownership_claim.py` for why it's a request with a
deadline rather than an inactivity check (short version: `last_seen` and
`ts_last_login_at` are both `auto_now` and never assigned, so they mean
"row last written", not "user last seen").
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.member_roles import EDITOR, VIEWER
from origin.services.ownership_claim import (
    CLAIM_REJECT_COOLDOWN_DAYS,
    CLAIM_RESPONSE_DAYS,
    ITEM_TYPE_OWNERSHIP_CLAIM,
)

User = get_user_model()

REQUEST_URL = "/api/v2/team/ownership-claim/request/"
RESPOND_URL = "/api/v2/team/ownership-claim/respond/"
FINALIZE_URL = "/api/v2/team/ownership-claim/finalize/"
STATUS_URL = "/api/v2/team/ownership-claim/"


def make_user(name):
    return User.objects.create_user(username=name, email=f"{name}@test.com", password="testpass123")


class ClaimTestCase(TestCase):
    def setUp(self):
        self.owner = make_user("owner")
        self.editor = make_user("editor")
        self.viewer = make_user("viewer")
        self.team = TeamMaster.objects.create(team_name="Acme", owner=self.owner)
        for user, role in (
            (self.owner, VIEWER),  # the owner's row keeps the column default
            (self.editor, EDITOR),
            (self.viewer, VIEWER),
        ):
            TeamMembers.objects.create(
                team_id=self.team.team_id, attendee_id=user.id, member_role=role
            )
        self.client = APIClient()

    def as_(self, user):
        self.client.force_authenticate(user=user)
        return self.client

    def file_claim(self, user=None):
        resp = self.as_(user or self.editor).post(
            REQUEST_URL, {"team_id": str(self.team.team_id)}, format="json"
        )
        return resp

    def expire(self, item_id):
        """Move a claim's deadline into the past."""
        item = InboxItems.objects.get(item_id=item_id)
        item.item_optionals = {
            **(item.item_optionals or {}),
            "deadline": (timezone.now() - timedelta(days=1)).isoformat(),
        }
        item.save(update_fields=["item_optionals"])

    def owner_of_team(self):
        return str(TeamMaster.objects.get(team_id=self.team.team_id).owner_id)


class TestFilingAClaim(ClaimTestCase):
    def test_an_editor_can_file(self):
        resp = self.file_claim()
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["responseDays"], CLAIM_RESPONSE_DAYS)
        item = InboxItems.objects.get(item_id=resp.json()["itemId"])
        self.assertEqual(item.item_type, ITEM_TYPE_OWNERSHIP_CLAIM)
        # Addressed TO the owner — that's what makes their silence evidence.
        self.assertEqual(str(item.receiver_id), str(self.owner.id))
        self.assertEqual(item.request_status, "pending")

    def test_the_owner_is_shown_something(self):
        """The claim must render in the owner's inbox.

        THIS IS A SAFETY TEST, not a formatting one. Finalizing is
        justified by the owner's silence, so the notice they stayed
        silent about has to be legible. The inbox renders `item_body`
        through BlockNote and gates on `itemBody[0]?.content?.length`
        (`InboxBubble.tsx`) — a structured dict there is an EMPTY CARD
        with a timestamp, and the owner is then timed out by a notice
        that showed them nothing.
        """
        item = InboxItems.objects.get(item_id=self.file_claim().json()["itemId"])
        self.assertIsInstance(item.item_body, list)
        self.assertTrue(item.item_body[0].get("content"))
        text = " ".join(
            span.get("text", "") for block in item.item_body for span in block.get("content", [])
        )
        self.assertIn("editor", text)  # who is asking
        self.assertIn("Acme", text)  # for what
        # ...and what happens if they do nothing, which is the whole point.
        self.assertIn(str(CLAIM_RESPONSE_DAYS), text)

    def test_the_machine_readable_half_rides_in_optionals(self):
        # Same split as every other request type: prose in `item_body`,
        # ids and dates in `item_optionals`.
        item = InboxItems.objects.get(item_id=self.file_claim().json()["itemId"])
        self.assertEqual(item.item_optionals["kind"], "team_ownership_claim")
        self.assertEqual(item.item_optionals["owner_id_at_request"], str(self.owner.id))
        self.assertTrue(item.item_optionals["deadline"])

    def test_a_viewer_cannot_file(self):
        # The escalation this feature must not become: a read-only member
        # taking a team over by waiting.
        self.assertEqual(self.file_claim(self.viewer).status_code, 403)

    def test_a_non_member_cannot_file(self):
        self.assertEqual(self.file_claim(make_user("stranger")).status_code, 403)

    def test_the_owner_cannot_file_against_themselves(self):
        self.assertEqual(self.file_claim(self.owner).status_code, 400)

    def test_only_one_open_claim_per_team(self):
        self.assertEqual(self.file_claim().status_code, 201)
        second = make_user("editor2")
        TeamMembers.objects.create(
            team_id=self.team.team_id, attendee_id=second.id, member_role=EDITOR
        )
        self.assertEqual(self.file_claim(second).status_code, 409)

    def test_filing_needs_authentication(self):
        anon = APIClient()
        resp = anon.post(REQUEST_URL, {"team_id": str(self.team.team_id)}, format="json")
        self.assertIn(resp.status_code, (401, 403))


class TestOwnerResponse(ClaimTestCase):
    def test_owner_approving_transfers_immediately(self):
        item_id = self.file_claim().json()["itemId"]
        resp = self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "approve"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.owner_of_team(), str(self.editor.id))

    def test_owner_rejecting_leaves_ownership_alone(self):
        item_id = self.file_claim().json()["itemId"]
        resp = self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "reject"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_the_claimant_cannot_approve_their_own_claim(self):
        # The whole guard, in one test.
        item_id = self.file_claim().json()["itemId"]
        resp = self.as_(self.editor).post(
            RESPOND_URL, {"item_id": item_id, "decision": "approve"}, format="json"
        )
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_a_rejected_claimant_is_on_cooldown(self):
        item_id = self.file_claim().json()["itemId"]
        self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "reject"}, format="json"
        )
        retry = self.file_claim()
        self.assertEqual(retry.status_code, 429)
        self.assertIn("retryAfter", retry.json())

    def test_the_cooldown_expires(self):
        item_id = self.file_claim().json()["itemId"]
        self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "reject"}, format="json"
        )
        later = timezone.now() + timedelta(days=CLAIM_REJECT_COOLDOWN_DAYS + 1)
        with patch("django.utils.timezone.now", return_value=later):
            self.assertEqual(self.file_claim().status_code, 201)


class TestFinalizing(ClaimTestCase):
    def test_cannot_finalize_before_the_deadline(self):
        item_id = self.file_claim().json()["itemId"]
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_can_finalize_after_the_deadline(self):
        item_id = self.file_claim().json()["itemId"]
        self.expire(item_id)
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.owner_of_team(), str(self.editor.id))

    def test_the_displaced_owner_is_left_as_an_editor(self):
        # Otherwise they land on the column default (`viewer`) and lose any
        # way to respond — including filing their own claim to undo this.
        item_id = self.file_claim().json()["itemId"]
        self.expire(item_id)
        self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        row = TeamMembers.objects.get(team_id=self.team.team_id, attendee_id=self.owner.id)
        self.assertEqual(row.member_role, EDITOR)

    def test_only_the_claimant_can_finalize(self):
        item_id = self.file_claim().json()["itemId"]
        self.expire(item_id)
        other = make_user("editor3")
        TeamMembers.objects.create(
            team_id=self.team.team_id, attendee_id=other.id, member_role=EDITOR
        )
        resp = self.as_(other).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_an_intervening_transfer_invalidates_the_claim(self):
        # THE RACE: the owner transfers to Carol normally while this claim
        # sits pending. Finalizing must not take the team from Carol, who
        # never had a claim filed against her.
        item_id = self.file_claim().json()["itemId"]
        carol = make_user("carol")
        TeamMembers.objects.create(
            team_id=self.team.team_id, attendee_id=carol.id, member_role=EDITOR
        )
        team = TeamMaster.objects.get(team_id=self.team.team_id)
        team.owner_id = carol.id
        team.save(update_fields=["owner"])

        self.expire(item_id)
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.owner_of_team(), str(carol.id))
        self.assertEqual(InboxItems.objects.get(item_id=item_id).request_status, "rejected")

    def test_a_demoted_claimant_cannot_finalize(self):
        # Eligibility is re-checked at finalize, not trusted from filing.
        item_id = self.file_claim().json()["itemId"]
        TeamMembers.objects.filter(team_id=self.team.team_id, attendee_id=self.editor.id).update(
            member_role=VIEWER
        )
        self.expire(item_id)
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_a_claim_cannot_be_finalized_twice(self):
        item_id = self.file_claim().json()["itemId"]
        self.expire(item_id)
        self.assertEqual(
            self.as_(self.editor)
            .post(FINALIZE_URL, {"item_id": item_id}, format="json")
            .status_code,
            200,
        )
        again = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(again.status_code, 404)

    def test_an_approved_claim_cannot_then_be_finalized(self):
        item_id = self.file_claim().json()["itemId"]
        self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "approve"}, format="json"
        )
        self.expire(item_id)
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_a_claim_with_no_recorded_deadline_can_never_be_finalized(self):
        # Fail closed. A row that predates `claim_optionals`, or one made
        # by hand, must not become finalizable by having no deadline.
        item_id = self.file_claim().json()["itemId"]
        InboxItems.objects.filter(item_id=item_id).update(item_optionals={})
        resp = self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_everyone_is_told_when_a_claim_is_finalized(self):
        # A silent ownership change is what would make this a liability
        # rather than a safety net.
        item_id = self.file_claim().json()["itemId"]
        self.expire(item_id)
        with patch("origin.services.webpush_dispatch.schedule_push_to_user") as push:
            self.as_(self.editor).post(FINALIZE_URL, {"item_id": item_id}, format="json")
        notified = {str(c.kwargs["recipient_id"]) for c in push.call_args_list}
        self.assertIn(str(self.owner.id), notified)
        self.assertIn(str(self.viewer.id), notified)


class TestClaimStatus(ClaimTestCase):
    """The claimant's only view of their own claim.

    The claim is an `InboxItems` row and the inbox GET filters on
    `receiver` — the OWNER. The claimant is the `sender`, so without this
    endpoint they can file a claim and then never see it again, and have
    nothing to finalize from.
    """

    def get_status(self, user):
        return self.as_(user).get(STATUS_URL, {"team_id": str(self.team.team_id)})

    def test_the_claimant_can_see_their_own_pending_claim(self):
        item_id = self.file_claim().json()["itemId"]
        body = self.get_status(self.editor).json()
        self.assertEqual(body["claim"]["itemId"], item_id)
        self.assertTrue(body["claim"]["isMine"])
        self.assertTrue(body["claim"]["deadline"])

    def test_finalize_is_not_offered_before_the_deadline(self):
        self.file_claim()
        self.assertFalse(self.get_status(self.editor).json()["claim"]["canFinalize"])

    def test_finalize_is_offered_after_the_deadline(self):
        self.expire(self.file_claim().json()["itemId"])
        self.assertTrue(self.get_status(self.editor).json()["claim"]["canFinalize"])

    def test_another_editor_is_not_offered_someone_elses_finalize(self):
        self.expire(self.file_claim().json()["itemId"])
        other = make_user("editor4")
        TeamMembers.objects.create(
            team_id=self.team.team_id, attendee_id=other.id, member_role=EDITOR
        )
        claim = self.get_status(other).json()["claim"]
        self.assertFalse(claim["isMine"])
        self.assertFalse(claim["canFinalize"])

    def test_an_editor_with_no_open_claim_is_told_they_may_request(self):
        body = self.get_status(self.editor).json()
        self.assertIsNone(body["claim"])
        self.assertTrue(body["canRequest"])
        self.assertEqual(body["responseDays"], CLAIM_RESPONSE_DAYS)

    def test_a_viewer_is_not_told_they_may_request(self):
        self.assertFalse(self.get_status(self.viewer).json()["canRequest"])

    def test_a_rejected_claimant_sees_their_cooldown(self):
        item_id = self.file_claim().json()["itemId"]
        self.as_(self.owner).post(
            RESPOND_URL, {"item_id": item_id, "decision": "reject"}, format="json"
        )
        body = self.get_status(self.editor).json()
        self.assertFalse(body["canRequest"])
        self.assertIsNotNone(body["retryAfter"])

    def test_a_non_member_cannot_read_the_status(self):
        # `resolve_team_role` answers `viewer` for someone with NO member
        # row, so membership has to be checked explicitly here or this
        # endpoint hands a team's claim state to any authenticated
        # stranger who guesses the id.
        self.assertEqual(self.get_status(make_user("stranger2")).status_code, 403)


class TestLiveNotice(ClaimTestCase):
    """The payload the sockets service relays to the owner.

    This flow files over HTTP; request types 1-4 file through the
    sockets service, which pushes the new row as it creates it. Without
    a relay the owner's open tab shows nothing until a full reload —
    and their silence is what authorises the takeover, so "they never
    saw it" is not an acceptable failure mode.
    """

    def get_status(self, user):
        return self.as_(user).get(STATUS_URL, {"team_id": str(self.team.team_id)})

    def test_the_claimant_gets_a_payload_addressed_to_the_owner(self):
        item_id = self.file_claim().json()["itemId"]
        notice = self.get_status(self.editor).json()["claim"]["notice"]
        self.assertEqual(notice["receiver"], str(self.owner.id))
        self.assertEqual(notice["data"]["itemId"], item_id)
        self.assertEqual(notice["data"]["itemType"], ITEM_TYPE_OWNERSHIP_CLAIM)
        # The card renders from these two; empty means a blank card.
        self.assertTrue(notice["data"]["itemBody"])
        self.assertTrue(notice["data"]["itemOptionals"]["deadline"])

    def test_nobody_else_is_handed_a_relayable_payload(self):
        # The relay is triggered by the client, so the payload must only
        # ever reach the person who filed the claim. Otherwise it turns
        # into a way to push inbox content at an arbitrary user.
        self.file_claim()
        other = make_user("editor5")
        TeamMembers.objects.create(
            team_id=self.team.team_id, attendee_id=other.id, member_role=EDITOR
        )
        self.assertNotIn("notice", self.get_status(other).json()["claim"])
        self.assertNotIn("notice", self.get_status(self.viewer).json()["claim"])
