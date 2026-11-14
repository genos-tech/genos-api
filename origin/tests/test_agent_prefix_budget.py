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
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.prompts import AGENT_SYSTEM_PROMPT

# 112,091 when this was set: about 26.5k tokens by Gemini's own count. Raised to
# 118,000 for MIP-880's three tools (115,095 measured, the same headroom as
# before), and to 122,000 for MIP-882's sprint and project tools (119,270 measured
# with the project tools), and to 126,000 for MIP-883's note folder, favorite and
# delete tools (123,487 measured), and to 130,000 for the note sharing, history and
# restore tools (127,860 measured), and to 133,000 for the rest of MIP-882's project
# tools (129,821 measured with the tag and label tools, 130,993 with the join-request
# ones), and to 137,000 for MIP-1424's `list_my_unread` (134,315 measured: the tool is 1,339
# characters, about the size of `list_my_inbox`, 1,230, and `list_my_chats`, 1,470), each time
# with the routing cases for them in both languages, as the rule above asks. To 138,500 for the
# ten flags MIP-1089 turned on by default (137,391 measured: +3,076 over the 134,315 of the same
# tree with every flag off, all of it in tool descriptions and one prompt rule; the grouped eval of
# 2026-11-14 measured each flag and the all-on arm, see the hub comment on MIP-1089).
# To 140,000 for `get_timeline` published to the in-app agent (138,866 measured: +1,475 characters,
# one declaration), flipped on by default after the t1281 cases were re-read (2026-11-14).
# To 142,000 for the parity batch's parameters (MIP-1561, AGENT_PARITY_TOOLS, default OFF): +1,898
# characters over `create_task`, `update_task` and `create_task_plan` (140,836 measured with it on
# over the shipped flags, 140,982 with EVERY flag on, the arm the grouped run measures; the later
# parity tasks add to the same flag and raise this again). The routing numbers come from that one
# grouped run, not from this PR: the flag ships off, so production sends what it sent (138,938).
BUDGET_CHARS = 142_000


def _declared_chars() -> int:
    return sum(
        len(d.name) + len(d.description) + len(json.dumps(d.parameters_schema, ensure_ascii=False))
        for d in _build_tool_declarations()
    )


class PrefixBudgetTests(SimpleTestCase):
    def test_the_prefix_stays_inside_its_budget(self) -> None:
        # What PRODUCTION sends: the test settings keep the flags MIP-1089 turned on off, so lay the
        # production defaults over them (`apis.settings` keeps them).
        from apis import settings as production  # noqa: PLC0415

        with mock.patch.dict(settings.SEARCH_ENGINE, production.SEARCH_ENGINE):
            total = _declared_chars() + len(AGENT_SYSTEM_PROMPT)
        self.assertLessEqual(
            total,
            BUDGET_CHARS,
            f"The prompt and tool declarations are {total} characters, over the budget "
            f"of {BUDGET_CHARS}. Trim, or run `manage.py agent_eval_routing` to show the "
            "addition earns its place and raise BUDGET_CHARS in this PR.",
        )

    def test_the_prefix_with_the_parity_flag_on_stays_inside_its_budget(self) -> None:
        # The flag ships off, so production does not pay this yet; the day it is flipped it must
        # already fit, or the flip is also a budget decision nobody made.
        from apis import settings as production  # noqa: PLC0415

        with mock.patch.dict(
            settings.SEARCH_ENGINE, {**production.SEARCH_ENGINE, "AGENT_PARITY_TOOLS": True}
        ):
            total = _declared_chars() + len(AGENT_SYSTEM_PROMPT)
        self.assertLessEqual(total, BUDGET_CHARS)

    def test_the_count_covers_every_declared_tool(self) -> None:
        # A budget that silently measured nothing would always pass.
        self.assertGreater(_declared_chars(), 50_000)
