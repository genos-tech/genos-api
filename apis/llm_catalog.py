"""Loads `llm_models.yaml` into the shapes `settings.SEARCH_ENGINE` needs.

Imported from `settings.py` at module level, so it must not import
Django — it runs before settings exist.

Everything here fails LOUD. A malformed catalog raises at boot rather
than degrading, for the same reason `TIER_QUOTAS_JSON` does: the
failure modes are all silent-and-expensive. A model with no cap is
UNLIMITED, not zero; a mis-ordered rung makes quota fallback step
*up* in price; a tier name typo uncaps that whole tier. None of those
surface as errors at request time — they surface on the invoice.

The two derived shapes:

    catalog       -> SEARCH_ENGINE["MODEL_CATALOG"]
                     [{"provider", "model", "label", "note"}, ...]
                     ordered cheap→expensive within each provider.

    model_daily   -> SEARCH_ENGINE["TIER_QUOTAS"][tier]["model_daily"]
                     {tier: {model: cap}}; {} for an unlimited tier.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Class names, cheapest first. Also the ordering used to sanity-check
# that a provider's rungs don't jump around.
CLASSES = ("light", "middle", "highend")

# Marker for a tier that gets no per-model caps at all.
UNLIMITED = "unlimited"

_REQUIRED_FIELDS = ("model", "class", "label", "note", "price")


class CatalogError(Exception):
    """The catalog file is unusable. Raised at import time."""


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
    )
