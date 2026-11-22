"""The viewer's own agent sessions (MIP-2008), the `sessions` block of `GET /api/v2/agent/work/`.

A session is one `run_id` of the agent call log (`services/agent_calls`; how a run id is chosen is
in that module's docstring). This module sums the rows of one run into one line for the Agent
Activity tab: which app, when, how much it opened, searched, changed, decided and was refused, which
items it touched, and whether it changed things and left no completion comment.

**Only the viewer's own rows (D3).** `AgentCall` is private to the person: the query filters on the
viewer and on the team being looked at, and nothing a teammate's agent did is ever read here, so a
`user_id` filter naming someone else gives no sessions. A team owner does not see another member's
reads either; the item feed in the same response is where shared work shows up.

**No text.** The rows hold ids and words (`services/agent_calls`); a session line adds only counts
and `{type, id}` pairs. A title is not looked up for an item, so nothing here depends on the
viewer's current access to it; the one lookup of text is the title PREFIX of a note the call wrote,
to count a "Decision: ...", and a note the viewer can no longer read does not count.

**The log may be off.** `AGENT_CALL_LOG` is off in production until it is switched on: with no
rows the block is an empty list and a total of 0, never an error. It is also empty for a person
whose agents never called.

**Bounded.** One aggregate query over the window, then (only when there are sessions) one query
for the newest `MAX_SESSIONS` sessions' targets, capped at `SCAN_LIMIT` rows, plus one lookup per
note model for decisions. The number of queries does not grow with the number of sessions.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from django.db.models import Count, Max, Min, Q, Sum

from origin.models.common.agent_call_models import (
    APP_CHATGPT,
    APP_CLAUDE,
    CREDENTIAL_API_KEY,
    OP_DELETE,
    OP_READ,
    OP_SEARCH,
    OP_WRITE,
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_REFUSED_DESTRUCTIVE,
    OUTCOME_REFUSED_LIMIT,
    OUTCOME_REFUSED_SCOPE,
    OUTCOME_TOOL_ERROR,
    AgentCall,
)
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.search_engine.agent.tools.base import ToolContext
from origin.services.note_scope import readable_note_rows

MAX_SESSIONS = 100
# The newest target rows read for the sessions on the page. Items are a summary, so a very long
# run shows its newest items, not all of them.
SCAN_LIMIT = 5000
MAX_ITEMS = 10
DECISION_PREFIX = "Decision:"

# The calls that leave a comment on a task or a note: the "completion comment" the core rules ask
# for at the end of work. A session that changed things and made none of these is flagged.
COMMENT_TOOLS = (
    "add_comment",
    "comment_on_task_body",
    "reply_to_task_body_comment",
    "comment_on_note",
    "reply_to_note_comment",
)
REFUSED = (OUTCOME_REFUSED_SCOPE, OUTCOME_REFUSED_DESTRUCTIVE, OUTCOME_REFUSED_LIMIT)
FAILED = (OUTCOME_TOOL_ERROR, OUTCOME_FAILED)
CHANGING = (OP_WRITE, OP_DELETE)
# `note_type` of the two note models that can hold a decision, as the target id spells it.
_NOTE_TYPES = {"personal": 1, "task": 2}


def agent_label(credential_kind: str, app_family: str) -> str:
    """The same label the item feed gives (`agent_work.AGENTS`): `api_key`, `claude`, `chatgpt` or
    `other_app`. A key is `api_key` whatever it is used from."""
    if credential_kind == CREDENTIAL_API_KEY:
        return "api_key"
    return app_family if app_family in (APP_CLAUDE, APP_CHATGPT) else "other_app"


def _label_filter(agent: str | None) -> Q | None:
    """`agent=` as a filter on the rows. `person` is the unmarked web writes: an agent call log
    never holds one, so it leaves nothing."""
    if agent is None:
        return None
    if agent == "person":
        return Q(pk__in=[])
    if agent == "api_key":
        return Q(credential_kind=CREDENTIAL_API_KEY)
    if agent in (APP_CLAUDE, APP_CHATGPT):
        return ~Q(credential_kind=CREDENTIAL_API_KEY) & Q(app_family=agent)
    return ~Q(credential_kind=CREDENTIAL_API_KEY) & ~Q(app_family__in=[APP_CLAUDE, APP_CHATGPT])


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def empty() -> dict[str, Any]:
    return {"items": [], "total": 0}


def build(
    *,
    viewer_id: Any,
    team_id: str,
    since: datetime,
    agent: str | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """`{"items": [...newest first...], "total": n}` for the viewer's own runs in the window.

    Per session: `opened` (reads, collapsed repeats included), `searched` (search and list calls),
    `changed` (calls that changed something), `decided` ("Decision: ..." notes written),
    `refused`, `failed`, `commented`; `items_touched` / `items_changed` (distinct ids named by
    successful calls) and `items` (the first ten, changed first)."""
    if user_id is not None and str(user_id) != str(viewer_id):
        return empty()  # somebody else's sessions are not the viewer's to read
    rows = AgentCall.objects.filter(user_id=viewer_id, team_id=team_id, ts__gte=since).exclude(
        run_id=""
    )
    if (extra := _label_filter(agent)) is not None:
        rows = rows.filter(extra)

    ok = Q(outcome=OUTCOME_OK)
    grouped = list(
        rows.values("run_id", "credential_kind", "app_family")
        .annotate(
            started=Min("ts"),
            ended=Max("ts"),
            calls=Count("pk"),
            opened=Sum("count", filter=ok & Q(op=OP_READ)),
            searched=Count("pk", filter=ok & Q(op=OP_SEARCH)),
            # Successful calls that changed something. A call's `count` is not used: it is the
            # number of ids the call named (a task AND its project), not a number of changes.
            changed=Count("pk", filter=ok & Q(op__in=CHANGING)),
            commented=Count("pk", filter=ok & Q(tool__in=COMMENT_TOOLS)),
            refused=Count("pk", filter=Q(outcome__in=REFUSED)),
            failed=Count("pk", filter=Q(outcome__in=FAILED)),
        )
        .order_by("-ended", "run_id")
    )
    if not grouped:
        return empty()
    page = grouped[:MAX_SESSIONS]
    touched = _touched(viewer_id, team_id, since, {g["run_id"] for g in page})
    decided = _decisions(viewer_id, team_id, touched)

    items = []
    for g in page:
        mine = touched.get(g["run_id"], {"items": {}, "notes": set()})
        changed = g["changed"] or 0
        items.append(
            {
                "run_id": g["run_id"],
                "agent": agent_label(g["credential_kind"], g["app_family"]),
                "started_at": _iso(g["started"]),
                "ended_at": _iso(g["ended"]),
                "calls": g["calls"],
                "opened": g["opened"] or 0,
                "searched": g["searched"],
                "changed": changed,
                "decided": len(mine["notes"] & decided),
                "refused": g["refused"],
                "failed": g["failed"],
                "commented": g["commented"],
                # Items were changed and nothing in the run left a comment (the core rules ask
                # for one completion comment). Computed here; nothing extra is stored.
                "no_completion_comment": changed > 0 and g["commented"] == 0,
                "items_touched": len(mine["items"]),
                "items_changed": sum(1 for was_changed in mine["items"].values() if was_changed),
                "items": [
                    {"type": t, "id": i, "changed": was_changed}
                    for (t, i), was_changed in sorted(
                        mine["items"].items(), key=lambda kv: (not kv[1], kv[0])
                    )[:MAX_ITEMS]
                ],
            }
        )
    return {"items": items, "total": len(grouped)}


def _touched(viewer_id: Any, team_id: str, since: datetime, run_ids: set[str]) -> dict[str, dict]:
    """Per run: the distinct `(type, id)` its successful calls named, each with whether a changing
    call named it, and the notes (as `(note_type, note_id)`) its changing calls named."""
    found: dict[str, dict] = {}
    rows = (
        AgentCall.objects.filter(
            user_id=viewer_id,
            team_id=team_id,
            ts__gte=since,
            run_id__in=run_ids,
            outcome=OUTCOME_OK,
        )
        .exclude(targets=[])
        .order_by("-ts", "-id")
        .values_list("run_id", "op", "targets")[:SCAN_LIMIT]
    )
    for run_id, op, targets in rows:
        slot = found.setdefault(run_id, {"items": {}, "notes": set()})
        changing = op in CHANGING
        for target in targets if isinstance(targets, list) else ():
            if not isinstance(target, dict):
                continue
            kind, ident = str(target.get("type") or ""), str(target.get("id") or "")
            if not kind or not ident:
                continue
            key = (kind, ident)
            slot["items"][key] = slot["items"].get(key, False) or changing
            if changing and kind == "note":
                note = _note_key(ident)
                if note:
                    slot["notes"].add(note)
    return found


def _note_key(ident: str) -> tuple[int, int] | None:
    family, _, raw = ident.partition(":")
    note_type = _NOTE_TYPES.get(family)
    return (note_type, int(raw)) if note_type and raw.isdigit() else None


def _decisions(viewer_id: Any, team_id: str, touched: dict[str, dict]) -> set[tuple[int, int]]:
    """Of the notes the sessions' changing calls named, the ones titled "Decision: ..." that the
    viewer can still read."""
    wanted: set[tuple[int, int]] = set().union(*(t["notes"] for t in touched.values()))
    if not wanted:
        return set()
    out: set[tuple[int, int]] = set()
    for model, note_type in ((PersonalNoteMaster, 1), (TaskNoteMaster, 2)):
        ids = {nid for nt, nid in wanted if nt == note_type}
        if ids:
            out |= {
                (note_type, nid)
                for nid in model.objects.filter(
                    team_id=team_id, note_id__in=ids, title__istartswith=DECISION_PREFIX
                ).values_list("note_id", flat=True)
            }
    if not out:
        return set()
    readable = readable_note_rows(ToolContext(team_id=str(team_id), user_id=str(viewer_id)), out)
    return {key for key in out if key in readable}
