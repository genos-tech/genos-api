"""What an agent probably forgot when it filed a task, as a short `suggestions` list (MIP-2015).

Decision (Ken, 2026-11-22, the "Decision: Genos guides agents with suggestions in tool replies, not
hard rejections" note on MIP-2014): Genos never refuses a write for a missing parent or missing
blockers. The write succeeds, and the MCP reply carries up to two entries telling the agent what to
set, with likely candidates:

    {"kind": "parent_task", "message": "...", "candidates": [{"task_id", "display_id", "title"}, ...]}
    {"kind": "dependencies", "message": "...", "candidates": [{"task_id", "display_id", "title"}, ...]}

The keys are stable (`kind`, `message`, `candidates`) and `suggestions` is absent, not empty, when
there is nothing to say. At most `MAX_CANDIDATES` candidates per entry, one short line each.

How the candidates are found, and why this is not `origin.search_engine.search`. The vector and
keyword search embeds the query and (on MCP) rewrites it with a model: that is a paid AI call on every
task an agent files (Ken: every AI operation spends credits), the index lags a task created a moment
ago, and it takes hundreds of milliseconds. This is one indexed Postgres query over the OPEN tasks of
the one project (titles only, newest first, at most `SCAN_LIMIT` rows) and an IDF-weighted word
overlap in Python: no model, no network, a few milliseconds. Latin words and CJK character pairs are
both compared, so a Japanese title matches too.

Parent candidates are open tasks that look like containers (they already have an open sub-task, or
they sit at the top of the project / directly under a milestone) and share words with the new task.
The rest of the matches are offered as possible blockers or blocked tasks. Candidates are hints, not
facts: the message says to link only what really is related.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from origin.models.task.task_models import TaskMaster
from origin.services.task_status_values import FINISHED_STATUSES

#: Candidates per entry (parents, blockers).
MAX_CANDIDATES = 3
#: Open tasks of the project looked at, newest first. Bounds the cost of the one query and the
#: scoring loop; a project with more open tasks than this is matched against the most recently touched.
SCAN_LIMIT = 400
#: Characters of the new task's body used as extra words (its title counts double).
BODY_CHARS = 600
#: Statuses that are no longer open work, so never offered as a parent or a blocker.
_DONE = FINISHED_STATUSES
#: Titles shown to the agent are cut here: the list stays one short block.
_TITLE_CHARS = 90

_WORD = re.compile(r"[a-z0-9][a-z0-9_\-]*", re.IGNORECASE)
_CJK = re.compile(r"[぀-ヿ㐀-鿿豈-﫿가-힯]+")
#: Words too common in task titles to say anything. English only; CJK pairs are not filtered.
_STOP = frozenset(
    "a an and are as at be by can do for from has have how in into is it its new of on or per "
    "the this that to up use via was were will with without add adds adding fix fixes fixing "
    "make makes making update updates updating task tasks when not no all".split()
)

PARENT_MESSAGE = (
    "No parent task and no milestone: set `parent_task_id` (or `milestone_id`, never both) with "
    "`update_task` (a task with a place keeps the board organized), or say in your reply why "
    "this one has none. Candidates are guesses from the title; use one only if it really is the parent."
)
#: For a task filed directly under a milestone: it HAS a place, so the hint must not read as if it
#: had none, and must say that a parent moves it.
PARENT_MESSAGE_IN_MILESTONE = (
    "Filed directly under a milestone, with no parent task: if it belongs under a task, set "
    "`parent_task_id` with `update_task` (it then moves to that task's milestone), or say in "
    "your reply why it has none. Candidates are guesses from the title."
)
#: A plan of this many tasks or more with no dependency between any two reads as a list, not an order
#: of work. Two tasks can honestly be independent; three or more rarely are.
NO_DEPENDENCIES_MIN_TASKS = 3
NO_DEPENDENCIES_MESSAGE = (
    "These tasks have no dependencies between them: if one must be done before another, link it "
    "with `update_task_dependencies` (blocker, blocked); read the graph back with "
    "`list_task_dependencies`. Tasks that really can all start at once need nothing; say so in "
    "your reply."
)

PARENT_MESSAGE_PLAN = (
    "Some of these tasks have no parent task: set `parent_task_id` with `update_task`, or "
    "say in your reply why they have none. Candidates are guesses from the titles."
)
DEPENDENCIES_MESSAGE = (
    "Possibly related: if one of these blocks the new task or waits on it, link it with "
    "`update_task_dependencies`; skip it if it is only similar."
)
PARENT_MESSAGE_PLAN_IN_MILESTONE = (
    "Some of these tasks sit directly under a milestone with no parent task: if one belongs under "
    "a task, set `parent_task_id` with `update_task` (it then moves to that task's milestone), or "
    "say in your reply why it has none. Candidates are guesses from the titles."
)
DEPENDENCIES_MESSAGE_PLAN = (
    "Possibly related to the new tasks: if one blocks a new task or waits on one, link it with "
    "`update_task_dependencies`; skip it if it is only similar."
)


#: Japanese particles: a character pair that contains one ("の修", "面の") says nothing about the
#: topic, so it is not a token.
_PARTICLES = frozenset("のはをにがでともへや")
#: A CJK pair is half a word, so one short shared word ("修正") is not enough to be a match.
_CJK_WEIGHT = 0.5
#: Shared words (CJK pairs count half) needed before a task is offered.
MIN_SHARED_UNITS = 1.5


def _is_cjk_token(token: str) -> bool:
    return bool(_CJK.fullmatch(token))


def _tokens(text: str) -> set[str]:
    """Lower-cased words (stop words and 1-2 letter words dropped) and CJK character pairs without
    a particle."""
    lowered = (text or "").lower()
    out = {w for w in _WORD.findall(lowered) if len(w) > 2 and w not in _STOP}
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            if run not in _PARTICLES:
                out.add(run)
            continue
        out.update(
            run[i : i + 2]
            for i in range(len(run) - 1)
            if run[i] not in _PARTICLES and run[i + 1] not in _PARTICLES
        )
    return out


def _units(tokens: Iterable[str]) -> float:
    """How many words a set of shared tokens stands for (a CJK pair counts half)."""
    return sum(_CJK_WEIGHT if _is_cjk_token(t) else 1.0 for t in tokens)


def _display_id(project_code: str | None, number: int | None, task_id: int) -> str:
    """`TaskMaster.display_id` without loading the row and its project one by one."""
    if project_code and number is not None:
        return f"{project_code}-{number}"
    return f"#{task_id}"


def _candidate(row: dict[str, Any]) -> dict[str, Any]:
    title = row["title"] or ""
    if len(title) > _TITLE_CHARS:
        title = title[: _TITLE_CHARS - 1].rstrip() + "…"
    return {"task_id": row["task_id"], "display_id": row["display_id"], "title": title}


def _scan(project_id: int, team_id: str, exclude_ids: Iterable[int]) -> list[dict[str, Any]]:
    """The project's open tasks as small dicts. ONE query, titles and tree position only."""
    rows = list(
        TaskMaster.objects.filter(project_id=project_id, team_id=team_id, is_deleted=False)
        .exclude(status__in=_DONE)
        .exclude(is_milestone=True)
        .exclude(task_id__in=list(exclude_ids))
        .order_by("-ts_updated_at")
        .values_list("task_id", "title", "parent_task_id", "project_task_number", "project__code")[
            :SCAN_LIMIT
        ]
    )
    return [
        {
            "task_id": task_id,
            "title": title,
            "parent_task_id": parent_id,
            "display_id": _display_id(code, number, task_id),
        }
        for task_id, title, parent_id, number, code in rows
    ]


