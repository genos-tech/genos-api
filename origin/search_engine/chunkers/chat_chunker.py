"""Chat chunker covering DM, GM, MDM, PM.

For each chat *thread* (including the main non-thread channel as
thread_id=None), we produce:

  - One `chat_message` chunk per individual message (good for keyword
    search and short queries).
  - One `chat_thread_window` chunk concatenating every message in the
    thread, in order (good for semantic / natural-language search and
    to give future RAG enough context).

Phase 9 — preceding-message context. Each `chat_message` chunk's
`search_text` is prefixed with the previous N messages from the
same channel/thread (default N=2, via `RAG_CHAT_CONTEXT_WINDOW`).
This gives the embedding lane real conversational context so terse
replies like "yes, ship it" embed near related preceding messages
instead of in their own desert. The `snippet_text` stays focused
on the focal message so the UI doesn't show prior text as the
"matched" content.

ACL is denormalized per chunk: we copy the chat's allowed user list
into `acl_user_ids` so retrieval-time filtering is a single
`terms` clause without any joins.
"""

from collections import defaultdict
from datetime import datetime
from typing import Iterator, Optional

from django.conf import settings

from origin.models.chat.dm_models import DMMaster, DMMessages, DMThreadMessages
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages
from origin.models.chat.mdm_models import (
    MDMMaster,
    MDMMembers,
    MDMMessages,
    MDMThreadMessages,
)
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

from origin.search_engine.chunkers.base import (
    CHAT_TYPE_DM,
    CHAT_TYPE_GM,
    CHAT_TYPE_LABEL,
    CHAT_TYPE_MDM,
    CHAT_TYPE_PM,
    Chunk,
    EntityChunks,
    chat_entity_id,
    iso,
    make_snippet,
)
from origin.search_engine.models import ThreadSummary
from origin.search_engine.text_extraction import extract_text

# v2: thread-window chunks are suppressed for any thread that already
# has an LLM-generated `ThreadSummary` row — the abstract is strictly
# better than raw concatenation for vector recall. Built once per
# ingest run via `_load_summarized_threads`.
_LABEL_TO_CHAT_TYPE_CODE = {v: k for k, v in CHAT_TYPE_LABEL.items()}


def iter_dm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    """Yield one EntityChunks per DM thread (including main channel)."""

    dm_qs = DMMaster.objects.filter(is_deleted=False).select_related("team")

    msg_qs = DMMessages.objects.filter(is_deleted=False).select_related("dm", "dm__team", "task")
    thread_msg_qs = DMThreadMessages.objects.filter(is_deleted=False).select_related(
        "dm", "dm__team"
    )

    if since is not None:
        dirty_ids = set()
        for dm_id in msg_qs.filter(ts_updated_at__gte=since).values_list("dm_id", flat=True):
            dirty_ids.add(dm_id)
        for dm_id in thread_msg_qs.filter(ts_updated_at__gte=since).values_list(
            "dm_id", flat=True
        ):
            dirty_ids.add(dm_id)
        dm_qs = dm_qs.filter(dm_id__in=dirty_ids)
        msg_qs = msg_qs.filter(dm_id__in=dirty_ids)
        thread_msg_qs = thread_msg_qs.filter(dm_id__in=dirty_ids)

    msgs_by_dm = defaultdict(list)
    for m in msg_qs.order_by("dm_id", "message_id"):
        msgs_by_dm[m.dm_id].append(m)
    thread_msgs_by_key = defaultdict(list)
    for tm in thread_msg_qs.order_by("dm_id", "thread_id", "thread_message_id"):
        thread_msgs_by_key[(tm.dm_id, tm.thread_id)].append(tm)

    # v2 — pre-resolve sender names + summarized threads in one pass.
    sender_ids: set = set()
    for msg_list in msgs_by_dm.values():
        for msg in msg_list:
            if msg.sender_id:
                sender_ids.add(msg.sender_id)
    for tms in thread_msgs_by_key.values():
        for tm in tms:
            if tm.sender_id:
                sender_ids.add(tm.sender_id)
    sender_names = _load_sender_names(sender_ids)
    summarized = _load_summarized_threads()

    for dm in dm_qs:
        if not dm.team_id:
            continue
        acl = [str(dm.user_1_id), str(dm.user_2_id)]
        yield from _emit_chat_chunks(
            chat_label="dm",
            chat_id=dm.dm_id,
            team_id=str(dm.team_id),
            acl_user_ids=acl,
            chat_title=f"DM {dm.dm_id}",
            messages=msgs_by_dm.get(dm.dm_id, []),
            thread_msgs_by_thread_id=_group_threads(thread_msgs_by_key, dm.dm_id),
            message_body_attr="message_body",
            thread_body_attr="thread_message_body",
            sender_names=sender_names,
            summarized_threads=summarized,
        )


