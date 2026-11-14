"""MIP-1471: grouped-eval loose ends. NO real model.

* the case loader knows what a `kind: resume` case may carry (a typo'd key used to be ignored,
  so the case ran without its follow-up), and a resume case has no fixture;
* an eval team's `AgentRun` / `AgentSession` rows (the team is a bare string there, not a
  foreign key) are gone with its team;
* the test settings pin the embedding provider, so a runner's environment cannot change it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent.evals.group import harness
from origin.search_engine.models import (
    AgentRun,
    AgentSession,
    AgentStep,
    NoteSummary,
    ThreadSummary,
)
from origin.tests.test_eval_group_resume_runner import RunnerBase

ROOT = Path(__file__).resolve().parents[2]


def resume_case(**extra):
    return {
        "id": "r.x.en",
        "kind": "resume",
        "lang": "en",
        "tags": ["eval-group", "t977"],
        "fixture": False,
        "query": "q",
        "proposal": {"tool": "create_task", "args": {"title": "t"}},
        "decision": "approve",
        "expect": {"answer_contains_any": ["x"]},
        **extra,
    }


class LoaderTests(SimpleTestCase):
    def load(self, *entries):
        with tempfile.TemporaryDirectory() as d:
            group = Path(d) / "group"
            group.mkdir()
            (group / "x.yaml").write_text(yaml.safe_dump(list(entries)), encoding="utf-8")
            return harness.load_cases(
                group_dir=group, evals_dir=Path(d) / "none", include_legacy=False
            )

    def test_a_well_formed_resume_case_loads_and_has_no_fixture(self):
        (case,) = self.load(resume_case(polarity="pos", reps=2))
        self.assertEqual(case.fixture, "")
        self.assertEqual(case.reps, 2)

    def test_a_resume_case_without_a_fixture_key_has_none_either(self):
        entry = resume_case()
        del entry["fixture"]
        (case,) = self.load(entry)
        self.assertEqual(case.fixture, "", "the group-file default `v1` is meaningless for it")

    def test_a_typo_in_a_resume_key_is_refused_by_name(self):
        with self.assertRaises(harness.HarnessError) as caught:
            self.load(resume_case(**{"follow-up": "is it still there?"}))
        self.assertIn("follow-up", str(caught.exception))
        self.assertIn("r.x.en", str(caught.exception))

    def test_a_fixture_value_on_a_resume_case_is_accepted_and_changes_nothing_else(self):
        # the runner builds its own team and never reads it; refusing it would break callers
        # (and tests) that still write `fixture: v1` on every case
        (case,) = self.load(resume_case(fixture="v1"))
        self.assertEqual(case.fixture, "v1")
        self.assertEqual(case.kind, "resume")

    def test_thread_keys_are_checked_too(self):
        thread = {"messages": ["a", "b"], "summary": "s", "first_sender": "other"}
        self.load(resume_case(thread=thread))
        with self.assertRaisesMessage(harness.HarnessError, "unknown `thread:` key"):
            self.load(resume_case(thread={**thread, "sumary": "typo"}))
        with self.assertRaisesMessage(harness.HarnessError, "unknown `thread:` key"):
            self.load(resume_case(thread=["not", "a", "mapping"]))

    def test_polarity_is_pos_or_neg(self):
        self.load(resume_case(polarity="neg"))
        with self.assertRaisesMessage(harness.HarnessError, "polarity"):
            self.load(resume_case(polarity="positive"))

    def test_the_closing_line_scenario_keys_are_known(self):
        self.load(
            resume_case(
                scenario="closing_line",
                task_title="Fix it",
                close=True,
                reopen_and_close_again=True,
            )
        )

    def test_behaviour_and_routing_cases_keep_their_open_schema(self):
        # nothing that loads today may stop loading: only resume cases are checked
        behaviour = {
            "id": "b.en",
            "tags": ["eval-group"],
            "query": "q",
            "some_future_key": 1,
            "polarity": "whatever",
        }
        routing = {"id": "r.en", "kind": "routing", "query": "q", "ok": ["x"], "x": 1}
        loaded = self.load(behaviour, routing)
        self.assertEqual({c.id for c in loaded}, {"b.en", "r.en"})
        self.assertEqual({c.id: c.fixture for c in loaded}["b.en"], "v1")

    def test_every_key_in_the_shipped_resume_cases_is_known(self):
        shipped = harness.load_cases(include_legacy=False)
        resume = [c for c in shipped if c.kind == "resume"]
        self.assertTrue(resume)
        for case in resume:
            self.assertLessEqual(set(case.raw), harness.RESUME_KEYS, case.id)
            self.assertEqual(case.fixture, "", case.id)


class TeardownLeavesNoAgentRowsTests(RunnerBase):
    """`AgentRun.team_id` is a plain string: deleting the team never reached its runs."""

    def counts(self):
        return {
            "runs": AgentRun.objects.count(),
            "steps": AgentStep.objects.count(),
            "sessions": AgentSession.objects.count(),
            "thread_summaries": ThreadSummary.objects.count(),
            "note_summaries": NoteSummary.objects.count(),
        }

    def assert_clean(self, case_id, overrides=None):
        before = self.counts()
        result = self.run_case(self.case(case_id), overrides)
        self.assertNotIn("error", result)
        self.assertEqual(self.counts(), before, case_id)

    def test_an_approve_case_leaves_no_run_behind(self):
        self.assert_clean("t977.plain-ask.approve.en")

    def test_a_case_with_earlier_turns_leaves_no_session_behind(self):
        # `earlier_turns` are written straight to AgentSession / AgentRun rows
        case_id = next(c["id"] for c in self.cases_with("earlier_turns"))
        self.assert_clean(case_id)

    def test_an_undo_case_with_a_follow_up_leaves_nothing_behind(self):
        self.assert_clean("t889-undo.still-there.en")

    def test_the_closing_line_scenario_leaves_nothing_behind(self):
        self.assert_clean("t1283.client-asks.en")

    def test_it_deletes_only_this_teams_rows(self):
        from origin.search_engine.agent.evals.group import resume_runner

        other = AgentRun.objects.create(
            team_id="not-this-team", user_id="u", query="q", final_answer_text="a", status="done"
        )
        session = AgentSession.objects.create(team_id="not-this-team", user_id="u")
        resume_runner.delete_agent_rows("some-other-team")
        self.assertTrue(AgentRun.objects.filter(pk=other.pk).exists())
        self.assertTrue(AgentSession.objects.filter(pk=session.pk).exists())
        resume_runner.delete_agent_rows("not-this-team")
        self.assertFalse(AgentRun.objects.filter(pk=other.pk).exists())
        self.assertFalse(AgentSession.objects.filter(pk=session.pk).exists())

    def cases_with(self, key):
        from origin.tests.test_eval_group_resume_runner import cases

        return [c for c in cases() if c.get(key)]


class TestSettingsPinTests(SimpleTestCase):
    def test_the_provider_is_the_one_ci_runs(self):
        self.assertEqual(settings.SEARCH_ENGINE["EMBEDDING_PROVIDER"], "openai")

    def test_an_ambient_vertex_setting_cannot_change_it(self):
        """A container built from the dev compose env carried EMBEDDING_PROVIDER=vertex without
        GEMINI_USE_VERTEX: every ingestion-touching test logged a caught error."""
        code = "import apis.settings_test as s;print(s.SEARCH_ENGINE['EMBEDDING_PROVIDER'])"
        env = {
            **os.environ,
            "DJANGO_SETTINGS_MODULE": "apis.settings_test",
            "EMBEDDING_PROVIDER": "vertex",
            "GEMINI_USE_VERTEX": "false",
        }
        out = subprocess.run(  # noqa: S603 - fixed command, test only
            [sys.executable, "-c", code],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        ).stdout.split()
        self.assertEqual(out[-1:], ["openai"])
