"""`search_workspace` can ask for milestones and note folders (MIP-1377).

`entity_types` took only chat, task, note and todo, although the index also holds milestones
(`milestone_chunker`) and note folders (`note_folder_chunker`). "Find the milestone about X"
or "which folder is about Y" could not be asked: they only turned up mixed into other results.

The tool is shared (MCP `search_workspace` is the in-app agent's `search_knowledge_base`), so
one schema change covers both. OpenSearch and the embedder are faked: what is asserted is the
filter each lane is asked to run, and the shape of the hits the tool hands back. Who may match a
milestone or a folder is decided by the chunkers' `acl_user_ids` (tested with them); here it is
that the same ACL filter is on every request, whatever type is asked for.
"""

from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from origin.search_engine import search as S
from origin.search_engine.agent.tools import search_kb
from origin.search_engine.agent.tools.base import ToolContext

CTX = ToolContext(team_id="t", user_id="u")
NEW_TYPES = ["milestone", "note_folder"]


def _hit(entity_type, entity_id, **extra):
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": "T",
        "snippet": "s",
        "chunks": [],
        **extra,
    }


class SchemaTests(SimpleTestCase):
    def _enum(self):
        items = search_kb.SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"]["entity_types"]
        return items["items"]["enum"]

    def test_both_new_types_are_accepted_next_to_the_old_ones(self):
        self.assertEqual(
            set(self._enum()), {"chat", "task", "note", "todo", "milestone", "note_folder"}
        )

    def test_the_lanes_that_are_never_offered_stay_out(self):
        # Grounding guard: the memory lanes are not something a caller may ask for here.
        for lane in ("conversation", "spotlight_answer"):
            self.assertNotIn(lane, self._enum())

    def test_the_description_says_what_each_type_is_and_that_folders_are_opt_in(self):
        schema = search_kb.SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"]["entity_types"]
        text = schema["description"]
        for word in ("milestone", "note_folder", "only returned when asked"):
            self.assertIn(word, text)
        self.assertIn("note_folder", search_kb.SEARCH_KNOWLEDGE_BASE.description)


class ToolPassThroughTests(SimpleTestCase):
    def _run(self, **args):
        with mock.patch.object(search_kb, "search", return_value={"results": []}) as s:
            search_kb._run({"query": "pricing", **args}, CTX)
        return s.call_args.kwargs

    def test_each_type_reaches_the_search_as_asked(self):
        for kind in NEW_TYPES:
            with self.subTest(kind=kind):
                self.assertEqual(self._run(entity_types=[kind])["entity_types"], [kind])

    def test_the_memory_lanes_stay_excluded_whatever_is_asked(self):
        for kind in NEW_TYPES:
            self.assertEqual(
                self._run(entity_types=[kind])["exclude_lanes"],
                frozenset({"conversation", "spotlight_answer"}),
            )

    def test_no_entity_types_is_still_no_restriction(self):
        self.assertIsNone(self._run()["entity_types"])


class HitShapeTests(SimpleTestCase):
    def _matches(self, *hits):
        with mock.patch.object(search_kb, "search", return_value={"results": list(hits)}):
            return search_kb._run({"query": "x"}, CTX)["matches"]

    def test_a_milestone_hit_names_its_id_as_a_number(self):
        (m,) = self._matches(_hit("milestone", "milestone:12", project_id="3"))
        self.assertEqual(m["milestone_id"], 12)
        self.assertNotIn("folder_id", m)
        self.assertEqual(m["project_id"], 3)

    def test_a_folder_hit_names_its_id_as_a_number(self):
        (m,) = self._matches(_hit("note_folder", "note_folder:7", folder_id="7"))
        self.assertEqual(m["folder_id"], 7)
        self.assertNotIn("milestone_id", m)

    def test_a_folder_hit_without_a_folder_id_field_falls_back_to_its_entity_id(self):
        (m,) = self._matches(_hit("note_folder", "note_folder:9"))
        self.assertEqual(m["folder_id"], 9)

    def test_a_task_or_note_hit_gets_neither(self):
        for hit in (_hit("task", "task:5", task_id="5"), _hit("note", "note:my:4")):
            (m,) = self._matches(hit)
            self.assertNotIn("milestone_id", m)
            self.assertNotIn("folder_id", m)

    def test_a_malformed_id_is_left_out_rather_than_guessed(self):
        for hit in (
            _hit("milestone", "milestone:abc"),
            _hit("milestone", "task:12"),
            _hit("note_folder", "note_folder:"),
        ):
            (m,) = self._matches(hit)
            self.assertNotIn("milestone_id", m)
            self.assertNotIn("folder_id", m)


class FilterTests(SimpleTestCase):
    """The request each lane runs, for the new types."""

    def setUp(self):
        self.kw, self.vec = [], []

        def keyword(client, index, variant, base_filter, *a, **k):
            self.kw.append(base_filter)
            return []

        def vector(client, index, qvec, base_filter, *a, **k):
            self.vec.append(base_filter)
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
                }
            )
        )

    def _search(self, **kw):
        S.search(query="q", team_id="t", user_id="u", for_agent=True, **kw)

    def test_each_type_is_its_own_entity_type_filter_in_both_lanes(self):
        for kind in NEW_TYPES:
            with self.subTest(kind=kind):
                self.kw.clear(), self.vec.clear()
                self._search(entity_types=[kind])
                self.assertTrue(self.kw and self.vec)
                for filt in (self.kw[0], self.vec[0]):
                    self.assertIn({"terms": {"entity_type": [kind]}}, filt)

    def test_the_acl_and_team_filters_are_the_same_as_for_any_other_type(self):
        # The filter for a milestone/folder search is the filter for a note search with only the
        # entity_type value different: nothing was loosened to make the new types reachable.
        self._search(entity_types=["note"])
        baseline = self.kw[0]
        for kind in NEW_TYPES:
            with self.subTest(kind=kind):
                self.kw.clear()
                self._search(entity_types=[kind])
                swapped = [
                    {"terms": {"entity_type": [kind]}}
                    if c == {"terms": {"entity_type": ["note"]}}
                    else c
                    for c in baseline
                ]
                self.assertEqual(self.kw[0], swapped)
                self.assertIn({"term": {"team_id": "t"}}, self.kw[0])
                self.assertIn({"terms": {"acl_user_ids": ["u", "team:t"]}}, self.kw[0])

    def test_scopes_still_only_narrow_with_the_new_types(self):
        self._search(entity_types=["milestone"], project_ids=["1"])
        filt = self.kw[0]
        self.assertIn({"terms": {"project_id": ["1"]}}, filt)
        self.assertIn({"terms": {"acl_user_ids": ["u", "team:t"]}}, filt)

    def test_an_unrestricted_search_is_what_it_was(self):
        # Milestones were already reachable by default; folders were kept out (MIP-841) and
        # still are, so asking for them is the only way to get one.
        self._search()
        must_not = {
            t["term"]["entity_type"]
            for c in self.kw[0]
            for t in c.get("bool", {}).get("must_not", [])
            if "term" in t
        }
        self.assertIn("note_folder", must_not)
        self.assertNotIn("milestone", must_not)
        self.assertFalse(any("entity_type" in str(c) and "terms" in c for c in self.kw[0]))
