"""The versioned credit policy — V2 layers 3 and 4.

What must hold, in rough order of expense-if-wrong:

  1. The arithmetic is PURE. Same stored inputs + same policy object ->
     same output, forever. That is the entire basis for dual-policy
     comparison (V2 §5.1) and for the immutable-charge rule (§3.6).
  2. Exclusions fail customer-favorably: an unknown surface bills 0, a
     non-success result bills 0, cost above the cap is ours.
  3. The two fingerprints move independently — a conversion change and
     an entitlement change are different commercial acts, and a ledger
     row records which of each regime it was posted under.
  4. The loader is boot-fatal. There is no safe default for a
     commercial number.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from django.conf import settings as dj_settings
from django.test import SimpleTestCase, TestCase, override_settings

from apis.credit_policy import CreditPolicy, CreditPolicyError, load_credit_policy
from origin.search_engine import credits, spend_recorder
from origin.search_engine.llm import spend
from origin.search_engine.llm.types import CallUsage
from origin.search_engine.models import AiRequestCost


def _se(**overrides):
    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


METER_ON = override_settings(SEARCH_ENGINE=_se(AI_COST_METER=True))

GOOD = """
policy:
    label: "cp-test"
    credit_jpy: 15
    request_max_credits: 5
    billable_surfaces: [ask, thread_summary, note_summary]
    billable_results: [success, credits_exhausted]
    excluded_purposes: []
entitlements:
    free: 10
    core: 40
    pro: 100
    max: 200
    enterprise: unlimited
monthly_ceiling_jpy:
    free: 150
    core: 600
    pro: 1500
    max: 3000
    enterprise: unlimited
