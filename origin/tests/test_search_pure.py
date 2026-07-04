"""Tests for the "pure + DB-backed-but-no-external" search_engine modules.

Covers:
  * quota.py          — tier resolution + ModelUsageCounter F() increments,
                        daily / per-model / cross-dimensional counters (real DB).
  * llm/choice.py     — resolve_user_choice fallback + warning paths (no DB).
  * reranker.py       — RRF score fusion math + index parsing + dispatch
                        (LLM/Cohere clients mocked).
  * friendly_titles.py — viewer-friendly chat title resolution (real DB:
                        Channel / ChannelMember / ProjectMaster lookups).
  * query_rewriter.py — variant parsing + rewrite_query (model client mocked).

External seams (LLM model client) are mocked at the import site; no live
OpenSearch / embeddings / network / LLM calls happen.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelMember
from origin.models.common.usage_models import ModelUsageCounter
from origin.models.project.prj_models import ProjectMaster
from origin.search_engine import query_rewriter, quota, reranker
from origin.search_engine.friendly_titles import (
    apply_friendly_titles,
    friendly_chat_title,
)
from origin.search_engine.llm.choice import LlmChoice, resolve_user_choice
from origin.search_engine.llm.types import FunctionCall
from origin.tests.test_base import BaseAPITestCase

# Deterministic, self-contained tier-quota config so the assertions don't
# depend on the placeholder numbers in apis/settings.py drifting over time.
_TIER_QUOTAS = {
    "free": {
        "llm_ask_daily": 20,
        "web_search_daily": 10,
        "model_daily": {
            "gemini-3.5-flash": 5,
            "claude-opus-4-7": 0,
        },
    },
    "pro": {
        "llm_ask_daily": 100,
        "web_search_daily": 50,
        "model_daily": {
            "gemini-3.5-flash": 100,
        },
    },
    # NOTE: "max" tier intentionally omitted to exercise the
    # _tier_cfg() fall-through to "free".
}


def _se(**overrides):
    """Return a SEARCH_ENGINE dict for @override_settings with overrides applied."""
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


# --------------------------------------------------------------------------- #
# quota.py — tier resolution                                                  #
# --------------------------------------------------------------------------- #


@override_settings(SEARCH_ENGINE_PATCHED=None)
class GetUserTierTests(BaseAPITestCase):
    def test_returns_users_tier(self):
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        self.assertEqual(quota.get_user_tier(str(self.user.id)), "pro")

    def test_default_free_tier(self):
        # Fresh users default to "free" (model field default).
        self.assertEqual(quota.get_user_tier(str(self.user.id)), "free")

    def test_unknown_user_id_falls_back_to_free(self):
        # A well-formed but non-existent UUID -> .first() is None -> "free".
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertEqual(quota.get_user_tier(missing), "free")

    def test_malformed_user_id_falls_back_to_free(self):
        # A non-UUID raises inside the ORM; the bare-except returns "free".
        self.assertEqual(quota.get_user_tier("not-a-uuid"), "free")

    def test_empty_tier_string_coerces_to_free(self):
        # `tier or "free"` — an empty stored value falls back too.
        self.user.tier = ""
        self.user.save(update_fields=["tier"])
        self.assertEqual(quota.get_user_tier(str(self.user.id)), "free")


# --------------------------------------------------------------------------- #
# quota.py — get_quota dispatch                                               #
# --------------------------------------------------------------------------- #


class GetQuotaTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.uid = str(self.user.id)

    def test_llm_ask_key_uses_llm_ask_daily(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, quota.LLM_ASK_KEY), 20)

    def test_web_search_key_uses_web_search_daily(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, quota.WEB_SEARCH_KEY), 10)

    def test_model_key_uses_model_daily(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, "gemini-3.5-flash"), 5)

    def test_model_zero_quota_is_int_zero_not_none(self):
        # A model with an explicit 0 limit must return 0 (blocked), NOT
        # None (unlimited). int(0) is falsy but `if v is None` guards it.
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, "claude-opus-4-7"), 0)

    def test_unknown_model_returns_none_unlimited(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertIsNone(quota.get_quota(self.uid, "some-unlisted-model"))

    def test_tier_without_model_daily_returns_none(self):
        cfg = {"free": {"llm_ask_daily": 7}}  # no model_daily key at all
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=cfg)):
            self.assertIsNone(quota.get_quota(self.uid, "gemini-3.5-flash"))
            self.assertEqual(quota.get_quota(self.uid, quota.LLM_ASK_KEY), 7)

    def test_pro_tier_resolves_pro_limits(self):
        self.user.tier = "pro"
        self.user.save(update_fields=["tier"])
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, quota.LLM_ASK_KEY), 100)
            self.assertEqual(quota.get_quota(self.uid, "gemini-3.5-flash"), 100)

    def test_missing_tier_cfg_falls_back_to_free(self):
        # "max" is absent from _TIER_QUOTAS -> _tier_cfg returns free's dict.
        self.user.tier = "max"
        self.user.save(update_fields=["tier"])
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            self.assertEqual(quota.get_quota(self.uid, quota.LLM_ASK_KEY), 20)

    def test_no_tier_quotas_config_returns_none(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS={})):
            self.assertIsNone(quota.get_quota(self.uid, quota.LLM_ASK_KEY))

    def test_string_numeric_limit_coerced_to_int(self):
        cfg = {"free": {"llm_ask_daily": "42"}}
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=cfg)):
            v = quota.get_quota(self.uid, quota.LLM_ASK_KEY)
            self.assertEqual(v, 42)
            self.assertIsInstance(v, int)


# --------------------------------------------------------------------------- #
# quota.py — counters (real DB, F() increments)                               #
# --------------------------------------------------------------------------- #


class CounterTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.uid = str(self.user.id)

    def test_used_today_zero_when_no_row(self):
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 0)

    def test_increment_creates_row_with_count_one(self):
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 1)
        row = ModelUsageCounter.objects.get(
            user_id=self.user.id,
            model_name=quota.LLM_ASK_KEY,
            usage_date=timezone.now().date(),
        )
        self.assertEqual(row.count, 1)

    def test_repeated_increment_uses_f_expression(self):
        for _ in range(5):
            quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 5)
        # Exactly one row — the increments mutate it, not create new rows.
        self.assertEqual(
            ModelUsageCounter.objects.filter(
                user_id=self.user.id, model_name=quota.LLM_ASK_KEY
            ).count(),
            1,
        )

    def test_counters_independent_per_key(self):
        # The three quota dimensions share one table, keyed by model_name.
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        quota.increment_usage(self.uid, quota.WEB_SEARCH_KEY)
        quota.increment_usage(self.uid, "gemini-3.5-flash")
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 2)
        self.assertEqual(quota.get_used_today(self.uid, quota.WEB_SEARCH_KEY), 1)
        self.assertEqual(quota.get_used_today(self.uid, "gemini-3.5-flash"), 1)

    def test_counters_independent_per_user(self):
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        quota.increment_usage(str(self.user2.id), quota.LLM_ASK_KEY)
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 2)
        self.assertEqual(
            quota.get_used_today(str(self.user2.id), quota.LLM_ASK_KEY), 1
        )

    def test_used_today_scoped_to_today_only(self):
        # A row dated yesterday must NOT be counted in today's usage.
        yesterday = timezone.now().date() - datetime.timedelta(days=1)
        ModelUsageCounter.objects.create(
            user_id=self.user.id,
            model_name=quota.LLM_ASK_KEY,
            usage_date=yesterday,
            count=9,
        )
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 0)
        quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        # Today's increment creates a NEW row, leaving yesterday's intact.
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 1)
        self.assertEqual(
            ModelUsageCounter.objects.filter(
                user_id=self.user.id, model_name=quota.LLM_ASK_KEY
            ).count(),
            2,
        )

    def test_increment_swallows_errors(self):
        # The contract: a counter write must never raise to the caller.
        with patch.object(
            ModelUsageCounter.objects,
            "get_or_create",
            side_effect=RuntimeError("db down"),
        ):
            # Should not raise.
            quota.increment_usage(self.uid, quota.LLM_ASK_KEY)
        # And nothing was written.
        self.assertEqual(quota.get_used_today(self.uid, quota.LLM_ASK_KEY), 0)


# --------------------------------------------------------------------------- #
# quota.py — check_remaining                                                  #
# --------------------------------------------------------------------------- #


class CheckRemainingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.uid = str(self.user.id)

    def test_unlimited_when_no_quota(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            quota.increment_usage(self.uid, "unlisted-model")
            allowed, used, limit = quota.check_remaining(self.uid, "unlisted-model")
        self.assertTrue(allowed)
        self.assertEqual(used, 1)
        self.assertIsNone(limit)

    def test_allowed_below_limit(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            quota.increment_usage(self.uid, "gemini-3.5-flash")  # 1 of 5
            allowed, used, limit = quota.check_remaining(self.uid, "gemini-3.5-flash")
        self.assertTrue(allowed)
        self.assertEqual(used, 1)
        self.assertEqual(limit, 5)

    def test_blocked_at_limit(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            for _ in range(5):
                quota.increment_usage(self.uid, "gemini-3.5-flash")  # 5 of 5
            allowed, used, limit = quota.check_remaining(self.uid, "gemini-3.5-flash")
        # used == limit -> NOT allowed (strict `used < limit`).
        self.assertFalse(allowed)
        self.assertEqual(used, 5)
        self.assertEqual(limit, 5)

    def test_zero_limit_blocks_immediately(self):
        with override_settings(SEARCH_ENGINE=_se(TIER_QUOTAS=_TIER_QUOTAS)):
            allowed, used, limit = quota.check_remaining(self.uid, "claude-opus-4-7")
        self.assertFalse(allowed)  # 0 < 0 is False
        self.assertEqual(used, 0)
        self.assertEqual(limit, 0)


# --------------------------------------------------------------------------- #
# llm/choice.py — resolve_user_choice                                         #
# --------------------------------------------------------------------------- #


class _ChoiceConfig:
    """SEARCH_ENGINE config with a known provider/model/catalog for choice tests."""

    BASE = {
        "LLM_PROVIDER": "gemini",
        "GEMINI_MODEL": "gemini-2.5-flash",
        "CLAUDE_MODEL": "claude-sonnet-4-6",
        "MODEL_CATALOG": [
            {"provider": "gemini", "model": "gemini-3.5-flash"},
            {"provider": "claude", "model": "claude-opus-4-7"},
        ],
    }


def _choice_se():
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(_ChoiceConfig.BASE)
    return cfg


class ResolveUserChoiceTests(SimpleTestCase):
    """No DB needed — resolve_user_choice reads settings only."""

    def _run(self, provider, model):
        with override_settings(SEARCH_ENGINE=_choice_se()):
            return resolve_user_choice(provider, model)

    def test_both_blank_returns_server_default(self):
        choice = self._run(None, None)
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))

    def test_empty_strings_treated_as_blank(self):
        choice = self._run("  ", "  ")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))

    def test_server_default_claude_when_provider_is_claude(self):
        cfg = _choice_se()
        cfg["LLM_PROVIDER"] = "claude"
        with override_settings(SEARCH_ENGINE=cfg):
            choice = resolve_user_choice(None, None)
        self.assertEqual(choice, LlmChoice("claude", "claude-sonnet-4-6"))

    def test_valid_catalog_pair_honored(self):
        choice = self._run("gemini", "gemini-3.5-flash")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-3.5-flash"))

    def test_valid_catalog_pair_claude(self):
        choice = self._run("claude", "claude-opus-4-7")
        self.assertEqual(choice, LlmChoice("claude", "claude-opus-4-7"))

    def test_provider_normalized_lowercase(self):
        choice = self._run("GEMINI", "gemini-3.5-flash")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-3.5-flash"))

    def test_unknown_provider_falls_back_with_warning(self):
        with self.assertLogs("origin.search_engine.llm.choice", level="WARNING") as cm:
            choice = self._run("openai", "gpt-4")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))
        self.assertTrue(any("unknown preferred_llm_provider" in m for m in cm.output))

    def test_provider_only_no_model_uses_provider_default(self):
        # Known provider, blank model -> that provider's server default model.
        choice = self._run("claude", None)
        self.assertEqual(choice, LlmChoice("claude", "claude-sonnet-4-6"))

    def test_provider_only_gemini_uses_gemini_default(self):
        choice = self._run("gemini", "")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))

    def test_stale_model_not_in_catalog_falls_back_with_warning(self):
        with self.assertLogs("origin.search_engine.llm.choice", level="WARNING") as cm:
            choice = self._run("gemini", "gemini-removed-model")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))
        self.assertTrue(any("not in MODEL_CATALOG" in m for m in cm.output))

    def test_wrong_provider_for_model_falls_back(self):
        # claude-opus-4-7 exists in catalog but under provider 'claude',
        # so (gemini, claude-opus-4-7) is NOT a catalog pair -> fallback.
        with self.assertLogs("origin.search_engine.llm.choice", level="WARNING"):
            choice = self._run("gemini", "claude-opus-4-7")
        self.assertEqual(choice, LlmChoice("gemini", "gemini-2.5-flash"))


# --------------------------------------------------------------------------- #
# settings.py — shipped model defaults must stay in the catalog               #
# --------------------------------------------------------------------------- #


class DefaultModelsInCatalogTests(SimpleTestCase):
    """Guard against drift between the server default models and the picker.

    `_server_default_choice()` (llm/choice.py) is an intentional operator
    escape hatch — it does NOT catalog-validate the env-configured default.
    So a stale `GEMINI_MODEL` / `CLAUDE_MODEL` default in settings.py ships
    silently: the agent loop runs it while the Settings picker (AgentModelsView,
    fed by MODEL_CATALOG) never offers it — and it may be retired on the
    provider, 404-ing every default user's ask. This pins the committed
    defaults to the catalog. It runs under CI's default config (no model env
    overrides), so it validates what we ship, not any per-deploy override.
    """

    def test_default_models_are_in_catalog(self):
        from django.conf import settings

        cfg = settings.SEARCH_ENGINE
        catalog = cfg.get("MODEL_CATALOG") or []

        def in_catalog(provider, model):
            return any(
                e.get("provider") == provider and e.get("model") == model
                for e in catalog
            )

        gemini_default = cfg.get("GEMINI_MODEL")
        claude_default = cfg.get("CLAUDE_MODEL")

        self.assertTrue(
            in_catalog("gemini", gemini_default),
            f"Default GEMINI_MODEL={gemini_default!r} is not in MODEL_CATALOG; "
            "users with no saved preference would run a model the Settings "
            "picker never offers (and that may be retired on the provider).",
        )
        self.assertTrue(
            in_catalog("claude", claude_default),
            f"Default CLAUDE_MODEL={claude_default!r} is not in MODEL_CATALOG.",
        )


# --------------------------------------------------------------------------- #
# gemini_client.py — Vertex region resolution (LLM vs embedder decoupling)     #
# --------------------------------------------------------------------------- #


class GeminiClientLocationTests(SimpleTestCase):
    """`_build_client` must let GEMINI_LLM_LOCATION override the LLM region
    without touching the embedder's GEMINI_LOCATION — so a preview model on
    `global`/us-central1 is reachable while embeddings stay on their index's
    region. The genai SDK is mocked; no client is really constructed."""

    def _build_with(self, **overrides):
        from django.conf import settings as dj_settings

        from origin.search_engine.llm import gemini_client

        cfg = dict(dj_settings.SEARCH_ENGINE)
        cfg.update(
            {
                "GEMINI_USE_VERTEX": True,
                "GEMINI_PROJECT": "proj-x",
                "GEMINI_SERVICE_ACCOUNT_FILE": "",  # ADC branch, no file load
            }
        )
        cfg.update(overrides)
        with override_settings(SEARCH_ENGINE=cfg):
            with patch.object(gemini_client, "genai") as mock_genai:
                gemini_client._build_client()
        return mock_genai.Client.call_args.kwargs

    def test_llm_location_overrides_gemini_location(self):
        kwargs = self._build_with(
            GEMINI_LOCATION="asia-northeast1", GEMINI_LLM_LOCATION="global"
        )
        self.assertEqual(kwargs["location"], "global")
        self.assertTrue(kwargs["vertexai"])
        self.assertEqual(kwargs["project"], "proj-x")

    def test_falls_back_to_gemini_location_when_unset(self):
        kwargs = self._build_with(
            GEMINI_LOCATION="asia-northeast1", GEMINI_LLM_LOCATION=""
        )
        self.assertEqual(kwargs["location"], "asia-northeast1")

    def test_final_fallback_us_central1(self):
        kwargs = self._build_with(GEMINI_LOCATION="", GEMINI_LLM_LOCATION="")
        self.assertEqual(kwargs["location"], "us-central1")


# --------------------------------------------------------------------------- #
# reranker.py — _fuse_by_score math (pure)                                    #
# --------------------------------------------------------------------------- #


class FuseByScoreTests(SimpleTestCase):
    def test_empty_returns_empty(self):
        self.assertEqual(reranker._fuse_by_score([], {}, 0.5, 10), [])

    def test_pure_rrf_weight_zero_preserves_rrf_order(self):
        # w=0 -> fused == rrf_norm; highest RRF score ranks first.
        cands = [
            {"id": "a", "score": 0.01},
            {"id": "b", "score": 0.05},
            {"id": "c", "score": 0.03},
        ]
        # Relevance deliberately inverts the RRF order — must be ignored at w=0.
        rel = {0: 1.0, 1: 0.0, 2: 0.0}
        out = reranker._fuse_by_score(cands, rel, 0.0, 10)
        self.assertEqual([c["id"] for c in out], ["b", "c", "a"])

    def test_pure_reranker_weight_one_uses_relevance_only(self):
        cands = [
            {"id": "a", "score": 0.05},  # highest RRF
            {"id": "b", "score": 0.01},
            {"id": "c", "score": 0.03},
        ]
        rel = {0: 0.1, 1: 0.9, 2: 0.5}  # b > c > a
        out = reranker._fuse_by_score(cands, rel, 1.0, 10)
        self.assertEqual([c["id"] for c in out], ["b", "c", "a"])

    def test_blend_half_half_explicit_math(self):
        # Two candidates: RRF normalizes to [0,1] = [0.0, 1.0].
        #   fused[0] = 0.5*0.0 + 0.5*rel0
        #   fused[1] = 0.5*1.0 + 0.5*rel1
        cands = [{"id": "lo", "score": 0.01}, {"id": "hi", "score": 0.05}]
        # Give the low-RRF item a huge relevance so it overtakes.
        rel = {0: 1.0, 1: 0.0}
        # fused[0] = 0.5, fused[1] = 0.5  -> tie; sorted is stable, keeps order.
        out = reranker._fuse_by_score(cands, rel, 0.5, 10)
        self.assertEqual([c["id"] for c in out], ["lo", "hi"])
        # Now push rel0 just over the tie.
        rel = {0: 1.0, 1: 0.0}
        cands2 = [{"id": "lo", "score": 0.01}, {"id": "hi", "score": 0.05}]
        out2 = reranker._fuse_by_score(cands2, {0: 1.0, 1: 0.01}, 0.5, 10)
        # fused = [0.5, 0.5+0.005=0.505] -> hi wins
        self.assertEqual([c["id"] for c in out2], ["hi", "lo"])

    def test_dropped_candidate_degrades_to_rrf_share_not_removed(self):
        # Candidate 1 absent from relevance -> relevance 0.0, but it is
        # still RETURNED (degraded), not dropped.
        cands = [
            {"id": "a", "score": 0.01},
            {"id": "b", "score": 0.05},  # high RRF, no relevance entry
            {"id": "c", "score": 0.03},
        ]
        rel = {0: 0.9, 2: 0.4}  # index 1 (b) intentionally missing
        out = reranker._fuse_by_score(cands, rel, 0.5, 10)
        # All three returned (nothing removed):
        self.assertEqual(len(out), 3)
        self.assertEqual({c["id"] for c in out}, {"a", "b", "c"})
        # rrf_norm: a=0.0, b=1.0, c=0.5 ; w=0.5
        #   a: 0.5*0.0 + 0.5*0.9 = 0.45
        #   b: 0.5*1.0 + 0.5*0.0 = 0.50
        #   c: 0.5*0.5 + 0.5*0.4 = 0.45
        # b first; a and c tie at 0.45 -> original order a before c.
        self.assertEqual([c["id"] for c in out], ["b", "a", "c"])

    def test_all_equal_rrf_lets_relevance_decide(self):
        # span == 0 -> every rrf_norm = 1.0; relevance breaks the tie.
        cands = [{"id": "a", "score": 0.02}, {"id": "b", "score": 0.02}]
        out = reranker._fuse_by_score(cands, {0: 0.1, 1: 0.9}, 0.5, 10)
        self.assertEqual([c["id"] for c in out], ["b", "a"])

    def test_output_k_truncates(self):
        cands = [{"id": str(i), "score": float(i)} for i in range(5)]
        out = reranker._fuse_by_score(cands, {}, 0.0, 2)
        self.assertEqual(len(out), 2)
        # Highest RRF scores: 4 then 3.
        self.assertEqual([c["id"] for c in out], ["4", "3"])

    def test_output_k_zero_returns_empty(self):
        cands = [{"id": "a", "score": 0.1}]
        self.assertEqual(reranker._fuse_by_score(cands, {0: 1.0}, 0.5, 0), [])

    def test_weight_clamped_above_one(self):
        # weight > 1 is clamped to 1.0 (pure reranker).
        cands = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.1}]
        out = reranker._fuse_by_score(cands, {0: 0.0, 1: 1.0}, 5.0, 10)
        self.assertEqual([c["id"] for c in out], ["b", "a"])

    def test_weight_clamped_below_zero(self):
        # weight < 0 clamped to 0.0 (pure RRF).
        cands = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.1}]
        out = reranker._fuse_by_score(cands, {0: 0.0, 1: 1.0}, -3.0, 10)
        self.assertEqual([c["id"] for c in out], ["a", "b"])

    def test_missing_score_treated_as_zero(self):
        cands = [{"id": "a"}, {"id": "b", "score": 0.5}]  # a has no score
        out = reranker._fuse_by_score(cands, {}, 0.0, 10)
        # rrf = [0.0, 0.5]; norm = [0.0, 1.0]; b first.
        self.assertEqual([c["id"] for c in out], ["b", "a"])


# --------------------------------------------------------------------------- #
# reranker.py — _parse_indices                                                #
# --------------------------------------------------------------------------- #


class ParseIndicesTests(SimpleTestCase):
    def test_simple_array(self):
        self.assertEqual(reranker._parse_indices("[2, 0, 1]", valid_range=3), [2, 0, 1])

    def test_empty_array_valid(self):
        # [] means "no candidate relevant" — a VALID parse, not None.
        self.assertEqual(reranker._parse_indices("[]", valid_range=3), [])

    def test_array_embedded_in_prose(self):
        raw = "Here you go: [1, 0]. Done."
        self.assertEqual(reranker._parse_indices(raw, valid_range=2), [1, 0])

    def test_code_fenced_array(self):
        raw = "```json\n[0, 2]\n```"
        self.assertEqual(reranker._parse_indices(raw, valid_range=3), [0, 2])

    def test_no_array_returns_none(self):
        self.assertIsNone(reranker._parse_indices("no array here", valid_range=3))

    def test_out_of_range_index_returns_none(self):
        self.assertIsNone(reranker._parse_indices("[0, 5]", valid_range=3))

    def test_float_element_returns_none(self):
        # A float is not an int -> _parse_indices rejects the whole array.
        self.assertIsNone(reranker._parse_indices("[1.5]", valid_range=3))

    def test_negative_literal_no_array_match_returns_none(self):
        # The integer-only regex \[[\s\d,]*\] cannot match "[-1]" at all
        # (the '-' breaks the char class and there is no other '['), so
        # _parse_indices finds no array and returns None. (Verified: the
        # regex .search() returns None for this input.)
        self.assertIsNone(reranker._parse_indices("[-1]", valid_range=3))

    def test_out_of_range_high_index_returns_none(self):
        # The `x >= valid_range` guard rejects the whole array.
        self.assertIsNone(reranker._parse_indices("[9]", valid_range=3))

    def test_duplicate_indices_deduped(self):
        # Duplicates are skipped (not an error), order preserved.
        self.assertEqual(
            reranker._parse_indices("[0, 1, 0, 1]", valid_range=2), [0, 1]
        )

    def test_non_int_element_returns_none(self):
        self.assertIsNone(reranker._parse_indices('["a", 1]', valid_range=3))

    def test_first_array_wins(self):
        # The regex grabs the FIRST [\s\d,]* array.
        self.assertEqual(
            reranker._parse_indices("[0, 1] then [2]", valid_range=3), [0, 1]
        )


# --------------------------------------------------------------------------- #
# reranker.py — _build_user_prompt & _cohere_doc_text                         #
# --------------------------------------------------------------------------- #


class PromptBuildingTests(SimpleTestCase):
    def test_build_user_prompt_numbers_and_truncates(self):
        long_snip = "x" * 300
        cands = [
            {"entity_id": "e1", "title": "Title One", "snippet": long_snip},
            {"entity_id": "e2", "title": "", "snippet": ""},
        ]
        prompt = reranker._build_user_prompt("my query", cands)
        self.assertIn("Query: my query", prompt)
        self.assertIn("[0] e1 | Title One |", prompt)
        self.assertIn("[1] e2 |", prompt)
        # Truncated to 200 chars + ellipsis.
        self.assertIn("…", prompt)
        self.assertNotIn("x" * 250, prompt)

    def test_build_user_prompt_strips_workspace_tags(self):
        cands = [
            {
                "entity_id": "e1",
                "title": "T",
                "snippet": "<workspace_content>hi</workspace_content>",
            }
        ]
        prompt = reranker._build_user_prompt("q", cands)
        self.assertNotIn("<workspace_content>", prompt)
        self.assertIn("hi", prompt)

    def test_build_user_prompt_missing_entity_id(self):
        cands = [{"title": "T", "snippet": "s"}]  # no entity_id
        prompt = reranker._build_user_prompt("q", cands)
        self.assertIn("[0] ? | T | s", prompt)

    def test_cohere_doc_text_title_and_snippet(self):
        e = {"title": "Hello", "snippet": "world"}
        self.assertEqual(reranker._cohere_doc_text(e), "Hello\nworld")

    def test_cohere_doc_text_title_only(self):
        self.assertEqual(reranker._cohere_doc_text({"title": "Hello"}), "Hello")

    def test_cohere_doc_text_snippet_only(self):
        self.assertEqual(
            reranker._cohere_doc_text({"snippet": "just snippet"}), "just snippet"
        )

    def test_cohere_doc_text_empty_is_untitled(self):
        self.assertEqual(reranker._cohere_doc_text({}), "(untitled)")

    def test_cohere_doc_text_strips_tags_and_truncates(self):
        e = {
            "title": "T",
            "snippet": "<workspace_content>" + ("y" * 300) + "</workspace_content>",
        }
        out = reranker._cohere_doc_text(e)
        self.assertNotIn("<workspace_content>", out)
        self.assertTrue(out.endswith("…"))


# --------------------------------------------------------------------------- #
# reranker.py — rerank() dispatch + _rerank_llm (client mocked)               #
# --------------------------------------------------------------------------- #


def _mk_entities(n):
    return [
        {"entity_id": f"e{i}", "title": f"T{i}", "snippet": f"s{i}", "score": 0.01 * (i + 1)}
        for i in range(n)
    ]


def _client_yielding(text):
    """Build a MagicMock ModelClient whose generate_step yields (text, None)."""
    client = MagicMock()
    client.generate_step.return_value = iter([(text, None)])
    return client


class RerankLlmTests(SimpleTestCase):
    def setUp(self):
        # Default config: reranker reorders (no fusion, no keep-dropped).
        self._patcher = override_settings(
            SEARCH_ENGINE=_se(
                RAG_RERANKER_PROVIDER="llm",
                RAG_RERANK_FUSION=False,
                RAG_RERANK_KEEP_DROPPED=False,
                RAG_RERANKER_MODEL="",
            )
        )
        self._patcher.enable()

    def tearDown(self):
        self._patcher.disable()

    def test_fewer_than_two_candidates_short_circuits(self):
        ents = _mk_entities(1)
        # input_k <= 1 -> returns entities[:output_k] without calling client.
        with patch("origin.search_engine.reranker.get_model_client") as gmc:
            out = reranker.rerank(query="q", entities=ents, input_k=1, output_k=10)
            gmc.assert_not_called()
        self.assertEqual(out, ents)

    def test_empty_entities_short_circuits(self):
        with patch("origin.search_engine.reranker.get_model_client") as gmc:
            out = reranker.rerank(query="q", entities=[], input_k=20, output_k=10)
            gmc.assert_not_called()
        self.assertEqual(out, [])

    def test_reorders_by_model_indices(self):
        ents = _mk_entities(3)
        client = _client_yielding("[2, 0, 1]")
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        self.assertEqual([e["entity_id"] for e in out], ["e2", "e0", "e1"])

    def test_drops_omitted_indices(self):
        ents = _mk_entities(3)
        client = _client_yielding("[2]")  # model keeps only index 2
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        self.assertEqual([e["entity_id"] for e in out], ["e2"])

    def test_empty_model_array_returns_nothing(self):
        ents = _mk_entities(3)
        client = _client_yielding("[]")
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        self.assertEqual(out, [])

    def test_unparseable_output_falls_back_to_prerank(self):
        ents = _mk_entities(3)
        client = _client_yielding("the model rambled with no array")
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=2)
        # Fallback is candidates[:output_k] (pre-rerank order).
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])

    def test_client_exception_falls_back_to_prerank(self):
        ents = _mk_entities(3)
        client = MagicMock()
        client.generate_step.side_effect = RuntimeError("boom")
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=2)
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])

    def test_input_k_limits_candidates_sent(self):
        ents = _mk_entities(5)
        client = _client_yielding("[1, 0]")
        with patch("origin.search_engine.reranker.get_model_client", return_value=client):
            out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=5)
        # Only first 2 were candidates -> reorder within those.
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])

    def test_keep_dropped_appends_omitted_in_order(self):
        ents = _mk_entities(3)
        client = _client_yielding("[2]")  # keep only index 2
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_RERANKER_PROVIDER="llm",
                RAG_RERANK_FUSION=False,
                RAG_RERANK_KEEP_DROPPED=True,
            )
        ):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        # index 2 first, then the omitted 0, 1 appended in original order.
        self.assertEqual([e["entity_id"] for e in out], ["e2", "e0", "e1"])

    def test_fusion_blends_positional_relevance(self):
        ents = _mk_entities(2)  # e0 score 0.01, e1 score 0.02
        # Model says order [0, 1] -> positional relevance: idx0=1.0, idx1=0.5
        client = _client_yielding("[0, 1]")
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_RERANKER_PROVIDER="llm",
                RAG_RERANK_FUSION=True,
                RAG_RERANK_FUSION_WEIGHT=0.5,
            )
        ):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        # rrf_norm: e0=0.0, e1=1.0. relevance: 0->1.0, 1->0.5
        #   e0: 0.5*0.0 + 0.5*1.0 = 0.5
        #   e1: 0.5*1.0 + 0.5*0.5 = 0.75  -> e1 first
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])

    def test_model_override_passed_through(self):
        ents = _mk_entities(2)
        client = _client_yielding("[1, 0]")
        with override_settings(
            SEARCH_ENGINE=_se(
                RAG_RERANKER_PROVIDER="llm",
                RAG_RERANK_FUSION=False,
                RAG_RERANKER_MODEL="gemini-3.5-flash",
            )
        ):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        _, kwargs = client.generate_step.call_args
        self.assertEqual(kwargs.get("model_override"), "gemini-3.5-flash")

    def test_unexpected_function_call_warned_text_still_used(self):
        # No tools are declared, so a function-call yield is unexpected:
        # it's logged + ignored, and the accompanying text still drives
        # the reorder.
        ents = _mk_entities(3)
        client = MagicMock()
        client.generate_step.return_value = iter(
            [
                (None, FunctionCall(name="surprise", args={})),
                ("[2, 0, 1]", None),
            ]
        )
        with patch(
            "origin.search_engine.reranker.get_model_client", return_value=client
        ):
            with self.assertLogs(
                "origin.search_engine.reranker", level="WARNING"
            ) as cm:
                out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        self.assertEqual([e["entity_id"] for e in out], ["e2", "e0", "e1"])
        self.assertTrue(any("function call" in m for m in cm.output))


class RerankDispatchTests(SimpleTestCase):
    def test_unknown_provider_falls_back_to_llm_with_warning(self):
        ents = _mk_entities(2)
        client = _client_yielding("[1, 0]")
        with override_settings(SEARCH_ENGINE=_se(RAG_RERANKER_PROVIDER="bogus")):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                with self.assertLogs(
                    "origin.search_engine.reranker", level="WARNING"
                ) as cm:
                    out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])
        self.assertTrue(any("unknown" in m for m in cm.output))

    def test_future_provider_falls_back_to_llm_with_warning(self):
        ents = _mk_entities(2)
        client = _client_yielding("[0, 1]")
        with override_settings(SEARCH_ENGINE=_se(RAG_RERANKER_PROVIDER="jina")):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                with self.assertLogs(
                    "origin.search_engine.reranker", level="WARNING"
                ) as cm:
                    reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        self.assertTrue(any("not yet implemented" in m for m in cm.output))

    def test_cohere_without_key_falls_back_to_llm(self):
        ents = _mk_entities(2)
        client = _client_yielding("[1, 0]")
        with override_settings(
            SEARCH_ENGINE=_se(RAG_RERANKER_PROVIDER="cohere", COHERE_API_KEY="")
        ):
            with patch(
                "origin.search_engine.reranker.get_model_client", return_value=client
            ):
                with self.assertLogs(
                    "origin.search_engine.reranker", level="WARNING"
                ) as cm:
                    out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        # Falls back to the LLM reranker, which reorders per the mock.
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])
        self.assertTrue(any("COHERE_API_KEY is unset" in m for m in cm.output))


def _mock_httpx_client(status_code=200, json_body=None, headers=None):
    """Build a context-manager-capable mock httpx.Client.

    The real code does `with httpx.Client(timeout=...) as client:` then
    `client.post(...)`. We patch `httpx.Client` to a MagicMock whose
    __enter__ returns an inner client whose .post returns a fake response.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body if json_body is not None else {}
    resp.text = "" if json_body is not None else "error body"

    inner = MagicMock()
    inner.post.return_value = resp

    client_cm = MagicMock()
    client_cm.__enter__.return_value = inner
    client_cm.__exit__.return_value = False

    factory = MagicMock(return_value=client_cm)
    return factory, inner, resp


