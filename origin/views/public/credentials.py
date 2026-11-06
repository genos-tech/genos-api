"""What a caller authenticates MCP with (MIP-1078).

Until now every public-API and MCP code path assumed the credential was an `ApiKey`:
the permission, the throttle, the team, the scope, the "via" written on a task change,
the security events. OAuth (MIP-1075) adds a second kind of credential for the same
endpoint, so this is the one interface they have to satisfy, and everything that used
to ask `isinstance(auth, ApiKey)` asks it instead. A credential the endpoint has never
heard of can then be added by implementing these members, with nothing else to find.

**REST stays API-key only.** OAuth tokens are for MCP: `HasApiKey` still guards the REST
views, so a token cannot reach them. MCP guards itself with `HasCredential`.

**One team rule, written once** (`team_for`): a team key is pinned to its team; any other
credential works in the team the request names, or its default team, and only inside the
teams it was granted if it has such a list; and the person has to still be IN the team, every
call, because a credential outlives the membership that justified it. REST used to carry
its own copy of this rule and MCP the other half of it; both call this now.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol, runtime_checkable

from origin.models.common.team_models import TeamMaster
from origin.views.utils.scope_guards import is_team_member

KIND_API_KEY = "api_key"


@runtime_checkable
class Credential(Protocol):
    """What the public API and MCP need to know about whoever is calling."""

    # "api_key", or the kind of credential that was added later.
    kind: str
    id: uuid.UUID
    # The person it acts as: it inherits their access and no more.
    user: Any
    # "read" or "write": what it may do at all.
    scope: str
    # A destroying tool also needs this opt-in on top of write scope (MIP-723).
    allow_destructive: bool

    @property
    def pinned_team_id(self) -> str | None:
        """A team key is bound to one team and can name no other."""

    @property
    def allowed_team_ids(self) -> frozenset[str] | None:
        """The teams it was granted, or None when it is not limited to a list (it
        reaches whichever of the person's teams the request names)."""

    @property
    def default_team_id(self) -> str | None:
        """The team used when a request names none, or None when it must always name one."""

    @property
    def label(self) -> str:
        """A name for a person reading an audit line: never the secret."""

    def throttle_ident(self) -> str:
        """The bucket its rate limit counts in. One credential, one bucket."""

    def audit_ref(self) -> str:
        """How a refusal is counted against it (`key:<id>` for an API key)."""

    def via(self) -> dict[str, Any]:
        """How its writes are recorded in a task's history."""


def credential_of(request) -> Credential | None:
    """The credential on this request, or None (an anonymous or session request)."""
    auth = getattr(request, "auth", None)
    return auth if isinstance(auth, Credential) else None


def _str_id(value) -> str | None:
    return str(value) if value else None


def team_for(credential: Credential, user_id, named_team_id=None) -> TeamMaster | None:
    """The team this credential operates in for this request, or None.

    `named_team_id` is what the request asked for (`?team_id=`), already a UUID string or
    None. Every refusal reads the same to the caller, a team they may not use.
    """
    pinned = credential.pinned_team_id
    if pinned:
        # A team key names its team, and the name is not negotiable: the key's owner still
        # has to be in it.
        team_id = str(pinned)
    else:
        team_id = _str_id(named_team_id) or _str_id(credential.default_team_id)
        if not team_id:
            return None
        allowed = credential.allowed_team_ids
        if allowed is not None and team_id not in allowed:
            return None
    if not is_team_member(team_id, user_id):
        return None
    return TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()


def reaches_team(credential: Credential, user_id, team_id) -> bool:
    """May this credential touch an object that lives in `team_id`?

    For routes that name an object rather than a team. A pinned team key reaches only its
    own team, and only while `team_for` still admits it; a credential limited to a list
    reaches only the teams on it; the caller's own access check narrows the rest.
    """
    pinned = credential.pinned_team_id
    if pinned:
        if team_id is None or str(team_id) != str(pinned):
            return False
        return team_for(credential, user_id) is not None
    allowed = credential.allowed_team_ids
    if allowed is not None:
        return team_id is not None and str(team_id) in allowed
    return True
