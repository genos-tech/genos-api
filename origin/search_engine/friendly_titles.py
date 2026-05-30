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
    # and avoid circular-import surprises during Django startup. Chat
    # identity resolves off the v3 unified schema via the
    # `Channel.legacy_chat_id` bridge (the legacy DM/GM/MDM master tables
    # are gone); `cid` is the legacy int the index/search rows carry.
    if label == "dm":
        from origin.models.chat.unified_models import ChannelMember
        from origin.models.common.user_models import CustomUser
        from origin.services.legacy_chat_bridge import resolve_channel

        channel = resolve_channel(1, cid)  # CHAT_TYPE_DM
        if channel is None:
            return None
        member_ids = list(
            ChannelMember.objects.filter(channel=channel, is_deleted=False).values_list(
                "user_id", flat=True
            )
        )
        # The DM partner is the *other* member; self-DM falls back to self.
        partner_id = next((uid for uid in member_ids if str(uid) != viewer_user_id), None)
        if partner_id is None and member_ids:
            partner_id = member_ids[0]
        if not partner_id:
            return None
        try:
            return CustomUser.objects.get(id=partner_id).username or None
        except CustomUser.DoesNotExist:
            return None

    if label == "gm" or label == "mdm":
        from origin.services.legacy_chat_bridge import resolve_channel

        # v3 `Channel.title` holds the group / MDM display name.
        channel = resolve_channel(2 if label == "gm" else 4, cid)
        return (channel.title or None) if channel else None

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
