"""Loads `llm_models.yaml` into the shapes `settings.SEARCH_ENGINE` needs.

Imported from `settings.py` at module level, so it must not import
Django — it runs before settings exist.

Everything here fails LOUD. A malformed catalog raises at boot rather
than degrading, for the same reason `TIER_QUOTAS_JSON` does: the
failure modes are all silent-and-expensive. A model with no cap is
UNLIMITED, not zero; a mis-ordered rung makes quota fallback step
*up* in price; a tier name typo uncaps that whole tier. None of those
surface as errors at request time — they surface on the invoice.

The derived shapes:

    catalog       -> SEARCH_ENGINE["MODEL_CATALOG"]
                     [{"provider", "model", "label", "note"}, ...]
                     ordered cheap→expensive within each provider.

    model_daily   -> SEARCH_ENGINE["TIER_QUOTAS"][tier]["model_daily"]
                     {tier: {model: cap}}; {} for an unlimited tier.

    efforts / subprocess_rungs -> consumed via settings.LLM_CATALOG by
                     the effort-level resolution in llm/choice.py
                     (AGENT_EFFORT_LEVELS). `rung` values are LIST
                     INDICES into a provider's cheapest-first models.

    prices / rate_card -> the ONLY price source for cost accounting,
                     read via settings.LLM_CATALOG. `price_for()` is
                     exact-id; `rate_card.version` is what a cost row
                     stores so a later price change can never restate
                     a past request.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Class names, cheapest first. Also the ordering used to sanity-check
# that a provider's rungs don't jump around.
CLASSES = ("light", "middle", "highend")

# Effort levels, cheapest first. Order matters: it is both the
# monotonicity axis for profile validation and the ladder
# `effort_for_model` collapses a legacy saved model onto.
EFFORTS = ("low", "medium", "high")

# Marker for a tier that gets no per-model caps at all.
UNLIMITED = "unlimited"

_REQUIRED_FIELDS = ("model", "class", "label", "note", "price")

# Every rate a billable token can be charged at. All four are required
# on every model: an absent cache rate has no safe default (assume
# input and you over-bill the cached path ~10x, assume 0 and you
# under-bill it), and neither error is visible in the output. Write 0
# explicitly for "this provider does not bill this line".
_PRICE_FIELDS = ("input", "output", "cached_input", "cache_write")

_EFFORT_FIELDS = (
    "rung",
    "max_steps",
    "rewrite_variants",
    "use_reranker",
    "critique_steps",
    "max_output_tokens",
)

# Sub-process kinds that may carry a rung pin. All three required in
# the YAML: a missing kind would silently mean "inherit the synthesis
# model", which is exactly the accidental-Opus-reranker behavior the
# pins exist to end.
SUBPROCESS_KINDS = ("rewrite", "rerank", "summaries")


class CatalogError(Exception):
    """The catalog file is unusable. Raised at import time."""


@dataclass(frozen=True)
class EffortProfile:
    """One effort level's loop parameters + synthesis-model rung.

    `rung` is a LIST INDEX into the provider's cheapest-first model
    list, resolved per provider at request time by `model_for_effort`
    (clamped, so a 2-model provider still has a `high`). See the
    MEDIUM INVARIANT note in llm_models.yaml before editing medium.
    """

    name: str
    rung: int
    max_steps: int
    rewrite_variants: int
    use_reranker: bool
    critique_steps: int
    max_output_tokens: int | None


@dataclass(frozen=True)
class ModelPrice:
    """One model's USD-per-1M-token rates.

    Mirrors the four billable lines the providers actually meter, and
    the four `CallUsage` buckets the cost meter sums. `cached_input` is
    the discounted re-read of a prompt prefix (~10% of `input` on all
    three providers today); `cache_write` is Anthropic's cache_creation
    surcharge (~125% of `input`) and is a real 0 for Gemini's implicit
    cache and OpenAI's automatic cache, which bill no write line.
    """

    input: float
    output: float
    cached_input: float
    cache_write: float


@dataclass(frozen=True)
class RateCard:
    """The money metadata stamped onto every cost row.

    `fingerprint` is DERIVED from every price plus `fx_jpy_per_usd`,
    not declared — it is the grouping key for cost reports, and the
    failure it exists to prevent is two different price regimes sharing
    one identifier because someone edited a price and forgot to bump a
    version string. `label` is the human name and carries no such duty.

    `fx_jpy_per_usd` is pinned, never looked up live: historical totals
    that drift with today's exchange rate cannot be reconciled against
    an invoice.
    """

    label: str
    fx_jpy_per_usd: float
    fingerprint: str

    @property
    def version(self) -> str:
        """`label+fingerprint` — what a ledger row should store."""
        return f"{self.label}+{self.fingerprint}"


@dataclass(frozen=True)
class LlmCatalog:
    catalog: list[dict[str, str]]
    model_daily: dict[str, dict[str, int]]
    # {tier: {class: cap}} as declared, BEFORE per-model `caps:`
    # overrides are applied. This is the design intent; `model_daily` is
    # the intent plus its deliberate exceptions.
    tier_caps: dict[str, dict[str, int]] = field(default_factory=dict)
    # model id -> the raw YAML entry, for adapters that need a
    # capability flag (see `supports_temperature`).
    by_model: dict[str, dict[str, Any]] = field(default_factory=dict)
    # {"low"|"medium"|"high": EffortProfile}
    efforts: dict[str, EffortProfile] = field(default_factory=dict)
    # {"rewrite"|"rerank"|"summaries": rung index}
    subprocess_rungs: dict[str, int] = field(default_factory=dict)
    # model id -> ModelPrice, exact-id keyed. See `price_for`.
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    # The money metadata every cost row is stamped with.
    rate_card: RateCard | None = None
    # provider -> ordered (cheapest-first) model ids; derived once at
    # load so request-time lookups never re-scan the catalog list.
    _provider_models: dict[str, list[str]] = field(default_factory=dict)

    def provider_models(self, provider: str) -> list[str]:
        """The provider's model ids, cheapest first. [] for unknown."""
        return list(self._provider_models.get(provider, ()))

    def provider_order(self) -> list[str]:
        """Provider names in YAML declaration order (stable for UIs)."""
        return list(self._provider_models)

    def model_for_effort(self, provider: str, effort: str) -> str:
        """The synthesis model for (provider, effort).

        Clamps the profile's rung to the provider's list length, so a
        provider with fewer rungs than efforts still resolves (its top
        model serves both medium and high). Raises for an unknown
        provider or effort — callers validate both first, and a silent
        empty string here would propagate into an SDK 404.
        """
        profile = self.efforts.get(effort)
        if profile is None:
            raise CatalogError(f"unknown effort {effort!r}")
        return self.model_for_rung(provider, profile.rung)

    def model_for_rung(self, provider: str, rung: int) -> str:
        """The provider's model at `rung`, clamped to the list end."""
        models = self._provider_models.get(provider)
        if not models:
            raise CatalogError(f"unknown provider {provider!r}")
        return models[min(rung, len(models) - 1)]

    def effort_for_model(self, provider: str, model: str) -> str | None:
        """The effort a LEGACY saved model maps onto, or None.

        List index collapsed onto the effort ladder (index 3+ would
        only exist if a provider grew a fourth rung — it maps to high).
        None when the model isn't in the provider's list (stale saved
        preference); the caller falls back to the default effort.
        """
        models = self._provider_models.get(provider, ())
        if model not in models:
            return None
        return EFFORTS[min(models.index(model), len(EFFORTS) - 1)]

    def price_for(self, model: str) -> ModelPrice | None:
        """This model's rates, or None if it isn't in the catalog.

        EXACT id match, never a prefix. The offline aggregator used to
        do longest-prefix `str.startswith` against a hand-maintained
        sheet, and the result was that `"gemini-3.6-flash"` did not
        match `"gemini-3-flash"` (dot vs hyphen) — so in production,
        where Gemini is the default provider, EVERY Gemini and every
        GPT call priced as unknown and silently contributed 0 to the
        cost report. A prefix scheme fails soft and looks plausible;
        exact match fails loud and is checked by a test that walks the
        whole catalog.

        None is a real answer, not an error: an operator can pin a
        preview model id via `GEMINI_MODEL` that was never added here.
        Callers must record that as `unpriced` rather than 0 — an
        unpriced call dropped from a total is how a whole provider
        goes missing from a bill that still looks about right.
        """
        return self.prices.get(model)

    def supports_temperature(self, model: str) -> bool:
        """False for models whose API rejects `temperature`.

        Defaults to True for an UNKNOWN model: a model outside the
        catalog is almost always an operator pinning a preview id via
        env, and the old behaviour (send it) is the safer default to
        preserve for them. Catalog entries opt out explicitly.
        """
        entry = self.by_model.get(model)
        if entry is None:
            return True
        return bool(entry.get("supports_temperature", True))