def iter_gm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    gm_qs = GMMaster.objects.filter(is_deleted=False).select_related("owner_team")
    msg_qs = GMMessages.objects.filter(is_deleted=False).select_related(
        "gm", "gm__owner_team", "task"
    )
    thread_msg_qs = GMThreadMessages.objects.filter(is_deleted=False).select_related(
        "gm", "gm__owner_team"
    )

    if since is not None:
        dirty_ids = set()
        for gm_id in msg_qs.filter(ts_updated_at__gte=since).values_list("gm_id", flat=True):
            dirty_ids.add(gm_id)
        for gm_id in thread_msg_qs.filter(ts_updated_at__gte=since).values_list(
            "gm_id", flat=True
        ):
            dirty_ids.add(gm_id)
        gm_qs = gm_qs.filter(gm_id__in=dirty_ids)
        msg_qs = msg_qs.filter(gm_id__in=dirty_ids)
        thread_msg_qs = thread_msg_qs.filter(gm_id__in=dirty_ids)

    members_by_gm = defaultdict(list)
    for member in GMMembers.objects.filter(gm_id__in=gm_qs.values_list("gm_id", flat=True)).values(
        "gm_id", "attendee_id"
    ):
        if member["attendee_id"] is not None:
            members_by_gm[member["gm_id"]].append(str(member["attendee_id"]))

    msgs_by_gm = defaultdict(list)
    for m in msg_qs.order_by("gm_id", "message_id"):
        msgs_by_gm[m.gm_id].append(m)
    thread_msgs_by_key = defaultdict(list)
    for tm in thread_msg_qs.order_by("gm_id", "thread_id", "thread_message_id"):
        thread_msgs_by_key[(tm.gm_id, tm.thread_id)].append(tm)

    sender_ids: set = set()
    for msg_list in msgs_by_gm.values():
        for msg in msg_list:
            if msg.sender_id:
                sender_ids.add(msg.sender_id)
    for tms in thread_msgs_by_key.values():
        for tm in tms:
            if tm.sender_id:
                sender_ids.add(tm.sender_id)
    sender_names = _load_sender_names(sender_ids)
    summarized = _load_summarized_threads()

    for gm in gm_qs:
        if not gm.owner_team_id:
            continue
        yield from _emit_chat_chunks(
            chat_label="gm",
            chat_id=gm.gm_id,
            team_id=str(gm.owner_team_id),
            acl_user_ids=members_by_gm.get(gm.gm_id, []),
            chat_title=gm.group_name or f"Group {gm.gm_id}",
            messages=msgs_by_gm.get(gm.gm_id, []),
            thread_msgs_by_thread_id=_group_threads(thread_msgs_by_key, gm.gm_id),
            message_body_attr="message_body",
            thread_body_attr="thread_message_body",
            sender_names=sender_names,
            summarized_threads=summarized,
        )


