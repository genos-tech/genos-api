"""The grouped-eval pieces of MIP-1059 (arm `parity`, case file `t1059-calendar.yaml`, seeds `calendar_*`).

No model runs and nothing reaches Google. The paid session measures the parity batch in ONE run, so what has to
be right before it is the files: every case exists in both languages, the positives need the arm (they cannot
pass without `account`, the new tool and the citation shape being declared) and the negatives run on arm0, the
cases name a viewer and fixture that exist, the seeds write the accounts the cases quote, the in-process
Google (`calendar_stub`) answers each account only its own events and refuses a write, and each case's own
`pending_write` spec passes on the approval card the real card code builds for a proposal it should accept,
and fails on the proposals it should not.
"""

from __future__ import annotations

import re
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.models.common.user_models import ConnectedAccount
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.group import (
    calendar_stub,
    content_checks,
    harness,
    seeds,
    sequence,
)
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.create_calendar_event import CREATE_CALENDAR_EVENT
from origin.search_engine.agent.tools.list_calendar_events import LIST_CALENDAR_EVENTS
from origin.search_engine.agent.tools.list_calendars import LIST_CALENDARS
from origin.services.oauth.accounts import accounts_for

from .test_base import BaseAPITestCase

FLAG = "AGENT_PARITY_TOOLS"
POSITIVES = ("read-second", "cite-event", "create-named", "delete-second")
NEGATIVES = ("look-only", "one-account", "one-account-named")
SLUGS = (*POSITIVES, *NEGATIVES)
WRITES = ["create_calendar_event", "update_calendar_event", "delete_calendar_event"]
PERSONAL = "eval.personal@genos-eval.example"
WORK = "eval.work@genos-eval.example"


def cases():
    return [c for c in harness.load_cases(include_legacy=False) if "t1059" in c.tags]


