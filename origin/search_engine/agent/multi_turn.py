"""Phase 3.5 — multi-turn context preparation.

Shared between the production /ask/ view and the eval runner so both
paths exercise the same prior-turn truncation / summarisation logic.

The decision tree, given `all_turns` (full session history of
(query, answer) pairs) and the `max_verbatim` window:

  * `rolling_summary == False` OR `len(all_turns) <= max_verbatim`:
    pass through the last `max_verbatim` turns verbatim, no summary.
    This is the current pre-3.5 behavior; flag-off keeps it exactly.

  * `rolling_summary == True` AND `len(all_turns) > max_verbatim`:
    keep the last `max_verbatim` turns verbatim AND summarise the
    earlier turns into a single short paragraph that the controller
    prepends to the messages list.

The summary is built INCREMENTALLY, which is the whole point of the
`prior_summary` / `summarised_through` arguments.

It used to be rebuilt from the entire aged-out history on every turn:
turn N paid to summarise turns 1..N-3, turn N+1 paid again for
1..N-2 — near-identical work whose PROMPT GREW WITH THE CONVERSATION.
That is quadratic in a session's length, and it is the "known internal
waste" the metering strategy says must not be billed to a customer.

Now each turn folds only the turns that have newly aged out into the
summary we already had, so the summary call's prompt is
`summary + 1 turn` no matter how long the conversation is:

    turns   from-scratch prompt   incremental prompt
      5           2 turns              summary + 1
     10           7 turns              summary + 1
     20          17 turns              summary + 1

The state lives on `AgentSession` in production and in a local variable
in the eval runner. Deliberately: the ALGORITHM is here, shared, and
only the storage differs — passing the state in rather than reaching
for a session is what keeps the two callers from diverging.

Correctness rests on `summarised_through` — how many of the oldest
turns the text already covers. Without it there is no way to tell which
turns are already represented, and a fold would either re-summarise
everything or double-count the overlap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.conf import settings

from origin.search_engine.llm import AgentMessage, get_model_client
from origin.search_engine.llm.spend import spend_purpose

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PriorContext:
    """What the caller prepends, plus the summary state to carry forward.

    A dataclass rather than a tuple because the caller has to persist
    two of these three fields, and `a, b, c = ...` positional unpacking
    is exactly how the wrong one gets stored.
    """

    verbatim: list[tuple[str, str]]
    summary: str | None
    #: How many of the oldest turns `summary` covers. Store alongside
    #: the text; a summary without it cannot be safely extended.
    summarised_through: int = 0


_SUMMARY_SYSTEM = """\
You are summarising a multi-turn conversation between a user and an AI
workspace assistant. The summary will be re-injected as context for the
NEXT turn so the assistant can resolve references to earlier topics.

Produce ONE short paragraph (<= 120 words). Capture:
- TOPICS the user asked about (one phrase each).
- KEY ENTITIES mentioned by name (project names, task names, person
  names) — verbatim, so later turns can match them.
- The conversation ARC (e.g. "started with X, then narrowed to Y").

Skip greetings, meta-commentary, and the assistant's prose. Just the
substantive context. Write it as a third-person factual recap, not as
dialogue (do not say "the user said …" — just state the topics).
"""

_SUMMARY_USER_TEMPLATE = """\
Conversation so far ({n_turns} turns, oldest first):

{convo}

Now write the summary paragraph.
"""

_FOLD_SYSTEM = """\
You are maintaining a running summary of a conversation between a user
and an AI workspace assistant. The summary is re-injected as context for
the NEXT turn so the assistant can resolve references to earlier topics.

You are given the summary so far and the turns that have just dropped
out of the verbatim window. Return the UPDATED summary — one short
paragraph (<= 120 words) covering both.

Rules:
- KEEP key entities named in the existing summary (project names, task
  names, person names) unless the new turns make them irrelevant. They
  are the reason the summary exists.
- Fold the new turns in; do not append them as a separate list.
- Preserve the conversation ARC across the whole span.
- Stay within the word limit by compressing the OLDEST material first.

Output the paragraph only — no preamble, no headings.
"""

_FOLD_USER_TEMPLATE = """\
Summary so far (covers the first {n_covered} turn(s)):

{summary}

Turns that have just aged out of the verbatim window ({n_new}, oldest
first):

{convo}

