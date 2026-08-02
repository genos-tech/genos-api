"""Managing webhook endpoints.

    GET    /api/v2/webhooks/          list this team's endpoints
    POST   /api/v2/webhooks/          create — the ONLY time the secret is shown
    DELETE /api/v2/webhooks/<id>/     delete

JWT only, and owner/editor only. A webhook is a standing instruction to
send this team's data to an address somebody chose, so creating one is
management, not membership — and a leaked API key must not be able to
add an exfiltration target.
"""

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.team_models import TeamMaster
from origin.models.common.webhook_models import (
    ALL_EVENTS,
    WebhookEndpoint,
    generate_secret,
)
from origin.services.member_roles import can_manage, resolve_team_role
from origin.services.webhook_delivery import WebhookUrlError, validate_webhook_url
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import is_team_member


def _serialize(e: WebhookEndpoint) -> dict:
    return {
        "id": str(e.id),
        "url": e.url,
        "description": e.description,
        "events": e.events,
        "is_active": e.is_active,
        "consecutive_failures": e.consecutive_failures,
        "disabled_at": e.disabled_at,
        "created_at": e.ts_created_at,
    }


def _require_manager(request, team_id):
    """`(team, None)` when allowed, `(None, Response)` otherwise."""
    team = TeamMaster.objects.filter(team_id=team_id, is_deleted=False).first()
    if team is None or not is_team_member(team_id, request.user.id):
        return None, Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage(resolve_team_role(team, request.user.id)):
        return None, Response(
            {"error": "Only the team owner or an editor can manage webhooks."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return team, None


class WebhookListCreateView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def get(self, request):
        team_id = request.GET.get("team_id")
        if not team_id:
            return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        team, err = _require_manager(request, team_id)
        if err:
            return err
        endpoints = WebhookEndpoint.objects.filter(team=team)
        return Response({"webhooks": [_serialize(e) for e in endpoints]}, status=status.HTTP_200_OK)

    def post(self, request):
        team_id = request.data.get("team_id")
        if not team_id:
            return Response({"error": "team_id is required."}, status=status.HTTP_400_BAD_REQUEST)
        team, err = _require_manager(request, team_id)
        if err:
            return err

        try:
            url = validate_webhook_url(request.data.get("url"))
        except WebhookUrlError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        events = request.data.get("events") or []
        if not isinstance(events, list) or not events:
            return Response(
                {"error": f"events must be a non-empty list from {ALL_EVENTS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        unknown = [e for e in events if e not in ALL_EVENTS]
        if unknown:
            return Response(
                {"error": f"Unknown events: {unknown}. Known events: {ALL_EVENTS}."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        raw_secret = generate_secret()
        endpoint = WebhookEndpoint(
            team=team,
            url=url,
            description=(request.data.get("description") or "")[:200],
            events=events,
            created_by=request.user,
        )
        endpoint.set_secret(raw_secret)
        endpoint.save()

        payload = _serialize(endpoint)
        # Shown once. The receiver needs it to verify signatures, and we
        # keep it encrypted rather than hashed precisely so we can sign
        # with it — but there is still no reason to hand it back twice.
        payload["secret"] = raw_secret
        return Response(payload, status=status.HTTP_201_CREATED)


class WebhookDetailView(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]

    def delete(self, request, webhook_id):
        endpoint = WebhookEndpoint.objects.filter(id=webhook_id).first()
        if endpoint is None:
            return Response({"error": "Webhook not found."}, status=status.HTTP_404_NOT_FOUND)
        _, err = _require_manager(request, endpoint.team_id)
        if err:
            # 404 rather than the manager 403 when the caller is not even
            # in the team — don't confirm the id names a real endpoint.
            return err
        endpoint.delete()
        return Response({"id": str(webhook_id)}, status=status.HTTP_200_OK)
