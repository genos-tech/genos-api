"""Best-effort resolution of inline citation tokens in agent answers.

The agent loop emits one source chip per entity returned by a tool
call. When the agent cites an entity it didn't actually retrieve via a
tool (typically because the entity was mentioned in pre-injected
context like a note / thread summary, or carried over from a prior
turn), the inline `[type:id]` token has no matching source — and the
frontend rewriter leaves it as raw text.

This module fills that gap: after the agent emits the final answer,
the controller scans it for citation tokens that aren't already in
`seen_sources_by_id`, looks each entity up in the DB with ACL
enforcement, and returns new source dicts the controller appends to
the registry. The frontend then resolves the token to a titled link
like every other citation.

ACL is intentionally strict — if the user can't see the entity, we
return nothing rather than leak the title. The frontend is tolerant
of unresolved tokens (they render plain), so a missed resolution is
a worse-looking answer, not a security leak.

Source-builder injection: this module avoids importing from
`controller.py` (which would create a circular dependency — controller
imports this module) by accepting the `_xxx_source` builders as
function arguments. Callers pass references to `_task_source`,
`_project_source`, `_chat_source`, `_note_source`, `_todo_source`, and
`_milestone_source` from the controller.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Callable

from origin.models.chat.todo_models import ToDoItem
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import (
    chat_acl_user_ids,
    chat_note_acl_user_ids,
    may_read_note,
    milestone_acl_user_ids,
    task_acl_user_ids,
    task_note_acl_user_ids,
)
from origin.search_engine.chunkers.base import (
    CHAT_TYPE_LABEL,
    NOTE_TYPE_CHAT,
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
)

log = logging.getLogger(__name__)

# Every spelling of a citation the agent emits (§4.6 D5 + MIP-1279):
#   - the natural-prose link `[prose](type:id)` and the bare `[type:id]`;
#   - a GROUP `[task:1, task:2]` (separators `,` `;` `、` `，`);
#   - a raw id in parentheses / CJK brackets `(task:1)` `（task:12）` `【task:5】`;
#   - a bare id in a sentence ("blocked by task:42.");
#   - an un-prefixed chat token `gm:<uuid>[:thread:<uuid>]`, which models copy
#     from the source entity_id (the frontend accepts it too).
# The frontend strips all of them (citationUtils.ts), so a source cited in
# any spelling the resolver misses is stripped from the prose AND gets no
# chip: it just vanishes. Rather than model each bracket shape, scan for the
# TOKEN wherever it sits. The token grammar is what keeps this from biting
# prose, and mirrors the frontend's `_RAW_LEAK_STRIP_PATTERN`:
#   - re.ASCII, so `\w` is ASCII like JS: an id run stops at `）` or `、`, and a
#     token right after a CJK character ("参照task:12。") still starts;
#   - the entity prefix is the known set, and the id tail must contain a digit
#     ("note: remember", "task:done", "chat: hello" stay prose);
#   - the leading boundary stops it biting into a URL/path/word
#     (`https://x/project:18`, `docs.task:5`, `foo:task:1`);
#   - an un-prefixed chat label (`dm|gm|pm|mdm`) only counts when its id holds a
#     UUID, so "5 pm:30" cannot become a chat lookup.
# Keep the prefix set in sync with `CITATION_PATTERN` / `CITATION_LINK_PATTERN`
# in the frontend's citationUtils.ts: a type the frontend strips but this
# resolver can't back-fill would be silently dropped (no chip) when cited from
# context rather than retrieved.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
_CITATION_TOKEN_RE = re.compile(
    r"(?<![\w:/.-])(?:"
    r"(?P<typed>(?:chat|task|note|project|todo|milestone):[\w:-]*\d[\w:-]*)"
    rf"|(?P<chat>(?:dm|gm|pm|mdm):[\w:-]*{_UUID}[\w:-]*)"
    r")",
    re.ASCII,
)


def extract_citation_tokens(answer: str) -> set[str]:
    """Return the canonical `type:id` token of every citation in `answer`.

    Un-prefixed chat tokens come back prefixed (`chat:gm:...`), the form the
    rest of this module parses. Whether the entity exists or the user may see
    it is decided later, per type; this only finds the spelling.
    """
    tokens: set[str] = set()
    for m in _CITATION_TOKEN_RE.finditer(answer):
        # A sentence can end on the id ("task:42: blocked", "note:x-").
        token = (m.group("typed") or "chat:" + m.group("chat")).rstrip(":-")
        tokens.add(token)
    return tokens


# Frontend uses "my" as the URL label for personal notes; the LLM
# tends to echo what it sees in the system prompt (which uses
# `note_type_label` → "personal"). Accept both.
_NOTE_LABEL_TO_CODE: dict[str, int] = {
    "personal": NOTE_TYPE_PERSONAL,
    "my": NOTE_TYPE_PERSONAL,
    "task": NOTE_TYPE_TASK,
    "chat": NOTE_TYPE_CHAT,
}

_CHAT_LABEL_TO_CODE: dict[str, int] = {v: k for k, v in CHAT_TYPE_LABEL.items()}


def canonical_citation_token(token: str) -> str:
    """Lowercase `token` and spell a personal note `note:personal:<id>`.

    `[note:my:9]` and `[note:personal:9]` cite the same note (see
    `_NOTE_LABEL_TO_CODE`), and a source chip carries the `personal` spelling.
    Anything that compares a cited token with a source key (chip ranking) goes
    through this, so the two spellings cannot disagree.
    """
    token = token.lower()
    if token.startswith("note:my:"):
        return "note:personal:" + token[len("note:my:") :]
    return token


# Caller signatures for the source builders (kept loose with `Any`
# because the controller's helpers take positional args that don't
# map 1:1 across types).
TaskSourceBuilder = Callable[..., dict[str, Any]]
ProjectSourceBuilder = Callable[..., dict[str, Any]]
ChatSourceBuilder = Callable[..., dict[str, Any]]
NoteSourceBuilder = Callable[..., dict[str, Any]]
TodoSourceBuilder = Callable[..., dict[str, Any]]
MilestoneSourceBuilder = Callable[..., dict[str, Any]]


def resolve_unresolved_citations(
    *,
    answer: str,
    seen_keys: set[tuple[str | None, str | None]],
    team_id: str,
    user_id: str,
    build_task_source: TaskSourceBuilder,
    build_project_source: ProjectSourceBuilder,
    build_chat_source: ChatSourceBuilder,
    build_note_source: NoteSourceBuilder,
    build_todo_source: TodoSourceBuilder,
    build_milestone_source: MilestoneSourceBuilder,
) -> list[dict[str, Any]]:
    """Return new source dicts for citation tokens not in `seen_keys`.

    The caller is expected to merge the returned dicts into its
    `seen_sources_by_id` map and re-emit the `sources` event so the
    frontend rewriter picks them up.

    `seen_keys` are `(entity_type, entity_id)` tuples — the same shape
    `seen_sources_by_id` uses as its key.
    """
    if not answer:
        return []

    tokens = extract_citation_tokens(answer)
    if not tokens:
        return []

    # Bucket parsed citations by entity type, dropping any whose key
    # already lives in `seen_keys` (the tool loop already added them).
    task_ids: set[int] = set()
    project_ids: set[int] = set()
    note_lookups: set[tuple[str, int]] = set()
    chat_lookups: set[tuple[str, str, str | None]] = set()
    todo_item_ids: set[int] = set()
    milestone_ids: set[int] = set()

    for token in tokens:
        parts = token.split(":")
        if not parts:
            continue
        etype = parts[0]

        if etype == "task":
            tid = _safe_int(parts[1] if len(parts) >= 2 else None)
            if tid is None:
                continue
            if ("task", f"task:{tid}") in seen_keys:
                continue
            task_ids.add(tid)

        elif etype == "project":
            pid = _safe_int(parts[1] if len(parts) >= 2 else None)
            if pid is None:
                continue
            if ("project", f"project:{pid}") in seen_keys:
                continue
            project_ids.add(pid)

        elif etype == "note" and len(parts) >= 3:
            label = parts[1]
            nid = _safe_int(parts[2])
            if nid is None:
                continue
            if ("note", f"note:{label}:{nid}") in seen_keys:
                continue
            note_lookups.add((label, nid))

        elif etype == "chat" and len(parts) >= 3:
            chat_label = parts[1]
            # chat_id / thread_id are v3 UUIDs (opaque strings) — keep
            # them as-is rather than coercing to int.
            chat_id = parts[2] or None
            if chat_id is None:
                continue
            thread_id: str | None = None
            if len(parts) >= 5 and parts[3] == "thread":
                thread_id = parts[4] or None
                if thread_id is None:
                    continue
            # Controller convention: chat entity_id has no "chat:"
            # prefix (matches the chunker's id format).
            entity_id = f"{chat_label}:{chat_id}"
            if thread_id is not None:
                entity_id += f":thread:{thread_id}"
            if ("chat", entity_id) in seen_keys:
                continue
            chat_lookups.add((chat_label, chat_id, thread_id))

        elif etype == "todo" and len(parts) >= 4 and parts[2] == "item":
            # `todo:YYYY-MM-DD:item:<id>`. item_id is the globally-unique
            # PK, so the date is used only for the seen_keys check — the
            # source's date comes from the item's group at resolve time.
            iid = _safe_int(parts[3])
            if iid is None:
                continue
            if ("todo", f"todo:{parts[1]}:item:{iid}") in seen_keys:
                continue
            todo_item_ids.add(iid)

        elif etype == "milestone":
            mid = _safe_int(parts[1] if len(parts) >= 2 else None)
            if mid is None:
                continue
            if ("milestone", f"milestone:{mid}") in seen_keys:
                continue
            milestone_ids.add(mid)

    new_sources: list[dict[str, Any]] = []
    if task_ids:
        new_sources.extend(_resolve_tasks(task_ids, team_id, user_id, build_task_source))
    if project_ids:
        new_sources.extend(_resolve_projects(project_ids, team_id, user_id, build_project_source))
    if note_lookups:
        new_sources.extend(_resolve_notes(note_lookups, team_id, user_id, build_note_source))
    if chat_lookups:
        new_sources.extend(_resolve_chats(chat_lookups, user_id, build_chat_source))
    if todo_item_ids:
        new_sources.extend(_resolve_todos(todo_item_ids, team_id, user_id, build_todo_source))
    if milestone_ids:
        new_sources.extend(
            _resolve_milestones(milestone_ids, team_id, user_id, build_milestone_source)
        )
    return new_sources


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Per-type resolvers                                                          #
# --------------------------------------------------------------------------- #


def _resolve_tasks(
    task_ids: set[int],
    team_id: str,
    user_id: str,
    build: TaskSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            TaskMaster.objects.filter(
                task_id__in=task_ids,
                team_id=team_id,
                is_deleted=False,
            )
            .select_related("project")
            .values(
                "task_id",
                "title",
                "project_id",
                "assignee_id",
                "reporter_id",
            )
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the answer
        log.exception("citation_resolver: task lookup failed")
        return []

    out: list[dict[str, Any]] = []
    for row in rows:
        allowed = task_acl_user_ids(row["project_id"], row["assignee_id"], row["reporter_id"])
        if user_id not in allowed:
            continue
        out.append(
            build(
                row["task_id"],
                row["title"] or "",
                row["project_id"],
            )
        )
    return out


def _resolve_projects(
    project_ids: set[int],
    team_id: str,
    user_id: str,
    build: ProjectSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            ProjectMaster.objects.filter(
                project_id__in=project_ids,
                team_id=team_id,
                is_deleted=False,
            ).values("project_id", "project_name", "is_private")
        )
    except Exception:  # noqa: BLE001
        log.exception("citation_resolver: project lookup failed")
        return []

    private_pids = [r["project_id"] for r in rows if r["is_private"]]
    member_pids: set[int] = set()
    if private_pids:
        try:
            member_pids = {
                int(pid)
                for pid in ProjectMembers.objects.filter(
                    project_id__in=private_pids,
                    attendee_id=user_id,
                ).values_list("project_id", flat=True)
            }
        except Exception:  # noqa: BLE001
            log.exception("citation_resolver: project member lookup failed")
            return []

    out: list[dict[str, Any]] = []
    for row in rows:
        if row["is_private"] and int(row["project_id"]) not in member_pids:
            continue
        out.append(build(row["project_id"], row["project_name"] or ""))
    return out


def _resolve_notes(
    note_lookups: set[tuple[str, int]],
    team_id: str,
    user_id: str,
    build: NoteSourceBuilder,
) -> list[dict[str, Any]]:
    by_type: dict[int, list[int]] = {}
    for label, nid in note_lookups:
        code = _NOTE_LABEL_TO_CODE.get(label)
        if code is None:
            continue
        by_type.setdefault(code, []).append(nid)

    out: list[dict[str, Any]] = []
    for code, nids in by_type.items():
        if code == NOTE_TYPE_PERSONAL:
            out.extend(_resolve_personal_notes(nids, team_id, user_id, build))
        elif code == NOTE_TYPE_TASK:
            out.extend(_resolve_task_notes(nids, team_id, user_id, build))
        elif code == NOTE_TYPE_CHAT:
            out.extend(_resolve_chat_notes(nids, team_id, user_id, build))
    return out


def _resolve_personal_notes(
    nids: list[int],
    team_id: str,
    user_id: str,
    build: NoteSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            PersonalNoteMaster.objects.filter(note_id__in=nids, team_id=team_id).values(
                "note_id", "title", "owner_id"
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("citation_resolver: personal note lookup failed")
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        # Not the search-index set: see `may_read_note` (MIP-869). The team
        # is already scoped by the query above.
        if not may_read_note(
            user_id=user_id,
            note_type_code=NOTE_TYPE_PERSONAL,
            note_id=r["note_id"],
            owner_id=r["owner_id"],
        ):
            continue
        out.append(
            build(
                "personal",
                r["note_id"],
                r["title"] or "",
                {},
            )
        )
    return out


def _resolve_task_notes(
    nids: list[int],
    team_id: str,
    user_id: str,
    build: NoteSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            TaskNoteMaster.objects.filter(note_id__in=nids, team_id=team_id).values(
                "note_id", "title", "owner_id", "project_id", "task_id"
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("citation_resolver: task note lookup failed")
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        allowed = task_note_acl_user_ids(
            owner_id=r["owner_id"],
            project_id=r["project_id"],
            note_id=r["note_id"],
        )
        if user_id not in allowed:
            continue
        parent_context: dict[str, Any] = {}
        if r["project_id"]:
            parent_context["project_id"] = str(r["project_id"])
        if r["task_id"]:
            parent_context["task_id"] = str(r["task_id"])
        out.append(
            build(
                "task",
                r["note_id"],
                r["title"] or "",
                parent_context,
            )
        )
    return out


def _resolve_chat_notes(
    nids: list[int],
    team_id: str,
    user_id: str,
    build: NoteSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            ChatNoteMaster.objects.filter(note_id__in=nids, team_id=team_id).values(
                "note_id",
                "title",
                "owner_id",
                "chat_type",
                "channel_id",
                "is_thread",
                "thread_root_id",
            )
        )
    except Exception:  # noqa: BLE001
        log.exception("citation_resolver: chat note lookup failed")
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        allowed = chat_note_acl_user_ids(
            owner_id=r["owner_id"],
            chat_type_code=r["chat_type"],
            channel_id=r["channel_id"],
            note_id=r["note_id"],
        )
        if user_id not in allowed:
            continue
        # The parent_context dict KEY names stay chat_id / thread_id
        # (opaque deep-link feed-through); only the source VALUE changes
        # to the v3 channel / thread-root UUID.
        parent_context: dict[str, Any] = {}
        chat_label = CHAT_TYPE_LABEL.get(r["chat_type"])
        if chat_label:
            parent_context["chat_type"] = chat_label
        if r["channel_id"]:
            parent_context["chat_id"] = str(r["channel_id"])
        if r["thread_root_id"]:
            parent_context["thread_id"] = str(r["thread_root_id"])
        if r["is_thread"]:
            parent_context["is_thread"] = True
        out.append(
            build(
                "chat",
                r["note_id"],
                r["title"] or "",
                parent_context,
            )
        )
    return out


def _resolve_chats(
    chat_lookups: set[tuple[str, str, str | None]],
    user_id: str,
    build: ChatSourceBuilder,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for chat_label, chat_id, thread_id in chat_lookups:
        code = _CHAT_LABEL_TO_CODE.get(chat_label)
        if code is None:
            continue
        try:
            allowed = chat_acl_user_ids(code, chat_id)
        except Exception:  # noqa: BLE001
            log.exception("citation_resolver: chat ACL lookup failed")
            continue
        if user_id not in allowed:
            continue
        # No title here — `_apply_friendly_titles` in the controller
        # will fill it (DM partner / GM name / PM project name) before
        # we emit.
        out.append(build(chat_label, chat_id, thread_id))
    return out


def _resolve_todos(
    item_ids: set[int],
    team_id: str,
    user_id: str,
    build: TodoSourceBuilder,
) -> list[dict[str, Any]]:
    # ACL is enforced in the query: a todo item is private to its group's
    # owner, so constraining `group__user_id == the requester` means we
    # can only ever return the caller's own items — no separate ACL pass.
    try:
        rows = list(
            ToDoItem.objects.filter(
                item_id__in=item_ids,
                group__user_id=user_id,
                group__team_id=team_id,
            ).values("item_id", "title", "group__local_date")
        )
    except Exception:  # noqa: BLE001 — best-effort; never break the answer
        log.exception("citation_resolver: todo lookup failed")
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        local_date = r["group__local_date"]
        out.append(
            build(
                r["item_id"],
                r["title"] or "",
                local_date.isoformat() if local_date else "",
            )
        )
    return out


def _resolve_milestones(
    milestone_ids: set[int],
    team_id: str,
    user_id: str,
    build: MilestoneSourceBuilder,
) -> list[dict[str, Any]]:
    try:
        rows = list(
            MilestoneMaster.objects.filter(
                milestone_id__in=milestone_ids,
                team_id=team_id,
                is_deleted=False,
            ).values("milestone_id", "title", "project_id", "task_id")
        )
    except Exception:  # noqa: BLE001
        log.exception("citation_resolver: milestone lookup failed")
        return []

    out: list[dict[str, Any]] = []
    for r in rows:
        allowed = milestone_acl_user_ids(r["project_id"], r["milestone_id"])
        if user_id not in allowed:
            continue
        # task_id is the backing task (nullable for legacy milestones) —
        # the frontend deep-links a milestone chip through the task view.
        out.append(
            build(
                r["milestone_id"],
                r["title"] or "",
                r["project_id"],
                r["task_id"],
            )
        )
    return out
