"""`python manage.py rejudge_team_answers` -- apply the mention rule to answers already collected.

Since MIP-980 `shared_answers.judge_run` audits a run's @/# mentions like
sources: a mentioned PRIVATE note narrows the audience of a collected answer to
the note's readers, a to-do to its owner, a mention that cannot be classified
withholds the answer. New asks and every reindex pass follow it. Answers that
were collected BEFORE the rule still sit in the team-shared `spotlight_answer`
lane with the wider audience, and the orphan sweep cannot fix them (it only runs
the cheap DB half of the rule, `shared_answers.still_shareable`). This command
re-judges exactly those:

    python manage.py rejudge_team_answers              # dry run (default)
    python manage.py rejudge_team_answers --dry-run
    python manage.py rejudge_team_answers --apply

  * Scope: the runs that carry mentions (`AgentRun.mentions` not empty) AND
    have a tracked team-lane chunk (`RagChunk`). Nothing else is read or written.
  * Each is judged by the one rule, `judge_run` (through the chunker's own
    per-run builder, so what is written is what a reindex would write):
      - no longer COLLECT -> the chunk is purged (`purge_agent_runs`, by its
        deterministic id as well as its tracking row);
      - still COLLECT but the stored chunk differs from what the chunker builds
        now (its ACL narrowed) -> the entity goes through `ingest_entity_stream`.
        A changed `acl_user_ids` changes the chunk's metadata fingerprint, so
        ingestion refreshes the document IN PLACE (a metadata update, no
        re-embedding, no LLM call); if the text also changed it is embedded as
        any reindex would.
  * A dry run is DB-only and prints COUNTS ONLY (never an id, a title, a
    question or an answer; mention labels are titles). `--apply` pings
    OpenSearch first, does the work, then counts again and fails if anything is
    left (a failed delete or write): run it again.

Idempotent: a second run finds nothing to purge or rewrite. It never reads or
writes the run rows, and the private `conversation` lane is untouched.

This is a new command, not an option of `purge_scoped_team_answers`: that one
needs no rule at all (a DB filter, purge only), this one needs `judge_run`, the
source audit and a re-ingest.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.core.management.base import BaseCommand, CommandError

from origin.search_engine import shared_answers
from origin.search_engine.agent.source_visibility import shareable_acl_for_sources
from origin.search_engine.chunkers.spotlight_answer_chunker import (
    iter_spotlight_answer_chunks_for_runs,
    spotlight_answer_chunks_for_run,
)
from origin.search_engine.embeddings import hash_text
from origin.search_engine.ingestion import _meta_fingerprint, ingest_entity_stream
from origin.search_engine.models import AgentRun, RagChunk
from origin.search_engine.opensearch_client import get_client
from origin.search_engine.purge import purge_agent_runs

LANE = "spotlight_answer"
_BATCH = 500


@dataclass
class Plan:
    runs_with_mentions: int = 0
    tracked_team_chunks: int = 0
    purge: list[str] = field(default_factory=list)  # run ids, no longer COLLECT
    rewrite: list[AgentRun] = field(default_factory=list)  # COLLECT, stored chunk differs
    narrowed: int = 0  # COLLECT with an audience smaller than the tools' sources alone
    unchanged: int = 0


def _tracked_entity_ids(entity_ids: list[str]) -> set[str]:
    tracked: set[str] = set()
    for i in range(0, len(entity_ids), _BATCH):
        tracked |= set(
            RagChunk.objects.filter(
                entity_type=LANE, entity_id__in=entity_ids[i : i + _BATCH]
            ).values_list("entity_id", flat=True)
        )
    return tracked


def make_plan() -> Plan:
    """Judge every run with mentions that has a tracked team-lane chunk."""
    plan = Plan()
    runs = [
        run
        for run in AgentRun.objects.exclude(mentions=[])
        .select_related("session")
        .order_by("started_at")
        if run.mentions is not None and run.mentions != []
    ]
    plan.runs_with_mentions = len(runs)
    tracked = _tracked_entity_ids([f"{LANE}:{run.run_id}" for run in runs])
    opted_out = shared_answers.opted_out_user_ids()

    for run in runs:
        entity_id = f"{LANE}:{run.run_id}"
        if entity_id not in tracked:
            continue
        plan.tracked_team_chunks += 1
        entity = spotlight_answer_chunks_for_run(run, opted_out=opted_out)
        if entity is None or not entity.chunks:  # SKIP or WITHHELD: not collected now
            plan.purge.append(str(run.run_id))
            continue
        chunk = entity.chunks[0]
        # What the tools' sources alone would have allowed (the chunk keeps
        # those as its provenance): informational, "how many did a mention narrow".
        without_mentions = shareable_acl_for_sources(
            chunk.answer_sources, asker_id=str(run.user_id) if run.user_id else None
        )
        if set(chunk.acl_user_ids or ()) != set(without_mentions or ()):
            plan.narrowed += 1
        row = RagChunk.objects.filter(entity_type=LANE, chunk_id=chunk.chunk_id).first()
        if (
            row is None
            or row.text_hash != hash_text(chunk.search_text or "")
            or row.meta_hash != _meta_fingerprint(chunk)
        ):
            plan.rewrite.append(run)
        else:
            plan.unchanged += 1
    return plan


class Command(BaseCommand):
    help = (
        "Re-judge the collected team-lane (spotlight_answer) answers whose runs carry @/# "
        "mentions: purge those no longer collectable, narrow the ACL of the rest. "
        "Dry run unless --apply; prints counts only."
    )

    def add_arguments(self, parser):
        mode = parser.add_mutually_exclusive_group()
        mode.add_argument(
            "--dry-run",
            action="store_true",
            help="Count only; change nothing (the default).",
        )
        mode.add_argument(
            "--apply",
            action="store_true",
            help="Purge and re-ingest.",
        )

    @staticmethod
    def _report(mode: str, plan: Plan) -> dict:
        return {
            "mode": mode,
            "runs_with_mentions": plan.runs_with_mentions,
            "tracked_team_chunks": plan.tracked_team_chunks,
            "to_purge": len(plan.purge),
            "to_rewrite": len(plan.rewrite),
            "audience_narrowed_by_mentions": plan.narrowed,
            "unchanged": plan.unchanged,
        }

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        plan = make_plan()
        report = self._report("apply" if apply else "dry-run", plan)

        failed = 0
        if apply and (plan.purge or plan.rewrite):
            # `purge_agent_runs` is best-effort and never raises, so an
            # unreachable index would look like success. Say so up front.
            if not get_client().ping():
                raise CommandError("OpenSearch is unreachable (ping failed); nothing changed.")
            for i in range(0, len(plan.purge), _BATCH):
                purge_agent_runs(plan.purge[i : i + _BATCH], lanes=(LANE,))
            if plan.rewrite:
                # The chunker's own builder, so the row written is the row a
                # reindex would write. `ingest_entity_stream` never raises for
                # one entity: failures come back in `stats.errors`.
                stats = ingest_entity_stream(iter_spotlight_answer_chunks_for_runs(plan.rewrite))
                failed += len(stats.errors)
            after = make_plan()
            report["to_purge_remaining"] = len(after.purge)
            report["to_rewrite_remaining"] = len(after.rewrite)
            report["write_errors"] = failed

        self.stdout.write(json.dumps(report, indent=2))
        left = report.get("to_purge_remaining", 0) + report.get("to_rewrite_remaining", 0)
        if left or failed:
            raise CommandError(
                "Some chunks could not be purged or rewritten (the index refused them); "
                "run the command again."
            )
        if not apply:
            self.stdout.write(
                self.style.WARNING("Dry run: nothing changed. Add --apply to purge and narrow.")
            )
