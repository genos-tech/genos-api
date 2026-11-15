"""A task or a milestone as the context of an ask, and of the conversation that follows (MIP-1057).

"Ask Genos" on a task or milestone used to be a mention pre-filled into the one app-level ask, so what was
asked about a task lived in the Cmd-K session and not with the task. Now the ask carries
`task_context: {"task_id": N}` and the conversation belongs to that object: the session is bound to it
(`AgentSession.task_id`), reopening restores it (`AgentTaskSessionView`), and this module is the other half:
what the model is told about the object, and who may be told.

Accepted only while `AGENT_PARITY_TOOLS` is on (the shared flag of the MIP-578 batch). With it off the field is
ignored exactly as an unknown one always was, and nothing here runs.

**One id.** `task_id` is `TaskMaster.task_id`. A milestone is its BACKING task row (what `askGenosRefForTask`
already sends and every agent surface already uses), so there is no second id to keep in step, and the
conversation survives turning a task into a milestone and back (MIP-1042 keeps the row). Whether it is a
milestone is read from the row, never taken from the client.

**Who may read it.** `read_task` is the one rule, used by the ask, by the restore endpoint and by the leg
resumed after an approval, and it is the rule `fetch_task` applies, no wider: the task exists and is not deleted,
it is in the team the request names, its project is not deleted, and the asker is in `task_acl_user_ids` (a
project member, or the assignee or reporter while still on the team). A milestone is also held to what the
milestone endpoints hold it to: the asker is on its project. A collaborator who is none of those cannot open the
task (MIP-1567), so cannot ask about it. A task that does not exist, one that was deleted, one in another team and
one the asker may not read all give `None`: the same answer, so nothing here says which of them it was.

**What reaches the model.** One block per ask, in the per-ask `system_extra` (the slot a thread or note summary
takes; never `AGENT_SYSTEM_PROMPT` or a tool declaration, so the cached prefix does not move). Everything a person
wrote (the title, the names, the status words, the start of the description) sits inside ONE
`<workspace_content>` fence, which the prompt's rule 5 already says is data. Outside the fence there are only
integers the server read and ISO dates it formatted. The text is cleaned, then cut, then fenced, in the order
`ask_refs` pins (see its docstring for the characters dropped and why). The client's own title never reaches the
prompt: the block is built from the row.

**After an approval.** The paused run keeps the ID, never the text (`resume_object`); `/decide/` reads the task
again as the asker may read it THEN (`rebuild_block`), so a task deleted or lost to them while the card waited is
not replayed, a change made to it meanwhile is in the block, and the run row holds nothing of the task. The
approved write itself runs AFTER the block is read (as it does for every block of the resumed leg), so what it did
reaches the model through the tool result that follows, not through the block; the next ask of the conversation
reads the task afresh.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from origin.search_engine.agent.ask_refs import _DATA_RULE, RAW_WINDOW, clean_label, clean_text
from origin.search_engine.agent.fence import WORKSPACE_TAG, wrap

log = logging.getLogger(__name__)

#: How much of the description the block carries. About one screen: enough to answer "what is this about" and
#: "what is left" without a tool call, and far less than the tool reads (`fetch_task` has the whole body). In
#: characters, so a Japanese description costs about one token each: the worst case is a thousand or so tokens.
MAX_BODY_CHARS = 1_500

#: Milestone assignees named in a milestone's block (the rest are counted).
MAX_ASSIGNEES = 5

#: Status, priority and effort are short words from a fixed vocabulary in the app, but the column is free text.
_MAX_WORD_CHARS = 60

#: What a malformed `task_context` is refused with.
MALFORMED = "task_context must have an integer task_id."

#: What every way the object cannot be read is answered with: a missing task, a deleted one, one in another
#: team, one the asker may not read, a team the asker is not in. One text, so none of them can be told apart.
NOT_FOUND = "Task not found."


@dataclass(frozen=True)
class TaskRead:
    """One task or milestone, read for an ask. Content: never persisted, never logged."""

    task_id: int
    #: "task" or "milestone"
    kind: str
    project_id: int | None
    #: The milestone this row IS (a live backing row), or None. Milestone tools take this id, not `task_id`.
    milestone_id: int | None
    parent_task_id: int | None
    due_date: str | None
    start_date: str | None
    # Cleaned and cut, not escaped (`wrap` escapes):
    display_id: str
    title: str
    status: str
    priority: str
    effort: str
    people: tuple[tuple[str, str], ...]  # (role, name)
    people_more: int
    body: str
    body_cut: bool


def valid_task_id(raw: Any) -> int | None:
    """`raw` as a task id (a positive integer that fits the column), or None. A bool is not one."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        # Plain ASCII digits only: `int()` would also take "+5", "1_0" and other scripts' digits, and a
        # task would then be reachable under several spellings.
        text = raw.strip()
        if not (text.isascii() and text.isdigit()) or len(text) > 19:
            return None
        value = int(text)
    elif isinstance(raw, int):
        value = raw
    else:
        return None
    return value if 0 < value < 2**63 else None