def iter_mdm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    mdm_qs = MDMMaster.objects.filter(is_deleted=False).select_related("owner_team")
    msg_qs = MDMMessages.objects.filter(is_deleted=False).select_related(
        "mdm", "mdm__owner_team", "task"
    )
    thread_msg_qs = MDMThreadMessages.objects.filter(is_deleted=False).select_related(
        "mdm", "mdm__owner_team"
    )

    if since is not None:
        dirty_ids = set()
        for mdm_id in msg_qs.filter(ts_updated_at__gte=since).values_list("mdm_id", flat=True):
            dirty_ids.add(mdm_id)
        for mdm_id in thread_msg_qs.filter(ts_updated_at__gte=since).values_list(
            "mdm_id", flat=True
        ):
            dirty_ids.add(mdm_id)
        mdm_qs = mdm_qs.filter(mdm_id__in=dirty_ids)
        msg_qs = msg_qs.filter(mdm_id__in=dirty_ids)
        thread_msg_qs = thread_msg_qs.filter(mdm_id__in=dirty_ids)

    members_by_mdm = defaultdict(list)
    for member in MDMMembers.objects.filter(
        mdm_id__in=mdm_qs.values_list("mdm_id", flat=True)
    ).values("mdm_id", "attendee_id"):
        if member["attendee_id"] is not None:
            members_by_mdm[member["mdm_id"]].append(str(member["attendee_id"]))

    msgs_by_mdm = defaultdict(list)
    for m in msg_qs.order_by("mdm_id", "message_id"):
        msgs_by_mdm[m.mdm_id].append(m)
    thread_msgs_by_key = defaultdict(list)
    for tm in thread_msg_qs.order_by("mdm_id", "thread_id", "thread_message_id"):
        thread_msgs_by_key[(tm.mdm_id, tm.thread_id)].append(tm)

    sender_ids: set = set()
    for msg_list in msgs_by_mdm.values():
        for msg in msg_list:
            if msg.sender_id:
                sender_ids.add(msg.sender_id)
    for tms in thread_msgs_by_key.values():
        for tm in tms:
            if tm.sender_id:
                sender_ids.add(tm.sender_id)
    sender_names = _load_sender_names(sender_ids)
    summarized = _load_summarized_threads()

    for mdm in mdm_qs:
        if not mdm.owner_team_id:
            continue
        yield from _emit_chat_chunks(
            chat_label="mdm",
            chat_id=mdm.mdm_id,
            team_id=str(mdm.owner_team_id),
            acl_user_ids=members_by_mdm.get(mdm.mdm_id, []),
            chat_title=mdm.display_name or f"MDM {mdm.mdm_id}",
            messages=msgs_by_mdm.get(mdm.mdm_id, []),
            thread_msgs_by_thread_id=_group_threads(thread_msgs_by_key, mdm.mdm_id),
            message_body_attr="message_body",
            thread_body_attr="thread_message_body",
            sender_names=sender_names,
            summarized_threads=summarized,
        )


def iter_pm_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    """PM chat = the conversation on a project. No PMMaster; we treat
    each ProjectMaster as the chat container."""
    project_qs = ProjectMaster.objects.filter(is_deleted=False).select_related("team")
    msg_qs = PMMessages.objects.filter(is_deleted=False).select_related(
        "project", "project__team", "task"
    )
    thread_msg_qs = PMThreadMessages.objects.filter(is_deleted=False).select_related(
        "project", "project__team"
    )

    if since is not None:
        dirty_ids = set()
        for project_id in msg_qs.filter(ts_updated_at__gte=since).values_list(
            "project_id", flat=True
        ):
            dirty_ids.add(project_id)
        for project_id in thread_msg_qs.filter(ts_updated_at__gte=since).values_list(
            "project_id", flat=True
        ):
            dirty_ids.add(project_id)
        project_qs = project_qs.filter(project_id__in=dirty_ids)
        msg_qs = msg_qs.filter(project_id__in=dirty_ids)
        thread_msg_qs = thread_msg_qs.filter(project_id__in=dirty_ids)

    members_by_project = defaultdict(list)
    for member in ProjectMembers.objects.filter(
        project_id__in=project_qs.values_list("project_id", flat=True)
    ).values("project_id", "attendee_id"):
        if member["attendee_id"] is not None:
            members_by_project[member["project_id"]].append(str(member["attendee_id"]))

    msgs_by_project = defaultdict(list)
    for m in msg_qs.order_by("project_id", "message_id"):
        msgs_by_project[m.project_id].append(m)
    thread_msgs_by_key = defaultdict(list)
    for tm in thread_msg_qs.order_by("project_id", "thread_id", "thread_message_id"):
        thread_msgs_by_key[(tm.project_id, tm.thread_id)].append(tm)

    sender_ids: set = set()
    for msg_list in msgs_by_project.values():
        for msg in msg_list:
            if msg.sender_id:
                sender_ids.add(msg.sender_id)
    for tms in thread_msgs_by_key.values():
        for tm in tms:
            if tm.sender_id:
                sender_ids.add(tm.sender_id)
    sender_names = _load_sender_names(sender_ids)
    summarized = _load_summarized_threads()

    for project in project_qs:
        if not project.team_id:
            continue
        yield from _emit_chat_chunks(
            chat_label="pm",
            chat_id=project.project_id,
            team_id=str(project.team_id),
            acl_user_ids=members_by_project.get(project.project_id, []),
            chat_title=project.project_name or f"Project {project.project_id}",
            messages=msgs_by_project.get(project.project_id, []),
            thread_msgs_by_thread_id=_group_threads(thread_msgs_by_key, project.project_id),
            message_body_attr="message_body",
            thread_body_attr="thread_message_body",
            project_id=str(project.project_id),
            sender_names=sender_names,
            summarized_threads=summarized,
        )


