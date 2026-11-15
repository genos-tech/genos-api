"""The grouped-eval pieces of MIP-1057 (arm `parity`, case file `t1057-task-sessions.yaml`, the resume runner's `task_context`).

NO MODEL RUNS. The model under test is the stub of `test_eval_group_resume_runner` (it records the exact prompt it is sent), so what
this proves without spending anything is what has to be right BEFORE the paid session:

  * the file: every case in both languages, all of them need the arm (with the flag off `task_context` is an unknown field, so there
    is nothing to measure on arm0), and the harness accepts every key (it refuses an unknown one at load time);
  * the runner (extended additively for this item): the world builds the tasks, the due dates, the descriptions and the milestone; the
    first leg carries `task_context`; the follow-up carries it again with the conversation's session, or, for `follow_up_task`,
    carries ANOTHER task and no session; a case that does not name a task context sends none;
  * every case pauses on its write in both states of the flag, and its prompt checks go the intended way: a positive case FAILS them with
    the flag off and PASSES them with it on, a negative case is the same in both, so no case passes without measuring something.
"""

from __future__ import annotations

from unittest import mock

import yaml

from origin.search_engine.agent.evals.group import harness, resume_runner
from origin.search_engine.agent.parity import PARITY_FLAG

from .test_eval_group_resume_runner import GROUP_DIR, PROMPT_CHECKS, RunnerBase

SLUGS = (
    "approve-follow-up",
    "reject-follow-up",
    "second-task",
    "answer-in-context",
    "milestone",
    "injection",
    "plain-ask",
)
# The flags production runs with (the MIP-977 / MIP-991 flags were flipped on by default on 2026-11-14; the key-free test
# settings still default them off), then the same with the parity flag on: the two states a paid run compares.
PRODUCTION = {"AGENT_RESUME_KEEPS_BLOCKS": True, "AGENT_RESUME_KEEPS_TURNS": True}
PARITY_ON = {**PRODUCTION, PARITY_FLAG: True}
WRITES = {
    "delete_task",
    "update_task",
    "update_tasks_bulk",
    "delete_milestone",
    "manage_milestone",
    "delete_project",
    "manage_project",
    "delete_comment",
    "delete_note",
}


def cases() -> list[dict]:
    path = GROUP_DIR / "t1057-task-sessions.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


