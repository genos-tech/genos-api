"""The agent call log (MIP-2006): one private row per MCP tool call, ids and tool names only.

The table and the rules about what it may never hold are in `models/common/agent_call_models.py`.
This module is the only writer. Read that docstring first; the short version: no argument, query,
result text, IP address or user agent is ever stored, rows are the person's alone, reads collapse
within ten minutes, and rows are kept `RETENTION_DAYS` days.

**Behind a switch, off by default.** `AGENT_CALL_LOG` (environment, read on every call so flipping
it needs no restart; not in `apis/settings.py`). Production switches it on only after the privacy
page names the log (MIP-2007). With it off, `McpView` does no work for this beyond one environment
lookup.

**It must never block or fail a call.** The view builds a `CallTrace` (plain facts, no copy of any
text), and `schedule` writes the row AFTER the response has gone out: it is attached to the Django
response as a resource closer, which the server runs once the response was sent
(`HttpResponse.close()`). Every step is guarded: a raise here is logged at WARNING and swallowed.
The write is in its own savepoint so a database error cannot poison a surrounding transaction.

**What is read from where.**
  * who: the authenticated user, the team and the credential (kind, id, official app family);
  * what: the tool's registered name (`unknown` for a name no tool has, so a caller's text is never
    stored), its class (`op`) from the registered tool, and the outcome from the shape of the
    answer;
  * on what: `{type, id}` pairs from `member_events.subjects` (the SAME allowlist the AI-app
    markers use, which keeps only validated integer / UUID ids) plus ids the result carries under
    fixed keys (`task_id`, `note_id` ...). Never a title, a name or a text;
  * how much: duration, the length of the result text, and the AI credits a search was charged.

**Run ids** (MIP-2008; the spike's findings, note "which MCP clients send Mcp-Session-Id back").
A `run_id` is one agent session, chosen in this order:

  1. ChatGPT's per-conversation id (`params._meta["openai/session"]`) when the call carries one: the
     run id is a keyed hash of it, so a conversation is one run however long it lasts;
  2. otherwise a synthetic session: one id per credential AND client name (the `clientInfo` name
     the client sends, else the first User-Agent token without its version), kept in the cache and
     renewed once that pair has been quiet for `RUN_QUIET_SECONDS`, so two apps on one key do not
     merge;
  3. an `Mcp-Session-Id` is only an EXTRA split signal on top of 2: a pair that presents an id it
     has not presented before, while it holds others, starts a new run; an id it has presented
     (up to five are remembered) returns to its own run, so two windows of one client on one key
     keep two runs instead of one run per call. A call with no id stays in the pair's current run.
     It is never the only key (claude.ai sends one id for every chat; Claude Code sends none).

Only keyed hashes of the conversation id and the session id are kept, in the cache and (as the run
id) in the row; the raw values are never stored. A repeated read collapses only within one run.
When the cache cannot answer (production's cache ignores Redis errors and then reports a miss) every
call gets a fresh run id: the rows are right, the grouping is lost until the cache is back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import F, QuerySet
from django.utils import timezone
from django.utils.crypto import salted_hmac

from origin.models.common.agent_call_models import (
    APP_OTHER,
    CREDENTIAL_API_KEY,
    MAX_TARGETS,
    OP_ADMIN,
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
    new_run_id,
)
from origin.services import member_events
from origin.services.mcp_client_family import credential_family

log = logging.getLogger(__name__)

FLAG_ENV = "AGENT_CALL_LOG"
_ON = frozenset({"1", "true", "yes", "on"})

# How long a row is kept (Ken, 2026-11-22, D1). The ONE place the number lives: the prune command
# prints it, the tests read it and the privacy line quotes it.
RETENTION_DAYS = 90
# Reads of the same object by the same credential within this window are one row with a count.
COLLAPSE_WINDOW = timedelta(minutes=10)
# A credential quiet for this long starts a new run.
RUN_QUIET_SECONDS = 20 * 60

UNKNOWN_TOOL = "unknown"
_BATCH = 1_000

# Where a client names itself and its conversation (`protocol.py` / OpenAI Apps SDK).
_CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
_OPENAI_SESSION_META = "openai/session"
_SESSION_HEADER = "Mcp-Session-Id"
_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_NAME = 40
_MAX_RAW_ID = 200

# Tools that change who can see or do something, a class of their own for the owner's counts.
ADMIN_TOOLS = frozenset(
    {
        "manage_project_members",
        "manage_note_sharing",
        "approve_join_request",
        "decline_join_request",
    }
)

# Fixed keys a SUCCESSFUL result carries ids under (the same ones `mcp_notice` reads). Lists of
# rows are read the same way. Nothing else of a result is looked at, except its length.
_RESULT_ID_KEYS = (
    ("task_id", "task"),
    ("milestone_id", "milestone"),
    ("project_id", "project"),
)
_RESULT_ROW_LISTS = ("tasks", "updated")
_COUNT_LISTS = ("results", "tasks", "updated", "items", "hits", "milestones", "projects", "notes")


def enabled() -> bool:
    """True when the log is switched on (`AGENT_CALL_LOG` = 1 / true / yes / on). Off by default."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _ON


