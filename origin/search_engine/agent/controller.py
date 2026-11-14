"""Multi-step agent loop, with Phase 7 pause/resume for write tools.

Two entry points share the same per-step loop body:

  * `run_agent(query, ctx, emit, run_id=...)` — fresh run from a user
    query. Returns `None` on a clean finish, or a dict
    `{"paused": True, "approval_token": UUID, ...}` when the loop hit
    a `requires_approval` tool. The caller (view layer) is expected
    to write the token back onto `AgentRun.pending_approval_token`
    and flip `AgentRun.status` to `"awaiting_approval"`.

  * `resume_agent(run, decision, ctx, emit)` — resume a paused run.
    `decision` is `"approve"` or `"reject"`. Reconstructs the
    `messages` list from persisted `AgentStep` rows, executes (or
    rejects) the pending tool, and continues the loop. Same return
    shape as `run_agent` (could pause again on a subsequent write
    tool, though current tools don't chain that way).

Event types emitted (full NDJSON protocol):

  tool_call_start              read-only tool dispatch
  tool_call_result             read-only tool success (+ duration_ms
                               when the tool actually executed — absent
                               on session-cache hits; an approved write
                               the person edited first adds `edited:
                               true` and `edited_arguments`, the
                               {path: value} they typed; an approved
                               write that can be undone adds `undo:
                               {step_id, available_until}` — present
                               only when what undo needs was stored,
                               so it, not `undoable`, decides whether
                               to show Undo: `agent/undo.py`)
  tool_call_error              tool error (incl. user-rejected writes;
                               + duration_ms when a tool ran)
  tool_call_pending_approval   write tool — paused, awaiting user (+
                               `editable`, the argument paths the card
                               may let the person change before
                               approving: `agent/approval_edits.py`;
                               + `undoable`, whether this call could be
                               undone after approval, and when it
                               could not, `undo_reason`, a code:
                               `agent/undo.py`. A prediction)
  sources                      citation chips (after search calls)
  answer_delta                 streaming text from the final answer
  done                         final answer delivered (the streaming
                               adapter adds elapsed_ms — total stream
                               wall time — in agent_views)
  error                        fatal mid-stream
"""

from __future__ import annotations

import dataclasses
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable
from uuid import UUID

from django.conf import settings
from django.db import connections
from django.utils import timezone

from apis.request_id import describe_exception
from origin.search_engine.agent import (
    approval_edits,
    approval_notes,
    approval_reason,
    content_length,
    tool_cache,
    undo,
)
from origin.search_engine.agent.abstention import ABSTAIN_MESSAGE, is_abstention
from origin.search_engine.agent.citation_resolver import (
    canonical_citation_token,
    extract_citation_tokens,
    resolve_unresolved_citations,
)
from origin.search_engine.agent.egress import EgressRefused, check_web_query
from origin.search_engine.agent.fence import unwrap
from origin.search_engine.agent.label_acl import LabelAccess
from origin.search_engine.agent.languages import language_directive
from origin.search_engine.agent.model_fence import fence_for_model
from origin.search_engine.agent.prompts import (
    AGENT_CRITIQUE_RETRIEVAL_DIRECTIVE,
    AGENT_SELF_CRITIQUE_PROMPT_TEMPLATE,
    AGENT_SELF_CRITIQUE_SYSTEM,
    agent_system_prompt,
)
from origin.search_engine.agent.references import declared_schema, resolve_call
from origin.search_engine.agent.tool_flag_text import declared_description
from origin.search_engine.agent.tools import (
    MCP_ONLY_REGISTRY,
    REGISTRY,
    ToolContext,
    ToolError,
    flagged_tool,
    flagged_tools,
)
from origin.search_engine.agent.tools.plan_task_bodies import fill_plan_task_bodies_for_caller
from origin.search_engine.agent.tools.task_custom_fields import (
    project_fields,
    read_custom_fields,
    resolve_field,
    stored_member_ids,
)
from origin.search_engine.agent.tools.task_tags import tag_names
from origin.search_engine.llm import (
    AgentMessage,
    CallUsage,
    FunctionCall,
    GenerationParams,
    ModelRefusal,
    ToolDeclaration,
    get_model_client,
    spend,
)
from origin.search_engine.llm.choice import (
    _server_default_choice,
    active_effort_profile,
    get_llm_choice,
)
from origin.search_engine.llm.spend import spend_purpose
from origin.search_engine.models import AgentLlmCall, AgentRun, AgentStep
from origin.services.user_time import resolve_zone, zone_for_user_id

log = logging.getLogger(__name__)

# Emitted when a run stops on the per-request cost ceiling. A module
# constant because `agent_views` classifies the run's outcome from it —
# a ceiling stop is an APPLICATION failure (our decision, we spent the
# money, the user got nothing usable), NOT a provider failure. Two
# copies of this string would eventually drift and silently start
# blaming the provider for our own ceiling.
COST_CEILING_MESSAGE = (
    "This request grew too large to finish. Try narrowing it to a more specific question."
)

# Emitted when a run stops because the user's AI credits ran out
# mid-request. Classified by `agent_views` the same way the ceiling
# message is — but into a BILLABLE result, because unlike the ceiling
# (our own cap, our own cost) these tokens were spent on work the user
# asked for and can still read: the partial answer stays on screen.
#
# Says what happened, what it cost them, and when it comes back, because
# all three are things the user can act on — unlike the ceiling message,
# which deliberately hides a yen figure nobody agreed to.
CREDITS_EXHAUSTED_MESSAGE = (
    "You've run out of AI credits, so this request stopped partway. "
    "Your credits reset at the start of next month — or upgrade your "
    "plan to keep going now."
)

# Emitted when the provider's safety layer declines the request (Claude
# `stop_reason: "refusal"`, MIP-859). A module constant for the same
# reason as the two above: `agent_views` classifies the run's outcome
# from it, into `safety_refusal`, which is NOT billable (the user got
# nothing, and the credit policy bills only work performed). Says what
# happened and what to do; names no category (it can be absent, and a
# policy label would read as an accusation).
MODEL_REFUSAL_MESSAGE = (
    "The AI model declined to answer this request. "
    "Try rephrasing it or asking about something more specific."
)

# Emitted when the model's reply was cut off at the OUTPUT cap while it was writing a tool call
# that needs approval (any content write: a task, note, milestone, plan or comment body), and no
# retry with more room could fit either (MIP-905). Showing the half-written body on an approval
# card would let the person approve a truncated note, so nothing is proposed. A module constant
# for the same reason as the messages above: `agent_views` classifies the run's outcome from it
# (we spent the money, they got nothing).
OUTPUT_TRUNCATED_MESSAGE = (
    "That was too long to write in one go, so nothing was proposed. "
    "Ask for it in smaller parts (for example a few tasks at a time, or one section at a time), "
    "or ask for a shorter version."
)

# Appended to a TEXT reply the provider cut off at the output cap (MIP-905), so the person is not
# left reading a sentence that stops for no reason. Worded for both cases the loop cannot tell
# apart on every provider (Gemini may drop a half-written function call and return only the text
# before it): a cut-off answer, or a cut-off request to write something.
ANSWER_CUT_OFF_NOTICE = (
    "\n\n*(This reply was cut off because it reached the length limit. Ask me to continue, "
    "or, if you asked me to write something, ask for it in smaller parts.)*"
)

#: A cut-off write is retried once with more room: twice the cap the call ran under (or twice what
#: it produced, thinking included, if that is more), at least FLOOR and at most CEILING tokens. No
#: retry when that would not exceed the cap the first call ran under, and none when the call ran
#: with no cap at all (the provider's own model maximum already applied).
TRUNCATION_RETRY_FLOOR = 16384
TRUNCATION_RETRY_CEILING = 32768

# Appended as a final user turn when the loop exhausts its step budget
# without ever writing a final answer (AGENT_STEP_CAP_WRAPUP). By that
# point the transcript holds every tool result the run gathered, so the
# directive forces one last TOOL-LESS synthesis over that material
# instead of discarding it behind an error the user can't act on.
STEP_CAP_WRAPUP_DIRECTIVE = (
    "You have used the entire tool budget for this request; no further "
    "tool calls are possible. Using ONLY the information already "
    "gathered above, write your best final answer to the user's "
    "question now. Cite sources with [type:id] tokens where you have "
    "them. If the gathered information is incomplete, answer what you "
    "can and say clearly which parts you could not verify."
)

# Marker stored in AgentStep.summary while a write tool is awaiting the
# user's decision. The resume path uses it to locate the pending row.
PENDING_APPROVAL_MARKER = "awaiting_approval"

# Decision strings accepted by `resume_agent`.
DECISION_APPROVE = "approve"
DECISION_REJECT = "reject"


def _persist_step(run_id: UUID | None, **fields: Any) -> AgentStep | None:
    """Best-effort write of one `AgentStep` row.

    Observability must NEVER break the user-facing path — if the DB
    insert fails for any reason, log it and move on. The agent stream
    completes regardless. Returns the saved row (or None if persistence
    is disabled / failed) so the pause path can update it later.
    """
    if run_id is None:
        return None
    try:
        return AgentStep.objects.create(run_id=run_id, **fields)
    except Exception:  # noqa: BLE001 — must not fail the response stream
        log.exception("Failed to persist AgentStep for run %s", run_id)
        return None


def _persist_llm_call(
    run_id: UUID | None,
    team_id: str,
    *,
    step_index: int,
    purpose: str,
    latency_ms: int,
    usage: CallUsage,
) -> None:
    """Best-effort write of one `AgentLlmCall` telemetry row.

    Same contract as `_persist_step`: observability must NEVER break the
    user-facing stream, so a failed insert is logged and swallowed.
    No-ops when there's no run (eval / test / sessionless callers) or
    when metric collection is turned off via
    `SEARCH_ENGINE["AGENT_COLLECT_METRICS"]` (default on) — the flag lets
    an operator kill the writes without a deploy.
    """
    if run_id is None:
        return
    if not settings.SEARCH_ENGINE.get("AGENT_COLLECT_METRICS", True):
        return
    try:
        AgentLlmCall.objects.create(
            run_id=run_id,
            team_id=team_id or "",
            step_index=step_index,
            purpose=purpose,
            provider=usage.provider,
            model=usage.model,
            latency_ms=latency_ms,
            prompt_tokens=usage.prompt_tokens,
            cached_tokens=usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            output_tokens=usage.output_tokens,
            thought_tokens=usage.thought_tokens,
            tool_prompt_tokens=usage.tool_prompt_tokens,
            total_tokens=usage.total_tokens,
        )
    except Exception:  # noqa: BLE001 — must not fail the response stream
        log.exception("Failed to persist AgentLlmCall for run %s", run_id)


# One-shot latch so the kill-switch state is logged once per worker
# process (visible near startup) instead of spamming every request.
_KILLSWITCH_LOGGED = False


def _operator_disabled_tools() -> set[str]:
    """Resolve the `AGENT_DISABLED_TOOLS` ops kill-switch (+ log once).

    FAIL-OPEN: unset/empty disables nothing — env vars are per-service /
    per-environment config, so a service that misses the var must run at
    full capability rather than silently losing tools (see settings.py
    and SPOTLIGHT_AGENT_CHANGE_SAFETY.md §4.4). Unknown names disable
    nothing and are logged at ERROR: the operator believes something is
    switched off that isn't — exactly a typo's failure mode.
    """
    global _KILLSWITCH_LOGGED
    configured = settings.SEARCH_ENGINE.get("AGENT_DISABLED_TOOLS") or frozenset()
    if not configured:
        return set()
    # An MCP-only tool is not in REGISTRY but is a real name here: MCP honours the
    # same switch, and naming one must not read as a typo (MIP-1070).
    known = set(configured) & (set(REGISTRY) | set(MCP_ONLY_REGISTRY))
    if not _KILLSWITCH_LOGGED:
        _KILLSWITCH_LOGGED = True
        log.warning(
            "AGENT_DISABLED_TOOLS active — tools hidden from the agent: %s",
            sorted(known),
        )
        unknown = set(configured) - known
        if unknown:
            log.error(
                "AGENT_DISABLED_TOOLS names unknown tool(s) %s — probable "
                "typo; they disable nothing",
                sorted(unknown),
            )
    return known


def _app_tool(name: str) -> Any:
    """The tool the loop runs for the name the MODEL sent: a registered one, or a flag-gated
    in-app tool whose flag is on (MIP-1289). Looks in this module's `REGISTRY`, as every lookup
    here did, so a test that patches it keeps working."""
    tool = REGISTRY.get(name)
    return tool if tool is not None else flagged_tool(name)


def _build_tool_declarations(
    disabled_tools: set[str] | None = None,
) -> list[ToolDeclaration]:
    """Translate each registered Tool into a provider-neutral declaration.

    Tools whose name appears in `disabled_tools` are omitted from the
    list, so the model never even sees them as callable. Used to honour
    the frontend "Web search" toggle (filters out `search_web`) and §4.5
    tool subsetting; the `AGENT_DISABLED_TOOLS` ops kill-switch is
    unioned in here — the single choke point every declaration build
    passes through. Note this hides tools from NEW model turns; a write
    tool already paused for approval still resumes if the user approves.
    """
    disabled = (disabled_tools or set()) | _operator_disabled_tools()
    # `flagged_tools()` is empty unless a flag-gated in-app tool's flag is on (MIP-1289): the
    # list is then exactly what it was, and the flagged tools come after the registered ones.
    return [
        ToolDeclaration(
            name=t.name,
            description=declared_description(t),
            parameters_schema=declared_schema(t.name, t.parameters_schema),
        )
        for t in (*REGISTRY.values(), *flagged_tools())
        if t.name not in disabled
    ]


# Tool arguments that are raw DB primary keys. The agent emits these
# verbatim in `tool_call_start` / `tool_call_pending_approval` events,
# where they'd surface in the UI's approval card and activity strip as
# meaningless numbers ("project_id: 46"). `_friendly_arguments` swaps
# them for human-readable labels before emission. The raw values stay
# in the persisted `AgentStep.arguments_json` row so the resume path
# still re-runs the tool with the canonical primary key.
#
# The ids come from the model, so every label is looked up through
# `LabelAccess` (MIP-842): a name is printed only when the caller may read
# the object, by the same rule as the matching read tool. Anything else —
# unreadable, another team's, or a miss — stays the raw id.
def _resolve_task_display_id(raw: Any, access: LabelAccess) -> str | None:
    """`TaskMaster.display_id` ("WRD-5") for a raw task primary key."""
    return access.task_display_id(raw)


def _resolve_project_name(raw: Any, access: LabelAccess) -> str | None:
    return access.project_name(raw)


def _resolve_user_name(raw: Any, access: LabelAccess) -> str | None:
    return access.username(raw) if raw else None


def _resolve_user_names(raw: Any, access: LabelAccess) -> list[Any] | None:
    """`member_ids`: a list of people, one query. Each id the caller may see
    becomes a name; every other entry stays as the model wrote it."""
    if not isinstance(raw, list):
        return None
    names = access.usernames(raw)
    return [names.get(str(item), item) for item in raw]


def _resolve_join_request(raw: Any, access: LabelAccess) -> str | None:
    """`inbox_item_id` of a project join request: who is asking to join what."""
    return access.join_request_label(raw)


def _resolve_label_names(raw: Any, access: LabelAccess) -> list[Any] | None:
    """`label_ids`: a list of project labels, one query. Each id the caller may see
    becomes a name; every other entry stays as the model wrote it."""
    if not isinstance(raw, list):
        return None
    names = access.project_label_names(raw)
    return [names.get(_label_key(item), item) for item in raw]


def _label_key(item: Any) -> int | None:
    if isinstance(item, bool):  # `True` is not label 1
        return None
    try:
        return int(item)
    except (TypeError, ValueError):
        return None


def _resolve_todo_item_title(raw: Any, access: LabelAccess) -> str | None:
    """`ToDoItem.title` for a raw item primary key so the approval card
    surfaces the human-readable todo instead of "73". None when the id
    doesn't resolve (e.g. a non-todo tool happens to use an `item_id` arg
    too — the raw value is shown).
    """
    return access.todo_title(raw)


def _resolve_milestone_title(raw: Any, access: LabelAccess) -> str | None:
    return access.milestone_title(raw)


def _resolve_note_folder_name(raw: Any, access: LabelAccess) -> str | None:
    return access.folder_name(raw)


def _resolve_milestone_folder_name(raw: Any, access: LabelAccess) -> str | None:
    return access.milestone_folder_name(raw)


def _resolve_sprint_name(raw: Any, access: LabelAccess) -> str | None:
    return access.sprint_name(raw)


# Argument-key → resolver. Resolvers return None on a miss so we fall
# back to the raw value (the user sees the ID rather than a blank).
_FRIENDLY_ARG_RESOLVERS: dict[str, Callable[[Any, LabelAccess], str | None]] = {
    "task_id": _resolve_task_display_id,
    "project_id": _resolve_project_name,
    "assignee_id": _resolve_user_name,
    "default_assignee_id": _resolve_user_name,
    "reporter_id": _resolve_user_name,
    "new_assignee_id": _resolve_user_name,
    # The project tools: the person being added, and the people a new project
    # starts with. (Only the project tools take either argument.)
    "user_id": _resolve_user_name,
    "member_ids": _resolve_user_names,
    # `manage_project_labels`: the labels it puts on a project or takes off.
    "label_ids": _resolve_label_names,
    # `approve_join_request` / `decline_join_request`. Not `item_id`: that name is a to-do.
    "inbox_item_id": _resolve_join_request,
    "item_id": _resolve_todo_item_title,
    "parent_item_id": _resolve_todo_item_title,
    "milestone_id": _resolve_milestone_title,
    "existing_milestone_id": _resolve_milestone_title,
    "parent_task_id": _resolve_task_display_id,
    # Only the note tools take `folder_id` (My-Notes sidebar folders).
    "folder_id": _resolve_note_folder_name,
    # `manage_note_folder` nests a new folder inside this one.
    "parent_folder_id": _resolve_note_folder_name,
    # A milestone's folder is another table; its argument has its own name so
    # the note-folder resolver above can never label it (MIP-879).
    "milestone_folder_id": _resolve_milestone_folder_name,
    "sprint_id": _resolve_sprint_name,
}