class RerankCohereTests(SimpleTestCase):
    """Cohere path with a key present — httpx fully mocked, no network."""

    def _se_cohere(self, **extra):
        base = dict(
            RAG_RERANKER_PROVIDER="cohere",
            COHERE_API_KEY="test-key",
            RAG_RERANK_FUSION=False,
        )
        base.update(extra)
        return _se(**base)

    def test_honors_cohere_score_desc_order_without_fusion(self):
        ents = _mk_entities(3)
        # Cohere returns results already sorted by relevance desc.
        body = {
            "id": "x",
            "results": [
                {"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1},
            ],
        }
        factory, inner, _ = _mock_httpx_client(json_body=body)
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=3)
        self.assertEqual([e["entity_id"] for e in out], ["e2", "e0", "e1"])
        # The request was actually built and posted.
        self.assertTrue(inner.post.called)

    def test_top_n_truncates_via_output_k(self):
        ents = _mk_entities(4)
        body = {
            "results": [
                {"index": 3, "relevance_score": 0.9},
                {"index": 1, "relevance_score": 0.6},
            ]
        }
        factory, inner, _ = _mock_httpx_client(json_body=body)
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                out = reranker.rerank(query="q", entities=ents, input_k=4, output_k=2)
        self.assertEqual([e["entity_id"] for e in out], ["e3", "e1"])
        # top_n in the payload is min(output_k, len(documents)) == 2.
        _, kwargs = inner.post.call_args
        self.assertEqual(kwargs["json"]["top_n"], 2)
        self.assertEqual(kwargs["json"]["model"], "rerank-v3.5")

    def test_fusion_blends_cohere_relevance(self):
        ents = _mk_entities(2)  # e0 score 0.01, e1 score 0.02
        body = {
            "results": [
                {"index": 0, "relevance_score": 1.0},
                {"index": 1, "relevance_score": 0.0},
            ]
        }
        factory, _, _ = _mock_httpx_client(json_body=body)
        with override_settings(
            SEARCH_ENGINE=self._se_cohere(
                RAG_RERANK_FUSION=True, RAG_RERANK_FUSION_WEIGHT=0.5
            )
        ):
            with patch("httpx.Client", factory):
                out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=2)
        # rrf_norm: e0=0.0, e1=1.0. relevance: 0->1.0, 1->0.0
        #   e0: 0.5*0.0 + 0.5*1.0 = 0.5
        #   e1: 0.5*1.0 + 0.5*0.0 = 0.5  -> tie -> stable order [e0, e1]
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])

    def test_out_of_range_and_dup_indices_filtered(self):
        ents = _mk_entities(2)
        body = {
            "results": [
                {"index": 5, "relevance_score": 0.9},  # out of range -> skip
                {"index": 1, "relevance_score": 0.8},
                {"index": 1, "relevance_score": 0.7},  # dup -> skip
                {"index": "x", "relevance_score": 0.6},  # non-int -> skip
                {"index": 0, "relevance_score": 0.5},
            ]
        }
        factory, _, _ = _mock_httpx_client(json_body=body)
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                out = reranker.rerank(query="q", entities=ents, input_k=2, output_k=5)
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])

    def test_no_usable_indices_falls_back_to_prerank(self):
        ents = _mk_entities(3)
        body = {"results": [{"index": 99, "relevance_score": 0.9}]}  # all OOR
        factory, _, _ = _mock_httpx_client(json_body=body)
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                with self.assertLogs(
                    "origin.search_engine.reranker", level="WARNING"
                ) as cm:
                    out = reranker.rerank(
                        query="q", entities=ents, input_k=3, output_k=2
                    )
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])
        self.assertTrue(any("no usable indices" in m for m in cm.output))

    def test_non_200_falls_back_to_prerank(self):
        ents = _mk_entities(3)
        factory, _, _ = _mock_httpx_client(status_code=500)
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                with self.assertLogs(
                    "origin.search_engine.reranker", level="WARNING"
                ) as cm:
                    out = reranker.rerank(
                        query="q", entities=ents, input_k=3, output_k=2
                    )
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])
        self.assertTrue(any("returned 500" in m for m in cm.output))

    def test_network_exception_falls_back_to_prerank(self):
        ents = _mk_entities(3)
        factory = MagicMock(side_effect=RuntimeError("connection reset"))
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                out = reranker.rerank(query="q", entities=ents, input_k=3, output_k=2)
        self.assertEqual([e["entity_id"] for e in out], ["e0", "e1"])

    def test_429_then_200_retries_and_succeeds(self):
        ents = _mk_entities(2)
        # First POST 429, second POST 200 — assert the retry happens and
        # the successful body is used. Patch time.sleep so no real wait.
        resp_429 = MagicMock()
        resp_429.status_code = 429
        resp_429.headers = {"Retry-After": "0"}
        resp_429.text = "rate limited"

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.headers = {}
        resp_ok.json.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.1},
            ]
        }

        inner = MagicMock()
        inner.post.side_effect = [resp_429, resp_ok]
        client_cm = MagicMock()
        client_cm.__enter__.return_value = inner
        client_cm.__exit__.return_value = False
        factory = MagicMock(return_value=client_cm)

        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client", factory):
                with patch("time.sleep") as slept:
                    with self.assertLogs(
                        "origin.search_engine.reranker", level="WARNING"
                    ) as cm:
                        out = reranker.rerank(
                            query="q", entities=ents, input_k=2, output_k=2
                        )
        self.assertEqual([e["entity_id"] for e in out], ["e1", "e0"])
        self.assertEqual(inner.post.call_count, 2)
        self.assertTrue(slept.called)
        self.assertTrue(any("429" in m for m in cm.output))

    def test_empty_entities_short_circuits_before_key_check(self):
        # input_k<=1 / empty entities return before any network setup.
        with override_settings(SEARCH_ENGINE=self._se_cohere()):
            with patch("httpx.Client") as factory:
                out = reranker.rerank(query="q", entities=[], input_k=20, output_k=10)
                factory.assert_not_called()
        self.assertEqual(out, [])


