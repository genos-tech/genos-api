"""AI-made Inbox items carry what they cost and a way to turn their source off (MIP-1209).

What is pinned here, each by a test that fails when the guard is removed:

  * the inventory: every file that files an Inbox item is listed with whether it spends AI usage,
    so a new producer fails a test until someone answers that question; an AI producer cannot
    file without its cost block (the digest always has one; a scheduled report whose job
    `uses_ai` is refused, and nothing is filed, when its job does not say which request paid);
  * the cost is read from the member's OWN `AiRequestCost` row, in credits (milli), with the
    amount deducted, the call count and "at least" when some calls were unpriced; a missing
    meter or row is `metered: false`, never an invented number; an item that used no AI says
    nothing about cost;
  * it is stored in `item_optionals`, never in `item_body`, so `list_my_inbox` (which hands the
    body to the in-app model) shows nothing new;
  * turning a source off: the digest or that schedule, from the member's own item only (anything
    else is the same 404), with a sentence saying what happened, an undo, and nothing deleted.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from datetime import timezone as dt_timezone
from io import StringIO
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.management import call_command
from django.test import SimpleTestCase
from django.utils import timezone

from origin.models.common.inbox_models import (
    ACTIVITY_INBOX_ITEM_TYPES,
    ITEM_TYPE_GENOS_REPORT,
    InboxItems,
)
from origin.models.common.schedule_models import MemberSchedule
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_my_inbox import LIST_MY_INBOX
from origin.search_engine.llm import spend
from origin.search_engine.management.commands import agent_digest as digest_mod
from origin.search_engine.models import AiRequestCost
from origin.services import inbox_ai, schedule_delivery, schedules
from origin.services.schedule_jobs import JOBS, Job
from origin.tests.test_agent_digest import _fake_run_agent
from origin.tests.test_base import BaseAPITestCase

ROOT = Path(__file__).resolve().parents[2]
_MONDAY_8AM_JST = datetime(2026, 8, 2, 23, 0, tzinfo=dt_timezone.utc)


def usd_for_credits(credits: float) -> int:
    """Micro-USD that `credits` credits cost under the live policy."""
    return int(settings.CREDIT_POLICY.credit_usd * 1_000_000 * credits)


def cost_row(user, **kw) -> AiRequestCost:
    fields = {
        "request_id": uuid.uuid4(),
        "user_id": str(user.id),
        "surface": "digest",
        "started_at": timezone.now(),
        "computed_usd_micro": usd_for_credits(1.83),
        "call_count": 7,
        "shadow_credits_milli": 0,
        "has_unpriced": False,
        **kw,
    }
    return AiRequestCost.objects.create(**fields)


class TestTheInventory(SimpleTestCase):
    PATTERNS = ("InboxItems.objects.create(", "InboxItemsSerializer(data=")

    def producers_on_disk(self) -> set[str]:
        found = set()
        for path in (ROOT / "origin").rglob("*.py"):
            rel = path.relative_to(ROOT).as_posix()
            if "/tests/" in rel or "/migrations/" in rel:
                continue
            text = path.read_text(encoding="utf-8")
            if any(p in text for p in self.PATTERNS):
                found.add(rel)
        return found

    def test_every_file_that_files_an_inbox_item_is_in_the_inventory(self):
        self.assertEqual(
            self.producers_on_disk(),
            set(inbox_ai.PRODUCERS),
            "A file that files Inbox items is not in `inbox_ai.PRODUCERS` (or is listed but no "
            "longer does): say whether it spends AI usage and where the cost is attached.",
        )

    def test_an_ai_producer_attaches_its_cost_and_the_others_say_they_use_none(self):
        for rel, entry in inbox_ai.PRODUCERS.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            if entry["ai"]:
                self.assertIn("inbox_ai", text, f"{rel} spends AI usage but never attaches a cost")
            else:
                self.assertNotIn(
                    "run_agent(", text, f"{rel} calls the agent but says it uses no AI"
                )
                self.assertNotIn("metered_request(", text, f"{rel} meters AI but says it uses none")

    def test_the_two_that_can_spend_are_the_digest_and_the_reports(self):
        spends = {rel for rel, e in inbox_ai.PRODUCERS.items() if e["ai"]}
        self.assertEqual(
            spends,
            {
                "origin/search_engine/management/commands/agent_digest.py",
                "origin/services/schedule_delivery.py",
            },
        )

    def test_no_built_in_job_claims_to_use_ai_yet(self):
        # When one does, its report must name the request that paid (below); this is the reminder.
        self.assertEqual([k for k, j in JOBS.items() if j.uses_ai], [])


class TestTheCostBlock(BaseAPITestCase):
    def test_it_is_read_from_the_request_in_credits_and_says_what_was_deducted(self):
        row = cost_row(self.user, shadow_credits_milli=250)
        block = inbox_ai.cost_block("digest", row.request_id, self.user.id)
        self.assertEqual(
            block,
            {
                "producer": "digest",
                "request_id": str(row.request_id),
                "credits_milli": 1830,
                "deducted_credits_milli": 250,
                "calls": 7,
                "lower_bound": False,
                "metered": True,
            },
        )

    def test_an_unpriced_call_makes_it_a_lower_bound(self):
        row = cost_row(self.user, has_unpriced=True)
        self.assertTrue(inbox_ai.cost_block("digest", row.request_id, self.user.id)["lower_bound"])

    def test_the_digest_is_not_billed_but_its_cost_is_still_shown(self):
        row = cost_row(self.user, shadow_credits_milli=0)
        block = inbox_ai.cost_block("digest", row.request_id, self.user.id)
        self.assertEqual((block["credits_milli"], block["deducted_credits_milli"]), (1830, 0))

    def test_someone_elses_request_is_never_read(self):
        row = cost_row(self.user2)
        block = inbox_ai.cost_block("digest", row.request_id, self.user.id)
        self.assertEqual((block["metered"], block["credits_milli"]), (False, 0))

    def test_no_meter_no_row_or_a_bad_id_is_not_metered_and_never_a_number(self):
        for rid in (None, "", "nope", str(uuid.uuid4()), 5):
            block = inbox_ai.cost_block("digest", rid, self.user.id)
            self.assertEqual(
                (block["metered"], block["credits_milli"], block["calls"], block["lower_bound"]),
                (False, 0, 0, True),
                rid,
            )

    def test_a_failure_to_read_never_raises(self):
        row = cost_row(self.user)
        with mock.patch("origin.search_engine.models.AiRequestCost.objects") as objects:
            objects.filter.side_effect = RuntimeError("db gone")
            block = inbox_ai.cost_block("digest", row.request_id, self.user.id)
        self.assertFalse(block["metered"])


class TestTheDigest(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.team.plan = "max"
        self.team.save(update_fields=["plan"])
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

    def tick(self, run_agent=None):
        with (
            mock.patch.object(digest_mod.timezone, "now", return_value=_MONDAY_8AM_JST),
            mock.patch.object(digest_mod, "run_agent", side_effect=run_agent or _fake_run_agent()),
            mock.patch.object(digest_mod, "dispatch_push_for_inbox_item"),
        ):
            call_command("agent_digest", stdout=StringIO(), at_hour=8)
        return list(InboxItems.objects.filter(item_type=digest_mod.ITEM_TYPE_DIGEST))

    def test_the_digest_item_always_carries_its_cost_block(self):
        [item] = self.tick()
        ai = item.item_optionals["ai"]
        self.assertEqual(ai["producer"], "digest")
        self.assertEqual(set(ai), set(inbox_ai.cost_block("digest", None, 0)))

    def test_it_is_the_request_that_paid_for_the_run(self):
        seen = {}
        text = "- Task X slipped.\nNext: review X."
        inner = _fake_run_agent(text)

        def paid(query, ctx, emit, **kwargs):
            bound = spend.current_context()
            seen["request_id"] = bound.request_id
            cost_row(self.user, request_id=bound.request_id)
            return inner(query, ctx, emit, **kwargs)

        [item] = self.tick(run_agent=paid)
        ai = item.item_optionals["ai"]
        self.assertEqual(ai["request_id"], seen["request_id"])
        self.assertTrue(ai["metered"])
        self.assertEqual(ai["credits_milli"], 1830)
        self.assertEqual(ai["deducted_credits_milli"], 0)
        self.assertEqual(ai["calls"], 7)

    def test_the_cost_is_in_the_optionals_and_not_in_the_body(self):
        [item] = self.tick()
        self.assertNotIn("ai", item.item_body)
        self.assertNotIn("credits", json.dumps(item.item_body))

    def test_the_in_app_models_inbox_tool_shows_no_cost(self):
        [item] = self.tick()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        out = LIST_MY_INBOX.run({"include_read": True}, ctx)
        blob = json.dumps(out)
        self.assertIn(str(item.item_id), blob)
        for word in ("credits_milli", "deducted", "request_id", '"ai"'):
            self.assertNotIn(word, blob)
        self.assertEqual(
            set(out["items"][0]),
            {
                "item_id",
                "item_type",
                "item_type_label",
                "sender_id",
                "sender_username",
                "is_read",
                "request_status",
                "item_body",
                "ts_created_at",
            },
        )


class AiJobBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.paid = None

        def ai_job(ctx, params):
            report = {"title": "Smart report", "summary": "Thought hard.", "sections": []}
            if self.paid is not None:
                report["ai_request_id"] = str(self.paid)
            return report

        patcher = mock.patch.dict(
            JOBS,
            {
                "smart": Job("smart", "Smart", "Uses a model.", ai_job, uses_ai=True),
                "plain": Job(
                    "plain",
                    "Plain",
                    "Uses none.",
                    lambda ctx, params: {"title": "Plain", "summary": "Nope.", "sections": []},
                ),
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def schedule(self, job, name="Report", at="09:00"):
        return schedules.create(self.ctx, name=name, job=job, frequency="daily", time_of_day=at)

    def reports(self):
        return InboxItems.objects.filter(item_type=ITEM_TYPE_GENOS_REPORT)


class TestAScheduledReport(AiJobBase):
    def test_a_job_that_uses_ai_is_filed_with_its_cost(self):
        self.paid = cost_row(self.user, surface="schedule").request_id
        sched = self.schedule("smart")
        item = schedule_delivery.deliver(sched, JOBS["smart"].run(self.ctx, {}))
        ai = item.item_optionals["ai"]
        self.assertEqual(
            (ai["producer"], ai["credits_milli"], ai["metered"]), ("schedule", 1830, True)
        )
        self.assertEqual(item.item_optionals["schedule_id"], str(sched.id))
        self.assertNotIn("ai", item.item_body)

    def test_a_job_that_uses_ai_and_names_no_request_files_nothing(self):
        sched = self.schedule("smart")
        with self.assertRaises(inbox_ai.AiCostMissing):
            schedule_delivery.deliver(sched, JOBS["smart"].run(self.ctx, {}))
        self.assertEqual(self.reports().count(), 0)

    def test_that_fails_the_run_like_any_failing_job(self):
        sched = self.schedule("smart")
        out = schedules.run_now(self.ctx, sched.id)
        self.assertEqual(out["status"] if "status" in out else out.get("last_status"), "failed")
        self.assertEqual(self.reports().count(), 0)

    def test_a_job_that_uses_none_says_nothing_about_cost(self):
        sched = self.schedule("plain", name="Plain")
        item = schedule_delivery.deliver(sched, JOBS["plain"].run(self.ctx, {}))
        self.assertNotIn("ai", item.item_optionals)

    def test_a_request_that_is_not_the_members_is_not_a_cost(self):
        self.paid = cost_row(self.user2).request_id
        sched = self.schedule("smart")
        item = schedule_delivery.deliver(sched, JOBS["smart"].run(self.ctx, {}))
        self.assertFalse(item.item_optionals["ai"]["metered"])
        self.assertEqual(item.item_optionals["ai"]["credits_milli"], 0)

    def test_the_request_id_a_job_gives_is_not_stored_on_the_body(self):
        self.paid = cost_row(self.user).request_id
        sched = self.schedule("smart")
        item = schedule_delivery.deliver(sched, JOBS["smart"].run(self.ctx, {}))
        self.assertNotIn(str(self.paid), json.dumps(item.item_body))


LIST = "/api/v2/inbox/"


class TestTurnItOff(AiJobBase):
    def setUp(self):
        super().setUp()
        self.authenticate()

    def digest_item(self, user=None):
        user = user or self.user
        return InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=user,
            item_type=6,
            item_body={"title": "Digest", "text": "x"},
            item_optionals={"ai": inbox_ai.cost_block("digest", None, user.id)},
            request_status="",
        )

    def report_item(self, sched=None):
        self.paid = cost_row(self.user).request_id
        sched = sched or self.schedule("smart")
        return schedule_delivery.deliver(sched, JOBS["smart"].run(self.ctx, {})), sched

    def url(self, item, tail=""):
        return f"{LIST}{item.item_id}/ai/{tail}"

    def test_the_digest_is_turned_off_and_back_on_and_the_item_stays(self):
        item = self.digest_item()
        before = self.client.get(self.url(item)).data
        self.assertEqual(
            before, {"kind": "digest", "enabled": True, "label": "Genos digest", "used_ai": True}
        )
        off = self.client.post(self.url(item, "off/"))
        self.assertEqual(off.status_code, 200, off.data)
        self.assertEqual(
            (off.data["kind"], off.data["enabled"], off.data["was_enabled"]),
            ("digest", False, True),
        )
        self.assertIn("digest is off", off.data["what"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.digest_enabled)
        self.assertTrue(InboxItems.objects.filter(pk=item.pk, is_deleted=False).exists())
        self.assertFalse(self.client.get(self.url(item)).data["enabled"])
        on = self.client.post(self.url(item, "on/"))
        self.assertEqual((on.status_code, on.data["enabled"]), (200, True))
        self.user.refresh_from_db()
        self.assertTrue(self.user.digest_enabled)

    def test_a_report_pauses_its_schedule_and_the_undo_resumes_it(self):
        item, sched = self.report_item()
        off = self.client.post(self.url(item, "off/"))
        self.assertEqual(off.status_code, 200, off.data)
        self.assertEqual((off.data["kind"], off.data["label"]), ("schedule", "Report"))
        self.assertIn("is paused", off.data["what"])
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.paused_reason, "manual")
        self.assertEqual(self.reports().count(), 1)
        self.assertEqual(self.client.get(self.url(item)).data["enabled"], False)
        on = self.client.post(self.url(item, "on/"))
        self.assertEqual(on.status_code, 200, on.data)
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)
        self.assertIsNotNone(sched.next_run_at)

    def test_turning_off_twice_says_it_was_already_off(self):
        item = self.digest_item()
        self.client.post(self.url(item, "off/"))
        again = self.client.post(self.url(item, "off/"))
        self.assertEqual((again.data["enabled"], again.data["was_enabled"]), (False, False))

    def test_a_second_off_or_a_replayed_on_says_so_and_writes_nothing(self):
        item, sched = self.report_item()
        MemberSchedule.objects.filter(pk=sched.pk).update(consecutive_failures=2)
        before = MemberSchedule.objects.get(pk=sched.pk)
        on = self.client.post(self.url(item, "on/"))
        self.assertEqual((on.status_code, on.data["was_enabled"]), (200, True))
        self.assertIn("already on", on.data["what"])
        after = MemberSchedule.objects.get(pk=sched.pk)
        self.assertEqual(
            (after.next_run_at, after.consecutive_failures, after.ts_updated_at),
            (before.next_run_at, 2, before.ts_updated_at),
        )
        self.client.post(self.url(item, "off/"))
        again = self.client.post(self.url(item, "off/"))
        self.assertIn("already off", again.data["what"])
        digest = self.digest_item()
        self.assertIn("already on", self.client.post(self.url(digest, "on/")).data["what"])

    def test_a_schedule_is_switched_in_its_own_team_not_the_items(self):
        item, sched = self.report_item()
        other = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        InboxItems.objects.filter(pk=item.pk).update(team=other)
        res = self.client.post(self.url(item, "off/"))
        self.assertEqual(res.status_code, 200, res.data)
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)

    def test_the_source_is_never_taken_from_the_request(self):
        item, sched = self.report_item()
        other = self.schedule("plain", name="Other", at="10:00")
        res = self.client.post(
            self.url(item, "off/"), {"schedule_id": str(other.id), "kind": "digest"}, format="json"
        )
        self.assertEqual(res.status_code, 200)
        other.refresh_from_db()
        sched.refresh_from_db()
        self.user.refresh_from_db()
        self.assertEqual(
            (other.enabled, sched.enabled, self.user.digest_enabled), (True, False, True)
        )

    def test_someone_elses_item_an_item_without_ai_and_a_deleted_one_are_the_same_404(self):
        theirs = self.digest_item(self.user2)
        plain = InboxItems.objects.create(
            team=self.team, receiver=self.user, item_type=0, item_body={"title": "t", "text": "x"}
        )
        gone = self.digest_item()
        InboxItems.objects.filter(pk=gone.pk).update(is_deleted=True)
        answers = set()
        for item in (theirs, plain, gone):
            for tail in ("", "off/", "on/"):
                call = self.client.get if not tail else self.client.post
                res = call(self.url(item, tail))
                self.assertEqual(res.status_code, 404, (item.item_id, tail))
                answers.add(json.dumps(res.data))
        self.assertEqual(len(answers), 1)
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.digest_enabled)
        missing = self.client.post(f"{LIST}999999/ai/off/")
        self.assertEqual(json.dumps(missing.data), next(iter(answers)))

    def test_a_forged_ai_block_on_the_wrong_kind_of_item_switches_nothing(self):
        forged = InboxItems.objects.create(
            team=self.team,
            receiver=self.user,
            item_type=1,
            item_body={"title": "t", "text": "x"},
            item_optionals={"ai": {"producer": "digest"}},
            request_status="pending",
        )
        self.assertEqual(self.client.post(self.url(forged, "off/")).status_code, 404)
        self.user.refresh_from_db()
        self.assertTrue(self.user.digest_enabled)

    def test_a_forged_block_naming_someone_elses_schedule_switches_nothing(self):
        theirs = schedules.create(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)),
            name="Theirs",
            job="plain",
            frequency="daily",
            time_of_day="10:00",
        )
        forged = InboxItems.objects.create(
            team=self.team,
            receiver=self.user,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_body={"title": "t", "text": "x"},
            item_optionals={"ai": {"producer": "schedule"}, "schedule_id": str(theirs.id)},
            request_status="",
        )
        res = self.client.post(self.url(forged, "off/"))
        self.assertEqual(res.status_code, 404)
        theirs.refresh_from_db()
        self.assertTrue(theirs.enabled)
        self.assertEqual(self.client.get(self.url(forged)).data["gone"], True)

    def test_a_schedule_block_on_an_item_of_another_type_switches_nothing(self):
        sched = self.schedule("plain", name="Mine")
        forged = InboxItems.objects.create(
            team=self.team,
            receiver=self.user,
            item_type=0,
            item_body={"title": "t", "text": "x"},
            item_optionals={"ai": {"producer": "schedule"}, "schedule_id": str(sched.id)},
        )
        self.assertEqual(self.client.post(self.url(forged, "off/")).status_code, 404)
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)

    def test_a_deleted_schedule_has_nothing_to_turn_off(self):
        item, sched = self.report_item()
        schedules.delete(self.ctx, sched.id)
        res = self.client.post(self.url(item, "off/"))
        self.assertEqual(res.status_code, 404)
        self.assertIn("no longer exists", res.data["error"])
        self.assertEqual(self.client.get(self.url(item)).data["gone"], True)

    def test_you_must_be_signed_in(self):
        item = self.digest_item()
        self.client.credentials()
        for call in (self.client.get, self.client.post):
            self.assertIn(call(self.url(item, "off/")).status_code, (401, 403))

    def test_the_list_gives_each_ai_item_its_source_and_leaves_the_others_alone(self):
        digest = self.digest_item()
        report, sched = self.report_item()
        plain = InboxItems.objects.create(
            team=self.team, receiver=self.user, item_type=0, item_body={"title": "t", "text": "x"}
        )
        res = self.client.get(f"{LIST}?team_id={self.team.team_id}")
        rows = {r["itemId"]: r for r in res.data["data"]["items"]}
        self.assertEqual(rows[digest.item_id]["aiSource"]["kind"], "digest")
        self.assertEqual(
            rows[report.item_id]["aiSource"],
            {
                "kind": "schedule",
                "enabled": True,
                "label": "Report",
                "schedule_id": str(sched.id),
                "used_ai": True,
            },
        )
        self.assertNotIn("aiSource", rows[plain.item_id])
        self.assertIn("ai", rows[digest.item_id]["itemOptionals"])
        self.client.post(self.url(report, "off/"))
        res = self.client.get(f"{LIST}?team_id={self.team.team_id}")
        rows = {r["itemId"]: r for r in res.data["data"]["items"]}
        self.assertFalse(rows[report.item_id]["aiSource"]["enabled"])
        self.assertTrue(rows[digest.item_id]["aiSource"]["enabled"])

    # -- every digest and every report has a switch, AI or not (MIP-1234) --

    def plain_report(self, name="Morning plan", user=None):
        """A report of a job that uses no AI, filed by the real delivery."""
        sched = self.schedule("plain", name=name)
        return schedule_delivery.deliver(sched, JOBS["plain"].run(self.ctx, {})), sched

    def test_a_report_that_used_no_ai_still_has_its_source_and_says_no_ai(self):
        item, sched = self.plain_report()
        self.assertNotIn("ai", item.item_optionals)
        res = self.client.get(self.url(item))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data,
            {
                "kind": "schedule",
                "enabled": True,
                "label": "Morning plan",
                "schedule_id": str(sched.id),
                "used_ai": False,
            },
        )
        rows = self.client.get(f"{LIST}?team_id={self.team.team_id}").data["data"]["items"]
        (row,) = [r for r in rows if r["itemId"] == item.item_id]
        self.assertEqual(row["aiSource"], res.data)
        self.assertNotIn("ai", row["itemOptionals"])

    def test_a_report_that_used_ai_says_so_and_a_digest_always_does(self):
        report, _ = self.report_item()
        digest = self.digest_item()
        # An older digest that kept no cost block is still an AI item.
        old_digest = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=6,
            item_body={"title": "Old", "text": "x"},
            item_optionals=None,
            request_status="",
        )
        for item in (report, digest, old_digest):
            self.assertIs(self.client.get(self.url(item)).data["used_ai"], True, item.item_id)

    def test_a_report_that_used_no_ai_is_turned_off_and_back_on_from_the_item(self):
        item, sched = self.plain_report()
        off = self.client.post(self.url(item, "off/"))
        self.assertEqual(off.status_code, 200, off.data)
        self.assertEqual(
            (off.data["kind"], off.data["enabled"], off.data["was_enabled"]),
            ("schedule", False, True),
        )
        sched.refresh_from_db()
        self.assertFalse(sched.enabled)
        self.assertEqual(sched.paused_reason, "manual")
        self.assertFalse(self.client.get(self.url(item)).data["enabled"])
        on = self.client.post(self.url(item, "on/"))
        self.assertTrue(on.data["enabled"])
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)
        self.assertTrue(self.reports().filter(pk=item.pk).exists(), "the item stays")

    def test_the_other_member_cannot_switch_a_plain_report_of_mine(self):
        item, sched = self.plain_report()
        self.authenticate(self.user2)
        self.assertEqual(self.client.get(self.url(item)).status_code, 404)
        self.assertEqual(self.client.post(self.url(item, "off/")).status_code, 404)
        sched.refresh_from_db()
        self.assertTrue(sched.enabled)

    def test_a_deleted_plain_report_item_is_a_404(self):
        item, _ = self.plain_report()
        InboxItems.objects.filter(pk=item.pk).update(is_deleted=True)
        self.assertEqual(self.client.post(self.url(item, "off/")).status_code, 404)

    def test_the_source_comes_from_the_type_not_from_the_blob(self):
        sched = self.schedule("plain", name="Mine")
        theirs = schedules.create(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)),
            name="Theirs",
            job="plain",
            frequency="daily",
            time_of_day="10:00",
        )
        for item_type in (0, 1, 2, 3, 4, 5, 7, 8, 9):
            forged = InboxItems.objects.create(
                team=self.team,
                receiver=self.user,
                item_type=item_type,
                item_body={"title": "t", "text": "x"},
                item_optionals={
                    "kind": "schedule_report",
                    "schedule_id": str(sched.id),
                    "job": "plain",
                    "ai": {"producer": "schedule"},
                },
                request_status="" if item_type in ACTIVITY_INBOX_ITEM_TYPES else "pending",
            )
            self.assertEqual(self.client.get(self.url(forged)).status_code, 404, item_type)
            self.assertEqual(self.client.post(self.url(forged, "off/")).status_code, 404, item_type)
        # A digest item whose blob names a schedule (someone else's, even) is only the digest.
        digest_forged = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=6,
            item_body={"title": "t", "text": "x"},
            item_optionals={"ai": {"producer": "schedule"}, "schedule_id": str(theirs.id)},
            request_status="",
        )
        res = self.client.post(self.url(digest_forged, "off/"))
        self.assertEqual((res.data["kind"], res.data["label"]), ("digest", "Genos digest"))
        for row in (sched, theirs):
            row.refresh_from_db()
            self.assertTrue(row.enabled)

    def test_a_report_without_a_real_schedule_id_has_no_source(self):
        for optionals in (
            None,
            {},
            {"schedule_id": ""},
            {"schedule_id": "nope"},
            {"schedule_id": 5},
            "x",
        ):
            InboxItems.objects.all().delete()
            item = InboxItems.objects.create(
                team=self.team,
                sender=None,
                receiver=self.user,
                item_type=ITEM_TYPE_GENOS_REPORT,
                item_body={"title": "t", "text": "x"},
                item_optionals=optionals,
                request_status="",
            )
            self.assertEqual(self.client.get(self.url(item)).status_code, 404, optionals)

    def test_a_plain_report_naming_someone_elses_schedule_switches_nothing(self):
        theirs = schedules.create(
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id)),
            name="Theirs",
            job="plain",
            frequency="daily",
            time_of_day="10:00",
        )
        forged = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_body={"title": "t", "text": "x"},
            item_optionals={
                "kind": "schedule_report",
                "schedule_id": str(theirs.id),
                "job": "plain",
            },
            request_status="",
        )
        self.assertTrue(self.client.get(self.url(forged)).data["gone"])
        self.assertEqual(self.client.post(self.url(forged, "off/")).status_code, 404)
        theirs.refresh_from_db()
        self.assertTrue(theirs.enabled)

    def test_an_ai_block_of_the_wrong_producer_does_not_make_a_report_an_ai_report(self):
        sched = self.schedule("plain", name="Mine")
        item = InboxItems.objects.create(
            team=self.team,
            sender=None,
            receiver=self.user,
            item_type=ITEM_TYPE_GENOS_REPORT,
            item_body={"title": "t", "text": "x"},
            item_optionals={
                "kind": "schedule_report",
                "schedule_id": str(sched.id),
                "ai": {"producer": "digest"},
            },
            request_status="",
        )
        res = self.client.get(self.url(item))
        self.assertEqual((res.data["kind"], res.data["used_ai"]), ("schedule", False))

    def test_a_deleted_schedules_report_still_says_whether_it_used_ai(self):
        plain, plain_sched = self.plain_report()
        smart, smart_sched = self.report_item()
        schedules.delete(self.ctx, plain_sched.id)
        schedules.delete(self.ctx, smart_sched.id)
        for item, used in ((plain, False), (smart, True)):
            res = self.client.get(self.url(item))
            self.assertTrue(res.data["gone"])
            self.assertIs(res.data["used_ai"], used)

    def test_the_list_gives_every_report_and_digest_a_source_and_nothing_else_one(self):
        plain_report, _ = self.plain_report()
        ai_report, _ = self.report_item(self.schedule("smart", name="Smart", at="10:30"))
        digest = self.digest_item()
        notice = InboxItems.objects.create(
            team=self.team, receiver=self.user, item_type=0, item_body={"title": "t", "text": "x"}
        )
        reminder = InboxItems.objects.create(
            team=self.team, receiver=self.user, item_type=9, item_body={"title": "t", "text": "x"}
        )
        rows = {
            r["itemId"]: r
            for r in self.client.get(f"{LIST}?team_id={self.team.team_id}").data["data"]["items"]
        }
        self.assertIs(rows[plain_report.item_id]["aiSource"]["used_ai"], False)
        self.assertIs(rows[ai_report.item_id]["aiSource"]["used_ai"], True)
        self.assertIs(rows[digest.item_id]["aiSource"]["used_ai"], True)
        self.assertNotIn("aiSource", rows[notice.item_id])
        self.assertNotIn("aiSource", rows[reminder.item_id])

    def test_the_list_costs_one_query_for_all_the_schedules(self):
        for n in range(3):
            self.report_item(self.schedule("smart", name=f"R{n}", at=f"1{n}:00"))
        from django.db import connection  # noqa: PLC0415
        from django.test.utils import CaptureQueriesContext  # noqa: PLC0415

        with CaptureQueriesContext(connection) as q:
            self.client.get(f"{LIST}?team_id={self.team.team_id}")
        schedule_reads = [s for s in q.captured_queries if "origin_memberschedule" in s["sql"]]
        self.assertEqual(len(schedule_reads), 1)

    def test_the_cap_of_one_digest_flag_is_the_members_own(self):
        item = self.digest_item()
        self.client.post(self.url(item, "off/"))
        self.user2.refresh_from_db()
        self.assertTrue(self.user2.digest_enabled)


class TestTheSentences(SimpleTestCase):
    def test_a_uuid_is_checked_before_it_is_looked_up(self):
        self.assertTrue(re.match(inbox_ai._UUID.pattern, str(uuid.uuid4())))