class CaseFileTests(RunnerBase):
    def test_every_case_is_there_in_both_languages_and_nothing_else(self):
        ids = [c["id"] for c in cases()]
        self.assertEqual(
            sorted(ids), sorted(f"t1057.{slug}.{lang}" for slug in SLUGS for lang in ("en", "ja"))
        )
        self.assertEqual(len(ids), len(set(ids)))

    def test_the_harness_loads_them_and_every_one_needs_the_arm(self):
        loaded = [c for c in harness.load_cases(include_legacy=False) if "t1057" in c.tags]
        self.assertEqual(len(loaded), len(cases()))
        for c in loaded:
            self.assertEqual(c.kind, "resume", c.id)
            self.assertEqual(c.requires_arm, "parity", c.id)
            self.assertIn("eval-group", c.tags)

    def test_each_language_is_asked_in_its_language_and_ends_in_it(self):
        for c in cases():
            self.assertTrue(c["id"].endswith(f".{c['lang']}"), c["id"])
            has_japanese = any("぀" <= ch <= "ヿ" or "㐀" <= ch <= "鿿" for ch in c["query"])
            self.assertEqual(has_japanese, c["lang"] == "ja", c["id"])
            self.assertIs(c["fixture"], False, c["id"])  # its own world, never the v1 team

    def test_every_case_but_the_control_names_a_task_the_ask_is_about(self):
        for c in cases():
            slug = c["id"].split(".")[1]
            if slug == "plain-ask":
                self.assertNotIn("task_context", c, c["id"])
                self.assertEqual(c["polarity"], "neg", c["id"])
                continue
            keys = {t["key"] for t in c["tasks"]}
            self.assertIn(c["task_context"], keys, c["id"])
            if "follow_up_task" in c:
                self.assertIn(c["follow_up_task"], keys - {c["task_context"]}, c["id"])
            self.assertEqual(c["polarity"], "pos", c["id"])

    def test_the_cases_that_need_a_second_task_have_one_and_a_follow_up(self):
        for c in cases():
            if c["id"].startswith("t1057.second-task"):
                self.assertEqual(len(c["tasks"]), 2, c["id"])
                self.assertIn("follow_up", c, c["id"])
                self.assertTrue(c["expect"]["follow_up_prompt_excludes"], c["id"])
                self.assertTrue(c["expect"]["answer_excludes"], c["id"])

    def test_the_follow_up_cases_check_the_follow_ups_own_prompt(self):
        for c in cases():
            if "follow_up" in c:
                self.assertTrue(c["expect"].get("follow_up_prompt_contains"), c["id"])

    def test_the_milestone_cases_are_about_a_milestone(self):
        for c in cases():
            if c["id"].startswith("t1057.milestone"):
                task = next(t for t in c["tasks"] if t["key"] == c["task_context"])
                self.assertTrue(task["milestone"], c["id"])
                self.assertIn(
                    "THE MILESTONE THIS CONVERSATION", c["expect"]["resumed_prompt_contains"][0]
                )

    def test_the_no_tool_case_forbids_the_reading_tools_and_caps_the_calls(self):
        for c in cases():
            if c["id"].startswith("t1057.answer-in-context"):
                expect = c["expect"]
                self.assertTrue(
                    {"fetch_task", "search_knowledge_base"} <= set(expect["tools_not_called"])
                )
                self.assertLessEqual(expect["max_calls"], 2)
                self.assertEqual(c["decision"], "reject")  # nothing runs, so nothing else is called

    def test_the_injection_cases_forbid_every_write_and_a_proposal_and_hold_the_forgery(self):
        for c in cases():
            if c["id"].startswith("t1057.injection"):
                self.assertEqual(set(c["expect"]["tools_not_called"]), WRITES, c["id"])
                self.assertIs(c["expect"]["no_pending"], True, c["id"])
                body = c["tasks"][0]["body"]
                self.assertIn("</workspace_content>", body, c["id"])
                self.assertIn("SYSTEM:", body, c["id"])
                self.assertIn("approval_token", body, c["id"])

    def test_the_arm_file_says_what_it_adds(self):
        text = (harness.ARMS_DIR / "parity.yaml").read_text()
        self.assertIn("MIP-1057", text)
        self.assertEqual(harness.load_arms()["parity"].own, {PARITY_FLAG: True})
        self.assertFalse(
            (harness.ARMS_DIR / "t1057.yaml").exists()
        )  # the shared flag, no arm of its own


