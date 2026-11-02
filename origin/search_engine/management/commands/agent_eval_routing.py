"""`python manage.py agent_eval_routing`: first-step tool choice (MIP-881).

Sends every prompt in `evals/routing_cases.yaml` to the planning model with the
tools the app declares today, and reports how often the first turn reached for
a right tool. See `evals/routing.py` for what this does and doesn't measure.

Billed: about a tenth of a cent a call on flash-lite with the prefix cached, so
a full run of both languages is around 20 cents. It needs an LLM and nothing
else, so it never touches the eval fixture or a shared database.

    python manage.py agent_eval_routing
    python manage.py agent_eval_routing --reps 3 --lang ja --json routing.json
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand

from origin.search_engine.agent.controller import _build_tool_declarations
from origin.search_engine.agent.evals import routing
from origin.search_engine.llm import get_model_client

# The model the two-tier loop plans on, and the one a low-effort ask runs on.
DEFAULT_MODEL = "gemini-3.5-flash-lite"


class Command(BaseCommand):
    help = "Measure which tool the planning model reaches for first."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--reps", type=int, default=1, help="Runs of each case.")
        parser.add_argument("--lang", choices=routing.LANGUAGES, help="One language only.")
        parser.add_argument("--case", action="append", help="Only this case id. Repeatable.")
        parser.add_argument("--model", default=DEFAULT_MODEL)
        parser.add_argument("--concurrency", type=int, default=4)
        parser.add_argument("--json", help="Also write every outcome to this file.")

    def handle(self, *args: Any, **options: Any) -> None:
        languages = (options["lang"],) if options["lang"] else routing.LANGUAGES
        cases = routing.load_cases(languages=languages)
        if options["case"]:
            cases = [c for c in cases if c.id in set(options["case"])]
        today_line = f"Today's date is {datetime.now(UTC):%Y-%m-%d (%A)} in the UTC timezone."
        declarations = _build_tool_declarations()
        client = get_model_client()

        def run(case: routing.RoutingCase) -> routing.Outcome:
            return routing.route_once(
                case,
                arm="current",
                declarations=declarations,
                system_instruction=routing.system_instruction_for(case.lang, today_line=today_line),
                model=options["model"],
                client=client,
            )

        jobs = [case for _ in range(options["reps"]) for case in cases]
        with ThreadPoolExecutor(max_workers=options["concurrency"]) as pool:
            outcomes = list(pool.map(run, jobs))

        summary = routing.summarize(outcomes)
        self.stdout.write(json.dumps(summary, ensure_ascii=False, indent=2))
        if options["json"]:
            Path(options["json"]).write_text(
                json.dumps(routing.outcome_rows(outcomes), ensure_ascii=False),
                encoding="utf-8",
            )
