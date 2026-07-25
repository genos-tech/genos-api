"""`apis/llm_catalog.py` — the YAML catalog loader.

Models are managed in `apis/llm_models.yaml` so they can be swapped
whenever a provider ships something new. That makes the loader's
VALIDATION the safety net for a file that gets hand-edited often, under
time pressure, by someone who just wants the new model live. Every
failure it guards is silent and expensive:

  * a model with no cap is UNLIMITED, not zero → unbounded premium subsidy
  * a rung in the wrong slot makes quota fallback step UP in price
  * a tier-name typo uncaps that entire tier

None of those raise at request time. They show up on the invoice.
"""

from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings
from django.test import SimpleTestCase

from apis.llm_catalog import CatalogError, load_llm_catalog

GOOD = """
version: 1
providers:
    gemini:
        - model: g-cheap
          class: light
          label: G Cheap
          note: Fast.
          price: {input: 0.30, output: 2.50}
        - model: g-mid
          class: middle
          label: G Mid
          note: Balanced.
          price: {input: 1.50, output: 7.50}
        - model: g-top
          class: highend
          label: G Top
          note: Slow.
          price: {input: 2.00, output: 12.00}
          supports_temperature: false
tier_caps:
    free: {light: 20, middle: 10, highend: 0}
    pro: {light: 250, middle: 25, highend: 4}
    enterprise: unlimited
"""


def _load(text):
    with TemporaryDirectory() as d:
        p = Path(d) / "llm_models.yaml"
        p.write_text(text, encoding="utf-8")
        return load_llm_catalog(p)


class LoaderTests(SimpleTestCase):
    def test_builds_catalog_in_file_order(self):
        cat = _load(GOOD)
        self.assertEqual(
            [(e["provider"], e["model"]) for e in cat.catalog],
            [("gemini", "g-cheap"), ("gemini", "g-mid"), ("gemini", "g-top")],
        )
        self.assertEqual(cat.catalog[0]["label"], "G Cheap")

    def test_caps_are_derived_per_class_for_every_model(self):
        cat = _load(GOOD)
        self.assertEqual(
            cat.model_daily["free"], {"g-cheap": 20, "g-mid": 10, "g-top": 0}
        )
        self.assertEqual(cat.model_daily["pro"], {"g-cheap": 250, "g-mid": 25, "g-top": 4})

    def test_unlimited_tier_gets_no_entries(self):
        # `{}` is what the quota engine reads as "no per-model cap".
        self.assertNotIn("enterprise", _load(GOOD).model_daily)

    def test_per_model_override_beats_the_class_cap(self):
        cat = _load(GOOD.replace("          note: Balanced.", "          note: Balanced.\n          caps: {free: 0}"))
        self.assertEqual(cat.model_daily["free"]["g-mid"], 0)
        self.assertEqual(cat.model_daily["pro"]["g-mid"], 25, "override must be tier-scoped")

    def test_supports_temperature_defaults_true_and_opts_out(self):
        cat = _load(GOOD)
        self.assertTrue(cat.supports_temperature("g-cheap"))
        self.assertFalse(cat.supports_temperature("g-top"))
        # Unknown = an operator pinned a preview id via env; keep the
        # pre-existing send-it behaviour for them.
        self.assertTrue(cat.supports_temperature("some-preview-model"))


class LoaderRejectsTests(SimpleTestCase):
    def assertRejects(self, text, fragment):
        with self.assertRaises(CatalogError) as cm:
            _load(text)
        self.assertIn(fragment, str(cm.exception))

    def test_rejects_a_rung_listed_out_of_price_order(self):
        """`cheaper_models_same_provider()` treats list order AS cost
        order — it slices everything before the chosen model and calls
        it cheaper. Nothing else checks that, and inserting a new model
        in the wrong slot is the single easiest mistake to make in this
        file."""
        self.assertRejects(
            GOOD.replace("input: 0.30, output: 2.50", "input: 9.00, output: 9.00"),
            "must be ordered cheapest first",
        )

    def test_allows_equal_priced_capability_rungs(self):
        # Non-decreasing, not strictly increasing: claude-opus-4-7 and
        # -4-8 were exactly this — same price, different capability.
        cat = _load(GOOD.replace("input: 2.00, output: 12.00", "input: 1.50, output: 7.50"))
        self.assertEqual(len(cat.catalog), 3)

    def test_rejects_an_unknown_class(self):
        self.assertRejects(GOOD.replace("class: middle", "class: medium"), "unknown class")

    def test_rejects_a_tier_missing_a_class_cap(self):
        # Otherwise every model of that class silently has no cap.
        self.assertRejects(
            GOOD.replace("free: {light: 20, middle: 10, highend: 0}", "free: {light: 20}"),
            "missing a cap",
        )

    def test_rejects_an_override_for_a_tier_that_does_not_exist(self):
        # A tier typo in `caps:` would otherwise be a no-op — the model
        # would keep the class cap and the intended exception would
        # simply never apply.
        self.assertRejects(
            GOOD.replace("          note: Fast.", "          note: Fast.\n          caps: {fre: 0}"),
            "unknown tier",
        )

    def test_rejects_a_duplicate_model_id(self):
        self.assertRejects(GOOD.replace("model: g-mid", "model: g-cheap"), "duplicate model id")

    def test_rejects_a_missing_required_field(self):
        self.assertRejects(GOOD.replace("          label: G Mid\n", ""), "missing `label`")

    def test_rejects_a_model_with_no_price(self):
        self.assertRejects(
            GOOD.replace("          price: {input: 1.50, output: 7.50}\n", ""), "missing `price`"
        )

    def test_rejects_a_missing_file(self):
        self.assertRejects_path("/nonexistent/llm_models.yaml", "not found")

    def assertRejects_path(self, path, fragment):
        with self.assertRaises(CatalogError) as cm:
            load_llm_catalog(path)
        self.assertIn(fragment, str(cm.exception))

    def test_rejects_invalid_yaml(self):
        self.assertRejects("providers: [unclosed\n", "not valid YAML")