def parse_context(raw: Any) -> dict[str, int] | None:
    """`{"task_id": N}` from the request's `task_context`, or None when it is not one (the caller says 400)."""
    if not isinstance(raw, dict):
        return None
    task_id = valid_task_id(raw.get("task_id"))
    return {"task_id": task_id} if task_id is not None else None


def _word(value: Any) -> str:
    return clean_label(value)[:_MAX_WORD_CHARS].rstrip()


def _cut(raw: str) -> tuple[str, bool]:
    """`(text, was_cut)`: `raw` cleaned and cut to `MAX_BODY_CHARS`, never escaped."""
    clean = clean_text(raw[:RAW_WINDOW]).strip()
    return clean[:MAX_BODY_CHARS].rstrip(), len(raw) > RAW_WINDOW or len(clean) > MAX_BODY_CHARS


def _readable(task_id: int, team_id: str, user_id: str):
    """`(task, milestone_id)` when `user_id` may read the task in `team_id`, else None. The rule, once."""
    from origin.models.task.milestone_models import MilestoneMaster  # noqa: PLC0415
    from origin.models.task.task_models import TaskMaster  # noqa: PLC0415
    from origin.search_engine.agent.acl import task_acl_user_ids  # noqa: PLC0415
    from origin.views.utils.scope_guards import is_project_member  # noqa: PLC0415

    task = TaskMaster.objects.select_related("project").filter(task_id=task_id).first()
    if task is None or task.is_deleted:
        return None
    # The tenant first: a task of another team is not described, deleted or not.
    if not team_id or str(getattr(task, "team_id", "") or "") != team_id:
        return None
    if task.project_id is not None and (task.project is None or task.project.is_deleted):
        return None
    if user_id not in task_acl_user_ids(task.project_id, task.assignee_id, task.reporter_id):
        return None
    milestone_id = None
    if task.is_milestone:
        milestone_id = (
            MilestoneMaster.objects.filter(task_id=task.task_id, is_deleted=False)
            .values_list("milestone_id", flat=True)
            .first()
        )
        # The milestone endpoints are for the project's members (`MilestoneView.get`), a narrower
        # set than the task's: its assignee or reporter off the project may open the row as a task.
        if milestone_id is not None and not is_project_member(task.project_id, user_id):
            return None
    return task, milestone_id


def can_read(task_id: Any, team_id: Any, user_id: Any) -> bool:
    """Whether `user_id` may read the task in `team_id`: the check of `read_task`, without reading it."""
    wanted = valid_task_id(task_id)
    team_id, user_id = str(team_id or ""), str(user_id or "")
    return wanted is not None and bool(user_id) and _readable(wanted, team_id, user_id) is not None


def read_task(task_id: Any, team_id: Any, user_id: Any) -> TaskRead | None:
    """The task `task_id` as `user_id` may read it while working in `team_id`, or None.

    None for a task that does not exist, one that was deleted, one in another team, one in a deleted project
    and one the asker may not read (see the module docstring): all alike."""
    from origin.models.task.milestone_models import MilestoneAssignees  # noqa: PLC0415
    from origin.search_engine.agent.label_acl import LabelAccess  # noqa: PLC0415
    from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415
    from origin.search_engine.agent.tools.blocknote_render import blocks_to_markdown  # noqa: PLC0415
    from origin.search_engine.agent.tools.template_prompts import (  # noqa: PLC0415
        without_untouched_prompts,
    )
    from origin.search_engine.text_extraction import extract_text  # noqa: PLC0415

    wanted = valid_task_id(task_id)
    team_id, user_id = str(team_id or ""), str(user_id or "")
    if wanted is None or not user_id:
        return None
    found = _readable(wanted, team_id, user_id)
    if found is None:
        return None
    task, milestone_id = found

    # Template prompts nobody filled in are instructions, not content (MIP-825); the same body `fetch_task` reads.
    body = without_untouched_prompts(task.content)
    text, cut = _cut(blocks_to_markdown(body) or extract_text(body))

    # The people, named as `get_team_members` would name them to this asker (a guest sees fewer): an id the
    # asker may not see a name for is simply not named.
    roles: list[tuple[str, str]] = []
    more = 0
    if milestone_id is not None:
        ids = [
            str(uid)
            for uid in MilestoneAssignees.objects.filter(milestone_id=milestone_id)
            .order_by("ts_created_at", "id")
            .values_list("user_id", flat=True)
        ]
        roles = [("Assignee", uid) for uid in ids[:MAX_ASSIGNEES]]
        more = max(len(ids) - MAX_ASSIGNEES, 0)
    elif task.assignee_id:
        roles = [("Assignee", str(task.assignee_id))]
    if task.reporter_id:
        roles.append(("Reporter", str(task.reporter_id)))
    names = LabelAccess(ToolContext(team_id=team_id, user_id=user_id)).usernames(
        [uid for _, uid in roles]
    )
    people = tuple((role, _word(names[uid])) for role, uid in roles if names.get(uid))

    return TaskRead(
        task_id=task.task_id,
        kind="milestone" if milestone_id is not None else "task",
        project_id=task.project_id,
        milestone_id=milestone_id,
        parent_task_id=task.parent_task_id,
        due_date=task.due_date.isoformat() if task.due_date else None,
        start_date=task.start_date.isoformat() if task.start_date else None,
        display_id=_word(task.display_id),
        title=clean_label(task.title),
        status=_word(task.status),
        priority=_word(task.priority),
        effort=_word(task.effort_level),
        people=people,
        people_more=more,
        body=text,
        body_cut=cut,
    )


