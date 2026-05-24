"""Resolve viewer-friendly chat titles for search/source rows.

The chunker writes a viewer-agnostic placeholder into the chat `title`
field at index time (e.g. "DM 9", because a DM has no shared name — the
"name" is just the other participant, which depends on who's looking).
This module turns those placeholders into something a person can read:

  * DM  → the partner's username
  * GM  → the group's `group_name`
  * MDM → the group's `display_name`
  * PM  → the project's `project_name`

Used by:
  * `search.py` — after `_group_by_entity`, so typeahead responses
    show friendly names in result rows.
  * `agent/controller.py` — after each source-emitting tool call, so
    spotlight citation chips show friendly names.

Both call sites pass the requesting user's id (the viewer). DM
resolution is the only one that's viewer-dependent — the others
return the same name regardless — but threading user_id through the
single API keeps the call sites symmetric.
"""

from __future__ import annotations

from typing import Any


def friendly_chat_title(
    viewer_user_id: str,
    chat_type_label: Any,
    chat_id: Any,
) -> str | None:
    """Resolve a viewer-facing chat title; returns None on lookup failure.

    Best-effort: callers should treat None as "keep whatever title was
    already on the row" rather than blanking the field. A missing
    partner / soft-deleted chat / unknown chat-type label all return
    None silently.
    """
    if not chat_type_label or not chat_id:
        return None
    try:
        cid = int(chat_id)
    except (TypeError, ValueError):
        return None

    label = str(chat_type_label).lower()

    # Lazy imports — keep this module dependency-free at import time
    # and avoid circular-import surprises during Django startup.
    if label == "dm":
        from origin.models.chat.dm_models import DMMaster
        from origin.models.common.user_models import CustomUser

        try:
            dm = DMMaster.objects.get(dm_id=cid)
        except DMMaster.DoesNotExist:
            return None
        partner_id = dm.user_2_id if str(dm.user_1_id) == viewer_user_id else dm.user_1_id
        if not partner_id:
            return None
        try:
            user = CustomUser.objects.get(id=partner_id)
        except CustomUser.DoesNotExist:
            return None
        return user.username or None

    if label == "gm":
        from origin.models.chat.gm_models import GMMaster

        try:
            return GMMaster.objects.get(gm_id=cid).group_name or None
        except GMMaster.DoesNotExist:
            return None

    if label == "mdm":
        from origin.models.chat.mdm_models import MDMMaster

        try:
            return MDMMaster.objects.get(mdm_id=cid).display_name or None
        except MDMMaster.DoesNotExist:
            return None

    if label == "pm":
        from origin.models.project.prj_models import ProjectMaster

        try:
            # For PM chats, chat_id == project_id (see fetch_chat_thread).
            return ProjectMaster.objects.get(project_id=cid).project_name or None
        except ProjectMaster.DoesNotExist:
            return None

    return None


def apply_friendly_titles(rows: list[dict[str, Any]], viewer_user_id: str) -> list[dict[str, Any]]:
    """Replace placeholder chat titles ('DM 9') with viewer-friendly names.

    Mutates and returns the same list. Only rows with `entity_type ==
    "chat"` are touched. Lookup failures leave the row's existing title
    in place.
    """
    for row in rows:
        if row.get("entity_type") != "chat":
            continue
        title = friendly_chat_title(viewer_user_id, row.get("chat_type"), row.get("chat_id"))
        if title:
            row["title"] = title
    return rows