"""


def _load(text):
    with TemporaryDirectory() as d:
        p = Path(d) / "credit_policy.yaml"
        p.write_text(text, encoding="utf-8")
        return load_credit_policy(p)


def _policy(**overrides) -> CreditPolicy:
    """A hand-built policy for the pure-function tests, so they read as
    tables rather than YAML surgery."""
    base = dict(
        label="t",
        credit_jpy=15.0,
        request_max_credits_milli=5000,
        billable_surfaces=frozenset({"ask", "thread_summary", "note_summary"}),
        billable_results=frozenset({"success", "credits_exhausted"}),
        excluded_purposes=frozenset(),
        entitlements_milli={"free": 10_000},
        monthly_ceiling_jpy={"free": 150.0},
        fingerprint="f",
        entitlement_fingerprint="e",
    )
    base.update(overrides)
    return CreditPolicy(**base)


class PolicyLoaderTests(SimpleTestCase):
    def test_good_fixture_parses(self):
        p = _load(GOOD)
        self.assertEqual(p.credit_jpy, 15.0)
        self.assertEqual(p.request_max_credits_milli, 5000)
        self.assertEqual(p.request_max_jpy_milli(), 75_000)  # 5 credits x ¥15
        self.assertEqual(p.entitlements_milli["pro"], 100_000)
        self.assertIsNone(p.entitlements_milli["enterprise"], "unlimited -> None")
        self.assertEqual(p.monthly_ceiling_jpy["max"], 3000.0)
        self.assertIsNone(p.monthly_ceiling_jpy["enterprise"])

    def test_a_missing_plan_refuses_to_boot(self):
        """An absent plan would fail nowhere and behave as SOME default,
        and there is no safe default for a commercial number."""
        with self.assertRaises(CreditPolicyError) as cm:
            _load(GOOD.replace("    core: 40\n", ""))
        self.assertIn("core", str(cm.exception))

    def test_an_unknown_plan_refuses_to_boot(self):
        with self.assertRaises(CreditPolicyError):
            _load(GOOD.replace("    pro: 100", "    pro: 100\n    platinum: 999"))

    def test_a_negative_number_refuses_to_boot(self):
        with self.assertRaises(CreditPolicyError):
            _load(GOOD.replace("credit_jpy: 15", "credit_jpy: -15"))
        with self.assertRaises(CreditPolicyError):
            _load(GOOD.replace("free: 10", "free: -10"))

    def test_unknown_keys_refuse_to_boot(self):
        with self.assertRaises(CreditPolicyError):
            _load(GOOD.replace("credit_jpy: 15", "credit_jpy: 15\n    credit_usd: 0.1"))
        with self.assertRaises(CreditPolicyError):
            _load(GOOD + "topup_packs: {}\n")

    def test_empty_billable_surfaces_refuses_to_boot(self):
        """All-zero billing configured by accident should not boot."""
        with self.assertRaises(CreditPolicyError):
            _load(
                GOOD.replace(
                    "billable_surfaces: [ask, thread_summary, note_summary]",
                    "billable_surfaces: []",
                )
            )

    def test_a_missing_file_refuses_to_boot(self):
        with self.assertRaises(CreditPolicyError):
            load_credit_policy("/nonexistent/credit_policy.yaml")


class PolicyFingerprintTests(SimpleTestCase):
    """The two fingerprints move independently — §5.3's distinction
    between a conversion change and an entitlement change, made
    mechanical."""

    def test_conversion_edits_move_the_policy_fingerprint_only(self):
        base = _load(GOOD)
        for edit in (
            ("credit_jpy: 15", "credit_jpy: 12"),
            ("request_max_credits: 5", "request_max_credits: 8"),
            ("excluded_purposes: []", "excluded_purposes: [summary]"),
            (
                "billable_surfaces: [ask, thread_summary, note_summary]",
                "billable_surfaces: [ask]",
            ),
        ):
            moved = _load(GOOD.replace(*edit))
            self.assertNotEqual(base.fingerprint, moved.fingerprint, edit)
            self.assertEqual(
                base.entitlement_fingerprint,
                moved.entitlement_fingerprint,
                f"{edit}: a conversion edit must not move the entitlement version",
            )

    def test_entitlement_edits_move_the_entitlement_fingerprint_only(self):
        base = _load(GOOD)
        moved = _load(GOOD.replace("pro: 100", "pro: 120"))
        self.assertNotEqual(base.entitlement_fingerprint, moved.entitlement_fingerprint)
        self.assertEqual(
            base.fingerprint,
            moved.fingerprint,
            "an entitlement edit must not move the conversion version",
        )

    def test_a_label_edit_moves_neither(self):
        base = _load(GOOD)
        moved = _load(GOOD.replace('label: "cp-test"', 'label: "cp-renamed"'))
        self.assertEqual(base.fingerprint, moved.fingerprint)
        self.assertEqual(base.entitlement_fingerprint, moved.entitlement_fingerprint)
        self.assertNotEqual(base.version, moved.version, "the label still shows in the version")


class ShippedPolicyTests(SimpleTestCase):
    """The checked-in credit_policy.yaml is the V2 §1 configuration."""

    def setUp(self):
        self.policy = dj_settings.CREDIT_POLICY

    def test_the_v2_scale_is_configured(self):
        self.assertEqual(self.policy.credit_jpy, 15.0)
        self.assertEqual(
            {p: v for p, v in self.policy.entitlements_milli.items() if v is not None},
            {"free": 10_000, "core": 40_000, "pro": 100_000, "max": 200_000},
        )
        self.assertEqual(
            {p: v for p, v in self.policy.monthly_ceiling_jpy.items() if v is not None},
            {"free": 150.0, "core": 600.0, "pro": 1500.0, "max": 3000.0},
        )

    def test_the_billable_surfaces_are_exactly_the_user_charged_ones(self):
        self.assertEqual(
            self.policy.billable_surfaces,
            frozenset({"ask", "thread_summary", "note_summary"}),
            "search is measured but deliberately not billable; index/eval/"
            "judge are our own cost",
        )

    def test_result_success_pin_matches_the_model(self):
        """credits.py must stay importable without Django, so it pins
        the literal; this is the check that the pin cannot drift."""
        self.assertEqual(credits.RESULT_SUCCESS, AiRequestCost.RESULT_SUCCESS)


class EligibleCostTests(SimpleTestCase):
    """Layer 3 — every exclusion fails customer-favorably."""

    def test_a_billable_success_is_its_computed_cost(self):
        self.assertEqual(
            credits.eligible_jpy_milli(
                result="success", surface="ask", computed_jpy_milli=12_345, policy=_policy()
            ),
            12_345,
        )

    def test_a_non_billable_surface_is_zero(self):
        for surface in ("search", "index", "eval", "judge", "unattributed", "brand-new"):
            self.assertEqual(
                credits.eligible_jpy_milli(
                    result="success", surface=surface, computed_jpy_milli=9999, policy=_policy()
                ),
                0,
                f"{surface}: an unlisted surface must exclude itself",
            )

    def test_every_non_success_result_is_zero(self):
        for result in (
            AiRequestCost.RESULT_USER_CANCELLATION,
            AiRequestCost.RESULT_PROVIDER_FAILURE,
            AiRequestCost.RESULT_APPLICATION_FAILURE,
            AiRequestCost.RESULT_SAFETY_REFUSAL,
            "",
        ):
            self.assertEqual(
                credits.eligible_jpy_milli(
                    result=result, surface="ask", computed_jpy_milli=9999, policy=_policy()
                ),
                0,
                result,
            )

    def test_cost_above_the_cap_is_absorbed(self):
        p = _policy()  # cap = 5 credits x ¥15 = 75_000 milli-yen
        self.assertEqual(
            credits.eligible_jpy_milli(
                result="success", surface="ask", computed_jpy_milli=999_999, policy=p
            ),
            75_000,
        )

    def test_negative_computed_clamps_to_zero(self):
        self.assertEqual(
            credits.eligible_jpy_milli(
                result="success", surface="ask", computed_jpy_milli=-5, policy=_policy()
            ),
            0,
        )


class CreditsConversionTests(SimpleTestCase):
    """Layer 4 — milli in, milli out, ¥15 per credit."""

    def test_fifteen_yen_is_one_credit(self):
        self.assertEqual(credits.credits_milli(15_000, _policy()), 1000)

    def test_fractional_credits_survive(self):
        # A ¥1.50 request is 0.1 credit, not rounded up to 1.
        self.assertEqual(credits.credits_milli(1_500, _policy()), 100)
        # A ¥3 request at ¥15/credit: 0.2 credits.
        self.assertEqual(credits.credits_milli(3_000, _policy()), 200)

    def test_purity_same_inputs_same_output(self):
        p = _policy()
        results = {credits.credits_milli(7_777, p) for _ in range(50)}
        self.assertEqual(len(results), 1)

    def test_a_candidate_policy_replays_without_touching_settings(self):
        """The dual-calculation contract: the same stored input under a
        DIFFERENT policy object is just another function call."""
        live = _policy()
        candidate = _policy(credit_jpy=10.0, fingerprint="f2")
        self.assertEqual(credits.credits_milli(30_000, live), 2000)
        self.assertEqual(credits.credits_milli(30_000, candidate), 3000)

    def test_quote_is_the_flat_policy_cap_for_now(self):
        self.assertEqual(credits.quote_max_credits_milli(_policy()), 5000)


class _RestoresRecorder:
    def setUp(self):
        super().setUp()
        original = spend._recorder
        self.addCleanup(spend.set_recorder, original)


class RollupCreditFieldsTests(_RestoresRecorder, TestCase):
    """`close_request` stamps eligible cost, policy-derived shadow
    credits, and BOTH version identifiers onto the rollup."""

    def _run_request(self, *, surface="ask", result=AiRequestCost.RESULT_SUCCESS, tokens=1_000_000):
        rid = str(uuid.uuid4())
        with spend.spend_context(surface=surface, user_id="u1", request_id=rid) as ctx:
            usage = CallUsage(provider="gemini", model="gemini-3.6-flash")
            usage.prompt_tokens = tokens
            spend.record_llm_call(usage)
            spend_recorder.close_request(ctx, result=result)
        return AiRequestCost.objects.get(request_id=rid)

    @METER_ON
    def test_a_billable_success_carries_eligible_and_credits(self):
        row = self._run_request()
        policy = dj_settings.CREDIT_POLICY
        self.assertGreater(row.computed_jpy_milli, 0)
        expected_eligible = credits.eligible_jpy_milli(
            result="success",
            surface="ask",
            computed_jpy_milli=row.computed_jpy_milli,
            policy=policy,
        )
        self.assertEqual(row.eligible_jpy_milli, expected_eligible)
        self.assertEqual(
            row.shadow_credits_milli, credits.credits_milli(expected_eligible, policy)
        )
        self.assertEqual(row.credit_policy_version, policy.version)
        self.assertEqual(row.plan_entitlement_version, policy.entitlement_version)

    @METER_ON
    def test_a_non_billable_surface_rolls_up_with_zero_eligible(self):
        row = self._run_request(surface="search")
        self.assertGreater(row.computed_jpy_milli, 0, "the COST is still real and recorded")
        self.assertEqual(row.eligible_jpy_milli, 0)
        self.assertEqual(row.shadow_credits_milli, 0)

    @METER_ON
    def test_a_failed_request_rolls_up_with_zero_eligible(self):
        row = self._run_request(result=AiRequestCost.RESULT_PROVIDER_FAILURE)
        self.assertEqual(row.eligible_jpy_milli, 0)
        self.assertEqual(row.shadow_credits_milli, 0)

    @METER_ON
    def test_eligible_is_capped_at_the_per_request_max(self):
        # 60M prompt tokens on the default model ≈ ¥13,500 — far over
        # the 5-credit (¥75) cap.
        row = self._run_request(tokens=60_000_000)
        policy = dj_settings.CREDIT_POLICY
        self.assertEqual(row.eligible_jpy_milli, policy.request_max_jpy_milli())
        self.assertEqual(
            row.shadow_credits_milli, policy.request_max_credits_milli,
            "at the cap, the charge IS the quote ceiling",
        )