def iter_all_chat_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    yield from iter_dm_chunks(since)
    yield from iter_gm_chunks(since)
    yield from iter_mdm_chunks(since)
    yield from iter_pm_chunks(since)


# ---------- helpers ----------


def _load_summarized_threads() -> set[tuple[str, int, int]]:
    """Return {(chat_label, chat_id, thread_id)} that already have a
    `ThreadSummary` row. The chat_chunker uses this to skip emitting
    a `chat_thread_window` chunk for any thread whose abstract is
    already in the index (entity_type="thread_summary") — the abstract
    is strictly better than the raw concatenation for vector recall.
    """
    out: set[tuple[str, int, int]] = set()
    for row in ThreadSummary.objects.all().values("chat_type", "chat_id", "thread_id"):
        label = CHAT_TYPE_LABEL.get(row["chat_type"])
        if not label:
            continue
        out.add((label, row["chat_id"], row["thread_id"]))
    return out


def _load_sender_names(sender_ids: set) -> dict[str, str]:
    """Batch-resolve sender_id → display name. Used by the chat-message
    chunker to denormalize `author_name` onto each focal-message chunk
    so source chips can render the sender without a DB round-trip at
    query time.

    Names fall back to "" for users that no longer exist (deleted
    accounts) — search results will still work, just without a friendly
    name in the chip.
    """
    out: dict[str, str] = {}
    if not sender_ids:
        return out
    clean = [s for s in sender_ids if s]
    if not clean:
        return out
    for u in CustomUser.objects.filter(id__in=clean).values("id", "username"):
        out[str(u["id"])] = u["username"] or ""
    return out


def _group_threads(thread_msgs_by_key, chat_id):
    return {
        thread_id: msgs for (cid, thread_id), msgs in thread_msgs_by_key.items() if cid == chat_id
    }


def _emit_chat_chunks(
    *,
    chat_label: str,
    chat_id,
    team_id: str,
    acl_user_ids: list[str],
    chat_title: str,
    messages: list,
    thread_msgs_by_thread_id: dict,
    message_body_attr: str,
    thread_body_attr: str,
    project_id: Optional[str] = None,
    sender_names: Optional[dict[str, str]] = None,
    summarized_threads: Optional[set[tuple[str, int, int]]] = None,
) -> Iterator[EntityChunks]:
    """Bucket messages by thread and produce one EntityChunks per
    (chat, thread)."""

    chat_id_str = str(chat_id)
    main_msgs = []
    thread_anchor_by_thread_id = {}  # thread_id -> the main-channel message that anchors a thread

    for m in messages:
        if m.thread_id is None:
            main_msgs.append(m)
        else:
            thread_anchor_by_thread_id[m.thread_id] = m

    sender_names = sender_names or {}
    summarized_threads = summarized_threads or set()

    # 1) Main-channel entity (non-thread messages).
    main_entity_id = chat_entity_id(chat_label, chat_id_str)
    # PM main "messages" are really task panels in the UI; scroll
    # targeting keys off task_id, not the per-project message_id. See
    # `_build_message_chunks` for the fallback path.
    use_task_id_as_msg = chat_label == "pm"
    main_chunks = _build_message_chunks(
        chat_label=chat_label,
        chat_id=chat_id_str,
        thread_id=None,
        team_id=team_id,
        acl_user_ids=acl_user_ids,
        chat_title=chat_title,
        entity_id=main_entity_id,
        messages=main_msgs,
        body_attr=message_body_attr,
        project_id=project_id,
        use_task_id_as_msg=use_task_id_as_msg,
        sender_names=sender_names,
    )
    if main_chunks:
        yield EntityChunks(entity_type="chat", entity_id=main_entity_id, chunks=main_chunks)

    # 2) One entity per thread.
    thread_ids = set(thread_msgs_by_thread_id.keys()) | set(thread_anchor_by_thread_id.keys())
    for thread_id in thread_ids:
        thread_msgs = thread_msgs_by_thread_id.get(thread_id, [])
        anchor = thread_anchor_by_thread_id.get(thread_id)
        thread_entity_id = chat_entity_id(chat_label, chat_id_str, thread_id)
        skip_window = (chat_label, int(chat_id_str), int(thread_id)) in summarized_threads
        chunks = _build_thread_chunks(
            chat_label=chat_label,
            chat_id=chat_id_str,
            thread_id=str(thread_id),
            team_id=team_id,
            acl_user_ids=acl_user_ids,
            chat_title=chat_title,
            entity_id=thread_entity_id,
            anchor_msg=anchor,
            thread_msgs=thread_msgs,
            anchor_body_attr=message_body_attr,
            thread_body_attr=thread_body_attr,
            project_id=project_id,
            sender_names=sender_names,
            skip_window=skip_window,
        )
        if chunks:
            yield EntityChunks(entity_type="chat", entity_id=thread_entity_id, chunks=chunks)


