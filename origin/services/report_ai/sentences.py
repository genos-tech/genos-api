"""One sentence of context per task, for a report (MIP-1210). The orchestration.

`sentences_for(ctx, task_ids, effort...)` returns `Sentences`: `{task_id: sentence}` for at most
`MAX_SENTENCES` of the listed tasks, the request that paid (when a call was made), and why there
was no AI part (when there was none). It NEVER raises and never fails a report.

The steps, cheapest first, so the model is asked about as little as possible:

1. **Access.** Only tasks the member can open are read (`context.gather`).
2. **Worth asking.** A task with no comment and no relevant change is not sent: nothing could
   explain its state. At most `MAX_TASKS` tasks go in one prompt (the first by the report's own
   order of importance).
3. **Cache.** A task's sentence (or the fact that it has none) is kept per member, task, the
   fingerprint of what the prompt says about it (comments, status, due date, changes), the
   prompt version and the effort level, until any of that changes. Only the misses are sent.
4. **One batched call** for the misses, on the model the effort level names, metered.
5. **Clean** (`validate`), cache, and keep the first `MAX_SENTENCES` in the report's order.

`MAX_SENTENCES` is 6: a report lists a handful of tasks per section, and a sentence under every
one would be the wall of text Ken asked us to avoid; six also bounds the output tokens.
`MAX_TASKS` is 10, so the cost of one run is bounded whatever the report lists.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from django.core.cache import cache

from origin.search_engine.agent.tools.base import ToolContext
from origin.services.report_ai import context as task_context
from origin.services.report_ai import effort as effort_mod
from origin.services.report_ai import llm, prompt, validate

log = logging.getLogger(__name__)

MAX_SENTENCES = 6
MAX_TASKS = 10
CACHE_SECONDS = 14 * 24 * 3600


@dataclass
class Sentences:
    by_task: dict[int, str] = field(default_factory=dict)
    request_id: str = ""
    skipped: str = ""
    called: bool = False


def _key(user_id: str, task: task_context.TaskContext, level: str) -> str:
    raw = f"{user_id}|{task.task_id}|{task.fingerprint}|{prompt.PROMPT_VERSION}|{level}"
    return "reportai:" + hashlib.sha256(raw.encode()).hexdigest()


def _cache_get_many(keys: list[str]) -> dict[str, Any]:
    try:
        return cache.get_many(keys)
    except Exception:  # noqa: BLE001 - a cache outage costs a call, nothing else
        return {}


def _cache_set_many(items: dict[str, str]) -> None:
    try:
        cache.set_many(items, CACHE_SECONDS)
    except Exception:  # noqa: BLE001
        log.warning("report_ai: cache write failed")


def _may_name(ctx: ToolContext):
    """`may_name(display_ids)`: whether the member can open every task those ids name, in this
    team. An id that names no task, or one they cannot open, is False."""
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415

    def check(display_ids: set[str]) -> bool:
        wanted: list[int] = []
        for display_id in display_ids:
            code, _, number = display_id.rpartition("-")
            found = (
                TaskMaster.objects.filter(
                    team_id=ctx.team_id,
                    project__code=code,
                    project_task_number=int(number),
                    is_deleted=False,
                )
                .values_list("task_id", flat=True)
                .first()
            )
            if found is None:
                return False
            wanted.append(found)
        return task_context.can_open(ctx, wanted) == set(wanted)

    return check


def sentences_for(
    ctx: ToolContext,
    task_ids: list[int],
    *,
    effort: Any,
    default_effort: str,
    now: datetime,
    today_iso: str,
) -> Sentences:
    out = Sentences()
    try:
        return _run(ctx, task_ids, effort, default_effort, now, today_iso, out)
    except Exception as exc:  # noqa: BLE001 - never fails a report
        log.warning("report_ai: sentences skipped (%s)", type(exc).__name__, exc_info=True)
        out.by_task = {}
        out.skipped = out.skipped or llm.SKIP_FAILED
        return out


def _run(ctx, task_ids, effort, default_effort, now, today_iso, out: Sentences) -> Sentences:
    run = effort_mod.resolve(effort, default_effort, ctx.user_id)
    reason = llm.preflight(run)
    level = run.effort if run else default_effort

    candidates = [t for t in task_context.gather(ctx, task_ids, now=now) if t.has_context][
        :MAX_TASKS
    ]
    if not candidates:
        out.skipped = "nothing_to_say"
        return out

    keys = {t.task_id: _key(ctx.user_id, t, level) for t in candidates}
    cached = _cache_get_many(list(keys.values()))
    known: dict[int, str] = {}
    misses = []
    for t in candidates:
        hit = cached.get(keys[t.task_id])
        if isinstance(hit, str):
            known[t.task_id] = hit  # "" = it had nothing worth saying, cached too
        else:
            misses.append(t)

    if misses and reason:
        # Cannot ask (switch off, no meter, no credentials): what is cached still shows.
        out.skipped = reason
    elif misses:
        refs = {f"t{i}": t for i, t in enumerate(misses, 1)}
        result = llm.complete(
            run,
            system=prompt.SYSTEM,
            user=prompt.build_user_message(list(refs.items()), today_iso),
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            max_output_tokens=_output_cap(len(misses)),
        )
        out.request_id = result.request_id
        out.called = bool(result.request_id)
        if result.skipped:
            out.skipped = result.skipped
        else:
            parsed = validate.parse(result.text, set(refs))
            check = _may_name(ctx)
            fresh: dict[str, str] = {}
            for ref, task in refs.items():
                sentence = validate.clean_sentence(parsed.get(ref), may_name=check)
                known[task.task_id] = sentence
                if ref in parsed:  # an answer, even "nothing": a missing key is not cached
                    fresh[keys[task.task_id]] = sentence
            if fresh:
                _cache_set_many(fresh)

    shown = 0
    for t in candidates:  # the report's own order
        sentence = known.get(t.task_id, "")
        if sentence and shown < MAX_SENTENCES:
            out.by_task[t.task_id] = sentence
            shown += 1
    return out


def _output_cap(n_tasks: int) -> int:
    """About 60 tokens a task (a 180-character sentence and its key) and a little over."""
    return 60 * n_tasks + 40
