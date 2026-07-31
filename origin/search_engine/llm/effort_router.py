"""Adaptive effort — the "auto" router (UX tier model §5.3).

Today effort is a config chore: the user picks Low/Medium/High in
Settings. The premium experience is Genos choosing per question — a
trivial lookup runs low, "why is this blocked and what should we do"
runs high. A saved `preferred_llm_effort == "auto"` routes each ask
through ONE rung-0 classification call, whose single-word answer picks
the effort; `resolve_user_effort`'s tier ceiling then clamps it.

Contracts:
  * FAIL-OPEN to "medium" — any router error, timeout, or off-script
    output resolves to the balanced rung. The user asked for "Genos
    decides"; a broken decider must degrade to a sensible decision,
    never to an error.
  * Cheapest possible call: the provider's rung-0 model, no tools, a
    tight output cap. Measure the cost in an A/B before flipping
    `AGENT_AUTO_EFFORT` (AGENT_COST_OPTIMIZATION.md has the method).
  * Spend attribution: callers invoke this INSIDE the ask's spend
    context; `@spend_purpose("effort_router")` labels the rows.
"""

from __future__ import annotations

import logging

from django.conf import settings

from apis.llm_catalog import EFFORTS

from . import AgentMessage, get_model_client
from .spend import spend_purpose
from .types import GenerationParams

log = logging.getLogger(__name__)

AUTO_EFFORT = "auto"

_ROUTER_SYSTEM = """\
You classify how much reasoning effort a workspace-assistant request
needs. Reply with EXACTLY one word — low, medium, or high.

low    — a lookup or single-fact question: find/show/list one thing,
         a status check, a simple summary of one item.
medium — normal multi-step work: search + synthesize, summarize
         several items, draft content, a straightforward create/update.
high   — analysis or planning: "why", trade-offs, cross-referencing
         many items, reorganizing or planning work, anything where a
         wrong answer is expensive.

No punctuation, no explanation. One word."""


@spend_purpose("effort_router")
def route_effort(query: str, provider: str) -> str:
    """Classify `query` into an effort level via one rung-0 call.

    `provider` is the user's resolved provider — the router stays
    same-provider (like every subprocess pin) so a Claude user's
    classification can never be handed to the Gemini adapter.
    """
    try:
        rung0 = settings.LLM_CATALOG.model_for_effort(provider, "low")
        client = get_model_client()
        chunks: list[str] = []
        for text, _fcall in client.generate_step(
            messages=[AgentMessage(role="user", text=query[:2000])],
            tools=[],
            system_instruction=_ROUTER_SYSTEM,
            model_override=rung0,
            params=GenerationParams(max_output_tokens=8),
        ):
            if text:
                chunks.append(text)
        verdict = "".join(chunks).strip().lower().rstrip(".")
        if verdict in EFFORTS:
            return verdict
        log.warning("effort router returned %r; falling back to medium", verdict)
        return "medium"
    except Exception:  # noqa: BLE001
        log.exception("effort router failed; falling back to medium")
        return "medium"
