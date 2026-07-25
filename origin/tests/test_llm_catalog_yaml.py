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
efforts:
    low:    {rung: 0, max_steps: 6,  rewrite_variants: 1, use_reranker: true, critique_steps: 1, max_output_tokens: 4096}
    medium: {rung: 1, max_steps: 10, rewrite_variants: 3, use_reranker: true, critique_steps: 2, max_output_tokens: null}
    high:   {rung: 2, max_steps: 10, rewrite_variants: 3, use_reranker: true, critique_steps: 2, max_output_tokens: null}
subprocesses:
    rewrite: 0
    rerank: 0
    summaries: 0
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


class EffortProfileTests(SimpleTestCase):
    """The `efforts` + `subprocesses` sections (effort-level plumbing)."""

    def test_profiles_parse_with_all_fields(self):
        cat = _load(GOOD)
        self.assertEqual(set(cat.efforts), {"low", "medium", "high"})
        low = cat.efforts["low"]
        self.assertEqual(
            (low.rung, low.max_steps, low.rewrite_variants, low.max_output_tokens),
            (0, 6, 1, 4096),
        )
        self.assertIsNone(cat.efforts["medium"].max_output_tokens)
        self.assertEqual(cat.subprocess_rungs, {"rewrite": 0, "rerank": 0, "summaries": 0})

    def test_model_for_effort_indexes_the_provider_list(self):
        cat = _load(GOOD)
        self.assertEqual(cat.model_for_effort("gemini", "low"), "g-cheap")
        self.assertEqual(cat.model_for_effort("gemini", "medium"), "g-mid")
        self.assertEqual(cat.model_for_effort("gemini", "high"), "g-top")

    def test_rung_clamps_for_a_short_provider_list(self):
        """A 2-model provider still resolves every effort — its top
        model serves both medium and high. Without the clamp, adding a
        lean provider would 500 every high-effort ask."""
        two_model = GOOD.replace(
            """        - model: g-top
          class: highend
          label: G Top
          note: Slow.
          price: {input: 2.00, output: 12.00}
          supports_temperature: false
""",
            "",
        )
        cat = _load(two_model)
        self.assertEqual(cat.model_for_effort("gemini", "high"), "g-mid")
        self.assertEqual(cat.model_for_rung("gemini", 5), "g-mid")

    def test_effort_for_model_maps_legacy_saved_models(self):
        """The read-time migration: a saved model becomes its rung's
        effort, so nobody's provider or model changes at the flip."""
        cat = _load(GOOD)
        self.assertEqual(cat.effort_for_model("gemini", "g-cheap"), "low")
        self.assertEqual(cat.effort_for_model("gemini", "g-mid"), "medium")
        self.assertEqual(cat.effort_for_model("gemini", "g-top"), "high")
        self.assertIsNone(cat.effort_for_model("gemini", "g-retired"))
        self.assertIsNone(cat.effort_for_model("nope", "g-cheap"))

    def test_unknown_provider_raises_not_empty_string(self):
        # An empty model id would propagate into an SDK 404 mid-request.
        cat = _load(GOOD)
        with self.assertRaises(CatalogError):
            cat.model_for_effort("mistral", "low")


class EffortRejectsTests(SimpleTestCase):
    def assertRejects(self, text, fragment):
        with self.assertRaises(CatalogError) as cm:
            _load(text)
        self.assertIn(fragment, str(cm.exception))

    def test_rejects_a_missing_effort(self):
        self.assertRejects(
            GOOD.replace(
                "    high:   {rung: 2, max_steps: 10, rewrite_variants: 3, "
                "use_reranker: true, critique_steps: 2, max_output_tokens: null}\n",
                "",
            ),
            "exactly",
        )

    def test_rejects_an_unknown_profile_key(self):
        self.assertRejects(
            GOOD.replace("max_output_tokens: 4096", "max_output_tokens: 4096, temprature: 1"),
            "unknown key",
        )

    def test_rejects_a_missing_profile_field(self):
        self.assertRejects(
            GOOD.replace("rung: 0, max_steps: 6, ", "rung: 0, "), "missing"
        )

    def test_rejects_non_monotonic_efforts(self):
        """'High' must never do less work than 'low' — the swapped-value
        mis-edit answers worse forever and never raises at request time."""
        self.assertRejects(
            GOOD.replace(
                "    low:    {rung: 0, max_steps: 6, ", "    low:    {rung: 0, max_steps: 12, "
            ),
            "non-decreasing",
        )
        self.assertRejects(
            GOOD.replace("medium: {rung: 1,", "medium: {rung: 0,").replace(
                "    low:    {rung: 0,", "    low:    {rung: 1,"
            ),
            "non-decreasing",
        )

    def test_rejects_a_missing_subprocess_kind(self):
        # A missing kind silently means "inherit the synthesis model" —
        # the exact accidental behavior the pins exist to end.
        self.assertRejects(GOOD.replace("    rerank: 0\n", ""), "exactly")

    def test_rejects_a_bool_rung(self):
        # YAML `true` is a Python bool, which is an int subclass — the
        # guard must not accept it as rung 1.
        self.assertRejects(GOOD.replace("rewrite: 0", "rewrite: true"), "non-negative")


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

    def test_MEDIUM_INVARIANT_medium_effort_is_todays_default_experience(self):
        """THE proof that flipping AGENT_EFFORT_LEVELS is a no-op for
        every user who never touched the picker.

        Today's default experience = the GEMINI_MODEL server default +
        the env loop params. The medium profile must equal all of them,
        byte for byte. If EITHER side moves — a YAML edit to medium, or
        an env-default change in settings.py — this fails CI, forcing
        whoever moved it to acknowledge they are changing the DEFAULT
        experience and re-run the eval gates.

        (Runs against CI's default env. A stale local docker/.env pin
        of GEMINI_MODEL fails here the same way it already fails
        test_default_models_are_in_catalog — fix the pin, not the test.)
        """
        cfg = settings.SEARCH_ENGINE
        medium = settings.LLM_CATALOG.efforts["medium"]
        provider = cfg["LLM_PROVIDER"]
        default_model = {
            "claude": cfg["CLAUDE_MODEL"],
            "openai": cfg["OPENAI_MODEL"],
        }.get(provider, cfg["GEMINI_MODEL"])
        self.assertEqual(
            settings.LLM_CATALOG.model_for_effort(provider, "medium"),
            default_model,
            "medium's rung no longer resolves to the server default model",
        )
        self.assertEqual(medium.max_steps, int(cfg["AGENT_MAX_STEPS"]))
        self.assertEqual(medium.rewrite_variants, int(cfg["RAG_REWRITE_NUM_VARIANTS"]))
        self.assertEqual(medium.use_reranker, bool(cfg["RAG_USE_RERANKER"]))
        self.assertEqual(medium.critique_steps, int(cfg["RAG_CRITIQUE_MAX_STEPS"]))
        self.assertIsNone(
            medium.max_output_tokens,
            "medium must keep provider-default output behavior (no new cap)",
        )

    def test_subprocess_pins_resolve_for_every_provider(self):
        """Every (provider, kind) pin must resolve to a real model —
        the pins are same-provider by construction, so this is the
        whole cross-provider-safety story."""
        cat = settings.LLM_CATALOG
        providers = {e["provider"] for e in cat.catalog}
        for provider in providers:
            models = set(cat.provider_models(provider))
            for kind, rung in cat.subprocess_rungs.items():
                self.assertIn(cat.model_for_rung(provider, rung), models, f"{provider}/{kind}")

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
