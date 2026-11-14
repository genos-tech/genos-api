"""`search_workspace`: a date window and newest-first order, on MCP only (MIP-1379).

The search backend is a stub throughout (no OpenSearch, no embedder, no model). What is pinned:

  * the window reaches `search()` as `date_from` / `date_to` and, through the real `search()`,
    the range clause of every lane, next to the unchanged ACL clause;
  * `order=newest` only re-orders, and never adds a hit;
  * a bad date is a clear error and nothing is searched;
  * the in-app agent: same declaration, same `search()` call, even if a model invents the
    arguments.
"""

from __future__ import annotations

import hashlib
import json
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine import search as S
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.search_window import SearchWindow

from .test_mcp_endpoint import McpBase

MCP_CTX = ToolContext(team_id="t", user_id="u", surface="mcp")
APP_CTX = ToolContext(team_id="t", user_id="u")


def _entity(entity_id, updated_at, title="T"):
    return {
        "entity_type": "task",
        "entity_id": entity_id,
        "title": title,
        "snippet": "s",
        "chunks": [{"chunk_id": f"c{entity_id}", "text": "body"}],
        "updated_at": updated_at,
    }


# Relevance order: b is the best match but the oldest.
RESULTS = {
    "results": [
        _entity("b", "2026-11-01T10:00:00+00:00"),
        _entity("d", "2026-11-12T08:00:00Z"),
        _entity("a", "2026-11-09T23:59:59.500000+00:00"),
        _entity("c", None),
    ]
}


def _run(args, ctx=MCP_CTX, results=RESULTS):
    with mock.patch.object(search_kb, "search", return_value=results) as s:
        out = search_kb._run({"query": "retries", **args}, ctx)
    return out, s


def _ids(out):
    return [m["entity_id"] for m in out["matches"]]


class WindowArgumentTests(SimpleTestCase):
    def test_no_window_calls_search_exactly_as_before(self):
        _, s = _run({})
        self.assertNotIn("date_from", s.call_args.kwargs)
        self.assertNotIn("date_to", s.call_args.kwargs)

    def test_a_bare_after_date_is_the_start_of_that_day_utc(self):
        _, s = _run({"updated_after": "2026-11-07"})
        self.assertEqual(s.call_args.kwargs["date_from"], "2026-11-07T00:00:00+00:00")
        self.assertNotIn("date_to", s.call_args.kwargs)

    def test_a_bare_before_date_includes_the_whole_day(self):
        _, s = _run({"updated_before": "2026-11-09"})
        self.assertEqual(s.call_args.kwargs["date_to"], "2026-11-09T23:59:59.999999+00:00")

    def test_a_datetime_with_an_offset_is_converted_to_utc(self):
        _, s = _run({"updated_after": "2026-11-10T09:00:00+09:00"})
        self.assertEqual(s.call_args.kwargs["date_from"], "2026-11-10T00:00:00+00:00")

    def test_a_datetime_without_an_offset_is_utc(self):
        _, s = _run(
            {"updated_after": "2026-11-10T09:30:00", "updated_before": "2026-11-11T00:00:00Z"}
        )
        self.assertEqual(s.call_args.kwargs["date_from"], "2026-11-10T09:30:00+00:00")
        self.assertEqual(s.call_args.kwargs["date_to"], "2026-11-11T00:00:00+00:00")

    def test_blank_values_mean_no_window(self):
        out, s = _run({"updated_after": "", "updated_before": "  ", "order": ""})
        self.assertNotIn("date_from", s.call_args.kwargs)
        self.assertEqual(_ids(out), ["b", "d", "a", "c"])

    def test_a_bad_date_is_a_clear_error_and_nothing_is_searched(self):
        for name in ("updated_after", "updated_before"):
            for bad in ("yesterday", "2026-13-40", "11/10/2026", "2026-11"):
                with self.subTest(name=name, bad=bad):
                    with mock.patch.object(search_kb, "search") as s:
                        with self.assertRaises(ToolError) as cm:
                            search_kb._run({"query": "q", name: bad}, MCP_CTX)
                    s.assert_not_called()
                    msg = str(cm.exception)
                    self.assertIn(name, msg)
                    self.assertIn(repr(bad), msg)
                    self.assertIn("2026-11-10", msg)  # an example of what to send

    def test_a_non_string_date_is_refused(self):
        with self.assertRaisesRegex(ToolError, "updated_after"):
            _run({"updated_after": 20261110})

    def test_after_later_than_before_is_refused(self):
        with self.assertRaisesRegex(ToolError, "later than"):
            _run({"updated_after": "2026-11-10", "updated_before": "2026-11-01"})

    def test_after_and_before_on_the_same_day_is_a_valid_window(self):
        out, _ = _run({"updated_after": "2026-11-09", "updated_before": "2026-11-09"})
        self.assertEqual(_ids(out), ["a"])

    def test_an_unknown_order_is_refused(self):
        with self.assertRaisesRegex(ToolError, "relevance, newest"):
            _run({"order": "oldest"})