class RunnerTests(RunnerBase):
    """The additive keys of the resume runner, observed on the posts the runner makes."""

    def run_recorded(self, case, overrides=None):
        posts: list[tuple[str, dict]] = []
        real_client = resume_runner._client

        def spy(user):
            client = real_client(user)
            real_post = client.post

            def post(path, data=None, **kwargs):
                posts.append((path, dict(data or {})))
                return real_post(path, data, **kwargs)

            client.post = post
            return client

        with mock.patch.object(resume_runner, "_client", side_effect=spy):
            result = self.run_case(case, overrides)
        return result, posts

    def case(self, case_id):
        (found,) = [c for c in cases() if c["id"] == case_id]
        return found

    def test_the_first_leg_and_the_follow_up_carry_the_object_and_the_conversation(self):
        result, posts = self.run_recorded(self.case("t1057.approve-follow-up.en"), PARITY_ON)
        self.assertNotIn("error", result)
        asks = [p for path, p in posts if path == resume_runner.ASK_URL]
        self.assertEqual(len(asks), 2)
        first, follow_up = asks
        self.assertIn("task_context", first)
        self.assertNotIn("session_id", first)
        self.assertEqual(follow_up["task_context"], first["task_context"])
        self.assertIn("session_id", follow_up)

    def test_a_follow_up_about_another_task_carries_that_task_and_no_session(self):
        result, posts = self.run_recorded(self.case("t1057.second-task.en"), PARITY_ON)
        self.assertNotIn("error", result)
        first, follow_up = [p for path, p in posts if path == resume_runner.ASK_URL]
        self.assertNotEqual(follow_up["task_context"]["task_id"], first["task_context"]["task_id"])
        self.assertNotIn("session_id", follow_up)

    def test_a_case_without_an_object_sends_none(self):
        result, posts = self.run_recorded(self.case("t1057.plain-ask.en"), PARITY_ON)
        self.assertNotIn("error", result)
        for path, payload in posts:
            self.assertNotIn("task_context", payload)

    def test_the_world_builds_the_rows_the_case_names(self):
        case = self.case("t1057.milestone.en")
        world = resume_runner.World(case)
        try:
            from origin.models.task.milestone_models import MilestoneMaster

            launch = world.tasks["launch"]
            self.assertTrue(launch.is_milestone)
            launch.refresh_from_db()
            self.assertEqual(launch.due_date.isoformat(), "2026-12-12")
            self.assertIn("ten customers", str(launch.content))
            self.assertEqual(MilestoneMaster.objects.get(task=launch).title, "Q4 launch")
            self.assertEqual(world.task_context("launch"), {"task_id": launch.task_id})
        finally:
            world.teardown()

    def test_a_row_without_the_new_keys_is_what_it_always_was(self):
        world = resume_runner.World(
            {"tasks": [{"key": "plain", "title": "Plain", "priority": "Critical"}]}
        )
        try:
            task = world.tasks["plain"]
            self.assertEqual((task.status, task.priority), ("Open", "Critical"))
            self.assertFalse(task.is_milestone)
            self.assertIsNone(task.due_date)
            self.assertIsNone(task.content)
            self.assertEqual(task.assignee_id, world.user.id)
        finally:
            world.teardown()


class EveryCaseTests(RunnerBase):
    def checks(self, result):
        return {
            c["name"]: c
            for c in result["checks"]
            if c["name"] in PROMPT_CHECKS + ("paused_on_the_write",)
        }

    def test_every_case_pauses_and_its_prompt_checks_go_the_intended_way(self):
        problems: list[str] = []
        for case in cases():
            off = self.run_case(case, PRODUCTION)
            on = self.run_case(case, PARITY_ON)
            for label, result in (("flag off", off), ("flag on", on)):
                if result.get("error"):
                    problems.append(f"{case['id']} [{label}]: crashed: {result['error']}")
            if any(r.get("error") for r in (off, on)):
                continue
            c_off, c_on = self.checks(off), self.checks(on)
            if not (c_off["paused_on_the_write"]["ok"] and c_on["paused_on_the_write"]["ok"]):
                problems.append(f"{case['id']}: the first leg did not pause")
                continue
            names = [n for n in c_on if n in PROMPT_CHECKS]
            if not names:
                problems.append(
                    f"{case['id']}: no prompt check, so the stub cannot show a direction"
                )
                continue
            if case["polarity"] == "pos":
                if all(c_off[n]["ok"] for n in names):
                    problems.append(f"{case['id']}: passes its prompt checks with the flag off")
                if not all(c_on[n]["ok"] for n in names):
                    failed = [(n, c_on[n]["detail"]) for n in names if not c_on[n]["ok"]]
                    problems.append(
                        f"{case['id']}: fails its prompt checks with the flag on: {failed}"
                    )
            else:
                for n in names:
                    if not (c_off[n]["ok"] and c_on[n]["ok"]):
                        problems.append(f"{case['id']}: negative case differs or fails ({n})")
        self.maxDiff = None
        self.assertEqual(problems, [])

    def test_the_second_task_never_sees_the_first_ones_words_in_its_follow_up(self):
        (case,) = [c for c in cases() if c["id"] == "t1057.second-task.en"]
        self.run_case(case, PARITY_ON)
        # The stub saw: the resumed leg of the first ask, then the follow-up's first call.
        prompts = [
            system + " ".join(m.text or "" for m in messages)
            for system, messages in self.model.calls
        ]
        follow_up = prompts[-1]
        self.assertIn("Billing report", follow_up)
        for first_only in ("Export fix", "Harbor Labs", "2026-11-21"):
            self.assertNotIn(first_only, follow_up)
