"""Managing your own API keys.

    GET    /api/v2/api-keys/            list (never the key itself)
    POST   /api/v2/api-keys/            create — the ONLY time the key is shown
    DELETE /api/v2/api-keys/<id>/       revoke

Authenticated with a JWT, deliberately not with an API key: a leaked key
must not be able to mint further keys or revoke the ones that would let
you notice. Key management is a session-only surface.
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
    ApiKey,
    generate_key,
    hash_key,
)
from origin.models.common.team_models import TeamMaster
from origin.services.member_roles import can_manage, resolve_team_role
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_team_member

# A key that never expires is the common case for a bot, so `null` stays
# legal — but an unbounded default would make "forever" the accident
# rather than the choice.
_MAX_EXPIRY_DAYS = 3650


def _serialize(key: ApiKey) -> dict:
    return {
        "id": str(key.id),
        "name": key.name,
        "prefix": key.prefix,
        "scope": key.scope,
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

        expires_at = None
        raw_days = request.data.get("expires_in_days")
        if raw_days is not None:
            try:
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