def _context_window_size() -> int:
    """How many preceding messages to fold into each chat_message chunk."""
    try:
        return max(0, int(settings.SEARCH_ENGINE.get("RAG_CHAT_CONTEXT_WINDOW", 2)))
    except (ValueError, TypeError):
        return 2


def _search_text_with_context(focal_text: str, prior_texts: list[str]) -> str:
    """Build the `search_text` for a chat_message chunk.

    With context disabled (`RAG_CHAT_CONTEXT_WINDOW=0`) or no prior
    messages available (first message in a channel/thread), returns
    the focal text unchanged — embeddings of older chunks already in
    the index stay byte-identical, which keeps the hash-diff in
    `ingestion.py` from re-embedding them on the post-Phase-9 reindex.
    """
    if not prior_texts:
        return focal_text
    prior = "\n".join(prior_texts)
    return f"Previously:\n{prior}\n\nMessage:\n{focal_text}"


def _build_message_chunks(
    *,
    chat_label,
    chat_id,
    thread_id,
    team_id,
    acl_user_ids,
    chat_title,
    entity_id,
    messages,
    body_attr,
    project_id,
    # When True, the trailing `:msg:<id>` portion of each chunk_id is
    # the linked task's id rather than the underlying `m.message_id`.
    # Set for PM main-channel because the PM chat surface keys messages
    # by task_id for scroll targeting (the per-project sequential
    # message_id isn't what the UI exposes). Falls back to message_id
    # for any PM main message that happens to lack a task FK.
    use_task_id_as_msg: bool = False,
    sender_names: Optional[dict[str, str]] = None,
) -> list[Chunk]:
    out = []
    context_size = _context_window_size()
    recent_texts: list[str] = []
    sender_names = sender_names or {}
    for m in messages:
        text = extract_text(getattr(m, body_attr, None))
        if not text:
            continue
        related = []
        if getattr(m, "task_id", None):
            related.append(f"task:{m.task_id}")
        msg_key = (getattr(m, "task_id", None) if use_task_id_as_msg else None) or m.message_id
        chunk_id = f"chat:{chat_label}:{chat_id}:msg:{msg_key}"
        prior = recent_texts[-context_size:] if context_size else []
        sender_id_str = str(m.sender_id) if getattr(m, "sender_id", None) else None
        out.append(
            Chunk(
                chunk_id=chunk_id,
                entity_type="chat",
                entity_id=entity_id,
                chunk_type="chat_message",
                team_id=team_id,
                acl_user_ids=acl_user_ids,
                title=chat_title,
                search_text=_search_text_with_context(text, prior),
                snippet_text=make_snippet(text),
                related_entity_ids=related,
                chat_type=chat_label,
                chat_id=chat_id,
                thread_id=thread_id,
                project_id=project_id,
                # v2 — author identity + per-message PK for deep-link
                # citation chips.
                author_id=sender_id_str,
                author_name=sender_names.get(sender_id_str) if sender_id_str else None,
                chat_message_id=str(m.message_id),
                created_at=iso(getattr(m, "ts_sent_at", None)),
                updated_at=iso(getattr(m, "ts_updated_at", None)),
            )
        )
        recent_texts.append(text)
    return out


