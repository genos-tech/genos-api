"""Mentions v2 — mention-aware retrieval units.

Covers the full plumbing chain with no OpenSearch / LLM / network:

  * `search._apply_mention_boost` — the pure chunk-level multiplier
    (person fields, entity ids, related_entity_ids, no stacking).
  * `search._build_filter` — the `person_id` hard-filter clause shape.
  * `mentions.mention_search_params` — as_json dicts → boost params
    (chunker entity_id grammar).
  * `search_kb` — derives boost params from `ctx.resolved_mentions`
    and forwards the LLM-visible `person_id` arg (search patched).
  * View threading — /ask/ stashes resolved mentions on ToolContext;
    /decide/ rehydrates them from the persisted `AgentRun.mentions`.
"""

import uuid
from unittest.mock import patch

from django.test import SimpleTestCase

from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.mentions import mention_search_params
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE
from origin.search_engine.models import AgentRun
from origin.search_engine.search import _apply_mention_boost, _build_filter

from .test_base import BaseAPITestCase

ASK_URL = "/api/v2/agent/ask/"
DECIDE_URL = "/api/v2/agent/decide/"


def _hit(score: float, **source):
    return {"chunk_id": source.get("chunk_id", "c"), "score": score, "source": source}


class ApplyMentionBoostTests(SimpleTestCase):
    def test_boosts_each_person_field(self):
        for field in ("author_id", "task_assignee_id", "note_owner_id"):
            fused = [_hit(1.0, **{field: "u-bob"}), _hit(1.0, **{field: "u-carol"})]
            _apply_mention_boost(
                fused, person_ids=["u-bob"], entity_ids=[], project_ids=[], weight=2.0
            )
            self.assertEqual([h["score"] for h in fused], [2.0, 1.0], field)

    def test_boosts_entity_id_and_related_entity_ids(self):
        fused = [
            _hit(1.0, entity_id="task:123"),
            _hit(1.0, entity_id="note:personal:9", related_entity_ids=["task:123"]),
            _hit(1.0, entity_id="task:999"),
        ]
        _apply_mention_boost(
            fused, person_ids=[], entity_ids=["task:123"], project_ids=[], weight=1.5
        )
        self.assertEqual([h["score"] for h in fused], [1.5, 1.5, 1.0])

    def test_boosts_project_id_chunks(self):
        # A mentioned project lifts everything carrying its project_id
        # (task, task-note, PM-chat, milestone chunks alike).
        fused = [
            _hit(1.0, entity_id="task:1", project_id="77"),
            _hit(1.0, entity_id="milestone:3", project_id="77"),
            _hit(1.0, entity_id="task:2", project_id="88"),
        ]
        _apply_mention_boost(fused, person_ids=[], entity_ids=[], project_ids=["77"], weight=1.5)
        self.assertEqual([h["score"] for h in fused], [1.5, 1.5, 1.0])

    def test_multiplier_applies_at_most_once(self):
        # Bob-authored chunk ABOUT the mentioned task IN the mentioned
        # project: one boost, not three.
        fused = [_hit(1.0, author_id="u-bob", entity_id="task:123", project_id="77")]
        _apply_mention_boost(
            fused, person_ids=["u-bob"], entity_ids=["task:123"], project_ids=["77"], weight=2.0
        )
        self.assertEqual(fused[0]["score"], 2.0)

    def test_noop_on_empty_params_or_unit_weight(self):
        fused = [_hit(1.0, author_id="u-bob")]
        _apply_mention_boost(fused, person_ids=[], entity_ids=[], project_ids=[], weight=2.0)
        self.assertEqual(fused[0]["score"], 1.0)
        _apply_mention_boost(fused, person_ids=["u-bob"], entity_ids=[], project_ids=[], weight=1.0)
        self.assertEqual(fused[0]["score"], 1.0)

    def test_missing_source_fields_do_not_crash_or_match(self):
        fused = [_hit(1.0), {"score": 1.0, "source": None}]
        _apply_mention_boost(
            fused, person_ids=["u-bob"], entity_ids=["task:1"], project_ids=[], weight=2.0
        )
        self.assertEqual([h["score"] for h in fused], [1.0, 1.0])


class BuildFilterPersonTests(SimpleTestCase):
    def test_person_clause_shape(self):
        filt = _build_filter("team-1", "user-1", None, None, None, person_id="u-bob")
        person_clauses = [f for f in filt if "bool" in f and "should" in f["bool"]]
        self.assertEqual(len(person_clauses), 1)
        clause = person_clauses[0]["bool"]
        self.assertEqual(clause["minimum_should_match"], 1)
        self.assertEqual(
            clause["should"],
            [
                {"term": {"author_id": "u-bob"}},
                {"term": {"task_assignee_id": "u-bob"}},
                {"term": {"note_owner_id": "u-bob"}},
            ],
        )
        # ACL + tenant guards are untouched.
        self.assertIn({"term": {"team_id": "team-1"}}, filt)
        self.assertIn({"term": {"acl_user_ids": "user-1"}}, filt)

    def test_no_person_clause_without_person_id(self):
        filt = _build_filter("team-1", "user-1", None, None, None)
        self.assertFalse(any("should" in f.get("bool", {}) for f in filt))