class WindowFilterAndOrderTests(SimpleTestCase):
    def test_the_window_drops_what_is_outside_it_and_what_cannot_be_dated(self):
        out, _ = _run({"updated_after": "2026-11-05", "updated_before": "2026-11-11"})
        self.assertEqual(_ids(out), ["a"])

    def test_the_post_filter_drops_a_graph_neighbour_outside_the_window(self):
        # `_graph_expand` fetches neighbours with a filter that has no window, so the
        # search itself can hand back a hit from outside it. It is removed here.
        out, _ = _run({"updated_after": "2026-11-10"})
        self.assertEqual(_ids(out), ["d"])

    def test_newest_orders_by_updated_at_with_undated_last(self):
        out, _ = _run({"order": "newest"})
        self.assertEqual(_ids(out), ["d", "a", "b", "c"])

    def test_newest_returns_the_same_hits_as_relevance(self):
        relevance, _ = _run({"order": "relevance"})
        newest, _ = _run({"order": "newest"})
        self.assertEqual(sorted(_ids(newest)), sorted(_ids(relevance)))
        self.assertEqual(newest["__summary__"], relevance["__summary__"])

    def test_newest_does_not_ask_the_search_for_anything_else(self):
        _, plain = _run({})
        _, newest = _run({"order": "newest"})
        self.assertEqual(plain.call_args.kwargs, newest.call_args.kwargs)

    def test_equal_stamps_keep_their_relevance_order(self):
        same = {
            "results": [_entity(i, "2026-11-01T00:00:00+00:00") for i in ("x", "y", "z")],
        }
        out, _ = _run({"order": "newest"}, results=same)
        self.assertEqual(_ids(out), ["x", "y", "z"])

    def test_mixed_stamp_formats_compare_as_times_not_text(self):
        mixed = {
            "results": [
                _entity("early", "2026-11-10T09:00:00+09:00"),  # 00:00 UTC
                _entity("late", "2026-11-10T01:00:00Z"),
            ]
        }
        out, _ = _run({"order": "newest"}, results=mixed)
        self.assertEqual(_ids(out), ["late", "early"])

    def test_the_summary_counts_what_is_returned(self):
        out, _ = _run({"updated_after": "2026-11-10"})
        self.assertIn("1 matches", out["__summary__"])


class InAppAgentIsUnchangedTests(SimpleTestCase):
    def test_the_arguments_are_ignored_off_the_mcp_surface(self):
        invented = {
            "updated_after": "2026-11-10",
            "updated_before": "garbage",
            "order": "newest",
        }
        out, s = _run(invented, ctx=APP_CTX)
        plain, plain_s = _run({}, ctx=APP_CTX)
        self.assertEqual(out, plain)
        self.assertEqual(s.call_args.kwargs, plain_s.call_args.kwargs)
        self.assertEqual(_ids(out), ["b", "d", "a", "c"])

    def test_the_window_is_inert_without_a_surface(self):
        self.assertEqual(SearchWindow.from_args({"updated_after": "nope"}, APP_CTX), SearchWindow())

    def test_the_shared_declaration_does_not_carry_the_options(self):
        props = search_kb.SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"]
        self.assertEqual(
            sorted(props),
            ["entity_types", "limit", "milestone_id", "person_id", "project_id", "query"],
        )

    def test_the_shared_declaration_hash_is_pinned(self):
        # sha256 of the tool's name, description and schema as `origin/main` declares them.
        tool = search_kb.SEARCH_KNOWLEDGE_BASE
        blob = json.dumps(
            [tool.name, tool.description, tool.parameters_schema], sort_keys=True
        ).encode()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), DECLARATION_SHA256)


DECLARATION_SHA256 = "a2cfb0b2c22515072f7b557911d9104203101e6d41898d763c455d45e865260c"


