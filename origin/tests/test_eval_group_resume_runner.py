"""`kind: resume` runner of the grouped eval (`evals/group/resume_runner.py`) and its case files.

NO REAL MODEL. The model under test is a stub that records the exact prompt it is sent. What
this proves without spending anything:

  * the runner drives the PRODUCTION ask -> pause -> decide path (the real views, the loop, the
    real write tool, the production undo) in a team of its own, and tears it down;
  * a flag changes what the resumed leg is sent, in the intended way: the thread / note block, the
    @/# references and the earlier turns (977, 991), the reject reason and rule 7 (889), the edit
    note and the undo note (889), the closing line in the thread (1283), per arm;
  * every case in `group/t*.yaml` is well formed: its world builds, its first leg pauses, and the
    prompt checks of a positive case FAIL in arm0 and PASS in the case's arm, while those of a
    negative case are the same in both (a case that passes in arm0 measures nothing);
  * spend is attributed to the case (the views run the loop on a bare thread: without the
    binding every call would be `UNATTRIBUTED`).

`TransactionTestCase`, as the resume tests: both legs stream on real worker threads whose writes
must really commit.
"""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import yaml
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TransactionTestCase

from origin.models.common.team_models import TeamMaster
from origin.search_engine import agent_views
from origin.search_engine.agent import controller
from origin.search_engine.agent.evals.group import resume_runner
from origin.search_engine.agent.languages import language_directive
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage, FunctionCall

GROUP_DIR = Path(resume_runner.__file__).parent
MY_TAGS = ("t977", "t991", "t1283", "t889-reason", "t889-undo", "t1530")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def cases(tag: str | None = None) -> list[dict[str, Any]]:
    out = []
    for path in sorted(GROUP_DIR.glob("t*.yaml")):
        if tag and path.stem != tag:
            continue
        out += [
            c for c in yaml.safe_load(path.read_text(encoding="utf-8")) if c.get("kind") == "resume"
        ]
    return out


def arm_overrides(tag: str) -> dict[str, Any]:
    """The effective overrides of an arm, `requires` resolved (what the harness applies)."""
    data = yaml.safe_load((GROUP_DIR / "arms" / f"{tag}.yaml").read_text(encoding="utf-8"))
    merged: dict[str, Any] = {}
    for dep in data.get("requires") or []:
        merged.update(arm_overrides(dep))
    merged.update(data.get("overrides") or {})
    return merged


class StubModel:
    """The model under test: records every prompt, spends like an adapter, answers a fixed line.
    For a case that asks about a chat thread it first reads it with `fetch_chat_thread`, as the
    real model would, so the thread's text reaches its prompt."""

    def __init__(self):
        self.calls: list[tuple[str, list]] = []

    def generate_step(self, messages, tools, system_instruction, **kwargs):
        self.calls.append((system_instruction, list(messages)))
        try:
            asked = [m.text for m in messages if m.role == "user"][-1] or ""
            ids = UUID.findall(asked)
            if len(ids) >= 2 and not any(m.role == "tool_response" for m in messages):
                yield (
                    None,
                    FunctionCall(
                        name="fetch_chat_thread", args={"chat_id": ids[0], "thread_id": ids[1]}
                    ),
                )
            else:
                yield ("Stub answer.", None)
        finally:
            spend.record_llm_call(
                CallUsage(provider="stub", model="stub", prompt_tokens=100, output_tokens=10)
            )


