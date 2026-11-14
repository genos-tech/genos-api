"""Support for the content-length eval cases (MIP-905, tag `t905`).

Three small things the grouped harness did not have, all optional per case and inert when a case
does not use them:

  `member_prefs: {content_length: short|standard|long}`
      The member's saved level for THIS case, applied with `content_length.override_level` (a
      ContextVar, like the case locale), never by writing the shared fixture member's row: cases
      run in parallel threads and would race on it. Only acts while the arm has
      `AGENT_CONTENT_LENGTH` on; on arm0 the level is ignored by the agent, as in production.

  `llm: {provider: gemini, effort: low}`
      The model choice for THIS case (an `LlmChoice` bound around the run, winning over a
      command-level `--provider/--effort`). Used where the case is only meaningful on a given
      rung (a cap that binds, the Medium two-tier loop).

  `expect: {pending_write: {...}, answer_chars_at_most: N, answer_chars_at_least: N}`
      Checks on what the model PROPOSED (the `tool_call_pending_approval` events: the write
      never runs in an eval) and on the answer's length, in characters (Japanese tokens cost two
      to three times English, so a character bound is the only fair one across languages).
      `pending_write` keys: tool, tasks_at_least, tasks_at_most, total_chars_at_least,
      total_chars_at_most, each_body_chars_at_least, each_body_chars_at_most,
      each_body_headings_at_least, each_body_headings_at_most, not_cut_off,
      each_body_kind_at_least (MIP-1532: `skeleton` = no task body is empty, `content` = no task
      body is a bare template), report_body_kinds (MIP-1532: only count none / skeleton / content).

The pending events are teed out of `run_agent` by wrapping the name `runner.run_agent` for the
duration of a case, reference-counted and routed by a ContextVar (the same pattern as the locale
patch in `case_runner`), so `runner.py` is not edited.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

PENDING_EVENT = "tool_call_pending_approval"

#: Argument keys that hold a body the model wrote, in any write tool (a plan nests them).
BODY_KEYS = frozenset(
    {
        "content_text",
        "content_markdown",
        "description_markdown",
        "body_append",
        "body_prepend",
        "new_text",
    }
)

# --------------------------------------------------------------------------- #
# member_prefs and llm                                                        #
# --------------------------------------------------------------------------- #


@contextmanager
def member_prefs(prefs: dict[str, Any] | None) -> Iterator[None]:
    """Apply a case's `member_prefs` for the duration of the block (see the module docstring)."""
    level = (prefs or {}).get("content_length")
    if not level:
        yield
        return
    from origin.search_engine.agent import content_length  # noqa: PLC0415

    with content_length.override_level(str(level)):
        yield


@contextmanager
def case_llm_choice(llm: dict[str, Any] | None) -> Iterator[None]:
    """Bind a case's `llm: {provider, effort}` as the run's `LlmChoice` (see the docstring)."""
    if not llm:
        yield
        return
    from django.conf import settings  # noqa: PLC0415

    from origin.search_engine.llm.choice import (  # noqa: PLC0415
        LlmChoice,
        reset_llm_choice,
        set_llm_choice,
    )

    provider, effort = str(llm.get("provider") or ""), str(llm.get("effort") or "")
    model = settings.LLM_CATALOG.model_for_effort(provider, effort)
    token = set_llm_choice(LlmChoice(provider=provider, model=model, effort=effort))
    try:
        yield
    finally:
        reset_llm_choice(token)


# --------------------------------------------------------------------------- #
# Teeing the pending-approval events                                          #
# --------------------------------------------------------------------------- #

_pending_var: ContextVar[list[dict[str, Any]] | None] = ContextVar("group_pending", default=None)
_patch_lock = threading.Lock()
_patch_state: dict[str, Any] = {"count": 0, "original": None}


def _install() -> None:
    from origin.search_engine.agent.evals import runner  # noqa: PLC0415

    with _patch_lock:
        if _patch_state["count"] == 0:
            original = runner.run_agent
            _patch_state["original"] = original

            def run_agent(query: Any, ctx: Any, emit: Any, *args: Any, **kwargs: Any) -> Any:
                sink = _pending_var.get()
                if sink is None:
                    return original(query, ctx, emit, *args, **kwargs)

                def tee(event: dict[str, Any]) -> None:
                    if event.get("type") == PENDING_EVENT:
                        sink.append(event)
                    emit(event)

                return original(query, ctx, tee, *args, **kwargs)

            runner.run_agent = run_agent
        _patch_state["count"] += 1


