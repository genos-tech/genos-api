"""Forgetting a thread's cached summary when the thread or its chat goes (MIP-1802).

`ThreadSummary` is the LLM digest of a thread's replies, shared by every member. It has no foreign
key (`chat_id` and `thread_id` are bare UUID columns), so no delete cascaded to it: a deleted
message, a deleted chat (soft or hard) and a torn-down team all left the digest text behind, and
the search chunk made from it.

The rule, in one place:

* a message of a thread is deleted (the root or a reply): the thread's summary goes. The digest is
  made of the replies, so a deleted reply's text lives in it; the root's delete ends the thread.
  The next "ask about this thread" writes a fresh one from what is left (the stored fingerprint
  already marked the old one stale, so this costs nothing the ask would not have cost);
* a chat is deleted, softly or for good: every summary of the chat goes.

The row goes inside the caller's transaction (these helpers open none); the search chunk goes after
the commit, best-effort, so a rolled-back delete never touches the index.
"""

from __future__ import annotations

from django.db import transaction

from origin.models.chat.unified_models import ChannelKind

_KINDS = [int(kind) for kind in ChannelKind]


def _delete(qs) -> list[tuple[int, object, object]]:
    from origin.search_engine.purge import purge_thread_summaries

    rows = list(qs.values_list("pk", "chat_type", "chat_id", "thread_id"))
    if not rows:
        return []
    qs.model.objects.filter(pk__in=[pk for pk, *_ in rows]).delete()
    triples = [(kind, chat, thread) for _pk, kind, chat, thread in rows]
    transaction.on_commit(lambda: purge_thread_summaries(triples), robust=True)
    return triples


def forget_thread(*, channel_id, thread_id) -> int:
    """Drop the summary of one thread. Returns how many rows went (0 or 1).

    The common case (the thread was never summarised) is one indexed read: `chat_type` is listed so
    the unique index `(chat_type, chat_id, thread_id)` is used without the caller dereferencing the
    channel for its kind.
    """
    from origin.search_engine.models import ThreadSummary

    return len(
        _delete(
            ThreadSummary.objects.filter(
                chat_type__in=_KINDS, chat_id=channel_id, thread_id=thread_id
            )
        )
    )


def forget_message_thread(message) -> int:
    """Drop the summary of the thread `message` is in: its root, or its own thread when it is one."""
    return forget_thread(
        channel_id=message.channel_id, thread_id=message.thread_root_id or message.id
    )


def forget_channels(channel_ids) -> int:
    """Drop every summary of these chats. Returns how many rows went."""
    from origin.search_engine.models import ThreadSummary

    ids = list(channel_ids)
    if not ids:
        return 0
    return len(_delete(ThreadSummary.objects.filter(chat_type__in=_KINDS, chat_id__in=ids)))
