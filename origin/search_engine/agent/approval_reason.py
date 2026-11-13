"""The reason a person gives when they reject an approval card (MIP-889).

`POST /decide/` takes an optional `reason` with `decision: "reject"`. It is stored on the
step (`AgentStep.decision_reason`) whatever the flag says. What the model sees of it is
decided here, behind `SEARCH_ENGINE["AGENT_REJECT_REASON"]` (default off):

  * off   the model's result is `{"error": "user_rejected"}`, exactly as before;
  * on    `{"error": "user_rejected", "reason": "<their words>"}`. The `error` key stays
          exact (the prompt's rule 7, the UI and the evals match it).

The reason is the PERSON'S OWN words, typed on their own card, so it is not wrapped as
workspace content (that is for text other people wrote). It is still capped and stripped of
control characters on the way in AND on the way to the model, so a row written by any other
path cannot hand the model more than that.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

MAX_CHARS = 500

# Tab / newline / carriage return / line and paragraph separators become a space, so one
# line stays one line; every other C0 and C1 control character, DEL, and the bidirectional
# overrides are dropped.
_WHITESPACE_CONTROLS = re.compile(r"[\t\n\r  ]+")
_CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f‪-‮⁦-⁩]")


def clean(text: Any) -> str:
    """The reason as it may be stored or shown: text only, controls gone, trimmed, capped."""
    if not isinstance(text, str):
        return ""
    text = _WHITESPACE_CONTROLS.sub(" ", text)
    text = _CONTROLS.sub("", text)
    return text.strip()[:MAX_CHARS]


def enabled() -> bool:
    return bool(settings.SEARCH_ENGINE.get("AGENT_REJECT_REASON", False))


def rejection_result(stored_reason: Any) -> dict[str, Any]:
    """The function response the model gets for a rejected call."""
    result: dict[str, Any] = {"error": "user_rejected"}
    if enabled():
        reason = clean(stored_reason)
        if reason:
            result["reason"] = reason
    return result
