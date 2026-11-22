"""GET /api/v2/agent/credits/usage/ — the caller's own AI credit usage (MIP-1971).

Ranked by expense-if-wrong: (1) nobody reads another person's usage, a team owner included;
(2) numbers are the ledger's, grouped per job; (3) an unknown source is "other", not an error.
"""

from __future__ import annotations

import uuid

from django.conf import settings as dj_settings
from django.core.cache import cache
from django.test import override_settings
from django.utils import timezone

from origin.models.common.inbox_models import InboxItems
from origin.models.common.schedule_models import MemberSchedule
from origin.search_engine import credit_ledger, credit_usage
from origin.search_engine.models import AiCreditEntry, AiRequestCost

from .test_base import BaseAPITestCase

URL = "/api/v2/agent/credits/usage/"


def _se(**kw):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(AI_COST_METER=True, AI_CREDITS_SHADOW=True, AI_CREDITS_AUTHORITATIVE=True)
    cfg.update(kw)
    return cfg


def charge(user, surface, credits_milli, *, period=None):
    rid = uuid.uuid4()
    AiRequestCost.objects.create(
        request_id=rid, user_id=str(user.id), surface=surface, started_at=timezone.now()
    )
    AiCreditEntry.objects.create(
        user_id=str(user.id),
        entry_type=AiCreditEntry.ENTRY_CHARGE,
        credits_milli=-credits_milli,
        request_id=rid,
        period=period or credit_ledger.period_for(),
        actor="system",
    )
    return rid


