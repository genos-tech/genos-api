"""Tests for the MEMORY ladder (UX tier model §6).

Two lanes (`conversation`, `spotlight_answer`), three levels
(none / own / team), one new seam: `_build_filter(exclude_lanes=...)`,
applied UNCONDITIONALLY so an explicit `entity_types` request cannot
tunnel under a tier gate — while the trap the plan calls out (§6.2)
stays covered in the other direction: `search_past_conversations` opts
into its lane via `entity_types` and must NEVER be handed an
`exclude_lanes`, or a paying user's memory tool returns empty forever
with nothing in the logs. Both directions are pinned here.

Filter-shape tests are pure (no OpenSearch round-trip), same idiom as
test_search_project_filter / test_rag_retention.
"""

from unittest import mock

from django.test import SimpleTestCase, override_settings

from origin.search_engine import quota
from origin.search_engine.agent import tool_tiers
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.search import _build_filter, memory_exclude_lanes

from .test_base import BaseAPITestCase
from .test_quota_monthly import TEST_QUOTAS, _search_engine_with_quotas


def _must_not_entity_types(filt):
    """Every entity_type named in a must_not clause of a built filter."""
    out = []
    for clause in filt:
        for inner in clause.get("bool", {}).get("must_not", []):
            et = inner.get("term", {}).get("entity_type")
            if et:
                out.append(et)
    return out


class BuildFilterExcludeLanesTests(SimpleTestCase):
    def test_default_exclusions_unchanged_without_exclude_lanes(self):
        filt = _build_filter("team-1", "user-1", None, None, None)
        self.assertEqual(
            sorted(_must_not_entity_types(filt)), ["conversation", "spotlight_answer"]
        )

    def test_explicit_entity_types_bypass_default_exclusions(self):
        # The pre-existing contract the opt-in tools rely on.
        filt = _build_filter("team-1", "user-1", ["conversation"], None, None)
        self.assertEqual(_must_not_entity_types(filt), [])

    def test_exclude_lanes_applies_even_with_explicit_entity_types(self):
        # The new property: a tier gate cannot be tunnelled under by
        # asking for the lane by name.
        filt = _build_filter(
            "team-1",
            "user-1",
            ["conversation"],
            None,
            None,
            exclude_lanes=frozenset({"conversation"}),
        )
        self.assertEqual(_must_not_entity_types(filt), ["conversation"])

    def test_empty_exclude_lanes_adds_no_clause(self):
        # The silent-empty guard at the filter level: a lane-opting
        # call with no tier exclusion must carry NO clause against its
        # own lane.
        filt = _build_filter(
            "team-1", "user-1", ["conversation"], None, None, exclude_lanes=frozenset()
        )
        self.assertEqual(_must_not_entity_types(filt), [])

    def test_typeahead_keeps_spotlight_answers_by_default(self):
        filt = _build_filter("team-1", "user-1", None, None, None, mode="typeahead")
        self.assertEqual(_must_not_entity_types(filt), ["conversation"])

    def test_typeahead_loses_answers_when_the_tier_says_so(self):
        # A Core (agent_memory="own") typeahead: the team answer lane
        # disappears even though the default branch would keep it.
        filt = _build_filter(
            "team-1",
            "user-1",
            None,
            None,
            None,
            mode="typeahead",
            exclude_lanes=frozenset({"spotlight_answer"}),
        )
        self.assertEqual(
            sorted(_must_not_entity_types(filt)), ["conversation", "spotlight_answer"]
        )


_MEMORY_QUOTAS = {
    level: {
        **TEST_QUOTAS,
        "free": {**TEST_QUOTAS["free"], "agent_memory": level},
    }
    for level in ("none", "own", "team")
}


class MemoryExcludeLanesMappingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([self.user.id])

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])
        super().tearDown()

    def test_level_to_lane_mapping(self):
        expectations = {
            "none": frozenset({"conversation", "spotlight_answer"}),
            "own": frozenset({"spotlight_answer"}),
            "team": frozenset(),
        }
        for level, expected in expectations.items():
            with override_settings(
                SEARCH_ENGINE=_search_engine_with_quotas(_MEMORY_QUOTAS[level])
            ):
                self.assertEqual(
                    memory_exclude_lanes(str(self.user.id)), expected, level
                )

    @override_settings(SEARCH_ENGINE=_search_engine_with_quotas(TEST_QUOTAS))
    def test_missing_key_excludes_nothing(self):
        # Dark/permissive: a config table that predates the key (or a
        # pre-key TIER_QUOTAS_JSON override) behaves like `team`.
        self.assertEqual(memory_exclude_lanes(str(self.user.id)), frozenset())

    def test_memory_disabled_tools(self):
        with override_settings(
            SEARCH_ENGINE=_search_engine_with_quotas(_MEMORY_QUOTAS["none"])
        ):
            self.assertEqual(
                tool_tiers.memory_disabled_tools(str(self.user.id)),
                {"search_past_conversations"},
            )
        for level in ("own", "team"):
            with override_settings(
                SEARCH_ENGINE=_search_engine_with_quotas(_MEMORY_QUOTAS[level])
            ):
                self.assertEqual(
                    tool_tiers.memory_disabled_tools(str(self.user.id)), set()
                )

    def test_disabled_tools_for_user_unions_the_pillars(self):
        rigged = {
            **TEST_QUOTAS,
            "free": {
                **TEST_QUOTAS["free"],
                "agent_tool_level": "read",
                "agent_memory": "none",
            },
        }
        writes = {t.name for t in REGISTRY.values() if t.requires_approval}
        with override_settings(SEARCH_ENGINE=_search_engine_with_quotas(rigged)):
            self.assertEqual(
                tool_tiers.disabled_tools_for_user(str(self.user.id)),
                writes | {"search_past_conversations"},
            )


class EntryPointWiringTests(SimpleTestCase):
    """§6.2's two directions, pinned at the tool seam."""

    def test_search_past_conversations_never_passes_exclude_lanes(self):
        # THE silent-empty guard: hand this tool an exclude_lanes and a
        # team-tier user's memory returns empty forever, silently.
        from origin.search_engine.agent.tools import search_past_conversations as spc

        ctx = ToolContext(team_id="t1", user_id="u1")
        with mock.patch.object(
            spc, "search", return_value={"results": []}
        ) as fake_search:
            REGISTRY["search_past_conversations"].run({"query": "q"}, ctx)
        kwargs = fake_search.call_args.kwargs
        self.assertEqual(kwargs["entity_types"], ["conversation"])
        self.assertNotIn("exclude_lanes", kwargs)

    def test_search_knowledge_base_pins_both_lanes(self):
        from origin.search_engine.agent.tools import search_kb

        ctx = ToolContext(team_id="t1", user_id="u1")
        with mock.patch.object(
            search_kb, "search", return_value={"results": []}
        ) as fake_search:
            REGISTRY["search_knowledge_base"].run({"query": "q"}, ctx)
        self.assertEqual(
            fake_search.call_args.kwargs["exclude_lanes"],
            frozenset({"conversation", "spotlight_answer"}),
        )