class RunnerBase(TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.model = StubModel()
        real_run, real_resume = controller.run_agent, controller.resume_agent

        def closing(real):
            def call(*args, **kwargs):
                try:
                    return real(*args, **kwargs)
                finally:
                    connections.close_all()  # the stream's worker thread: leave nothing behind

            return call

        for patcher in (
            mock.patch.object(controller, "get_model_client", return_value=self.model),
            mock.patch.object(agent_views, "run_agent", side_effect=closing(real_run)),
            mock.patch.object(agent_views, "resume_agent", side_effect=closing(real_resume)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config_before = dict(settings.SEARCH_ENGINE)

    def run_case(self, case, overrides=None):
        return resume_runner.run_resume_case(case, dict(overrides or {}))

    def case(self, case_id):
        (found,) = [c for c in cases() if c["id"] == case_id]
        return found


class ShapeTests(RunnerBase):
    def test_the_result_has_the_contract_shape(self):
        result = self.run_case(self.case("t977.plain-ask.approve.en"))
        for key in ("case_id", "arm", "lang", "passed", "checks", "tools", "answer", "usage"):
            self.assertIn(key, result)
        self.assertEqual(result["case_id"], "t977.plain-ask.approve.en")
        self.assertEqual(result["lang"], "en")
        self.assertEqual(
            sorted(result["usage"]),
            ["cached_tokens", "calls", "cost_usd", "input_tokens", "output_tokens"],
        )
        self.assertNotIn("error", result)
        self.assertEqual(result["answer"], "Stub answer.")

    def test_both_legs_ran_through_the_production_views(self):
        result = self.run_case(self.case("t977.plain-ask.approve.en"))
        names = {c["name"]: c for c in result["checks"]}
        self.assertTrue(names["paused_on_the_write"]["ok"])
        # The real write tool ran on approve, in the scenario's own team: a tool_call_start was
        # emitted by `resume_agent` for it.
        self.assertIn("create_task", result["tools"])

    def test_the_scripted_first_leg_is_free_only_the_resumed_leg_uses_the_model(self):
        self.run_case(self.case("t977.plain-ask.approve.en"))
        self.assertEqual(len(self.model.calls), 1)  # the resumed leg's one call

    def test_usage_is_attributed_to_the_case(self):
        # The views run the loop on a bare thread with its own spend context. Without the
        # binding in the runner the calls land in UNATTRIBUTED and this is 0.
        result = self.run_case(self.case("t977.plain-ask.approve.en"))
        self.assertEqual(result["usage"]["calls"], len(self.model.calls))
        self.assertEqual(result["usage"]["input_tokens"], 100 * len(self.model.calls))
        self.assertEqual(result["usage"]["output_tokens"], 10 * len(self.model.calls))

    def test_the_world_is_torn_down_and_settings_restored(self):
        teams = TeamMaster.objects.count()
        users = get_user_model().objects.count()
        self.run_case(
            self.case("t977.thread-facts.approve.en"), {"AGENT_RESUME_KEEPS_BLOCKS": True}
        )
        self.assertEqual(TeamMaster.objects.count(), teams)
        self.assertEqual(get_user_model().objects.count(), users)
        self.assertEqual(dict(settings.SEARCH_ENGINE), self.config_before)

    def test_a_crash_is_a_failed_result_not_an_exception(self):
        broken = dict(self.case("t977.plain-ask.approve.en"))
        broken["proposal"] = {"tool": "no_such_tool", "args": {}}
        result = self.run_case(broken)
        self.assertFalse(result["passed"])
        self.assertTrue(result.get("error") or any(not c["ok"] for c in result["checks"]))

    def test_cases_do_not_overlap(self):
        # The runner patches process-global seams, so the harness's thread pool must be
        # serialised through one lock.
        self.assertIsInstance(resume_runner._RUN_LOCK, type(threading.Lock()))
        self.assertFalse(resume_runner._RUN_LOCK.locked())

    def test_an_unknown_override_does_not_leak(self):
        self.run_case(self.case("t977.plain-ask.approve.en"), {"AGENT_RESUME_KEEPS_BLOCKS": True})
        self.assertFalse(settings.SEARCH_ENGINE.get("AGENT_RESUME_KEEPS_BLOCKS"))


class MeterOnUsageTests(RunnerBase):
    """MIP-1504: with `AI_COST_METER` on (the dev compose env, so the paid session) the ask leg
    writes an `AiRequestCost` row linked to the run, and `/decide/` REJOINS that row's request id
    instead of the one the runner bound. Every call of the resumed leg then landed in
    `UNATTRIBUTED`: `calls=0`, `$0` in the rows, and the cost cap could not see them. The meter is
    off in the key-free test container, so the plain usage test above cannot see it."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(settings.SEARCH_ENGINE, {"AI_COST_METER": True})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_before = dict(settings.SEARCH_ENGINE)

    def assert_counted(self, result):
        self.assertTrue(self.model.calls)
        self.assertEqual(result["usage"]["calls"], len(self.model.calls))
        self.assertEqual(result["usage"]["input_tokens"], 100 * len(self.model.calls))
        self.assertEqual(result["usage"]["output_tokens"], 10 * len(self.model.calls))

    def test_the_resumed_leg_is_counted_with_the_meter_on(self):
        self.assert_counted(self.run_case(self.case("t977.plain-ask.approve.en")))

    def test_a_follow_up_ask_is_counted_too(self):
        result = self.run_case(self.case("t889-undo.still-there.en"))
        self.assertGreaterEqual(len(self.model.calls), 2)  # the resumed leg and the follow-up
        self.assert_counted(result)

    def test_the_closing_line_ask_is_counted(self):
        self.assert_counted(self.run_case(self.case("t1283.client-asks.en")))

    def test_the_harness_capture_sees_the_calls_too(self):
        # The harness wraps every case in its own capture; the runner's capture reuses that
        # request id, so both must agree (the harness overwrites the row's usage with its own).
        from origin.search_engine.agent.evals.group import usage as group_usage

        before = group_usage.UNATTRIBUTED.calls
        with group_usage.capture_usage() as outer:
            result = self.run_case(self.case("t977.plain-ask.approve.en"))
        self.assert_counted(result)
        self.assertEqual(outer.calls, len(self.model.calls))
        self.assertEqual(group_usage.UNATTRIBUTED.calls, before)


class LocaleTests(RunnerBase):
    """Production builds `ToolContext(locale=...)` from the request body (else the user's
    language) and the controller appends `language_directive(locale)` as the LAST line of the
    system instruction, on every leg. A `lang: ja` case without it would be measured in the wrong
    language."""

    def systems(self, case_id, overrides=None, **case_extra):
        case = {**self.case(case_id), **case_extra}
        self.run_case(case, overrides)
        return [system for system, _messages in self.model.calls]

    def test_a_ja_resume_case_ends_its_resumed_prompt_with_the_production_directive(self):
        directive = language_directive("ja")
        self.assertTrue(directive)
        systems = self.systems("t977.plain-ask.approve.ja")
        self.assertTrue(systems)
        for system in systems:
            self.assertTrue(system.endswith(directive), system[-300:])

    def test_an_en_resume_case_has_no_directive(self):
        for system in self.systems("t977.plain-ask.approve.en"):
            self.assertNotIn("Write your reply to the user in", system)

    def test_the_harness_locale_wins_over_the_case_language(self):
        directive = language_directive("ja")
        for system in self.systems("t977.plain-ask.approve.en", locale="ja"):
            self.assertTrue(system.endswith(directive))

    def test_the_second_leg_keeps_the_locale_the_first_leg_resolved(self):
        # `/decide/` carries no locale: the run kept it. The recorded call IS the resumed leg
        # (the first leg is scripted), so this is the rehydration working through the runner.
        self.assertEqual(len(self.systems("t889-reason.corrective.ja")), 1)

    def test_the_follow_up_ask_and_the_closing_line_ask_carry_it_too(self):
        directive = language_directive("ja")
        for system in self.systems("t889-undo.still-there.ja"):
            self.assertTrue(system.endswith(directive))
        for system in self.systems("t1283.client-asks.ja"):
            self.assertTrue(system.endswith(directive))


class PromptDirectionTests(RunnerBase):
    """The headline: per flag, the resumed leg's prompt, recorded from the stub."""

    def prompts(self, case_id, overrides):
        result = self.run_case(self.case(case_id), overrides)
        return result, result["resumed_prompt"]

    def test_977_thread_block(self):
        off, prompt_off = self.prompts("t977.thread-facts.approve.en", {})
        on, prompt_on = self.prompts(
            "t977.thread-facts.approve.en", {"AGENT_RESUME_KEEPS_BLOCKS": True}
        )
        self.assertNotIn("Priya", prompt_off)
        self.assertIn("Priya", prompt_on)
        self.assertIn("21 November", prompt_on)
        self.assertFalse(off["kept_context"])
        self.assertTrue(on["kept_context"])

    def test_977_note_block_after_a_reject(self):
        _, off = self.prompts("t977.note-fact.reject.ja", {})
        _, on = self.prompts("t977.note-fact.reject.ja", {"AGENT_RESUME_KEEPS_BLOCKS": True})
        self.assertNotIn("12月5日", off)
        self.assertIn("12月5日", on)

    def test_977_references_block(self):
        _, off = self.prompts("t977.reference-lookup.approve.en", {})
        _, on = self.prompts(
            "t977.reference-lookup.approve.en", {"AGENT_RESUME_KEEPS_BLOCKS": True}
        )
        self.assertNotIn("USER-PROVIDED REFERENCES", off)
        self.assertIn("USER-PROVIDED REFERENCES", on)

    def test_991_needs_the_977_flag(self):
        turns_only = {"AGENT_RESUME_KEEPS_TURNS": True}
        _, off = self.prompts("t991.owner-follow-up.approve.en", turns_only)
        both = {"AGENT_RESUME_KEEPS_BLOCKS": True, "AGENT_RESUME_KEEPS_TURNS": True}
        _, on = self.prompts("t991.owner-follow-up.approve.en", both)
        self.assertNotIn("Hana Sato", off)
        self.assertIn("Hana Sato", on)

    def test_889_reason_reaches_the_model_and_rule_7_grows(self):
        _, off = self.prompts("t889-reason.corrective.en", {})
        _, on = self.prompts("t889-reason.corrective.en", {"AGENT_REJECT_REASON": True})
        self.assertNotIn("production bug", off)
        self.assertIn("production bug", on)
        self.assertNotIn('carries a "reason"', off)
        self.assertIn('carries a "reason"', on)

    def test_889_undo_note_in_the_follow_up(self):
        off = self.run_case(self.case("t889-undo.still-there.en"), {})
        on = self.run_case(self.case("t889-undo.still-there.en"), {"AGENT_TELL_MODEL_UNDO": True})
        undone = {c["name"]: c for c in on["checks"]}["undo_performed"]
        self.assertTrue(undone["ok"], undone["detail"])
        self.assertTrue({c["name"]: c for c in on["checks"]}["follow_up_prompt_contains"]["ok"])
        self.assertFalse({c["name"]: c for c in off["checks"]}["follow_up_prompt_contains"]["ok"])

    def test_1283_the_line_exists_only_in_the_arm_with_the_flag(self):
        case = self.case("t1283.client-asks.en")
        off = self.run_case(case, {})
        on = self.run_case(case, {"AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True})
        self.assertFalse({c["name"]: c for c in off["checks"]}["final_prompt_contains"]["ok"])
        self.assertTrue({c["name"]: c for c in on["checks"]}["final_prompt_contains"]["ok"])
        self.assertIn("fetch_chat_thread", on["tools"])

    def test_1283_the_asker_is_the_client_who_wrote_the_request(self):
        on = self.run_case(
            self.case("t1283.client-asks.en"), {"AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True}
        )
        self.assertNotIn("error", on)
        names = {c["name"]: c for c in on["checks"]}
        self.assertTrue(names["final_prompt_contains"]["ok"], names)

    def test_1283_reopened_twice_says_it_twice(self):
        on = self.run_case(
            self.case("t1283.reopened-twice.en"), {"AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True}
        )
        self.assertTrue({c["name"]: c for c in on["checks"]}["final_prompt_contains"]["ok"])


PROMPT_CHECKS = (
    "resumed_prompt_contains",
    "resumed_prompt_excludes",
    "follow_up_prompt_contains",
    "follow_up_prompt_excludes",
    "final_prompt_contains",
    "final_prompt_excludes",
)


class EveryCaseFileTests(RunnerBase):
    """Each case in `group/t*.yaml`, under arm0 and its own arm, against the stub."""

    def checks(self, result):
        return {
            c["name"]: c
            for c in result["checks"]
            if c["name"] in PROMPT_CHECKS + ("paused_on_the_write",)
        }

    def test_every_case_is_well_formed_and_measures_something(self):
        problems: list[str] = []
        seen = 0
        for case in cases():
            tag = next(t for t in case["tags"] if t in MY_TAGS)
            seen += 1
            arm0 = self.run_case(case, {})
            arm = self.run_case(case, arm_overrides(tag))
            for label, result in (("arm0", arm0), (tag, arm)):
                if result.get("error"):
                    problems.append(f"{case['id']} [{label}]: crashed: {result['error']}")
            if problems and problems[-1].startswith(case["id"]):
                continue
            c0, c1 = self.checks(arm0), self.checks(arm)
            if case.get("scenario") != "closing_line" and not (
                c0.get("paused_on_the_write", {}).get("ok") and c1["paused_on_the_write"]["ok"]
            ):
                problems.append(f"{case['id']}: the first leg did not pause")
                continue
            prompt_names = [n for n in c1 if n in PROMPT_CHECKS]
            if not prompt_names:
                problems.append(
                    f"{case['id']}: no prompt check, so the stub cannot show a direction"
                )
                continue
            if case["polarity"] == "pos":
                if all(c0[n]["ok"] for n in prompt_names):
                    problems.append(
                        f"{case['id']}: passes its prompt checks in arm0 (measures nothing)"
                    )
                if not all(c1[n]["ok"] for n in prompt_names):
                    problems.append(
                        f"{case['id']}: fails its prompt checks in its own arm: "
                        f"{[(n, c1[n]['detail']) for n in prompt_names if not c1[n]['ok']]}"
                    )
            elif case["polarity"] == "neg":
                for n in prompt_names:
                    if not (c0[n]["ok"] and c1[n]["ok"]):
                        problems.append(f"{case['id']}: negative case differs or fails ({n})")
            else:
                problems.append(f"{case['id']}: polarity must be pos or neg")
        self.maxDiff = None
        self.assertEqual(problems, [])
        self.assertGreaterEqual(seen, 30)

    def test_every_tag_has_en_and_ja_with_a_positive_and_a_negative(self):
        for tag in MY_TAGS:
            for lang in ("en", "ja"):
                mine = [c for c in cases(tag) if c["lang"] == lang]
                self.assertGreaterEqual(len(mine), 3, (tag, lang))
                polarities = {c["polarity"] for c in mine}
                self.assertEqual(polarities, {"pos", "neg"}, (tag, lang))
                for c in mine:
                    self.assertIn("eval-group", c["tags"])
                    self.assertIn(tag, c["tags"])
                    self.assertIs(c.get("fixture"), False)  # its own world, never the v1 team

    def test_ids_are_unique_and_end_in_the_language(self):
        ids = [c["id"] for c in cases()]
        self.assertEqual(len(ids), len(set(ids)))
        for c in cases():
            self.assertTrue(c["id"].endswith(f".{c['lang']}"), c["id"])

    def test_arms_are_the_flags_the_brief_names(self):
        self.assertEqual(arm_overrides("t977"), {"AGENT_RESUME_KEEPS_BLOCKS": True})
        self.assertEqual(
            arm_overrides("t991"),
            {"AGENT_RESUME_KEEPS_BLOCKS": True, "AGENT_RESUME_KEEPS_TURNS": True},
        )
        self.assertEqual(arm_overrides("t1283"), {"AGENT_CLOSING_LINE_IN_ORIGIN_THREAD": True})
        self.assertEqual(arm_overrides("t889-reason"), {"AGENT_REJECT_REASON": True})
        self.assertEqual(arm_overrides("t889-undo"), {"AGENT_TELL_MODEL_UNDO": True})
        self.assertEqual(arm_overrides("t1530"), {"AGENT_UNDO_NOTE_IN_MEMORY": True})
        self.assertNotIn("AGENT_TELL_MODEL_EDIT_UNDO", settings.SEARCH_ENGINE)
        for tag in MY_TAGS:
            for key in arm_overrides(tag):
                self.assertIn(key, settings.SEARCH_ENGINE)  # the harness refuses unknown keys