class CaseFileTests(SimpleTestCase):
    def setUp(self):
        self.cases = cases()
        self.by_id = {c.id: c for c in self.cases}

    def test_every_case_is_there_in_both_languages_and_nothing_else(self):
        for slug in SLUGS:
            for lang in ("en", "ja"):
                self.assertIn(f"t1059.{slug}.{lang}", self.by_id)
        self.assertEqual(len(self.cases), 2 * len(SLUGS))

    def test_ids_are_unique_and_each_language_has_a_twin(self):
        ids = [c.id for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))
        for c in self.cases:
            twin = c.id[:-2] + ("ja" if c.lang == "en" else "en")
            self.assertIn(twin, self.by_id, c.id)

    def test_each_language_has_a_positive_a_negative_and_an_edge(self):
        for lang in ("en", "ja"):
            slugs = {c.id.split(".")[1] for c in self.cases if c.lang == lang}
            self.assertGreaterEqual(len(slugs & set(POSITIVES)), 1)
            self.assertGreaterEqual(len(slugs & set(NEGATIVES)), 2)
            self.assertGreaterEqual(len(slugs), 3)

    def test_japanese_cases_are_asked_in_japanese_by_a_japanese_viewer(self):
        for c in self.cases:
            if c.lang == "ja":
                self.assertRegex(c.raw["query"], r"[぀-ヿ㐀-鿿]", c.id)
                self.assertIn(c.raw["viewer"], {"ja-b", "ja"}, c.id)
            else:
                self.assertIn(c.raw["viewer"], {"b", "c"}, c.id)

    def test_every_viewer_is_one_the_scenario_has_and_every_case_names_a_calendar_seed(self):
        for c in self.cases:
            self.assertIn(c.raw["viewer"], sequence.VIEWERS, c.id)
            self.assertEqual(c.raw["fixture"], "sequence", c.id)
            slug = c.id.split(".")[1]
            wanted = (
                "calendar_one_account"
                if slug.startswith("one-account")
                else "calendar_two_accounts"
            )
            self.assertEqual(c.raw["seed"], [wanted], c.id)
            _team, person = sequence.VIEWERS[c.raw["viewer"]]
            self.assertIn(person, seeds.VISIBLE_TO[wanted], c.id)

    def test_positives_need_the_arm_and_the_negatives_run_on_arm0(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            if slug in POSITIVES:
                self.assertEqual(c.requires_arm, "parity", c.id)
            else:
                self.assertFalse(c.requires_arm, c.id)

    def test_every_check_key_is_one_the_harness_knows_and_every_pattern_compiles(self):
        known = {"tool", "args_present", "args_absent", "args_match", "task_args_match"}
        for c in self.cases:
            spec = (c.raw["expect"] or {}).get("pending_write")
            if spec is None:
                continue
            self.assertFalse(set(spec) - known, c.id)
            for pattern in (spec.get("args_match") or {}).values():
                re.compile(pattern)

    def test_the_writes_judge_the_proposal_and_the_reads_forbid_every_calendar_write(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            expect = c.raw["expect"]
            if slug in ("create-named", "one-account", "one-account-named"):
                self.assertEqual(expect["pending_tools_contains"], ["create_calendar_event"], c.id)
                self.assertEqual(expect["pending_write"]["tool"], "create_calendar_event", c.id)
            elif slug == "delete-second":
                self.assertEqual(expect["pending_tools_contains"], ["delete_calendar_event"], c.id)
            else:
                self.assertLessEqual(set(WRITES), set(expect["pending_tools_excludes"]), c.id)

    def test_a_member_with_one_account_must_send_no_account(self):
        for lang in ("en", "ja"):
            spec = self.by_id[f"t1059.one-account.{lang}"].raw["expect"]["pending_write"]
            self.assertEqual(spec["args_absent"], ["account"])

    def test_a_member_with_one_account_who_names_it_must_land_on_it_whatever_the_model_looks_up(
        self,
    ):
        # The hint can send this member to `list_calendars` (it cannot know the count before it looks): the
        # case allows that and holds the proposal to the one account, by the card, not by an argument.
        for lang in ("en", "ja"):
            spec = self.by_id[f"t1059.one-account-named.{lang}"].raw["expect"]["pending_write"]
            self.assertEqual(spec["args_match"]["account_label"], WORK, lang)
            self.assertEqual(spec["args_match"]["account_count"], "^1$", lang)
            self.assertNotIn("args_absent", spec, lang)
            self.assertNotIn("account", spec["args_present"], lang)
            expect = self.by_id[f"t1059.one-account-named.{lang}"].raw["expect"]
            self.assertNotIn("tools_used_excludes", expect, lang)
            self.assertIn("parity", self.by_id[f"t1059.one-account-named.{lang}"].tags)

    def test_a_member_with_one_account_is_not_sent_to_list_calendars(self):
        # The account hint (`create_calendar_event`'s flag-on sentence) must add no call for a member who names
        # no account: the guard that fails when it makes the model look up an account nobody asked for.
        for lang in ("en", "ja"):
            expect = self.by_id[f"t1059.one-account.{lang}"].raw["expect"]
            self.assertEqual(expect["tools_used_excludes"], ["list_calendars"], lang)

    def test_the_named_account_cases_quote_the_personal_address_the_card_will_carry(self):
        for slug in ("create-named", "delete-second"):
            for lang in ("en", "ja"):
                match = self.by_id[f"t1059.{slug}.{lang}"].raw["expect"]["pending_write"][
                    "args_match"
                ]
                self.assertEqual(match["account_label"], PERSONAL)

    def test_the_cases_quote_only_titles_the_seeds_have(self):
        for c in self.cases:
            slug = c.id.split(".")[1]
            name = (
                "calendar_one_account"
                if slug.startswith("one-account")
                else "calendar_two_accounts"
            )
            texts = set(seeds.seed_texts(name, c.lang))
            # The "Team lunch" and "Standup" events are what the member asks to ADD: not seeded.
            added = {"Team lunch", "チームランチ", "Standup", "朝会"}
            for quoted in re.findall(r'"([^"]+)"|「([^」]+)」', c.raw["query"]):
                text = quoted[0] or quoted[1]
                if text not in added:
                    self.assertIn(text, texts, f"{c.id}: {text!r}")

    def test_the_dates_are_far_from_any_this_week(self):
        # A case that names a date names one in March 2027 (the seeds' own, `calendar_stub`): far from any
        # "this week" a neighbouring case could ask about. Some cases name no date.
        for c in self.cases:
            self.assertLessEqual(set(re.findall(r"\d{4}", c.raw["query"])), {"2027"}, c.id)

    def test_the_seeds_say_who_reads_them_and_have_text_in_both_languages(self):
        for name in ("calendar_two_accounts", "calendar_one_account"):
            self.assertIn(name, seeds.SEEDS)
            for lang in ("en", "ja"):
                self.assertTrue(seeds.seed_texts(name, lang), (name, lang))
        self.assertEqual(seeds.VISIBLE_TO["calendar_two_accounts"], frozenset({"b"}))
        self.assertEqual(seeds.VISIBLE_TO["calendar_one_account"], frozenset({"a", "c"}))


class WhatTheRunIsDeclaredTests(SimpleTestCase):
    """The behaviour runner applies NO tier or reach gate (only a case's own `agent_tool_level`, which none of
    these sets), so a case on the `parity` arm is declared the calendar tools whatever tier the sequence viewer
    has. Without this the positives could fail on a gate, not on the model."""

    CALENDAR = {
        "list_calendars",
        "list_calendar_events",
        "get_calendar_event",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
    }

    def test_no_case_sets_an_agency_level(self):
        for c in cases():
            self.assertNotIn("agent_tool_level", c.raw, c.id)

    def test_the_arm_is_declared_every_calendar_tool_and_arm0_is_not_the_new_one(self):
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True}):
            on = {d.name for d in controller._build_tool_declarations(None)}
        with mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: False}):
            off = {d.name for d in controller._build_tool_declarations(None)}
        self.assertLessEqual(self.CALENDAR, on)
        self.assertEqual(self.CALENDAR - off, {"get_calendar_event"})


