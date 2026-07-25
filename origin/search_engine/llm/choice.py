"""Per-request LLM choice resolution.

The factory in `origin.search_engine.llm.__init__` consults the
`ContextVar` set by `set_llm_choice()` to decide which provider +
model adapter to return for the current request. When unset (no
choice resolved yet, or a non-request code path), the factory falls
back to `settings.SEARCH_ENGINE["LLM_PROVIDER"]` / `GEMINI_MODEL` /
`CLAUDE_MODEL` / `OPENAI_MODEL`.

Threading note: `AgentAskView` runs the controller loop on a
`threading.Thread` (see `_stream_ndjson` in `agent_views.py`). Bare
threads do NOT inherit `ContextVar` values from their parent thread,
so callers must either `set_llm_choice()` from inside the worker
thread, or wrap the worker with `contextvars.copy_context().run(...)`.

Singleton note: the Gemini/Claude SDK clients are module-level
singletons keyed on server-wide credentials (API key / service
account). Only the model id is per-user; that flows through each
`generate_step(..., model_override=...)` call. A future change to
per-user *auth* (e.g. customer-supplied API keys) would also need
to unwind the singletons in `gemini_client.py` and `claude_client.py`.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from dataclasses import dataclass

from django.conf import settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class LlmChoice:
    """Provider + model id pair, normalized lowercase."""

    provider: str  # 'gemini' | 'claude' | 'openai'
    model: str  # e.g. 'gemini-2.5-pro' / 'claude-sonnet-4-6' / 'gpt-5.6-terra'


_current_choice: ContextVar[LlmChoice | None] = ContextVar("llm_choice", default=None)


def set_llm_choice(choice: LlmChoice | None):
    """Bind `choice` to the current context; returns the reset token.

    Always pair with `reset_llm_choice(token)` in a `finally:` block —
    sync Django reuses worker threads across requests, so a missing
    reset would leak this user's choice to the next request on the
    same worker.
    """
    return _current_choice.set(choice)


def reset_llm_choice(token) -> None:
    _current_choice.reset(token)


def get_llm_choice() -> LlmChoice | None:
    return _current_choice.get()


def _catalog_has(provider: str, model: str) -> bool:
    """True iff `(provider, model)` is in `SEARCH_ENGINE['MODEL_CATALOG']`."""
    catalog = settings.SEARCH_ENGINE.get("MODEL_CATALOG") or []
    return any(e.get("provider") == provider and e.get("model") == model for e in catalog)


def cheaper_models_same_provider(chosen: LlmChoice) -> list[str]:
    """Same-provider catalog models cheaper than `chosen`, NEAREST-first.

    Cost order is `MODEL_CATALOG` order: the catalog is curated
    cheap→expensive within each provider (flash→pro, haiku→sonnet→opus,
    luna→terra→sol), a contract already relied on by the frontend picker
    and the `catalog[0]` stale-preference fallback. So the models
    *before* `chosen` in its provider's slice are never more expensive.

    ⚠️ Non-decreasing, NOT strictly increasing: the slice may contain
    EQUAL-cost rungs. `claude-opus-4-7` and `claude-opus-4-8` are the
    same price per ask — 4-8 is a capability rung, not a cost one — so
    stepping down from 4-8 lands on 4-7 at the same cost, freeing quota
    without saving money. A step down preserves cost order; it does not
    guarantee a cheaper ask.

    Returned nearest-first (the rung just below `chosen` first), so a
    quota-fallback caller steps down one rung at a time and preserves as
    much of the user's chosen quality as headroom allows — a pro user
    who exhausts opus drops to sonnet, not all the way to haiku.

    Empty when `chosen` is already its provider's cheapest model, or when
    `chosen` isn't in the catalog at all (stale preference — the caller
    has already been resolved to a server default by then, but be safe).
    """
    catalog = settings.SEARCH_ENGINE.get("MODEL_CATALOG") or []
    same = [e["model"] for e in catalog if e.get("provider") == chosen.provider and e.get("model")]
    if chosen.model not in same:
        return []
    idx = same.index(chosen.model)
    return list(reversed(same[:idx]))


def _provider_default_choice(provider: str) -> LlmChoice:
    """That provider's server-configured default model.

    Deliberately NOT catalog-validated: `*_MODEL` is an operator escape
    hatch for pinning something the catalog doesn't list (a preview id,
    a region-specific build). `test_default_models_are_in_catalog`
    covers the committed defaults; an env override is on the operator.
    """
    cfg = settings.SEARCH_ENGINE
    if provider == "claude":
        return LlmChoice(provider="claude", model=cfg.get("CLAUDE_MODEL") or "")
    if provider == "openai":
        return LlmChoice(provider="openai", model=cfg.get("OPENAI_MODEL") or "")
    return LlmChoice(provider="gemini", model=cfg.get("GEMINI_MODEL") or "")


def _server_default_choice() -> LlmChoice:
    """The choice implied by env vars when no user preference applies."""
    provider = (settings.SEARCH_ENGINE.get("LLM_PROVIDER") or "gemini").lower()
    return _provider_default_choice(provider)


def resolve_user_choice(
    preferred_provider: str | None,
    preferred_model: str | None,
) -> LlmChoice:
    """Pick the effective `LlmChoice` for a user.

    - Both fields blank → server default.
    - Provider UNKNOWN → server default + warning.
    - Provider known but model blank or stale → that PROVIDER's default
      model. A stale model is the routine outcome of a catalog refresh,
      and it must not change which provider answers the user.
    """
    provider = (preferred_provider or "").lower().strip()
    model = (preferred_model or "").strip()

    if not provider and not model:
        return _server_default_choice()

    if provider not in ("gemini", "claude", "openai"):
        log.warning(
            "User has unknown preferred_llm_provider=%r; falling back to server default",
            preferred_provider,
        )
        return _server_default_choice()

    if not model or not _catalog_has(provider, model):
        if model:
            # STALE MODEL, VALID PROVIDER — the common case after a
            # catalog swap, which now happens routinely (models are
            # managed in apis/llm_models.yaml precisely so they can be
            # refreshed often). Keep the user on the provider they
            # chose: falling through to `_server_default_choice()` would
            # silently move every Claude and GPT user onto Gemini,
            # because that is the server default. The picker's own
            # same-provider fallback only fixes what's *displayed*, so
            # the mismatch would be invisible — their asks would just
            # quietly change model AND provider.
            log.info(
                "User preference (%s, %s) is no longer in MODEL_CATALOG; "
                "falling back to that provider's default",
                provider,
                model,
            )
        return _provider_default_choice(provider)

    return LlmChoice(provider=provider, model=model)


def _provider_default_choice(provider: str) -> LlmChoice:
    """That provider's server-configured default model."""
    cfg = settings.SEARCH_ENGINE
    if provider == "claude":
        return LlmChoice(provider="claude", model=cfg.get("CLAUDE_MODEL") or "")
    if provider == "openai":
        return LlmChoice(provider="openai", model=cfg.get("OPENAI_MODEL") or "")
    return LlmChoice(provider="gemini", model=cfg.get("GEMINI_MODEL") or "")
