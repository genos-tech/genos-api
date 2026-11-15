"""The grouped-eval pieces of MIP-1607 (arm `followups`, case file `t1607-calendar-all.yaml`). No model runs.

What has to be right before the paid run is the files and the checks: every case exists in both languages, the
positives need the arm (`account: "all"` is not accepted without the flag) and the negatives run on arm0 with the
arm's tag (`test_agent_eval_arm_coverage` holds that for every arm), the cases quote only what the seeds hold, and the
new check, `tool_calls_match` (count and arguments of the READ calls), judges what it says it judges: it fails a model
that read each account separately and passes one that sent one call with `account: all`.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import calendar_stub, content_checks, harness, seeds
from origin.search_engine.agent.parity import FOLLOWUPS_FLAG, PARITY_FLAG
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.services.oauth.accounts import accounts_for

from .test_base import BaseAPITestCase

POSITIVES = ("all-accounts", "all-accounts-unprompted")
NEGATIVES = ("one-account", "named-account")
WRITES = ["create_calendar_event", "update_calendar_event", "delete_calendar_event"]


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1607" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_case_is_there_in_both_languages_and_nothing_else(self):
        for slug in (*POSITIVES, *NEGATIVES):
            for lang in ("en", "ja"):
                self.assertIn(f"t1607.{slug}.{lang}", self.by_id)
        self.assertEqual(len(self.cases), 2 * (len(POSITIVES) + len(NEGATIVES)))
        for c in self.cases:
            twin = c.id[:-2] + ("ja" if c.lang == "en" else "en")
            self.assertIn(twin, self.by_id, c.id)

    def test_japanese_cases_are_asked_in_japanese_by_a_japanese_viewer(self):
        for c in self.cases:
            if c.lang == "ja":
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)
                self.assertTrue(str(c.raw["viewer"]).startswith("ja"), c.id)
            else:
                self.assertFalse(str(c.raw["viewer"]).startswith("ja"), c.id)

    def test_positives_need_the_arm_and_negatives_run_on_arm0_with_the_arms_tag(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in POSITIVES:
                self.assertEqual(c.requires_arm, "followups", c.id)
                self.assertNotIn("followups", c.tags, c.id)
            else:
                self.assertFalse(c.requires_arm, c.id)
                self.assertIn("followups", c.tags, c.id)
            self.assertIn("t1607", c.tags)
            self.assertIn(harness.GROUP_TAG, c.tags)

    def test_every_viewer_has_the_seed_that_gives_them_the_accounts_the_case_assumes(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            want = "calendar_one_account" if slug == "one-account" else "calendar_two_accounts"
            self.assertEqual(c.raw["seed"], [want], c.id)
            self.assertEqual(c.raw["fixture"], "sequence", c.id)
            viewer = c.raw["viewer"]
            # `ja` is the Japanese team's `a`
            person = "a" if viewer == "ja" else viewer.removeprefix("ja-")
            self.assertIn(person, seeds.VISIBLE_TO[want], c.id)

    def test_the_cases_judge_reads_and_forbid_every_calendar_write(self):
        for c in self.cases:
            expect = c.raw["expect"]
            self.assertLessEqual(set(WRITES), set(expect["pending_tools_excludes"]), c.id)
            self.assertNotIn("pending_write", expect, c.id)
            self.assertIn("list_calendar_events", expect["tools_used_contains"], c.id)

    def test_the_positive_wants_one_events_call_that_sends_all_and_at_most_one_lookup(self):
        for lang in ("en", "ja"):
            specs = self.by_id[f"t1607.all-accounts.{lang}"].raw["expect"]["tool_calls_match"]
            by_tool = {s["tool"]: s for s in specs}
            self.assertEqual(by_tool["list_calendar_events"]["count"], 1)
            self.assertEqual(by_tool["list_calendar_events"]["args_match"], {"account": "^all$"})
            self.assertEqual(
                by_tool["list_calendars"], {"tool": "list_calendars", "count_at_most": 1}
            )

    def test_a_member_with_one_account_sends_no_account_and_is_not_sent_to_list_calendars(self):
        for lang in ("en", "ja"):
            expect = self.by_id[f"t1607.one-account.{lang}"].raw["expect"]
            self.assertEqual(expect["tools_used_excludes"], ["list_calendars"])
            self.assertEqual(
                expect["tool_calls_match"],
                [{"tool": "list_calendar_events", "count": 1, "args_absent": ["account"]}],
            )

    def test_the_member_who_names_an_account_must_not_be_answered_with_all_of_them(self):
        for lang in ("en", "ja"):
            c = self.by_id[f"t1607.named-account.{lang}"]
            spec = c.raw["expect"]["tool_calls_match"][0]
            self.assertTrue(re.search(spec["args_match"]["account"], "uuid-1234"))
            self.assertFalse(re.search(spec["args_match"]["account"], "all"))
            self.assertTrue(c.raw["expect"]["answer_does_not_contain"])

    def test_every_check_key_is_one_the_harness_knows_and_every_pattern_compiles(self):
        known_spec = {
            "tool",
            "count",
            "count_at_most",
            "count_at_least",
            *content_checks.ARGUMENT_KEYS,
        }
        self.assertIn("tool_calls_match", harness.GROUP_EXPECT_KEYS)
        for c in self.cases:
            for spec in c.raw["expect"].get("tool_calls_match") or []:
                self.assertFalse(set(spec) - known_spec, c.id)
                for pattern in (spec.get("args_match") or {}).values():
                    re.compile(pattern)

    def test_the_cases_quote_only_titles_the_seeds_have_and_name_dates_in_march_2027(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            name = "calendar_one_account" if slug == "one-account" else "calendar_two_accounts"
            texts = set(seeds.seed_texts(name, c.lang))
            for contains in c.raw["expect"].get("answer_contains_all") or []:
                self.assertTrue(any(contains in t for t in texts), f"{c.id}: {contains!r}")
            self.assertLessEqual(set(re.findall(r"\d{4}", c.raw["query"])), {"2027"}, c.id)

    def test_the_cases_use_the_arm_that_exists(self):
        arms = harness.load_arms()
        self.assertEqual(arms["followups"].effective, {FOLLOWUPS_FLAG: True})


class ToolCallChecksTests(SimpleTestCase):
    """`tool_calls_match` is not vacuous: it fails the behaviour the cases are written to catch."""

    @staticmethod
    def call(name, **arguments):
        return {"tool_name": name, "arguments": arguments, "result": {}}

    def verdict(self, specs, calls):
        out = content_checks.check_tool_calls(specs, calls)
        return all(c["ok"] for c in out), out

    ONE_ALL = [{"tool": "list_calendar_events", "count": 1, "args_match": {"account": "^all$"}}]

    def test_one_call_with_all_passes(self):
        ok, _ = self.verdict(
            self.ONE_ALL,
            [self.call("list_calendars"), self.call("list_calendar_events", account="all")],
        )
        self.assertTrue(ok)

    def test_each_account_read_separately_fails_on_the_count_and_on_the_argument(self):
        calls = [
            self.call("list_calendar_events", account="uuid-1"),
            self.call("list_calendar_events", account="uuid-2"),
        ]
        ok, out = self.verdict(self.ONE_ALL, calls)
        self.assertFalse(ok)
        names = {c["name"] for c in out if not c["ok"]}
        self.assertIn("tool_calls_match.list_calendar_events.count", names)
        self.assertIn("tool_calls_match.list_calendar_events[0].args_match", names)

    def test_a_default_read_fails_the_all_case_and_no_read_fails_the_count(self):
        self.assertFalse(self.verdict(self.ONE_ALL, [self.call("list_calendar_events")])[0])
        ok, out = self.verdict(self.ONE_ALL, [])
        self.assertFalse(ok)
        self.assertEqual([c["name"] for c in out], ["tool_calls_match.list_calendar_events.count"])

    def test_args_absent_fails_a_member_with_one_account_who_was_sent_an_account(self):
        spec = [{"tool": "list_calendar_events", "count": 1, "args_absent": ["account"]}]
        self.assertTrue(self.verdict(spec, [self.call("list_calendar_events")])[0])
        self.assertFalse(self.verdict(spec, [self.call("list_calendar_events", account="all")])[0])
        self.assertFalse(self.verdict(spec, [self.call("list_calendar_events", account="uuid")])[0])
        self.assertTrue(self.verdict(spec, [self.call("list_calendar_events", account="")])[0])

    def test_count_at_most_and_at_least(self):
        two = [self.call("list_calendars"), self.call("list_calendars")]
        self.assertFalse(self.verdict([{"tool": "list_calendars", "count_at_most": 1}], two)[0])
        self.assertTrue(self.verdict([{"tool": "list_calendars", "count_at_most": 2}], two)[0])
        self.assertFalse(self.verdict([{"tool": "list_calendars", "count_at_least": 3}], two)[0])
        self.assertTrue(self.verdict([{"tool": "list_calendars", "count_at_most": 1}], [])[0])

    def test_another_tools_calls_are_not_counted(self):
        calls = [
            self.call("list_calendars", account="x"),
            self.call("list_calendar_events", account="all"),
        ]
        self.assertTrue(self.verdict(self.ONE_ALL, calls)[0])

    def test_a_spec_without_a_tool_is_a_failed_check_not_a_silent_pass(self):
        self.assertFalse(self.verdict([{"count": 1}], [])[0])

    def test_group_checks_wires_it_in_and_a_run_without_the_key_gets_no_new_check(self):
        got = harness.group_checks(
            {"tool_calls_match": self.ONE_ALL},
            answer="",
            tools=[],
            tool_text="",
            calls=1,
            tool_calls=[self.call("list_calendar_events", account="all")],
        )
        self.assertTrue(all(c["ok"] for c in got) and got)
        self.assertEqual(harness.group_checks({}, answer="", tools=[], tool_text="", calls=1), [])


class StubRunTests(BaseAPITestCase):
    """The positive is answerable: with the seeds and the in-process Google, ONE `account: all` call returns the
    work event and both personal ones, each on its own account."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {PARITY_FLAG: True, FOLLOWUPS_FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)
        network = mock.patch("requests.request", side_effect=AssertionError("network"))
        network.start()
        self.addCleanup(network.stop)
        self.addCleanup(calendar_stub.remove)
        self.info = {
            "team": "v1",
            "person": "b",
            "team_id": str(self.team.team_id),
            "user_id": str(self.user.id),
        }
        seeds.apply(["calendar_two_accounts"], self.info)
        self.ctx = ToolContext(team_id=self.info["team_id"], user_id=self.info["user_id"])

    def test_one_all_call_returns_the_events_of_both_accounts(self):
        work, home = list(accounts_for(self.user, "google"))
        out = LIST_CALENDAR_EVENTS.run({"account": "all"}, self.ctx)
        self.assertEqual(
            [(e["id"], e["account_id"]) for e in out["events"]],
            [
                ("evalwork1", str(work.id)),
                ("evalhome1", str(home.id)),
                ("evalhome2", str(home.id)),
            ],
        )
        self.assertEqual(
            [a["label"] for a in out["accounts"]],
            [calendar_stub.LABELS["work"], calendar_stub.LABELS["home"]],
        )
        self.assertNotIn("failed_accounts", out)
