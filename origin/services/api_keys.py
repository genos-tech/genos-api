"""Ending an API key, and leaving the trail (MIP-1078: one credential layer).

The one place a key is revoked with its security event: the member's own revoke, a team
owner's or editor's, and a password reset all come through `revoke_api_key`, so the log
reads the same whichever way a key ended.
"""

from __future__ import annotations

from origin.models.common.api_key_models import ApiKey
from origin.models.common.security_event_models import SecurityAction
from origin.services.security_events import record_security_event


def key_label(key: ApiKey) -> str:
    return f"{key.name} ({key.prefix})"


def revoke_api_key(key: ApiKey, *, by: str, request=None, actor=None) -> bool:
    """Revoke and record it; True if this call ended the key.

    Revoking an already-revoked key changes nothing, so it records nothing. `by` is who
    ended it (`self`, `team_owner`, `team_editor`, `password_reset`). `actor` is for a
    caller whose request is not authenticated as the person (a reset)."""
    if key.revoked_at is not None:
        return False
    key.revoke()
    record_security_event(
        SecurityAction.API_KEY_REVOKED,
        request=request,
        actor=actor,
        team_id=key.team_id,
        target_type="api_key",
        target_id=key.id,
        target_label=key_label(key),
        metadata={"by": by, "owner_id": str(key.user_id)},
    )
    return True
