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
"""

from __future__ import annotations

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
