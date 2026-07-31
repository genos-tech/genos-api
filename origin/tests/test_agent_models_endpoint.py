"""`GET /api/v2/agent/models/` — the Settings model-picker payload.

This endpoint had NO test at all until the effort-level work began,
despite being the single source the frontend picker renders from. The
pins below freeze the CURRENT contract before anything reshapes it:
the effort-level change is required to be additive (`current.effort` +
`efforts[]` appear; everything asserted here stays byte-compatible),
and these tests are what prove that.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.search_engine import quota

User = get_user_model()

URL = "/api/v2/agent/models/"


def _daily_era_se():
    """Ambient settings with the credits era pinned OFF.

    Credits-authoritative adds a `credits` block to this payload (its
    presence is the frontend's render switch), which the exact shape
    assertions below don't tolerate — by design: this file freezes the
    DAILY-era contract, and the credits-era payload has its own tests.
    The effort-level additions (`efforts[]`, `current.effort`) stay
    UNPINNED because the contract requires them to be additive, and
    the set-subtraction tolerances assert exactly that under either
    flag state.
    """
    se = dict(settings.SEARCH_ENGINE)
    se["AI_CREDITS_SHADOW"] = False
    se["AI_CREDITS_AUTHORITATIVE"] = False
    return se


@override_settings(SEARCH_ENGINE=_daily_era_se())
class AgentModelsEndpointTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="models-user",
            email="models@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])

    def test_requires_auth(self):
        resp = APIClient().get(URL)
        self.assertIn(resp.status_code, (401, 403))

    def test_payload_shape(self):
        """The full top-level contract the frontend picker relies on."""
        resp = self.client.get(URL)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            set(data) - {"efforts", "max_effort"}, {"tier", "current", "models", "limits"}
        )
        self.assertIn(data["tier"], ("free", "core", "pro", "max", "enterprise"))
        self.assertEqual(set(data["current"]) - {"effort"}, {"provider", "model"})
        self.assertEqual(set(data["limits"]), {"llm_ask", "web_search"})
        for block in data["limits"].values():
            self.assertEqual(set(block), {"used", "limit"})

    def test_models_mirror_the_catalog_in_order(self):
        """Rows come from MODEL_CATALOG, verbatim and IN ORDER — order is
        the cheap→expensive contract the whole catalog design leans on
        (quota fallback walks it; effort levels will index into it)."""
        resp = self.client.get(URL)
        rows = resp.json()["models"]
        catalog = settings.SEARCH_ENGINE["MODEL_CATALOG"]
        self.assertEqual(
            [(r["provider"], r["model"]) for r in rows],
            [(e["provider"], e["model"]) for e in catalog],
        )
        for row, entry in zip(rows, catalog):
            self.assertEqual(row["label"], entry["label"])
            self.assertEqual(row["note"], entry["note"])
            self.assertEqual(
                set(row),
                {"provider", "model", "label", "note", "daily_limit", "used_today"},
            )

    def test_per_model_quota_rows_reflect_the_tier_table(self):
        """daily_limit comes from the user's tier's model_daily; a model
        the tier caps at 0 must surface 0 (blocked), not null (unlimited)."""
        resp = self.client.get(URL)
        data = resp.json()
        model_daily = settings.SEARCH_ENGINE["TIER_QUOTAS"][data["tier"]]["model_daily"]
        for row in data["models"]:
            self.assertEqual(
                row["daily_limit"],
                model_daily.get(row["model"]),
                f"{row['model']}: endpoint and TIER_QUOTAS disagree",
            )
            self.assertEqual(row["used_today"], 0)

    def test_current_reflects_a_saved_preference(self):
        entry = settings.SEARCH_ENGINE["MODEL_CATALOG"][-1]
        self.user.preferred_llm_provider = entry["provider"]
        self.user.preferred_llm_model = entry["model"]
        self.user.save(update_fields=["preferred_llm_provider", "preferred_llm_model"])
        data = self.client.get(URL).json()
        self.assertEqual(data["current"]["provider"], entry["provider"])
        self.assertEqual(data["current"]["model"], entry["model"])

    def test_stale_saved_model_is_normalized_to_a_catalog_entry(self):
        """The picker-fallback path: a saved model that left the catalog
        must still render as a selectable option (same provider), never
        as a value the <Select> has no <Option> for."""
        self.user.preferred_llm_provider = "claude"
        self.user.preferred_llm_model = "claude-retired-model"
        self.user.save(update_fields=["preferred_llm_provider", "preferred_llm_model"])
        data = self.client.get(URL).json()
        self.assertEqual(data["current"]["provider"], "claude")
        catalog_pairs = {(r["provider"], r["model"]) for r in data["models"]}
        self.assertIn(("claude", data["current"]["model"]), catalog_pairs)


def _efforts_se(max_effort=None):
    """Effort levels ON (daily era), optionally with a free-tier
    `max_effort` ceiling in TIER_QUOTAS."""
    se = _daily_era_se()
    se["AGENT_EFFORT_LEVELS"] = True
    quotas = {t: dict(cfg) for t, cfg in se["TIER_QUOTAS"].items()}
    if max_effort is not None:
        quotas["free"]["max_effort"] = max_effort
    else:
        quotas["free"].pop("max_effort", None)
    se["TIER_QUOTAS"] = quotas
    return se


class EffortCeilingPayloadTests(TestCase):
    """The picker must never offer what the server would clamp
    (UX tier model §5): rungs above the tier ceiling are declared but
    `locked`, so the frontend renders an upgrade hint, not an option."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="ceiling-user",
            email="ceiling@test.com",
            password="testpass123",
            is_email_verified=True,
        )
        self.client.force_authenticate(user=self.user)

    def tearDown(self):
        quota.invalidate_effective_tier([self.user.id])

    @override_settings(SEARCH_ENGINE=_efforts_se(max_effort="low"))
    def test_rungs_above_the_ceiling_are_locked(self):
        data = self.client.get(URL).json()
        self.assertEqual(data["max_effort"], "low")
        for row in data["efforts"]:
            self.assertEqual(
                row["locked"],
                row["effort"] != "low",
                f"{row['provider']}/{row['effort']}",
            )

    @override_settings(SEARCH_ENGINE=_efforts_se())
    def test_missing_ceiling_locks_nothing(self):
        # The permissive/dark contract: a config table without the key
        # (or a pre-key TIER_QUOTAS_JSON override) offers every rung.
        data = self.client.get(URL).json()
        self.assertEqual(data["max_effort"], "high")
        self.assertFalse(any(row["locked"] for row in data["efforts"]))