@dataclass(frozen=True)
class RunKeys:
    """What names the session a call belongs to. Hashes and a client word only: the conversation
    id and the session id are reduced to keyed hashes the moment they are read."""

    client: str = ""  # the client's name, `[A-Za-z0-9_.-]`, at most 40 characters
    conversation: str = ""  # keyed hash of `_meta["openai/session"]`
    transport: str = ""  # keyed hash of the `Mcp-Session-Id` header


def _keyed(*parts: str) -> str:
    """A 32-character keyed hash (Django's `salted_hmac`, keyed by `SECRET_KEY`): stable for equal
    input, not reversible, not comparable with a hash from another purpose. It is an identifier for
    grouping, not a password hash: the inputs are client-chosen conversation and session ids."""
    value = "\x1f".join(p[:_MAX_RAW_ID] for p in parts)
    return salted_hmac("agent-call-run", value).hexdigest()[:32]


def _name(value: Any) -> str:
    return _NAME_UNSAFE.sub("", value)[:_MAX_NAME] if isinstance(value, str) else ""


def identify(request: Any, params: Any) -> RunKeys:
    """The run keys of one `tools/call` request. Never raises: an unreadable request is `RunKeys()`
    (one synthetic session per credential)."""
    try:
        params = params if isinstance(params, dict) else {}
        meta = params.get("_meta")
        meta = meta if isinstance(meta, dict) else {}
        info = meta.get(_CLIENT_INFO_META)
        client = _name(info.get("name")) if isinstance(info, dict) else ""
        if not client:
            # A legacy client names itself once, in `initialize`; its later calls carry only the
            # User-Agent. The product token without its version, so an upgrade does not split a run.
            raw = request.headers.get("User-Agent") or ""
            client = _name(raw.split(" ", 1)[0].split("/", 1)[0])
        conversation = meta.get(_OPENAI_SESSION_META)
        conversation = (
            _keyed("conversation", conversation)
            if isinstance(conversation, str) and conversation.strip()
            else ""
        )
        sid = (request.headers.get(_SESSION_HEADER) or "").strip()
        return RunKeys(
            client=client,
            conversation=conversation,
            transport=_keyed("transport", sid) if sid else "",
        )
    except Exception:  # noqa: BLE001 - the log never fails a call
        return RunKeys()


@dataclass
class CallTrace:
    """The facts of one `tools/call`, gathered while it runs. Holds no copy of any text: the
    arguments are kept only until the row is built (to read ids from them) and dropped."""

    user: Any
    team_id: str | None
    credential: Any
    tool_name: Any
    scope: str
    started: float = field(default_factory=time.perf_counter)
    raw_args: Any = None
    resolved_args: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    destructive_refused: bool = False
    limit_category: str | None = None
    error_code: str = ""
    failed: bool = False
    credit_request_id: str | None = None
    duration_ms: int = 0
    run_keys: RunKeys = field(default_factory=RunKeys)

    def finish(self) -> None:
        self.duration_ms = max(0, int((time.perf_counter() - self.started) * 1000))

    def mark_limited(self, category: Any) -> None:
        self.limit_category = str(category or "limit")[:64]

    def mark_failed(self, error: BaseException) -> None:
        self.failed = True
        code = getattr(error, "code", None)
        self.error_code = (f"rpc_{code}" if isinstance(code, int) else "internal_error")[:64]


