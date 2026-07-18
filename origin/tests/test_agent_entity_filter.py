"""Spotlight filter chips → agent ask scope (`entity_types` on /ask/).

The overlay's service filter, when active at ask time, rides the request as
`entity_types` and must:
  * land on `ToolContext.pinned_entity_types` (server-trusted, whitelisted to
    the workspace types — never the excluded `conversation` /
    `spotlight_answer` lanes, which would defeat the answer→grounding guard);
  * hard-scope every `search_knowledge_base` call: the LLM may narrow WITHIN
    the pin, but anything outside it is discarded (foreign/empty choice falls
    back to the full pin);
  * leave asks without the field completely unscoped (empty tuple, behavior
    identical to before).
"""

from unittest.mock import patch

from django.test import SimpleTestCase

from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.search_kb import SEARCH_KNOWLEDGE_BASE

from .test_base import BaseAPITestCase

ASK_URL = "/api/v2/agent/ask/"


class SearchKbPinnedEntityTypesTests(SimpleTestCase):
    def _search_kwargs(self, args, ctx):
        with patch(
            "origin.search_engine.agent.tools.search_kb.search",
            return_value={"results": []},
        ) as mock_search:
            SEARCH_KNOWLEDGE_BASE.run(args, ctx)
        return mock_search.call_args.kwargs

    def test_no_pin_forwards_llm_choice_unchanged(self):
        ctx = ToolContext(team_id="t", user_id="u")
        kwargs = self._search_kwargs({"query": "x", "entity_types": ["note"]}, ctx)
        self.assertEqual(kwargs["entity_types"], ["note"])
        kwargs = self._search_kwargs({"query": "x"}, ctx)
        self.assertIsNone(kwargs["entity_types"])

    def test_pin_applies_when_llm_omits_entity_types(self):
        ctx = ToolContext(team_id="t", user_id="u", pinned_entity_types=("chat", "task"))
        kwargs = self._search_kwargs({"query": "x"}, ctx)
        self.assertEqual(kwargs["entity_types"], ["chat", "task"])

    def test_llm_may_narrow_within_the_pin(self):
        ctx = ToolContext(team_id="t", user_id="u", pinned_entity_types=("chat", "task"))
        kwargs = self._search_kwargs({"query": "x", "entity_types": ["task"]}, ctx)
        self.assertEqual(kwargs["entity_types"], ["task"])

    def test_llm_choice_outside_the_pin_is_discarded(self):
        ctx = ToolContext(team_id="t", user_id="u", pinned_entity_types=("chat", "task"))
        kwargs = self._search_kwargs({"query": "x", "entity_types": ["note", "todo"]}, ctx)
        self.assertEqual(kwargs["entity_types"], ["chat", "task"])


class AskViewEntityTypesThreadingTests(BaseAPITestCase):
    """/ask/ validates the field and stashes it on ToolContext."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def _ask(self, body_extra):
        captured: dict = {}

        def fake_stream(worker, **kwargs):
            captured["worker"] = worker
            return iter([b""])

        def fake_run_agent(query, ctx, emit, **kwargs):
            captured["ctx"] = ctx
            captured["system_extra"] = kwargs.get("system_extra")
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
                {"query": "q", "team_id": str(self.team.team_id), **body_extra},
                format="json",
            )
            captured["worker"](lambda event: None)
        self.assertEqual(resp.status_code, 200)
        return captured

    def test_entity_types_land_on_tool_context_and_prompt(self):
        cap = self._ask({"entity_types": ["chat", "task", "milestone"]})
        self.assertEqual(cap["ctx"].pinned_entity_types, ("chat", "task", "milestone"))
        self.assertIn("chat, task, milestone", cap["system_extra"] or "")

    def test_excluded_lanes_and_junk_are_dropped(self):
        # conversation/spotlight_answer must never be pinnable (grounding
        # guard); junk entries are dropped, not fatal.
        cap = self._ask({"entity_types": ["conversation", "spotlight_answer", 42, "note", "bogus"]})
        self.assertEqual(cap["ctx"].pinned_entity_types, ("note",))

    def test_all_invalid_leaves_ask_unscoped(self):
        cap = self._ask({"entity_types": ["conversation", "bogus"]})
        self.assertEqual(cap["ctx"].pinned_entity_types, ())

    def test_absent_field_leaves_ask_unscoped(self):
        cap = self._ask({})
        self.assertEqual(cap["ctx"].pinned_entity_types, ())
        self.assertNotIn("restricted", (cap["system_extra"] or ""))