def _stream(*, answer: str, proposed: tuple[str, ...] = (), used: tuple[str, ...] = ()):
    events = [{"type": "tool_call_start", "tool_name": name, "step": 0} for name in used]
    events += [
        {"type": "tool_call_pending_approval", "tool_name": name, "step": 1, "arguments": {}}
        for name in proposed
    ]
    events.append({"type": "answer_delta", "text": answer})
    return events


class ChecksAreNotVacuousTests(SimpleTestCase):
    def reasons(self, case_id, **kw):
        case = {c.id: c for c in cases()}[case_id]
        return runner._check_behavior_expectations(_stream(**kw), case.raw["expect"])

    def test_the_reads_fail_a_model_that_proposed_a_write_and_pass_one_that_did_not(self):
        for slug in ("look-only", "read-second", "cite-event"):
            for lang in ("en", "ja"):
                case_id = f"t1059.{slug}.{lang}"
                bad = self.reasons(
                    case_id,
                    answer="Done. 15:00 15時 午後3時",
                    proposed=("delete_calendar_event",),
                    used=("list_calendar_events",),
                )
                self.assertTrue(
                    [r for r in bad if r.startswith("pending_tools_excludes")], (case_id, bad)
                )

    def test_one_account_fails_a_model_that_looked_up_accounts_it_was_not_asked_about(self):
        for lang in ("en", "ja"):
            case_id = f"t1059.one-account.{lang}"
            looked = self.reasons(
                case_id, answer="", proposed=("create_calendar_event",), used=("list_calendars",)
            )
            self.assertTrue([r for r in looked if r.startswith("tools_used_excludes")], looked)
            straight = self.reasons(case_id, answer="", proposed=("create_calendar_event",))
            self.assertEqual([r for r in straight if r.startswith("tools_used_excludes")], [])

    def test_read_second_needs_the_time_in_the_answer_and_a_calendar_read(self):
        ok = self.reasons(
            "t1059.read-second.en", answer="It is at 15:00.", used=("get_calendar_event",)
        )
        self.assertEqual(ok, [])
        no_time = self.reasons(
            "t1059.read-second.en", answer="I could not find it.", used=("list_calendar_events",)
        )
        self.assertTrue([r for r in no_time if r.startswith("answer_contains_any")])
        no_tool = self.reasons("t1059.read-second.en", answer="It is at 15:00.")
        self.assertTrue([r for r in no_tool if r.startswith("tools_used_contains_any")])
        ja = self.reasons(
            "t1059.read-second.ja", answer="午後3時です。", used=("get_calendar_event",)
        )
        self.assertEqual(ja, [])

    def test_the_citation_case_sees_a_calendar_event_token_in_the_scanner_the_runner_pins(self):
        # `runner._CITATION_RE` is deliberately separate and narrow (eval scoring is pinned to it): the
        # event token has to be one it finds, or the case could never pass.
        token = "calendar_event:0a8f1f5e-6a0b-4a52-9c2b-0b3b6f1d7a11:evalhome2"
        for form in (f"[Piano lesson]({token})", f"[{token}]"):
            found = {
                (m.group(1) or m.group(2)).lower()
                for m in runner._CITATION_RE.finditer(form.lower())
            }
            self.assertEqual(found, {token}, form)
        for lang in ("en", "ja"):
            case_id = f"t1059.cite-event.{lang}"
            cited = self.reasons(
                case_id,
                answer=f"It is on March 4: [lesson]({token}).",
                used=("get_calendar_event",),
            )
            self.assertEqual(cited, [], cited)
            plain = self.reasons(case_id, answer="It is on March 4.", used=("get_calendar_event",))
            self.assertTrue([r for r in plain if "citation" in r or "calendar_event" in r], plain)