# -- reading the call ---------------------------------------------------------------------------


def _tool(name: Any):
    from origin.views.public.mcp import tools  # noqa: PLC0415 - the view imports this module

    return tools.BY_NAME.get(name) if isinstance(name, str) else None


def op_for(tool) -> str:
    """The class of a registered tool. `unknown` (no such tool) counts as a read."""
    if tool is None:
        return OP_READ
    name = tool.name
    if name in ADMIN_TOOLS:
        return OP_ADMIN
    if tool.destructive or name.startswith("delete_"):
        return OP_DELETE
    if tool.write:
        return OP_WRITE
    if name == "search_workspace" or name.startswith("list_"):
        return OP_SEARCH
    return OP_READ


def _credential_facts(credential: Any) -> tuple[str, str, str]:
    kind = getattr(credential, "kind", CREDENTIAL_API_KEY)
    kind = "oauth" if kind == "oauth" else CREDENTIAL_API_KEY
    ident = str(getattr(credential, "id", "") or "")[:40]
    family = credential_family(credential) or APP_OTHER
    return kind, ident, family


def outcome_of(trace: CallTrace, tool) -> tuple[str, str]:
    """`(outcome, error_code)` from the shape of the answer. Never reads the answer's text."""
    if trace.limit_category is not None:
        return OUTCOME_REFUSED_LIMIT, trace.limit_category
    if trace.failed:
        return OUTCOME_FAILED, trace.error_code
    result = trace.result
    if not isinstance(result, dict) or not result.get("isError"):
        return OUTCOME_OK, ""
    # The scope check first, as `tools.call` does it: a read-only key calling a delete tool is
    # refused for its scope (and also reaches the destructive audit), not for the opt-in.
    if tool is not None and tool.write and trace.scope != "write":
        return OUTCOME_REFUSED_SCOPE, "read_only_key"
    if trace.destructive_refused:
        return OUTCOME_REFUSED_DESTRUCTIVE, "destructive_not_allowed"
    return OUTCOME_TOOL_ERROR, ""


def _targets_and_count(trace: CallTrace, tool) -> tuple[list[dict[str, str]], int]:
    found: list[tuple[str, str]] = []
    if tool is not None:
        args = trace.resolved_args if trace.resolved_args is not None else trace.raw_args
        if isinstance(args, dict):
            found.extend(member_events.subjects(tool.name, args))
    count = 0
    result = trace.result if isinstance(trace.result, dict) else None
    payload = result.get("structuredContent") if result and not result.get("isError") else None
    if isinstance(payload, dict):
        # The ids a result carries are read for a call that makes something (its new id exists
        # only in the result). A read names its object by its arguments alone: its result also
        # carries the project and the like, which would split one object's reads into rows.
        written = tool is not None and tool.write
        for key, kind in _RESULT_ID_KEYS if written else ():
            ident = member_events._int_id(payload.get(key))  # noqa: SLF001 - the shared id check
            if ident:
                found.append((kind, ident))
        for key in _RESULT_ROW_LISTS if written else ():
            rows = payload.get(key)
            if isinstance(rows, list):
                for row in rows[:MAX_TARGETS]:
                    ident = (
                        member_events._int_id(row.get("task_id")) if isinstance(row, dict) else None
                    )
                    if ident:
                        found.append(("task", ident))
        note_id = member_events._int_id(payload.get("note_id")) if written else None  # noqa: SLF001
        family = member_events._NOTE_FAMILY.get(  # noqa: SLF001
            str(payload.get("note_type") or "").lower().strip()
        )
        if note_id and family:
            found.append(("note", f"{family}:{note_id}"))
        for key in _COUNT_LISTS:
            rows = payload.get(key)
            if isinstance(rows, list):
                count = len(rows)
                break
    unique = list(dict.fromkeys(found))[:MAX_TARGETS]
    return [{"type": t, "id": i} for t, i in unique], count or len(unique) or 1


def _result_chars(result: Any) -> int:
    if not isinstance(result, dict):
        return 0
    total = 0
    for item in result.get("content") or ():
        text = item.get("text") if isinstance(item, dict) else None
        if isinstance(text, str):
            total += len(text)
    return total


