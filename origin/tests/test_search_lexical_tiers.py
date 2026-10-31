"""Spotlight (typeahead) orders results by lexical tier before score.

  * An exact-phrase match (two or more words) is always returned, first,
    even when the relevance threshold would cut it.
  * For a short query (up to 3 words, or 6 CJK characters): all words
    matched > some words matched > vector-only.
  * For a long query: after the pinned phrase hits, plain RRF order.
  * The agent and eval modes keep the single any-word query and plain
    RRF, untouched.

No OpenSearch round-trip: the client is a mock that only has `search`,
so a call to `msearch` fails the test: production's `genos_app` role
can't run `_msearch` (403).
"""

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine import search as S
from origin.search_engine.milestone_mirrors import resolve_milestone_mirrors

FILTER = [{"term": {"team_id": "t"}}]
SENTENCE = "Result after writing through the new parent:"


def _hit(hid, **src):
    return {"_id": hid, "_source": {"chunk_id": hid, **src}}


def _resp(*ids):
    return {"hits": {"hits": [_hit(i) for i in ids]}}


def _kind(body):
    mm = body["query"]["bool"]["must"].get("multi_match", {})
    if mm.get("type") == "phrase":
        return S.TIER_PHRASE
    if mm.get("operator") == "and":
        return S.TIER_ALL_WORDS
    return "terms"


def _client(terms, all_words=(), phrase=()):
    answers = {"terms": terms, S.TIER_ALL_WORDS: all_words, S.TIER_PHRASE: phrase}
    client = mock.Mock(spec=["search"])
    client.search.side_effect = lambda index, body: _resp(*answers[_kind(body)])
    return client


def _bodies(client):
    return [c.kwargs["body"] for c in client.search.call_args_list]


def _lane(query, terms, all_words=(), phrase=(), size=20):
    client = _client(terms, all_words, phrase)
    hits = S._run_keyword(client, "rag", query, FILTER, size, tiered=True)
    return [(h["_id"], h["_lexical_tier"]) for h in hits], client


class TestQueryLength(SimpleTestCase):
    def test_short_by_words(self):
        for q in ["parent", "Q3 OKRs", "design system inventory", "  parent  "]:
            self.assertTrue(S._is_short_query(q), q)
        self.assertFalse(S._is_short_query("design system inventory audit"))

    def test_short_by_cjk_characters(self):
        self.assertTrue(S._is_short_query("設定画面"))
        self.assertTrue(S._is_short_query("アニメーショ"))  # 6
        self.assertFalse(S._is_short_query("アニメーション"))  # 7
        self.assertFalse(
            S._is_short_query("アニメーションライブラリの選定に依存しているタスクは？")
        )

    def test_hangul_counts_by_words(self):
        self.assertTrue(S._is_short_query("디자인 시스템 인벤토리"))

    def test_multi_token(self):
        self.assertFalse(S._is_multi_token("parent"))
        self.assertTrue(S._is_multi_token("new parent"))
        self.assertTrue(S._is_multi_token("設定"))
        self.assertFalse(S._is_multi_token("設"))


