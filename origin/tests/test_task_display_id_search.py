"""MIP-739: a task is findable by its display id ("MIP-739").

* Task and milestone chunks carry `task_display_id`; a renamed project
  code re-emits the project's tasks and milestones in the incremental
  window.
* Spotlight / agent search pin the tasks whose id the query is (or
  starts with): the exact id first, then by number. Eval mode is raw.
* `GET /api/v2/search/taskByDisplayId/` finds one task by id whatever
  its status, in projects the caller can reach — the `#` menus' and task
  search bar's lists hold open work only.
* A compact chip (`labelMode: "id"`) survives the markdown round trip,
  and a bare `#MIP-739` written by the agent becomes one.
* New mapping fields reach the live index before the first write.
"""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine import acl_dirty, mapping_guard
from origin.search_engine import search as S
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown
from origin.search_engine.agent.tools.entity_chips import chip_resolver
from origin.search_engine.chunkers.milestone_chunker import iter_milestone_chunks
from origin.search_engine.chunkers.task_chunker import iter_task_chunks

from .test_base import BaseAPITestCase

FILTER = [{"term": {"team_id": "t"}}]


def _hit(entity_id, display_id, entity_type="task"):
    return {
        "_id": f"{entity_id}:title_content",
        "_source": {
            "chunk_id": f"{entity_id}:title_content",
            "entity_type": entity_type,
            "entity_id": entity_id,
            "chunk_type": f"{entity_type}_title_content",
            "title": display_id,
            "task_display_id": display_id,
        },
    }


def _entity(entity_id, score=1.0):
    return {"entity_type": "task", "entity_id": entity_id, "score": score, "title": entity_id}


def _pin(query, hits, grouped=None, *, for_agent=False):
    client = mock.Mock(spec=["search"])
    client.search.return_value = {"hits": {"hits": hits}}
    with mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows):
        out = S._pin_display_id_matches(
            list(grouped or []),
            query,
            client=client,
            index="rag",
            base_filter=FILTER,
            for_agent=for_agent,
        )
    return out, client


class PinLaneTests(SimpleTestCase):
    def test_only_an_id_shaped_query_runs_the_lookup(self):
        for query in ("roadmap", "MIP 739", "fix MIP-739", "", "MIP-739-2", "1234"):
            with self.subTest(query=query):
                out, client = _pin(query, [_hit("task:1", "MIP-739")], [_entity("task:9")])
                client.search.assert_not_called()
                self.assertEqual([e["entity_id"] for e in out], ["task:9"])

    def test_the_lookup_is_a_case_insensitive_term_or_prefix_inside_the_acl(self):
        _out, client = _pin("#mip-7", [_hit("task:1", "MIP-7")])
        q = client.search.call_args.kwargs["body"]["query"]["bool"]
        self.assertEqual(
            q["should"],
            [
                {"term": {"task_display_id": {"value": "mip-7", "case_insensitive": True}}},
                {"prefix": {"task_display_id": {"value": "mip-7", "case_insensitive": True}}},
            ],
        )
        self.assertIn(FILTER[0], q["filter"])
        self.assertIn(
            {"terms": {"chunk_type": ["task_title_content", "milestone_title_content"]}},
            q["filter"],
        )

    def test_exact_id_first_then_by_number_ahead_of_everything_else(self):
        hits = [
            _hit("task:3", "MIP-73"),
            _hit("task:2", "MIP-700"),
            _hit("task:1", "MIP-7"),
            _hit("task:4", "MIP-71"),
        ]
        out, _ = _pin("MIP-7", hits, [_entity("task:9", 5.0), _entity("task:2", 4.0)])
        self.assertEqual(
            [e["entity_id"] for e in out], ["task:1", "task:4", "task:3", "task:2", "task:9"]
        )
        self.assertTrue(all(e.get("display_id_match") for e in out[:4]))
        self.assertNotIn("display_id_match", out[4])
        self.assertEqual(out[0]["task_display_id"], "MIP-7")

    def test_a_half_typed_code_lists_the_projects_tasks(self):
        out, _ = _pin("MIP-", [_hit("task:2", "MIP-12"), _hit("task:1", "MIP-3")])
        self.assertEqual([e["task_display_id"] for e in out], ["MIP-3", "MIP-12"])

    def test_the_agent_shape_gets_the_id_too(self):
        out, _ = _pin("MIP-7", [_hit("task:1", "MIP-7")], for_agent=True)
        self.assertEqual(out[0]["task_display_id"], "MIP-7")

    def test_a_failed_lookup_leaves_the_results_alone(self):
        client = mock.Mock(spec=["search"])
        client.search.side_effect = RuntimeError("down")
        grouped = [_entity("task:9")]
        out = S._pin_display_id_matches(
            grouped, "MIP-7", client=client, index="rag", base_filter=FILTER, for_agent=False
        )
        self.assertEqual(out, grouped)


