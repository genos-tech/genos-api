"""Tests for Spotlight's project filter and the task-status projection.

Two independent additions, both pure (no OpenSearch round-trip), in the
same filter-shape style as test_rag_retention / test_mention_search:

  * `_build_filter(project_ids=...)` — a HARD `terms` filter on
    `project_id`, so "search only these projects" really excludes
    everything outside them (including chunks with no project at all).
  * `task_status` on the entity-level rows — projected into the UI shape
    for the Spotlight status chip and deliberately WITHHELD from the
    agent shape, whose rows are serialized into LLM grounding context.
    That asymmetry is the point of the `for_agent` tests below: a
    presentational field must not change what the agent reads.
"""

from django.test import SimpleTestCase

from origin.search_engine.search import _build_filter, _group_by_entity, _source_fields


def _project_clauses(filt):
    """Every `terms` clause targeting `project_id` in a built filter."""
    return [c for c in filt if "project_id" in c.get("terms", {})]


def _chunk(entity_id, *, entity_type="task", chunk_type="task_title_body", **src):
    """One fused-chunk record shaped like `_rrf_fuse` output."""
    return {
        "chunk_id": f"{entity_id}:{chunk_type}",
        "score": 1.0,
        "keyword_rank": 1,
        "vector_rank": None,
        "matched_terms": [],
        "source": {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "chunk_type": chunk_type,
            **src,
        },
    }


class BuildFilterProjectScopeTests(SimpleTestCase):
    def test_no_project_ids_no_clause(self):
        filt = _build_filter("team-1", "user-1", None, None, None)
        self.assertEqual(_project_clauses(filt), [])

    def test_empty_list_no_clause(self):
        # Falsy — same "omit the key entirely" contract as entity_types,
        # so an empty selection means unscoped rather than match-nothing.
        filt = _build_filter("team-1", "user-1", None, None, None, project_ids=[])
        self.assertEqual(_project_clauses(filt), [])

    def test_project_ids_add_single_terms_clause(self):
        filt = _build_filter("team-1", "user-1", None, None, None, project_ids=["12", "31"])
        clauses = _project_clauses(filt)
        self.assertEqual(len(clauses), 1)
        self.assertEqual(clauses[0], {"terms": {"project_id": ["12", "31"]}})

    def test_int_project_ids_are_cast_to_strings(self):
        # Project ids are ints in the app but keywords in the index — an
        # int here would silently match nothing.
        filt = _build_filter("team-1", "user-1", None, None, None, project_ids=[12, 31])
        self.assertEqual(_project_clauses(filt)[0], {"terms": {"project_id": ["12", "31"]}})

    def test_composes_with_entity_types(self):
        # "Tasks, in project 12" must apply BOTH clauses.
        filt = _build_filter("team-1", "user-1", ["task"], None, None, project_ids=["12"])
        self.assertIn({"terms": {"entity_type": ["task"]}}, filt)
        self.assertIn({"terms": {"project_id": ["12"]}}, filt)

    def test_acl_and_tenant_clauses_still_first(self):
        filt = _build_filter("team-1", "user-1", None, None, None, project_ids=["12"])
        self.assertEqual(filt[0], {"term": {"team_id": "team-1"}})
        self.assertEqual(filt[1], {"term": {"acl_user_ids": "user-1"}})


class TaskStatusProjectionTests(SimpleTestCase):
    def test_task_status_requested_from_opensearch(self):
        # Must be in `_source.includes` or `_group_by_entity` can never
        # see it, whatever the mappings say.
        self.assertIn("task_status", _source_fields())
        self.assertIn("task_status", _source_fields(for_agent=True))

    def test_ui_shape_carries_task_status(self):
        rows = _group_by_entity([_chunk("task:7", task_id="7", task_status="In Progress")])
        self.assertEqual(rows[0]["task_status"], "In Progress")

    def test_ui_shape_task_status_is_none_when_absent(self):
        # Docs indexed before the field was written (every milestone doc
        # until it is reingested) — key present, value None, so the
        # frontend chip can branch on it.
        rows = _group_by_entity([_chunk("milestone:3", entity_type="milestone")])
        self.assertIsNone(rows[0]["task_status"])

    def test_agent_shape_omits_task_status(self):
        # The guard that keeps this change presentational: agent rows are
        # serialized into LLM grounding context, so the key must be
        # absent entirely — not None.
        rows = _group_by_entity(
            [_chunk("task:7", task_id="7", task_status="In Progress")], for_agent=True
        )
        self.assertNotIn("task_status", rows[0])
        self.assertIn("chunks", rows[0])

    def test_status_comes_from_the_top_ranked_chunk(self):
        # Comment chunks inherit their parent task's status (task_chunker),
        # so whichever chunk ranks first carries the same value — assert
        # the grouping doesn't drop it when several chunks share an entity.
        rows = _group_by_entity(
            [
                _chunk("task:7", chunk_type="task_comment", task_id="7", task_status="Closed"),
                _chunk("task:7", chunk_type="task_title_body", task_id="7", task_status="Closed"),
            ]
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["task_status"], "Closed")