def build_block(t: TaskRead) -> str:
    """The `system_extra` block for the task or milestone an ask is about."""
    kind = t.kind
    facts = [f"  - task_id: {t.task_id}"]
    if t.milestone_id is not None:
        facts.append(
            f"  - milestone_id: {t.milestone_id} (this row is the milestone's own; the milestone tools "
            "take milestone_id, never the task_id)"
        )
    if t.project_id is not None:
        facts.append(f"  - project_id: {t.project_id}")
    if t.parent_task_id is not None:
        facts.append(f"  - parent_task_id: {t.parent_task_id}")
    if t.start_date:
        facts.append(f"  - start_date: {t.start_date}")
    if t.due_date:
        facts.append(f"  - due_date: {t.due_date}")
    lines = [f"Display id: {t.display_id}", f"Title: {t.title}"]
    for label, value in (
        ("Status", t.status),
        ("Priority", t.priority),
        ("Effort", t.effort),
    ):
        if value:
            lines.append(f"{label}: {value}")
    lines.extend(f"{role}: {name}" for role, name in t.people)
    # What was left out is said OUTSIDE the fence, in server text the description cannot fake or hide.
    if t.people_more:
        facts.append(f"  - assignees: {t.people_more} more are not named here")
    if t.body_cut:
        facts.append(
            f"  - description: cut after {MAX_BODY_CHARS} characters [truncated]; the rest is not shown"
        )
    lines.append(f"Description:\n{t.body}" if t.body else "Description: none")
    fenced = wrap(WORKSPACE_TAG, "\n".join(lines))
    steer = (
        f"For the work inside it use get_milestone_summary(milestone_id={t.milestone_id}) and "
        f"list_tasks(milestone_id={t.milestone_id}); fetch_task(task_id={t.task_id}) reads the milestone's own row. "
        if t.milestone_id is not None
        else f"fetch_task(task_id={t.task_id}) reads the rest of it. "
    )
    return (
        f"THE {kind.upper()} THIS CONVERSATION IS ABOUT\n"
        f"The user opened Ask Genos from one {kind}, and this conversation belongs to it: a question that "
        f"does not name something else is about this {kind}. The server read it with that user's own access, "
        f"so it is one they can open.\n" + "\n".join(facts) + "\n"
        "The display id, the title, the status words, the names and the start of the description are "
        f"between the markers. {_DATA_RULE}\n"
        f"{fenced}\n"
        f"Answer from this block when it holds the answer, without a tool call. Call a tool only for what it "
        f"does not hold (the comments, the deliveries, the sub-tasks, the rest of the description): {steer}"
        f'Refer to it as "this {kind}" in prose rather than emitting a citation for it; cite OTHER entities as '
        "usual."
    )


def seed_source(t: TaskRead) -> dict[str, Any]:
    """The source chip of the object, so a stray inline citation of it still resolves to a clickable label."""
    from origin.search_engine.agent.controller import _task_source  # noqa: PLC0415

    return _task_source(t.task_id, t.title, t.project_id, t.display_id)


def resume_object(t: TaskRead) -> dict[str, Any]:
    """What a paused run keeps of the object: its id and nothing else (see the module docstring)."""
    return {"task_id": t.task_id}


def rebuild_block(kept: Any, *, team_id: str, user_id: str) -> str | None:
    """The block for the leg resumed after an approval: the task read AGAIN, as the asker may read it now.

    None when it is gone or no longer theirs (no block, as for a message `ask_refs.rebuild_block` cannot read),
    when the run kept none, and while the flag is off, so a rollback takes effect on a run already paused."""
    from origin.search_engine.agent.parity import parity_on  # noqa: PLC0415

    if not parity_on() or not isinstance(kept, dict):
        return None
    read = read_task(kept.get("task_id"), team_id, user_id)
    return build_block(read) if read is not None else None