def _credits_for(request_id: str | None) -> Decimal | None:
    """What the credit engine charged for this call (a `search_workspace` that used a model), in
    credits, or None when nothing was charged or it cannot be read."""
    if not request_id:
        return None
    try:
        from origin.search_engine.models import AiCreditEntry  # noqa: PLC0415

        milli = (
            AiCreditEntry.objects.filter(request_id=request_id, entry_type="charge")
            .values_list("credits_milli", flat=True)
            .first()
        )
        return (
            None if milli is None else (Decimal(-milli) / Decimal(1000)).quantize(Decimal("0.0001"))
        )
    except Exception:  # noqa: BLE001 - the number is a copy; the ledger stays the source
        log.debug("agent call credits lookup failed", exc_info=True)
        return None


def _run_id(credential_kind: str, credential_id: str, keys: RunKeys | None = None) -> str:
    """The run this call belongs to (see the module docstring). A cache that cannot answer gives a
    fresh id, never an error."""
    keys = keys or RunKeys()
    if keys.conversation:
        # The client says which conversation this is: no gap, no cache.
        return _keyed("run", credential_kind, credential_id, keys.conversation)
    key = f"agentcall:run:{credential_kind}:{credential_id}:{keys.client or '-'}"
    try:
        fresh = new_run_id()
        # `add` first: parallel calls at the start of a session must all land in ONE run.
        if cache.add(
            key, {"run": fresh, "sids": _bind({}, keys.transport, fresh)}, RUN_QUIET_SECONDS
        ):
            return fresh
        state = cache.get(key)
        state = state if isinstance(state, dict) and state.get("run") else {"run": None}
        sids = dict(state.get("sids") or {})
        run = state["run"]
        if not run:
            run = fresh  # the entry expired between `add` and `get`
        elif keys.transport and keys.transport in sids:
            run = sids[keys.transport]  # a window we have seen: back to its own run
        elif keys.transport and sids:
            run = fresh  # a session id this pair has not presented while others are live
        # no session id: the pair's current run. A first id after id-less calls joins that run.
        cache.set(key, {"run": run, "sids": _bind(sids, keys.transport, run)}, RUN_QUIET_SECONDS)
        return run
    except Exception:  # noqa: BLE001 - a run id is a convenience; the row is what matters
        return new_run_id()


_MAX_SIDS = 5


def _bind(sids: dict[str, str], transport: str, run: str) -> dict[str, str]:
    """`sids` with `transport` bound to `run` (the newest kept, at most `_MAX_SIDS`)."""
    if not transport:
        return sids
    out = {t: r for t, r in sids.items() if t != transport}
    out[transport] = run
    return dict(list(out.items())[-_MAX_SIDS:])


def _lock_key(fields: dict[str, Any], targets: list[dict[str, str]]) -> int:
    """A signed 64-bit key for one (person, credential, tool, object): what a collapsed read locks on."""
    raw = "|".join(str(fields[k]) for k in sorted(fields)) + json.dumps(targets, sort_keys=True)
    return int.from_bytes(hashlib.blake2b(raw.encode(), digest_size=8).digest(), "big", signed=True)


# -- writing ------------------------------------------------------------------------------------