def _milestone_backing_ids(project_id: int, team_id: str) -> set[int]:
    """Task ids of the project's milestone rows: a task directly under one is "at the top"."""
    return set(
        TaskMaster.objects.filter(
            project_id=project_id, team_id=team_id, is_deleted=False, is_milestone=True
        ).values_list("task_id", flat=True)
    )


def find_candidates(
    *,
    project_id: int,
    team_id: str,
    title_texts: Sequence[str],
    body_texts: Sequence[str] = (),
    exclude_task_ids: Iterable[int] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """`(parent_candidates, related_candidates)` for tasks with these titles (and bodies).

    Each list is at most `MAX_CANDIDATES` long, best match first, and no task is in both. Never
    raises on an odd input: an empty title just finds nothing.
    """
    title_words: set[str] = set()
    for text in title_texts:
        title_words |= _tokens(text)
    body_words: set[str] = set()
    for text in body_texts:
        body_words |= _tokens((text or "")[:BODY_CHARS])
    body_words -= title_words
    if not title_words and not body_words:
        return [], []

    rows = _scan(project_id, team_id, exclude_task_ids)
    if not rows:
        return [], []

    doc_words = [_tokens(r["title"]) for r in rows]
    doc_freq: Counter[str] = Counter()
    for words in doc_words:
        doc_freq.update(words)
    n = len(rows)

    def idf(word: str) -> float:
        # Smoothed, always positive: a word in every title still counts a little.
        return math.log(1 + (n + 1) / (doc_freq.get(word, 0) + 1))

    scored: list[tuple[float, dict[str, Any]]] = []
    for row, words in zip(rows, doc_words):
        shared_title = words & title_words
        shared_body = words & body_words
        if not (shared_title or shared_body):
            continue
        shared = _units(shared_title) + _units(shared_body)
        # One shared word is enough only for a one-word title; otherwise it is a coincidence. Two
        # Latin words pass (2.0), so do three CJK pairs, i.e. a shared four-character word (1.5);
        # a single shared pair ("修正" alone, 0.5) does not.
        if shared < MIN_SHARED_UNITS and len(title_words) + len(body_words) > 1 and len(words) > 1:
            continue
        score = 2.0 * sum(idf(w) for w in shared_title) + sum(idf(w) for w in shared_body)
        # A long title that shares two words is a weaker match than a short one that shares them.
        scored.append((score / math.sqrt(len(words)), row))
    if not scored:
        return [], []
    scored.sort(key=lambda pair: (-pair[0], -pair[1]["task_id"]))

    has_open_child = {r["parent_task_id"] for r in rows if r["parent_task_id"] is not None}
    backing = _milestone_backing_ids(project_id, team_id)

    def container_like(row: dict[str, Any]) -> bool:
        parent = row["parent_task_id"]
        return row["task_id"] in has_open_child or parent is None or parent in backing

    # Tasks that already have an open sub-task come first (a stable sort keeps the score order
    # inside each group), then the loose top-level ones.
    containers = [row for _score, row in scored if container_like(row)]
    containers.sort(key=lambda row: row["task_id"] not in has_open_child)
    parents = containers[:MAX_CANDIDATES]
    chosen = {row["task_id"] for row in parents}
    related = [row for _score, row in scored if row["task_id"] not in chosen][:MAX_CANDIDATES]
    return [_candidate(r) for r in parents], [_candidate(r) for r in related]


def suggestions_for_new_task(
    *,
    project_id: int,
    team_id: str,
    task_id: int,
    title: str,
    body_text: str = "",
    in_milestone: bool = False,
) -> list[dict[str, Any]]:
    """The `suggestions` list for ONE task made with no parent task. `in_milestone`: it was filed
    directly under a milestone (the message then says a parent would move it)."""
    parents, related = find_candidates(
        project_id=project_id,
        team_id=team_id,
        title_texts=[title],
        body_texts=[body_text],
        exclude_task_ids=[task_id],
    )
    out: list[dict[str, Any]] = [
        {
            "kind": "parent_task",
            "message": PARENT_MESSAGE_IN_MILESTONE if in_milestone else PARENT_MESSAGE,
            "candidates": parents,
        }
    ]
    if related:
        out.append({"kind": "dependencies", "message": DEPENDENCIES_MESSAGE, "candidates": related})
    return out


def suggestions_for_new_plan(
    *,
    project_id: int,
    team_id: str,
    parentless: Sequence[tuple[int, str, str]],
    all_task_ids: Iterable[int],
    in_milestone: bool = False,
) -> list[dict[str, Any]]:
    """The `suggestions` list for a batch where `parentless` ((task_id, title, body_text) of the
    tasks that sit under no parent task) is not empty. One entry per kind for the whole batch, so
    the reply does not grow with the plan; empty when every task has a parent."""
    if not parentless:
        return []
    parents, related = find_candidates(
        project_id=project_id,
        team_id=team_id,
        title_texts=[title for _tid, title, _body in parentless],
        body_texts=[body for _tid, _title, body in parentless],
        exclude_task_ids=all_task_ids,
    )
    out: list[dict[str, Any]] = [
        {
            "kind": "parent_task",
            "message": PARENT_MESSAGE_PLAN_IN_MILESTONE if in_milestone else PARENT_MESSAGE_PLAN,
            "candidates": parents,
        }
    ]
    if related:
        out.append(
            {"kind": "dependencies", "message": DEPENDENCIES_MESSAGE_PLAN, "candidates": related}
        )
    return out


def no_dependencies_suggestion(task_count: int, dependency_count: int) -> dict[str, Any] | None:
    """The hint for a plan of `NO_DEPENDENCIES_MIN_TASKS` tasks or more that created no dependency
    edge at all, else None. A suggestion only: the plan is written either way."""
    if task_count < NO_DEPENDENCIES_MIN_TASKS or dependency_count > 0:
        return None
    return {"kind": "no_dependencies", "message": NO_DEPENDENCIES_MESSAGE, "candidates": []}
