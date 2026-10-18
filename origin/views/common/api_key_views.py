"""Managing API keys — your own, and your team's.

    GET    /api/v2/api-keys/                         list (never the key itself)
    POST   /api/v2/api-keys/                         create — the ONLY time the key is shown
    DELETE /api/v2/api-keys/<id>/                    revoke
    GET    /api/v2/team/api-keys/?team_id=<uuid>     every live team key (owner only)
    DELETE /api/v2/team/api-keys/<id>/?team_id=<uuid>  revoke a member's team key (owner only)

Authenticated with a JWT, deliberately not with an API key: a leaked key
must not be able to mint further keys or revoke the ones that would let
you notice. Key management is a session-only surface. (Since MIP-718 a
key authenticates nowhere outside `/api/public/v1/` anyway; the explicit
`authentication_classes` here stay so that remains true if the default
ever widens again.)
"""

import uuid

from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.api_key_models import (
    DISPLAY_PREFIX_LEN,
    SCOPE_CHOICES,
    SCOPE_READ,
    SCOPE_WRITE,
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMaster
from origin.services.member_roles import can_manage, resolve_team_role
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_team_member

# Every key expires (MIP-718). A key used to default to never expiring,
# with an optional cap of ten years, so the keys that leak — the ones
# pasted into a CI config and forgotten — were the ones that never
# stopped working. An integrator who still needs one after a year mints
# a replacement, which is also the moment they find out who holds it.
_DEFAULT_EXPIRY_DAYS = 90
_MAX_EXPIRY_DAYS = 365


def _serialize(key: ApiKey) -> dict:
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "scope": key.scope,
        "allowDestructive": key.allow_destructive,
        "teamId": str(key.team_id) if key.team_id else None,
        "lastUsedAt": key.last_used_at,
        "expiresAt": key.expires_at,
        "revokedAt": key.revoked_at,
        "createdAt": key.ts_created_at,
    }