def _remove() -> None:
    from origin.search_engine.agent.evals import runner  # noqa: PLC0415

    with _patch_lock:
        _patch_state["count"] -= 1
        if _patch_state["count"] == 0:
            runner.run_agent = _patch_state["original"]
            _patch_state["original"] = None


@contextmanager
def capture_pending() -> Iterator[list[dict[str, Any]]]:
    """Yield a list that fills with this case's `tool_call_pending_approval` events."""
    sink: list[dict[str, Any]] = []
    token = _pending_var.set(sink)
    _install()
    try:
        yield sink
    finally:
        _remove()
        _pending_var.reset(token)


# --------------------------------------------------------------------------- #
# Reading the proposals                                                       #
# --------------------------------------------------------------------------- #


def bodies_of(arguments: Any) -> list[str]:
    """Every body string in a write call's arguments, in document order (a plan's milestone
    description first, then each task's body)."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in BODY_KEYS and isinstance(value, str):
                    found.append(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(arguments)
    return found


def task_bodies_of(arguments: Any) -> list[str]:
    """The `content_markdown` of each task of a plan ("" for a task without one)."""
    tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
    if not isinstance(tasks, list):
        return []
    return [str(t.get("content_markdown") or "") if isinstance(t, dict) else "" for t in tasks]


def task_count(arguments: Any) -> int:
    tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
    return len(tasks) if isinstance(tasks, list) else 0


def heading_count(body: str) -> int:
    return len(re.findall(r"(?m)^\s{0,3}#{1,6}\s+\S", body))


_TERMINAL = set(".。!！?？)）]】\"'”’`*_|:;…~」』")
_LIST_OR_HEADING = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}\s|\|)")

# A last line that ends on one of these has not finished its sentence. A word that ends a deliberate
# short line (a title: "Roll out to 10% of users", "フィルターUIの設計") is NOT in the list.
_DANGLING_CHARS = set(",、，;；(（[「『{-–—/&+=<>")
_DANGLING_WORDS = frozenset(
    "a an the and or but of to in on at for with by from into onto as that which who whose if "
    "than then so is are was were be been will would can could should may might not no".split()
)
_DANGLING_PARTICLES = ("を", "に", "の", "は", "が", "と", "で", "や", "も", "へ", "から", "より", "ため")
_OPENERS = {"(": ")", "（": "）", "[": "]", "「": "」", "『": "』", "{": "}"}


def looks_cut_off(body: str) -> bool:
    """A heuristic for "this body stops mid-thought". The real signals are the task count and the
    absence of an error event; this catches a body that visibly trails off.

    Cut off when: the body is empty, has an odd number of code fences, or its last line (not a
    list item, heading or table row, which may end on anything) leaves a bracket or quote open,
    ends on a joining character (a comma, a dash, an opening bracket) or ends on a word that
    cannot end a sentence (an article, a preposition, a conjunction; a Japanese particle).

    MIP-1532: this used to be "the last character is a letter or digit". Every CJK character is
    alphanumeric to Python and a short deliberate body ("### Summary" + a title) never ends in a
    full stop, so EVERY plain body was flagged, in English and Japanese alike. Ending without a
    full stop is how titles and short notes end. What this cannot see is a body cut inside a word
    ("The first par"); the model's stop reason and the error event are the signal for that."""
    text = body.rstrip()
    if not text:
        return True
    if text.count("```") % 2:
        return True
    last_line = text.splitlines()[-1].rstrip()
    if _LIST_OR_HEADING.match(last_line):
        return False
    for opener, closer in _OPENERS.items():
        if last_line.count(opener) > last_line.count(closer):
            return True
    if last_line.count("\u201c") > last_line.count("\u201d"):
        return True
    tail = last_line[-1]
    if tail in _DANGLING_CHARS:
        return True
    if tail in _TERMINAL:
        return False
    words = re.findall(r"[A-Za-z']+$", last_line)
    if words and words[0].lower() in _DANGLING_WORDS:
        return True
    return last_line.endswith(_DANGLING_PARTICLES)


