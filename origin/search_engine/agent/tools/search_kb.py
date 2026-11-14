"""`search_knowledge_base` tool — agent's entry point into the index.

Thin wrapper over `origin.search_engine.search.search(...)` with
`for_agent=True` so the LLM sees full chunk text (not just snippets)
and gets up to a few chunks per matched entity for grounding.

The controller takes special action for this tool: after a successful
call it promotes the `matches` to citation chips via a `sources`
NDJSON event (deduplicated against earlier searches in the same run).
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

from origin.search_engine.agent.mentions import mention_search_params
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.search_engine.agent.tools.search_window import SearchWindow, skip_rewrite
from origin.search_engine.search import search

_MAX_LIMIT = 20

# What a caller may ask for. `milestone` and `note_folder` are in the index (MIP-1377); a
# folder stays out of an unrestricted search on purpose (MIP-841: it has no body to ground an
# answer on), so it only comes back when named here. The ACL filter is the same for all six.
SEARCHABLE_ENTITY_TYPES = ["chat", "task", "note", "todo", "milestone", "note_folder"]

# Longest chunk text an MCP caller gets back (MIP-798). A task body is indexed
# as one chunk, so a single hit could carry thousands of characters next to a
# `snippet` that is already enough to judge it. `get_task` / `get_note` read
# the rest. MCP only: the in-app agent keeps the full chunk, so its grounding
# does not move.
MCP_CHUNK_MAX_CHARS = 1000


def _chunk_payload(chunk: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    text = chunk.get("text", "") or ""
    out: dict[str, Any] = {"chunk_id": chunk.get("chunk_id")}
    # Cut BEFORE fencing: the MCP layer strips a fence only when the string
    # still starts with the opener and ends with the closer, so cutting the
    # wrapped string would leave the markup in the caller's context.
    if ctx.surface == "mcp" and len(text) > MCP_CHUNK_MAX_CHARS:
        text = text[:MCP_CHUNK_MAX_CHARS]
        out["text"] = wrap_workspace_content(text)
        out["truncated"] = True
    else:
        out["text"] = wrap_workspace_content(chunk.get("text", ""))
    return out


def _scope_ids(args: dict[str, Any], key: str) -> list[str] | None:
    """`[id]` for a positive integer argument, None when it is absent. A
    non-integer is refused so a typo cannot silently search everything."""
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ToolError(f"`{key}` must be an integer (got {raw!r}).")
    if value <= 0:
        raise ToolError(f"`{key}` must be a positive integer (got {raw!r}).")
    return [str(value)]


def _as_int(value: Any) -> Any:
    """The index stores ids as strings; every tool reports `project_id` as a
    number (MIP-781). Anything that is not an integer is returned as it was."""
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return value


_MILESTONE_ENTITY_RE = re.compile(r"^milestone:(\d+)$")
_FOLDER_ENTITY_RE = re.compile(r"^note_folder:(\d+)$")


def _own_ids(entity: dict[str, Any]) -> dict[str, Any]:
    """The id a milestone or note-folder hit is known by (MIP-1377).

    Both come back as `entity_id` ("milestone:12", "note_folder:7"), which the next call
    (`get_milestone_summary`, `create_note`, ...) does not take. Reported as a number, like
    `project_id`, and only on the hit it belongs to: a task hit has no `milestone_id` of its
    own here, so none is invented. A milestone document carries no id field; a folder one
    carries `folder_id`, with the `entity_id` as a fallback.
    """
    kind = entity.get("entity_type")
    entity_id = str(entity.get("entity_id") or "")
    if kind == "milestone":
        m = _MILESTONE_ENTITY_RE.match(entity_id)
        return {"milestone_id": int(m.group(1))} if m else {}
    if kind == "note_folder":
        folder_id = _as_int(entity.get("folder_id"))
        if not isinstance(folder_id, int):
            m = _FOLDER_ENTITY_RE.match(entity_id)
            folder_id = int(m.group(1)) if m else None
        return {"folder_id": folder_id} if folder_id is not None else {}
    return {}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = (args.get("query") or "").strip()
    if not query:
        return {
            "matches": [],
            "__summary__": "No query supplied.",
        }

    entity_types = args.get("entity_types")
    if entity_types is not None and not isinstance(entity_types, list):
        entity_types = None

    try:
        limit = int(args.get("limit", 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(limit, _MAX_LIMIT))

    # MCP-only date window / order (MIP-1379). Inert for the in-app agent: it reads the
    # arguments only when `ctx.surface == "mcp"`, so the shared declaration is unchanged.
    # Parsed here, before any search, so a bad date is a clear error and costs nothing.
    window = SearchWindow.from_args(args, ctx)
    no_rewrite = skip_rewrite(args, ctx)

    # Phase 10 — query rewriting is scoped to the agent path only.
    # The Spotlight typeahead never passes through here, so the
    # `RAG_USE_QUERY_REWRITE` flag can't fire an LLM call per keystroke.
    # Enabled by default (see apis/settings.py) — measured +2 net pass /
    # 0 regressions on the agent eval suite (roadmap §1.1).
    use_rewrite = bool(settings.SEARCH_ENGINE.get("RAG_USE_QUERY_REWRITE", False))
    # MCP-only opt-out (`rewrite: false`): searches the raw query, no rewrite LLM call. It can
    # only switch the rewrite off, never on; inert for the in-app agent (see `skip_rewrite`).
    if no_rewrite:
        use_rewrite = False

    # Mentions v2 — explicit person filter (LLM-chosen). A bogus or
    # unauthorized id simply matches nothing: the ACL filter still
    # applies on top of the person clause.
    person_id = (args.get("person_id") or "").strip() or None

    # Scope to one project / milestone (MIP-797). Both only NARROW: the ACL
    # filter is applied regardless, so naming something the caller cannot see
    # returns nothing rather than an error that would confirm it exists.
    project_ids = _scope_ids(args, "project_id")
    milestone_ids = _scope_ids(args, "milestone_id")

    # Mentions v2 — invisible soft boost derived from the run's
    # server-trusted resolved mentions (never from LLM args).
    boost = mention_search_params(ctx.resolved_mentions)

    result = search(
        query=query,
        team_id=ctx.team_id,
        user_id=ctx.user_id,
        entity_types=entity_types,
        limit=limit,
        for_agent=True,
        rewrite=use_rewrite,
        person_id=person_id,
        project_ids=project_ids,
        milestone_ids=milestone_ids,
        boost_person_ids=boost["boost_person_ids"],
        boost_entity_ids=boost["boost_entity_ids"],
        boost_project_ids=boost["boost_project_ids"],
        # Grounding guard, tier-independent: neither memory lane may
        # ever feed a NEW answer through this tool. The default
        # exclusions only cover the no-entity_types path; this one also
        # covers a model-supplied `entity_types=["conversation"]` /
        # ["spotlight_answer"], which used to tunnel under them. Recall
        # is `search_past_conversations`' job (and the answer→grounding
        # loop guard is a standing rule — see chunkers/spotlight docs).
        exclude_lanes=frozenset({"conversation", "spotlight_answer"}),
        **window.search_kwargs(),
    )

    # Trim the per-entity payload to what the LLM actually needs:
    # title + snippet + a few chunks. The full UI-shape (scores, ranks,
    # related_entity_ids, etc.) bloats the prompt for no benefit.
    matches: list[dict[str, Any]] = []
    for entity in result.get("results", []):
        # Wrap each chunk's body + the entity snippet in the
        # <workspace_content> boundary marker so the model is steered
        # to treat them as data, not instructions. See
        # `wrap_workspace_content` for the rationale.
        chunks = [_chunk_payload(c, ctx) for c in (entity.get("chunks") or [])]
        matches.append(
            {
                "entity_type": entity.get("entity_type"),
                "entity_id": entity.get("entity_id"),
                "title": entity.get("title"),
                "snippet": wrap_workspace_content(entity.get("snippet") or ""),
                "chunks": chunks,
                # Surface ids the model might want to pass to fetch_*:
                "chat_type": entity.get("chat_type"),
                "chat_id": entity.get("chat_id"),
                "thread_id": entity.get("thread_id"),
                "task_id": entity.get("task_id"),
                "note_id": entity.get("note_id"),
                "note_type": entity.get("note_type"),
                "project_id": _as_int(entity.get("project_id")),
                **_own_ids(entity),
                # Where a note lives (folders, project > task, chat), for a
                # viewer who may see it (MIP-839). Names are user-typed, so
                # they go through the same fence as the snippet.
                **(
                    {"context_path": wrap_workspace_content(" › ".join(entity["context_path"]))}
                    if entity.get("context_path")
                    else {}
                ),
                # Surfaced for the source-chip → workspace deep-link.
                # `message_id` lets a chat citation focus the exact
                # matched bubble; `related_entity_ids` is the fallback
                # the frontend reads when chunks pre-date direct
                # task_id / chat_* fields on note rows; the rest match
                # the SpotlightResult shape used by search-result rows
                # so the agent-source path can reuse the same router.
                "message_id": entity.get("message_id"),
                "matched_chunk_types": list(entity.get("matched_chunk_types") or []),
                "matched_terms": list(entity.get("matched_terms") or []),
                "related_entity_ids": list(entity.get("related_entity_ids") or []),
                "updated_at": entity.get("updated_at"),
            }
        )

    matches = window.apply(matches)

    return {
        "matches": matches,
        "__summary__": f"Searched knowledge base — {len(matches)} matches",
    }


SEARCH_KNOWLEDGE_BASE = Tool(
    name="search_knowledge_base",
    description=(
        "Hybrid keyword + semantic search over the user's chats, tasks, "
        "notes, and todos. Call this first when the user's question is "
        "vague or you don't know which specific entity to fetch. Returns "
        "the top matches with title, short snippet, and a few full chunks "
        "of text per match. ACL is enforced automatically — the user only "
        "sees results they have access to. `entity_types` also takes "
        "`milestone` (hit has `milestone_id`) and `note_folder` (`folder_id`)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Natural-language search query.",
            },
            "entity_types": {
                "type": "ARRAY",
                "items": {"type": "STRING", "enum": SEARCHABLE_ENTITY_TYPES},
                "description": (
                    "Optional. Restrict to a subset of entity types. Omit to "
                    "search all; `note_folder` is only returned when asked for."
                ),
            },
            "limit": {
                "type": "INTEGER",
                "description": (
                    f"Max entity-level matches to return (1-{_MAX_LIMIT}). Default 10."
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": (
                    "Optional. Search only this project (id from "
                    "`list_projects`); `limit` is filled from it. Drops "
                    "whatever belongs to no project: DMs, personal notes, "
                    "todos. Omit to search everywhere."
                ),
            },
            "milestone_id": {
                "type": "INTEGER",
                "description": (
                    "Optional. Search only this milestone (id from "
                    "`list_milestones`): its tasks, their comments and the "
                    "milestone itself. A task under a milestone's row with no "
                    "milestone set is not matched."
                ),
            },
            "person_id": {
                "type": "STRING",
                "description": (
                    "Optional. Only return content authored by, assigned "
                    "to, or owned by this team member (user_id UUID from "
                    "get_team_members or the USER-PROVIDED REFERENCES "
                    "block). Use for 'what did X say / write / work on' "
                    "questions."
                ),
            },
        },
        "required": ["query"],
    },
    run=_run,
)