class ShippedCatalogTests(SimpleTestCase):
    """The real `apis/llm_models.yaml`, as loaded into settings."""

    def test_every_provider_offers_exactly_three_rungs(self):
        """The product promise: pick a provider, then pick one of three
        rungs — cheapest / middle / top.

        Asserted on COUNT, not on `class`. The user-facing rung is list
        POSITION; `class` is a cost bucket that only picks a daily cap.
        They diverge for Gemini, whose priciest model ($2/$12) is still
        mid-cost, so Gemini has no `highend` entry — classing it up for
        symmetry would meter it as if it cost JPY60/ask.
        """
        counts: dict[str, int] = {}
        for entry in settings.LLM_CATALOG.catalog:
            counts[entry["provider"]] = counts.get(entry["provider"], 0) + 1
        self.assertEqual(counts, {"gemini": 3, "claude": 3, "openai": 3})

    def test_the_no_preference_default_is_never_metered_below_the_umbrella(self):
        """The model that answers users who never touched the picker
        must not run out before `llm_ask_daily` does.

        Otherwise they 429 on the default path while the usage screen
        still shows asks remaining — and they have no idea a model
        choice exists, let alone that changing it would help. Only the
        ACTIVE server default is checked: `CLAUDE_MODEL` / `OPENAI_MODEL`
        answer users who deliberately chose an expensive provider, and
        metering those is the entire point of the per-model caps.
        """
        cfg = settings.SEARCH_ENGINE
        provider = cfg["LLM_PROVIDER"]
        default = cfg[{"claude": "CLAUDE_MODEL", "openai": "OPENAI_MODEL"}.get(provider, "GEMINI_MODEL")]
        for tier, caps in settings.LLM_CATALOG.model_daily.items():
            umbrella = cfg["TIER_QUOTAS"][tier]["llm_ask_daily"]
            if default not in caps or umbrella is None:
                continue
            self.assertGreaterEqual(
                caps[default],
                umbrella,
                f"server default {default!r} is capped at {caps[default]}/day on '{tier}' "
                f"but the tier allows {umbrella} asks — it would run out first",
            )

    def test_tier_quotas_cap_every_catalog_model(self):
        """Structurally guaranteed now, but keep the assertion: it is
        the invariant the whole YAML migration exists to protect, and
        TIER_QUOTAS_JSON can still full-replace the table at runtime."""
        catalog_models = {e["model"] for e in settings.SEARCH_ENGINE["MODEL_CATALOG"]}
        for tier, cfg in settings.SEARCH_ENGINE["TIER_QUOTAS"].items():
            if tier == "enterprise":
                continue
            self.assertEqual(
                catalog_models - set(cfg["model_daily"] or {}),
                set(),
                f"tier '{tier}' leaves some catalog models UNLIMITED",
            )

    def test_highend_caps_are_the_tightest_in_every_tier(self):
        """Worst-case-at-cap fills the day with the priciest allowed
        model, so the premium caps are the actual subsidy lever — a
        refresh that quietly relaxed them would widen the envelope
        without anyone re-running the cost model."""
        # Asserted on the DECLARED class caps, not on realized per-model
        # numbers: `caps:` overrides exist precisely to break class
        # uniformity (gpt-5.6-terra is 0 on free), and a legitimate
        # override should not have to fight a test. The invariant that
        # matters is the class ladder itself.
        for tier, caps in settings.LLM_CATALOG.tier_caps.items():
            self.assertLessEqual(caps["highend"], caps["middle"], tier)
            self.assertLessEqual(caps["middle"], caps["light"], tier)
