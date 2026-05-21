"""App → Google Calendar sync helpers for the opt-in task auto-sync.

Used by:
  - `origin.signals.task_signals` — post_save on TaskMaster fires
    `sync_task_event` (deferred via `transaction.on_commit`).
  - The "Sync existing tasks now" backfill endpoint in
    `origin.views.common.user_views`.

Design notes:
  - All Google API calls are wrapped in try/except. A failure logs and
    returns; it must NEVER raise back into a request transaction.
  - Tasks are saved with `update_fields={"linked_calendar_event_id",
    "linked_calendar_id"}` so the post_save signal can early-return
    on its own writes (recursion guard lives in `task_signals`).
  - Status transitions to "done" prefix the event title with a check
    so the calendar reflects work done without losing history.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Literal, Optional

import requests

from origin.models.common.user_models import ConnectedAccount
from origin.services.oauth.tokens import get_valid_access_token

logger = logging.getLogger(__name__)

CALENDAR_API_BASE = "https://www.googleapis.com/calendar/v3"
LINK_ONLY_FIELDS = ("linked_calendar_event_id", "linked_calendar_id")
DONE_STATUS_PREFIX = "✓ "
# Prefixes synced task titles with this emoji to make calendar
# entries scannable — without it a "Implement login" event looks
# identical to a real meeting. Stripped on Done transitions so the
# done-prefix is the only marker on a completed task.
DUE_TITLE_PREFIX = "📅 "
_KNOWN_TITLE_PREFIXES = (DONE_STATUS_PREFIX, DUE_TITLE_PREFIX)
# Status values that are considered "done" / closed. Kept liberal so
# variations across the codebase (e.g. "Done" vs "Closed") all trigger
# the check-prefix update.
_DONE_STATUS_VALUES = frozenset({"closed", "done", "completed"})


def get_google_connected_account(user) -> Optional[ConnectedAccount]:
    """Look up the user's connected Google account, if any."""
    if user is None:
        return None
    return ConnectedAccount.objects.filter(user=user, provider="google").first()


def _is_done(task) -> bool:
    s = (task.status or "").strip().lower()
    return s in _DONE_STATUS_VALUES


def _event_title(task) -> str:
    base = task.title or ""
    # Strip any stale prefix(es) from a prior sync so transitions
    # (Open ↔ Done) never accumulate stacked markers like
    # "✓ 📅 Foo" or "📅 ✓ Foo". Loops because pre-fix legacy
    # titles may contain both stacked; bails as soon as the
    # remaining base no longer starts with a known prefix.
    while True:
        stripped = False
        for prefix in _KNOWN_TITLE_PREFIXES:
            if base.startswith(prefix):
                base = base[len(prefix) :]
                stripped = True
                break
        if not stripped:
            break
    if _is_done(task):
        return f"{DONE_STATUS_PREFIX}{base}"
    return f"{DUE_TITLE_PREFIX}{base}"


def _all_day_payload(task) -> dict:
    """Build Google's all-day event payload from a task's `due_date`.

    Google all-day uses `start.date` / `end.date` (exclusive end).
    No timezone — date-only events are timezone-agnostic.
    """
    return {
        "summary": _event_title(task),
        "start": {"date": task.due_date.isoformat()},
        "end": {"date": (task.due_date + timedelta(days=1)).isoformat()},
    }


def _google(account: ConnectedAccount, method: str, path: str, **kwargs):
    token = get_valid_access_token(account)
    headers = kwargs.pop("headers", {}) or {}
    headers["Authorization"] = f"Bearer {token}"
    headers.setdefault("Accept", "application/json")
    return requests.request(
        method, f"{CALENDAR_API_BASE}{path}", headers=headers, timeout=15, **kwargs
    )


SyncOutcome = Literal["created", "patched", "cleared", "failed"]


def sync_task_event(account: ConnectedAccount, task) -> SyncOutcome:
    """Create or patch the linked Google event for `task`.

    Returns one of:
      - "created" : new event posted; link columns now set. Caller saves.
      - "patched" : existing event PATCHed in place. No model change.
      - "cleared" : Google returned 404; link columns nulled. Caller saves.
                    Per the "never re-create" decision, no follow-up POST.
      - "failed"  : API error or transport blew up (already logged).

    The "created" vs "patched" distinction matters for the backfill
    count UI; both are "real" syncs. "cleared" looks like a sync to
    the old bool API but isn't one — surfacing it explicitly so the
    backfill endpoint can exclude it.
    """
    if task is None or task.due_date is None:
        return "failed"
    calendar_id = task.linked_calendar_id or "primary"
    body = _all_day_payload(task)

    try:
        if task.linked_calendar_event_id:
            resp = _google(
                account,
                "PATCH",
                f"/calendars/{calendar_id}/events/{task.linked_calendar_event_id}",
                json=body,
            )
            if resp.status_code == 404:
                # Hard-deleted upstream — clear the link and bail.
                # Per the never-re-create decision, no follow-up POST.
                task.linked_calendar_event_id = None
                task.linked_calendar_id = None
                return "cleared"
            if not resp.ok:
                logger.warning(
                    "calendar_sync patch failed task=%s status=%s body=%s",
                    task.pk,
                    resp.status_code,
                    resp.text[:500],
                )
                return "failed"
            # Google soft-deletes events from the UI: PATCH against
            # a cancelled event returns 200 with `status="cancelled"`
            # instead of 404. Without this check we'd count it as a
            # successful "patched" sync while the user sees no event
            # on their calendar — surfaced by a user as misleading.
            # Treat the same as a 404: clear the link, never re-create.
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if payload.get("status") == "cancelled":
                task.linked_calendar_event_id = None
                task.linked_calendar_id = None
                return "cleared"
            return "patched"

        # No link yet — create.
        resp = _google(account, "POST", f"/calendars/{calendar_id}/events", json=body)
        if not resp.ok:
            logger.warning(
                "calendar_sync create failed task=%s status=%s body=%s",
                task.pk,
                resp.status_code,
                resp.text[:500],
            )
            return "failed"
        event = resp.json()
        task.linked_calendar_event_id = event.get("id")
        task.linked_calendar_id = calendar_id
        return "created"
    except requests.RequestException as exc:
        logger.warning("calendar_sync transport error task=%s err=%s", task.pk, exc)
        return "failed"


def delete_task_event(account: ConnectedAccount, task) -> bool:
    """Delete the linked Google event for `task` and clear the link
    columns. Used when a task is soft-deleted or its `due_date` is
    cleared while auto-sync is on. Returns True if the model row was
    modified (the caller saves with update_fields=LINK_ONLY_FIELDS).

    A 404 is treated as success — the upstream is already gone, we
    just need to clear our pointer.
    """
    if not task.linked_calendar_event_id:
        return False
    calendar_id = task.linked_calendar_id or "primary"
    try:
        resp = _google(
            account,
            "DELETE",
            f"/calendars/{calendar_id}/events/{task.linked_calendar_event_id}",
        )
        if resp.status_code not in (200, 204, 404) and not resp.ok:
            logger.warning(
                "calendar_sync delete failed task=%s status=%s body=%s",
                task.pk,
                resp.status_code,
                resp.text[:500],
            )
            # Even on failure, clear the link so the user can re-link
            # later without a stale pointer. Mirrors how a 404 is
            # handled in `sync_task_event`.
        task.linked_calendar_event_id = None
        task.linked_calendar_id = None
        return True
    except requests.RequestException as exc:
        logger.warning("calendar_sync delete transport error task=%s err=%s", task.pk, exc)
        return False