class SearchModeTests(BaseAPITestCase):
    """The lane runs in typeahead and ai_search, never in eval."""

    def _search(self, mode):
        client = mock.Mock(spec=["search"])

        def answer(index, body):
            should = body["query"]["bool"].get("should")
            if should and "task_display_id" in str(should):
                return {"hits": {"hits": [_hit("task:1", "MIP-7")]}}
            return {"hits": {"hits": []}}

        client.search.side_effect = answer
        with (
            mock.patch.object(S, "get_client", return_value=client),
            mock.patch.object(S, "resolve_milestone_mirrors", side_effect=lambda rows: rows),
        ):
            return S.search(
                query="MIP-7",
                team_id=str(self.team.team_id),
                user_id=str(self.user.id),
                use_vector=False,
                mode=mode,
            )["results"]

    def test_typeahead_and_ai_search_pin_the_task(self):
        for mode in ("typeahead", "ai_search"):
            with self.subTest(mode=mode):
                self.assertEqual([r["entity_id"] for r in self._search(mode)], ["task:1"])

    def test_eval_mode_stays_raw(self):
        self.assertEqual(self._search("eval"), [])


class ChunkerTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Genos", code="MIP", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Search by id", status="Open"
        )
        self.task.refresh_from_db()

    def _task_chunks(self, **kw):
        return [c for b in iter_task_chunks(**kw) for c in b.chunks]

    def test_task_chunks_carry_the_display_id(self):
        chunks = self._task_chunks()
        self.assertTrue(chunks)
        self.assertEqual(
            {c.task_display_id for c in chunks}, {f"MIP-{self.task.project_task_number}"}
        )

    def test_no_code_no_display_id(self):
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(code=None)
        self.assertEqual({c.task_display_id for c in self._task_chunks()}, {None})

    def test_milestone_chunk_carries_its_backing_tasks_id(self):
        backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="M1", status="Open", is_milestone=True
        )
        backing.refresh_from_db()
        MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M1", task=backing
        )
        chunks = [c for b in iter_milestone_chunks() for c in b.chunks]
        self.assertEqual(chunks[0].task_display_id, f"MIP-{backing.project_task_number}")

    def test_a_renamed_code_re_emits_the_projects_tasks_and_milestones(self):
        MilestoneMaster.objects.create(team=self.team, project=self.project, title="M1")
        future = timezone.now() + timedelta(days=1)
        # Adding the fixture's member marked the project; start clean.
        acl_dirty.release(acl_dirty.claim())
        self.assertEqual(acl_dirty.claim().project_ids, frozenset())
        self.authenticate()
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "code": "GEN"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        dirty = acl_dirty.claim()
        self.assertIn(str(self.project.project_id), {str(p) for p in dirty.project_ids})
        # Nothing else changed, so only the mark brings them back.
        chunks = self._task_chunks(since=future, dirty=dirty)
        self.assertEqual(
            {c.task_display_id for c in chunks}, {f"GEN-{self.task.project_task_number}"}
        )
        self.assertEqual(len(list(iter_milestone_chunks(since=future, dirty=dirty))), 1)