def _friendly_task_plan_arguments(
    out: dict[str, Any], _args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `create_task_plan` arguments.

    The flat resolver pass above only touches top-level keys; a plan's
    assignees live inside `tasks[i].assignee_id` and
    `milestone.assignee_ids`. Resolve them all with ONE batched user
    query so the approval card shows usernames, not UUIDs. Only people
    the caller may see by name get a label. Structure is otherwise
    preserved — the frontend's structured preview renders the same shape
    the model proposed.
    """
    # A task proposed without a body shows (and gets) the project's template body (MIP-1510):
    # the same text `create_task_plan` fills in when it runs. Read from the model's own `_args`
    # (`out` has labels where ids were), and before the assignee pass below, which returns early
    # when no one is named.
    filled = fill_plan_task_bodies_for_caller(_args, access.team_id, access.user_id)
    if filled is not _args:
        out["tasks"] = filled["tasks"]

    tasks = out.get("tasks") if isinstance(out.get("tasks"), list) else []
    milestone = out.get("milestone") if isinstance(out.get("milestone"), dict) else None

    ids: set[str] = set()
    for t in tasks:
        if isinstance(t, dict) and t.get("assignee_id"):
            ids.add(str(t["assignee_id"]))
    if milestone:
        for uid in milestone.get("assignee_ids") or []:
            if uid:
                ids.add(str(uid))
    if not ids:
        return

    names = access.usernames(ids)
    new_tasks = []
    for t in tasks:
        if isinstance(t, dict) and t.get("assignee_id"):
            t = {**t, "assignee_id": names.get(str(t["assignee_id"]), t["assignee_id"])}
        new_tasks.append(t)
    out["tasks"] = new_tasks
    if milestone and milestone.get("assignee_ids"):
        out["milestone"] = {
            **milestone,
            "assignee_ids": [names.get(str(uid), uid) for uid in milestone["assignee_ids"] if uid],
        }


def _friendly_bulk_update_arguments(
    out: dict[str, Any], _args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `update_tasks_bulk` arguments.

    Each update row gains `display_id` / `title` and a `current` snapshot
    of the fields the tool can change (one batched query for the whole
    batch), so the approval card can render a true old→new diff table.
    A milestone move is labelled on both sides: the proposed
    `milestone_id` becomes the target's title and `current.milestone_id`
    holds the present one (None = in no milestone), keyed alike so the
    card diffs it like any other field. The other proposed values and
    `task_id` stay untouched — the persisted `arguments_json` is raw
    anyway; this only shapes the wire event.

    A row for a task the caller can't read is left exactly as proposed:
    no title, no snapshot. The same goes for a milestone they can't read.
    """
    updates = out.get("updates")
    if not isinstance(updates, list) or not updates:
        return

    ids = [row.get("task_id") for row in updates if isinstance(row, dict)]
    by_id = access.tasks(ids)
    targets = access.milestone_titles(
        row.get("milestone_id") for row in updates if isinstance(row, dict)
    )
    # Proposed and current assignees, labelled in ONE query (MIP-823). An
    # empty id is "unassign" and stays as sent.
    people: set[str] = set()
    for row in updates:
        if not isinstance(row, dict):
            continue
        if row.get("assignee_id"):
            people.add(str(row["assignee_id"]))
        try:
            task = by_id.get(int(row.get("task_id")))
        except (TypeError, ValueError):
            task = None
        if task is not None and task.assignee_id:
            people.add(str(task.assignee_id))
    names = access.usernames(people) if people else {}
    new_updates = []
    for row in updates:
        if isinstance(row, dict):
            try:
                task = by_id.get(int(row.get("task_id")))
            except (TypeError, ValueError):
                task = None
            if task is not None:
                row = {
                    **row,
                    "display_id": task.display_id,
                    "title": task.title,
                    "current": {
                        "priority": task.priority,
                        "effort_level": task.effort_level,
                        "status": task.status,
                        "due_date": task.due_date.isoformat() if task.due_date else None,
                        "start_date": task.start_date.isoformat() if task.start_date else None,
                        "tags": tag_names(task.tags),
                        "milestone_id": _current_milestone_title(task, access),
                        "assignee_id": (
                            names.get(str(task.assignee_id), str(task.assignee_id))
                            if task.assignee_id
                            else None
                        ),
                    },
                }
            if row.get("assignee_id"):
                row = {**row, "assignee_id": names.get(str(row["assignee_id"]), row["assignee_id"])}
            row = _with_milestone_title(row, targets)
        new_updates.append(row)
    out["updates"] = new_updates


def _current_milestone_title(task: Any, access: LabelAccess) -> Any:
    """The task's present milestone title; None means "in no milestone". When
    the caller can't read that milestone it stays the raw id — a task they
    can see never leaks the name of one they can't, and the card doesn't
    claim the task is in none."""
    if not task.milestone_id or not task.milestone:
        return None
    return task.milestone.title if access.milestone_readable(task.milestone) else task.milestone_id


def _with_milestone_title(row: dict[str, Any], titles: dict[int, str]) -> dict[str, Any]:
    """Label a row's proposed `milestone_id` the way the flat pass labels a
    top-level one. `''` (detach) stays `''` so the card can show it as
    removed; an id that doesn't resolve stays raw."""
    try:
        title = titles.get(int(row.get("milestone_id")))
    except (TypeError, ValueError):
        return row
    return {**row, "milestone_id": title} if title is not None else row


def _current_parent_label(task: Any, access: LabelAccess) -> Any:
    """The task's present parent as a display id; None means "top-level". A
    parent the caller can't read stays the raw id, like the milestone: the
    card neither names it nor claims the task has none."""
    if not task.parent_task_id:
        return None
    label = access.task_display_id(task.parent_task_id)
    return label if label is not None else task.parent_task_id


def _friendly_task_fields(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess, project_id: Any, task: Any
) -> dict[str, Any]:
    """Label the people a task write proposes, and for an existing `task` return
    the present value of each of `start_date`, `collaborators` and
    `custom_fields` the call changes (MIP-878).

    `collaborators` are user ids, and so are the values of member custom
    fields; both become usernames in `out` for the people `LabelAccess` lets the
    caller name, and stay raw ids otherwise. The custom fields are the
    project's, shown by name only to a member of it (the REST list is
    member-gated), so for anyone else they stay as proposed and no `current` is
    added for them. Returns what to merge into `current` (empty for a create).
    """
    proposed_people = args.get("collaborators")
    entries = args.get("custom_fields")
    fields = (
        project_fields(project_id)
        if isinstance(entries, list) and access.project_name(project_id) is not None
        else []
    )
    resolved: list[tuple[Any, Any]] = []
    for entry in entries if isinstance(entries, list) else []:
        try:
            field = (
                resolve_field(entry.get("name"), fields)
                if isinstance(entry, dict) and fields
                else None
            )
        except ToolError:
            field = None
        resolved.append((entry, field))

    wanted: set[str] = set()
    if isinstance(proposed_people, list):
        wanted |= {i for i in proposed_people if isinstance(i, str)}
    for entry, field in resolved:
        if (
            field is not None
            and field.field_type == "member"
            and isinstance(entry.get("value"), str)
        ):
            wanted.add(entry["value"])
    if task is not None:
        wanted |= {str(uid) for uid in task.collaborators.values_list("id", flat=True)}
        wanted |= stored_member_ids(task.custom_field_values, fields)
    names = access.usernames(wanted) if wanted else {}

    def label(uid: Any) -> Any:
        return names.get(str(uid), uid)

    if isinstance(proposed_people, list):
        out["collaborators"] = [label(i) if isinstance(i, str) else i for i in proposed_people]
    if resolved:
        out["custom_fields"] = [
            {**entry, "value": label(entry["value"])}
            if field is not None
            and field.field_type == "member"
            and isinstance(entry.get("value"), str)
            and entry["value"]
            else entry
            for entry, field in resolved
        ]

    current: dict[str, Any] = {}
    if task is None:
        return current
    if args.get("start_date") is not None:
        current["start_date"] = task.start_date.isoformat() if task.start_date else None
    if isinstance(proposed_people, list):
        current["collaborators"] = sorted(
            str(label(str(uid))) for uid in task.collaborators.values_list("id", flat=True)
        )
    if any(field is not None for _entry, field in resolved):
        shown = {
            row["name"]: row
            for row in read_custom_fields(task.custom_field_values, fields, names, fence=False)
        }
        values: dict[str, Any] = {}
        for entry, field in resolved:
            if field is None:
                continue
            row = shown.get(field.field_name)
            if row is None:
                value: Any = None
            elif field.field_type == "tag":
                value = row.get("values")
            else:
                value = row.get("username") or row.get("value")
            values[str(entry.get("name"))] = value
        current["custom_fields"] = values
    return current


def _friendly_update_task_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `update_task` arguments.

    The flat pass already labels the proposed `milestone_id`, `parent_task_id`
    and `project_id`; for each one being changed this ADDS the task's
    present value under `current` (a milestone title, a parent's display id,
    a project name; None = none) so the card can show old → new — the same
    shape a bulk row's `current` carries. The same goes for `start_date`
    (an ISO date or None), `collaborators` (usernames) and `custom_fields`
    (the present value of each field named, by the name the call used); the
    proposed `collaborators` and member-field values are labelled with
    usernames. The flat pass has also replaced `task_id` with a display id, so
    the task is looked up from the raw `args`. Nothing is added for a task the
    caller can't read."""
    moving = [
        k for k in ("milestone_id", "parent_task_id", "project_id") if args.get(k) is not None
    ]
    planning = [
        k for k in ("start_date", "collaborators", "custom_fields") if args.get(k) is not None
    ]
    if not moving and not planning:
        return
    task = access.task(args.get("task_id"))
    if planning:
        current = _friendly_task_fields(
            out, args, access, task.project_id if task is not None else None, task
        )
    else:
        current = {}
    if task is None:
        return
    if "milestone_id" in moving:
        current["milestone_id"] = _current_milestone_title(task, access)
    if "parent_task_id" in moving:
        current["parent_task_id"] = _current_parent_label(task, access)
    if "project_id" in moving:
        name = access.project_name(task.project_id)
        current["project_id"] = name if name is not None else task.project_id
    if current:
        out["current"] = current


def _friendly_create_task_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `create_task` arguments: the proposed
    `collaborators` and member-field values as usernames. There is no present
    value to show, so no `current`."""
    if args.get("collaborators") is None and args.get("custom_fields") is None:
        return
    _friendly_task_fields(out, args, access, args.get("project_id"), None)


def _friendly_update_todo_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `update_todo_item` arguments.

    For what the call changes, ADDS the to-do's present value under
    `current` so the card can show old → new: `target_date` (the day it is on
    now) and `reminder_at` (its pending reminder, None = none, in the
    user's zone with the offset). `time_zone` names the zone a `remind_at`
    will be read in, since the card shows the time as the model wrote it.
    Nothing is added for a to-do that isn't on the caller's own list."""
    from origin.search_engine.agent.tools.todo_args import local_iso  # noqa: PLC0415
    from origin.services import todo_reminders  # noqa: PLC0415
    from origin.services.user_time import zone_for_user_id  # noqa: PLC0415

    item = access.todo_item(args.get("item_id"))
    if item is None:
        return
    zone = zone_for_user_id(access.user_id)
    current: dict[str, Any] = {}
    if args.get("target_date") not in (None, ""):
        current["target_date"] = item.group.local_date.isoformat()
    if (
        args.get("remind_at") not in (None, "")
        or args.get("remind_in_minutes") is not None
        or args.get("cancel_reminder")
    ):
        pending = todo_reminders.pending_by_item([item.item_id]).get(item.item_id)
        current["reminder_at"] = local_iso(pending, zone)
    if args.get("remind_at") not in (None, ""):
        out["time_zone"] = zone.key
    if current:
        out["current"] = current


def _friendly_create_todo_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """ADDS `time_zone` when the new to-do carries a `remind_at`: the zone the
    time will be read in, which the model's own words don't say."""
    from origin.services.user_time import zone_for_user_id  # noqa: PLC0415

    if args.get("remind_at") not in (None, "") and access.usable:
        out["time_zone"] = zone_for_user_id(access.user_id).key


def _friendly_delete_todo_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `delete_todo_item` arguments.

    The flat pass names the to-do; this ADDS what else goes with it, under
    `current`: its day, how many sub-items are deleted along, and its
    pending reminder (None = none), so the card says what the delete takes.
    Nothing is added for a to-do that isn't on the caller's own list."""
    from origin.search_engine.agent.tools.todo_args import local_iso  # noqa: PLC0415
    from origin.services import todo_reminders  # noqa: PLC0415
    from origin.services.user_time import zone_for_user_id  # noqa: PLC0415

    item = access.todo_item(args.get("item_id"))
    if item is None:
        return
    pending = todo_reminders.pending_by_item([item.item_id]).get(item.item_id)
    out["current"] = {
        "target_date": item.group.local_date.isoformat(),
        "sub_items": item.subitems.count(),
        "reminder_at": local_iso(pending, zone_for_user_id(access.user_id)),
    }


def _friendly_update_note_arguments(
    out: dict[str, Any], _args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `update_note` arguments.

    ADDS `note_title` (resolved from the note tables) so the approval
    card can say which note is being edited — only when the caller may
    read that note. `note_id` itself is left numeric — unlike the flat
    resolvers this must not replace the key, because the preview needs
    both the label and the raw id."""
    title = access.note_title(out.get("note_type"), out.get("note_id"))
    if title:
        out["note_title"] = title


def _friendly_create_note_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `create_note` arguments.

    ADDS `chat_name` for a chat note so the approval card can say which
    conversation the note goes on: the chat's title, or for a DM / MDM
    (which have none) its members' names. `chat_id` stays as sent. Only
    for a chat the caller is a member of."""
    if str(args.get("note_type") or "").lower() != "chat" or not args.get("chat_id"):
        return
    from origin.search_engine.agent.tools.base import ToolError  # noqa: PLC0415
    from origin.search_engine.agent.tools.note_targets import parse_chat_ref  # noqa: PLC0415

    try:
        channel_id, _thread = parse_chat_ref(args.get("chat_id"), args.get("thread_id"))
    except ToolError:
        return
    from origin.models.chat.unified_models import ChannelMember  # noqa: PLC0415

    channel = access.readable_channel(channel_id)
    if channel is None:
        return
    name = (channel.title or "").strip()
    if not name:
        names = list(
            ChannelMember.objects.filter(channel_id=channel_id, is_deleted=False)
            .values_list("user__username", flat=True)
            .order_by("user__username")[:6]
        )
        name = ", ".join(n for n in names if n)
    if name:
        out["chat_name"] = name


def _friendly_dependency_arguments(
    out: dict[str, Any], _args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `update_task_dependencies` arguments.

    Each `add` / `remove` edge names two raw task ids below the top level,
    where the flat pass can't reach; label both ends "PRJ-12 Title" (one
    batched query for the whole call) so the approval card says which
    tasks get linked. An id that doesn't resolve, or names a task the
    caller can't read, stays raw."""
    edges = [
        row
        for key in ("add", "remove")
        if isinstance(out.get(key), list)
        for row in out[key]
        if isinstance(row, dict)
    ]
    readable = access.tasks(
        row.get(end) for row in edges for end in ("blocker_task_id", "blocked_task_id")
    )
    if not readable:
        return

    labels = {t.task_id: f"{t.display_id} {t.title}".strip() for t in readable.values()}

    def _label(raw: Any) -> Any:
        try:
            return labels.get(int(raw), raw)
        except (TypeError, ValueError):
            return raw

    for key in ("add", "remove"):
        if isinstance(out.get(key), list):
            out[key] = [
                {
                    **row,
                    "blocker_task_id": _label(row.get("blocker_task_id")),
                    "blocked_task_id": _label(row.get("blocked_task_id")),
                }
                if isinstance(row, dict)
                else row
                for row in out[key]
            ]


def _friendly_manage_milestone_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `manage_milestone` arguments (MIP-879).

    The flat pass labels `project_id`, `milestone_id`, `milestone_folder_id` and
    `sprint_id`. This names the people in `assignee_ids` (a list, below the flat
    pass) and, for an update, ADDS the present value of every field being
    changed under `current`, keyed by argument name, so the card can show
    old → new. A rename or delete of a folder gets the folder's present name
    (and how many milestones a delete unfiles). Nothing is added for an object
    the caller can't read."""
    from origin.models.task.milestone_models import (  # noqa: PLC0415
        MilestoneAssignees,
        MilestoneMaster,
    )
    from origin.search_engine.agent.tools.task_tags import tag_names  # noqa: PLC0415

    proposed = args.get("assignee_ids")
    proposed = [u for u in proposed if u] if isinstance(proposed, list) else []
    action = args.get("action")

    milestone = access.milestone(args.get("milestone_id")) if action == "update" else None
    current_people = (
        [
            str(uid)
            for uid in MilestoneAssignees.objects.filter(milestone=milestone).values_list(
                "user_id", flat=True
            )
        ]
        if milestone is not None and args.get("assignee_ids") is not None
        else []
    )
    names = access.usernames([*proposed, *current_people]) if proposed or current_people else {}
    if proposed:
        out["assignee_ids"] = [names.get(str(u), u) for u in proposed]

    if milestone is not None:
        current: dict[str, Any] = {}
        for key in ("title", "status", "priority", "effort_level"):
            if args.get(key) is not None:
                current[key] = getattr(milestone, key) or None
        for key in ("start_date", "due_date"):
            if args.get(key) is not None:
                value = getattr(milestone, key)
                current[key] = value.isoformat() if value else None
        if args.get("tags") is not None:
            current["tags"] = tag_names(milestone.tags)
        if args.get("assignee_ids") is not None:
            current["assignee_ids"] = [names.get(uid, uid) for uid in current_people]
        if args.get("milestone_folder_id") is not None:
            current["milestone_folder_id"] = (
                (access.milestone_folder_name(milestone.folder_id) or milestone.folder_id)
                if milestone.folder_id
                else None
            )
        if args.get("sprint_id") is not None:
            current["sprint_id"] = (
                (access.sprint_name(milestone.sprint_id) or milestone.sprint_id)
                if milestone.sprint_id
                else None
            )
        out["current"] = current
        return

    if action in ("rename_folder", "delete_folder"):
        folder = access.milestone_folder(args.get("milestone_folder_id"))
        if folder is None:
            return
        current = {"folder_name": folder.name}
        if action == "delete_folder":
            current["milestones"] = MilestoneMaster.objects.filter(
                folder=folder, is_deleted=False
            ).count()
        out["current"] = current


def _friendly_delete_milestone_arguments(
    out: dict[str, Any], args: dict[str, Any], access: LabelAccess
) -> None:
    """In-place nested enrichment for `delete_milestone` arguments (MIP-879).

    ADDS what the approver is about to lose track of: the milestone's present
    title and how many tasks the delete detaches (they are kept). Only for a
    milestone the caller can read."""
    from origin.search_engine.agent.tools.delete_milestone import (  # noqa: PLC0415
        milestone_task_count,
    )

    milestone = access.milestone(args.get("milestone_id"))
    if milestone is None:
        return
    out["current"] = {"title": milestone.title, "tasks": milestone_task_count(milestone)}


# Tool-name → nested enrichment pass, applied after the flat resolvers.
# Each edits the labelled copy and may read the caller's raw args for an
# id the flat pass has already replaced with a label.
_FRIENDLY_NESTED_ENRICHERS: dict[
    str, Callable[[dict[str, Any], dict[str, Any], LabelAccess], None]
] = {
    "create_task_plan": _friendly_task_plan_arguments,
    "update_task": _friendly_update_task_arguments,
    "manage_milestone": _friendly_manage_milestone_arguments,
    "delete_milestone": _friendly_delete_milestone_arguments,
    "update_todo_item": _friendly_update_todo_arguments,
    "create_todo_item": _friendly_create_todo_arguments,
    # The note it stars, named; a folder takes the flat `folder_id` label.
    "favorite_note": _friendly_update_note_arguments,
    "delete_note": _friendly_update_note_arguments,
    "manage_note_sharing": _friendly_update_note_arguments,
    "restore_note_version": _friendly_update_note_arguments,
    "delete_todo_item": _friendly_delete_todo_arguments,
    "update_tasks_bulk": _friendly_bulk_update_arguments,
    "update_task_dependencies": _friendly_dependency_arguments,
    "update_note": _friendly_update_note_arguments,
    "create_note": _friendly_create_note_arguments,
    "create_task": _friendly_create_task_arguments,
}


def _friendly_arguments(
    args: dict[str, Any], tool_name: str | None = None, *, ctx: ToolContext
) -> dict[str, Any]:
    """Return a copy of `args` with raw IDs replaced by human labels.

    Applied only at the wire-event boundary (the approval card + tool
    progress strip render this). The persisted `arguments_json` keeps
    the canonical primary keys so the resume path re-runs the tool
    correctly. `tool_name` selects an optional nested enrichment pass
    for composite tools whose IDs live below the top level.

    The ids come from the model, so `ctx` is REQUIRED: a label is printed
    only for an object the caller may read (`LabelAccess`, MIP-842). Any
    other id, and any lookup that fails, stays raw.
    """
    access = LabelAccess(ctx)
    out: dict[str, Any] = {}
    for key, value in args.items():
        resolver = _FRIENDLY_ARG_RESOLVERS.get(key)
        if resolver is not None:
            try:
                friendly = resolver(value, access)
            except Exception:  # noqa: BLE001 — labels never break the loop
                # Not the value: the model sometimes passes a title for an id.
                log.exception("Friendly-arg lookup failed for %s (%s)", key, type(value).__name__)
                friendly = None
            out[key] = friendly if friendly is not None else value
        else:
            out[key] = value

    enricher = _FRIENDLY_NESTED_ENRICHERS.get(tool_name or "")
    if enricher is not None:
        try:
            enricher(out, args, access)
        except Exception:  # noqa: BLE001 — labels never break the loop
            log.exception("Nested friendly-arg enrichment failed for %s", tool_name)
    return out


def _coerce_signature(raw: Any) -> bytes | None:
    """Normalise a persisted thought_signature back to `bytes | None`.

    `models.BinaryField` may surface as `bytes`, `memoryview`, or
    `None` depending on the DB driver. The Gemini SDK's `Part`
    constructor expects `bytes`, so coerce explicitly. Empty buffers
    become None — an empty signature would still be rejected by the
    API, so treating it as missing avoids sending a malformed echo.
    """
    if raw is None:
        return None
    if isinstance(raw, memoryview):
        raw = raw.tobytes()
    if isinstance(raw, bytes):
        return raw or None
    # Anything else (e.g. str under SQLite quirks) — coerce defensively.
    try:
        return bytes(raw) or None
    except (TypeError, ValueError):
        return None


def _user_turn(query: str) -> AgentMessage:
    return AgentMessage(role="user", text=query)


def _prior_messages(
    prior_summary: str | None, prior_turns: list[tuple[str, str]] | None
) -> list[AgentMessage]:
    """The earlier turns of a conversation as the messages that go in front of its
    next question: the rolling recap, then each earlier (question, answer) pair.

    The recap (Phase 3.5, `multi_turn.py`) is an assistant "note to self" so the
    model can refer to topics that have fallen out of the verbatim window. It goes
    first, then the verbatim turns oldest to newest.

    ONE function for both legs of a run: `run_agent` puts these in front of the
    question, and `resume_agent` puts the same ones in front of the replay of what
    the run did since (MIP-991). They are two callers of one construction because
    the whole point of the second is that its messages START WITH the first's,
    exactly: a copy of these lines in `resume_agent` would be one edit away from
    sending the model a different conversation after an approval, and from losing
    the shared prefix a provider's cache keys on.

    Empty for a first turn (no recap, no turns).
    """
    messages: list[AgentMessage] = []
    if prior_summary:
        messages.append(
            AgentMessage(
                role="assistant",
                text=f"[Context recap from earlier in this conversation: {prior_summary}]",
            )
        )
    for prior_query, prior_answer in prior_turns or []:
        messages.append(_user_turn(prior_query))
        messages.append(AgentMessage(role="assistant", text=prior_answer))
    return messages


def _assistant_function_call_turn(function_call: FunctionCall) -> AgentMessage:
    return AgentMessage(role="assistant", function_call=function_call)


def _function_response_turn(name: str, response: dict[str, Any]) -> AgentMessage:
    # The model's copy only. The result dict the caller passed is what the
    # source chips, the persisted step and the evals read, and those need
    # titles bare; this returns a fenced copy and leaves it untouched
    # (MIP-735, `model_fence`). Errors carry no user-typed names.
    return AgentMessage(
        role="tool_response",
        function_response_name=name,
        function_response=fence_for_model(response, name),
    )


def _strip_workspace_marker(s: str | None) -> str | None:
    """Reverse of `wrap_workspace_content` for UI-bound snippet text: the fence
    off, and the escape of any fence tag in the text undone (MIP-927), so a chip
    shows what was written. A snippet that is not exactly one fence is left."""
    if not s:
        return s
    inner = unwrap(s)
    return s if inner is None else inner


def _ui_source_for_match(match: dict[str, Any]) -> dict[str, Any]:
    """Shape a search-tool match into the UI's `sources` event payload.

    Mirrors the `SpotlightResult` shape returned by `/api/v2/search/`
    so the frontend can hand the source chip directly to the same
    `handleSpotlightSelect` router that the search-result rows use.
    Two fields are essential for routing parity:

      * `message_id` — lets a chat citation deep-link to the exact
        bubble that matched (not just the chat/thread).
      * `related_entity_ids` — fallback the frontend reads when chunks
        pre-date direct `task_id` / `chat_*` fields on note rows. Older
        chat-note / task-note chunks only carry their parent entity in
        this list, so dropping it breaks routing for unupgraded data.
    """
    return {
        "entity_type": match.get("entity_type"),
        "entity_id": match.get("entity_id"),
        "title": match.get("title"),
        "snippet": _strip_workspace_marker(match.get("snippet")),
        "chat_type": match.get("chat_type"),
        "chat_id": match.get("chat_id"),
        "thread_id": match.get("thread_id"),
        "message_id": match.get("message_id"),
        "task_id": match.get("task_id"),
        # Human-readable task ID ("<project.code>-<project_task_number>",
        # e.g. "PRJ-42"). Hydrated by `_hydrate_task_display_ids` after
        # the source list is built — the OpenSearch index doesn't carry it.
        "task_display_id": None,
        "note_id": match.get("note_id"),
        "note_type": match.get("note_type"),
        "project_id": match.get("project_id"),
        "matched_chunk_types": list(match.get("matched_chunk_types") or []),
        "matched_terms": list(match.get("matched_terms") or []),
        "related_entity_ids": list(match.get("related_entity_ids") or []),
        "updated_at": match.get("updated_at"),
        # These ranking fields are search-result-only; the agent never
        # ranks sources itself. Defaults keep the shape uniform so the
        # frontend doesn't have to branch on agent-vs-search origin.
        "score": 0.0,
        "keyword_rank": None,
        "vector_rank": None,
    }


from origin.search_engine.friendly_titles import (
    apply_friendly_titles as _resolve_chat_titles,
)


def _apply_friendly_titles(sources: list[dict[str, Any]], ctx: ToolContext) -> list[dict[str, Any]]:
    """Replace placeholder chat titles ('DM 9') with viewer-friendly names.

    Thin adapter over the shared `friendly_titles.apply_friendly_titles`
    helper — kept so the in-loop call signature stays terse and so
    structured-tool sources (which don't go through `search()`) still
    get title resolution before chip emission.
    """
    return _resolve_chat_titles(sources, ctx.user_id)


def _hydrate_task_display_ids(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backfill `task_display_id` for task sources that don't already have one.

    `_task_source` (called from structured tools like list_tasks /
    fetch_task) sets display_id directly because the tool result already
    carries it. `_ui_source_for_match` (search-knowledge-base path) does
    NOT — the OpenSearch index stores only the raw task_id. We resolve
    those missing ones here with one batched DB query.
    """
    missing_ids: list[int] = []
    for src in sources:
        if src.get("entity_type") != "task" or src.get("task_display_id"):
            continue
        raw = src.get("task_id")
        if raw is None:
            continue
        try:
            missing_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    if missing_ids:
        from origin.models.task.task_models import TaskMaster

        by_id: dict[int, str] = {}
        for t in TaskMaster.objects.select_related("project").filter(task_id__in=missing_ids):
            by_id[t.task_id] = t.display_id

        for src in sources:
            if src.get("entity_type") != "task" or src.get("task_display_id"):
                continue
            try:
                tid = int(src.get("task_id"))
            except (TypeError, ValueError):
                continue
            if tid in by_id:
                src["task_display_id"] = by_id[tid]
    # Every emit path funnels through here, so the names a write tool's result
    # did not carry are filled in once, for all of them.
    _hydrate_source_names(sources)
    return sources


def _hydrate_source_names(sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Backfill what a write tool's result left off its chip: a project's name,
    a note's title, and the task / chat refs a task or chat note's link needs.

    The project-scoped writes (members, tags, labels, sprints) and
    `manage_note_sharing` return ids only, and `restore_note_version` returns no
    parent context, so their chips would read "Project" / "Note" or open the wrong
    place. One batched read per kind, only for chips that lack it. The ids come
    from a result the tool already checked access for, so this only names an
    object the person just operated on. Cosmetic: a failed lookup leaves the chip
    as it was and never breaks the run.
    """
    project_chips: dict[int, list[dict[str, Any]]] = {}
    note_chips: dict[str, dict[int, list[dict[str, Any]]]] = {}
    for src in sources:
        etype = src.get("entity_type")
        if etype == "project" and not src.get("title"):
            try:
                project_chips.setdefault(int(src.get("project_id")), []).append(src)
            except (TypeError, ValueError):
                continue
        elif etype == "note":
            family = src.get("note_type")
            if family not in ("personal", "task", "chat"):
                continue
            lacks_refs = (
                family == "task" and not (src.get("project_id") and src.get("task_id"))
            ) or (family == "chat" and not src.get("chat_id"))
            if src.get("title") and not lacks_refs:
                continue
            try:
                note_chips.setdefault(family, {}).setdefault(int(src.get("note_id")), []).append(
                    src
                )
            except (TypeError, ValueError):
                continue
    if not project_chips and not note_chips:
        return sources

    try:
        if project_chips:
            from origin.models.project.prj_models import ProjectMaster

            for pid, name in ProjectMaster.objects.filter(
                project_id__in=list(project_chips)
            ).values_list("project_id", "project_name"):
                for src in project_chips[pid]:
                    src["title"] = name or ""
        if note_chips:
            from origin.models.note.chat_note_models import ChatNoteMaster
            from origin.models.note.personal_note_models import PersonalNoteMaster
            from origin.models.note.task_note_models import TaskNoteMaster
            from origin.search_engine.agent.tools.note_targets import chat_note_parent_context

            models = {
                "personal": PersonalNoteMaster,
                "task": TaskNoteMaster,
                "chat": ChatNoteMaster,
            }
            for family, by_id in note_chips.items():
                for note in models[family].objects.filter(note_id__in=list(by_id)):
                    if family == "chat":
                        refs = chat_note_parent_context(note)
                    elif family == "task":
                        refs = {"project_id": note.project_id or None, "task_id": note.task_id}
                    else:
                        refs = {}
                    for src in by_id[note.note_id]:
                        if not src.get("title"):
                            src["title"] = note.title or ""
                        for key in ("project_id", "task_id", "chat_type", "chat_id", "thread_id"):
                            if not src.get(key) and refs.get(key) is not None:
                                src[key] = (
                                    str(refs[key])
                                    if key in ("project_id", "task_id")
                                    else refs[key]
                                )
    except Exception:  # noqa: BLE001 — cosmetic backfill must never break a run
        log.exception("Failed to backfill chip names")
    return sources


def _blank_source(entity_type: str, entity_id: str) -> dict[str, Any]:
    """Skeleton source dict; structured-tool helpers fill in the type-specific fields."""
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "title": None,
        "snippet": None,
        "chat_type": None,
        "chat_id": None,
        "thread_id": None,
        "message_id": None,
        "task_id": None,
        "task_display_id": None,
        "note_id": None,
        "note_type": None,
        "project_id": None,
        "matched_chunk_types": [],
        "matched_terms": [],
        "related_entity_ids": [],
        "updated_at": None,
        "score": 0.0,
        "keyword_rank": None,
        "vector_rank": None,
    }


def _task_source(
    task_id: Any, title: Any, project_id: Any, display_id: Any = None
) -> dict[str, Any]:
    s = _blank_source("task", f"task:{task_id}")
    s["title"] = title or ""
    s["task_id"] = str(task_id) if task_id is not None else None
    s["task_display_id"] = display_id or None
    s["project_id"] = str(project_id) if project_id is not None else None
    return s


def _project_source(project_id: Any, project_name: Any) -> dict[str, Any]:
    s = _blank_source("project", f"project:{project_id}")
    s["title"] = project_name or ""
    s["project_id"] = str(project_id) if project_id is not None else None
    return s


def _chat_source(
    chat_type: Any,
    chat_id: Any,
    thread_id: Any = None,
    title: Any = None,
) -> dict[str, Any]:
    # Chunker convention: entity_id has no leading "chat:" prefix.
    base = f"{chat_type}:{chat_id}"
    eid = f"{base}:thread:{thread_id}" if thread_id else base
    s = _blank_source("chat", eid)
    s["title"] = title or ""
    s["chat_type"] = chat_type
    s["chat_id"] = str(chat_id) if chat_id is not None else None
    s["thread_id"] = str(thread_id) if thread_id else None
    return s


def _note_source(
    note_type: Any,
    note_id: Any,
    title: Any = None,
    parent_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    s = _blank_source("note", f"note:{note_type}:{note_id}")
    s["title"] = title or ""
    s["note_id"] = str(note_id) if note_id is not None else None
    s["note_type"] = note_type
    pc = parent_context or {}
    # Tools report `project_id` as a number (MIP-781); the chip carries a string.
    pid = pc.get("project_id")
    s["project_id"] = str(pid) if pid is not None else None
    s["task_id"] = pc.get("task_id")
    s["chat_type"] = pc.get("chat_type")
    s["chat_id"] = pc.get("chat_id")
    s["thread_id"] = pc.get("thread_id")
    return s


def _todo_source(item_id: Any, title: Any, local_date: Any) -> dict[str, Any]:
    # entity_id mirrors the chunker shape: `todo:YYYY-MM-DD:item:<id>`.
    # `related_entity_ids` points at the day-level grouping so a future
    # daily-summary chunker can co-link.
    s = _blank_source("todo", f"todo:{local_date}:item:{item_id}")
    s["title"] = title or ""
    s["related_entity_ids"] = [f"todo:{local_date}"]
    return s


def _milestone_source(
    milestone_id: Any, title: Any, project_id: Any, task_id: Any = None
) -> dict[str, Any]:
    # entity_id mirrors the milestone chunker (`milestone:<id>`). The
    # backing task_id + project_id let the frontend deep-link the chip
    # through the task view (App.tsx handleSpotlightSelect routes a
    # milestone the same as its backing task). task_id is None for legacy
    # milestones whose backing task hasn't been auto-created yet.
    s = _blank_source("milestone", f"milestone:{milestone_id}")
    s["title"] = title or ""
    s["project_id"] = str(project_id) if project_id is not None else None
    s["task_id"] = str(task_id) if task_id is not None else None
    return s


# Which write tools reach a chip branch of `_sources_from_tool_result`, and which are
# deliberately without one. `test_agent_write_source_chips` holds the two to the registry
# both ways, so a new approval-gated tool has to land in one of them.
_TASK_WRITE_TOOLS = frozenset(
    {
        "create_task",
        "update_task",
        "assign_task",
        "add_comment",
        "update_comment",
        "delete_comment",
        "restore_task_body",
        "add_task_delivery",
        "remove_task_delivery",
    }
)
_NOTE_SOURCE_TOOLS = frozenset(
    {
        "fetch_note",
        "create_note",
        "update_note",
        "restore_note_version",
        "manage_note_sharing",
    }
)
_PROJECT_WRITE_TOOLS = frozenset(
    {
        "manage_project",
        "manage_sprint",
        "manage_project_members",
        "add_project_tag",
        "manage_project_labels",
        "approve_join_request",
        "decline_join_request",
    }
)
# Chip branches that live inline above for the rest: create_task_plan, update_tasks_bulk,
# update_task_dependencies, manage_milestone, delete_milestone, create_todo_item,
# update_todo_item.
NO_CHIP_WRITE_TOOLS: dict[str, str] = {
    "delete_note": "the note is gone: a chip would be a dead link",
    "delete_note_folder": "a folder is not a source type, and the folders it deleted are gone",
    "manage_note_folder": "a folder is not a source type: there is no chip or citation form for one",
    "favorite_note": (
        "its result names a note without its type, and adding it would change what the model "
        "sees (a December item); a folder is not a source type"
    ),
    "delete_todo_item": "the to-do is gone: a chip would be a dead link",
    "delete_sprint": (
        "its result carries no project, and adding one would change what the model sees "
        "(a December item)"
    ),
    "delete_project": "the project is gone, and there is no container to chip instead",
    "create_calendar_event": "a calendar event is not a source type yet",
    "update_calendar_event": "a calendar event is not a source type yet",
    "delete_calendar_event": "a calendar event is not a source type yet",
}


def _ui_sources_from_tool_result(call_name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Build UI source dicts from a non-search tool's result, marking
    sources produced by a WRITE tool (`requires_approval`) with
    `operated: true`.

    The mark is the frontend's guarantee that every object the agent
    created/updated surfaces as a clickable chip even when the model's
    prose forgets to cite it (`citedChipSources` keeps operated sources
    unconditionally; ordinary read sources it drops when uncited).
    Marked here — the one funnel every emit path shares (the resume
    path's write pass, `_drive_loop`, and `reconstruct_sources_for_run`)
    — rather than per branch, so a new write tool can't forget it.
    """
    sources = _sources_from_tool_result(call_name, result)
    if sources and getattr(REGISTRY.get(call_name), "requires_approval", False):
        for s in sources:
            s["operated"] = True
    return sources


def _sources_from_tool_result(call_name: str, result: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-tool source-dict mapping — see `_ui_sources_from_tool_result`.

    Returns [] for tools whose results don't map to a clickable entity
    (e.g. analytics aggregations without per-row ids, get_current_user,
    get_team_members — no user-detail view exists to link to; calendar
    events, which have no Spotlight entity type or citation grammar).

    Sources are deduped upstream by (entity_type, entity_id), so emitting
    the same task from both `list_tasks` and `search_knowledge_base` in
    one run only produces a single chip.
    """
    if not isinstance(result, dict):
        return []

    if call_name in ("list_tasks", "get_stale_tasks"):
        tasks = result.get("tasks") or []
        task_sources = [
            _task_source(
                t.get("task_id"),
                t.get("title"),
                t.get("project_id"),
                display_id=t.get("display_id"),
            )
            for t in tasks
            if t.get("task_id")
        ]
        # Also emit one source per distinct project so inline
        # `[project:N]` citations the model writes (e.g.
        # "In Q2 Roadmap [project:18]: ...") resolve via the frontend
        # rewriter — without this, the bare token renders raw. Phase 4.2
        # citation-density ranking pushes uncited project chips down,
        # so unprompted-chip noise is bounded.
        seen_project_ids: set[Any] = set()
        project_sources: list[dict[str, Any]] = []
        for t in tasks:
            pid = t.get("project_id")
            if pid is None or pid in seen_project_ids:
                continue
            seen_project_ids.add(pid)
            project_sources.append(_project_source(pid, t.get("project_name")))
        return task_sources + project_sources

    if call_name == "fetch_task":
        tid = result.get("task_id")
        if not tid:
            return []
        return [
            _task_source(
                tid,
                result.get("title"),
                result.get("project_id"),
                display_id=result.get("display_id"),
            )
        ]

    if call_name == "create_task_plan":
        # Approved plan → chips for everything it created, so the final
        # answer's citations resolve and the user can click straight
        # into the new milestone/tasks. Not capped here: the tool itself takes
        # at most `_MAX_PLAN_TASKS` (20), and the chip row folds the rest into
        # "+N more".
        sources: list[dict[str, Any]] = []
        pid = result.get("project_id")
        ms = result.get("milestone") or {}
        if ms.get("milestone_id"):
            sources.append(
                _milestone_source(ms["milestone_id"], ms.get("title"), pid, ms.get("task_id"))
            )
        parent = result.get("parent_task") or {}
        if parent.get("task_id"):
            # Sub-task mode: chip for the anchor task the batch nested under.
            sources.append(
                _task_source(
                    parent["task_id"],
                    parent.get("title"),
                    pid,
                    display_id=parent.get("display_id"),
                )
            )
        for t in result.get("tasks") or []:
            if t.get("task_id"):
                sources.append(
                    _task_source(t["task_id"], t.get("title"), pid, display_id=t.get("display_id"))
                )
        if pid:
            sources.append(_project_source(pid, result.get("project_name")))
        return sources

    if call_name == "update_tasks_bulk":
        # Approved organize pass → chips for the touched tasks so the
        # answer's "I bumped X to Critical" citations resolve. Every row the
        # tool applied (it takes at most `_MAX_BULK_UPDATES`, 30).
        return [
            _task_source(
                row.get("task_id"),
                row.get("title"),
                row.get("project_id"),
                display_id=row.get("display_id"),
            )
            for row in result.get("updated") or []
            if row.get("task_id")
        ]

    if call_name == "update_task_dependencies":
        # Both ends of every touched edge, once each (the tool takes at most
        # `_MAX_EDGES`, 30, so this is bounded), so "linked X → Y" cites resolve. No project_id in the result — the downstream
        # display-id hydration backfills it like assign_task's.
        seen: dict[Any, dict[str, Any]] = {}
        for key in ("added", "removed"):
            for edge in result.get(key) or []:
                for end in ("blocker", "blocked"):
                    tid = edge.get(f"{end}_task_id")
                    if tid and tid not in seen:
                        seen[tid] = _task_source(
                            tid,
                            edge.get(f"{end}_title"),
                            None,
                            display_id=edge.get(f"{end}_display_id"),
                        )
        return list(seen.values())

    if call_name in _TASK_WRITE_TOOLS:
        # Single-task write tools → chip the operated task, so every
        # approved write hands the user a clickable ref to what changed.
        # Several don't echo title/project_id (assign_task, the comment and
        # delivery tools): display-id hydration backfills the id downstream.
        # A comment delete or a delivery unlink chips the TASK, which is still
        # there; only the comment or link is gone.
        tid = result.get("task_id")
        if not tid:
            return []
        return [
            _task_source(
                tid,
                result.get("title"),
                result.get("project_id"),
                display_id=result.get("display_id"),
            )
        ]

    if call_name == "manage_milestone":
        # A milestone action → the milestone's chip; a folder action has no
        # milestone, so the chip is its project's.
        if result.get("milestone_id"):
            return [
                _milestone_source(
                    result["milestone_id"],
                    result.get("title"),
                    result.get("project_id"),
                    result.get("task_id"),
                )
            ]
        if result.get("project_id"):
            return [_project_source(result["project_id"], result.get("project_name"))]
        return []

    if call_name == "delete_milestone":
        # The milestone is gone, so a chip for it would be a dead link; the
        # project it left is the place to go.
        if result.get("project_id"):
            return [_project_source(result["project_id"], result.get("project_name"))]
        return []

    if call_name in _PROJECT_WRITE_TOOLS:
        # A project-scoped write (project / sprint / members / tags / labels /
        # join-request answers) → the project's chip. None of these has an
        # entity of its own to open, so the project is the place to go. A label
        # that was only created belongs to the team, not a project: no chip.
        pid = result.get("project_id")
        if not pid:
            return []
        # `name` is the project's only for manage_project; a sprint's `name` is the sprint's.
        name = result.get("project_name") or (
            result.get("name") if call_name == "manage_project" else None
        )
        return [_project_source(pid, name)]

    if call_name in ("create_todo_item", "update_todo_item"):
        # Todo write tools → chip the operated item (same entity_id
        # shape as the todo chunker: `todo:<local_date>:item:<id>`).
        iid = result.get("item_id")
        local_date = result.get("local_date")
        if not iid or not local_date:
            return []
        return [_todo_source(iid, result.get("title"), local_date)]

    if call_name == "list_projects":
        return [
            _project_source(p.get("project_id"), p.get("project_name"))
            for p in (result.get("projects") or [])
            if p.get("project_id")
        ]

    if call_name == "get_project_summary":
        pid = result.get("project_id")
        if not pid:
            return []
        return [_project_source(pid, result.get("project_name"))]

    if call_name == "fetch_chat_thread":
        chat_type = result.get("chat_type")
        chat_id = result.get("chat_id")
        if not chat_type or not chat_id:
            return []
        return [_chat_source(chat_type, chat_id, result.get("thread_id"))]

    if call_name in _NOTE_SOURCE_TOOLS:
        # The write tools return the same note_id / note_type /
        # title / parent_context fields as fetch_note, so one branch
        # gives approved note writes a clickable chip too (emitted via
        # the resume path's write-result pass). restore_note_version and
        # manage_note_sharing return less (no parent_context, sharing no title):
        # `_hydrate_source_names` fills those in from the note's row.
        nid = result.get("note_id")
        ntype = result.get("note_type")
        if not nid or not ntype:
            return []
        return [_note_source(ntype, nid, result.get("title"), result.get("parent_context"))]

    if call_name in ("list_milestones", "list_sprints"):
        # Milestones / sprints aren't a Spotlight entity_type today —
        # emit one project chip per distinct project so the user has at
        # least a deep-link surface to the right project.
        rows = result.get("milestones") if call_name == "list_milestones" else result.get("sprints")
        rows = rows or []
        seen: set[Any] = set()
        sources: list[dict[str, Any]] = []
        for r in rows:
            pid = r.get("project_id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            sources.append(_project_source(pid, r.get("project_name")))
        return sources

    if call_name in ("get_milestone_summary", "get_sprint_summary"):
        pid = result.get("project_id")
        if not pid:
            return []
        return [_project_source(pid, result.get("project_name"))]

    if call_name == "get_team_task_summary":
        return [
            _project_source(p.get("project_id"), p.get("project_name"))
            for p in (result.get("per_project") or [])
            if p.get("project_id")
        ]

    if call_name == "get_task_blockers":
        # Emit the target task + each blocker/blocked task as separate
        # chips so the user can click into any of them. Dedup is by
        # (entity_type, entity_id) one level up, so emitting them all is
        # safe even when the graph self-references.
        sources: list[dict[str, Any]] = []
        target_tid = result.get("task_id")
        if target_tid is not None:
            sources.append(
                _task_source(
                    target_tid,
                    result.get("title"),
                    None,  # target's project_id isn't echoed — chip stays simple
                    display_id=result.get("display_id"),
                )
            )
        seen_projects: set[Any] = set()
        for direction in ("blocked_by", "blocking"):
            for ref in result.get(direction) or []:
                tid = ref.get("task_id")
                if tid is None:
                    continue
                sources.append(
                    _task_source(
                        tid,
                        ref.get("title"),
                        ref.get("project_id"),
                        display_id=ref.get("display_id"),
                    )
                )
                pid = ref.get("project_id")
                if pid is not None and pid not in seen_projects:
                    seen_projects.add(pid)
                    sources.append(_project_source(pid, ref.get("project_name")))
        return sources

    # --- Phase 18: me-scoped tools ---
    # Same chip-shape choices as the workspace-scoped counterparts so the
    # UI rewriter resolves citations identically whether the model came
    # in via `list_tasks` or `get_my_focus_tasks`.

    if call_name in ("get_my_focus_tasks", "get_my_schedule"):
        # Both return task rows (`tasks` / `tasks_due`). Same chip pattern
        # as `list_tasks`: one task chip per row + one project chip per
        # distinct project.
        key = "tasks" if call_name == "get_my_focus_tasks" else "tasks_due"
        rows = result.get(key) or []
        out: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for t in rows:
            tid = t.get("task_id")
            if tid is None:
                continue
            out.append(
                _task_source(
                    tid,
                    t.get("title"),
                    t.get("project_id"),
                    display_id=t.get("display_id"),
                )
            )
        for t in rows:
            pid = t.get("project_id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            out.append(_project_source(pid, t.get("project_name")))
        return out

    if call_name == "get_my_task_summary":
        return [
            _project_source(p.get("project_id"), p.get("project_name"))
            for p in (result.get("per_project") or [])
            if p.get("project_id")
        ]

    if call_name == "list_my_milestones":
        seen: set[Any] = set()
        out_proj: list[dict[str, Any]] = []
        for m in result.get("milestones") or []:
            pid = m.get("project_id")
            if pid is None or pid in seen:
                continue
            seen.add(pid)
            out_proj.append(_project_source(pid, m.get("project_name")))
        return out_proj

    if call_name == "get_my_blockers":
        # Walk both halves; emit a task chip for each of my tasks and
        # for each edge endpoint, plus one project chip per distinct
        # project. Mirrors `get_task_blockers` shape choice.
        out_b: list[dict[str, Any]] = []
        seen_proj: set[Any] = set()
        for half_key, edge_key in (
            ("blocked_on_me", "blocked_by"),
            ("blocking_others", "blocking"),
        ):
            for row in result.get(half_key) or []:
                tid = row.get("task_id")
                if tid is not None:
                    out_b.append(
                        _task_source(
                            tid,
                            row.get("title"),
                            row.get("project_id"),
                            display_id=row.get("display_id"),
                        )
                    )
                pid = row.get("project_id")
                if pid is not None and pid not in seen_proj:
                    seen_proj.add(pid)
                    out_b.append(_project_source(pid, row.get("project_name")))
                for edge in row.get(edge_key) or []:
                    etid = edge.get("task_id")
                    if etid is None:
                        continue
                    out_b.append(
                        _task_source(
                            etid,
                            edge.get("title"),
                            edge.get("project_id"),
                            display_id=edge.get("display_id"),
                        )
                    )
                    epid = edge.get("project_id")
                    if epid is not None and epid not in seen_proj:
                        seen_proj.add(epid)
                        out_b.append(_project_source(epid, edge.get("project_name")))
        return out_b

    if call_name == "get_my_throughput":
        out_thr: list[dict[str, Any]] = []
        seen_thr: set[Any] = set()
        for t in result.get("recently_closed") or []:
            tid = t.get("task_id")
            if tid is None:
                continue
            out_thr.append(
                _task_source(
                    tid,
                    t.get("title"),
                    t.get("project_id"),
                    display_id=t.get("display_id"),
                )
            )
            pid = t.get("project_id")
            if pid is not None and pid not in seen_thr:
                seen_thr.add(pid)
                out_thr.append(_project_source(pid, t.get("project_name")))
        return out_thr

    if call_name == "list_my_mentions":
        # Each mention is addressed at a chat (thread or channel). The
        # chip lets the user click straight into that chat — same shape
        # the chunker emits for chat sources.
        out_m: list[dict[str, Any]] = []
        seen_m: set[tuple[Any, Any, Any]] = set()
        for row in result.get("mentions") or []:
            label = row.get("chat_type_label")
            cid = row.get("chat_id")
            tid_chat = row.get("thread_id") if row.get("is_thread") else None
            if not label or cid is None:
                continue
            key = (label, cid, tid_chat)
            if key in seen_m:
                continue
            seen_m.add(key)
            out_m.append(_chat_source(label, cid, tid_chat))
        return out_m

    if call_name == "list_my_chats":
        # One chip per chat, so the user can click straight into it. The name
        # is the row's own `title`; it is bare here, the fence goes only on
        # the copy the model reads.
        return [
            _chat_source(row["chat_type"], row["chat_id"], None, row.get("title"))
            for row in result.get("chats") or []
            if row.get("chat_type") and row.get("chat_id")
        ]

    # list_my_inbox: items don't map to a clickable Spotlight entity_type
    # today (no inbox-deep-link surface). Return [] so the agent has to
    # describe the items in prose rather than emit broken chips.

    # --- Todo tools ---
    if call_name == "list_today_todos":
        local_date = result.get("local_date")
        if not local_date:
            return []
        return [
            _todo_source(i.get("item_id"), i.get("title"), local_date)
            for i in result.get("items") or []
            if i.get("item_id")
        ]

    if call_name == "list_uncompleted_todos":
        return [
            _todo_source(i.get("item_id"), i.get("title"), i.get("local_date"))
            for i in result.get("items") or []
            if i.get("item_id") and i.get("local_date")
        ]

    if call_name in ("create_todo_item", "update_todo_item"):
        iid = result.get("item_id")
        # `update_todo_item` doesn't echo local_date; fall back to the
        # group-level prefix that's still resolvable on the frontend.
        ld = result.get("local_date") or ""
        if iid is None:
            return []
        return [_todo_source(iid, result.get("title"), ld)]

    return []


def reconstruct_sources_for_run(run) -> list[dict[str, Any]]:
    """Rebuild the same source list the live `/ask/` flow emitted for
    this run, replaying against persisted `AgentStep.result_json`.

    Walks the run's steps in insertion order and dispatches each one
    through the same per-tool source builders the live loop uses
    (`_ui_source_for_match` for `search_knowledge_base` matches,
    `_ui_sources_from_tool_result` for structured reads). Dedupes by
    `entity_id` so a task touched by both `list_tasks` and a follow-up
    `fetch_task` produces a single source row — matching the live
    `seen_sources_by_id` behavior in `_drive_loop`.

    Used by the History detail endpoint (so archived citation tokens
    resolve to clickable previews) AND by the `spotlight_answer`
    chunker (to derive each collected answer's provenance + ACL).
    The caller is responsible for prefetching `run.steps` if it wants
    to avoid an extra query.
    """
    seen_by_id: dict[str, dict[str, Any]] = {}
    for step in run.steps.all():
        if not step.tool_name or step.result_json is None:
            continue
        result = step.result_json
        if step.tool_name == "search_knowledge_base":
            new_sources = [_ui_source_for_match(m) for m in (result.get("matches") or [])]
        else:
            new_sources = _ui_sources_from_tool_result(step.tool_name, result)
        for s in new_sources:
            eid = s.get("entity_id")
            if not eid:
                continue
            if eid not in seen_by_id:
                seen_by_id[eid] = s
            elif s.get("operated"):
                # First-wins dedup, but the operated mark must survive a
                # read of the same entity earlier in the run (fetch_task
                # then update_task): the frontend's always-chip guarantee
                # rides on the flag.
                seen_by_id[eid]["operated"] = True
    return list(seen_by_id.values())


# --------------------------------------------------------------------------- #
# Phase 4.2 — source-chip ranking by citation density                         #
# --------------------------------------------------------------------------- #

# Cited ids come from `citation_resolver.extract_citation_tokens`, the same
# scanner that back-fills chips, so every spelling the frontend strips counts
# toward the ranking: `[type:id]`, `[prose](type:id)`, a group
# `[task:1, task:2]`, and a raw `(task:1)` / `（task:12）` / bare `task:1`.
# (`_CITATION_RE` in evals/runner.py is a deliberately separate, narrower
# scanner: eval scoring is pinned to it.)


def _rank_sources_by_citation(
    answer_text: str,
    sources: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Re-sort the source list so chips actually cited in the answer
    surface leftmost. Stable for sources with the same citation count
    (preserves original tool-emission order — which is already a
    reasonable secondary signal since the most-relevant tool usually
    fires first).

    Citation-id matching mirrors the frontend's `sourcesById` lookup
    (see Phase 0.3): citation tokens are always `<type>:<rest>`, but
    some entity types (chats) ship `entity_id` without the leading
    `<type>:` prefix. Normalise to the prefixed form for matching so
    `chat:dm:9:thread:4` in the answer text finds a chat source whose
    entity_id is `dm:9:thread:4`.
    """
    if not sources:
        return sources

    cited_tokens = {
        canonical_citation_token(tok) for tok in extract_citation_tokens(answer_text or "")
    }
    if not cited_tokens:
        return sources

    def _token_key(src: dict[str, Any]) -> str:
        etype = (src.get("entity_type") or "").lower()
        eid = (src.get("entity_id") or "").lower()
        if not eid:
            return ""
        return canonical_citation_token(eid if eid.startswith(f"{etype}:") else f"{etype}:{eid}")

    def _citation_count(src: dict[str, Any]) -> int:
        key = _token_key(src)
        return 1 if key and key in cited_tokens else 0

    # Stable sort by (cited > uncited). Python's `sorted` is stable so
    # within-bucket order is preserved (= original tool-emission order).
    return sorted(sources, key=lambda s: -_citation_count(s))


# --------------------------------------------------------------------------- #
# Public entry points                                                         #
# --------------------------------------------------------------------------- #


def run_agent(
    query: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    *,
    run_id: UUID | None = None,
    prior_turns: list[tuple[str, str]] | None = None,
    prior_summary: str | None = None,
    disabled_tools: set[str] | None = None,
    system_extra: str | None = None,
    seed_sources: list[dict[str, Any]] | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
    session_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Drive the agent loop from a fresh user query.

    `cancel_event`, when set by the caller mid-run, stops the loop
    before its next model call (see `_drive_loop`). The streaming view
    sets it when the client disconnects.

    `prior_turns` is an ordered list of (user_query, assistant_answer)
    pairs from earlier turns in the same session (Phase 8). When
    present they are prepended to the messages list so the model can
    resolve references like "that task" or "the note you mentioned".
    Each answer is already truncated to ~400 chars by the view layer
    to keep the context budget bounded.

    `seed_sources` is an optional list of pre-built source dicts to
    register *before* the loop starts. Used by the note / thread Q&A
    branches: the agent has the summary in its system prompt and may
    answer without ever calling a tool, but it can still emit a
    `[note:...]` or `[chat:...]` citation for the entity the user
    opened the modal from. Pre-seeding the source lets the frontend
    citation rewriter resolve those tokens to a titled link instead
    of rendering the raw bracketed id.

    Returns:
        None on clean completion (text answer, error, or step cap).
        A pause descriptor when the loop hits a write tool:
            {
                "paused": True,
                "approval_token": UUID,
                "step": int,
                "tool_name": str,
                "arguments": dict,
            }
        The view layer reflects the pause back onto the `AgentRun` row.
    """
    # Pre-populate the live source map and ship the initial chips. The
    # event MUST be emitted before the first model call so even a zero-
    # tool answer still gets the seeded sources to the frontend.
    seeded_map: dict[tuple, dict[str, Any]] = {}
    if seed_sources:
        _apply_friendly_titles(seed_sources, ctx)
        _hydrate_task_display_ids(seed_sources)
        for src in seed_sources:
            key = (src.get("entity_type"), src.get("entity_id"))
            if not all(key) or key in seeded_map:
                continue
            seeded_map[key] = src
        if seeded_map:
            emit({"type": "sources", "sources": list(seeded_map.values())})

    # The recap and the earlier turns, then this question. `resume_agent` builds
    # the same start (`_prior_messages`) for the leg that follows an approval.
    messages = _prior_messages(prior_summary, prior_turns)
    messages.append(_user_turn(query))

    # Phase 3.2 — optional self-critique pass. Dispatched here so the
    # resume_agent path (write-tool approval flow) is NOT critiqued;
    # critique only makes sense on a complete, un-paused turn.
    def _inner(
        emit_fn: Callable[[dict[str, Any]], None],
        trace_fn: Callable[[str, dict[str, Any], dict[str, Any]], None] | None,
    ) -> dict[str, Any] | None:
        if settings.SEARCH_ENGINE.get("RAG_AGENT_SELF_CRITIQUE", False):
            return _drive_loop_with_critique(
                user_query=query,
                messages=messages,
                ctx=ctx,
                emit=emit_fn,
                run_id=run_id,
                starting_step=0,
                seen_sources_by_id=seeded_map,
                disabled_tools=disabled_tools,
                system_extra=system_extra,
                trace_hook=trace_fn,
                session_id=session_id,
                cancel_event=cancel_event,
            )
        return _drive_loop(
            messages=messages,
            ctx=ctx,
            emit=emit_fn,
            run_id=run_id,
            starting_step=0,
            seen_sources_by_id=seeded_map,
            disabled_tools=disabled_tools,
            system_extra=system_extra,
            trace_hook=trace_fn,
            session_id=session_id,
            cancel_event=cancel_event,
        )

    # §4.1 abstention gate — only on a fresh workspace query. With
    # `seed_sources` (thread/note Q&A) or `prior_turns` (multi-turn) the
    # answer can be grounded in context this gate can't see, so skip it
    # there to avoid false abstentions.
    gate_on = (
        settings.SEARCH_ENGINE.get("RAG_ABSTENTION_GATE", False)
        and not seed_sources
        and not prior_turns
    )
    if gate_on:
        return _run_with_abstention_gate(_inner, emit, trace_hook)
    return _inner(emit, trace_hook)


def resume_agent(
    run: AgentRun,
    decision: str,
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    *,
    disabled_tools: set[str] | None = None,
    system_extra: str | None = None,
    prior_turns: list[tuple[str, str]] | None = None,
    prior_summary: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Resume a paused agent run after the user has approved or rejected.

    Reconstructs the conversation up to the pending tool call from
    `AgentStep` rows, executes (approve) or synthesizes a rejection
    (reject) for that one tool, then continues the loop. Returns
    `None` on completion or another pause descriptor if the resumed
    run hits a second write tool.

    `disabled_tools` is the same per-user + tier union a fresh ask
    computes; the view MUST pass it so the continued loop declares the
    same surface the original run did. It also vetoes the pending call
    itself: an approval issued before a downgrade (or before an ops
    kill-switch flipped) must not execute a tool the user no longer
    has.

    `system_extra` is the prompt context the run's first leg had: the thread or
    note summary of "Ask about this ...", the @/# references, the tier block
    (MIP-977). `_rebuild_messages` replays the conversation from the run's rows
    and none of that is in them, so the view hands it back (`/decide/` keeps it
    on the run, `_resumed_system_extra`). Without it, every approve and every
    reject answered the rest of the run as if the person had never opened it from
    that thread or note, and with a different system prompt from the leg before
    (so a provider's prompt cache could not carry over either). None (a plain
    ask, a run paused before it was kept) is what this always received.

    `prior_turns` and `prior_summary` are the earlier turns of the run's
    conversation exactly as `run_agent` was given them (MIP-991): the verbatim
    (question, answer) pairs and the rolling recap. Same reason as above: the
    rebuild below starts from `run.query` alone, so without them every approve and
    every reject finished a follow-up ask as if it were the first question of the
    conversation. They go in front of the rebuilt messages through the same
    `_prior_messages` the ask used, so this leg's messages START WITH its first
    leg's, in the same order, and a provider's prompt cache can carry the prefix
    over. The view hands them back from the run (`_resumed_prior_context`); empty
    (a first turn, a run paused before they were kept) is what this always got.
    """
    if decision not in (DECISION_APPROVE, DECISION_REJECT):
        emit(
            {
                "type": "error",
                "message": f"Invalid decision {decision!r} (expected 'approve' or 'reject').",
            }
        )
        return None

    messages, pending_step = _rebuild_messages(run)
    if pending_step is None:
        emit(
            {
                "type": "error",
                "message": "No pending tool call found on this run.",
            }
        )
        return None
    # The conversation the ask leg had before the question (`_rebuild_messages`
    # starts at the question, which is where `run.query` is).
    messages = [*_prior_messages(prior_summary, prior_turns), *messages]

    step_index = pending_step.step_index
    call_name = pending_step.tool_name
    # The model's own call, exactly as it proposed it. This, never the edited
    # form, is what is replayed to the model as its function_call (with its
    # thought_signature), so what the model sees does not change with an edit.
    call_args = dict(pending_step.arguments_json or {})
    function_call = FunctionCall(
        name=call_name,
        args=call_args,
        thought_signature=_coerce_signature(pending_step.thought_signature),
    )
    # Edit before approve (`approval_edits`). `/decide/` stored the person's
    # patch on the step, in the same transaction that spent the approval, and
    # checked it before doing so. It is checked again here (this function is
    # callable on its own, and the clock moves: a reminder can pass in between)
    # and merged into the EFFECTIVE arguments the tool runs with:
    #   run_args   what `tool.run` receives (typed text escaped so the tool's
    #              unfencing hands it back unchanged);
    #   shown_args the same, as typed, for the events.
    # The model's replay (`function_call` above, and `_rebuild_messages` on a later
    # pause) is still its original call beside the result of what ran, so its final answer
    # can describe the proposal rather than the edit. That is deliberate and stays: the
    # write tool's own result carries the applied values (an edit note was tried and dropped).
    run_args = call_args
    shown_args = call_args
    edit_patch: dict[str, Any] = {}
    edit_error: str | None = None
    stored_edits = pending_step.edited_arguments_json
    if decision == DECISION_APPROVE and isinstance(stored_edits, dict) and stored_edits:
        try:
            edit_patch = approval_edits.prepare(call_name, call_args, stored_edits, ctx)
        except approval_edits.EditRejected as exc:
            edit_error = "Your edits could not be applied, so nothing was changed: " + "; ".join(
                f"{path}: {message}" for path, message in exc.field_errors.items()
            )
        else:
            if edit_patch:
                run_args = approval_edits.apply_edits(call_args, edit_patch, for_run=True)
                shown_args = approval_edits.apply_edits(call_args, edit_patch, for_run=False)
    # Chips created by an approved write (filled in the approve branch);
    # doubles as the continued loop's sources dedup seed.
    resumed_sources: dict[tuple[Any, Any], dict[str, Any]] = {}

    # Emit the start event the original run skipped. Same step index so
    # the frontend can correlate the approve/reject card with the row
    # that's now actually executing. After an edit it shows what is running.
    emit(
        {
            "type": "tool_call_start",
            "step": step_index,
            "tool_name": call_name,
            "arguments": _friendly_arguments(shown_args, call_name, ctx=ctx),
        }
    )

    if decision == DECISION_REJECT:
        err = "User rejected this action."
        emit(
            {
                "type": "tool_call_error",
                "step": step_index,
                "tool_name": call_name,
                "error": err,
            }
        )
        try:
            pending_step.error = "user_rejected"
            pending_step.summary = ""
            pending_step.save(update_fields=["error", "summary"])
        except Exception:  # noqa: BLE001
            log.exception("Failed to update pending step %s on reject", pending_step.step_id)
        messages.append(_assistant_function_call_turn(function_call))
        messages.append(
            _function_response_turn(
                call_name, approval_reason.rejection_result(pending_step.decision_reason)
            )
        )
    else:
        # APPROVE — actually run the tool now. A pending call whose
        # tool has since been disabled (tier downgrade, toggled-off
        # preference, ops kill-switch) is vetoed exactly like an
        # unknown tool: the approval token must not outlive the grant.
        tool = REGISTRY.get(call_name)
        vetoed = (disabled_tools or set()) | _operator_disabled_tools()
        if tool is None or call_name in vetoed:
            err = (
                f"Unknown tool: {call_name}"
                if tool is None
                else f"Tool '{call_name}' is no longer available on this account."
            )
            emit(
                {
                    "type": "tool_call_error",
                    "step": step_index,
                    "tool_name": call_name,
                    "error": err,
                }
            )
            try:
                pending_step.error = err
                pending_step.summary = ""
                pending_step.save(update_fields=["error", "summary"])
            except Exception:  # noqa: BLE001
                log.exception(
                    "Failed to update pending step %s on unknown tool", pending_step.step_id
                )
            messages.append(_assistant_function_call_turn(function_call))
            messages.append(_function_response_turn(call_name, {"error": err}))
        else:
            # Same per-tool wall clock the main loop keeps in
            # `tool_durations` — the approved write executes here, outside
            # that loop, so it times (and reports) its own run.
            _tool_started = time.monotonic()
            try:
                if edit_error is not None:
                    raise ToolError(edit_error)
                # The one place a write is asked for its pre-image (`agent/undo.py`):
                # nothing else that runs a tool sets this.
                result = tool.run(run_args, dataclasses.replace(ctx, capture_undo=True))
            except ToolError as e:
                dur_ms = int((time.monotonic() - _tool_started) * 1000)
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step_index,
                        "tool_name": call_name,
                        "error": str(e),
                        "duration_ms": dur_ms,
                    }
                )
                try:
                    pending_step.error = str(e)
                    pending_step.summary = ""
                    pending_step.latency_ms = dur_ms
                    pending_step.save(update_fields=["error", "summary", "latency_ms"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after ToolError", pending_step.step_id
                    )
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, {"error": str(e)}))
            except Exception as e:  # noqa: BLE001
                # The keys only: the values are note and task text (MIP-622).
                log.exception("Tool %s crashed on args %s", call_name, sorted(call_args))
                dur_ms = int((time.monotonic() - _tool_started) * 1000)
                err = f"Internal error in tool '{call_name}'."
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step_index,
                        "tool_name": call_name,
                        "error": err,
                        "duration_ms": dur_ms,
                    }
                )
                try:
                    pending_step.error = err
                    pending_step.summary = ""
                    pending_step.latency_ms = dur_ms
                    pending_step.save(update_fields=["error", "summary", "latency_ms"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after exception", pending_step.step_id
                    )
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, {"error": err}))
            else:
                dur_ms = int((time.monotonic() - _tool_started) * 1000)
                summary = result.pop("__summary__", "ok")
                # The pre-image never goes where `result` goes: it is replayed to
                # the model and blanked by retention. It is kept on its own column.
                undo_payload = result.pop(undo.UNDO_KEY, None)
                result_event = {
                    "type": "tool_call_result",
                    "step": step_index,
                    "tool_name": call_name,
                    "summary": summary,
                    "duration_ms": dur_ms,
                }
                # Stored BEFORE the event goes out, so `undo` is only ever promised
                # for an undo that is there to run.
                undo_json = undo.stored(call_name, undo_payload)
                if undo_json is not None:
                    try:
                        pending_step.undo_json = undo_json
                        pending_step.save(update_fields=["undo_json"])
                        result_event["undo"] = undo.event_info(pending_step)
                    except Exception:  # noqa: BLE001 — the write stands; only its undo is lost
                        pending_step.undo_json = None
                        log.exception(
                            "Failed to store the undo of step %s (%s)",
                            pending_step.step_id,
                            call_name,
                        )
                # An approve that carried edits says so, and with what the
                # person typed, so the card can show what ran. For the browser
                # only: it is on the event, never on `result`, which is
                # persisted and replayed to the model.
                if edit_patch:
                    result_event["edited"] = True
                    result_event["edited_arguments"] = edit_patch
                # A deleted project leaves the sidebar and its page closes (MIP-882).
                if call_name == "delete_project" and result.get("project_id") is not None:
                    result_event["project"] = {
                        "project_id": result["project_id"],
                        "deleted": True,
                    }
                # Approved note writes carry a compact `note` ref so the
                # frontend can refresh caches and (for body updates) push
                # the new blocks into the live Yjs doc. Additive — older
                # frontends destructure known fields and ignore this.
                # A delete carries `deleted` so the browser closes the note instead of
                # refreshing it, and a restore carries both changed fields so it
                # re-reads the note (MIP-950, MIP-951).
                if call_name in (
                    "create_note",
                    "update_note",
                    "delete_note",
                    "restore_note_version",
                ):
                    note_ref = {
                        k: result.get(k)
                        for k in ("note_id", "note_type", "title", "changed_fields", "deleted")
                        if result.get(k) is not None
                    }
                    if note_ref.get("note_id"):
                        result_event["note"] = note_ref
                # A folder delete names what went, so the browser can close tabs and drop
                # local copies. `ids_truncated` means the lists were capped: reload instead.
                if call_name == "delete_note_folder":
                    result_event["note_folder"] = {
                        k: result.get(k)
                        for k in (
                            "folder_id",
                            "deleted_folder_ids",
                            "deleted_note_ids",
                            "ids_truncated",
                        )
                        if result.get(k) is not None
                    }
                emit(result_event)
                # C3: an APPROVED write just changed the workspace, so
                # every cached read this session made before it is now
                # suspect — drop them all (generation bump, O(1)).
                if run.session_id:
                    tool_cache.invalidate_session(str(run.session_id))
                try:
                    pending_step.summary = summary
                    pending_step.result_json = result
                    pending_step.latency_ms = dur_ms
                    pending_step.save(update_fields=["summary", "result_json", "latency_ms"])
                except Exception:  # noqa: BLE001
                    log.exception(
                        "Failed to update pending step %s after approve",
                        pending_step.step_id,
                    )
                # Chips for entities the approved write CREATED (e.g. a
                # create_task_plan's milestone + tasks) so the final
                # answer's citations resolve to clickable previews.
                # Mirrors the seeded-sources pattern in run_agent: emit
                # once here, then seed the continued loop's dedup map so
                # later read-tools don't re-add (and cumulative `sources`
                # events keep including them). Tools without a
                # _ui_sources_from_tool_result branch return [] — no
                # behavior change for the other write tools.
                for src in _ui_sources_from_tool_result(call_name, result):
                    key = (src.get("entity_type"), src.get("entity_id"))
                    if all(key) and key not in resumed_sources:
                        resumed_sources[key] = src
                if resumed_sources:
                    hydrated = _hydrate_task_display_ids(
                        _apply_friendly_titles(list(resumed_sources.values()), ctx)
                    )
                    emit({"type": "sources", "sources": hydrated})
                messages.append(_assistant_function_call_turn(function_call))
                messages.append(_function_response_turn(call_name, result))

    # Continue the loop from the next step. The original run wrote
    # steps 0..step_index inclusive, so we resume at step_index + 1.
    # `resumed_sources` seeds the dedup map with chips the approved
    # write emitted above; pre-approval `sources` events were already
    # sent in the original stream and are not re-seeded.
    return _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=emit,
        run_id=run.run_id,
        starting_step=step_index + 1,
        seen_sources_by_id=resumed_sources,
        disabled_tools=disabled_tools,
        system_extra=system_extra,
        session_id=str(run.session_id) if run.session_id else None,
        cancel_event=cancel_event,
    )


# --------------------------------------------------------------------------- #
# Shared loop body                                                            #
# --------------------------------------------------------------------------- #


def _run_tool_guarded(
    tool: Any,
    call_name: str,
    call_args: dict[str, Any],
    ctx: ToolContext,
    transcript: list[AgentMessage],
) -> tuple[str, Any]:
    """Execute one read-only tool, never raising.

    Returns a tagged outcome consumed by the loop's apply phase:
      ("ok", result_dict) | ("tool_error", message) | ("crash", message)
    The tag split preserves today's two error flavors exactly: a
    ToolError's message is user-facing (ACL denials, bad args), a crash
    gets the generic internal-error string + a server-side traceback.
    Shared by the serial path and the E1 parallel executor, so error
    semantics can't diverge between them — which is also why the
    `search_web` egress check lives here: `transcript` is what the model
    has seen, and a query lifting from it never leaves (MIP-723).
    """
    try:
        if call_name == "search_web":
            check_web_query(str(call_args.get("query") or ""), transcript)
    except EgressRefused as e:
        log.warning("search_web query refused by the egress check (user %s)", ctx.user_id)
        return ("tool_error", str(e))
    try:
        return ("ok", tool.run(call_args, ctx))
    except ToolError as e:
        return ("tool_error", str(e))
    except Exception:  # noqa: BLE001
        # The keys only: the values are note and task text (MIP-622).
        log.exception("Tool %s crashed on args %s", call_name, sorted(call_args))
        return ("crash", f"Internal error in tool '{call_name}'.")


def _execute_batch_parallel(
    calls: list[FunctionCall], ctx: ToolContext, transcript: list[AgentMessage]
) -> list[tuple[tuple[str, Any], int]]:
    """E1 — run a batch of read-only tool calls concurrently.

    Returns `(outcome, duration_ms)` per call IN CALL ORDER (pool.map
    preserves it), so the caller's emit/persist/messages sequence stays
    byte-deterministic regardless of completion order — `AgentStep` rows
    and the message transcript must not depend on thread scheduling
    (resume rebuilds from them). Wall-clock is bounded by the slowest
    call either way. `duration_ms` is each tool's own execution time
    (they overlap; the sum can exceed the batch wall-clock — that's the
    point, it's the tool-side bottleneck signal).

    Each worker closes its Django DB connections in `finally`:
    short-lived executor threads would otherwise leak a connection per
    call (`close_old_connections` is for long-lived threads; these die
    with the batch). All DB WRITES (_persist_step) happen on the
    controller thread after the join — no write concurrency here.
    """
    max_workers = min(
        len(calls), int(settings.SEARCH_ENGINE.get("RAG_PARALLEL_TOOLS_MAX_WORKERS", 4))
    )

    def _task(call: FunctionCall) -> tuple[tuple[str, Any], int]:
        started = time.monotonic()
        try:
            outcome = _run_tool_guarded(
                _app_tool(call.name), call.name, dict(call.args), ctx, transcript
            )
            return outcome, int((time.monotonic() - started) * 1000)
        finally:
            try:
                connections.close_all()
            except Exception:  # noqa: BLE001 — cleanup must not eat the outcome
                log.exception("close_all failed in parallel tool worker")

    # Context propagation. `pool.map` does NOT carry contextvars into
    # worker threads, so without this a parallel batch runs every
    # `search_kb` — and therefore its rewrite and rerank LLM calls —
    # with no spend context and no LlmChoice at all. That means the
    # spend lands in `unattributed`, AND (a pre-existing bug this also
    # closes) a Claude user's parallel-batch rewrites silently run on
    # the server default provider instead of theirs.
    #
    # One Context PER CALL, snapshotted here on the parent thread: a
    # Context object cannot be entered by two threads at once, and
    # copying inside `_task` would capture the worker's empty context
    # rather than ours.
    snapshots = [spend.copy_context_for_thread() for _ in calls]

    def _run_in_context(pair):
        snapshot, call = pair
        return snapshot.run(_task, call)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(_run_in_context, zip(snapshots, calls)))


def _resolve_planning_override() -> str | None:
    """B3 provider tier split (SPOTLIGHT_FUTURE_ARCHITECTURE.md §3).

    Returns the model id to run the loop's PLANNING steps on (passed as
    `model_override`, which beats the user's `LlmChoice`), or None when
    the split is inactive and every step runs on one model exactly as
    before. The final SYNTHESIS step always uses the user's model — see
    the discard-and-rerun logic in `_drive_loop`.

    Skips (returning None) when:
      * `RAG_PLANNING_MODEL` is empty — feature off, zero code-path change;
      * the planning model IS the effective synthesis model (user picked
        the fast model, or the server default already is it) — the split
        would buy nothing but the buffering machinery;
      * the planning model's provider doesn't match the active provider.
        This guard is PREVENTIVE, unlike the reranker's try/except
        fallback: a mid-loop model error kills the run, so we must never
        hand `ClaudeClient` a `gemini-*` id (or vice versa).
    """
    planning = (settings.SEARCH_ENGINE.get("RAG_PLANNING_MODEL") or "").strip()
    choice = get_llm_choice() or _server_default_choice()
    if not planning and settings.SEARCH_ENGINE.get("AGENT_TWO_TIER_LOOP"):
        # Two-tier loop: the effort profile names a LOOP RUNG and the
        # planning steps run that rung's model, synthesis the effort's
        # own. The anatomy behind it: loop calls are 99%+ of request
        # cost and most steps are tool selection, not writing — at
        # medium a flash-lite planning call costs ~$0.006 even on a
        # cache miss vs ~$0.028 on 3.6-flash. Same-provider by
        # construction (the rung indexes the USER'S provider list), so
        # the prefix guard below never fires for a profile-driven pin —
        # it stays as the guard for the env override. The env override
        # (RAG_PLANNING_MODEL, checked first) remains the operator's
        # no-deploy escape hatch and WINS over the profile.
        profile = active_effort_profile()
        if profile is not None and profile.loop_rung is not None:
            try:
                planning = settings.LLM_CATALOG.model_for_rung(choice.provider, profile.loop_rung)
            except Exception:  # noqa: BLE001 — a pin failure must never kill the ask
                log.warning(
                    "two-tier loop_rung %r failed for provider %r; single-model run",
                    profile.loop_rung,
                    choice.provider,
                )
                planning = ""
    if not planning:
        return None
    synthesis_model = (choice.model or "").strip()
    if not synthesis_model or planning == synthesis_model:
        return None
    # Per-provider model-id prefixes. OpenAI is the trap: its models
    # start with "gpt-", not "openai-" — the old two-way ternary here
    # ("claude" else "gemini") made every openai user fail the prefix
    # test, silently disabling the split for that whole provider.
    provider_prefix = {"claude": "claude", "openai": "gpt", "gemini": "gemini"}.get(
        choice.provider, "gemini"
    )
    if not planning.startswith(provider_prefix):
        log.warning(
            "RAG_PLANNING_MODEL=%r does not match the active provider %r; "
            "planning split disabled for this run",
            planning,
            choice.provider,
        )
        return None
    return planning


def _collect_step(
    client: Any,
    *,
    messages: list[AgentMessage],
    tools: list[ToolDeclaration],
    system_instruction: str,
    emit: Callable[[dict[str, Any]], None],
    model_override: str | None = None,
    emit_deltas: bool = True,
    usage_sink: CallUsage | None = None,
    params: GenerationParams | None = None,
) -> tuple[list[str], list[FunctionCall]]:
    """Run ONE model turn and collect `(text_parts, function_calls)`.

    `emit_deltas=False` buffers text instead of streaming it — the B3
    planning pass uses this because a text-only planning response is a
    DRAFT that will be discarded and re-generated on the user's model;
    streaming it would show the user an answer that then gets replaced.
    Exceptions propagate to the caller (the loop owns error handling).

    `usage_sink`, when passed, is handed to the adapter and populated
    with this call's token/model telemetry once the stream is drained
    (the `for` loop below fully consumes it, so the sink is filled by
    the time this returns).
    """
    text_parts: list[str] = []
    calls: list[FunctionCall] = []
    stream = client.generate_step(
        messages=messages,
        tools=tools,
        system_instruction=system_instruction,
        model_override=model_override,
        usage_sink=usage_sink,
        params=params,
    )
    for text_chunk, function_call in stream:
        if function_call is not None:
            calls.append(function_call)
        elif text_chunk:
            text_parts.append(text_chunk)
            if emit_deltas:
                emit({"type": "answer_delta", "text": text_chunk})
    return text_parts, calls


def _emit_final_answer_events(
    final_answer: str,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
) -> None:
    """Close out an answered turn: resolve late citations, re-emit the
    source list, then `done`. Shared by the normal final-text path and
    the step-cap wrap-up so the two closings can't drift. The answer
    text itself has already been streamed as `answer_delta` events.
    """
    # Post-process: resolve any `[type:id]` tokens in the final
    # answer that aren't already in the source registry. Common
    # causes: agent cited an entity carried over from a prior
    # turn, mentioned in a pre-injected summary, or otherwise
    # not retrieved via a tool this turn. Lookups are ACL-gated;
    # silent failure (raw token) is preferable to leaking titles.
    late_sources = resolve_unresolved_citations(
        answer=final_answer,
        seen_keys=set(seen_sources_by_id.keys()),
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        build_task_source=lambda task_id, title, project_id: _task_source(
            task_id, title, project_id
        ),
        build_project_source=lambda project_id, project_name: _project_source(
            project_id, project_name
        ),
        build_chat_source=lambda chat_type, chat_id, thread_id: _chat_source(
            chat_type, chat_id, thread_id
        ),
        build_note_source=lambda note_type, note_id, title, parent_context: _note_source(
            note_type, note_id, title, parent_context
        ),
        build_todo_source=lambda item_id, title, local_date: _todo_source(
            item_id, title, local_date
        ),
        build_milestone_source=lambda milestone_id, title, project_id, task_id: _milestone_source(
            milestone_id, title, project_id, task_id
        ),
    )
    if late_sources:
        _apply_friendly_titles(late_sources, ctx)
        _hydrate_task_display_ids(late_sources)
        for src in late_sources:
            key = (src.get("entity_type"), src.get("entity_id"))
            if not all(key) or key in seen_sources_by_id:
                continue
            seen_sources_by_id[key] = src

    # Phase 4.2 — re-emit the sources list re-sorted by citation
    # density before `done`. Frontend already handles `sources`
    # events by replacing wholesale, so the final emit overrides
    # the in-flight tool-emission order with the more-relevant
    # citation-first order. Gated on a flag (default True) so
    # operators can flip it off if the chip-reshuffle UX bites.
    if settings.SEARCH_ENGINE.get("RAG_RANK_SOURCES_BY_CITATION", True) and seen_sources_by_id:
        ranked = _rank_sources_by_citation(final_answer, list(seen_sources_by_id.values()))
        emit({"type": "sources", "sources": ranked})
    elif late_sources:
        # Rank flag off but we added sources after the last tool
        # emit — ship the updated list so the frontend rewriter
        # has them.
        emit({"type": "sources", "sources": list(seen_sources_by_id.values())})
    emit({"type": "done"})


def _batch_signature(calls: list[FunctionCall]) -> tuple[tuple[str, str], ...]:
    """A hashable fingerprint of one step's tool-call batch.

    Two steps share a signature iff they ask for the SAME set of tools
    with the SAME arguments — the tell of a loop that isn't going
    anywhere (re-running an identical read returns the identical, often
    session-cached, result). Args are serialised canonically
    (sort_keys) so key order can't disguise a repeat; `default=str`
    keeps a stray non-JSON value from raising. Order-insensitive
    (`sorted`) so the same two calls emitted in a different order still
    count as the same batch.

    Only consulted under AGENT_DYNAMIC_STEPS; the static-cap path never
    builds a signature.
    """
    items: list[tuple[str, str]] = []
    for c in calls:
        try:
            args_key = json.dumps(c.args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            args_key = repr(c.args)
        items.append((c.name, args_key))
    return tuple(sorted(items))


def _current_date_line(user_id: str | None) -> str:
    """The "today" anchor appended to the agent's system instruction.

    Gap 1 of SPOTLIGHT_TEMPORAL_REASONING.md: the model's only innate
    sense of "now" is its training cutoff, so it computes every relative
    date ("this week", "overdue", "in three days") against a date it
    invents — and does so silently and confidently. This hands it a real
    one.

    Resolved in the ASKING USER's timezone, the same way the date-taking
    tools do (`origin.services.user_time`): agent context is personal
    scope, so a Tokyo user's "today" is their calendar day, not the
    server's UTC day, and it matches what those tools already return.

    DATE ONLY, never the wall-clock time. This string is part of the
    model prefix that the Gemini explicit cache and the OpenAI prefix
    cache key on; a value that changed every second would turn a
    guaranteed prefix hit into a guaranteed miss on every loop step. A
    date is stable for the whole day, so the prefix still hits within a
    run.

    FAIL-OPEN: date grounding is an enhancement, never a reason to 500 an
    ask. `ctx.user_id` is a real UUID on every production path, but a
    malformed id or an unavailable DB must degrade to the server day, not
    break generation — the same rule the cache layers hold themselves to.
    """
    try:
        zone = zone_for_user_id(user_id)
    except Exception:  # noqa: BLE001 — see FAIL-OPEN above
        log.warning("current-date: zone lookup failed for %r; using server zone", user_id)
        zone = resolve_zone(None)
    today = timezone.now().astimezone(zone).date()
    return f"Today's date is {today:%Y-%m-%d (%A)} in the {zone.key} timezone."


def _drive_loop(
    *,
    messages: list[AgentMessage],
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    run_id: UUID | None,
    starting_step: int,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    disabled_tools: set[str] | None = None,
    system_extra: str | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
    max_steps: int | None = None,
    session_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """The core agent loop, shared by `run_agent` and `resume_agent`.

    Returns `None` on completion, or a pause descriptor on hitting a
    write tool. See `run_agent` for the descriptor shape.

    `system_extra`, when set, is appended to the canonical
    `AGENT_SYSTEM_PROMPT` before being passed to the model. The thread
    Q&A flow (AgentAskView's thread_context branch) uses this to inject
    the thread summary + a "stay scoped to this thread" directive.

    `max_steps`, when set, overrides the `AGENT_MAX_STEPS` ceiling for
    this call. The critique-with-retrieval continuation passes a tight
    budget (`starting_step + RAG_CRITIQUE_MAX_STEPS`) so the critique can
    fire at most one more retrieval before answering.

    `session_id`, when set, keys the C3 session tool-result cache — a
    follow-up turn re-calling the same read-only tool with identical
    args reuses the stored result. None (evals, tests, sessionless
    callers) bypasses the cache entirely.
    """
    # Effort profile: per-request loop params (AGENT_EFFORT_LEVELS).
    # None = flag off or no effort-carrying choice → the legacy env
    # values, byte-identical. Resolved ONCE here — the profile cannot
    # change mid-loop.
    profile = active_effort_profile()
    # Dynamic step budget (AGENT_DYNAMIC_STEPS): the loop earns extra
    # steps while it keeps issuing genuinely-new tool calls, up to a hard
    # cap. Active ONLY on the controller's own (max_steps is None) paths —
    # an explicit caller budget (the self-critique continuation) is a
    # deliberate ceiling and is never extended. Resolved BEFORE max_steps
    # is filled in, because the check IS `max_steps is None`.
    dynamic_steps = max_steps is None and settings.SEARCH_ENGINE.get("AGENT_DYNAMIC_STEPS", False)
    if max_steps is None:
        # An explicit max_steps argument still wins over the profile:
        # the critique continuation passes a computed budget that must
        # not be re-derived.
        if profile is not None:
            base_budget = profile.max_steps
        else:
            base_budget = int(settings.SEARCH_ENGINE.get("AGENT_MAX_STEPS", 5))
        if dynamic_steps:
            # Budget is RELATIVE to where this invocation starts, so a
            # resume gets a full fresh allowance instead of inheriting
            # the pre-pause run's shrunk ceiling — the direct cause of
            # "update 10 tasks" dying mid-way (each approved write pauses
            # and resumes one step higher, and a static absolute cap of
            # 10 leaves no room to finish). run_agent starts at 0, so its
            # initial ceiling is unchanged; only extension can grow it.
            max_steps = starting_step + base_budget
            hard_cap = starting_step + int(
                settings.SEARCH_ENGINE.get("AGENT_MAX_STEPS_HARD_CAP", 30)
            )
            # A misconfigured hard cap below the base must never shrink
            # the budget — clamp up so dynamic mode is at worst a no-op.
            hard_cap = max(hard_cap, max_steps)
            # Floor of 1: a batch must be allowed to run at least once, so
            # a misconfigured limit of 0 can't stop the loop before it has
            # executed anything (it would otherwise wrap up with no work).
            repeat_limit = max(1, int(settings.SEARCH_ENGINE.get("AGENT_LOOP_REPEAT_LIMIT", 3)))
        else:
            # Legacy absolute cap — byte-identical to the pre-dynamic
            # path (this is what the flag-off wiring tests pin).
            max_steps = base_budget
    # Thinking budget rides its own flag so it can flip (and roll back)
    # independently of the effort system itself. `is not None` twice:
    # 0 is a real budget ("thinking off"), absence means model default.
    thinking_budget = (
        profile.thinking_budget
        if profile is not None and settings.SEARCH_ENGINE.get("AGENT_THINKING_BUDGETS")
        else None
    )
    gen_params = (
        GenerationParams(
            max_output_tokens=profile.max_output_tokens,
            thinking_budget=thinking_budget,
        )
        if profile is not None and (profile.max_output_tokens or thinking_budget is not None)
        else None
    )
    client = get_model_client()
    # B3 — planning-model split. When active, loop steps run on the fast
    # planning model and only the final synthesis is written by the
    # user's model, via discard-and-rerun below. None = single-model
    # behavior, byte-identical to the pre-B3 loop.
    planning_model = _resolve_planning_override()
    tools = _build_tool_declarations(disabled_tools)
    # MIP-905 (AGENT_CONTENT_LENGTH, default off): how much CONTENT this member wants Genos to
    # write. Resolved ONCE per leg, after the tools are known (a run that cannot write content,
    # like a digest, has no level). `standard` leaves `gen_params` and the prompt untouched, so
    # a Standard member and the flag off send the same bytes; Short and Long set an explicit
    # per-call output cap here (keeping the profile's thinking budget) and add the guidance block
    # at the tail of the system instruction below.
    content_level = content_length.DEFAULT_LEVEL
    if settings.SEARCH_ENGINE.get("AGENT_CONTENT_LENGTH"):
        content_level = content_length.resolve_level(ctx.user_id, (t.name for t in tools))
        content_cap = content_length.max_output_tokens(content_level)
        if content_cap:
            gen_params = GenerationParams(
                max_output_tokens=content_cap, thinking_budget=thinking_budget
            )
    # Flag-gated prompt variant (AGENT_CHEATSHEET_FROM_REGISTRY) —
    # resolved per run, stable within it (and across runs for a given
    # flag state, keeping the cacheable prefix stable).
    base_prompt = agent_system_prompt()
    system_instruction = base_prompt
    if system_extra:
        system_instruction = f"{base_prompt}\n\n{system_extra}"
    # Temporal grounding (SPOTLIGHT_TEMPORAL_REASONING.md gap 1). `ctx`
    # carries the asking user, so this is the one place every surface —
    # fresh run, resume, and both critique paths — funnels through with
    # the identity needed to resolve their timezone. Appended LAST, after
    # any per-run `system_extra`: for Claude the whole `system` string is
    # already outside the cached prefix (the breakpoint sits on the tools
    # block), for Gemini the explicit cache digests the whole string
    # either way, and for OpenAI keeping the varying date at the tail
    # lets the automatic prefix cache still share the stable head across
    # users and days. Off is a rollback lever, not a steady state.
    if settings.SEARCH_ENGINE.get("AGENT_INJECT_CURRENT_DATE", True):
        system_instruction = f"{system_instruction}\n\n{_current_date_line(ctx.user_id)}"
    # Answer language (GENOS_CAPABILITY_ROADMAP §3.4). `ctx.locale` is the
    # asking user's UI locale, forwarded by the view (and rehydrated on
    # resume). Same tail-append rationale as the date line above: per-user,
    # low-cardinality, outside the cached prefix. Empty for English /
    # unknown locales, so the majority path stays byte-identical. Off is a
    # rollback lever, not a steady state.
    if settings.SEARCH_ENGINE.get("AGENT_INJECT_LANGUAGE", True):
        language_line = language_directive(ctx.locale)
        if language_line:
            system_instruction = f"{system_instruction}\n\n{language_line}"
    # Content length (MIP-905): last, after the per-run lines, for the same cache reasons. Empty
    # for Standard, for a run that cannot write content, and with the flag off.
    content_block = content_length.guidance_block(content_level)
    if content_block:
        system_instruction = f"{system_instruction}\n\n{content_block}"

    # The latest model call's end state (MIP-905: was it cut off at the output cap?), filled by
    # `_collect_and_record`. Read by the step logic right after the call.
    last_call: dict[str, Any] = {"truncated": False}

    def _collect_and_record(
        step_index: int,
        purpose: str,
        *,
        model_override: str | None = None,
        emit_deltas: bool = True,
        tools_override: list[ToolDeclaration] | None = None,
        params_override: GenerationParams | None = None,
    ) -> tuple[list[str], list[FunctionCall]]:
        """Run one model turn AND record its per-call telemetry.

        Wraps `_collect_step` with a `CallUsage` sink + a monotonic
        latency clock, then writes one `AgentLlmCall` row. Best-effort
        (`_persist_llm_call` swallows failures), so telemetry can't break
        the loop. A raising `_collect_step` propagates to the loop's
        error handler WITHOUT recording a row — an errored call has no
        usage to attribute.

        `tools_override` replaces the loop's tool declarations for this
        one call — the step-cap wrap-up passes `[]` so the model
        physically cannot ask for another tool. None = the loop's tools.
        """
        sink = CallUsage()
        started = time.monotonic()
        # Tag the cost ledger with the same purpose this row carries.
        # The adapter records from a `finally` during generator
        # consumption, so the tag has to stay bound across the call —
        # not just around building it.
        with spend_purpose(purpose):
            parts, fcalls = _collect_step(
                client,
                messages=messages,
                tools=tools if tools_override is None else tools_override,
                system_instruction=system_instruction,
                emit=emit,
                model_override=model_override,
                emit_deltas=emit_deltas,
                usage_sink=sink,
                params=gen_params if params_override is None else params_override,
            )
        # What the step logic below needs to know about the call that just ended.
        last_call.update(
            truncated=bool(getattr(sink, "truncated", False)),
            # Thinking shares the output cap on every provider, and Gemini reports it apart.
            produced=int(getattr(sink, "output_tokens", 0) or 0)
            + int(getattr(sink, "thought_tokens", 0) or 0),
            output_cap=int(getattr(sink, "output_cap", 0) or 0),
            purpose=purpose,
            model_override=model_override,
            emit_deltas=emit_deltas,
            tools_override=tools_override,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        _persist_llm_call(
            run_id,
            ctx.team_id,
            step_index=step_index,
            purpose=purpose,
            latency_ms=latency_ms,
            usage=sink,
        )
        return parts, fcalls

    def _report_model_error(exc: Exception, step_index: int) -> None:
        """A model call raised: tell the client and record the run's error. Shared by the step's
        own call and the retry of a cut-off write, so a failure reads the same either way."""
        if isinstance(exc, ModelRefusal):
            # The provider answered, by declining. Not a crash (no
            # traceback) and not a generic failure: a specific message
            # the user can act on, and a distinct run error so refusals
            # count separately from outages. Text already streamed this
            # step stays on screen.
            log.info("Agent run %s step %d declined by the provider: %s", run_id, step_index, exc)
            emit({"type": "error", "message": MODEL_REFUSAL_MESSAGE})
            _persist_step(run_id, step_index=step_index, error="model_refusal")
            return
        log.error("Agent step %d LLM call failed", step_index, exc_info=exc)
        # The type and status only: the client sees this, the run row
        # keeps it, and the evals read the status (MIP-622).
        failure = f"LLM call failed: {describe_exception(exc)}"
        emit({"type": "error", "message": failure})
        _persist_step(run_id, step_index=step_index, error=failure)

    def _retry_cut_off_write(step_index: int) -> tuple[list[FunctionCall] | None, str]:
        """Run the step that was cut off at the output cap ONCE more with more room.

        Same model, same messages, same tools: only the cap differs, so what the model is shown
        does not change. Returns `(calls, outcome)`:

          "ok"       the retry ended normally; `calls` are its function calls
          "too_long" no retry was possible (the call ran with no cap, or a larger cap would not
                     exceed the one it ran under) or it was cut off again / returned no call:
                     the caller says the content was too long
          "ended"    the run is over and already told the client: the retry was refused by the
                     between-steps guards (cancelled, cost ceiling, credits) or the provider
                     failed or declined; the caller returns without another message

        The retry's text is dropped: the first attempt's text (what came before the cut-off
        call) was already shown, streamed or flushed, and is what the step keeps.
        """
        ran_under = int(last_call.get("output_cap") or 0)
        produced = int(last_call.get("produced") or 0)
        if ran_under <= 0:
            # No cap was sent, so the provider's own maximum applied: nothing larger to ask for.
            return None, "too_long"
        cap = min(
            max(2 * ran_under, 2 * produced, TRUNCATION_RETRY_FLOOR), TRUNCATION_RETRY_CEILING
        )
        if cap <= ran_under:
            return None, "too_long"
        # The same guards the loop runs before every model call (a retry is one).
        if _stop_before_call(step_index):
            return None, "ended"
        base = gen_params
        params = GenerationParams(
            max_output_tokens=cap,
            thinking_budget=base.thinking_budget if base is not None else None,
        )
        try:
            _parts, calls = _collect_and_record(
                step_index,
                f"{last_call['purpose']}_retry",
                model_override=last_call.get("model_override"),
                emit_deltas=False,
                tools_override=last_call.get("tools_override"),
                params_override=params,
            )
        except Exception as exc:  # noqa: BLE001 — surfaced exactly as the step's own failure is
            _report_model_error(exc, step_index)
            return None, "ended"
        if last_call["truncated"] or not calls:
            return None, "too_long"
        return calls, "ok"

    def _stop_before_call(step_index: int) -> bool:
        """The checks that run before EVERY model call: cancelled, per-request cost ceiling, the
        member's credit balance. True = stop (the client was already told, where it needs to be)."""
        # Cooperative cancellation. A Python thread cannot be killed, so
        # the only way to stop burning tokens after the client is gone is
        # to ask between steps. Checked HERE (before the LLM call) rather
        # than after, so a cancel that lands during step N stops step N+1
        # from ever being paid for.
        #
        # Bounded, not instant: an in-flight call finishes. The bound is
        # one model turn plus its tools, which is what makes this worth
        # doing at all — the alternative was a worker that ran to
        # AGENT_MAX_STEPS on a stream nobody was reading.
        if cancel_event is not None and cancel_event.is_set():
            log.info(
                "Agent run %s cancelled at step %s — client disconnected; "
                "stopping before the next model call",
                run_id,
                step_index,
            )
            return True

        # Per-request cost ceiling — the one safety control that BLOCKS.
        # Same placement and the same bound as cancellation: checked
        # before the model call, so an in-flight call finishes and real
        # spend can exceed the ceiling by at most one call. That excess
        # is ours, which is exactly what `absorbed_jpy_milli` records.
        #
        # This bounds the case a per-user daily cap structurally cannot:
        # one pathological request looping to the step cap on the most
        # expensive model can burn a day's budget faster than any daily
        # check runs.
        ceiling = int(settings.SEARCH_ENGINE.get("AI_REQUEST_MAX_JPY_MILLI", 0) or 0)
        if ceiling > 0 and spend.request_cost_jpy_milli() >= ceiling:
            spend.mark_ceiling_hit()
            log.warning(
                "Agent run %s hit the per-request cost ceiling at step %s "
                "(%s of %s milli-yen); stopping before the next model call",
                run_id,
                step_index,
                spend.request_cost_jpy_milli(),
                ceiling,
            )
            # Deliberately says nothing about money. The user did not
            # choose a budget and cannot act on a yen figure; what they
            # CAN act on is asking something narrower.
            emit({"type": "error", "message": COST_CEILING_MESSAGE})
            _persist_step(run_id, step_index=step_index, error="cost_ceiling_reached")
            return True

        # The user's credit balance, enforced where it can actually be
        # enforced. A cost-denominated limit cannot be checked up front —
        # the cost is not known until the request runs — so the entry
        # gate only asks for a positive balance and the real decision is
        # made here, between steps, against what this run has actually
        # spent.
        #
        # Same bound as the two checks above: an in-flight call finishes,
        # so a run can end a little over budget. That overshoot is ours
        # (the charge is capped at the per-request maximum regardless),
        # which is the right direction to be wrong in — the opposite
        # would cut users off before they had spent what they paid for.
        #
        # Whatever the run produced up to this point has already been
        # streamed and stays on screen: stopping early degrades the
        # answer, it does not discard it.
        if spend.credit_budget_exhausted():
            log.info(
                "Agent run %s stopped at step %s — the user's AI credits ran out "
                "mid-request (%s milli-yen spent). This run IS charged: the work "
                "was performed.",
                run_id,
                step_index,
                spend.request_cost_jpy_milli(),
            )
            emit({"type": "error", "message": CREDITS_EXHAUSTED_MESSAGE})
            _persist_step(run_id, step_index=step_index, error="credits_exhausted")
            return True
        return False

    # Per-invocation loop-guard state (AGENT_DYNAMIC_STEPS only). Maps a
    # step's batch signature → how many times it has occurred, so the
    # loop can (a) stop when one signature repeats past the limit and
    # (b) decide whether a step did NEW work worth extending the budget
    # for. Fresh per invocation, so a resume starts clean.
    batch_sig_counts: dict[tuple[tuple[str, str], ...], int] = {}

    # `while` rather than `for range(...)` so the ceiling can move: under
    # AGENT_DYNAMIC_STEPS a productive step pushes `max_steps` out (up to
    # `hard_cap`) at the bottom of the body. With the flag off `max_steps`
    # never changes, so this is exactly `for step in range(...)`.
    step = starting_step
    while step < max_steps:
        if _stop_before_call(step):
            return None
        try:
            if planning_model is None:
                accumulated_text_parts, accumulated_function_calls = _collect_and_record(
                    step, "loop"
                )
            else:
                # Planning pass on the fast model, deltas BUFFERED — a
                # step is only known to be planning vs synthesis after
                # the model responds, and a synthesis draft from the
                # fast model must never reach the user.
                accumulated_text_parts, accumulated_function_calls = _collect_and_record(
                    step,
                    "planning",
                    model_override=planning_model,
                    emit_deltas=False,
                )
                if accumulated_function_calls and (
                    settings.SEARCH_ENGINE.get("AGENT_BODY_ON_USER_MODEL")
                    and any(
                        content_length.carries_body(c.name, c.args)
                        for c in accumulated_function_calls
                    )
                ):
                    # MIP-905 (AGENT_BODY_ON_USER_MODEL, default off): the cheap planning
                    # model proposed a write that carries a body (a task, note, milestone
                    # or plan). The body is what the member reads and approves, so it is
                    # written by THEIR model: DISCARD this proposal (and its buffered text)
                    # and run the step again with no override, streaming live, exactly as
                    # the final answer is. What that call returns (a write, another tool,
                    # or text) flows on as the step's result. Costs one more call per such
                    # write; recorded as its own row so the cost is visible.
                    accumulated_text_parts, accumulated_function_calls = _collect_and_record(
                        step, "write_rerun"
                    )
                elif accumulated_function_calls:
                    # Planning step confirmed — flush the buffered
                    # thinking-out-loud text now (same events, batched).
                    for chunk in accumulated_text_parts:
                        emit({"type": "answer_delta", "text": chunk})
                else:
                    # The fast model judged the loop ready to answer.
                    # DISCARD its draft and re-run this one step with no
                    # override (= the user's model), streaming live. Net
                    # cost vs single-model: the same one smart call plus
                    # N cheap planning calls and one wasted draft; net
                    # win: every planning round-trip at fast-model
                    # latency. If the smart model instead decides to dig
                    # further (it's the better judge), its calls flow
                    # into normal tool execution below. Recorded as a
                    # SEPARATE call row (the discarded planning draft is
                    # already recorded above — so a run's llm_calls show
                    # the true two-call cost of a B3 synthesis step).
                    accumulated_text_parts, accumulated_function_calls = _collect_and_record(
                        step, "synthesis"
                    )
        except Exception as e:  # noqa: BLE001 — surface as stream error (a refusal too)
            _report_model_error(e, step)
            return None

        # ---- Output cut off at the cap (MIP-905) ----
        # A call the provider ended at its output cap has half-written whatever it was writing. A
        # write tool's arguments (a task, note or plan body) must never reach an approval card in
        # that state: try the step once more with more room, else say so and propose nothing. A
        # cut-off TEXT answer gets a notice at its end. Nothing here runs for a call that ended
        # normally (`truncated` is False), so a normal run is unchanged.
        if last_call["truncated"]:
            cut_write = bool(accumulated_function_calls) and getattr(
                REGISTRY.get(accumulated_function_calls[-1].name), "requires_approval", False
            )
            if cut_write:
                retried, outcome = _retry_cut_off_write(step)
                if outcome == "ended":
                    return None
                if retried is None:
                    log.warning(
                        "Agent run %s step %d: %s was cut off at the output cap and the retry "
                        "did not fit either; proposing nothing",
                        run_id,
                        step,
                        accumulated_function_calls[-1].name,
                    )
                    emit({"type": "error", "message": OUTPUT_TRUNCATED_MESSAGE})
                    _persist_step(run_id, step_index=step, error="output_truncated")
                    return None
                accumulated_function_calls = retried
            elif not accumulated_function_calls and accumulated_text_parts:
                accumulated_text_parts = [*accumulated_text_parts, ANSWER_CUT_OFF_NOTICE]
                emit({"type": "answer_delta", "text": ANSWER_CUT_OFF_NOTICE})

        any_text_emitted = bool(accumulated_text_parts)

        if any_text_emitted:
            _persist_step(
                run_id,
                step_index=step,
                answer_text="".join(accumulated_text_parts),
            )

        if not accumulated_function_calls:
            if not any_text_emitted:
                emit(
                    {
                        "type": "error",
                        "message": "Model returned an empty response.",
                    }
                )
                _persist_step(run_id, step_index=step, error="empty_response")
                return None

            _emit_final_answer_events(
                "".join(accumulated_text_parts), seen_sources_by_id, ctx, emit
            )
            return None

        # ---- Dynamic-budget loop guard (AGENT_DYNAMIC_STEPS only) ----
        # This step wants tools. Fingerprint the batch: a signature seen
        # for the first time is NEW work (it will earn a budget extension
        # below); one that has recurred too many times is a stuck loop we
        # stop BEFORE paying to execute it again — the repeated call can
        # only return what it already returned. Breaking here falls into
        # the step-cap wrap-up, which salvages an answer from the work
        # already gathered rather than erroring.
        step_did_new_work = False
        if dynamic_steps:
            sig = _batch_signature(accumulated_function_calls)
            batch_sig_counts[sig] = batch_sig_counts.get(sig, 0) + 1
            step_did_new_work = batch_sig_counts[sig] == 1
            if batch_sig_counts[sig] > repeat_limit:
                log.info(
                    "Agent run %s: identical tool batch repeated %d× by step %d — "
                    "stopping the loop and wrapping up (dynamic-step loop guard)",
                    run_id,
                    batch_sig_counts[sig],
                    step,
                )
                break

        # ---- A person says WRD-5, not 1234 (MIP-881) ----
        # Resolved here, once, so the session cache, the approval card, the
        # persisted step and the resume all see the same id. `call` itself
        # stays as the model wrote it: that is what it is told back. A
        # reference that names nothing is answered like an unknown tool, so a
        # write can't reach an approval card for a task that isn't there.
        resolved_args: list[dict[str, Any]] = []
        ref_errors: dict[int, str] = {}
        for i, c in enumerate(accumulated_function_calls):
            try:
                resolved_args.append(resolve_call(c.name, c.args, ctx.team_id))
            except ToolError as exc:
                resolved_args.append(dict(c.args))
                ref_errors[i] = str(exc)

        # ---- C3: resolve session-cache hits before any execution ----
        # A follow-up turn re-calling the same read-only tool with the
        # same args inside the session TTL gets the stored result — no
        # tool round-trip. Hits are injected as pre-resolved "ok"
        # outcomes (summary re-attached so the shared pop below works);
        # write/unknown tools are never consulted.
        precomputed: dict[int, tuple[str, Any]] = {}
        cached_indices: set[int] = set()
        # Per-call tool execution wall time (ms), keyed by call index —
        # filled by the serial path inline and the parallel executor
        # below. Missing/0 for cache hits and no-execution rows.
        tool_durations: dict[int, int] = {}
        if session_id and tool_cache.enabled():
            for i, c in enumerate(accumulated_function_calls):
                t = _app_tool(c.name)
                if t is None or getattr(t, "requires_approval", False) or i in ref_errors:
                    continue
                hit = tool_cache.get_cached(session_id, c.name, dict(resolved_args[i]))
                if hit is not None:
                    precomputed[i] = ("ok", {**hit["result"], "__summary__": hit["summary"]})
                    cached_indices.add(i)

        # ---- E1: parallel dispatch for read-only batches (flag-gated) ----
        # Only when the WHOLE batch is known, read-only tools and there
        # is actual parallelism to win among the cache MISSES (>1). Any
        # unknown tool or `requires_approval` write keeps the serial
        # path byte-for-byte — the approval pause returns mid-batch and
        # drops the remaining calls, and "parallel + mid-stream pause"
        # is incoherent. All `tool_call_start` events go out first in
        # call order; outcomes come back in call order too (see
        # _execute_batch_parallel), so the emit/persist/messages
        # sequence below is unchanged.
        starts_pre_emitted = False
        miss_indices = [i for i in range(len(accumulated_function_calls)) if i not in precomputed]
        if (
            settings.SEARCH_ENGINE.get("RAG_PARALLEL_TOOLS", False)
            and len(miss_indices) > 1
            and not ref_errors
            and all(
                _app_tool(c.name) is not None
                and not getattr(_app_tool(c.name), "requires_approval", False)
                for c in accumulated_function_calls
            )
        ):
            for i, call in enumerate(accumulated_function_calls):
                emit(
                    {
                        "type": "tool_call_start",
                        "step": step,
                        "tool_name": call.name,
                        "arguments": _friendly_arguments(
                            dict(resolved_args[i]), call.name, ctx=ctx
                        ),
                    }
                )
            starts_pre_emitted = True
            miss_calls = [
                dataclasses.replace(accumulated_function_calls[i], args=resolved_args[i])
                for i in miss_indices
            ]
            for i, (outcome, dur_ms) in zip(
                miss_indices, _execute_batch_parallel(miss_calls, ctx, messages)
            ):
                precomputed[i] = outcome
                tool_durations[i] = dur_ms

        for call_idx, call in enumerate(accumulated_function_calls):
            call_args = dict(resolved_args[call_idx])
            call_name = call.name

            tool = _app_tool(call_name)
            if tool is None or call_idx in ref_errors:
                emit(
                    {
                        "type": "tool_call_start",
                        "step": step,
                        "tool_name": call_name,
                        "arguments": _friendly_arguments(call_args, call_name, ctx=ctx),
                    }
                )
                err = ref_errors.get(call_idx) or f"Unknown tool: {call_name}"
                emit(
                    {
                        "type": "tool_call_error",
                        "step": step,
                        "tool_name": call_name,
                        "error": err,
                    }
                )
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    thought_signature=call.thought_signature,
                    error=err,
                )
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

            # ---- Phase 7: write tools pause the loop ----
            if getattr(tool, "requires_approval", False):
                approval_token = uuid.uuid4()
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    thought_signature=call.thought_signature,
                    summary=PENDING_APPROVAL_MARKER,
                )
                # `run_id` is included so the frontend has everything it
                # needs to POST `/decide/`. When `run_id` is None (eval
                # / test paths) we omit the field rather than serialize
                # a `null` that the wire schema doesn't expect.
                # Edit before approve: the paths of `arguments` the card may
                # render as editable, and the only ones `/decide/` accepts in
                # `edited_args`. `[]` when nothing in this call is. At those paths
                # `arguments` carries the value as the tool will receive it (a
                # fence the model copied taken off): the editor starts from it.
                editable = approval_edits.editable_paths(call_name, call_args)
                event: dict[str, Any] = {
                    "type": "tool_call_pending_approval",
                    "step": step,
                    "tool_name": call_name,
                    "arguments": approval_edits.for_card(
                        _friendly_arguments(call_args, call_name, ctx=ctx), call_args, editable
                    ),
                    "editable": editable,
                    "approval_token": str(approval_token),
                }
                if run_id is not None:
                    event["run_id"] = str(run_id)
                # Whether this call could be undone once approved, so the card
                # says "can't be undone" BEFORE the person approves. A prediction
                # from the arguments (`undo.proposal`); the result event's `undo`
                # is what says it was.
                undoable = undo.proposal(call_name, call_args)
                event["undoable"] = undoable.undoable
                if not undoable.undoable:
                    event["undo_reason"] = undoable.reason
                emit(event)
                return {
                    "paused": True,
                    "approval_token": approval_token,
                    "step": step,
                    "tool_name": call_name,
                    "arguments": call_args,
                }

            # ---- Read-only tool ----
            # Serial path: emit start + resolve inline (cache hit or
            # run). Parallel path: starts were already emitted (call
            # order) and the outcome sits in `precomputed` — everything
            # from here down is identical for both, so E1/C3 cannot
            # change events, AgentStep rows, or the message transcript,
            # only whether/when `tool.run` executed.
            if not starts_pre_emitted:
                emit(
                    {
                        "type": "tool_call_start",
                        "step": step,
                        "tool_name": call_name,
                        "arguments": _friendly_arguments(call_args, call_name, ctx=ctx),
                    }
                )
            if call_idx in precomputed:
                kind, payload = precomputed[call_idx]
            else:
                _tool_started = time.monotonic()
                kind, payload = _run_tool_guarded(tool, call_name, call_args, ctx, messages)
                tool_durations[call_idx] = int((time.monotonic() - _tool_started) * 1000)
            from_cache = call_idx in cached_indices

            if kind != "ok":
                err = str(payload)
                error_event: dict[str, Any] = {
                    "type": "tool_call_error",
                    "step": step,
                    "tool_name": call_name,
                    "error": err,
                }
                if call_idx in tool_durations:
                    error_event["duration_ms"] = tool_durations[call_idx]
                emit(error_event)
                _persist_step(
                    run_id,
                    step_index=step,
                    tool_name=call_name,
                    arguments_json=call_args,
                    thought_signature=call.thought_signature,
                    error=err,
                    latency_ms=tool_durations.get(call_idx, 0),
                )
                if trace_hook is not None:
                    try:
                        trace_hook(call_name, call_args, {"error": err})
                    except Exception:  # noqa: BLE001
                        log.exception("trace_hook failed for tool %s (error path)", call_name)
                messages.append(_assistant_function_call_turn(call))
                messages.append(_function_response_turn(call_name, {"error": err}))
                continue

            result = payload
            summary = result.pop("__summary__", "ok")
            # A read is never asked for a pre-image; if a tool returns one anyway
            # it must not reach the model, the session cache or the step row.
            result.pop(undo.UNDO_KEY, None)
            result_event = {
                "type": "tool_call_result",
                "step": step,
                "tool_name": call_name,
                "summary": summary,
            }
            if call_idx in tool_durations:
                # Server-measured execution time, mirrored from the
                # `latency_ms` persisted below. The client can't derive
                # this itself: a parallel batch emits every start before
                # any result, so arrival-time deltas would attribute the
                # whole batch wall-clock to the first call. Absent on
                # session-cache hits (nothing executed).
                result_event["duration_ms"] = tool_durations[call_idx]
            if from_cache:
                # Observability marker only — the frontend destructures
                # step/tool_name/summary and ignores unknown fields.
                result_event["cached"] = True
            emit(result_event)
            # C3: cache the fresh result for follow-up turns in this
            # session. Cached hits are NOT re-stored (their TTL should
            # date from the original execution, not the last reuse).
            if not from_cache:
                tool_cache.store(session_id, call_name, call_args, summary, result)
            _persist_step(
                run_id,
                step_index=step,
                tool_name=call_name,
                arguments_json=call_args,
                thought_signature=call.thought_signature,
                summary=summary,
                result_json=result,
                latency_ms=tool_durations.get(call_idx, 0),
            )
            if trace_hook is not None:
                try:
                    trace_hook(call_name, call_args, result)
                except Exception:  # noqa: BLE001 — trace hook must never break the loop
                    log.exception("trace_hook failed for tool %s", call_name)

            # Collect citation chips from this tool's result. Search produces
            # them via _ui_source_for_match (one per match); structured read
            # tools produce them via _ui_sources_from_tool_result. Both feed
            # the same dedup map so a task surfaced by both list_tasks and
            # search_knowledge_base in one run is still a single chip.
            new_sources: list[dict[str, Any]] = []
            if call_name == "search_knowledge_base":
                new_sources = [_ui_source_for_match(m) for m in result.get("matches", [])]
            else:
                new_sources = _ui_sources_from_tool_result(call_name, result)

            # Swap viewer-agnostic placeholders ("DM 9") for friendly
            # titles (partner / group / project name) before chips ship.
            _apply_friendly_titles(new_sources, ctx)
            # Backfill PRJ-123 display ids for search-result task sources
            # (the index stores raw task_id only).
            _hydrate_task_display_ids(new_sources)

            added = False
            for src in new_sources:
                key = (src.get("entity_type"), src.get("entity_id"))
                if not all(key):
                    continue
                existing = seen_sources_by_id.get(key)
                if existing is None:
                    seen_sources_by_id[key] = src
                    added = True
                    continue
                # Same entity surfaced by two tools (e.g. fetch_chat_thread
                # then search_knowledge_base). First-writer-wins on the chip
                # itself, but upgrade the chat deep-link target if a later
                # source pinned the exact matched message/thread the first
                # one lacked — otherwise a message_id-less chip (fetch_*)
                # would suppress a message_id-bearing one (search) and the
                # click could only land at the chat top.
                if src.get("message_id") and not existing.get("message_id"):
                    existing["message_id"] = src.get("message_id")
                    if src.get("thread_id") and not existing.get("thread_id"):
                        existing["thread_id"] = src.get("thread_id")
                    added = True

            if added:
                emit(
                    {
                        "type": "sources",
                        "sources": list(seen_sources_by_id.values()),
                    }
                )

            messages.append(_assistant_function_call_turn(call))
            messages.append(_function_response_turn(call_name, result))

        # ---- Dynamic-budget extension (AGENT_DYNAMIC_STEPS only) ----
        # Advance the step counter, then — only when we've actually
        # reached the current ceiling — grant ONE more step if this step
        # did genuinely new work, capped at `hard_cap`. Growing lazily
        # (only at the ceiling) and by one keeps the budget tied to
        # demonstrated progress: a run doing varied work keeps earning
        # room, a run that stalls into repetition earns none and lands on
        # the wrap-up. With the flag off `max_steps` never moves, so the
        # increment alone makes this identical to the old `for range`.
        step += 1
        if dynamic_steps and step >= max_steps and step_did_new_work and max_steps < hard_cap:
            max_steps += 1

    # Step cap. The loop spent its whole budget on tool calls without
    # ever writing a final answer — but everything it retrieved is
    # already in `messages`. Erroring here would discard that work, so
    # force ONE last tool-less synthesis call over the gathered material
    # and ship that as the answer ("answer with what you have"). Falls
    # back to the historical hard error when the wrap-up is disabled,
    # pointless (client gone), fails, or returns nothing.
    wrapup_answer = ""
    if settings.SEARCH_ENGINE.get("AGENT_STEP_CAP_WRAPUP", True) and not (
        cancel_event is not None and cancel_event.is_set()
    ):
        try:
            messages.append(AgentMessage(role="user", text=STEP_CAP_WRAPUP_DIRECTIVE))
            # tools_override=[]: the model physically cannot request
            # another call, so this terminates. No model_override — the
            # wrap-up is synthesis, and synthesis belongs to the user's
            # model even when the loop planned on a cheaper rung (B3).
            wrapup_parts, _wrapup_calls = _collect_and_record(
                max_steps, "step_cap_wrapup", tools_override=[]
            )
            if last_call["truncated"] and wrapup_parts:
                wrapup_parts = [*wrapup_parts, ANSWER_CUT_OFF_NOTICE]
                emit({"type": "answer_delta", "text": ANSWER_CUT_OFF_NOTICE})
            wrapup_answer = "".join(wrapup_parts).strip()
        except Exception:  # noqa: BLE001 — degrade to the historical error
            log.exception("Step-cap wrap-up call failed for run %s", run_id)
    # One row records both facts: the run hit the cap AND what (if
    # anything) the salvage answer said. answer_text="" is exactly the
    # row this path always wrote, so cap-rate queries are unchanged.
    _persist_step(
        run_id,
        step_index=max_steps,
        error="step_cap_reached",
        answer_text=wrapup_answer,
    )
    if wrapup_answer:
        # Deltas already streamed live inside _collect_and_record; this
        # resolves citations, re-ranks the chips, and closes with `done`
        # — so the run ends "done" (answer delivered) rather than
        # "step_cap", and the frontend renders a normal answered turn.
        _emit_final_answer_events(wrapup_answer, seen_sources_by_id, ctx, emit)
        return None
    emit(
        {
            "type": "error",
            "message": f"Agent did not reach a final answer in {max_steps} steps.",
        }
    )
    return None


# --------------------------------------------------------------------------- #
# Phase 3.2 — self-critique reflection wrapper                                #
# --------------------------------------------------------------------------- #


def _drive_loop_with_critique(
    *,
    user_query: str,
    messages: list[AgentMessage],
    ctx: ToolContext,
    emit: Callable[[dict[str, Any]], None],
    run_id: UUID | None,
    starting_step: int,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    disabled_tools: set[str] | None = None,
    system_extra: str | None = None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None = None,
    session_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any] | None:
    """Run `_drive_loop` with captured events, then optionally rewrite
    the draft answer via a single self-critique LLM call.

    Wrapper design (intentionally NOT inside `_drive_loop`): the inner
    loop is untouched and remains the canonical control path. The
    wrapper buffers events, runs a critique pass, then replays events
    to the real `emit` with the draft answer possibly swapped for a
    revised version.

    Precision-tightening only — the critique cannot fire more tool
    calls in this MVP. If a recall gap turns out to be the bottleneck
    on a future suite, extend the critique prompt to allow emitting
    a query the loop then executes.

    Tradeoff: TTFT becomes "end of loop + critique" because all
    answer_delta events are buffered. Acceptable for an experimental
    flag (off by default). Production rollout should weigh streaming
    vs. precision wins.

    Pause path (write-tool approval) is passed through unchanged — the
    `_drive_loop` returns a pause descriptor and the wrapper flushes
    captured events as-is. Critique never fires on a paused run.
    """
    captured_events: list[dict[str, Any]] = []
    captured_tool_results: list[dict[str, Any]] = []

    def _capture_emit(event: dict[str, Any]) -> None:
        captured_events.append(event)

    def _capture_trace(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        captured_tool_results.append({"tool_name": name, "arguments": args, "result": result})
        # Also forward to the caller's trace_hook if any (e.g. the eval runner).
        if trace_hook is not None:
            try:
                trace_hook(name, args, result)
            except Exception:  # noqa: BLE001
                log.exception("Outer trace_hook failed inside critique wrapper for %s", name)

    pause_descriptor = _drive_loop(
        messages=messages,
        ctx=ctx,
        emit=_capture_emit,
        run_id=run_id,
        starting_step=starting_step,
        seen_sources_by_id=seen_sources_by_id,
        disabled_tools=disabled_tools,
        system_extra=system_extra,
        trace_hook=_capture_trace,
        session_id=session_id,
        cancel_event=cancel_event,
    )

    if pause_descriptor is not None:
        # Loop paused on a write tool; do not critique. Flush as captured.
        for e in captured_events:
            emit(e)
        return pause_descriptor

    draft_answer = "".join(
        (e.get("text") or "") for e in captured_events if e.get("type") == "answer_delta"
    )
    if not draft_answer.strip():
        # No final answer to critique (e.g. step-cap, fatal error).
        for e in captured_events:
            emit(e)
        return None

    # Critique-with-retrieval path (RAG_CRITIQUE_RETRIEVAL). Runs a short,
    # read-only continuation of the loop so the critique can re-retrieve to
    # fix a completeness gap, not just rewrite text. Falls through to the
    # precision-only path below when the sub-flag is off, leaving that
    # measured behavior untouched.
    if bool(settings.SEARCH_ENGINE.get("RAG_CRITIQUE_RETRIEVAL", False)):
        for e in _critique_with_retrieval(
            loop1_events=captured_events,
            draft=draft_answer,
            messages=messages,
            ctx=ctx,
            run_id=run_id,
            starting_step=starting_step,
            seen_sources_by_id=seen_sources_by_id,
            disabled_tools=disabled_tools,
            system_extra=system_extra,
            trace_hook=trace_hook,
            session_id=session_id,
            cancel_event=cancel_event,
        ):
            emit(e)
        return None

    try:
        revised = _run_self_critique(
            user_query=user_query,
            tool_results=captured_tool_results,
            draft=draft_answer,
        )
    except Exception:  # noqa: BLE001 — never break the loop on critique failure
        log.exception("Self-critique LLM call failed; emitting draft unchanged")
        for e in captured_events:
            emit(e)
        return None

    if revised is None or _critique_says_keep(revised):
        # KEEP path — flush as captured.
        for e in captured_events:
            emit(e)
        return None

    # Revise path — replay everything except the draft answer_delta
    # events, then emit the revised answer once (just before `done`).
    revised_emitted = False
    for e in captured_events:
        etype = e.get("type")
        if etype == "answer_delta":
            # Drop the draft text.
            continue
        if etype == "done" and not revised_emitted:
            emit({"type": "answer_delta", "text": revised})
            revised_emitted = True
        emit(e)
    # Defensive: if there was no `done` event in the capture (shouldn't
    # happen for a clean termination) but we have a revision, surface it.
    if not revised_emitted:
        emit({"type": "answer_delta", "text": revised})
        emit({"type": "done"})
    return None


def _critique_says_keep(text: str) -> bool:
    """Recognise the literal KEEP signal. Anything else is a revision.

    Strict: only `"KEEP"` (case-insensitive) plus optional surrounding
    whitespace counts. If the model writes "KEEP, but actually …" or
    "Looks good — KEEP", treat it as a revision so we don't accidentally
    suppress a corrective rewrite.
    """
    return text.strip().upper() == "KEEP"


@spend_purpose("critique")
def _run_self_critique(
    *,
    user_query: str,
    tool_results: list[dict[str, Any]],
    draft: str,
) -> str | None:
    """Run one self-critique LLM call. Returns the model's text response
    (which may be the literal `KEEP` or a revised final answer).
    """
    prompt = AGENT_SELF_CRITIQUE_PROMPT_TEMPLATE.format(
        user_query=user_query,
        tool_summary=_format_tool_results_for_critique(tool_results),
        draft=draft,
    )
    client = get_model_client()
    chunks: list[str] = []
    for text, _fcall in client.generate_step(
        messages=[AgentMessage(role="user", text=prompt)],
        tools=[],
        system_instruction=AGENT_SELF_CRITIQUE_SYSTEM,
    ):
        if text:
            chunks.append(text)
    out = "".join(chunks).strip()
    return out or None


def _critique_with_retrieval(
    *,
    loop1_events: list[dict[str, Any]],
    draft: str,
    messages: list[AgentMessage],
    ctx: ToolContext,
    run_id: UUID | None,
    starting_step: int,
    seen_sources_by_id: dict[tuple, dict[str, Any]],
    disabled_tools: set[str] | None,
    system_extra: str | None,
    trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None,
    session_id: str | None = None,
    cancel_event: threading.Event | None = None,
) -> list[dict[str, Any]]:
    """Retrieval-capable critique: re-enter `_drive_loop` for a short,
    read-only continuation so the model can fix a *completeness* gap with
    one more retrieval, then merge the result over the draft.

    Returns the event list the caller should emit. The draft is preserved
    verbatim unless the continuation actually retrieved AND produced an
    answer — see `_merge_critique_events`.

    Reuses the full loop (tool dispatch, source emission, citation
    resolution) rather than re-implementing them: the new sources surface
    as chips and the revised answer's `[type:id]` tokens resolve exactly
    as in a normal turn, because `seen_sources_by_id` is shared.
    """
    # Continuation conversation: show the model its own draft, then the
    # completeness directive. `messages` already carries loop 1's tool
    # turns (mutated in place by `_drive_loop`).
    messages.append(AgentMessage(role="assistant", text=draft))
    messages.append(AgentMessage(role="user", text=AGENT_CRITIQUE_RETRIEVAL_DIRECTIVE))

    # Read-only continuation: disable write tools (a write would pause for
    # approval mid-critique) on top of anything the caller already disabled.
    write_tools = {t.name for t in REGISTRY.values() if getattr(t, "requires_approval", False)}
    loop2_disabled = (disabled_tools or set()) | write_tools

    # Budget: continue past loop 1's steps (its tool steps + its answer
    # step), then allow `RAG_CRITIQUE_MAX_STEPS` more (one retrieval + one
    # answer by default). `last_step + 2` skips loop 1's last tool step and
    # its answer step.
    steps_seen = [e["step"] for e in loop1_events if isinstance(e.get("step"), int)]
    last_step = max(steps_seen) if steps_seen else (starting_step - 1)
    next_step = last_step + 2
    profile = active_effort_profile()
    crit_steps = max(
        1,
        profile.critique_steps
        if profile is not None
        else int(settings.SEARCH_ENGINE.get("RAG_CRITIQUE_MAX_STEPS", 2)),
    )

    loop2_events: list[dict[str, Any]] = []
    try:
        pause2 = _drive_loop(
            messages=messages,
            ctx=ctx,
            emit=loop2_events.append,
            run_id=run_id,
            starting_step=next_step,
            seen_sources_by_id=seen_sources_by_id,
            disabled_tools=loop2_disabled,
            system_extra=system_extra,
            trace_hook=trace_hook,
            max_steps=next_step + crit_steps,
            session_id=session_id,
            cancel_event=cancel_event,
        )
    except Exception:  # noqa: BLE001 — never lose the draft on a critique fault
        log.exception("Critique-with-retrieval continuation failed; keeping draft")
        return list(loop1_events)

    return _merge_critique_events(loop1_events, loop2_events, paused=pause2 is not None)


def _merge_critique_events(
    loop1_events: list[dict[str, Any]],
    loop2_events: list[dict[str, Any]],
    *,
    paused: bool = False,
) -> list[dict[str, Any]]:
    """Decide what to stream after a retrieval-capable critique. Pure (no
    I/O) so it's unit-testable without an LLM.

    Outcomes:
      * paused / loop 2 errored / loop 2 produced no answer → loop 1
        verbatim (the draft is never lost).
      * loop 2 made NO tool call → loop 1 verbatim. No retrieval means no
        new information, so loop 2's paraphrase is discarded to preserve
        the draft's exact wording + citations (the verbatim-KEEP
        guarantee the precision path has).
      * loop 2 retrieved AND answered → loop 1 events minus its
        `answer_delta` + final `done`, then all loop 2 events (whose new
        `sources` + revised answer + `done` supersede on the frontend's
        wholesale-replace).
    """
    loop2_answer = "".join(
        (e.get("text") or "") for e in loop2_events if e.get("type") == "answer_delta"
    )
    loop2_errored = any(e.get("type") == "error" for e in loop2_events)
    loop2_retrieved = any(e.get("type") == "tool_call_start" for e in loop2_events)

    if paused or loop2_errored or not loop2_answer.strip() or not loop2_retrieved:
        return list(loop1_events)

    merged = [e for e in loop1_events if e.get("type") not in ("answer_delta", "done")]
    merged.extend(loop2_events)
    return merged


def _should_abstain_gate(
    tool_results: list[tuple[str, dict[str, Any]]],
    answer: str,
    *,
    paused: bool,
) -> bool:
    """Pure decision for the abstention gate (no I/O — unit-testable).

    Fires only when the turn ATTEMPTED retrieval yet surfaced no
    evidence, and the model didn't already abstain. The evidence rule is
    the load-bearing part:

        had_evidence = (a search_knowledge_base call returned >=1 match)
                       OR (any non-search tool completed without error)

    An empty STRUCTURED result (e.g. `list_tasks` -> no overdue tasks) is
    a grounded "the answer is zero", so a successful non-search tool — even
    with an empty payload — counts as evidence and suppresses the gate.
    Only an empty semantic search with nothing else to lean on is treated
    as "no grounding". Errs toward NOT firing (the safe direction).
    """
    if paused or not answer.strip():
        return False
    if not tool_results:  # zero-tool answer (from context/seed) — leave it
        return False
    had_evidence = any(
        (name == "search_knowledge_base" and bool((result or {}).get("matches")))
        or (name != "search_knowledge_base" and not (result or {}).get("error"))
        for name, result in tool_results
    )
    if had_evidence:
        return False
    return not is_abstention(answer)


def _apply_abstention_to_events(events: list[dict[str, Any]], message: str) -> list[dict[str, Any]]:
    """Rewrite a buffered event stream to replace the answer with `message`.

    Drops the draft `answer_delta`s and any `sources` (there is no genuine
    grounding on the gate path), then injects `message` as a single
    `answer_delta` immediately before `done`. Pure — unit-testable.
    """
    out: list[dict[str, Any]] = []
    injected = False
    for e in events:
        etype = e.get("type")
        if etype in ("answer_delta", "sources"):
            continue
        if etype == "done" and not injected:
            out.append({"type": "answer_delta", "text": message})
            injected = True
        out.append(e)
    if not injected:
        out.append({"type": "answer_delta", "text": message})
        out.append({"type": "done"})
    return out


def _run_with_abstention_gate(
    driver: Callable[
        [
            Callable[[dict[str, Any]], None],
            Callable[[str, dict[str, Any], dict[str, Any]], None] | None,
        ],
        dict[str, Any] | None,
    ],
    emit: Callable[[dict[str, Any]], None],
    outer_trace_hook: Callable[[str, dict[str, Any], dict[str, Any]], None] | None,
) -> dict[str, Any] | None:
    """Buffer the inner driver, then drop in an honest abstention if the
    turn answered with no grounding (`_should_abstain_gate`).

    Buffering (rather than a live stream filter) is deliberate: a step may
    emit preamble `answer_delta` text *and* still call a tool, so "first
    delta" isn't reliably the final synthesis — only the complete stream
    tells us the answer. Costs TTFT like the self-critique wrapper, hence
    off by default. Composes around either inner driver (plain loop or
    the critique wrapper); a pause descriptor passes straight through.
    """
    captured_events: list[dict[str, Any]] = []
    tool_results: list[tuple[str, dict[str, Any]]] = []

    def _cap_emit(event: dict[str, Any]) -> None:
        captured_events.append(event)

    def _cap_trace(name: str, args: dict[str, Any], result: dict[str, Any]) -> None:
        tool_results.append((name, result))
        if outer_trace_hook is not None:
            try:
                outer_trace_hook(name, args, result)
            except Exception:  # noqa: BLE001
                log.exception("Outer trace_hook failed inside abstention gate for %s", name)

    pause = driver(_cap_emit, _cap_trace)

    answer = "".join(
        (e.get("text") or "") for e in captured_events if e.get("type") == "answer_delta"
    )
    if _should_abstain_gate(tool_results, answer, paused=pause is not None):
        for e in _apply_abstention_to_events(captured_events, ABSTAIN_MESSAGE):
            emit(e)
    else:
        for e in captured_events:
            emit(e)
    return pause


# Limits for the tool-result blob we hand to the critique LLM. The
# critique only needs the scalar fields (status / due_date / counts) to
# verify the draft — long comment bodies don't carry weight. Mirrors
# the same per-string / per-list caps the eval judge uses, but inlined
# here to keep controller / eval coupling at zero.
_CRITIQUE_MAX_STRING_LEN = 500
_CRITIQUE_MAX_LIST_LEN = 30


def _format_tool_results_for_critique(tool_results: list[dict[str, Any]]) -> str:
    """Compact, size-bounded JSON-ish rendering for the critique prompt."""
    import json  # local — only loaded when the critique fires

    lines: list[str] = []
    for i, tr in enumerate(tool_results, start=1):
        name = tr.get("tool_name") or "?"
        args = json.dumps(tr.get("arguments") or {}, ensure_ascii=False, default=str)
        result = json.dumps(
            _truncate_for_critique(tr.get("result") or {}), ensure_ascii=False, default=str
        )
        lines.append(f"  {i}. {name}({args})\n     result: {result}")
    return "\n".join(lines) if lines else "  (no tool calls)"


def _truncate_for_critique(value: Any) -> Any:
    """Recursively head-tail long strings and cap long lists.
    Scalars (numbers, bools, dates) pass through verbatim — those are
    where the critique's grounding checks land.
    """
    if isinstance(value, str):
        if len(value) > _CRITIQUE_MAX_STRING_LEN:
            half = _CRITIQUE_MAX_STRING_LEN // 2
            return f"{value[:half]} … [{len(value) - _CRITIQUE_MAX_STRING_LEN} chars elided] … {value[-half:]}"
        return value
    if isinstance(value, list):
        truncated = [_truncate_for_critique(v) for v in value[:_CRITIQUE_MAX_LIST_LEN]]
        if len(value) > _CRITIQUE_MAX_LIST_LEN:
            truncated.append(f"… [{len(value) - _CRITIQUE_MAX_LIST_LEN} more items elided]")
        return truncated
    if isinstance(value, dict):
        return {k: _truncate_for_critique(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------- #
# Resume helpers                                                              #
# --------------------------------------------------------------------------- #


def is_pending_step(step: AgentStep) -> bool:
    """Whether `step` is a write the run paused on and nobody has answered yet:
    the marker summary, and neither a result nor an error filled in. One
    definition for the resume (`_rebuild_messages`) and for `/decide/`, which has
    to find the same step to check an edit against it."""
    return bool(
        step.tool_name
        and step.summary == PENDING_APPROVAL_MARKER
        and not step.result_json
        and not step.error
    )


def pending_step_of(run: AgentRun) -> AgentStep | None:
    """The step `resume_agent` would resume `run` on, or None: the LAST one that
    `is_pending_step`, in the order `_rebuild_messages` walks them."""
    pending: AgentStep | None = None
    candidates = (
        run.steps.filter(summary=PENDING_APPROVAL_MARKER)
        .exclude(tool_name="")
        .order_by("step_index", "step_id")
    )
    for step in candidates:
        if is_pending_step(step):
            pending = step
    return pending


def _rebuild_messages(run: AgentRun) -> tuple[list[AgentMessage], AgentStep | None]:
    """Reconstruct the conversation up to (but not including) the pending step.

    The pending step is the one whose `summary == PENDING_APPROVAL_MARKER`
    and which has neither `result_json` nor `error` filled in yet.
    Returns (messages, pending_step). `pending_step` is None if there's
    no row matching the pending shape — the caller should treat that
    as an error.
    """
    messages: list[AgentMessage] = [_user_turn(run.query)]
    pending: AgentStep | None = None

    for step in run.steps.order_by("step_index", "step_id"):
        # Pending-write rows are skipped here — we hand them back to
        # the caller to resolve.
        if is_pending_step(step):
            pending = step
            continue

        # Text-only assistant turns.
        if step.answer_text and not step.tool_name:
            messages.append(AgentMessage(role="assistant", text=step.answer_text))
            continue

        # Completed tool calls (success OR error). Skip rows that have
        # no tool_name — they're error markers like "empty_response".
        if not step.tool_name:
            continue

        fc = FunctionCall(
            name=step.tool_name,
            args=dict(step.arguments_json or {}),
            thought_signature=_coerce_signature(step.thought_signature),
        )
        messages.append(_assistant_function_call_turn(fc))
        if step.error == "user_rejected":
            # The same response the model got when it was rejected, reason included
            # (`AGENT_REJECT_REASON`), so a later leg replays what the earlier one saw.
            messages.append(
                _function_response_turn(
                    step.tool_name, approval_reason.rejection_result(step.decision_reason)
                )
            )
        elif step.error:
            messages.append(_function_response_turn(step.tool_name, {"error": step.error}))
        else:
            # A later leg replays what the earlier one saw, so the undo
            # marker (`AGENT_TELL_MODEL_UNDO`) is added here too; off, the row as it was.
            messages.append(
                _function_response_turn(
                    step.tool_name, approval_notes.with_step_notes(step.result_json or {}, step)
                )
            )

    return messages, pending
