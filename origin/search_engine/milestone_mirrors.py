"""Resolve a milestone's backing-task mirror in a search result set.

A milestone in this workspace is TWO rows: a `MilestoneMaster` plus a
backing `TaskMaster` with `is_milestone=True` (see
`MilestoneMaster.task` — it owns the milestone's comments, attachments
and notes). `task_chunker` filters on `is_deleted` / `is_init_task`
only, so it indexes that backing row as an ordinary `entity_type="task"`
document. One thing the user thinks of as one milestone therefore has
two representations in the index, and the `task` twin renders with the
"Task" subtitle and the assignment icon instead of "Milestone" and the
flag.

This module fixes the result set rather than the index, which keeps the
backing task's own chunks — its comments in particular — searchable. Two
outcomes per mirrored `task` row:

  * The milestone is ALSO in the result set → drop the task row and
    hand its score to the milestone (so suppressing a mirror can never
    demote the milestone below results it had outranked).
  * The milestone is NOT in the result set → relabel the task row as
    its milestone. This is the case a drop-only rule would get wrong:
    when the match was in a comment, the backing task is the only
    representation there is, and dropping it would lose the result
    entirely.

The link is exact, not a guess: `MilestoneMaster.task_id` IS the backing
task's id, so one indexed query over the result set's task ids resolves
every mirror. That is why this is a separate pass from
`search._collapse_mirror_entities`, which matches on normalized title —
a heuristic that reorders results, hence a quality flag defaulting off.
This one is a correctness fix removing an internal artifact that was
never meant to be user-visible, so it always runs.

Precedent for the shape: `friendly_titles.apply_friendly_titles` is the
same thing — a DB-backed fixup of assembled rows, applied late. The
frontend already dedupes agent CITATION chips by this exact rule
(`citationUtils.ts` reads a milestone row's `task_id` and drops the
sibling task chip); result rows never got the same treatment.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)

# The frontend routes a milestone row by parsing its `entity_id` with an
# anchored `^milestone:(\d+)$` (`canonicalSpotlightHref.ts`). Anything
# else silently downgrades to the backing-task route, so a relabeled row
# MUST carry the canonical form — hence this is built here rather than
# copied off the task row.
_MILESTONE_ENTITY_ID = "milestone:{}"

_MILESTONE_ID_RE = re.compile(r"^milestone:(\d+)$")


def _milestone_id_of(row: dict) -> Optional[int]:
    m = _MILESTONE_ID_RE.match(str(row.get("entity_id") or ""))
    return int(m.group(1)) if m else None


def resolve_milestone_mirrors(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop or relabel `task` rows that are milestones' backing tasks.

    `entities` must be sorted by score desc; the return value keeps that
    invariant (it is re-sorted only when a score actually moved, so the
    common no-mirror case is untouched). Rows are mutated in place, as
    `apply_friendly_titles` does.

    Best-effort: a lookup failure logs and returns the input unchanged.
    A search must not fail over cosmetics.
    """
    task_rows = [
        e for e in entities if e.get("entity_type") == "task" and e.get("task_id") is not None
    ]
    if not task_rows:
        return entities

    try:
        # Lazy import — keeps this module importable without the Django
        # app registry ready, matching `friendly_titles`.
        from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415

        backing = {
            str(task_id): (milestone_id, status)
            for task_id, milestone_id, status in MilestoneMaster.objects.filter(
                task_id__in={str(e["task_id"]) for e in task_rows}, is_deleted=False
            ).values_list("task_id", "milestone_id", "status")
        }
    except Exception:  # noqa: BLE001 — never fail a search over a label
        log.exception("Milestone-mirror resolution failed; leaving result rows as-is")
        return entities

    if not backing:
        return entities

    # Milestones the set already represents in their own right.
    present: dict[int, dict] = {}
    for e in entities:
        if e.get("entity_type") == "milestone":
            mid = _milestone_id_of(e)
            if mid is not None:
                present[mid] = e

    out: list[dict[str, Any]] = []
    rescore = False
    for e in entities:
        mirror = (
            backing.get(str(e["task_id"]))
            if e.get("entity_type") == "task" and e.get("task_id") is not None
            else None
        )
        if mirror is None:
            out.append(e)
            continue

        milestone_id, milestone_status = mirror
        twin = present.get(milestone_id)
        if twin is not None:
            # Both representations present — keep the milestone, and let
            # it inherit whatever the mirror was better at.
            if e.get("score", 0) > twin.get("score", 0):
                twin["score"] = e["score"]
                rescore = True
            for rank in ("keyword_rank", "vector_rank"):
                if twin.get(rank) is None and e.get(rank) is not None:
                    twin[rank] = e[rank]
            # Spotlight orders by lexical tier before score: a comment on
            # the backing task matching the exact phrase must pin the
            # milestone, not vanish with the dropped mirror.
            tier = e.get("lexical_tier")
            if tier is not None and (
                twin.get("lexical_tier") is None or tier < twin["lexical_tier"]
            ):
                twin["lexical_tier"] = tier
            continue

        # Sole representation — relabel it in place.
        e["entity_type"] = "milestone"
        e["entity_id"] = _MILESTONE_ENTITY_ID.format(milestone_id)
        # `MilestoneMaster.status` is the milestone's OWN status, which is
        # a different column from the backing task's (`milestone_chunker`
        # indexes `m.status` for the same reason). The row's chip value
        # came off the task, so correct it to the authoritative one.
        # Absent on the agent shape, where `task_status` is deliberately
        # withheld — don't introduce the field there.
        if "task_status" in e:
            e["task_status"] = milestone_status or None
        out.append(e)

    if rescore:
        out.sort(key=lambda x: x.get("score", 0), reverse=True)
    return out
