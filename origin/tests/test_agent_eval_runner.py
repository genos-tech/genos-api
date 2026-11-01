"""Eval-runner hardening (quality round 2).

Contract under test:

  * `_infra_failure` flags LLM-provider deaths (Vertex 429 / 5xx in a
    fatal "LLM call failed" error event) and ONLY those — an answer that
    merely mentions "quota", or an ordinary tool error, must not trip it.
    Infra-flagged cases carry no continuous metrics, so quota weather
    can't breach the tool_recall north-star (2026-07-09 nightly).
  * `tools_used_contains_any` / `tool_call_errors_contain_any` — the
    any-of expectation variants for questions with more than one
    legitimate tool route and for denials whose exact phrasing is a
    security choice.
  * `rewrite: false` (MIP-860) pins the query rewriter off for a
    retrieval case, so a case that must not depend on an LLM can say so;
    a case can never force it ON.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from origin.search_engine.agent.evals import runner
from origin.search_engine.agent.evals.runner import (
    RETRIEVAL_CASES_PATH,
    _case_rewrite,
    _check_behavior_expectations,
    _infra_failure,
    load_cases,
)


def _err(msg):
    return {"type": "error", "message": msg}


def _tool(name):
    return {"type": "tool_call_start", "step": 0, "tool_name": name}


def _tool_err(name, error):
    return {"type": "tool_call_error", "step": 0, "tool_name": name, "error": error}


class InfraFailureTests(SimpleTestCase):
    def test_vertex_429_is_infra(self):
        events = [_err("LLM call failed: 429 RESOURCE_EXHAUSTED. {'error': {...}}")]
        self.assertTrue(_infra_failure(events))

    def test_503_unavailable_is_infra(self):
        self.assertTrue(_infra_failure([_err("LLM call failed: 503 UNAVAILABLE")]))

    def test_answer_mentioning_quota_is_not_infra(self):
        events = [{"type": "answer_delta", "text": "your quota resets at 429 pm"}]
        self.assertFalse(_infra_failure(events))

    def test_non_llm_fatal_error_is_not_infra(self):
        # A fatal error that isn't the LLM-call prefix (e.g. a genuine
        # agent bug) must still count as a real failure.
        self.assertFalse(_infra_failure([_err("run exceeded max steps")]))

    def test_ordinary_tool_error_is_not_infra(self):
        self.assertFalse(_infra_failure([_tool_err("fetch_task", "Task 9 not found.")]))


class AnyOfExpectationTests(SimpleTestCase):
    def test_tools_used_contains_any_passes_on_either_route(self):
        expect = {"tools_used_contains_any": ["list_tasks", "get_my_focus_tasks"]}
        self.assertEqual(
            _check_behavior_expectations([_tool("get_my_focus_tasks")], expect), []
        )
        self.assertEqual(_check_behavior_expectations([_tool("list_tasks")], expect), [])

    def test_tools_used_contains_any_fails_when_none_ran(self):
        expect = {"tools_used_contains_any": ["list_tasks", "get_my_focus_tasks"]}
        reasons = _check_behavior_expectations([_tool("search_knowledge_base")], expect)
        self.assertEqual(len(reasons), 1)
        self.assertIn("tools_used_contains_any", reasons[0])

    def test_tool_call_errors_contain_any_accepts_either_phrasing(self):
        expect = {
            "tool_call_errors_contain_any": ["not authorized", "not found or has no members"]
        }
        denial = [_tool_err("fetch_chat_thread", "Chat pm:1 not found or has no members.")]
        self.assertEqual(_check_behavior_expectations(denial, expect), [])

    def test_tool_call_errors_contain_any_fails_without_a_refusal(self):
        expect = {"tool_call_errors_contain_any": ["not authorized", "not found"]}
        reasons = _check_behavior_expectations([_tool("fetch_chat_thread")], expect)
        self.assertEqual(len(reasons), 1)
        self.assertIn("tool_call_errors_contain_any", reasons[0])


class RetrievalRewritePinTests(SimpleTestCase):
    def test_flag_decides_when_the_case_says_nothing(self):
        self.assertTrue(_case_rewrite({}, True))
        self.assertFalse(_case_rewrite({}, False))

    def test_a_case_can_pin_the_rewrite_off(self):
        self.assertFalse(_case_rewrite({"rewrite": False}, True))

    def test_a_case_cannot_force_the_rewrite_on(self):
        self.assertFalse(_case_rewrite({"rewrite": True}, False))
        self.assertTrue(_case_rewrite({"rewrite": True}, True))

    def _rewrite_arg(self, case, flag):
        case = {"id": "c", "query": "q", "team_id": "t", "user_id": "u", **case}
        with (
            override_settings(SEARCH_ENGINE={"RAG_USE_QUERY_REWRITE": flag}),
            mock.patch.object(runner, "search", return_value={"results": []}) as search,
        ):
            runner.run_retrieval_case(case)
        return search.call_args.kwargs["rewrite"]

    def test_run_retrieval_case_passes_the_pinned_value_to_search(self):
        self.assertTrue(self._rewrite_arg({}, True))
        self.assertFalse(self._rewrite_arg({"rewrite": False}, True))

    def test_shipped_cases_only_ever_pin_the_rewrite_off(self):
        cases = {c["id"]: c for c in load_cases(RETRIEVAL_CASES_PATH)}
        for cid, case in cases.items():
            if "rewrite" in case:
                self.assertIs(case["rewrite"], False, cid)
        # The two cases MIP-860 made deterministic.
        for cid in ("mention_project_boost", "mention_project_boost_control"):
            self.assertIs(cases[cid].get("rewrite"), False, cid)
