"""A team's security audit log, for its owner (MIP-725).

    GET /api/v2/team/security-events/?team_id=<uuid>[&before=<id>][&limit=<n>][&action=<a>]

Newest first, `limit` rows a page (50 by default, 200 at most). Pass the
response's `nextBefore` as `before` for the next page; it is null on the
last one.

Owner only, with the same 404 for "no such team", "not a member" and
"not the owner" as the team API-key list (`_owned_team`). The trail says
who did what to the team, including its editors, so it belongs to the
one person above them. Session (JWT) only, like key management: a leaked
key must not be able to read the trail that would show it being used.

Only team-scoped rows are listed. A person's own sign-ins, exports and
connected accounts carry no team, and are theirs, not the owner's.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.views.common.api_key_views import _owned_team, _team_not_found
from origin.views.common.base_auth_api_view import AuthenticatedAPIView

_DEFAULT_LIMIT = 50
_MAX_LIMIT = 200


def _serialize(event: SecurityEvent) -> dict:
    return {
        "id": event.event_id,
        "createdAt": event.ts_created_at,
        "action": event.action,
        "actionLabel": event.get_action_display(),
        "actorId": str(event.actor_id) if event.actor_id else None,
        "actorLabel": event.actor_label,
        "targetType": event.target_type,
        "targetId": event.target_id,
        "targetLabel": event.target_label,
        "source": event.source,
        "apiKeyId": str(event.api_key_id) if event.api_key_id else None,
        "ip": event.ip,
        "userAgent": event.user_agent,
        "metadata": event.metadata,
    }


def _positive_int(raw, default=None):
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


class TeamSecurityEventListView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        team = _owned_team(request)
        if team is None:
            return _team_not_found()

        limit = min(_positive_int(request.query_params.get("limit"), _DEFAULT_LIMIT), _MAX_LIMIT)
        events = SecurityEvent.objects.filter(team_id=team.team_id)

        before = request.query_params.get("before")
        if before is not None:
            before = _positive_int(before)
            if before is None:
                return Response(
                    {"error": "before must be a positive integer."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            events = events.filter(event_id__lt=before)

        action = request.query_params.get("action")
        if action:
            if action not in SecurityAction.values:
                return Response(
                    {"error": f"action must be one of {list(SecurityAction.values)}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            events = events.filter(action=action)

        # One extra row tells us whether there is another page without a
        # COUNT over the whole trail.
        page = list(events.order_by("-event_id")[: limit + 1])
        more = len(page) > limit
        page = page[:limit]
        return Response(
            {
                "events": [_serialize(e) for e in page],
                "nextBefore": page[-1].event_id if more else None,
            },
            status=status.HTTP_200_OK,
        )