class TaskByDisplayIdViewTests(BaseAPITestCase):
    URL = "/api/v2/search/taskByDisplayId/"

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Genos", code="MIP", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Old work", status="Closed"
        )
        self.task.refresh_from_db()
        self.display_id = f"MIP-{self.task.project_task_number}"
        self.authenticate()

    def _get(self, display_id):
        return self.client.get(self.URL, {"team_id": self.team.team_id, "display_id": display_id})

    def test_finds_a_closed_task_by_its_id_in_any_case(self):
        for typed in (self.display_id, self.display_id.lower(), f"#{self.display_id}"):
            with self.subTest(typed=typed):
                res = self._get(typed)
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.data["taskId"], self.task.task_id)
                self.assertEqual(res.data["displayId"], self.display_id)
                self.assertEqual(res.data["status"]["status"], "Closed")

    def test_a_project_you_cannot_reach_is_not_found(self):
        self.authenticate(self.user2)
        self.assertEqual(self._get(self.display_id).status_code, 404)

    def test_unknown_and_malformed_ids(self):
        self.assertEqual(self._get("MIP-99999").status_code, 404)
        for bad in ("", "MIP", "739", "MIP-x", "M-1-2"):
            with self.subTest(bad=bad):
                self.assertEqual(self._get(bad).status_code, 400)

    def test_deleted_tasks_stay_hidden(self):
        TaskMaster.objects.filter(task_id=self.task.task_id).update(is_deleted=True)
        self.assertEqual(self._get(self.display_id).status_code, 404)


class CompactChipTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Genos", code="MIP", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Chips", status="Open"
        )
        self.task.refresh_from_db()
        self.display_id = f"MIP-{self.task.project_task_number}"
        self.resolver = chip_resolver(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _chip(self, markdown):
        blocks = markdown_to_blocks(markdown, entity_chip_resolver=self.resolver)
        return blocks, blocks[0]["content"][0]

    def test_a_bare_id_becomes_a_compact_chip_and_round_trips(self):
        blocks, chip = self._chip(f"#{self.display_id}")
        self.assertEqual(chip["props"]["labelMode"], "id")
        rendered = blocks_to_markdown(blocks)
        self.assertIn(f"[#{self.display_id}](task:{self.task.task_id})", rendered)
        self.assertEqual(markdown_to_blocks(rendered, entity_chip_resolver=self.resolver), blocks)

    def test_a_link_labelled_with_the_id_is_compact(self):
        _blocks, chip = self._chip(f"[#{self.display_id.lower()}](task:{self.task.task_id})")
        self.assertEqual(chip["props"]["labelMode"], "id")

    def test_a_link_labelled_with_the_title_stays_full(self):
        blocks, chip = self._chip(f"[#{self.display_id} · Chips](task:{self.task.task_id})")
        self.assertNotIn("labelMode", chip["props"])
        self.assertIn(f"[#{self.display_id} · Chips](task:", blocks_to_markdown(blocks))


class MappingGuardTests(SimpleTestCase):
    def setUp(self):
        mapping_guard.reset_for_tests()
        self.addCleanup(mapping_guard.reset_for_tests)

    def _client(self, live_props):
        client = mock.Mock()
        client.indices.get_mapping.return_value = {
            "rag_v3": {"mappings": {"properties": live_props}}
        }
        return client

    def test_missing_fields_are_put_once_per_process(self):
        client = self._client({"title": {"type": "text"}})
        mapping_guard.ensure_additive_mapping(client, "rag")
        mapping_guard.ensure_additive_mapping(client, "rag")
        client.indices.put_mapping.assert_called_once()
        kwargs = client.indices.put_mapping.call_args.kwargs
        self.assertEqual(kwargs["index"], "rag_v3")
        self.assertEqual(kwargs["body"]["properties"]["task_display_id"], {"type": "keyword"})
        self.assertNotIn("title", kwargs["body"]["properties"])

    def test_an_up_to_date_index_is_left_alone(self):
        from origin.search_engine.index_config import build_mappings

        client = self._client(dict(build_mappings()["properties"]))
        mapping_guard.ensure_additive_mapping(client, "rag")
        client.indices.put_mapping.assert_not_called()

    def test_a_failure_is_retried_on_the_next_write(self):
        client = self._client({})
        client.indices.get_mapping.side_effect = [
            RuntimeError("down"),
            client.indices.get_mapping.return_value,
        ]
        mapping_guard.ensure_additive_mapping(client, "rag")
        client.indices.put_mapping.assert_not_called()
        client.indices.get_mapping.side_effect = None
        mapping_guard.ensure_additive_mapping(client, "rag")
        client.indices.put_mapping.assert_called_once()