# --------------------------------------------------------------------------- #
# What a body IS: none / skeleton / content (MIP-1532)                         #
# --------------------------------------------------------------------------- #

BODY_KINDS = ("none", "skeleton", "content")
_HEADING_LINE = re.compile(r"^#{1,6}(?:\s.*)?$")
_EMPTY_ITEM = re.compile(r"^(?:[-*+]|\d+[.)])(?:\s*\[[ xX]?\])?$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[*_`]", "", text).split()).casefold()


def body_kind(body: str, title: str = "") -> str:
    """`none` (blank), `skeleton` (nothing the member did not already have: only headings, empty
    list items and checkboxes, and at most the task's own title echoed) or `content`.

    Since MIP-1510 the server fills a task that came without a body with `### Summary` + title (or
    the project's default template with the title under its first heading), so a plan can no longer
    reach the card without a body. A skeleton is what the member FILLS IN; content is what the
    model wrote. A project template's hint paragraphs count as content (they are text)."""
    if not (body or "").strip():
        return "none"
    kept = []
    for line in _COMMENT.sub("", body).splitlines():
        line = line.strip()
        if line and not _HEADING_LINE.match(line) and not _EMPTY_ITEM.match(line):
            kept.append(line)
    rest = _plain(" ".join(kept))
    return "skeleton" if not rest or rest == _plain(title) else "content"


def task_titles_of(arguments: Any) -> list[str]:
    tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
    if not isinstance(tasks, list):
        return []
    return [str(t.get("title") or "") if isinstance(t, dict) else "" for t in tasks]


def check_pending(
    spec: dict[str, Any], events: list[dict[str, Any]], *, answer: str = ""
) -> list[dict[str, Any]]:
    """The `expect: pending_write:` checks. One check per key, `[{name, ok, detail}]`."""
    tool = spec.get("tool")
    matching = [e for e in events if not tool or e.get("tool_name") == tool]
    if not matching:
        seen = [e.get("tool_name") for e in events]
        return [
            {
                "name": "pending_write",
                "ok": False,
                "detail": f"no proposal of {tool or 'any write'}; proposed {seen or 'nothing'}"
                + (f"; answer began: {answer[:120]!r}" if answer else ""),
            }
        ]
    # One proposal is what the cases ask for; with several, the longest is judged.
    arguments = max(matching, key=lambda e: sum(map(len, bodies_of(e.get("arguments")))))[
        "arguments"
    ]
    all_bodies = bodies_of(arguments)
    # `each_body_*` and `not_cut_off` judge the TASK bodies of a plan (its milestone description
    # is a different shape); `total_chars_*` counts every body.
    bodies = task_bodies_of(arguments) or all_bodies
    lengths = [len(b) for b in bodies]
    tasks = task_count(arguments)
    checks: list[dict[str, Any]] = [
        {"name": "pending_write", "ok": True, "detail": f"{tool or 'a write'} proposed"}
    ]

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": f"pending_write.{name}", "ok": ok, "detail": detail})

    if "tasks_at_least" in spec:
        add("tasks_at_least", tasks >= int(spec["tasks_at_least"]), f"{tasks} tasks")
    if "tasks_at_most" in spec:
        add("tasks_at_most", tasks <= int(spec["tasks_at_most"]), f"{tasks} tasks")
    total = sum(len(b) for b in all_bodies)
    if "total_chars_at_least" in spec:
        add(
            "total_chars_at_least",
            total >= int(spec["total_chars_at_least"]),
            f"{total} chars over {len(all_bodies)} bodies, want >= {spec['total_chars_at_least']}",
        )
    if "total_chars_at_most" in spec:
        add(
            "total_chars_at_most",
            total <= int(spec["total_chars_at_most"]),
            f"{total} chars over {len(all_bodies)} bodies, want <= {spec['total_chars_at_most']}",
        )
    if "each_body_chars_at_least" in spec:
        small = [n for n in lengths if n < int(spec["each_body_chars_at_least"])]
        add(
            "each_body_chars_at_least",
            bool(bodies) and not small,
            f"body lengths {lengths}, want each >= {spec['each_body_chars_at_least']}",
        )
    if "each_body_chars_at_most" in spec:
        big = [n for n in lengths if n > int(spec["each_body_chars_at_most"])]
        add(
            "each_body_chars_at_most",
            bool(bodies) and not big,
            f"body lengths {lengths}, want each <= {spec['each_body_chars_at_most']}",
        )
    if "each_body_headings_at_least" in spec or "each_body_headings_at_most" in spec:
        counts = [heading_count(b) for b in bodies]
        if "each_body_headings_at_least" in spec:
            want = int(spec["each_body_headings_at_least"])
            add(
                "each_body_headings_at_least",
                bool(counts) and all(c >= want for c in counts),
                f"headings per body {counts}, want each >= {want}",
            )
        if "each_body_headings_at_most" in spec:
            want = int(spec["each_body_headings_at_most"])
            add(
                "each_body_headings_at_most",
                bool(counts) and all(c <= want for c in counts),
                f"headings per body {counts}, want each <= {want}",
            )
    titles = task_titles_of(arguments) if task_bodies_of(arguments) else []
    kinds = [body_kind(b, titles[i] if i < len(titles) else "") for i, b in enumerate(bodies)]
    if "each_body_kind_at_least" in spec or spec.get("report_body_kinds"):
        counts = {k: kinds.count(k) for k in BODY_KINDS}
        summary = ", ".join(f"{k} {counts[k]}" for k in BODY_KINDS) + f" of {len(kinds)} bodies"
        add("body_kinds", True, summary)  # information for the report, never a verdict
        if "each_body_kind_at_least" in spec:
            floor = str(spec["each_body_kind_at_least"])
            rank = {k: i for i, k in enumerate(BODY_KINDS)}
            low = [i for i, k in enumerate(kinds) if rank[k] < rank.get(floor, 0)]
            add(
                "each_body_kind_at_least",
                floor in rank and bool(kinds) and not low,
                f"want each body >= {floor}; below it: {low}; {summary}",
            )
    if spec.get("not_cut_off"):
        # A skeleton ends where the server ended it, by construction: never "cut off".
        cut = [
            i
            for i, b in enumerate(bodies)
            if looks_cut_off(b) and (not b.strip() or kinds[i] != "skeleton")
        ]
        add(
            "not_cut_off",
            bool(bodies) and not cut,
            f"bodies that look cut off: {cut}" if cut else f"{len(bodies)} bodies end cleanly",
        )
    return checks


