"""The agent search path's latency levers (MIP-843).

An MCP `search_workspace` call took 10-17 s. Measured on the local stack the
time was the rewrite LLM call (thinking tokens), four query embeddings run one
after another, and the rerank call (more thinking), with OpenSearch a small
part. So:

  * the rewrite and rerank sub-calls get a thinking budget of 0
    (`RAG_SUBPROCESS_THINKING_BUDGET`);
  * the variants' lanes run side by side, merged in variant order so the
    ranking cannot depend on which finished first;
  * the original query's lanes start while the rewrite is still running;
  * one log line says where the time went.

OpenSearch, the embedder and the LLM are never touched: every lane is faked.
"""

import logging
import threading
import time
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine import query_rewriter, reranker
from origin.search_engine import search as S
from origin.search_engine.llm.choice import subprocess_params
from origin.search_engine.llm.types import GenerationParams

FILTER = [{"term": {"team_id": "t"}}]


def _se(**over):
    return {**settings.SEARCH_ENGINE, **over}


class SubprocessParamsTests(SimpleTestCase):
    def _params(self, value):
        with override_settings(SEARCH_ENGINE=_se(RAG_SUBPROCESS_THINKING_BUDGET=value)):
            return subprocess_params("rewrite")

    def test_thinking_is_off_by_default(self):
        self.assertEqual(self._params("0"), GenerationParams(thinking_budget=0))

    def test_a_number_caps_it(self):
        self.assertEqual(self._params("256"), GenerationParams(thinking_budget=256))

    def test_default_or_empty_leaves_the_models_own_behaviour(self):
        for raw in ("default", "DEFAULT", "", "none", "  "):
            self.assertIsNone(self._params(raw), raw)

    def test_garbage_and_negatives_leave_the_models_own_behaviour(self):
        for raw in ("fast", "-1", "1.5"):
            self.assertIsNone(self._params(raw), raw)

    def test_the_rewrite_and_rerank_calls_send_them(self):
        seen = []

        class FakeClient:
            def generate_step(self, **kwargs):
                seen.append(kwargs.get("params"))
                yield ("[]", None)

        with (
            override_settings(SEARCH_ENGINE=_se(RAG_SUBPROCESS_THINKING_BUDGET="0")),
            mock.patch.object(query_rewriter, "get_model_client", return_value=FakeClient()),
            mock.patch.object(reranker, "get_model_client", return_value=FakeClient()),
        ):
            query_rewriter.rewrite_query("how are comments ranked", num_variants=2)
            reranker._rerank_llm(
                query="how are comments ranked",
                entities=[{"entity_id": "task:1", "title": "a", "snippet": "b", "score": 1.0}],
                input_k=5,
                output_k=5,
            )
        self.assertEqual(seen, [GenerationParams(thinking_budget=0)] * 2)


def _hit(cid, rank):
    return {
        "chunk_id": cid,
        "source": {"chunk_id": cid, "entity_type": "task", "entity_id": cid},
        "score": 1.0 / (60 + rank),
        "keyword_rank": rank,
        "vector_rank": None,
        "matched_terms": [],
    }


