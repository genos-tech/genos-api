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
        item.item_body = {
            **(item.item_body or {}),
            "deadline": (timezone.now() - timedelta(days=1)).isoformat(),
        }
        item.save(update_fields=["item_body"])

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