class TestKeywordLane(SimpleTestCase):
    def test_outside_spotlight_the_lane_is_one_plain_query(self):
        client = mock.Mock(spec=["search"])
        client.search.return_value = _resp("a", "b")
        hits = S._run_keyword(client, "rag", SENTENCE, FILTER, 20)
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual([h["_id"] for h in hits], ["a", "b"])
        self.assertTrue(all("_lexical_tier" not in h for h in hits))

    def test_a_phrase_hit_outside_the_pool_leads_the_lane(self):
        terms = [f"title:{i}" for i in range(20)]
        lane, _ = _lane(SENTENCE, terms, all_words=["comment"], phrase=["comment"])
        self.assertEqual(lane[0], ("comment", S.TIER_PHRASE))
        self.assertEqual(len(lane), 20)

    def test_tiers_order_the_lane_and_keep_the_any_word_order_inside_a_tier(self):
        lane, _ = _lane(
            "competitor analysis",
            terms=["title", "some1", "body", "all1", "some2"],
            all_words=["body", "all1", "title", "all_deep"],
            phrase=["body", "title"],
        )
        self.assertEqual(
            lane,
            [
                ("title", S.TIER_PHRASE),
                ("body", S.TIER_PHRASE),
                ("all1", S.TIER_ALL_WORDS),
                ("all_deep", S.TIER_ALL_WORDS),
                ("some1", S.TIER_SOME_WORDS),
                ("some2", S.TIER_SOME_WORDS),
            ],
        )

    def test_the_lane_never_exceeds_its_size(self):
        lane, _ = _lane(SENTENCE, ["t0", "t1", "t2"], phrase=["p0", "p1"], size=3)
        self.assertEqual([i for i, _ in lane], ["p0", "p1", "t0"])

    def test_the_tier_queries_keep_the_filter_and_use_the_base_fields(self):
        _, client = _lane(SENTENCE, ["a"])
        self.assertEqual({c.kwargs["index"] for c in client.search.call_args_list}, {"rag"})
        tiers = {_kind(b): b for b in _bodies(client)}
        self.assertEqual(len(tiers), 3)
        for tier in (S.TIER_ALL_WORDS, S.TIER_PHRASE):
            body = tiers[tier]
            self.assertEqual(body["query"]["bool"]["filter"], FILTER)
            mm = body["query"]["bool"]["must"]["multi_match"]
            self.assertEqual(mm["query"], SENTENCE)
            self.assertEqual(mm["fields"], ["title^4", "snippet_text^2", "search_text"])
            self.assertIn("highlight", body)
        self.assertEqual(
            tiers[S.TIER_ALL_WORDS]["query"]["bool"]["must"]["multi_match"]["type"], "cross_fields"
        )
        self.assertIn(
            "search_text.ja^1.0", tiers["terms"]["query"]["bool"]["must"]["multi_match"]["fields"]
        )

    def test_a_one_word_query_runs_one_query_and_counts_as_all_words(self):
        lane, client = _lane("mig_live_p0", ["a", "b"])
        self.assertEqual(client.search.call_count, 1)
        self.assertEqual(lane, [("a", S.TIER_ALL_WORDS), ("b", S.TIER_ALL_WORDS)])

    def test_a_failed_tier_query_leaves_the_any_word_hits(self):
        def search(index, body):
            if _kind(body) == "terms":
                return _resp("a", "b")
            raise RuntimeError("tier query refused")

        client = mock.Mock(spec=["search"])
        client.search.side_effect = search
        lane = [
            (h["_id"], h["_lexical_tier"])
            for h in S._run_keyword(client, "rag", SENTENCE, FILTER, 20, tiered=True)
        ]
        self.assertEqual(lane, [("a", S.TIER_SOME_WORDS), ("b", S.TIER_SOME_WORDS)])

    def test_a_failed_any_word_query_returns_nothing(self):
        client = mock.Mock(spec=["search"])
        client.search.side_effect = S.NotFoundError(404, "index_not_found_exception", {})
        self.assertEqual(S._run_keyword(client, "rag", SENTENCE, FILTER, 20, tiered=True), [])


def _src(entity_id, chunk_type="task_title_content", entity_type="task"):
    return {"entity_type": entity_type, "entity_id": entity_id, "chunk_type": chunk_type}


class TestTierSurvivesFusionAndGrouping(SimpleTestCase):
    def test_rrf_carries_the_tier_and_vector_only_has_none(self):
        kw = [{"_id": "k", "_source": _src("task:1"), "_lexical_tier": S.TIER_PHRASE}]
        vec = [{"_id": "v", "_source": _src("task:2")}]
        fused = {c["chunk_id"]: c for c in S._rrf_fuse(kw, vec)}
        self.assertEqual(fused["k"]["lexical_tier"], S.TIER_PHRASE)
        self.assertNotIn("lexical_tier", fused["v"])

    def test_an_entity_takes_its_best_tier_not_its_best_scoring_chunks(self):
        chunks = [
            {
                "chunk_id": "title",
                "score": 0.03,
                "keyword_rank": 1,
                "vector_rank": 1,
                "matched_terms": [],
                "source": _src("task:1"),
                "lexical_tier": S.TIER_SOME_WORDS,
            },
            {
                "chunk_id": "comment",
                "score": 0.01,
                "keyword_rank": 2,
                "vector_rank": None,
                "matched_terms": [],
                "source": _src("task:1", "task_comment"),
                "lexical_tier": S.TIER_PHRASE,
            },
        ]
        (entity,) = S._group_by_entity(chunks)
        self.assertEqual(entity["lexical_tier"], S.TIER_PHRASE)
        self.assertEqual(entity["score"], 0.03)

    def test_untiered_chunks_give_untiered_entities(self):
        chunks = [
            {
                "chunk_id": "a",
                "score": 1.0,
                "keyword_rank": 1,
                "vector_rank": None,
                "matched_terms": [],
                "source": _src("task:1"),
            }
        ]
        self.assertNotIn("lexical_tier", S._group_by_entity(chunks)[0])

    def test_a_dropped_milestone_mirror_hands_its_tier_to_the_milestone(self):
        milestone = {"entity_type": "milestone", "entity_id": "milestone:9", "score": 0.05}
        mirror = {
            "entity_type": "task",
            "entity_id": "task:3",
            "task_id": "3",
            "score": 0.01,
            "lexical_tier": S.TIER_PHRASE,
        }
        stub = mock.MagicMock()
        stub.objects.filter.return_value.values_list.return_value = [("3", 9, "WIP")]
        with mock.patch("origin.models.task.milestone_models.MilestoneMaster", stub):
            (row,) = resolve_milestone_mirrors([milestone, mirror])
        self.assertEqual(row["entity_id"], "milestone:9")
        self.assertEqual(row["lexical_tier"], S.TIER_PHRASE)