Now write the updated summary paragraph.
"""

# Hard cap on how much of each prior turn's text we feed into the
# summary call — defends against runaway prompt size when a prior
# answer is long-form markdown.
_PER_TURN_TEXT_CAP = 600


def build_prior_context(
    all_turns: list[tuple[str, str]],
    *,
    rolling_summary: bool | None = None,
    max_verbatim: int | None = None,
    model_override: str | None = None,
    prior_summary: str = "",
    summarised_through: int = 0,
) -> PriorContext:
    """Decide which prior turns travel verbatim and (optionally) extend
    the rolling summary of the rest.

    Args:
        all_turns: Full session history of (query, answer) pairs,
            chronologically oldest first.
        rolling_summary: Force the summary path on/off. Default None
            reads the `RAG_SESSION_ROLLING_SUMMARY` setting (so callers
            don't have to plumb it through).
        max_verbatim: Override the verbatim window. Default None reads
            `SESSION_MAX_PRIOR_TURNS` (so callers stay in sync with
            production).
        prior_summary: The summary carried over from the last turn.
            Empty means "none yet" — the first summarising turn builds
            from scratch, and every turn after that folds.
        summarised_through: How many of the oldest turns `prior_summary`
            already covers. MUST travel with the text; a summary of
            unknown span cannot be extended without double-counting.

    Returns a `PriorContext`. Callers that persist state store its
    `summary` and `summarised_through` together.

    Never raises — a summary is best-effort context, and losing it must
    degrade the answer, not fail the request.
    """
    if rolling_summary is None:
        rolling_summary = bool(settings.SEARCH_ENGINE.get("RAG_SESSION_ROLLING_SUMMARY", False))
    if max_verbatim is None:
        max_verbatim = int(settings.SEARCH_ENGINE.get("SESSION_MAX_PRIOR_TURNS", 3))

    if not all_turns:
        return PriorContext([], None, 0)

    verbatim = all_turns[-max_verbatim:]
    # How many of the oldest turns should be represented by a summary.
    needed = len(all_turns) - max_verbatim
    if not rolling_summary or needed <= 0:
        return PriorContext(verbatim, None, 0)

    carried = (prior_summary or "").strip()
    covered = max(int(summarised_through), 0)

    # Nothing new aged out — reuse verbatim and make NO llm call. Happens
    # on a retried turn, and on any caller that asks twice for the same
    # history.
    if carried and covered == needed:
        return PriorContext(verbatim, carried, covered)

    if carried and 0 < covered < needed:
        # The common path: fold in only what newly aged out, so the
        # prompt stays `summary + a turn or two` however long the
        # conversation gets.
        summary = _fold_summary(
            carried,
            all_turns[covered:needed],
            n_covered=covered,
            model_override=model_override,
        )
    else:
        # First summarising turn, or a carried summary we cannot trust
        # to be extensible (covered == 0, or somehow ahead of `needed`
        # after a history rewrite).
        summary = _summarise_turns(all_turns[:needed], model_override=model_override)

    if summary is None:
        # The call failed. Keep the summary we already had rather than
        # dropping to none: a stale summary of turns 1..N-4 is strictly
        # better context than nothing, and `covered` keeps it honest
        # about what it represents so the next turn folds the right
        # remainder in.
        return PriorContext(verbatim, carried or None, covered)

    return PriorContext(verbatim, summary, needed)


def _render_turns(turns: list[tuple[str, str]], *, start: int = 1) -> str:
    """Q/A lines for the summary prompt, each side length-capped.

    `start` numbers the turns by their real position in the conversation
    so a folded batch reads as "Q8/Q9", not "Q1/Q2" — the model is being
    told these come AFTER what the carried summary covers.
    """
    lines: list[str] = []
    for i, (q, a) in enumerate(turns, start=start):
        q_text = (q or "").strip()
        a_text = (a or "").strip()
        if len(q_text) > _PER_TURN_TEXT_CAP:
            q_text = q_text[: _PER_TURN_TEXT_CAP - 1] + "…"
        if len(a_text) > _PER_TURN_TEXT_CAP:
            a_text = a_text[: _PER_TURN_TEXT_CAP - 1] + "…"
        lines.append(f"Q{i}: {q_text}\nA{i}: {a_text}")
    return "\n\n".join(lines)


def _generate(system: str, user_prompt: str, *, model_override: str | None) -> str | None:
    """One tool-less LLM call returning joined text, or None on failure.

    Never raises: the summary is best-effort context. A failure here
    must degrade the answer, never break the agent loop.
    """
    try:
        client = get_model_client()
        chunks: list[str] = []
        for text, _fcall in client.generate_step(
            messages=[AgentMessage(role="user", text=user_prompt)],
            tools=[],
            system_instruction=system,
            model_override=model_override,
        ):
            if text:
                chunks.append(text)
        out = "".join(chunks).strip()
        return out or None
    except Exception:  # noqa: BLE001 — never break the agent loop on summary failure
        log.exception("Rolling-summary LLM call failed")
        return None


@spend_purpose("summary")
def _summarise_turns(
    turns: list[tuple[str, str]], *, model_override: str | None = None
) -> str | None:
    """Summarise `turns` from scratch. Only the FIRST summarising turn of
    a session should reach this — every turn after it folds instead.
    """
    if not turns:
        return None
    return _generate(
        _SUMMARY_SYSTEM,
        _SUMMARY_USER_TEMPLATE.format(n_turns=len(turns), convo=_render_turns(turns)),
        model_override=model_override,
    )


@spend_purpose("summary")
def _fold_summary(
    carried: str,
    new_turns: list[tuple[str, str]],
    *,
    n_covered: int,
    model_override: str | None = None,
) -> str | None:
    """Extend an existing summary with the turns that just aged out.

    The whole point: this prompt is `summary + the newly aged-out turns`
    — one or two of them in practice — where the from-scratch call it
    replaces grew by one turn on every turn of the conversation.

    Same `spend_purpose("summary")` tag as the from-scratch call, so the
    cost report keeps measuring the two as one line and the saving shows
    up there rather than hiding behind a renamed bucket.
    """
    if not new_turns:
        return carried or None
    return _generate(
        _FOLD_SYSTEM,
        _FOLD_USER_TEMPLATE.format(
            n_covered=n_covered,
            summary=carried,
            n_new=len(new_turns),
            convo=_render_turns(new_turns, start=n_covered + 1),
        ),
        model_override=model_override,
    )
