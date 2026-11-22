"""`search_workspace`: an opt-out of the query rewrite, on MCP only.

MCP default (Ken, 2026-11-22, MIP-1378): no query rewrite, no rewrite LLM call; `rewrite: true`
opts back in to the server setting. Nothing here uses a model: the model client is a stub that
counts its calls. The in-app agent keeps its rewrite. Recall was not measured (no paid A/B).

What is pinned:
  * absent, blank and `false` reach `search()` as `rewrite=False` on MCP; `true` and every
    in-app call are the server setting (goldens taken on `origin/main`);
  * `true` can never force a rewrite where the server has it off;
  * through the real `search()`, the rewriter's model client is not called and only the raw
    query is run by default, and is called once (with three variants run) with `rewrite: true`;
  * the reranker is a different model call and is not touched by this option.
"""

from __future__ import annotations

import hashlib
import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase, override_settings

from origin.search_engine import search as S
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.search_window import skip_rewrite
from origin.services import agent_pins

from .test_mcp_endpoint import McpBase

MCP_CTX = ToolContext(team_id="t", user_id="u", surface="mcp")
APP_CTX = ToolContext(team_id="t", user_id="u")

RESULTS = {
    "results": [
        {
            "entity_type": "task",
            "entity_id": "task:1",
            "title": "T1",
            "snippet": "s1",
            "chunks": [{"chunk_id": "c1", "text": "body one"}],
            "updated_at": "2026-11-01T10:00:00+00:00",
            "task_id": "1",
            "project_id": "3",
            "matched_terms": ["a"],
        },
        {
            "entity_type": "note",
            "entity_id": "note:2",
            "title": "N2",
            "snippet": "s2",
            "chunks": [{"chunk_id": "c2", "text": "x" * 1500}],
            "updated_at": None,
            "note_id": "2",
            "note_type": "personal",
        },
    ]
}

# sha256 of {search() kwargs, tool result} for the stub above, with the server's default settings, first
# recorded on pristine `origin/main` with the same snippet: keys `mcp` and `app` of
# origin/tests/golden/agent_pins.json (`python manage.py record_agent_goldens` re-records them).
PINS = agent_pins.Pins(__name__)


def _default(value):
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    return str(value)


def _golden(ctx, args):
    with mock.patch.object(search_kb, "search", return_value=RESULTS) as s:
        out = search_kb._run({"query": "q", "limit": 5, **args}, ctx)
    blob = json.dumps({"kwargs": s.call_args.kwargs, "out": out}, sort_keys=True, default=_default)
    return hashlib.sha256(blob.encode()).hexdigest()


def _rewrite_kwarg(args, ctx=MCP_CTX):
    with mock.patch.object(search_kb, "search", return_value={"results": []}) as s:
        search_kb._run({"query": "q", **args}, ctx)
    return s.call_args.kwargs["rewrite"]


class SkipRewriteArgumentTests(SimpleTestCase):
    def test_mcp_skips_it_by_default_and_on_false(self):
        for value in (False, "false", "FALSE", " False ", None, ""):
            with self.subTest(value=value):
                self.assertTrue(skip_rewrite({"rewrite": value}, MCP_CTX))
        self.assertTrue(skip_rewrite({}, MCP_CTX))

    def test_an_explicit_true_leaves_the_server_setting_alone(self):
        for value in (True, "true", "True"):
            with self.subTest(value=value):
                self.assertFalse(skip_rewrite({"rewrite": value}, MCP_CTX))

    def test_a_value_that_is_neither_is_refused(self):
        for bad in ("no", 0, 1, [], "off"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ToolError, "rewrite"):
                    skip_rewrite({"rewrite": bad}, MCP_CTX)

    def test_in_app_never_skips_it_whatever_it_sends(self):
        for value in (False, "false", "garbage", None):
            self.assertFalse(skip_rewrite({"rewrite": value}, APP_CTX))
        self.assertFalse(skip_rewrite({}, APP_CTX))