def _row(eid, score, tier=None):
    row = {"entity_type": "task", "entity_id": eid, "score": score}
    if tier is not None:
        row["lexical_tier"] = tier
    return row


def _ids(rows):
    return [r["entity_id"] for r in rows]


class TestFinalOrder(SimpleTestCase):
    def test_a_phrase_entity_the_threshold_cut_is_restored_first(self):
        top = _row("title", 0.033, S.TIER_SOME_WORDS)
        comment = _row("comment", 0.011, S.TIER_PHRASE)
        out = S._apply_lexical_tiers([top], [top, comment], short=False)
        self.assertEqual(_ids(out), ["comment", "title"])

    def test_only_phrase_entities_are_restored(self):
        top = _row("title", 0.033, S.TIER_SOME_WORDS)
        weak = _row("weak", 0.011, S.TIER_ALL_WORDS)
        out = S._apply_lexical_tiers([top], [top, weak], short=True)
        self.assertEqual(_ids(out), ["title"])

    def test_short_query_words_beat_meaning(self):
        rows = [
            _row("vector_only", 0.033),
            _row("some", 0.020, S.TIER_SOME_WORDS),
            _row("all", 0.016, S.TIER_ALL_WORDS),
        ]
        self.assertEqual(
            _ids(S._apply_lexical_tiers(rows, rows, short=True)), ["all", "some", "vector_only"]
        )

    def test_long_query_keeps_score_order_after_the_phrase_hits(self):
        rows = [
            _row("vector_only", 0.033),
            _row("some", 0.020, S.TIER_SOME_WORDS),
            _row("phrase", 0.010, S.TIER_PHRASE),
        ]
        self.assertEqual(
            _ids(S._apply_lexical_tiers(rows, rows, short=False)), ["phrase", "vector_only", "some"]
        )

    def test_inside_a_tier_score_decides(self):
        rows = [_row("b", 0.010, S.TIER_PHRASE), _row("a", 0.020, S.TIER_PHRASE)]
        self.assertEqual(_ids(S._apply_lexical_tiers(rows, rows, short=True)), ["a", "b"])


class TestSpotlightEndToEnd(SimpleTestCase):
    """`search()` with OpenSearch and the embedder mocked: the reported
    case, a long comment holding the typed sentence verbatim."""

    def setUp(self):
        titles = [_hit(f"t{i}", **_src(f"task:{i}")) for i in range(20)]
        comment = _hit("c", **_src("task:99", "task_comment"))
        self.titles, self.comment = titles, comment
        client = mock.Mock(spec=["search"])
        client.search.side_effect = lambda index, body: {
            "hits": {"hits": list(titles) if _kind(body) == "terms" else [comment]}
        }
        patches = [
            mock.patch.object(S, "get_client", return_value=client),
            mock.patch.object(S, "get_index_alias", return_value="rag"),
            mock.patch.object(S, "get_message_retention_days", return_value=None),
            mock.patch.object(S, "get_agent_history_retention_days", return_value=None),
            mock.patch.object(S, "embed_one", return_value=[0.1]),
            mock.patch.object(S, "_run_vector", return_value=[dict(h) for h in titles]),
            mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows),
            mock.patch("origin.search_engine.friendly_titles.apply_friendly_titles"),
            # The agent mode would otherwise call the LLM reranker.
            mock.patch.dict(
                settings.SEARCH_ENGINE, {"RAG_USE_RERANKER": False, "RAG_GRAPH_EXPANSION": False}
            ),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _ids(self, **kw):
        r = S.search(
            query=SENTENCE,
            team_id="t",
            user_id="u",
            limit=20,
            min_score_ratio=0.5,
            min_score=0.01,
            **kw,
        )
        return [x["entity_id"] for x in r["results"]]

    def test_spotlight_returns_the_comment_first(self):
        self.assertEqual(self._ids(use_vector=True)[0], "task:99")
        self.assertEqual(self._ids(use_vector=False)[0], "task:99")

    def test_the_agent_and_eval_modes_are_untouched(self):
        for mode in ("ai_search", "eval"):
            self.assertNotIn("task:99", self._ids(use_vector=True, mode=mode), mode)