def record(trace: CallTrace, *, now: datetime | None = None) -> AgentCall | None:
    """Write (or, for a repeated read, bump) the row for `trace`. Raises on a database error: the
    caller is `_write`, which swallows it."""
    now = now or timezone.now()
    tool = _tool(trace.tool_name)
    outcome, error_code = outcome_of(trace, tool)
    kind, credential_id, family = _credential_facts(trace.credential)
    targets, count = _targets_and_count(trace, tool)
    op = op_for(tool)
    user_id = getattr(trace.user, "id", None)
    if user_id is None:
        return None
    # Everything that can fail on its own is read BEFORE the transaction, so a failed lookup can
    # never abort the one the insert is in.
    credits = _credits_for(trace.credit_request_id)
    run_id = _run_id(kind, credential_id, trace.run_keys)
    fields = {
        "user_id": user_id,
        "team_id": trace.team_id or None,
        "credential_kind": kind,
        "credential_id": credential_id,
        # A repeated read collapses only inside ONE run: two conversations that read the same task
        # are two sessions, each with its own row (MIP-2008).
        "run_id": run_id,
        "tool": tool.name if tool is not None else UNKNOWN_TOOL,
    }
    chars = _result_chars(trace.result)
    collapsible = op == OP_READ and outcome == OUTCOME_OK and len(targets) == 1
    with transaction.atomic():
        if collapsible:
            # Two identical reads sent together would both find nothing and both insert; the
            # advisory lock (held to the end of this transaction) makes the second wait and see
            # the first's row.
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(%s)", [_lock_key(fields, targets)])
            newest = (
                AgentCall.objects.filter(
                    **fields,
                    op=OP_READ,
                    outcome=OUTCOME_OK,
                    targets=targets,
                    ts__gte=now - COLLAPSE_WINDOW,
                )
                .order_by("-ts", "-id")
                .values_list("pk", flat=True)
                .first()
            )
            if newest is not None:
                AgentCall.objects.filter(pk=newest).update(
                    count=F("count") + 1,
                    duration_ms=F("duration_ms") + trace.duration_ms,
                    result_chars=F("result_chars") + chars,
                )
                return None
        return AgentCall.objects.create(
            ts=now,
            app_family=family,
            op=op,
            outcome=outcome,
            error_code=error_code,
            targets=targets,
            count=count,
            duration_ms=trace.duration_ms,
            result_chars=chars,
            credits=credits,
            **fields,
        )


def _write(trace: CallTrace) -> None:
    try:
        record(trace)
    except Exception:  # noqa: BLE001 - the log never fails a call
        log.warning("agent call log write failed", exc_info=True)
    finally:
        # Nothing of the call is kept after the row (or the failure).
        trace.raw_args = trace.resolved_args = trace.result = None


def schedule(response: Any, trace: CallTrace) -> None:
    """Write `trace`'s row once `response` has been sent. Never raises.

    A Django response runs its resource closers when the server closes it, after the body went
    out, so the insert is not part of what the client waits for. A response without the hook
    (not an `HttpResponseBase`) is written at once instead.
    """
    try:
        trace.finish()
        closers = getattr(response, "_resource_closers", None)
        if isinstance(closers, list):
            closers.append(lambda: _write(trace))
        else:
            _write(trace)
    except Exception:  # noqa: BLE001
        log.warning("agent call log could not be scheduled", exc_info=True)


# -- retention, export, deletion ----------------------------------------------------------------


@dataclass
class PruneResult:
    matched: int
    deleted: int
    dry_run: bool


def expired(now: datetime | None = None) -> QuerySet:
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    return AgentCall.objects.filter(ts__lt=cutoff)


def prune(now: datetime | None = None, *, apply: bool = False) -> PruneResult:
    """Count (default) or delete (`apply=True`) the rows older than `RETENTION_DAYS`, in batches."""
    qs = expired(now)
    if not apply:
        return PruneResult(matched=qs.count(), deleted=0, dry_run=True)
    matched = deleted = 0
    while True:
        ids = list(qs.order_by("ts").values_list("pk", flat=True)[:_BATCH])
        if not ids:
            break
        matched += len(ids)
        deleted += AgentCall.objects.filter(pk__in=ids).delete()[0]
        if len(ids) < _BATCH:
            break
    return PruneResult(matched=matched, deleted=deleted, dry_run=False)


def export_queryset(user) -> QuerySet:
    """The person's own rows, newest first (the export caps and re-orders them)."""
    return AgentCall.objects.filter(user=user).order_by("-ts", "-id")


def export_row(c: AgentCall) -> dict[str, Any]:
    """One row for the data export. Ids and words only: the table holds nothing else."""
    return {
        "team_id": str(c.team_id) if c.team_id else None,
        "at": c.ts.isoformat(),
        "app": c.app_family,
        "credential": c.credential_kind,
        "run_id": c.run_id,
        "tool": c.tool,
        "op": c.op,
        "outcome": c.outcome,
        "error_code": c.error_code,
        "targets": c.targets,
        "count": c.count,
        "duration_ms": c.duration_ms,
        "result_chars": c.result_chars,
        "credits": str(c.credits) if c.credits is not None else None,
    }


def purge_user(user_id: Any) -> int:
    """Delete every row of one person (account deletion). Returns the number removed."""
    return AgentCall.objects.filter(user_id=user_id).delete()[0]