class RealSearchCarriesTheWindowTests(SimpleTestCase):
    """`search()` end to end: every lane gets the range AND the unchanged ACL clause."""

    def setUp(self):
        self.filters = []

        def keyword(client, index, variant, base_filter, *a, **k):
            self.filters.append(base_filter)
            return []

        def vector(client, index, qvec, base_filter, *a, **k):
            self.filters.append(base_filter)
            return []

        patches = [
            mock.patch.object(S, "get_client", return_value=mock.Mock(spec=["search"])),
            mock.patch.object(S, "get_index_alias", return_value="rag"),
            mock.patch.object(S, "get_message_retention_days", return_value=None),
            mock.patch.object(S, "get_agent_history_retention_days", return_value=None),
            mock.patch.object(S, "embed_one", return_value=[0.1]),
            mock.patch.object(S, "_run_keyword", side_effect=keyword),
            mock.patch.object(S, "_run_vector", side_effect=vector),
            mock.patch.object(S, "_viewer_acl_terms", return_value=["u", "team:t"]),
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
                    "RAG_USE_RERANKER": False,
                    "RAG_GRAPH_EXPANSION": False,
                    "RAG_USE_QUERY_REWRITE": False,
                }
            )
        )

    def _tool_filters(self, args, ctx=MCP_CTX):
        self.filters.clear()
        search_kb._run({"query": "retries", **args}, ctx)
        self.assertTrue(self.filters, "no lane ran")
        return list(self.filters)

    def test_both_lanes_get_the_range_on_updated_at(self):
        filters = self._tool_filters(
            {"updated_after": "2026-11-07", "updated_before": "2026-11-09"}
        )
        for filt in filters:
            self.assertIn(
                {
                    "range": {
                        "updated_at": {
                            "gte": "2026-11-07T00:00:00+00:00",
                            "lte": "2026-11-09T23:59:59.999999+00:00",
                        }
                    }
                },
                filt,
            )

    def test_the_acl_clauses_are_exactly_the_ones_a_search_without_a_window_has(self):
        plain = self._tool_filters({})
        windowed = self._tool_filters({"updated_after": "2026-11-07", "order": "newest"})
        for p, w in zip(plain, windowed):
            for clause in p:
                self.assertIn(clause, w)
            self.assertEqual(len(w), len(p) + 1)  # the range, nothing else
            self.assertIn({"terms": {"acl_user_ids": ["u", "team:t"]}}, w)
            self.assertIn({"term": {"team_id": "t"}}, w)

    def test_in_app_filters_carry_no_range_even_with_the_arguments_invented(self):
        filters = self._tool_filters({"updated_after": "2026-11-07"}, ctx=APP_CTX)
        for filt in filters:
            self.assertFalse(any("range" in c and "updated_at" in str(c) for c in filt))


class McpEndpointTests(McpBase):
    def _search(self, args, results=RESULTS):
        self._key()
        with mock.patch.object(search_kb, "search", return_value=results) as s:
            resp = self._call("search_workspace", {"query": "retries", **args})
        return self._result(resp), s

    def test_the_published_schema_has_the_three_arguments(self):
        from origin.views.public.mcp import tools

        props = tools.input_schema(tools.BY_NAME["search_workspace"])["properties"]
        for name in ("updated_after", "updated_before", "order"):
            self.assertIn(name, props)
        self.assertEqual(props["order"]["enum"], ["relevance", "newest"])
        for name in ("updated_after", "updated_before", "order"):
            self.assertGreater(len(props[name]["description"]), 40)

    def test_the_description_names_the_arguments_and_gives_an_example(self):
        from origin.views.public.mcp import tools

        desc = tools.BY_NAME["search_workspace"].description
        for needle in ("updated_after", "updated_before", "newest", "Example:"):
            self.assertIn(needle, desc)

    def test_a_windowed_newest_first_call_works_end_to_end(self):
        result, s = self._search({"updated_after": "2026-11-05", "order": "newest"})
        self.assertFalse(result.get("isError"))
        self.assertEqual(s.call_args.kwargs["date_from"], "2026-11-05T00:00:00+00:00")
        self.assertEqual(_ids(result["structuredContent"]), ["d", "a"])

    def test_a_bad_date_comes_back_as_an_error_the_model_can_act_on(self):
        self._key()
        with mock.patch.object(search_kb, "search") as s:
            resp = self._call("search_workspace", {"query": "q", "updated_after": "last week"})
        result = self._result(resp)
        s.assert_not_called()
        self.assertTrue(result["isError"])
        text = result["content"][0]["text"]
        self.assertIn("updated_after", text)
        self.assertIn("2026-11-10", text)

    def test_arguments_the_tool_does_not_have_are_still_refused(self):
        self._key()
        resp = self._call("search_workspace", {"query": "q", "since": "2026-11-01"})
        result = self._result(resp)
        self.assertTrue(result["isError"])
        self.assertIn("updated_after", result["content"][0]["text"])