@override_settings(SEARCH_ENGINE=_se())
class CreditUsageTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.authenticate()

    def test_requires_login(self):
        self.unauthenticate()
        self.assertIn(self.client.get(URL).status_code, (401, 403))

    def test_groups_this_month_by_job(self):
        charge(self.user, "ask", 1500)
        charge(self.user, "ask", 500)
        charge(self.user, "thread_summary", 300)
        charge(self.user, "note_summary", 200)
        charge(self.user, "schedule", 100)
        charge(self.user, "mcp_search", 50)  # the billable MCP surface (MIP-1965)
        charge(self.user, "brand_new_source", 40)  # unknown: never an error
        charge(self.user, "ask", 9000, period="2020-01")  # another month: not in this month
        data = self.client.get(URL).data
        by_job = {r["job"]: r for r in data["by_job"]}
        self.assertEqual(by_job["ask"], {"job": "ask", "count": 2, "credits": 2.0})
        self.assertEqual(by_job["summary"], {"job": "summary", "count": 2, "credits": 0.5})
        self.assertEqual(by_job["schedule"]["credits"], 0.1)
        self.assertEqual(by_job["connected_app"]["count"], 1)
        self.assertEqual(by_job["other"], {"job": "other", "count": 1, "credits": 0.04})
        self.assertEqual(data["month_total_credits"], 2.69)
        self.assertFalse(data["credits"]["unlimited"])
        self.assertIsNotNone(data["period_end_iso"])
        # the order is fixed, `other` last
        self.assertEqual(data["by_job"][-1]["job"], "other")

    def test_every_billable_surface_has_its_own_job(self):
        """No billable source may fall into `other` (MIP-2024): the ledger names MCP searches
        `mcp_search`, and the first version of the map only knew `mcp`."""

        for surface in dj_settings.CREDIT_POLICY.billable_surfaces:
            self.assertNotEqual(credit_usage.job_for_surface(surface), "other", surface)

    def test_recent_starts_with_the_newest_5(self):
        for _ in range(12):
            charge(self.user, "ask", 10)
        data = self.client.get(URL).data
        recent = data["recent"]
        self.assertEqual(len(recent), 5)
        self.assertEqual(recent, sorted(recent, key=lambda r: r["id"], reverse=True))
        self.assertEqual(
            set(recent[0]), {"id", "at", "job", "credits", "reversed", "detail", "source"}
        )
        self.assertEqual(data["next_cursor"], recent[-1]["id"])
        # the totals still count every charge, not just the page
        self.assertEqual(data["by_job"], [{"job": "ask", "count": 12, "credits": 0.12}])

    def test_recent_pages_by_cursor_until_the_end(self):
        for _ in range(12):
            charge(self.user, "ask", 10)
        first = self.client.get(URL).data
        ids = [r["id"] for r in first["recent"]]
        cursor = first["next_cursor"]
        while cursor is not None:
            page = self.client.get(URL, {"cursor": cursor, "limit": 5}).data
            self.assertEqual(set(page), {"recent", "next_cursor"}, "later pages carry no totals")
            ids += [r["id"] for r in page["recent"]]
            cursor = page["next_cursor"]
        self.assertEqual(len(ids), 12)
        self.assertEqual(len(set(ids)), 12, "no row twice")
        self.assertEqual(ids, sorted(ids, reverse=True))

    def test_exactly_one_page_has_no_next_cursor(self):
        for _ in range(5):
            charge(self.user, "ask", 10)
        data = self.client.get(URL).data
        self.assertEqual((len(data["recent"]), data["next_cursor"]), (5, None))

    def test_limit_is_clamped_and_a_bad_cursor_is_refused(self):
        for _ in range(30):
            charge(self.user, "ask", 10)
        self.assertEqual(len(self.client.get(URL, {"limit": 1000}).data["recent"]), 25)
        self.assertEqual(len(self.client.get(URL, {"limit": "abc"}).data["recent"]), 5)
        self.assertEqual(len(self.client.get(URL, {"limit": 0}).data["recent"]), 1)
        for bad in ("abc", "0", "-3", ""):
            self.assertEqual(self.client.get(URL, {"cursor": bad}).status_code, 400, bad)

    def test_a_cursor_cannot_reach_another_persons_charges(self):
        charge(self.user2, "ask", 700)
        mine = charge(self.user, "ask", 10)
        # another person's row id as the cursor only moves a position inside MY rows
        theirs = AiCreditEntry.objects.get(user_id=str(self.user2.id)).id
        page = self.client.get(URL, {"cursor": theirs + 1000}).data
        self.assertEqual([r["credits"] for r in page["recent"]], [0.01])
        self.assertEqual(AiCreditEntry.objects.get(request_id=mine).user_id, str(self.user.id))
        self.assertEqual(self.client.get(URL, {"cursor": theirs}).data["recent"], [])

    def test_a_row_without_a_named_job_carries_its_source(self):
        charge(self.user, "brand_new_source", 40)
        charge(self.user, "mcp_search", 40)
        recent = self.client.get(URL).data["recent"]
        by_job = {r["job"]: r["source"] for r in recent}
        self.assertEqual(by_job, {"other": "brand_new_source", "connected_app": ""})

    def test_requires_login_for_a_later_page_too(self):
        self.unauthenticate()
        self.assertIn(self.client.get(URL, {"cursor": 5}).status_code, (401, 403))

    def test_empty_state(self):
        data = self.client.get(URL).data
        self.assertEqual((data["by_job"], data["recent"], data["month_total_credits"]), ([], [], 0))

    def test_nobody_reads_another_persons_usage(self):
        charge(self.user2, "ask", 7000)
        # a team owner is no exception: the endpoint takes no user and shows only the caller's
        data = self.client.get(
            URL, {"user_id": str(self.user2.id), "user": str(self.user2.id)}
        ).data
        self.assertEqual((data["by_job"], data["recent"], data["month_total_credits"]), ([], [], 0))
        self.authenticate(self.user2)
        theirs = self.client.get(URL).data
        self.assertEqual(theirs["month_total_credits"], 7.0)
        self.assertEqual(len(theirs["recent"]), 1)

    def test_a_charge_whose_request_row_is_missing_is_other(self):
        AiCreditEntry.objects.create(
            user_id=str(self.user.id),
            entry_type=AiCreditEntry.ENTRY_CHARGE,
            credits_milli=-100,
            request_id=uuid.uuid4(),
            period=credit_ledger.period_for(),
            actor="system",
        )
        self.assertEqual(self.client.get(URL).data["by_job"][0]["job"], "other")

    def test_a_schedule_charge_carries_the_schedule_name_only_for_the_owner(self):
        rid = charge(self.user, "schedule", 120)
        sched = MemberSchedule.objects.create(
            user=self.user,
            team=self.team,
            name="Monday plan",
            job="smart",
            frequency="daily",
            time_of_day="09:00",
        )
        InboxItems.objects.create(
            team=self.team,
            receiver=self.user,
            item_type=10,
            item_body={"title": "t", "text": "x"},
            item_optionals={"schedule_id": str(sched.id), "ai": {"request_id": str(rid)}},
        )
        recent = self.client.get(URL).data["recent"]
        self.assertEqual((recent[0]["job"], recent[0]["detail"]), ("schedule", "Monday plan"))
        # someone else's item pointing at this request does not name anything for another user
        self.assertEqual(
            credit_usage._schedule_names(self.user2, [str(rid)]), {}, "scoped to the receiver"
        )

    @override_settings(SEARCH_ENGINE=_se(AI_CREDITS_AUTHORITATIVE=False))
    def test_credits_block_is_null_when_credits_are_not_authoritative(self):
        charge(self.user, "ask", 100)
        data = self.client.get(URL).data
        self.assertIsNone(data["credits"])
        self.assertIsNone(data["period_end_iso"])
        self.assertEqual(data["by_job"][0]["job"], "ask")

    def test_a_reversed_charge_is_netted_out(self):
        full = charge(self.user, "ask", 1000)
        part = charge(self.user, "ask", 600)
        for rid, back in ((full, 1000), (part, 200)):
            entry = AiCreditEntry.objects.get(request_id=rid)
            AiCreditEntry.objects.create(
                user_id=str(self.user.id),
                entry_type=AiCreditEntry.ENTRY_REVERSAL,
                credits_milli=back,
                ref_id=entry.id,
                period=entry.period,
                actor="op",
            )
        data = self.client.get(URL).data
        self.assertEqual(data["by_job"], [{"job": "ask", "count": 1, "credits": 0.4}])
        self.assertEqual(data["month_total_credits"], 0.4)
        by_credits = {r["credits"]: r["reversed"] for r in data["recent"]}
        self.assertEqual(by_credits, {0.0: True, 0.4: True})

    def test_job_mapping_tolerates_unknown_and_empty(self):
        for s in (None, "", "zzz", "digest", "index"):
            self.assertEqual(credit_usage.job_for_surface(s), "other")