def _fail(msg: str) -> None:
    raise CatalogError(f"llm_models.yaml: {msg}")


def _parse_price(provider: str, entry: dict[str, Any]) -> ModelPrice:
    """Validate one model's four rates. Boot-fatal on anything missing.

    Deliberately strict about types: YAML happily reads `0.30` as a
    float and `"0.30"` as a string, and a string rate would multiply
    into a `TypeError` deep inside the cost meter at request time
    rather than here at boot.
    """
    model = entry.get("model")
    price = entry.get("price")
    if not isinstance(price, dict):
        _fail(f"{provider}/{model!r}: `price` must be a mapping")
    unknown = set(price) - set(_PRICE_FIELDS)
    if unknown:
        _fail(f"{provider}/{model!r}: unknown price key(s) {sorted(unknown)}")
    values: dict[str, float] = {}
    for key in _PRICE_FIELDS:
        if key not in price:
            _fail(
                f"{provider}/{model!r}: price is missing `{key}`. All four rates are "
                f"required — an absent cache rate has no safe default (assume `input` "
                f"and the cached path over-bills ~10x; assume 0 and it under-bills), "
                f"and neither shows up in the output. Write 0 explicitly if this "
                f"provider does not bill that line."
            )
        v = price[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            _fail(f"{provider}/{model!r}: price.{key} must be a number, got {v!r}")
        if v < 0:
            _fail(f"{provider}/{model!r}: price.{key} must not be negative, got {v!r}")
        values[key] = float(v)
    return ModelPrice(**values)


def _parse_rate_card(raw: dict, prices: dict[str, ModelPrice]) -> RateCard:
    """Build the rate card, fingerprinting every price + the FX rate.

    The fingerprint is computed rather than declared so that editing a
    price cannot leave two different price regimes sharing one
    identifier in the ledger. Sorted keys keep it stable across YAML
    reorderings — only an actual number moving changes it.
    """
    rc = raw.get("rate_card")
    if not isinstance(rc, dict):
        _fail("`rate_card` must be a mapping with `label` and `fx_jpy_per_usd`")
    unknown = set(rc) - {"label", "fx_jpy_per_usd"}
    if unknown:
        _fail(f"rate_card has unknown key(s) {sorted(unknown)}")
    label = rc.get("label")
    if not isinstance(label, str) or not label.strip():
        _fail("rate_card.label must be a non-empty string")
    fx = rc.get("fx_jpy_per_usd")
    if isinstance(fx, bool) or not isinstance(fx, (int, float)) or fx <= 0:
        _fail(f"rate_card.fx_jpy_per_usd must be a positive number, got {fx!r}")

    payload = {
        "fx": float(fx),
        "prices": {
            model: [p.input, p.output, p.cached_input, p.cache_write]
            for model, p in sorted(prices.items())
        },
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]
    return RateCard(label=label.strip(), fx_jpy_per_usd=float(fx), fingerprint=fingerprint)


def _check_price_order(provider: str, entries: list[dict[str, Any]]) -> None:
    """Assert each provider's list really is cheap→expensive.

    `cheaper_models_same_provider()` treats list order AS cost order —
    it slices everything before the chosen model and calls it cheaper.
    Nothing else verifies that, and this is a file people hand-edit
    under time pressure when a provider ships something new. An
    inserted-in-the-wrong-slot rung would make the quota-fallback path
    step UP in price while reporting a step down.

    Non-decreasing, not strictly increasing: equal-priced capability
    rungs are legitimate (claude-opus-4-7 and -4-8 were exactly that).
    """
    prev = None
    for entry in entries:
        price = entry.get("price") or {}
        try:
            here = (float(price["input"]), float(price["output"]))
        except (KeyError, TypeError, ValueError):
            _fail(f"{provider}/{entry.get('model')!r}: price needs numeric input + output")
        if prev is not None and (here[0] < prev[1][0] or here[1] < prev[1][1]):
            _fail(
                f"{provider}: {entry['model']!r} (${here[0]}/${here[1]}) is CHEAPER than "
                f"{prev[0]!r} (${prev[1][0]}/${prev[1][1]}) but is listed after it. "
                f"Each provider's list must be ordered cheapest first — list order is "
                f"the cost order the quota-fallback path walks."
            )
        prev = (entry["model"], here)


def load_llm_catalog(path: str | Path) -> LlmCatalog:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CatalogError(f"llm_models.yaml not found at {path}")
    except yaml.YAMLError as e:
        raise CatalogError(f"llm_models.yaml is not valid YAML: {e}")
    if not isinstance(raw, dict):
        _fail("top level must be a mapping")

    providers = raw.get("providers")
    if not isinstance(providers, dict) or not providers:
        _fail("`providers` must be a non-empty mapping")

    tier_caps_raw = raw.get("tier_caps")
    if not isinstance(tier_caps_raw, dict) or not tier_caps_raw:
        _fail("`tier_caps` must be a non-empty mapping")

    # --- tier caps -----------------------------------------------------
    tier_caps: dict[str, dict[str, int] | None] = {}
    for tier, caps in tier_caps_raw.items():
        if caps == UNLIMITED:
            tier_caps[tier] = None
            continue
        if not isinstance(caps, dict):
            _fail(f"tier_caps.{tier} must be a mapping or the literal {UNLIMITED!r}")
        missing = set(CLASSES) - set(caps)
        if missing:
            _fail(f"tier_caps.{tier} is missing a cap for {sorted(missing)}")
        unknown = set(caps) - set(CLASSES)
        if unknown:
            _fail(f"tier_caps.{tier} has unknown class {sorted(unknown)}; expected {CLASSES}")
        for klass, cap in caps.items():
            if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
                _fail(f"tier_caps.{tier}.{klass} must be a non-negative integer, got {cap!r}")
        tier_caps[tier] = dict(caps)

    # --- models --------------------------------------------------------
    catalog: list[dict[str, str]] = []
    by_model: dict[str, dict[str, Any]] = {}
    prices: dict[str, ModelPrice] = {}
    model_daily: dict[str, dict[str, int]] = {t: {} for t, c in tier_caps.items() if c is not None}

    for provider, entries in providers.items():
        if not isinstance(entries, list) or not entries:
            _fail(f"providers.{provider} must be a non-empty list")
        for entry in entries:
            if not isinstance(entry, dict):
                _fail(f"providers.{provider} entries must be mappings")
            for key in _REQUIRED_FIELDS:
                if not entry.get(key):
                    _fail(f"providers.{provider}: an entry is missing `{key}`")
            model = entry["model"]
            if model in by_model:
                _fail(f"duplicate model id {model!r}")
            klass = entry["class"]
            if klass not in CLASSES:
                _fail(f"{model!r}: unknown class {klass!r}; expected one of {CLASSES}")

            overrides = entry.get("caps") or {}
            if not isinstance(overrides, dict):
                _fail(f"{model!r}: `caps` must be a mapping of tier -> integer")
            unknown_tiers = set(overrides) - set(tier_caps)
            if unknown_tiers:
                _fail(f"{model!r}: `caps` names unknown tier(s) {sorted(unknown_tiers)}")

            by_model[model] = entry
            prices[model] = _parse_price(provider, entry)
            catalog.append(
                {
                    "provider": provider,
                    "model": model,
                    "label": entry["label"],
                    "note": entry["note"],
                }
            )
            for tier, caps in tier_caps.items():
                if caps is None:
                    continue
                cap = overrides.get(tier, caps[klass])
                if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
                    _fail(f"{model!r}: caps.{tier} must be a non-negative integer, got {cap!r}")
                model_daily[tier][model] = cap

        _check_price_order(provider, entries)

    return LlmCatalog(
        catalog=catalog,
        model_daily=model_daily,
        by_model=by_model,
        tier_caps={t: c for t, c in tier_caps.items() if c is not None},
        efforts=_parse_efforts(raw),
        subprocess_rungs=_parse_subprocesses(raw),
        prices=prices,
        rate_card=_parse_rate_card(raw, prices),
        _provider_models={
            p: [e["model"] for e in entries] for p, entries in providers.items()
        },
    )


def _parse_efforts(raw: dict) -> dict[str, EffortProfile]:
    """Validate + build the three effort profiles. All failures boot-fatal.

    The monotonicity asserts mirror `_check_price_order`'s rationale:
    this file is hand-edited under time pressure, and the failure mode
    of a swapped value is silent — "high" quietly doing less work than
    "low" never raises at request time, it just answers worse.
    """
    efforts_raw = raw.get("efforts")
    if not isinstance(efforts_raw, dict):
        _fail("`efforts` must be a mapping of low/medium/high")
    if set(efforts_raw) != set(EFFORTS):
        _fail(f"`efforts` must define exactly {EFFORTS}, got {sorted(efforts_raw)}")

    profiles: dict[str, EffortProfile] = {}
    for name in EFFORTS:
        p = efforts_raw[name]
        if not isinstance(p, dict):
            _fail(f"efforts.{name} must be a mapping")
        unknown = set(p) - set(_EFFORT_FIELDS)
        if unknown:
            _fail(f"efforts.{name} has unknown key(s) {sorted(unknown)}")
        missing = set(_EFFORT_FIELDS) - set(p)
        if missing:
            _fail(f"efforts.{name} is missing {sorted(missing)}")
        for key in ("rung", "max_steps", "rewrite_variants", "critique_steps"):
            v = p[key]
            if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                _fail(f"efforts.{name}.{key} must be a non-negative integer, got {v!r}")
        if p["max_steps"] < 1:
            _fail(f"efforts.{name}.max_steps must be >= 1")
        if p["rung"] > len(EFFORTS) - 1:
            _fail(f"efforts.{name}.rung must be 0-{len(EFFORTS) - 1}, got {p['rung']}")
        if not isinstance(p["use_reranker"], bool):
            _fail(f"efforts.{name}.use_reranker must be a boolean")
        cap = p["max_output_tokens"]
        if cap is not None and (not isinstance(cap, int) or isinstance(cap, bool) or cap < 1):
            _fail(f"efforts.{name}.max_output_tokens must be a positive integer or null")
        profiles[name] = EffortProfile(
            name=name,
            rung=p["rung"],
            max_steps=p["max_steps"],
            rewrite_variants=p["rewrite_variants"],
            use_reranker=p["use_reranker"],
            critique_steps=p["critique_steps"],
            max_output_tokens=cap,
        )

    for key in ("rung", "max_steps", "rewrite_variants"):
        values = [getattr(profiles[name], key) for name in EFFORTS]
        if values != sorted(values):
            _fail(
                f"`{key}` must be non-decreasing low -> medium -> high, got {values} — "
                f"a mis-edit must never make a higher effort do less work"
            )
    return profiles


def _parse_subprocesses(raw: dict) -> dict[str, int]:
    """Validate the sub-process rung pins. All three kinds required."""
    sub_raw = raw.get("subprocesses")
    if not isinstance(sub_raw, dict):
        _fail("`subprocesses` must be a mapping")
    if set(sub_raw) != set(SUBPROCESS_KINDS):
        _fail(
            f"`subprocesses` must define exactly {SUBPROCESS_KINDS}, got {sorted(sub_raw)} — "
            f"a missing kind silently means 'inherit the user's synthesis model'"
        )
    for kind, rung in sub_raw.items():
        if not isinstance(rung, int) or isinstance(rung, bool) or rung < 0:
            _fail(f"subprocesses.{kind} must be a non-negative integer rung, got {rung!r}")
    return dict(sub_raw)
