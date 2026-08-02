"""Managing webhook endpoints.

    GET    /api/v2/webhooks/          list this team's endpoints
    POST   /api/v2/webhooks/          create — the ONLY time the secret is shown
    DELETE /api/v2/webhooks/<id>/     delete

JWT only, and owner/editor only. A webhook is a standing instruction to
send this team's data to an address somebody chose, so creating one is
management, not membership — and a leaked API key must not be able to
add an exfiltration target.
"""

import uuid

from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from origin.models.common.team_models import TeamMaster
from origin.models.common.webhook_models import (
    ALL_EVENTS,
    CHANNEL_SCOPED_EVENTS,
    SUBSCRIBABLE_CHANNEL_KINDS,
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
        "project_ids": e.project_ids,
        "channel_ids": e.channel_ids,
        "is_active": e.is_active,
        "consecutive_failures": e.consecutive_failures,
        "disabled_at": e.disabled_at,
        "created_at": e.ts_created_at,
    }


def _validate_scope(team, events, request_data):
    """`(project_ids, channel_ids, None)` or `(None, None, Response)`.

    Two different rules, because the two axes mean different things:

    **Projects** are an optional narrowing. Empty is legitimate and means
    the whole team, which is what the endpoint already did before scoping
    existed. Ids are checked for team membership so one team cannot
    subscribe to another's project — the same walkable-integer shape the
    ACL audit closed everywhere else.

    **Channels are mandatory for chat events, and DMs are never allowed.**
    A chat payload carries what people wrote, and this endpoint is
    configured by one admin on behalf of everyone who talks there. So
    there is no "all channels" and no way to name a private one-to-one.
    Enforced here rather than trusted from the client because the UI is
    not the only caller.
    """
    from origin.models.chat.unified_models import Channel  # noqa: PLC0415
    from origin.models.project.prj_models import ProjectMaster  # noqa: PLC0415

    project_ids = request_data.get("project_ids") or []
    channel_ids = request_data.get("channel_ids") or []
    if not isinstance(project_ids, list) or not isinstance(channel_ids, list):
        return (
            None,
            None,
            Response(
                {"error": "project_ids and channel_ids must be lists."},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    # Parse FIRST, then compare parsed-to-parsed. Comparing raw input
    # against ids read back from the database mixes types: a JSON client
    # sending `["12"]` — which is ordinary — had its own project reported
    # as unknown, because `"12" not in {12}`. And a value that is not a
    # scalar at all (`[{"id": 1}]`) raised `unhashable type` out of the
    # membership test — a 500 on request input.
    parsed_projects = []
    for raw in project_ids:
        try:
            parsed_projects.append(int(raw))
        except (TypeError, ValueError):
            return (
                None,
                None,
                Response(
                    {"error": "project_ids must be a list of project ids."},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

    if parsed_projects:
        valid = set(
            ProjectMaster.objects.filter(team=team, project_id__in=parsed_projects).values_list(
                "project_id", flat=True
            )
        )
        missing = [p for p in parsed_projects if p not in valid]
        if missing:
            return (
                None,
                None,
                Response(
                    {"error": f"Unknown projects for this team: {missing}."},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

    wants_chat = any(e in CHANNEL_SCOPED_EVENTS for e in events)
    if wants_chat and not channel_ids:
        return (
            None,
            None,
            Response(
                {
                    "error": (
                        "Chat events require an explicit channel_ids list. "
                        "There is no subscribe-to-all-chat: the payload carries "
                        "message text, so every channel has to be named."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )
    if channel_ids and not wants_chat:
        return (
            None,
            None,
            Response(
                {"error": "channel_ids only applies to chat events."},
                status=status.HTTP_400_BAD_REQUEST,
            ),
        )

    if channel_ids:
        rows = list(
            Channel.objects.filter(team=team, id__in=[str(c) for c in channel_ids]).values_list(
                "id", "kind"
            )
        )
        found = {str(cid) for cid, _ in rows}
        missing = [c for c in channel_ids if str(c) not in found]
        if missing:
            # Same 404-flavoured vagueness as everywhere else: a channel
            # in another team is reported exactly like one that does not
            # exist, so this cannot enumerate channel ids.
            return (
                None,
                None,
                Response(
                    {"error": f"Unknown channels for this team: {missing}."},
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )
        forbidden = [str(cid) for cid, kind in rows if kind not in SUBSCRIBABLE_CHANNEL_KINDS]
        if forbidden:
            return (
                None,
                None,
                Response(
                    {
                        "error": (
                            f"Direct messages cannot be subscribed to: {forbidden}. "
                            "Only group, project and multi-DM channels are eligible."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                ),
            )

    return parsed_projects, [str(c) for c in channel_ids], None


def _require_manager(request, team_id):
    """`(team, None)` when allowed, `(None, Response)` otherwise.

    `team_id` is parsed first: it is a UUIDField, and an unparseable
    value raised `ValidationError` out of the ORM rather than answering
    "no such team". Same 404 as a team that does not exist — a malformed
    id names nothing.
    """
    try:
        team_id = str(uuid.UUID(str(team_id)))
    except (ValueError, AttributeError, TypeError):
        return None, Response({"error": "Team not found."}, status=status.HTTP_404_NOT_FOUND)
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

        project_ids, channel_ids, scope_err = _validate_scope(team, events, request.data)
        if scope_err:
            return scope_err

        raw_secret = generate_secret()
        endpoint = WebhookEndpoint(
            team=team,
            url=url,
            description=(request.data.get("description") or "")[:200],
            events=events,
            project_ids=project_ids,
            channel_ids=channel_ids,
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
