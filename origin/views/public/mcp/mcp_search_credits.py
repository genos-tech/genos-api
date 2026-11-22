"""AI credits for MCP `search_workspace` (MIP-1965).

A search by a connected AI app runs two paid model calls (query rewrite and rerank). They are
metered under the billable surface `mcp_search`, so the credit engine charges the caller's AI
credits exactly like an in-app question (same ledger, `AiRequestCost.surface` tells them apart).
When the caller has nothing left the search still answers, as a plain search with no model call,
and says so in one line. It is never refused for credits.

Every failure here answers "has credits": a ledger hiccup must not degrade a paying user.
"""

from __future__ import annotations

import logging

from origin.search_engine import credit_ledger, quota
from origin.search_engine.agent.tools.search_kb import PLAIN_SEARCH_NOTICE

log = logging.getLogger(__name__)

MCP_SEARCH_SURFACE = "mcp_search"
SEARCH_TOOL = "search_workspace"
NOTICE = PLAIN_SEARCH_NOTICE


def out_of_credits(user_id: str) -> bool:
    """True only when credits are enforced and this person's balance is spent.

    Unlimited plans (balance None) and shadow-only deployments are never out of credits.
    """
    try:
        if not credit_ledger.credits_authoritative():
            return False
        uid = str(user_id)
        balance = credit_ledger.balance_milli(uid, quota.get_effective_tier(uid))
        return balance is not None and balance <= 0
    except Exception:  # noqa: BLE001
        log.debug("mcp search credit check failed", exc_info=True)
        return False