def summarize_proposals(events: list[dict[str, Any]], limit: int = 300) -> list[dict[str, str]]:
    """What the model proposed, for a result row (MIP-1504): `[{"tool", "args"}]`, the arguments
    as JSON cut to `limit` characters (an ellipsis marks the cut). `render_pending` shows only the
    BODIES, so a proposal with empty bodies (or none) was invisible in the row."""
    out = []
    for event in events or []:
        text = json.dumps(event.get("arguments"), ensure_ascii=False, default=str, sort_keys=True)
        if len(text) > limit:
            text = text[:limit] + "…"
        out.append({"tool": str(event.get("tool_name")), "args": text})
    return out


def render_pending(events: list[dict[str, Any]]) -> str:
    """The proposals as text for the report: `[tool] body ...` per proposal ("" when none)."""
    out = []
    for event in events:
        bodies = bodies_of(event.get("arguments"))
        if bodies:
            out.append(f"[proposed {event.get('tool_name')}]\n" + "\n---\n".join(bodies))
    return "\n\n".join(out)


def check_answer_length(expect: dict[str, Any], answer: str) -> list[dict[str, Any]]:
    """`answer_chars_at_most` / `answer_chars_at_least`, by characters."""
    n = len(answer or "")
    checks = []
    if "answer_chars_at_most" in expect:
        limit = int(expect["answer_chars_at_most"])
        checks.append(
            {
                "name": "answer_chars_at_most",
                "ok": n <= limit,
                "detail": f"{n} chars, limit {limit}",
            }
        )
    if "answer_chars_at_least" in expect:
        limit = int(expect["answer_chars_at_least"])
        checks.append(
            {
                "name": "answer_chars_at_least",
                "ok": n >= limit,
                "detail": f"{n} chars, want {limit}",
            }
        )
    return checks
