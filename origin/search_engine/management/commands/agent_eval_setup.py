"""`python manage.py agent_eval_setup` — provision the eval fixture.

Ensures the deterministic eval team / user / seeded data exists and is
indexed in OpenSearch. Idempotent by default; pass `--reseed` to force
a clean teardown + reseed (use when the seeder content has changed).

Run this once before `python manage.py agent_eval --retrieval` (or
`--all`). The fixture team uses fixed UUIDs so eval-case YAML can
hard-code `team_id` / `user_id` without drift.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from origin.search_engine.agent.evals.fixture import (
    FIXTURE_USER_ID,
    ensure_fixture,
    reseed_fixture,
)


class Command(BaseCommand):
    help = "Provision (or re-provision) the deterministic eval fixture."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reseed",
            action="store_true",
            help=(
                "Force a clean teardown + reseed even if the fixture "
                "already exists. Use when seeder content changed."
            ),
        )
        parser.add_argument(
            "--scenario",
            choices=["sequence"],
            help=(
                "Provision a scenario fixture in its OWN teams instead of the v1 eval fixture "
                "(which is left untouched). `sequence`: the 12-step client/PM/dev story of "
                "MIP-1269, in three teams (v1, v2, ja). Idempotent; with --reseed it rebuilds "
                "those teams only. Needs Postgres only: no OpenSearch, no model call."
            ),
        )

    def handle(self, *args, **options):
        force_reseed = options.get("reseed") or False

        if options.get("scenario") == "sequence":
            self._handle_sequence(force_reseed)
            return

        if force_reseed:
            self.stdout.write("Reseeding eval fixture from scratch…")
            summary = reseed_fixture()
        else:
            self.stdout.write("Ensuring eval fixture exists…")
            summary = ensure_fixture()

        action = "reseeded" if summary["reseeded"] else "already present"
        self.stdout.write(
            self.style.SUCCESS(
                f"\nEval fixture {action}.\n"
                f"  team_id: {summary['team_id']}\n"
                f"  user_id: {summary['user_id']}\n"
                f"\nYAML cases referencing the fixture should use:\n"
                f'  team_id: "{summary["team_id"]}"\n'
                f'  user_id: "{FIXTURE_USER_ID}"\n'
            )
        )

    def _handle_sequence(self, force_reseed: bool) -> None:
        from origin.search_engine.agent.evals.group import sequence  # noqa: PLC0415

        self.stdout.write(
            "Reseeding the sequence scenario…"
            if force_reseed
            else "Ensuring the sequence scenario…"
        )
        summary = sequence.ensure_sequence(reseed=force_reseed)
        built = ", ".join(summary["built"]) or "none (already present)"
        self.stdout.write(
            self.style.SUCCESS(f"\nSequence scenario ready. Teams built now: {built}.\n")
        )
        for key, info in summary["viewers"].items():
            self.stdout.write(
                f"  viewer {key:<5} team_id {info['team_id']}  user_id {info['user_id']}  "
                f"task {info['display_id']} (id {info['task_id']})"
            )
        self.stdout.write(
            "\nCases use `fixture: sequence` with `viewer: v1|v2|b|c|ja|ja-b`; the ids are resolved at run time.\n"
        )
