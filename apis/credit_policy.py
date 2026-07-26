"""Loader for `credit_policy.yaml` — the commercial layer's config.

Mirrors `llm_catalog.py`'s posture deliberately: boot-fatal validation
(a malformed commercial number must never survive to request time), and
DERIVED version identifiers rather than declared ones.

Two fingerprints, not one, because V2 §5.3 treats them as different
commercial acts:

  * `CreditPolicy.version`      — the conversion rules (`policy:`).
    Moving it means "what a request costs in credits changed".
  * `CreditPolicy.entitlement_version` — the plan grants
    (`entitlements:`). Moving it means "what a plan includes changed".

A ledger row stores both, so either kind of change separates old rows
from new ones on its own — the same argument as the rate card, and the
same failure it prevents: two regimes silently sharing one identifier
because an editor forgot to bump a string.

The dataclass is FROZEN and holds plain values only. The arithmetic
lives in `origin/search_engine/credits.py` as pure functions taking the
policy as an argument — never reading settings at call time — because
replaying stored requests under a CANDIDATE policy (dual calculation,
V2 §5.1) must be a function call, not a settings dance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml


class CreditPolicyError(RuntimeError):
    """Raised at boot for a missing or malformed credit_policy.yaml."""


_PLANS = ("free", "core", "pro", "max", "enterprise")


def _fail(msg: str) -> None:
    raise CreditPolicyError(f"credit_policy.yaml: {msg}")


@dataclass(frozen=True)
class CreditPolicy:
    """The commercial numbers, loaded once at boot.

    All credit quantities are MILLI-credits internally — fractional
    credits are a stated requirement (a cheap request must not round up
    to a whole credit), and storing the display unit is how rounding
    drift becomes permanent.
    """

    label: str
    credit_jpy: float
    request_max_credits_milli: int
    billable_surfaces: frozenset[str]
    excluded_purposes: frozenset[str]
    # plan -> monthly grant in milli-credits; None = unlimited
    # (enterprise: no entitlement accounting at all).
    entitlements_milli: dict[str, int | None]
    # plan -> internal monthly direct-AI ceiling in yen; None = none.
    monthly_ceiling_jpy: dict[str, float | None]
    fingerprint: str = ""
    entitlement_fingerprint: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def version(self) -> str:
        """`label+fingerprint` — what a ledger row stores."""
        return f"{self.label}+{self.fingerprint}"

    @property
    def entitlement_version(self) -> str:
        return f"{self.label}+{self.entitlement_fingerprint}"

    def request_max_jpy_milli(self) -> int:
        """The per-request eligible-cost cap, in milli-yen."""
        return int(round(self.request_max_credits_milli * self.credit_jpy))


def _parse_plan_map(raw: dict, key: str, *, scale: float) -> dict[str, float | None]:
    """Validate one plan->number map. `unlimited` -> None."""
    section = raw.get(key)
    if not isinstance(section, dict):
        _fail(f"`{key}` must be a mapping of plan -> number (or `unlimited`)")
    missing = set(_PLANS) - set(section)
    if missing:
        _fail(
            f"`{key}` is missing plan(s) {sorted(missing)}. Every plan must be "
            f"declared — an absent plan would fail nowhere and silently behave "
            f"as SOME default, and there is no safe default for a commercial number."
        )
    unknown = set(section) - set(_PLANS)
    if unknown:
        _fail(f"`{key}` names unknown plan(s) {sorted(unknown)}")
    out: dict[str, float | None] = {}
    for plan, value in section.items():
        if value == "unlimited":
            out[plan] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            _fail(f"{key}.{plan} must be a non-negative number or `unlimited`, got {value!r}")
        out[plan] = float(value) * scale
    return out


def load_credit_policy(path: str | Path) -> CreditPolicy:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        _fail(f"not found at {path}")
    except yaml.YAMLError as e:
        _fail(f"invalid YAML: {e}")
    if not isinstance(raw, dict):
        _fail("top level must be a mapping")

    policy = raw.get("policy")
    if not isinstance(policy, dict):
        _fail("`policy` must be a mapping")
    unknown = set(policy) - {
        "label",
        "credit_jpy",
        "request_max_credits",
        "billable_surfaces",
        "excluded_purposes",
    }
    if unknown:
        _fail(f"policy has unknown key(s) {sorted(unknown)}")

    label = policy.get("label")
    if not isinstance(label, str) or not label.strip():
        _fail("policy.label must be a non-empty string")

    credit_jpy = policy.get("credit_jpy")
    if isinstance(credit_jpy, bool) or not isinstance(credit_jpy, (int, float)) or credit_jpy <= 0:
        _fail(f"policy.credit_jpy must be a positive number, got {credit_jpy!r}")

    max_credits = policy.get("request_max_credits")
    if isinstance(max_credits, bool) or not isinstance(max_credits, (int, float)) or max_credits <= 0:
        _fail(f"policy.request_max_credits must be a positive number, got {max_credits!r}")

    surfaces = policy.get("billable_surfaces")
    if not isinstance(surfaces, list) or not surfaces:
        _fail("policy.billable_surfaces must be a non-empty list")
    for s in surfaces:
        if not isinstance(s, str) or not s.strip():
            _fail(f"policy.billable_surfaces entries must be non-empty strings, got {s!r}")

    purposes = policy.get("excluded_purposes")
    if purposes is None or not isinstance(purposes, list):
        _fail("policy.excluded_purposes must be a list (empty is fine — and a statement)")
    for p in purposes:
        if not isinstance(p, str) or not p.strip():
            _fail(f"policy.excluded_purposes entries must be non-empty strings, got {p!r}")

    entitlements = _parse_plan_map(raw, "entitlements", scale=1000.0)  # credits -> milli
    ceilings = _parse_plan_map(raw, "monthly_ceiling_jpy", scale=1.0)

    top_unknown = set(raw) - {"policy", "entitlements", "monthly_ceiling_jpy"}
    if top_unknown:
        _fail(f"unknown top-level key(s) {sorted(top_unknown)}")

    # --- fingerprints, derived never declared -------------------------
    policy_payload = {
        "credit_jpy": float(credit_jpy),
        "request_max_credits": float(max_credits),
        "billable_surfaces": sorted(surfaces),
        "excluded_purposes": sorted(purposes),
    }
    entitlement_payload = {
        plan: entitlements[plan] for plan in sorted(entitlements)
    }

    def _fp(payload) -> str:
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]

    return CreditPolicy(
        label=label.strip(),
        credit_jpy=float(credit_jpy),
        request_max_credits_milli=int(round(float(max_credits) * 1000)),
        billable_surfaces=frozenset(surfaces),
        excluded_purposes=frozenset(purposes),
        entitlements_milli={
            p: (int(v) if v is not None else None) for p, v in entitlements.items()
        },
        monthly_ceiling_jpy=ceilings,
        fingerprint=_fp(policy_payload),
        entitlement_fingerprint=_fp(entitlement_payload),
    )
