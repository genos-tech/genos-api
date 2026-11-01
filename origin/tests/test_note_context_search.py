"""MIP-839: searching by, and showing, what contains a note.

Container names are matched, and returned, only for the people in the
chunk's `context_acl`; the ACL itself never leaves `search()`.
"""

import copy
from unittest import mock

from django.test import SimpleTestCase

from origin.search_engine import search as S
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.index_config import build_mappings

from .test_base import BaseAPITestCase

FILTER = [{"term": {"team_id": "t"}}]
VIEWER = ["u1", "team:t"]


def _mm(query="specs roadmap", fields=("title^4", "search_text"), **extra):
    return {"multi_match": {"query": query, "fields": list(fields), **extra}}


class GateTests(SimpleTestCase):
    def test_no_viewer_terms_leaves_the_clause_alone(self):
        clause = _mm()
        for terms in (None, []):
            self.assertIs(S._with_context(clause, terms, S._TIER_CONTEXT_FIELDS), clause)

    def test_the_wide_branch_is_gated_on_context_acl_and_the_plain_one_is_kept(self):
        clause = _mm()
        before = copy.deepcopy(clause)
        out = S._with_context(clause, VIEWER, S._TIER_CONTEXT_FIELDS)
        plain, wide = out["bool"]["should"]
        self.assertEqual(out["bool"]["minimum_should_match"], 1)
        self.assertEqual(plain, before)
        self.assertEqual(wide["bool"]["filter"], [{"terms": {"context_acl": VIEWER}}])
        self.assertEqual(
            wide["bool"]["must"]["multi_match"]["fields"],
            ["title^4", "search_text", "context_text"],
        )
        # The caller's clause is not edited in place.
        self.assertEqual(clause, before)

    def test_every_multi_match_in_a_nested_clause_gets_the_field(self):
        # The all-words tier with a prefixed last word: bool.must of two.
        clause = {
            "bool": {"must": [_mm("specs", type="cross_fields"), _mm("road", type="phrase_prefix")]}
        }
        wide = S._with_context(clause, VIEWER, S._TIER_CONTEXT_FIELDS)["bool"]["should"][1]
        fields = [m["multi_match"]["fields"] for m in wide["bool"]["must"]["bool"]["must"]]
        self.assertTrue(all(f[-1] == "context_text" for f in fields))

    def test_the_any_word_query_searches_the_context_below_the_title(self):
        client = mock.Mock(spec=["search"])
        client.search.return_value = {"hits": {"hits": []}}
        S._run_keyword(client, "rag", "specs", FILTER, 20, context_terms=VIEWER)
        must = client.search.call_args.kwargs["body"]["query"]["bool"]["must"]
        plain, wide = must["bool"]["should"]
        self.assertNotIn("context_text", str(plain))
        wide_fields = wide["bool"]["must"]["multi_match"]["fields"]
        self.assertIn("context_text^0.8", wide_fields)
        # Below the title (^4) and the snippet (^2), whatever the settings.
        self.assertTrue(all(float(f.split("^")[1]) < 1 for f in wide_fields if "context_text" in f))

    def test_without_viewer_terms_the_any_word_query_is_unchanged(self):
        client = mock.Mock(spec=["search"])
        client.search.return_value = {"hits": {"hits": []}}
        S._run_keyword(client, "rag", "specs", FILTER, 20)
        body = client.search.call_args.kwargs["body"]
        self.assertNotIn("context", str(body["query"]))

    def test_tier_queries_are_gated_too(self):
        client = mock.Mock(spec=["search"])
        client.search.return_value = {"hits": {"hits": []}}
        S._run_keyword(
            client, "rag", "specs roadmap", FILTER, 20, tiered=True, context_terms=VIEWER
        )
        bodies = [c.kwargs["body"] for c in client.search.call_args_list]
        # any-word + all-words + phrase, each with the gated branch.
        self.assertEqual(len(bodies), 3)
        for body in bodies:
            must = body["query"]["bool"]["must"]
            self.assertIn(
                {"terms": {"context_acl": VIEWER}}, must["bool"]["should"][1]["bool"]["filter"]
            )
            # The ACL filter on the chunk itself is untouched.
            self.assertEqual(body["query"]["bool"]["filter"], FILTER)