# --------------------------------------------------------------------------- #
# query_rewriter.py — _parse_variants (pure)                                  #
# --------------------------------------------------------------------------- #


class ParseVariantsTests(SimpleTestCase):
    def test_simple_string_array(self):
        self.assertEqual(
            query_rewriter._parse_variants('["foo bar", "baz qux"]'),
            ["foo bar", "baz qux"],
        )

    def test_strips_whitespace_and_drops_blank(self):
        self.assertEqual(
            query_rewriter._parse_variants('["  a  ", "", "   ", "b"]'),
            ["a", "b"],
        )

    def test_no_array_returns_empty(self):
        self.assertEqual(query_rewriter._parse_variants("nope"), [])

    def test_malformed_json_returns_empty(self):
        self.assertEqual(query_rewriter._parse_variants("[foo, bar]"), [])

    def test_non_string_elements_dropped(self):
        self.assertEqual(
            query_rewriter._parse_variants('["keep", 1, true, "also"]'),
            ["keep", "also"],
        )

    def test_array_in_prose(self):
        self.assertEqual(
            query_rewriter._parse_variants('Sure: ["a", "b"] hope that helps'),
            ["a", "b"],
        )

    def test_non_greedy_picks_first_array(self):
        # `\[.*?\]` non-greedy stops at the first closing bracket.
        self.assertEqual(
            query_rewriter._parse_variants('["a"] and ["b"]'),
            ["a"],
        )

    def test_bracket_inside_variant_truncates_to_invalid_json(self):
        # The non-greedy regex stops at the FIRST `]`, so a variant string
        # that itself contains `]` yields a truncated, unparseable slice
        # -> json.loads fails -> []. Documents a real limitation.
        self.assertEqual(query_rewriter._parse_variants('["a]b", "c"]'), [])


