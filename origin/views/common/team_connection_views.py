"""Endpoints for team-to-team connections and cross-team object shares.

Three groups, matching the three nouns:

    /api/v2/team/connection/...              the relationship
    /api/v2/team/share/...                   one object lent to one team
    /api/v2/team/share/participants/         who from the guest team is in

The interesting asymmetry is in the last one. Connections and shares are
approved once, by the side being asked. Participants are then managed
repeatedly and unilaterally by the guest team's own managers — no request
back to the host — which is why they get their own endpoint rather than
being a field on the share. See `services/external_grants` for why.

Authorization is enforced in the services, not here. These views resolve
ids, translate the services' error codes into status codes, and shape the
payload; every "may you" question is answered one layer down, so it cannot
be forgotten by a new endpoint.
"""

from __future__ import annotations

import logging

from django.db.models import Q
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import (
    ExternalGrant,
    ShareStatus,
    TeamConnection,
    TeamMaster,
)
from origin.models.common.user_models import CustomUser
from origin.services.external_grants import (
    ExternalGrantError,
    add_external_participants,
    offer_grant,
    participant_ids,
    remove_external_participants,
    respond_to_grant,
    revoke_grant,
    set_role_ceiling,
)
from origin.services.team_connection import (
    TeamConnectionError,
    request_connection,
    respond_to_connection,
    revoke_connection,
)
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.scope_guards import (
    is_team_member,
    require_team_member_or_response,
)

logger = logging.getLogger(__name__)

# Error codes that must not confirm existence. A caller who is not a
# manager of the team they named, or who named an object owned by
# somebody else, is told the thing is not there — the same 404-never-403
# convention the rest of the ACL layer follows (`scope_guards`).
_NOT_FOUND_CODES = {"not_found", "team_unavailable", "bad_object", "not_owned"}

INBOX_TEAM_CONNECTION = 7
INBOX_EXTERNAL_SHARE = 8


def _error_response(exc):
    """Translate a service error code into a response."""
    code = exc.code
    if code in _NOT_FOUND_CODES:
        return Response({"error": code}, status=status.HTTP_404_NOT_FOUND)
    if code == "not_a_manager":
        return Response({"error": code}, status=status.HTTP_403_FORBIDDEN)
    return Response({"error": code}, status=status.HTTP_400_BAD_REQUEST)


def _team_name(team_id) -> str:
    row = TeamMaster.objects.filter(team_id=team_id).values("team_name").first()
    return row["team_name"] if row else ""


def _connection_payload(conn: TeamConnection, viewing_team_id) -> dict:
    """Shape a connection from one team's point of view.

    The row stores its pair sorted (see `TeamConnection`), so "the other
    team" has to be derived per viewer rather than read off a field. The
    client only ever cares about the counterparty and whether the ball is
    in its court, which is what `direction` answers.
    """
    lo, hi = str(conn.team_lo_id), str(conn.team_hi_id)
    other = hi if lo == str(viewing_team_id) else lo
    return {
        "connectionId": str(conn.id),
        "teamId": other,
        "teamName": _team_name(other),
        "status": conn.status,
        "direction": (
            "outgoing" if str(conn.requested_by_team_id) == str(viewing_team_id) else "incoming"
        ),
        "tsCreated": conn.ts_created_at,
        "tsUpdated": conn.ts_updated_at,
    }


def _grant_payload(grant: ExternalGrant, viewing_team_id) -> dict:
    is_host = str(grant.owner_team_id) == str(viewing_team_id)
    counterparty = grant.guest_team_id if is_host else grant.owner_team_id
    return {
        "grantId": str(grant.id),
        "objectType": grant.object_type,
        "objectId": grant.object_id,
        "roleCeiling": grant.role_ceiling,
        "status": grant.status,
        # "given" = we own the object and lent it out; "received" = we
        # were let in. Drives entirely different UI on each side.
        "side": "given" if is_host else "received",
        "teamId": str(counterparty),
        "teamName": _team_name(counterparty),
        "tsCreated": grant.ts_created_at,
        "tsUpdated": grant.ts_updated_at,
    }


def _notify_owner(*, team_id, sender, item_type, item_body, item_optionals) -> None:
    """Drop a request row in the receiving team owner's inbox.

    Written server-side, unlike the older join-request flows where the
    client posts to `/api/v2/inbox/joinXRequest/` after the fact. A
    cross-team request that silently failed to reach anyone would look
    to the asker exactly like one nobody had answered yet.

    Best-effort: the connection or share is already committed, and a
    notification problem must not fail it. The row is also discoverable
    from the connection list, so a lost inbox item delays the response
    rather than losing the request.
    """
    try:
        owner_id = (
            TeamMaster.objects.filter(team_id=team_id).values_list("owner_id", flat=True).first()
        )
        if not owner_id:
            return
        InboxItems.objects.create(
            team_id=team_id,
            sender=sender,
            receiver_id=owner_id,
            item_body=item_body,
            item_type=item_type,
            item_optionals=item_optionals,
            is_read=False,
            request_status="pending",
        )
    except Exception:  # noqa: BLE001 — never fail the request over its notice
        logger.exception("inbox notification failed for team=%s type=%s", team_id, item_type)