class ApiKeyListCreateView(AuthenticatedAPIView):
    # JWT only — see the module docstring.
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        keys = ApiKey.objects.filter(user=request.user, revoked_at__isnull=True)
        return Response({"api_keys": [_serialize(k) for k in keys]}, status=status.HTTP_200_OK)

    def post(self, request):
        # Type-check before `.strip()`: `{"name": 123}` raised
        # `AttributeError: 'int' object has no attribute 'strip'` — a 500
        # from a settings form, and the request body is JSON so the type
        # is the caller's to choose.
        raw_name = request.data.get("name")
        if raw_name is not None and not isinstance(raw_name, str):
            return Response({"error": "name must be a string."}, status=status.HTTP_400_BAD_REQUEST)
        name = (raw_name or "").strip()
        if not name:
            return Response({"error": "name is required."}, status=status.HTTP_400_BAD_REQUEST)

        scope = request.data.get("scope") or SCOPE_READ
        if scope not in dict(SCOPE_CHOICES):
            return Response(
                {"error": f"scope must be one of {sorted(dict(SCOPE_CHOICES))}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # Strictly a JSON boolean: `"false"` is truthy, and a string that
        # reads as no while granting deletes is the one mistake this flag
        # can't afford. Meaningless without write scope, so refused there
        # rather than stored as a grant that would wake up on its own if
        # scope ever became editable. Omitted or `null` is the default, off,
        # as with `expires_in_days` below.
        allow_destructive = request.data.get("allow_destructive")
        if allow_destructive is None:
            allow_destructive = False
        if not isinstance(allow_destructive, bool):
            return Response(
                {"error": "allow_destructive must be true or false."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if allow_destructive and scope != SCOPE_WRITE:
            return Response(
                {"error": "allow_destructive needs a write-scoped key."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        team = None
        team_id = request.data.get("team_id")
        if team_id:
            # Parsed before the query: `team_id` is a UUIDField, so a
            # malformed value raised out of the ORM instead of answering
            # "no such team". Same 404 either way.
            try:
                team_id = str(uuid.UUID(str(team_id)))
            except (ValueError, AttributeError, TypeError):
                return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
            team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
            if team is None or not is_team_member(team_id, request.user.id):
                return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
            # A team-scoped key acts on the team's behalf and outlives
            # the creator's involvement, so minting one is management —
            # the same gate as inviting a member.
            if not can_manage(resolve_team_role(team, request.user.id)):
                return Response(
                    {"error": "Only the team owner or an editor can create a team API key."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        # Omitted (or `null`) means the default, never "no expiry" — that
        # option is gone. `bool` is refused before `int()`, which would
        # otherwise read `true` as a one-day key.
        raw_days = request.data.get("expires_in_days")
        days = _DEFAULT_EXPIRY_DAYS
        if raw_days is not None:
            try:
                if isinstance(raw_days, bool):
                    raise TypeError
                days = int(raw_days)
            except (TypeError, ValueError):
                return Response(
                    {"error": "expires_in_days must be an integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if days < 1 or days > _MAX_EXPIRY_DAYS:
                return Response(
                    {"error": f"expires_in_days must be between 1 and {_MAX_EXPIRY_DAYS}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        expires_at = timezone.now() + timezone.timedelta(days=days)

        raw = generate_key()
        key = ApiKey.objects.create(
            user=request.user,
            team=team,
            name=name[:100],
            key_hash=hash_key(raw),
            prefix=raw[:DISPLAY_PREFIX_LEN],
            scope=scope,
            allow_destructive=allow_destructive,
            expires_at=expires_at,
            created_by=request.user,
        )
        payload = _serialize(key)
        # The one and only time the plaintext exists in a response. The
        # client must tell the user so before they close the dialog —
        # there is no recovery path, by design.
        payload["key"] = raw
        return Response(payload, status=status.HTTP_201_CREATED)


class ApiKeyDetailView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def delete(self, request, key_id):
        # Scoped to the caller's own keys: 404 rather than 403 so the id
        # space isn't a probe for which keys exist.
        key = ApiKey.objects.filter(id=key_id, user=request.user).first()
        if key is None:
            return Response({"error": "API key not found."}, status=status.HTTP_404_NOT_FOUND)
        key.revoke()
        return Response({"id": str(key.id), "revokedAt": key.revoked_at}, status=status.HTTP_200_OK)


def _owned_team(request):
    """The team named by `?team_id=`, iff the caller OWNS it; else `None`.

    Owner, not owner-or-editor, although editors may mint team keys: an
    editor revoking another editor's integration is a dispute between
    peers, and this is the place it would happen with no one above them
    to appeal to. The owner is the one person accountable for what acts
    on the team's behalf, so the oversight is theirs. One `None` for "no
    such team", "not a member" and "not the owner" — a member asking about
    a team's keys learns nothing either way.
    """
    raw = request.query_params.get("team_id")
    try:
        team_id = str(uuid.UUID(str(raw)))
    except (ValueError, AttributeError, TypeError):
        return None
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None or team.owner_id is None or str(team.owner_id) != str(request.user.id):
        return None
    return team


def _team_not_found():
    return Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)


class TeamApiKeyListView(AuthenticatedAPIView):
    """Every live key bound to the team, whoever minted it.

    The owner otherwise had no way to see them: `ApiKeyListCreateView`
    lists the caller's own keys only, so a team key an editor minted —
    which acts on the team for up to a year — was invisible to the person
    answerable for it. Expired keys are left out alongside revoked ones:
    they authenticate nothing, and listing them would bury the ones that
    do.
    """

    authentication_classes = [JWTAuthentication]

    def get(self, request):
        team = _owned_team(request)
        if team is None:
            return _team_not_found()
        keys = (
            ApiKey.objects.filter(team=team, revoked_at__isnull=True)
            .exclude(expires_at__lte=timezone.now())
            .select_related("user")
        )
        return Response(
            {
                "api_keys": [
                    {
                        **_serialize(k),
                        "userId": str(k.user_id),
                        "userName": k.user.username,
                        "userEmail": k.user.email,
                    }
                    for k in keys
                ]
            },
            status=status.HTTP_200_OK,
        )


class TeamApiKeyDetailView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def delete(self, request, key_id):
        team = _owned_team(request)
        if team is None:
            return _team_not_found()
        # Bound to THIS team. A personal token, or another team's key, is
        # not the owner's to revoke — same 404 as a key that doesn't exist.
        key = ApiKey.objects.filter(id=key_id, team=team).first()
        if key is None:
            return Response({"error": "API key not found."}, status=status.HTTP_404_NOT_FOUND)
        key.revoke()
        return Response({"id": str(key.id), "revokedAt": key.revoked_at}, status=status.HTTP_200_OK)
