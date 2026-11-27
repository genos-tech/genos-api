"""The web app's address of an item, for a reader OUTSIDE the app (an AI app over MCP), who cannot
follow the `task:` / `chat:` citation tokens the app itself turns into links (MIP-2485).

The paths are the web client's own routes (`genos-frontend/src/utils/parseInternalUrl.ts`).
"""

from __future__ import annotations

from django.conf import settings


def _url(path: str) -> str:
    return f"{str(settings.FRONTEND_BASE_URL).rstrip('/')}{path}"


def chat_url(chat_type: str, chat_id: str, thread_id: str | None = None) -> str:
    thread = f"/thread/{thread_id}" if thread_id else ""
    return _url(f"/workspace/chat/{chat_type}/{chat_id}{thread}")


def message_url(item_url: str, seq: int) -> str:
    """One message in a chat or thread. By its per-chat number, the form "Copy link to message"
    makes: the client's router reads the segment as a number."""
    return f"{item_url}/message/{seq}"


def task_url(project_id: int, task_id: int, comment_id: int | None = None) -> str:
    comment = f"/comment/{comment_id}" if comment_id else ""
    return _url(f"/workspace/tasks/project/{project_id}/task/{task_id}{comment}")