# --------------------------------------------------------------------------- #
# query_rewriter.py — rewrite_query (client mocked)                           #
# --------------------------------------------------------------------------- #


class RewriteQueryTests(SimpleTestCase):
    def test_empty_query_returns_empty_list(self):
        self.assertEqual(query_rewriter.rewrite_query(""), [])

    def test_whitespace_query_returns_empty_list(self):
        self.assertEqual(query_rewriter.rewrite_query("   "), [])

    def test_zero_variants_returns_original_only(self):
        # num_variants <= 0 short-circuits before any LLM call.
        with patch("origin.search_engine.query_rewriter.get_model_client") as gmc:
            out = query_rewriter.rewrite_query("hi", num_variants=0)
            gmc.assert_not_called()
        self.assertEqual(out, ["hi"])

    def test_happy_path_prepends_original(self):
        client = _client_yielding('["alt one", "alt two"]')
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("orig", num_variants=3)
        self.assertEqual(out, ["orig", "alt one", "alt two"])

    def test_variant_equal_to_original_deduped(self):
        # A variant matching the original (case-insensitive) is dropped.
        client = _client_yielding('["ORIG", "new one"]')
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("orig", num_variants=3)
        self.assertEqual(out, ["orig", "new one"])

    def test_duplicate_variants_deduped_case_insensitive(self):
        client = _client_yielding('["Alpha", "alpha", "beta"]')
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("q", num_variants=5)
        self.assertEqual(out, ["q", "Alpha", "beta"])

    def test_num_variants_caps_output(self):
        client = _client_yielding('["v1", "v2", "v3", "v4", "v5"]')
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("q", num_variants=2)
        # Original + only first 2 variants.
        self.assertEqual(out, ["q", "v1", "v2"])

    def test_unparseable_output_returns_original_only(self):
        client = _client_yielding("I cannot help with that")
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("q", num_variants=3)
        self.assertEqual(out, ["q"])

    def test_client_exception_returns_original_only(self):
        client = MagicMock()
        client.generate_step.side_effect = RuntimeError("llm down")
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            out = query_rewriter.rewrite_query("q", num_variants=3)
        self.assertEqual(out, ["q"])

    def test_model_override_passed_through(self):
        client = _client_yielding('["a"]')
        with override_settings(SEARCH_ENGINE=_se(RAG_REWRITE_MODEL="gemini-3.5-flash")):
            with patch(
                "origin.search_engine.query_rewriter.get_model_client",
                return_value=client,
            ):
                query_rewriter.rewrite_query("q", num_variants=3)
        _, kwargs = client.generate_step.call_args
        self.assertEqual(kwargs.get("model_override"), "gemini-3.5-flash")

    def test_unexpected_function_call_warned_text_still_used(self):
        # A function-call yield (no tools given) is logged + ignored; the
        # text yield still produces the variants.
        client = MagicMock()
        client.generate_step.return_value = iter(
            [
                (None, FunctionCall(name="surprise", args={})),
                ('["alt"]', None),
            ]
        )
        with patch(
            "origin.search_engine.query_rewriter.get_model_client", return_value=client
        ):
            with self.assertLogs(
                "origin.search_engine.query_rewriter", level="WARNING"
            ) as cm:
                out = query_rewriter.rewrite_query("orig", num_variants=3)
        self.assertEqual(out, ["orig", "alt"])
        self.assertTrue(any("function call" in m for m in cm.output))