class SeedAndCardTests(BaseAPITestCase):
    """The seed writes the accounts the cases name, the in-process Google answers each only its own events
    and no write, and the approval card built for a proposal carries what `pending_write` is asked to match."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {FLAG: True})
        patcher.start()
        self.addCleanup(patcher.stop)
        # The real `requests` must never be called: the fake is in front of every Google call.
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
        self.ctx = ToolContext(team_id=self.info["team_id"], user_id=self.info["user_id"])
        self.by_id = {c.id: c for c in cases()}
        seeds.apply(["calendar_two_accounts"], self.info)
        self.accounts = list(accounts_for(self.user, "google"))
        self.work, self.home = self.accounts

    def test_the_seed_gives_the_viewer_two_google_accounts_the_work_one_first(self):
        self.assertEqual([a.label for a in self.accounts], [calendar_stub.LABELS["work"], PERSONAL])
        self.assertEqual(self.info["default_account_id"], str(self.work.id))
        for account in self.accounts:
            self.assertIn("https://www.googleapis.com/auth/calendar.events", account.scopes)

    def test_the_seed_is_idempotent_and_finishes_a_half_done_run(self):
        seeds.apply(["calendar_two_accounts"], self.info)
        self.assertEqual(
            ConnectedAccount.objects.filter(user=self.user, provider="google").count(), 2
        )
        self.home.delete()
        seeds.apply(["calendar_two_accounts"], self.info)
        self.assertEqual(
            ConnectedAccount.objects.filter(user=self.user, provider="google").count(), 2
        )

    def test_each_account_answers_only_its_own_events(self):
        default = LIST_CALENDAR_EVENTS.run({}, self.ctx)
        self.assertEqual([e["id"] for e in default["events"]], ["evalwork1"])
        second = LIST_CALENDAR_EVENTS.run({"account": str(self.home.id)}, self.ctx)
        self.assertEqual([e["id"] for e in second["events"]], ["evalhome1", "evalhome2"])
        calendars = LIST_CALENDARS.run({}, self.ctx)
        self.assertEqual(
            {a["label"] for a in calendars["accounts"]},
            {calendar_stub.LABELS["work"], calendar_stub.LABELS["home"]},
        )

    def test_the_japanese_team_reads_japanese_titles(self):
        ja = dict(self.info, team="ja", person="b")
        # Another member of the same Django user, as a second team's viewer would be: separate accounts.
        ConnectedAccount.objects.filter(user=self.user, provider="google").delete()
        seeds.apply(["calendar_two_accounts"], ja)
        second = accounts_for(self.user, "google")[1]
        out = LIST_CALENDAR_EVENTS.run({"account": str(second.id)}, self.ctx)
        self.assertIn("歯医者の予約", out["events"][0]["summary"])

    def test_a_write_that_reaches_the_fake_is_a_bug_in_the_case(self):
        with self.assertRaisesRegex(AssertionError, "approves no write"):
            CREATE_CALENDAR_EVENT.run(
                {
                    "summary": "x",
                    "start_iso": "2027-03-10T12:00:00",
                    "end_iso": "2027-03-10T13:00:00",
                },
                self.ctx,
            )

    def test_the_fake_leaves_every_other_user_of_requests_alone(self):
        import requests  # noqa: PLC0415

        from origin.search_engine.agent import calendar  # noqa: PLC0415

        self.assertIsNot(calendar.requests, requests)
        calendar_stub.remove()
        self.assertIs(calendar.requests, requests)

    def check(self, case_id, tool, proposal):
        card = controller._friendly_arguments(proposal, tool, ctx=self.ctx, card=True)
        spec = self.by_id[case_id].raw["expect"]["pending_write"]
        out = content_checks.check_pending(spec, [{"tool_name": tool, "arguments": card}])
        return {c["name"]: c["ok"] for c in out}, card

    def test_create_named_passes_on_the_second_account_and_fails_on_the_default_or_none(self):
        base = {
            "summary": "Team lunch",
            "start_iso": "2027-03-10T12:00:00",
            "end_iso": "2027-03-10T13:00:00",
            "time_zone": "Asia/Tokyo",
        }
        checks, card = self.check(
            "t1059.create-named.en", "create_calendar_event", {**base, "account": str(self.home.id)}
        )
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["account_label"], PERSONAL)
        for wrong in ({**base, "account": str(self.work.id)}, base):
            checks, _ = self.check("t1059.create-named.en", "create_calendar_event", wrong)
            self.assertFalse(all(checks.values()), wrong)

    def test_delete_second_passes_on_the_right_event_and_account_and_fails_otherwise(self):
        right = {"event_id": "evalhome1", "account": str(self.home.id)}
        checks, card = self.check("t1059.delete-second.en", "delete_calendar_event", right)
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["current"]["summary"], "Dentist appointment")
        for wrong in (
            {"event_id": "evalhome2", "account": str(self.home.id)},
            {"event_id": "evalhome1", "account": str(self.work.id)},
            {"event_id": "evalhome1"},
        ):
            checks, _ = self.check("t1059.delete-second.en", "delete_calendar_event", wrong)
            self.assertFalse(all(checks.values()), wrong)

    def test_the_japanese_delete_card_names_the_japanese_event(self):
        ConnectedAccount.objects.filter(user=self.user, provider="google").delete()
        seeds.apply(["calendar_two_accounts"], dict(self.info, team="ja", person="b"))
        home = accounts_for(self.user, "google")[1]
        checks, card = self.check(
            "t1059.delete-second.ja",
            "delete_calendar_event",
            {"event_id": "evalhome1", "account": str(home.id)},
        )
        self.assertTrue(all(checks.values()), (checks, card))

    def test_one_account_named_passes_with_or_without_its_id_and_fails_on_a_made_up_one(self):
        ConnectedAccount.objects.filter(user=self.user, provider="google").delete()
        seeds.apply(["calendar_one_account"], dict(self.info, person="c"))
        (only,) = accounts_for(self.user, "google")
        base = {
            "summary": "Standup",
            "start_iso": "2027-03-12T09:00:00",
            "end_iso": "2027-03-12T09:15:00",
            "time_zone": "Asia/Tokyo",
        }
        for proposal in (base, {**base, "account": str(only.id)}):
            checks, card = self.check(
                "t1059.one-account-named.en", "create_calendar_event", proposal
            )
            self.assertTrue(all(checks.values()), (checks, card))
            self.assertEqual(card["account_label"], WORK)
            self.assertEqual(card["account_count"], 1)
        made_up = {**base, "account": "11111111-1111-4111-8111-111111111111"}
        checks, _ = self.check("t1059.one-account-named.en", "create_calendar_event", made_up)
        self.assertFalse(all(checks.values()))

    def test_one_account_passes_without_an_account_and_fails_with_one(self):
        ConnectedAccount.objects.filter(user=self.user, provider="google").delete()
        seeds.apply(["calendar_one_account"], dict(self.info, person="c"))
        (only,) = accounts_for(self.user, "google")
        base = {
            "summary": "Standup",
            "start_iso": "2027-03-11T09:00:00",
            "end_iso": "2027-03-11T09:15:00",
            "time_zone": "Asia/Tokyo",
        }
        checks, card = self.check("t1059.one-account.en", "create_calendar_event", base)
        self.assertTrue(all(checks.values()), (checks, card))
        self.assertEqual(card["account_count"], 1)
        checks, _ = self.check(
            "t1059.one-account.en", "create_calendar_event", {**base, "account": str(only.id)}
        )
        self.assertFalse(all(checks.values()))
