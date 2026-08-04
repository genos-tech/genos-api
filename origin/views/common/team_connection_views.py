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

from django.core.exceptions import ValidationError as DjangoValidationError
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
from origin.services.cross_team_notices import (
    ITEM_TYPE_EXTERNAL_SHARE,
    ITEM_TYPE_TEAM_CONNECTION,
    connection_request_body,
    notify_team_owner,
    pending_notices,
)
from origin.services.external_grants import (
    ExternalGrantError,
    add_external_participants,
    may_read_object_shares,
    offer_grant,
    participant_ids,
    remove_external_participants,
    respond_to_grant,
    revoke_grant,
    set_role_ceiling,
    visible_shares_for_object,
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

INBOX_TEAM_CONNECTION = ITEM_TYPE_TEAM_CONNECTION
INBOX_EXTERNAL_SHARE = ITEM_TYPE_EXTERNAL_SHARE


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


def _owner_team_ids(viewing_team_id) -> set:
    """Connected teams that OWN something currently shared with this one.

    A connection is symmetric — either side may ask for it, and `direction`
    records only who asked. Sharing is not symmetric: one team owns the
    project and the other works in it, and THAT is the fact someone reading
    this list needs. One query for the whole list rather than per row.
    """
    return {
        str(team_id)
        for team_id in ExternalGrant.objects.filter(
            guest_team_id=viewing_team_id, status=ShareStatus.ACTIVE
        ).values_list("owner_team_id", flat=True)
    }


def _connection_payload(conn: TeamConnection, viewing_team_id, owner_team_ids=frozenset()) -> dict:
    """Shape a connection from one team's point of view.

    The row stores its pair sorted (see `TeamConnection`), so "the other
    team" has to be derived per viewer rather than read off a field. The
    client only ever cares about the counterparty and whether the ball is
    in its court, which is what `direction` answers.

    `isOwner` says the other team owns shared work you have access to.
    Defaults false for the caller that has just CREATED a connection —
    correct rather than merely convenient, since nothing has been shared
    across a connection nobody has answered yet.
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
        "isOwner": other in owner_team_ids,
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
        owner_team_ids = _owner_team_ids(team_id)
        return Response(
            {"connections": [_connection_payload(c, team_id, owner_team_ids) for c in rows]},
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

        notify_team_owner(
            team_id=target_team_id,
            sender=request.user,
            item_type=INBOX_TEAM_CONNECTION,
            item_body=connection_request_body(
                requesting_team_name=_team_name(team_id),
                addressed_team_name=_team_name(target_team_id),
            ),
            item_optionals={
                "connection_id": str(conn.id),
                "requesting_team_id": str(team_id),
                "requesting_team_name": _team_name(team_id),
            },
            push_title=f"{_team_name(team_id)} would like to connect with your team",
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
        # Optional narrowing to one object, for the per-object panels (a
        # chat's or project's own "shared with" section). Narrowing only —
        # the team filter above is what makes this safe, so an object id
        # naming something this team has no share on returns nothing
        # rather than anything new.
        object_type = request.GET.get("object_type")
        object_id = request.GET.get("object_id")
        if object_type:
            rows = rows.filter(object_type=object_type)
        if object_id:
            rows = rows.filter(object_id=str(object_id))
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

        # The guest team's owner is notified by `offer_grant` itself.
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


class ExternalShareObjectView(AuthenticatedAPIView):
    """GET /api/v2/team/share/object/?object_type=&object_id=

    "Who is this object shared with", asked from the object rather than
    from a team. The distinction matters: an external participant reads a
    shared project or folder while switched into the HOST team's shell and
    belongs to neither team, so `ExternalShareView` — which gates on
    membership of the team named in the query — refuses them their own
    share. Keying on the object lets both sides use one call.

    Read-only. Offering and revoking stay on `/team/share/` (host
    managers) and the roster stays on `/team/share/participants/` (guest
    managers), so no authority is duplicated here.
    """

    def get(self, request):
        object_type = request.GET.get("object_type")
        object_id = request.GET.get("object_id")
        if not object_type or not object_id:
            return Response(
                {"error": "object_type and object_id are required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if object_type not in ExternalGrant.ObjectType.values:
            return Response({"error": "bad_object"}, status=status.HTTP_400_BAD_REQUEST)
        if not may_read_object_shares(object_type, object_id, request.user):
            # 404, not 403: a caller with no relationship to the object
            # learns nothing about whether it exists.
            return Response({"error": "not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(
            {"shares": visible_shares_for_object(object_type, object_id, request.user)},
            status=status.HTTP_200_OK,
        )


class CrossTeamNoticeView(AuthenticatedAPIView):
    """GET /api/v2/team/notice/ — the card(s) to relay for one request.

    Keyed by `connection_id`, by `grant_id`, or by the object a share was
    just created on (`object_type` + `object_id`) — that last form because
    creating an external chat offers it to several teams at once and the
    creator never sees the grant ids.

    Exists so the ASKING side can push its own request into the answering
    side's open inbox. Django files these rows itself, unlike inbox types
    1-4 which the sockets service files and delivers in one step, so
    without this the addressee sees nothing until a page reload — and a
    request nobody notices is a feature that stops at step one.

    The relay is triggered by the requester's browser, so the payload is
    read back here rather than accepted from it; otherwise the event would
    be a way to push arbitrary inbox content at any user. Authorisation is
    "you are the side that asked": a member of the team that filed the
    request, and it must still be unanswered.

    Nothing to relay is an empty list, never an error. A request that was
    already answered and one the caller has no standing over are the same
    non-event here, and telling them apart would leak which is which.
    """

    def get(self, request):
        connection_id = request.GET.get("connection_id")
        grant_id = request.GET.get("grant_id")
        object_type = request.GET.get("object_type")
        object_id = request.GET.get("object_id")
        if not connection_id and not grant_id and not (object_type and object_id):
            return Response(
                {"error": "connection_id, grant_id, or object_type + object_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        notices = []
        try:
            if connection_id:
                conn = TeamConnection.objects.filter(
                    id=connection_id, status=ShareStatus.PENDING
                ).first()
                if conn is not None and is_team_member(conn.requested_by_team_id, request.user.id):
                    notices = pending_notices(
                        item_type=INBOX_TEAM_CONNECTION, key="connection_id", values=[conn.id]
                    )
            else:
                grants = ExternalGrant.objects.filter(status=ShareStatus.PENDING)
                if grant_id:
                    grants = grants.filter(id=grant_id)
                else:
                    grants = grants.filter(object_type=object_type, object_id=str(object_id))
                mine = [g for g in grants if is_team_member(g.owner_team_id, request.user.id)]
                notices = pending_notices(
                    item_type=INBOX_EXTERNAL_SHARE,
                    key="grant_id",
                    values=[g.id for g in mine],
                )
        except (DjangoValidationError, ValueError, TypeError):
            # An id that isn't an id names nothing. Same empty answer as an
            # id that names something the caller may not relay.
            notices = []
        return Response({"notices": notices}, status=status.HTTP_200_OK)


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
