"""What a run costs, per effort level, before anyone makes a real call (MIP-1210).

AN ESTIMATE, labelled one wherever it is shown. No model is called to make it:

* the INPUT size is the size of the REAL prompt `prompt.build_user_message` produces for a
  realistic report (`typical_tasks`: 10 tasks with 3 comments and 2 changes each, the
  comment lengths a team really writes), plus the fixed system prompt, counted in characters and
  turned into tokens at 3.0 to 4.5 characters per token (Gemini's tokenizer is not available
  offline; Japanese text is nearer 1 to 2 characters per token, so that is a floor, see the PR);
* the OUTPUT size is what the answer can hold: up to `MAX_SENTENCES` sentences of at most
  `SENTENCE_CHARS` characters, as JSON, plus the thinking budget when the level has one;
* the PRICE is the catalog's (`apis/llm_models.yaml`), the one the cost ledger prices with.

"Per run" is a run that asks the model about every task (a cold cache). A cached task costs
nothing, so a typical run is cheaper.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from origin.services.report_ai import effort as effort_mod
from origin.services.report_ai import prompt, sentences, validate
from origin.services.report_ai.context import TaskContext

CHARS_PER_TOKEN_LOW, CHARS_PER_TOKEN_HIGH = 4.5, 3.0  # fewest and most tokens
RUNS_PER_MONTH = 30  # a daily report


def typical_tasks(n: int = sentences.MAX_TASKS) -> list[TaskContext]:
    comment = (
        "2026-11-07 Aiko Tanaka: "
        "<workspace_content>\nStill waiting on the vendor's reply about the API quota, I asked "
        "again this morning and they said Monday. Until then the export job cannot be tested "
        "against the real account.\n</workspace_content>"
    )
    event = (
        "2026-11-07: status Open to Blocked by <workspace_content>\nKen Sato\n</workspace_content>"
    )
    return [
        TaskContext(
            task_id=i,
            display_id=f"MIP-{100 + i}",
            title="Export the monthly usage report to CSV for the finance team",
            status="Blocked",
            due_date="2026-11-12",
            comments=[comment] * 3,
            events=[event] * 2,
            fingerprint=str(i),
        )
        for i in range(1, n + 1)
    ]


def prompt_chars(n_tasks: int = sentences.MAX_TASKS) -> int:
    refs = [(f"t{i}", t) for i, t in enumerate(typical_tasks(n_tasks), 1)]
    return len(prompt.SYSTEM) + len(prompt.build_user_message(refs, "2026-11-08"))


def _tokens(chars: int) -> tuple[int, int]:
    return round(chars / CHARS_PER_TOKEN_LOW), round(chars / CHARS_PER_TOKEN_HIGH)  # fewest, most


def estimate(level: str, n_tasks: int = sentences.MAX_TASKS) -> dict[str, Any] | None:
    """`{effort, model, input_tokens: [low, high], output_tokens: [low, high], usd_micro: [low,
    high], credits_milli: [low, high], estimate: True}` for one run at `level`, or None when the
    catalog has no price for the level's model."""
    run = effort_mod.resolve(level, level, "", clamp=False)
    if run is None:
        return None
    price = settings.LLM_CATALOG.price_for(run.model)
    if price is None:
        return None
    n_sentences = min(sentences.MAX_SENTENCES, n_tasks)
    answer_chars = n_tasks * 8 + n_sentences * validate.SENTENCE_CHARS
    in_low, in_high = _tokens(prompt_chars(n_tasks))
    out_low, out_high = (
        round(answer_chars / CHARS_PER_TOKEN_LOW) + 20,
        round(answer_chars / CHARS_PER_TOKEN_HIGH) + 20 + run.thinking_budget,
    )
    price = price.for_prompt(in_high)
    # tokens x USD per 1M tokens = micro-USD
    usd_low = round(in_low * price.input + out_low * price.output)
    usd_high = round(in_high * price.input + out_high * price.output)
    policy = getattr(settings, "CREDIT_POLICY", None)
    credits = _credits(usd_low, usd_high, policy)
    return {
        "effort": run.effort,
        "model": run.model,
        "input_tokens": [in_low, in_high],
        "output_tokens": [out_low, out_high],
        "usd_micro": [usd_low, usd_high],
        "credits_milli": credits,
        "estimate": True,
    }


def _credits(usd_low: int, usd_high: int, policy: Any) -> list[int] | None:
    if policy is None:
        return None
    from origin.search_engine import credits  # noqa: PLC0415

    return [credits.credits_milli(usd_low, policy), credits.credits_milli(usd_high, policy)]
