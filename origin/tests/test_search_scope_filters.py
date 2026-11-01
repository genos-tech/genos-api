"""`search_workspace` can be limited to one project or milestone (MIP-797).

It took only query, entity_types, limit and person_id, so a caller after
"Genos-Alpha" got project 3's tasks mixed in and had to discard rows
afterwards, while the off-project hits took result slots. The two new filters
are applied in the OpenSearch query (both lanes, every rewritten variant), so
they also decide which hits fill `limit`. They only narrow: the ACL filter is
unchanged.

OpenSearch and the embedder are faked: what is asserted is the filter each lane
is asked to run.
"""

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine import search as S
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.agent.tools.base import ToolContext, ToolError

CTX = ToolContext(team_id="t", user_id="u")


class BuildFilterTests(SimpleTestCase):
    def _filter(self, **kw):
        with mock.patch.object(S, "_viewer_acl_terms", return_value=["u", "team:t"]):
            return S._build_filter("t", "u", None, None, None, **kw)

    def _has(self, filt, clause):
        return any(c == clause for c in filt)

    def test_no_scope_adds_no_clause(self):
        filt = self._filter()
        self.assertFalse(any("project_id" in str(c) or "task_milestone_id" in str(c) for c in filt))

    def test_a_project_is_a_terms_filter(self):
        filt = self._filter(project_ids=["1"])
        self.assertTrue(self._has(filt, {"terms": {"project_id": ["1"]}}))

    def test_a_milestone_matches_its_tasks_and_its_own_chunk(self):
        filt = self._filter(milestone_ids=["111"])
        (clause,) = [c for c in filt if "bool" in c and "should" in c["bool"]]
        should = clause["bool"]["should"]
        self.assertIn({"terms": {"task_milestone_id": ["111"]}}, should)
        self.assertIn({"terms": {"entity_id": ["milestone:111"]}}, should)
        self.assertEqual(clause["bool"]["minimum_should_match"], 1)

    def test_the_acl_clause_is_still_there_and_scopes_only_narrow(self):
        plain = self._filter()
        scoped = self._filter(project_ids=["1"], milestone_ids=["111"])
        # Everything the unscoped filter requires, the scoped one requires too.
        for clause in plain:
            self.assertIn(clause, scoped)
        self.assertGreater(len(scoped), len(plain))
        self.assertTrue(any("acl_user_ids" in str(c) for c in scoped))


class ToolArgumentTests(SimpleTestCase):
    def _run(self, **args):
        with mock.patch.object(search_kb, "search", return_value={"results": []}) as s:
            search_kb._run({"query": "pagination", **args}, CTX)
        return s.call_args.kwargs

    def test_ids_reach_the_search(self):
        kw = self._run(project_id=1, milestone_id=111)
        self.assertEqual(kw["project_ids"], ["1"])
        self.assertEqual(kw["milestone_ids"], ["111"])

    def test_string_ids_from_a_client_are_accepted(self):
        kw = self._run(project_id="3", milestone_id="7")
        self.assertEqual((kw["project_ids"], kw["milestone_ids"]), (["3"], ["7"]))

    def test_absent_means_no_scope(self):
        kw = self._run()
        self.assertIsNone(kw["project_ids"])
        self.assertIsNone(kw["milestone_ids"])

    def test_a_bad_id_is_refused_rather_than_searching_everything(self):
        for key in ("project_id", "milestone_id"):
            for bad in ("abc", 0, -3, "1x"):
                with self.subTest(key=key, bad=bad):
                    with self.assertRaisesRegex(ToolError, key):
                        self._run(**{key: bad})

    def test_the_schema_publishes_both_arguments(self):
        props = search_kb.SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"]
        self.assertEqual(props["project_id"]["type"], "INTEGER")
        self.assertEqual(props["milestone_id"]["type"], "INTEGER")


class LanesCarryTheScopeTests(SimpleTestCase):
    """`search()` end to end: every lane of every variant is filtered."""

    def setUp(self):
        self.kw_filters, self.vec_filters = [], []

        def keyword(client, index, variant, base_filter, *a, **k):
            self.kw_filters.append((variant, base_filter))
            return []

        def vector(client, index, qvec, base_filter, *a, **k):
            self.vec_filters.append(base_filter)
            return []

        patches = [
            mock.patch.object(S, "get_client", return_value=mock.Mock(spec=["search"])),
            mock.patch.object(S, "get_index_alias", return_value="rag"),
            mock.patch.object(S, "get_message_retention_days", return_value=None),
            mock.patch.object(S, "get_agent_history_retention_days", return_value=None),
            mock.patch.object(S, "embed_one", return_value=[0.1]),
            mock.patch.object(S, "_run_keyword", side_effect=keyword),
            mock.patch.object(S, "_run_vector", side_effect=vector),
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
                    "RAG_USE_RERANKER": False,
                    "RAG_GRAPH_EXPANSION": False,
                }
            )
        )

    def test_both_lanes_and_every_variant_get_the_project_and_milestone(self):
        with mock.patch(
            "origin.search_engine.query_rewriter.rewrite_query",
            side_effect=lambda q, *, num_variants: [q, "q two", "q three"],
        ):
            S.search(
                query="q",
                team_id="t",
                user_id="u",
                for_agent=True,
                rewrite=True,
                project_ids=["1"],
                milestone_ids=["111"],
            )
        self.assertEqual({v for v, _ in self.kw_filters}, {"q", "q two", "q three"})
        for _variant, filt in self.kw_filters:
            self.assertIn({"terms": {"project_id": ["1"]}}, filt)
            self.assertTrue(any("task_milestone_id" in str(c) for c in filt))
        self.assertEqual(len(self.vec_filters), 3)
        for filt in self.vec_filters:
            self.assertIn({"terms": {"project_id": ["1"]}}, filt)
            self.assertTrue(any("task_milestone_id" in str(c) for c in filt))

    def test_without_a_scope_the_filters_are_what_they_were(self):
        S.search(query="q", team_id="t", user_id="u", for_agent=True)
        (_, filt) = self.kw_filters[0]
        self.assertFalse(any("project_id" in str(c) or "task_milestone_id" in str(c) for c in filt))
