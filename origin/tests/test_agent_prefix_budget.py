"""What every model call pays for, held to a number (MIP-881).

The system prompt and the tool declarations go out on every model call in
every run. On 2026-11-02 one day's new tools added 15,400 characters, about
3.6k tokens, to that prefix, and nothing noticed, because no test can fail on
"a bit more".

This is the thing that notices. It is not a claim that the number is right.
The routing eval found no link between prefix size and latency between 23k and
27k tokens, and the prefix is 10x cheaper cached, so it is a price rather than
a wall. The point is that paying more becomes a decision: run
`manage.py agent_eval_routing`, and raise the number in the same PR.

About 4.2 characters to a token, measured against Gemini's own count.
"""

from __future__ import annotations

import json

from django.test import SimpleTestCase

from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT

# 112,091 when this was set: about 26.5k tokens by Gemini's own count.
BUDGET_CHARS = 115_000


def _declared_chars() -> int:
    return sum(
        len(d.name) + len(d.description) + len(json.dumps(d.parameters_schema, ensure_ascii=False))
        for d in _build_tool_declarations()
    )


class PrefixBudgetTests(SimpleTestCase):
    def test_the_prefix_stays_inside_its_budget(self) -> None:
        total = _declared_chars() + len(AGENT_SYSTEM_PROMPT)
        self.assertLessEqual(
            total,
            BUDGET_CHARS,
            f"The prompt and tool declarations are {total} characters, over the budget "
            f"of {BUDGET_CHARS}. Trim, or run `manage.py agent_eval_routing` to show the "
            "addition earns its place and raise BUDGET_CHARS in this PR.",
        )

    def test_the_count_covers_every_declared_tool(self) -> None:
        # A budget that silently measured nothing would always pass.
        self.assertGreater(_declared_chars(), 50_000)
