"""An ownership claim is only as old and as real as the row says (MIP-1029).

The item-type fix stops a forged type-5 row being CREATED. Rows that already exist are not
covered by it, and nobody can audit production for them from here, so `finalize` no longer
trusts what a forger controlled:

* the deadline in `item_optionals` (a body field on the endpoint that planted the row) is
  never earlier than the row's own `ts_created_at` plus the response window;
* the claim must have been addressed to the person who owns the team now (the row's
  `receiver`, set by the server when a real claim is filed), not to whoever the forger chose;
* a recorded deadline that is not a timezone-aware ISO date is "no deadline" (fail closed),
  where it used to be a 500 for the claimant and for anyone reading the team's claim status.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.services.ownership_claim import CLAIM_RESPONSE_DAYS, ITEM_TYPE_OWNERSHIP_CLAIM
from origin.tests.test_team_ownership_claim import (
    FINALIZE_URL,
    STATUS_URL,
    ClaimTestCase,
)


class PlantedClaimTests(ClaimTestCase):
    def plant(self, *, receiver=None, age_days=0, optionals=None):
        """A pending type-5 row as the old `joinGMRequest` let a caller write one."""
        item = InboxItems.objects.create(
            team=self.team,
            sender=self.editor,
            receiver=receiver if receiver is not None else self.editor,
            item_type=ITEM_TYPE_OWNERSHIP_CLAIM,
            request_status="pending",
            item_body=[],
            item_optionals=optionals
            if optionals is not None
            else {"gm_id": "x", "deadline": "2000-01-01T00:00:00+00:00"},
        )
        InboxItems.objects.filter(item_id=item.item_id).update(
            ts_created_at=timezone.now() - timedelta(days=age_days)
        )
        return item

    def finalize(self, item):
        return self.as_(self.editor).post(FINALIZE_URL, {"item_id": item.item_id}, format="json")

    def test_a_row_addressed_to_the_forger_does_not_take_the_team(self):
        item = self.plant(receiver=self.editor, age_days=CLAIM_RESPONSE_DAYS + 1)
        resp = self.finalize(item)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))
        # Settled, so it is not offered again.
        self.assertEqual(InboxItems.objects.get(item_id=item.item_id).request_status, "rejected")

    def test_a_row_with_no_receiver_does_not_take_the_team(self):
        item = self.plant(age_days=CLAIM_RESPONSE_DAYS + 1)
        InboxItems.objects.filter(item_id=item.item_id).update(receiver=None)
        resp = self.finalize(item)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_a_past_deadline_on_a_new_row_is_not_a_deadline(self):
        # Addressed to the real owner, so only the age is wrong.
        item = self.plant(receiver=self.owner, age_days=0)
        resp = self.finalize(item)
        self.assertEqual(resp.status_code, 409, resp.content)
        self.assertEqual(resp.json()["error"], "The owner still has time to respond.")
        self.assertEqual(self.owner_of_team(), str(self.owner.id))

    def test_the_status_page_does_not_offer_finalize_for_it_either(self):
        self.plant(receiver=self.owner, age_days=1)
        body = self.as_(self.editor).get(STATUS_URL, {"team_id": str(self.team.team_id)}).json()
        self.assertFalse(body["claim"]["canFinalize"])

    def test_a_real_claim_is_finalizable_once_the_window_has_passed(self):
        # The same row, a full response window later: nothing about a real claim changes.
        item = self.plant(receiver=self.owner, age_days=CLAIM_RESPONSE_DAYS + 1)
        resp = self.finalize(item)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.owner_of_team(), str(self.editor.id))

    def test_a_deadline_that_is_not_a_date_never_finalizes_and_never_crashes(self):
        for deadline in ("abc", 5, [], {"a": 1}, True, "2000-01-01T00:00:00", ""):
            with self.subTest(deadline=deadline):
                InboxItems.objects.all().delete()
                item = self.plant(
                    receiver=self.owner,
                    age_days=CLAIM_RESPONSE_DAYS + 1,
                    optionals={"deadline": deadline},
                )
                status_page = self.as_(self.editor).get(
                    STATUS_URL, {"team_id": str(self.team.team_id)}
                )
                self.assertEqual(status_page.status_code, 200, status_page.content)
                self.assertFalse(status_page.json()["claim"]["canFinalize"])
                resp = self.finalize(item)
                self.assertEqual(resp.status_code, 409, resp.content)
                self.assertEqual(self.owner_of_team(), str(self.owner.id))