class MentionSearchParamsTests(SimpleTestCase):
    def test_derives_person_and_entity_ids_with_chunker_grammar(self):
        params = mention_search_params(
            [
                {"kind": "user", "label": "Bob", "user_id": "u-bob"},
                # A task mention carries its PARENT project_id — that
                # must not leak into boost_project_ids.
                {"kind": "task", "label": "T", "task_id": 123, "project_id": "88"},
                {"kind": "note", "label": "N", "note_type_label": "personal", "note_id": 50},
                {"kind": "chat", "label": "C", "chat_type_label": "gm", "chat_id": "abc"},
                {"kind": "project", "label": "P", "project_id": "77"},
            ]
        )
        self.assertEqual(params["boost_person_ids"], ["u-bob"])
        # Chat entity_ids carry no "chat:" prefix (chunker convention).
        self.assertEqual(params["boost_entity_ids"], ["task:123", "note:personal:50", "gm:abc"])
        self.assertEqual(params["boost_project_ids"], ["77"])

    def test_tolerates_partial_dicts(self):
        params = mention_search_params(
            [{"kind": "task"}, {"kind": "user"}, {"kind": "project"}, {}]
        )
        self.assertEqual(
            params,
            {"boost_person_ids": [], "boost_entity_ids": [], "boost_project_ids": []},
        )


class SearchKbMentionTests(SimpleTestCase):
    def _run_tool(self, args, ctx):
        with patch(
            "origin.search_engine.agent.tools.search_kb.search",
            return_value={"results": []},
        ) as mock_search:
            SEARCH_KNOWLEDGE_BASE.run(args, ctx)
        return mock_search.call_args.kwargs

    def test_boost_params_come_from_ctx_mentions(self):
        ctx = ToolContext(
            team_id="t",
            user_id="u",
            resolved_mentions=(
                {"kind": "user", "label": "Bob", "user_id": "u-bob"},
                {"kind": "task", "label": "T", "task_id": 7},
                {"kind": "project", "label": "P", "project_id": "77"},
            ),
        )
        kwargs = self._run_tool({"query": "x"}, ctx)
        self.assertEqual(kwargs["boost_person_ids"], ["u-bob"])
        self.assertEqual(kwargs["boost_entity_ids"], ["task:7"])
        self.assertEqual(kwargs["boost_project_ids"], ["77"])
        self.assertIsNone(kwargs["person_id"])

    def test_person_id_arg_forwarded_and_empty_ctx_is_noop(self):
        kwargs = self._run_tool(
            {"query": "x", "person_id": " u-bob "}, ToolContext(team_id="t", user_id="u")
        )
        self.assertEqual(kwargs["person_id"], "u-bob")
        self.assertEqual(kwargs["boost_person_ids"], [])
        self.assertEqual(kwargs["boost_entity_ids"], [])
        self.assertEqual(kwargs["boost_project_ids"], [])

    def test_schema_declares_person_id(self):
        self.assertIn("person_id", SEARCH_KNOWLEDGE_BASE.parameters_schema["properties"])


class ViewCtxThreadingTests(BaseAPITestCase):
    """/ask/ stashes resolved mentions on ToolContext; /decide/
    rehydrates from the persisted AgentRun row."""

    def setUp(self):
        super().setUp()
        self.task = TaskMaster.objects.create(
            team=self.team, title="Fix login flow", assignee=self.user, reporter=self.user
        )
        self.authenticate()

    def test_ask_threads_resolved_mentions_into_tool_context(self):
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["ctx"] = ctx
            emit({"type": "done"})

        with (
            patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            patch("origin.search_engine.agent_views.run_agent", side_effect=fake_run_agent),
            patch(
                "origin.search_engine.agent_views.check_remaining",
                return_value=(True, 0, None),
            ),
        ):
            resp = self.client.post(
                ASK_URL,
                {
                    "query": "Status of #Fix login flow?",
                    "team_id": str(self.team.team_id),
                    "mentions": [{"type": "task", "task_id": self.task.task_id}],
                },
                format="json",
            )
            captured["worker"](lambda event: None)
        self.assertEqual(resp.status_code, 200)
        ctx = captured["ctx"]
        self.assertEqual(len(ctx.resolved_mentions), 1)
        self.assertEqual(ctx.resolved_mentions[0]["kind"], "task")
        self.assertEqual(ctx.resolved_mentions[0]["task_id"], self.task.task_id)

    def test_decide_rehydrates_mentions_from_run_row(self):
        token = uuid.uuid4()
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            query="q",
            status="awaiting_approval",
            pending_approval_token=token,
            mentions=[{"kind": "user", "label": "Bob", "user_id": "u-bob"}],
        )
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_resume(run_arg, decision, ctx, emit):
            captured["ctx"] = ctx
            emit({"type": "done"})

        with (
            patch("origin.search_engine.agent_views._stream_ndjson", side_effect=fake_stream),
            patch("origin.search_engine.agent_views.resume_agent", side_effect=fake_resume),
        ):
            resp = self.client.post(
                DECIDE_URL,
                {
                    "run_id": str(run.run_id),
                    "approval_token": str(token),
                    "decision": "approve",
                },
                format="json",
            )
            captured["worker"](lambda event: None)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            captured["ctx"].resolved_mentions,
            ({"kind": "user", "label": "Bob", "user_id": "u-bob"},),
        )