def _build_thread_chunks(
    *,
    chat_label,
    chat_id,
    thread_id,
    team_id,
    acl_user_ids,
    chat_title,
    entity_id,
    anchor_msg,
    thread_msgs,
    anchor_body_attr,
    thread_body_attr,
    project_id,
    sender_names: Optional[dict[str, str]] = None,
    # v2 — when True, skip emitting the `chat_thread_window` chunk
    # because this thread already has an LLM-curated `ThreadSummary`
    # row indexed under `entity_type="thread_summary"`. The abstract is
    # strictly better than raw concatenation for vector recall.
    skip_window: bool = False,
) -> list[Chunk]:
    out = []
    window_parts = []
    related = set()
    latest_ts = None
    context_size = _context_window_size()
    recent_texts: list[str] = []
    sender_names = sender_names or {}

    if anchor_msg is not None:
        text = extract_text(getattr(anchor_msg, anchor_body_attr, None))
        if text:
            window_parts.append(text)
            anchor_sender_str = (
                str(anchor_msg.sender_id) if getattr(anchor_msg, "sender_id", None) else None
            )
            # Anchor has no preceding messages in the thread by definition.
            out.append(
                Chunk(
                    chunk_id=(
                        f"chat:{chat_label}:{chat_id}:thread:{thread_id}:"
                        f"anchor:{anchor_msg.message_id}"
                    ),
                    entity_type="chat",
                    entity_id=entity_id,
                    chunk_type="chat_message",
                    team_id=team_id,
                    acl_user_ids=acl_user_ids,
                    title=chat_title,
                    search_text=text,
                    snippet_text=make_snippet(text),
                    related_entity_ids=(
                        [f"task:{anchor_msg.task_id}"] if anchor_msg.task_id else []
                    ),
                    chat_type=chat_label,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    author_id=anchor_sender_str,
                    author_name=(
                        sender_names.get(anchor_sender_str) if anchor_sender_str else None
                    ),
                    chat_message_id=str(anchor_msg.message_id),
                    created_at=iso(getattr(anchor_msg, "ts_sent_at", None)),
                    updated_at=iso(getattr(anchor_msg, "ts_updated_at", None)),
                )
            )
            if anchor_msg.task_id:
                related.add(f"task:{anchor_msg.task_id}")
            latest_ts = getattr(anchor_msg, "ts_updated_at", None)
            recent_texts.append(text)

    for tm in thread_msgs:
        text = extract_text(getattr(tm, thread_body_attr, None))
        if not text:
            continue
        window_parts.append(text)
        prior = recent_texts[-context_size:] if context_size else []
        tm_sender_str = str(tm.sender_id) if getattr(tm, "sender_id", None) else None
        out.append(
            Chunk(
                chunk_id=(
                    f"chat:{chat_label}:{chat_id}:thread:{thread_id}:"
                    f"msg:{tm.thread_message_id}"
                ),
                entity_type="chat",
                entity_id=entity_id,
                chunk_type="chat_message",
                team_id=team_id,
                acl_user_ids=acl_user_ids,
                title=chat_title,
                search_text=_search_text_with_context(text, prior),
                snippet_text=make_snippet(text),
                related_entity_ids=[],
                chat_type=chat_label,
                chat_id=chat_id,
                thread_id=thread_id,
                project_id=project_id,
                author_id=tm_sender_str,
                author_name=sender_names.get(tm_sender_str) if tm_sender_str else None,
                chat_message_id=str(tm.thread_message_id),
                created_at=iso(getattr(tm, "ts_sent_at", None)),
                updated_at=iso(getattr(tm, "ts_updated_at", None)),
            )
        )
        recent_texts.append(text)
        tm_updated = getattr(tm, "ts_updated_at", None)
        if tm_updated and (latest_ts is None or tm_updated > latest_ts):
            latest_ts = tm_updated

    # Thread-window chunk: concatenated text for semantic search.
    # v2 — suppressed for threads that already have a `ThreadSummary`
    # row; the abstract supersedes the raw concatenation.
    if window_parts and not skip_window:
        window_text = "\n".join(window_parts)
        out.append(
            Chunk(
                chunk_id=f"chat:{chat_label}:{chat_id}:thread:{thread_id}:window",
                entity_type="chat",
                entity_id=entity_id,
                chunk_type="chat_thread_window",
                team_id=team_id,
                acl_user_ids=acl_user_ids,
                title=chat_title,
                search_text=window_text,
                snippet_text=make_snippet(window_text),
                related_entity_ids=sorted(related),
                chat_type=chat_label,
                chat_id=chat_id,
                thread_id=thread_id,
                project_id=project_id,
                created_at=iso(getattr(anchor_msg, "ts_sent_at", None)) if anchor_msg else None,
                updated_at=iso(latest_ts),
            )
        )
    return out
