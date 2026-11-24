"""Small helpers the built-in jobs share (MIP-1148): plain text out of the briefing's
fenced fields, and links to the things a report names.

The briefing's text fields are fenced (`wrap_workspace_content`) because a MODEL reads
them and must treat them as data. A report is read by the member, so the fence is taken
off here, once, at the edge: `plain`.

Links are in-app paths (`/workspace/...`, the same routes as `entity_links`). Each is built
only for something the briefing already decided the member may open, and the target view
checks access again when it is opened. A link we cannot build is left out: a line without
a link is better than a dead one.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.utils import timezone

from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.project_gone import live_tasks_q
from origin.services.schedule_jobs import AI_EFFORT_KEY, Report, ReportItem, ReportSection
from origin.services.user_time import zone_for_user_id

# A report line is a sentence, not a document: a snippet or a title is cut here.
LINE_CHARS = 240


def plain(value: Any) -> str:
    """The text of a briefing field, without the fence the briefing put round it."""
    if not isinstance(value, str) or not value:
        return ""
    inner = unwrap(value)
    return (value if inner is None else inner).strip()


def clip(text: str, limit: int = LINE_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def ai_sentences_on(params: dict[str, Any]) -> bool:
    """The AI part of a job is an OPT-IN (MIP-1969): on only when the member set
    `ai_sentences: true`. The client shows it as the schedule's "by Genos AI" mode.

    `what_to_do_today` schedules that existed before the opt-in were given an explicit `true` by
    migration 0267, so they keep what they did."""
    return params.get("ai_sentences") is True


# The schema entry of the `ai_sentences` parameter of the four jobs with a written summary
# (MIP-2350). `what_to_do_today` words its own (it writes a sentence under each task).
AI_SUMMARY_PARAM: dict[str, Any] = {
    "type": "boolean",
    "description": (
        "A short AI-written summary (2 to 4 sentences) at the top of the report, from what the "
        "report already lists. Off by default (it spends the owner's AI credits); true turns it "
        "on, and the plain report has no AI usage."
    ),
}
# The summary reads a few dozen short lines and writes a paragraph: the cheapest rung until a
# real-call comparison says the next one is worth its price (none is approved yet).
SUMMARY_EFFORT_DEFAULT = "low"


def add_narrative(
    ctx: ToolContext, params: dict[str, Any], report: Report, *, effort_default: str
) -> Report:
    """The AI part of a job whose report may open with a written summary (MIP-2350).

    Switched off (the default), it returns `report` as it is: no model call, nothing read, the
    same bytes as before. Switched on, it asks `report_ai.narrative` for the summary of the
    report ITSELF (so the prompt holds only what the card shows) and:

    * puts the summary first in `summary` and in `narrative`, and names the metered request
      that paid for it in `ai_request_id`;
    * or, when there was no call (nothing worth saying, the operator switch off, no credits, the
      model not reachable from this process, a failure), says why in `ai_skipped`, so the
      delivery step files the report plain with "No AI used" and never without a reason.
    """
    if not ai_sentences_on(params):
        return report
    from origin.services.report_ai import llm, narrative  # noqa: PLC0415 (heavy)

    zone = zone_for_user_id(ctx.user_id)
    result = narrative.narrative_for(
        ctx,
        dict(report),
        effort=params.get(AI_EFFORT_KEY),
        default_effort=effort_default,
        today_iso=timezone.now().astimezone(zone).date().isoformat(),
    )
    if result.text:
        report["narrative"] = result.text
        report["summary"] = f"{result.text} {report['summary']}"
    if result.request_id:
        report["ai_request_id"] = result.request_id
    else:
        report["ai_skipped"] = result.skipped or "cached"
    if result.skipped == llm.SKIP_NO_CREDITS:
        report["summary"] = clip(
            f"{report['summary']} The AI summary was left out: you have no AI credits left this month."
        )
    return report


def item(text: str, link: str | None = None, kind: str | None = None) -> ReportItem:
    out: ReportItem = {"text": clip(text)}
    if link:
        out["link"] = link
    if kind:
        out["kind"] = kind
    return out


_PRIORITY_RANK = {"Critical": 0, "High": 1, "Normal": 2, "Low": 3, "Minimal": 4}


def _urgency(facts: TaskFacts, today: date) -> tuple:
    """Within a project, what a member should look at first (MIP-1417). Overdue tasks come first,
    the most important priority first and, at the same priority, the longest overdue first: a Low
    task that has been late for a month is not ahead of a Critical one that went late yesterday.
    Then what is due soonest (the priority breaks a tie), tasks with no due date last, then by
    display id (the number: every task of a group has the same project code)."""
    due = facts.due_date
    rank = _PRIORITY_RANK.get(facts.priority, 2)
    number = facts.number or 0
    if due is not None and due < today and facts.status not in ("Closed", "Deleted"):
        return (0, rank, due, number, facts.task_id)
    return (1, due or date.max, rank, number, facts.task_id)


def _typed_fields(facts: TaskFacts, today: date) -> dict[str, Any]:
    """The fields a card draws a task with, from the task's own row: its id, title, status,
    priority and due date, and how many days overdue it is."""
    out: dict[str, Any] = {
        "display_id": (
            f"{facts.project_code}-{facts.number}"
            if facts.project_code and facts.number is not None
            else f"#{facts.task_id}"
        )
    }
    if facts.title:
        out["title"] = clip(facts.title)
    if facts.status:
        out["status"] = facts.status
    if facts.priority:
        out["priority"] = facts.priority
    if facts.due_date is not None:
        out["due_date"] = facts.due_date.isoformat()
        if facts.due_date < today and facts.status not in ("Closed", "Deleted"):
            out["overdue_days"] = (today - facts.due_date).days
    return out


def arrange(items: list[ReportItem], today: date) -> list[ReportItem]:
    """Group a section's TASK items by project and sort inside each group (MIP-1207).

    The rule, the same in every job: projects by name (case-insensitive, then id); inside a
    project, overdue first (most important priority first, then longest overdue: `_urgency`),
    then by due date, tasks with no due date last, then by display id. Items that are not tasks
    of a known project (to-dos, events, notes, chats, requests) keep their own order and follow
    the project groups. Every item of
    a project is contiguous, so a card can draw a header where `project` changes. Call it
    BEFORE appending the closing "and N more" line, which stays last."""
    grouped = [i for i in items if "_facts" in i]
    rest = [i for i in items if "_facts" not in i]
    grouped.sort(
        key=lambda i: (
            i["_facts"].project_name.casefold(),  # type: ignore[typeddict-item]
            i["_facts"].project_id or 0,  # type: ignore[typeddict-item]
            *_urgency(i["_facts"], today),  # type: ignore[typeddict-item]
        )
    )
    for i in grouped:
        i.update(_typed_fields(i.pop("_facts"), today))  # type: ignore[typeddict-item, misc]
    return [*grouped, *rest]


def in_order(items: list[ReportItem], today: date) -> list[ReportItem]:
    """The typed fields of a section's task items, in the order the job gave them (MIP-1236).

    For a section whose ORDER is the point: "Start with" is the briefing's top three by what
    matters to the member (MIP-1087), and grouping it by project would regroup them and lose
    the ranking. Every task still carries its `project`, so a card can name it on the row; the
    card draws no group headers for such a section (it has the key `start_with`)."""
    for i in items:
        if "_facts" in i:
            i.update(_typed_fields(i.pop("_facts"), today))  # type: ignore[typeddict-item, misc]
    return items


def more(count: int, noun: str) -> ReportItem:
    """The closing line of a capped list: how many are not shown."""
    return {"text": f"and {count} more {noun}", "kind": "more"}


def span(hours: int) -> str:
    """A look-back window in words: "24 hours", "7 days", "1 hour". Whole days only from two
    days up, so the default day reads "24 hours" and a week "7 days", never "168 hours" or
    "1 day"."""
    days, rest = divmod(hours, 24)
    if days >= 2 and not rest:
        return f"{days} days"
    return "1 hour" if hours == 1 else f"{hours} hours"


def counted(count: int, one: str, many: str | None = None) -> str:
    """ "1 task", "3 tasks": the number and the noun in the right number."""
    return f"{count} {one if count == 1 else (many or one + 's')}"


def status_line(total: int, breakdown: dict[str, int]) -> str:
    """ "17 tasks: 9 open, 5 wip, 1 blocked, 2 closed": the statuses that have a task, none that
    has not ("0 pending" is noise in a short report)."""
    parts = [
        f"{breakdown[s]} {s.lower()}"
        for s in ("Open", "WIP", "Blocked", "Pending", "Closed")
        if breakdown.get(s)
    ]
    return f"{counted(total, 'task')}: {', '.join(parts)}" if parts else counted(total, "task")


def dedupe_tasks(
    sections: list[ReportSection], priority: tuple[str, ...] = ()
) -> list[ReportSection]:
    """A task is listed once per report (MIP-1417).

    A task that is overdue, was changed by someone and is blocked used to be listed in all three
    sections, so a short report read as a long one. The first section (in `priority` order, by
    section key; sections the tuple does not name come after, in their own order) keeps the row;
    the same task in a later section is dropped. When the kept row has no sentence of its own
    (`detail`) it takes the dropped row's ("commented by mina"), so what the later section
    said is not lost. The sections keep their display order. A capped list's "and N more" line
    is not a task and stays (those tasks are NOT listed anywhere, so the line is still true); a
    section left with nothing goes."""
    rank = {key: n for n, key in enumerate(priority)}
    order = sorted(
        range(len(sections)), key=lambda n: (rank.get(sections[n].get("key", ""), len(rank)), n)
    )
    first: dict[int, ReportItem] = {}
    for n in order:
        kept: list[ReportItem] = []
        for entry in sections[n]["items"]:
            task_id = entry.get("task_id") if entry.get("kind") == "task" else None
            if task_id is None:
                kept.append(entry)
            elif task_id not in first:
                first[task_id] = entry
                kept.append(entry)
            elif entry.get("detail") and not first[task_id].get("detail"):
                first[task_id]["detail"] = entry["detail"]
        sections[n]["items"] = kept
    return [s for s in sections if s["items"]]


@dataclass(frozen=True)
class TaskFacts:
    """What a report knows about a task beyond the briefing's row: where it sits, and what it
    is sorted by. Read once per report, for tasks the briefing already decided the member may
    open."""

    task_id: int
    number: int | None
    project_id: int | None
    project_name: str
    project_code: str
    due_date: date | None
    status: str
    priority: str
    title: str = ""
    # In a sprint that is active now (MIP-1211: with a due date it is always worth a report).
    in_active_sprint: bool = False


class Links:
    """In-app paths for the tasks and notes of one report, resolved in two queries at most
    (the briefing's items name a task or a note but not its project)."""

    def __init__(self, ctx: ToolContext) -> None:
        self._team = ctx.team_id
        self._facts: dict[int, TaskFacts | None] = {}
        # What a report says about each task row it lists (MIP-1211): `why` it is listed, and
        # whether the card may offer the two answers. Set by a job that suppresses repeats.
        self.why: dict[int, str] = {}
        self.can_answer: bool = False
        # One sentence of context per task (MIP-1210), shown the first time the task is drawn
        # and not again: a task in two sections would otherwise say it twice.
        self.ai_sentences: dict[int, str] = {}
        self._task_of_note: dict[int, tuple[int | None, int | None]] = {}

    def preload(self, *, tasks: Iterable[Any] = (), task_notes: Iterable[Any] = ()) -> None:
        wanted = {int(t) for t in tasks if t is not None} - self._facts.keys()
        if wanted:
            found = {
                row["task_id"]: TaskFacts(
                    task_id=row["task_id"],
                    number=row["project_task_number"],
                    project_id=row["project_id"],
                    project_name=row["project__project_name"] or "",
                    project_code=row["project__code"] or "",
                    due_date=row["due_date"],
                    status=row["status"] or "",
                    priority=row["priority"] or "",
                    title=row["title"] or "",
                    in_active_sprint=(
                        row["sprint__status"] == "active" and row["sprint__is_deleted"] is False
                    ),
                )
                for row in TaskMaster.objects.filter(
                    # A task of a soft-deleted project is not linked from a report (MIP-1621, flag
                    # `AGENT_PARITY_FOLLOWUPS`); `Q()` while it is off.
                    live_tasks_q(),
                    task_id__in=wanted,
                    team_id=self._team,
                    is_deleted=False,
                ).values(
                    "task_id",
                    "project_task_number",
                    "project_id",
                    "project__project_name",
                    "project__code",
                    "due_date",
                    "status",
                    "priority",
                    "title",
                    "sprint__status",
                    "sprint__is_deleted",
                )
            }
            for task_id in wanted:
                self._facts[task_id] = found.get(task_id)
        notes = {int(n) for n in task_notes if n is not None} - self._task_of_note.keys()
        if notes:
            found_notes = {
                note_id: (project_id, task_id)
                for note_id, project_id, task_id in TaskNoteMaster.objects.filter(
                    note_id__in=notes, team_id=self._team
                ).values_list("note_id", "project_id", "task_id")
            }
            for note_id in notes:
                self._task_of_note[note_id] = found_notes.get(note_id, (None, None))

    def facts(self, task_id: Any) -> TaskFacts | None:
        return self._facts.get(int(task_id)) if task_id is not None else None

    def task(self, task_id: Any) -> str | None:
        facts = self.facts(task_id)
        if facts is None or not facts.project_id:
            return None
        return f"/workspace/tasks/project/{facts.project_id}/task/{int(task_id)}"

    def task_item(self, text: str, task_id: Any, detail: str | None = None) -> ReportItem:
        """A report line about a task: its link, its project, and what `arrange` sorts it by
        and turns into typed fields (the private `_facts`, removed by `arrange`). `detail` is
        the sentence about the task that is not in its fields (what changed, why it is here)."""
        out = item(text, self.task(task_id), "task")
        facts_known = self.facts(task_id)
        if facts_known is not None and facts_known.project_id:
            # The id the card answers by (a display id changes when a task moves project).
            out["task_id"] = facts_known.task_id
            if facts_known.task_id in self.why:
                out["why"] = self.why[facts_known.task_id]
            if self.can_answer:
                out["can_answer"] = True
        # The sentence goes in `detail` ONLY, never in `text`: the in-app model is shown `text`
        # (`list_my_inbox._model_body`) and not `detail`, so it never sees model-written lines.
        # It comes first; the deterministic reason, if any, follows it.
        sentence = self._take_sentence(task_id)
        if sentence:
            detail = f"{sentence} · {detail}" if detail else sentence
        if detail:
            out["detail"] = clip(detail)
        facts = self.facts(task_id)
        if facts is not None and facts.project_id and facts.project_name:
            project: dict[str, Any] = {"id": facts.project_id, "name": facts.project_name}
            if facts.project_code:
                project["code"] = facts.project_code
            out["project"] = project  # type: ignore[typeddict-item]
            out["_facts"] = facts  # type: ignore[typeddict-unknown-key]
        return out

    def _take_sentence(self, task_id: Any) -> str:
        try:
            return self.ai_sentences.pop(int(task_id), "") if task_id is not None else ""
        except (TypeError, ValueError):
            return ""

    def note(self, note_type: Any, note_id: Any) -> str | None:
        if note_id is None:
            return None
        if note_type == "personal":
            return f"/workspace/notes/my/{int(note_id)}"
        if note_type == "task":
            project_id, task_id = self._task_of_note.get(int(note_id), (None, None))
            if project_id and task_id:
                return (
                    f"/workspace/notes/task/project/{project_id}/task/{task_id}/note/{int(note_id)}"
                )
        return None  # a chat's note has no stable link

    @staticmethod
    def project(project_id: Any) -> str | None:
        return f"/workspace/tasks/project/{int(project_id)}" if project_id else None

    @staticmethod
    def milestone(project_id: Any, milestone_id: Any) -> str | None:
        if not project_id or not milestone_id:
            return None
        return f"/workspace/tasks/project/{int(project_id)}/milestone/{int(milestone_id)}"

    @staticmethod
    def chat(chat_type: Any, chat_id: Any, thread_id: Any = None) -> str | None:
        if chat_type not in ("dm", "gm", "pm", "mdm") or not chat_id:
            return None
        path = f"/workspace/chat/{chat_type}/{chat_id}"
        return f"{path}/thread/{thread_id}" if thread_id else path

    @staticmethod
    def todo(local_date: Any, item_id: Any) -> str | None:
        if not local_date or item_id is None:
            return None
        return f"/workspace/todo/{local_date}/item/{int(item_id)}"