class ToolPassesItOnTests(TestCase):
    def test_mcp_default_is_no_rewrite_and_true_opts_back_in(self):
        for args in ({}, {"rewrite": False}, {"rewrite": None}, {"rewrite": ""}):
            with self.subTest(args=args):
                self.assertFalse(_rewrite_kwarg(args))
        for args in ({"rewrite": True}, {"rewrite": "true"}):
            with self.subTest(args=args):
                self.assertTrue(_rewrite_kwarg(args))

    def test_true_cannot_switch_a_rewrite_on_where_the_server_has_it_off(self):
        with override_settings(
            SEARCH_ENGINE={**settings.SEARCH_ENGINE, "RAG_USE_QUERY_REWRITE": False}
        ):
            for args in ({}, {"rewrite": True}, {"rewrite": False}):
                with self.subTest(args=args):
                    self.assertFalse(_rewrite_kwarg(args))

    def test_the_in_app_agent_keeps_the_rewrite(self):
        for args in ({}, {"rewrite": False}):
            with self.subTest(args=args):
                self.assertTrue(_rewrite_kwarg(args, ctx=APP_CTX))

    def test_a_bad_value_is_refused_before_any_search(self):
        with mock.patch.object(search_kb, "search") as s:
            with self.assertRaisesRegex(ToolError, "true or false"):
                search_kb._run({"query": "q", "rewrite": "maybe"}, MCP_CTX)
        s.assert_not_called()

    def test_the_in_app_call_is_byte_identical_to_origin_main(self):
        PINS.check(self, "app", _golden(APP_CTX, {}))
        PINS.check(self, "app", _golden(APP_CTX, {"rewrite": False}))

    def test_rewrite_true_on_mcp_is_the_old_mcp_default(self):
        PINS.check(self, "mcp", _golden(MCP_CTX, {"rewrite": True}))

    def test_the_default_changes_only_the_rewrite_kwarg(self):
        with mock.patch.object(search_kb, "search", return_value=RESULTS) as s:
            out_default = search_kb._run({"query": "q"}, MCP_CTX)
            default_kwargs = dict(s.call_args.kwargs)
            out_on = search_kb._run({"query": "q", "rewrite": True}, MCP_CTX)
            on_kwargs = dict(s.call_args.kwargs)
        self.assertEqual(out_default, out_on)
        self.assertEqual(
            {k: v for k, v in default_kwargs.items() if k != "rewrite"},
            {k: v for k, v in on_kwargs.items() if k != "rewrite"},
        )
        self.assertFalse(default_kwargs["rewrite"])
        self.assertTrue(on_kwargs["rewrite"])


class FakeRewriteClient:
    """Stands in for the model client. Counts calls; never reaches a provider."""

    def __init__(self):
        self.calls = 0

    def generate_step(self, **kwargs):
        self.calls += 1
        yield '["retry backoff", "failed payment retry"]', None


class RealSearchRewriteTests(SimpleTestCase):
    def setUp(self):
        self.client = FakeRewriteClient()
        self.variants_run = []

        def keyword(client, index, variant, base_filter, *a, **k):
            self.variants_run.append(variant)
            return []

        patches = [
            mock.patch(
                "origin.search_engine.query_rewriter.get_model_client", return_value=self.client
            ),
            mock.patch.object(S, "get_client", return_value=mock.Mock(spec=["search"])),
            mock.patch.object(S, "get_index_alias", return_value="rag"),
            mock.patch.object(S, "get_message_retention_days", return_value=None),
            mock.patch.object(S, "get_agent_history_retention_days", return_value=None),
            mock.patch.object(S, "embed_one", return_value=[0.1]),
            mock.patch.object(S, "_run_keyword", side_effect=keyword),
            mock.patch.object(S, "_run_vector", return_value=[]),
            mock.patch.object(S, "_viewer_acl_terms", return_value=["u"]),
            mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows),
            mock.patch("origin.search_engine.friendly_titles.apply_friendly_titles"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.enterContext(
            override_settings(
                SEARCH_ENGINE={
                    **settings.SEARCH_ENGINE,
                    "RAG_USE_QUERY_REWRITE": True,
                    "RAG_REWRITE_MODEL": "stub-model",  # skips the DB-backed model choice
                    "RAG_USE_RERANKER": False,
                    "RAG_GRAPH_EXPANSION": False,
                }
            )
        )

    def _search(self, args):
        self.client.calls = 0
        self.variants_run.clear()
        search_kb._run({"query": "payment retries", **args}, MCP_CTX)

    def test_with_true_the_rewrite_runs_and_its_variants_are_searched(self):
        self._search({"rewrite": True})
        self.assertEqual(self.client.calls, 1)
        self.assertEqual(
            set(self.variants_run),
            {"payment retries", "retry backoff", "failed payment retry"},
        )

    def test_by_default_no_rewrite_call_and_only_the_raw_query_is_searched(self):
        for args in ({}, {"rewrite": False}):
            with self.subTest(args=args):
                self._search(args)
                self.assertEqual(self.client.calls, 0)
                self.assertEqual(set(self.variants_run), {"payment retries"})

    def test_in_app_still_rewrites(self):
        self.client.calls = 0
        search_kb._run({"query": "payment retries"}, APP_CTX)
        self.assertEqual(self.client.calls, 1)


class McpEndpointTests(McpBase):
    def test_the_argument_is_published_and_described(self):
        from origin.views.public.mcp import tools

        props = tools.input_schema(tools.BY_NAME["search_workspace"])["properties"]
        self.assertEqual(props["rewrite"]["type"], "boolean")
        self.assertIn("false", props["rewrite"]["description"])
        self.assertIn("rewrite: true", tools.BY_NAME["search_workspace"].description)

    def test_the_shared_declaration_does_not_have_it(self):
        props = search_kb.SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"]
        self.assertNotIn("rewrite", props)

    def test_a_call_without_the_flag_reaches_the_search_unrewritten(self):
        self._key()
        for args, expected in (({}, False), ({"rewrite": False}, False), ({"rewrite": True}, True)):
            with self.subTest(args=args):
                with mock.patch.object(search_kb, "search", return_value={"results": []}) as s:
                    resp = self._call("search_workspace", {"query": "q", **args})
                self.assertFalse(self._result(resp).get("isError"))
                self.assertIs(s.call_args.kwargs["rewrite"], expected)