class TeamConnectionView(AuthenticatedAPIView):
    """GET  /api/v2/team/connection/?team_id=  — this team's connections.
    POST /api/v2/team/connection/            — ask another team to connect.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := require_team_member_or_response(request.user, team_id):
            return res

        # Either position in the normalized pair. Declined rows are
        # withheld: a refusal the other team never has to explain is the
        # point of declining, and the asker sees the request simply go
        # away rather than a standing "no".
        rows = (
            TeamConnection.objects.filter(Q(team_lo=team_id) | Q(team_hi=team_id))
            .exclude(status=ShareStatus.DECLINED)
            .order_by("-ts_updated_at")
        )
        return Response(
            {"connections": [_connection_payload(c, team_id) for c in rows]},
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        team_id = request.data.get("team_id")
        target_team_id = request.data.get("target_team_id")
        if not team_id or not target_team_id:
            return Response(
                {"error": "team_id and target_team_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            conn = request_connection(team_id, target_team_id, request.user)
        except TeamConnectionError as exc:
            return _error_response(exc)

        _notify_owner(
            team_id=target_team_id,
            sender=request.user,
            item_type=INBOX_TEAM_CONNECTION,
            item_body={
                "title": "Team connection request",
                "text": f"{_team_name(team_id)} would like to connect with your team.",
            },
            item_optionals={
                "connection_id": str(conn.id),
                "requesting_team_id": str(team_id),
                "requesting_team_name": _team_name(team_id),
            },
        )
        return Response(
            _connection_payload(conn, team_id),
            status=status.HTTP_201_CREATED,
        )


class TeamConnectionRespondView(AuthenticatedAPIView):
    """POST /api/v2/team/connection/respond/ — approve or decline.

    Only a manager of the team that was ASKED may answer, which the
    service enforces. Settling the inbox item here keeps the owner's
    inbox from showing a request they have already answered.
    """

    def post(self, request):
        connection_id = request.data.get("connection_id")
        accept = bool(request.data.get("accept"))
        conn = TeamConnection.objects.filter(id=connection_id).first()
        try:
            conn = respond_to_connection(conn, request.user, accept)
        except TeamConnectionError as exc:
            return _error_response(exc)

        InboxItems.objects.filter(
            item_type=INBOX_TEAM_CONNECTION,
            item_optionals__connection_id=str(connection_id),
            request_status="pending",
        ).update(request_status="approved" if accept else "rejected", is_read=True)

        return Response(
            _connection_payload(conn, conn.requested_by_team_id),
            status=status.HTTP_200_OK,
        )


class TeamConnectionRevokeView(AuthenticatedAPIView):
    """POST /api/v2/team/connection/revoke/ — end it, and every share in it.

    Available to managers of either side. Reports how many participation
    rows were withdrawn so the UI can say what actually happened rather
    than a bare success.
    """

    def post(self, request):
        connection_id = request.data.get("connection_id")
        conn = TeamConnection.objects.filter(id=connection_id).first()
        try:
            withdrawn = revoke_connection(conn, request.user)
        except TeamConnectionError as exc:
            return _error_response(exc)
        return Response(
            {"connectionId": str(connection_id), "withdrawn": withdrawn},
            status=status.HTTP_200_OK,
        )


class ExternalShareView(AuthenticatedAPIView):
    """GET  /api/v2/team/share/?team_id=  — shares given and received.
    POST /api/v2/team/share/            — offer an object to a connected team.
    PUT  /api/v2/team/share/            — change a share's role ceiling.
    """

    def get(self, request):
        team_id = request.GET.get("team_id")
        if res := require_team_member_or_response(request.user, team_id):
            return res

        rows = (
            ExternalGrant.objects.filter(Q(owner_team=team_id) | Q(guest_team=team_id))
            .exclude(status=ShareStatus.DECLINED)
            .order_by("-ts_updated_at")
        )
        return Response(
            {"shares": [_grant_payload(g, team_id) for g in rows]},
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        data = request.data
        required = ("team_id", "guest_team_id", "object_type", "object_id")
        if any(not data.get(k) for k in required):
            return Response(
                {"error": f"{', '.join(required)} are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            grant = offer_grant(
                owner_team_id=data["team_id"],
                guest_team_id=data["guest_team_id"],
                object_type=data["object_type"],
                object_id=data["object_id"],
                role_ceiling=data.get("role_ceiling") or "viewer",
                actor=request.user,
            )
        except ExternalGrantError as exc:
            return _error_response(exc)

        _notify_owner(
            team_id=grant.guest_team_id,
            sender=request.user,
            item_type=INBOX_EXTERNAL_SHARE,
            item_body={
                "title": "External share offer",
                "text": (
                    f"{_team_name(grant.owner_team_id)} shared a "
                    f"{grant.get_object_type_display()} with your team."
                ),
            },
            item_optionals={
                "grant_id": str(grant.id),
                "object_type": grant.object_type,
                "object_id": grant.object_id,
                "owner_team_name": _team_name(grant.owner_team_id),
            },
        )
        return Response(
            _grant_payload(grant, grant.owner_team_id),
            status=status.HTTP_201_CREATED,
        )

    def put(self, request):
        grant = ExternalGrant.objects.filter(id=request.data.get("grant_id")).first()
        try:
            grant = set_role_ceiling(grant, request.data.get("role_ceiling"), request.user)
        except ExternalGrantError as exc:
            return _error_response(exc)
        return Response(_grant_payload(grant, grant.owner_team_id), status=status.HTTP_200_OK)


class ExternalShareRespondView(AuthenticatedAPIView):
    """POST /api/v2/team/share/respond/ — the guest team accepts or declines.

    Accepting admits nobody. It opens the door; walking people through
    it is `ExternalShareParticipantsView`, any time afterwards.
    """

    def post(self, request):
        grant_id = request.data.get("grant_id")
        accept = bool(request.data.get("accept"))
        grant = ExternalGrant.objects.filter(id=grant_id).first()
        try:
            grant = respond_to_grant(grant, request.user, accept)
        except ExternalGrantError as exc:
            return _error_response(exc)

        InboxItems.objects.filter(
            item_type=INBOX_EXTERNAL_SHARE,
            item_optionals__grant_id=str(grant_id),
            request_status="pending",
        ).update(request_status="approved" if accept else "rejected", is_read=True)

        return Response(_grant_payload(grant, grant.guest_team_id), status=status.HTTP_200_OK)


class ExternalShareRevokeView(AuthenticatedAPIView):
    """POST /api/v2/team/share/revoke/ — withdraw one share."""

    def post(self, request):
        grant_id = request.data.get("grant_id")
        grant = ExternalGrant.objects.filter(id=grant_id).first()
        try:
            withdrawn = revoke_grant(grant, request.user)
        except ExternalGrantError as exc:
            return _error_response(exc)
        return Response(
            {"grantId": str(grant_id), "withdrawn": withdrawn},
            status=status.HTTP_200_OK,
        )


class ExternalShareParticipantsView(AuthenticatedAPIView):
    """The delegated roster — the endpoint that is used repeatedly.

    GET    /api/v2/team/share/participants/?grant_id=  — who is in
    POST   /api/v2/team/share/participants/            — admit people
    DELETE /api/v2/team/share/participants/            — withdraw people

    POST is guest-team managers only: it is their roster. DELETE is open
    to either side's managers, so a host can eject one person without
    revoking the whole share. Both rules live in the service.
    """

    @staticmethod
    def _visible(grant, user) -> bool:
        """Either team's members may read the roster of a share.

        Both sides need it — the guest to manage it, the host to see who
        it let in — and a share whose membership the host could not
        inspect would be a share nobody could audit.
        """
        return is_team_member(grant.owner_team_id, user.id) or is_team_member(
            grant.guest_team_id, user.id
        )

    def get(self, request):
        grant = ExternalGrant.objects.filter(id=request.GET.get("grant_id")).first()
        if grant is None or not self._visible(grant, request.user):
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)

        ids = participant_ids(grant)
        users = CustomUser.objects.filter(id__in=ids).values(
            "id", "username", "email", "profile_image_file_name"
        )
        return Response(
            {
                "grantId": str(grant.id),
                "roleCeiling": grant.role_ceiling,
                "participants": [
                    {
                        "userId": str(u["id"]),
                        "userName": u["username"],
                        "email": u["email"],
                        "avatarUrl": u["profile_image_file_name"],
                    }
                    for u in users
                ],
            },
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        grant = ExternalGrant.objects.filter(id=request.data.get("grant_id")).first()
        user_ids = request.data.get("user_ids") or []
        try:
            admitted = add_external_participants(
                grant, user_ids, request.user, role=request.data.get("role")
            )
        except ExternalGrantError as exc:
            return _error_response(exc)
        return Response({"admitted": admitted}, status=status.HTTP_201_CREATED)

    def delete(self, request):
        grant = ExternalGrant.objects.filter(id=request.data.get("grant_id")).first()
        user_ids = request.data.get("user_ids") or []
        try:
            removed = remove_external_participants(grant, user_ids, request.user)
        except ExternalGrantError as exc:
            return _error_response(exc)
        return Response({"removed": removed}, status=status.HTTP_200_OK)
