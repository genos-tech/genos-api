"""`manage_note_sharing` write tool (MIP-883): share a note with a teammate, or stop.

Sharing widens who can read, and edit, a note, so on MCP (no approval card) it needs the
key's opt-in like a delete does; in the app the card names the person and the role.
The page's rules, through `origin.services.note_sharing`: only the note's Owner may,
the person must be on the team, the role is `editor` or `viewer` (never a second
owner), and nobody changes their own. The person is not notified.

Stopping a share is the narrowing half: the note's search entries are purged so the
person can no longer find it there.

Approval flow: `requires_approval=True`.
"""

from __future__ import annotations

import uuid
from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services.note_sharing import SharingRefused, share_note, unshare_note
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER

_ACTIONS = ("share", "unshare")
_ROLES = {"editor": ROLE_EDITOR, "viewer": ROLE_VIEWER}


def _person(raw: Any) -> tuple[str, str]:
    """`(user_id, username)` for the person named, or a ToolError."""
    text = str(raw or "").strip()
    try:
        user_id = str(uuid.UUID(text))
    except ValueError:
        raise ToolError("`user_id` is required: a person's id from get_team_members.")
    name = CustomUser.objects.filter(id=user_id).values_list("username", flat=True).first()
    return user_id, name or "that person"


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    action = args.get("action")
    if action not in _ACTIONS:
        raise ToolError(f"`action` must be one of {list(_ACTIONS)} (got {action!r}).")
    ref = load_readable_note(args, ctx)
    title = getattr(ref.note, "title", "") or "Untitled"
    user_id, username = _person(args.get("user_id"))

    if action == "share":
        role = args.get("role")
        if role not in _ROLES:
            raise ToolError(f"`role` must be 'editor' or 'viewer' (got {role!r}).")
    try:
        if action == "share":
            share_note(
                team_id=ctx.team_id,
                granter_id=ctx.user_id,
                note_type=ref.code,
                note_id=ref.note_id,
                target_user_id=user_id,
                role_id=_ROLES[role],
            )
        else:
            unshare_note(
                granter_id=ctx.user_id,
                note_type=ref.code,
                note_id=ref.note_id,
                target_user_id=user_id,
            )
    except SharingRefused as refused:
        raise ToolError(f"{refused.message} Nothing was changed.")
    except Exception as e:  # noqa: BLE001
        failure = log_failure("manage_note_sharing failed", e)
        raise ToolError(f"Failed to change the sharing (ref {failure}).")

    out = {"note_type": ref.family, "note_id": ref.note_id, "user_id": user_id, "action": action}
    if action == "share":
        out["role"] = role
        out["__summary__"] = (
            f'Shared "{title}" with {username} as {"an" if role == "editor" else "a"} {role}. '
            f"{username} is not notified."
        )
    else:
        out["__summary__"] = f'Stopped sharing "{title}" with {username}.'
    return out


MANAGE_NOTE_SHARING = Tool(
    name="manage_note_sharing",
    description=(
        "Share a note with a teammate, or stop sharing it. REQUIRES USER APPROVAL — it changes "
        "who can read, and edit, the note, and the person is not notified. Call "
        "`list_note_access` FIRST for the note's current shares and whether the caller may "
        "change them (only its owner may), and `get_team_members` for the user_id. action "
        "'share' gives the person `role` 'editor' or 'viewer' (never owner), or changes the "
        "one they have; action 'unshare' takes their share away. A note in a Team Notes "
        "folder is also open to whoever that folder is open to."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "enum": list(_ACTIONS),
                "description": "'share' with a person, or 'unshare'.",
            },
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "Which kind of note (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "The note's id."},
            "user_id": {
                "type": "STRING",
                "description": "The person (from get_team_members).",
            },
            "role": {
                "type": "STRING",
                "enum": list(_ROLES),
                "description": "share: 'editor' or 'viewer'.",
            },
        },
        "required": ["action", "note_type", "note_id", "user_id"],
    },
    run=_run,
    requires_approval=True,
)