# --------------------------------------------------------------------------- #
# friendly_titles.py — DB-backed lookups                                      #
# --------------------------------------------------------------------------- #


class FriendlyChatTitleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.viewer = str(self.user.id)

    # --- guard clauses ---

    def test_none_chat_type_returns_none(self):
        self.assertIsNone(friendly_chat_title(self.viewer, None, "anything"))

    def test_none_chat_id_returns_none(self):
        self.assertIsNone(friendly_chat_title(self.viewer, "dm", None))

    def test_unknown_label_returns_none(self):
        ch = Channel.objects.create(team=self.team, kind=2, title="grp")
        self.assertIsNone(friendly_chat_title(self.viewer, "weird", ch.id))

    # --- DM ---

    def test_dm_returns_partner_username(self):
        ch = Channel.objects.create(team=self.team, kind=1)
        ChannelMember.objects.create(channel=ch, user=self.user)
        ChannelMember.objects.create(channel=ch, user=self.user2)
        # Viewer is self.user -> partner is user2 ("otheruser").
        self.assertEqual(
            friendly_chat_title(self.viewer, "dm", ch.id), "otheruser"
        )

    def test_dm_self_dm_falls_back_to_self(self):
        ch = Channel.objects.create(team=self.team, kind=1)
        ChannelMember.objects.create(channel=ch, user=self.user)
        # Only the viewer is a member -> partner_id None -> falls back to self.
        self.assertEqual(
            friendly_chat_title(self.viewer, "dm", ch.id), "testuser"
        )

    def test_dm_deleted_channel_returns_none(self):
        ch = Channel.objects.create(team=self.team, kind=1, is_deleted=True)
        ChannelMember.objects.create(channel=ch, user=self.user)
        ChannelMember.objects.create(channel=ch, user=self.user2)
        self.assertIsNone(friendly_chat_title(self.viewer, "dm", ch.id))

    def test_dm_no_members_returns_none(self):
        ch = Channel.objects.create(team=self.team, kind=1)
        self.assertIsNone(friendly_chat_title(self.viewer, "dm", ch.id))

    def test_dm_deleted_member_excluded(self):
        ch = Channel.objects.create(team=self.team, kind=1)
        ChannelMember.objects.create(channel=ch, user=self.user)
        ChannelMember.objects.create(channel=ch, user=self.user2, is_deleted=True)
        # user2's membership is soft-deleted -> only viewer remains ->
        # partner None -> self.user fallback.
        self.assertEqual(
            friendly_chat_title(self.viewer, "dm", ch.id), "testuser"
        )

    def test_dm_malformed_chat_id_returns_none(self):
        # _channel() catches ValidationError/ValueError on a bad UUID.
        self.assertIsNone(friendly_chat_title(self.viewer, "dm", "not-a-uuid"))

    def test_dm_wrong_kind_returns_none(self):
        # The channel exists but is a GM (kind=2); _channel(1) won't match.
        ch = Channel.objects.create(team=self.team, kind=2, title="grp")
        self.assertIsNone(friendly_chat_title(self.viewer, "dm", ch.id))

    # --- GM / MDM ---

    def test_gm_returns_channel_title(self):
        ch = Channel.objects.create(team=self.team, kind=2, title="Engineering")
        self.assertEqual(friendly_chat_title(self.viewer, "gm", ch.id), "Engineering")

    def test_mdm_returns_channel_title(self):
        ch = Channel.objects.create(team=self.team, kind=4, title="Multi DM")
        self.assertEqual(friendly_chat_title(self.viewer, "mdm", ch.id), "Multi DM")

    def test_gm_blank_title_returns_none(self):
        # Channel.title default is "" -> `(channel.title or None)` -> None.
        ch = Channel.objects.create(team=self.team, kind=2, title="")
        self.assertIsNone(friendly_chat_title(self.viewer, "gm", ch.id))

    def test_gm_missing_channel_returns_none(self):
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertIsNone(friendly_chat_title(self.viewer, "gm", missing))

    # --- PM ---

    def test_pm_returns_project_name(self):
        # Creating a ProjectMaster auto-creates its kind=PM Channel via a
        # post_save signal (origin/signals/pm_channel_signals.py), so we
        # fetch that channel rather than create a colliding one.
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Apollo", owner=self.user
        )
        ch = Channel.objects.get(project=project, kind=3)
        self.assertEqual(friendly_chat_title(self.viewer, "pm", ch.id), "Apollo")

    def test_pm_channel_without_project_returns_none(self):
        # A kind=3 channel with project_id null -> None.
        ch = Channel.objects.create(team=self.team, kind=3)
        self.assertIsNone(friendly_chat_title(self.viewer, "pm", ch.id))

    def test_pm_missing_channel_returns_none(self):
        missing = "00000000-0000-0000-0000-000000000000"
        self.assertIsNone(friendly_chat_title(self.viewer, "pm", missing))


class ApplyFriendlyTitlesTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.viewer = str(self.user.id)

    def test_non_chat_rows_untouched(self):
        rows = [{"entity_type": "task", "title": "Do thing"}]
        out = apply_friendly_titles(rows, self.viewer)
        self.assertEqual(out[0]["title"], "Do thing")

    def test_chat_row_title_replaced(self):
        ch = Channel.objects.create(team=self.team, kind=2, title="Real Name")
        rows = [
            {
                "entity_type": "chat",
                "chat_type": "gm",
                "chat_id": str(ch.id),
                "title": "GM 5",  # placeholder
            }
        ]
        out = apply_friendly_titles(rows, self.viewer)
        self.assertEqual(out[0]["title"], "Real Name")

    def test_lookup_failure_keeps_existing_title(self):
        # A missing channel -> friendly_chat_title None -> title preserved.
        rows = [
            {
                "entity_type": "chat",
                "chat_type": "gm",
                "chat_id": "00000000-0000-0000-0000-000000000000",
                "title": "GM 5",
            }
        ]
        out = apply_friendly_titles(rows, self.viewer)
        self.assertEqual(out[0]["title"], "GM 5")

    def test_returns_same_list_object(self):
        rows = [{"entity_type": "note", "title": "x"}]
        self.assertIs(apply_friendly_titles(rows, self.viewer), rows)

    def test_mixed_rows(self):
        ch = Channel.objects.create(team=self.team, kind=2, title="Friendly")
        rows = [
            {"entity_type": "task", "title": "T"},
            {
                "entity_type": "chat",
                "chat_type": "gm",
                "chat_id": str(ch.id),
                "title": "placeholder",
            },
            {"entity_type": "note", "title": "N"},
        ]
        out = apply_friendly_titles(rows, self.viewer)
        self.assertEqual([r["title"] for r in out], ["T", "Friendly", "N"])