class VariantLanesTests(SimpleTestCase):
    """`_multi_variant_fuse` with faked lanes."""

    ARGS = dict(
        client=object(),
        index="rag",
        base_filter=FILTER,
        pool_size=10,
        use_vector=True,
        for_agent=True,
        ef_search=64,
    )

    def _fuse(self, variants, lane, **extra):
        # The faked lanes replace the two OpenSearch calls and the embedding;
        # the rest of the per-variant path (RRF, merge) is the real code.
        def keyword(client, index, variant, *a, **k):
            return lane(variant)["keyword"]

        def vector(client, index, qvec, *a, **k):
            return lane(qvec[0])["vector"]

        with (
            mock.patch.object(S, "_run_keyword", side_effect=keyword),
            mock.patch.object(S, "_run_vector", side_effect=vector),
            mock.patch.object(S, "embed_one", side_effect=lambda v: [v]),
        ):
            return S._multi_variant_fuse(variants=variants, **self.ARGS, **extra)

    @staticmethod
    def _lane_for(variant):
        # Distinct, overlapping hit lists per variant.
        table = {
            "orig": ([_hit("a", 1), _hit("b", 2), _hit("c", 3)], [_hit("b", 1), _hit("d", 2)]),
            "v1": ([_hit("c", 1), _hit("a", 2)], [_hit("e", 1)]),
            "v2": ([_hit("d", 1), _hit("c", 2)], [_hit("a", 1), _hit("e", 2)]),
        }
        kw, vec = table[variant]
        return {
            "keyword": [{"_id": h["chunk_id"], "_source": h["source"]} for h in kw],
            "vector": [{"_id": h["chunk_id"], "_source": h["source"]} for h in vec],
        }

    def test_the_result_does_not_depend_on_which_lane_finishes_first(self):
        delays = {"orig": 0.15, "v1": 0.05, "v2": 0.0}
        finished = []

        def slow_lane(variant):
            time.sleep(delays[variant])
            finished.append(variant)
            return self._lane_for(variant)

        parallel = self._fuse(["orig", "v1", "v2"], slow_lane)
        # The original really did finish last: the merge had to wait for it.
        self.assertEqual(finished[-1], "orig")

        reference = self._fuse(["orig", "v1", "v2"], self._lane_for)
        self.assertEqual(
            [(c["chunk_id"], c["score"]) for c in parallel],
            [(c["chunk_id"], c["score"]) for c in reference],
        )
        self.assertEqual(parallel, reference)

    def test_lanes_run_concurrently(self):
        started = threading.Barrier(3, timeout=5)

        def lane(variant):
            started.wait()  # only passes if all three are running at once
            return self._lane_for(variant)

        self.assertTrue(self._fuse(["orig", "v1", "v2"], lane))

    def test_a_started_first_lane_is_used_and_not_run_again(self):
        from concurrent.futures import ThreadPoolExecutor

        calls = []

        def lane(variant):
            calls.append(variant)
            return self._lane_for(variant)

        runner_calls = []
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(lambda: runner_calls.append("orig") or self._first_fused())
            fused = self._fuse(["orig", "v1", "v2"], lane, first_result=first)
        self.assertEqual(runner_calls, ["orig"])
        # The rewrites were run (each lane asks the fake twice: keyword, vector);
        # the original was not run a second time.
        self.assertEqual(set(calls), {"v1", "v2"})
        self.assertEqual(fused, self._fuse(["orig", "v1", "v2"], self._lane_for))

    def _first_fused(self):
        lane = self._lane_for("orig")
        with (
            mock.patch.object(S, "_run_keyword", return_value=lane["keyword"]),
            mock.patch.object(S, "_run_vector", return_value=lane["vector"]),
            mock.patch.object(S, "embed_one", return_value=[0.0]),
        ):
            return S._variant_runner(**self.ARGS, tiered=False, context_terms=None)("orig")

    def test_a_failing_lane_fails_the_search_as_before(self):
        def lane(variant):
            if variant == "v1":
                raise RuntimeError("opensearch down")
            return self._lane_for(variant)

        with self.assertRaises(RuntimeError):
            self._fuse(["orig", "v1", "v2"], lane)

    def test_a_single_variant_still_works(self):
        self.assertTrue(self._fuse(["orig"], self._lane_for))


class SearchOverlapsTheRewriteTests(SimpleTestCase):
    """`search()`: the original query's lanes start before the rewrite ends."""

    def setUp(self):
        self.events = []
        self.orig_started = threading.Event()

        def keyword(client, index, variant, *a, **k):
            self.events.append(f"keyword:{variant}")
            if variant == "q":
                self.orig_started.set()
            return []

        def rewrite(query, *, num_variants):
            # Returns only once the original's lane is running: if the lanes
            # waited for the rewrite, this would time out.
            self.assertTrue(self.orig_started.wait(timeout=5), "original lanes did not start")
            self.events.append("rewrite done")
            return [query, "q two", "q three"]

        client = mock.Mock(spec=["search"])
        patches = [
            mock.patch.object(S, "get_client", return_value=client),
            mock.patch.object(S, "get_index_alias", return_value="rag"),
            mock.patch.object(S, "get_message_retention_days", return_value=None),
            mock.patch.object(S, "get_agent_history_retention_days", return_value=None),
            mock.patch.object(S, "embed_one", return_value=[0.1]),
            mock.patch.object(S, "_run_keyword", side_effect=keyword),
            mock.patch.object(S, "_run_vector", return_value=[]),
            mock.patch.object(S, "_viewer_acl_terms", return_value=[]),
            mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows),
            mock.patch.object(query_rewriter, "rewrite_query", side_effect=rewrite),
            mock.patch("origin.search_engine.friendly_titles.apply_friendly_titles"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.enterContext(
            override_settings(SEARCH_ENGINE=_se(RAG_USE_RERANKER=False, RAG_GRAPH_EXPANSION=False))
        )

    def _search(self):
        return S.search(query="q", team_id="t", user_id="u", for_agent=True, rewrite=True)

    def test_all_three_variants_are_searched_and_the_original_did_not_wait(self):
        self._search()
        self.assertEqual(
            sorted(e for e in self.events if e.startswith("keyword")),
            ["keyword:q", "keyword:q three", "keyword:q two"],
        )
        # The original's lane began before the rewrite finished.
        self.assertLess(self.events.index("keyword:q"), self.events.index("rewrite done"))

    def test_the_original_is_searched_exactly_once(self):
        self._search()
        self.assertEqual(self.events.count("keyword:q"), 1)

    def test_one_timing_line_says_where_the_time_went(self):
        with self.assertLogs(S.log, level=logging.INFO) as logs:
            self._search()
        lines = [m for m in logs.output if "search timing" in m]
        self.assertEqual(len(lines), 1, logs.output)
        for field in ("mode=ai_search", "total_ms=", "rewrite_ms=", "retrieve_ms=", "rerank_ms="):
            self.assertIn(field, lines[0])

    def test_typeahead_does_not_log_timing_at_info(self):
        self.events.clear()
        self.orig_started.set()
        with mock.patch.object(query_rewriter, "rewrite_query") as rw:
            with self.assertNoLogs(S.log, level=logging.INFO):
                S.search(query="q", team_id="t", user_id="u", for_agent=False)
            rw.assert_not_called()