class FinalizeTests(SimpleTestCase):
    def _row(self, acl, path=("Design", "Specs"), **extra):
        return {
            "entity_type": "note",
            "entity_id": "note:personal:1",
            "note_type": "personal",
            "_context_acl": acl,
            "_context_path": list(path),
            **extra,
        }

    def test_a_viewer_in_the_acl_gets_the_path(self):
        rows = [self._row(["u1"])]
        S._finalize_context(rows, VIEWER, "u1")
        self.assertEqual(rows[0]["context_path"], ["Design", "Specs"])

    def test_the_team_sentinel_counts(self):
        rows = [self._row(["team:t"])]
        S._finalize_context(rows, VIEWER, "u1")
        self.assertEqual(rows[0]["context_path"], ["Design", "Specs"])

    def test_anyone_else_gets_no_path_and_the_acl_is_always_dropped(self):
        rows = [self._row(["u2"])]
        S._finalize_context(rows, VIEWER, "u1")
        self.assertNotIn("context_path", rows[0])
        self.assertFalse([k for k in rows[0] if k.startswith("_context")])

    def test_a_row_without_context_is_untouched(self):
        row = {"entity_type": "task", "entity_id": "task:1"}
        S._finalize_context([row], VIEWER, "u1")
        self.assertEqual(row, {"entity_type": "task", "entity_id": "task:1"})

    def test_a_chat_note_shows_the_viewers_name_for_the_chat(self):
        rows = [
            self._row(["u1"], path=("alice & bob",), note_type="chat", chat_type="dm", chat_id="c")
        ]
        with mock.patch(
            "origin.search_engine.friendly_titles.friendly_chat_title", return_value="Alice"
        ) as friendly:
            S._finalize_context(rows, VIEWER, "u1")
        friendly.assert_called_once_with("u1", "dm", "c")
        self.assertEqual(rows[0]["context_path"], ["Alice"])

    def test_a_chat_note_keeps_the_stored_label_when_the_name_cannot_be_resolved(self):
        rows = [self._row(["u1"], path=("Design",), note_type="chat", chat_type="gm", chat_id="c")]
        with mock.patch(
            "origin.search_engine.friendly_titles.friendly_chat_title", return_value=None
        ):
            S._finalize_context(rows, VIEWER, "u1")
        self.assertEqual(rows[0]["context_path"], ["Design"])


def _hit(acl):
    return {
        "_id": "note:personal:1:section:0",
        "_source": {
            "chunk_id": "note:personal:1:section:0",
            "entity_type": "note",
            "entity_id": "note:personal:1",
            "chunk_type": "note_section",
            "title": "Roadmap",
            "note_id": "1",
            "note_type": "personal",
            "context_path": ["Design", "Specs"],
            "context_acl": acl,
        },
    }


class SearchEndToEndTests(BaseAPITestCase):
    def _search(self, *, mode="typeahead", acl=None, user=None):
        user = user or self.user
        client = mock.Mock(spec=["search"])
        client.search.return_value = {"hits": {"hits": [_hit(acl or [str(user.id)])]}}
        with (
            mock.patch.object(S, "get_client", return_value=client),
            mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda r: r),
        ):
            result = S.search(
                query="specs roadmap",
                team_id=str(self.team.team_id),
                user_id=str(user.id),
                use_vector=False,
                mode=mode,
            )
        return result["results"], client

    def test_the_path_reaches_a_viewer_who_may_see_it(self):
        rows, _ = self._search()
        self.assertEqual(rows[0]["context_path"], ["Design", "Specs"])

    def test_the_acl_never_leaves_search(self):
        for mode in ("typeahead", "ai_search", "eval"):
            with self.subTest(mode=mode):
                rows, _ = self._search(mode=mode)
                self.assertFalse([k for k in rows[0] if "acl" in k.lower()], rows[0].keys())

    def test_someone_not_in_the_acl_gets_the_note_without_the_path(self):
        rows, _ = self._search(acl=["somebody-else"])
        self.assertEqual(rows[0]["entity_id"], "note:personal:1")
        self.assertNotIn("context_path", rows[0])

    def test_eval_mode_does_not_search_container_names(self):
        def queries(client):
            return str([c.kwargs["body"]["query"] for c in client.search.call_args_list])

        _, eval_client = self._search(mode="eval")
        self.assertNotIn("context", queries(eval_client))
        _, typeahead_client = self._search(mode="typeahead")
        self.assertIn("context_acl", queries(typeahead_client))

    def test_the_viewer_terms_include_the_team_sentinel_for_a_member(self):
        _, client = self._search()
        terms = [
            t
            for call in client.search.call_args_list
            for t in [call.kwargs["body"]["query"]["bool"]]
            if "context_acl" in str(t)
        ]
        self.assertTrue(terms)
        self.assertIn(f"team:{self.team.team_id}", str(terms[0]))


class AgentToolTests(SimpleTestCase):
    def _run(self, entity):
        ctx = ToolContext(team_id="t", user_id="u")
        with mock.patch(
            "origin.search_engine.agent.tools.search_kb.search", return_value={"results": [entity]}
        ):
            return SEARCH_KNOWLEDGE_BASE.run({"query": "x"}, ctx)["matches"][0]

    def test_the_path_is_fenced_as_workspace_content(self):
        match = self._run(
            {
                "entity_type": "note",
                "entity_id": "note:personal:1",
                "context_path": ["Design", "Specs"],
            }
        )
        self.assertIn("Design › Specs", match["context_path"])
        self.assertIn("workspace_content", match["context_path"])

    def test_no_path_no_key(self):
        match = self._run({"entity_type": "note", "entity_id": "note:personal:1"})
        self.assertNotIn("context_path", match)


class MappingTests(SimpleTestCase):
    def test_the_context_fields_are_declared(self):
        props = build_mappings()["properties"]
        self.assertEqual(props["context_acl"], {"type": "keyword"})
        self.assertEqual(props["context_path"], {"type": "keyword", "index": False})
        self.assertEqual(props["context_text"]["type"], "text")
        self.assertEqual(set(props["context_text"]["fields"]), {"icu", "ja"})
