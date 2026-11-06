"""The consent step of connecting an AI app to Genos over MCP (MIP-1077).

`GET /oauth/authorize/` (a client's browser) ends at the consent page of the Genos app. The page
is the member's, so it talks to the API as them, with their JWT, through these two endpoints:

    POST /api/v2/mcp/oauth/consent/context/   what is being asked, to show the member
    POST /api/v2/mcp/oauth/consent/decision/  what they decided; answers where to send the browser

Both take the authorization parameters the page was given and **validate them again from
scratch** with the same library and the same checks as `/oauth/authorize/`: the page is not
trusted, a forged request gets the same refusal. JWT only, never an API key: a key that could
grant an AI app access would let one app mint another.

An approval creates or updates the member's `McpConnection` for that app (scope, destructive
opt-in, the teams chosen, a default team) and issues the authorization code, bound to the MCP
resource, in one transaction. A denial changes nothing and answers with `access_denied`.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from django.db import transaction
from django.db.models import Q
from django.http import Http404
from django.test import RequestFactory
from oauth2_provider.cimd import is_cimd_client_id
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.models import get_application_model
from rest_framework import status
from rest_framework.response import Response
from rest_framework_simplejwt.authentication import JWTAuthentication

from apis.mcp_oauth_settings import SCOPES
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster
from origin.search_engine.quota import get_mcp_enabled
from origin.services import mcp_oauth
from origin.services.security_events import record_security_event
from origin.services.throttles import WindowRateThrottle
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.public.oauth_views import Library, describe_error, refusal


class McpConsentThrottle(WindowRateThrottle):
    scope = "mcp_oauth_consent"
    fail_closed = True


def _problem(error: str, description: str, http: int = 400, **extra) -> Response:
    return Response({"error": error, "error_description": description, **extra}, status=http)


def _teams_of(user) -> list[TeamMaster]:
    return list(
        TeamMaster.objects.filter(is_deleted=False)
        .filter(Q(owner=user) | Q(team_members__attendee=user, team_members__is_deleted=False))
        .distinct()
        .order_by("team_name")
    )


class _ConsentBase(AuthenticatedAPIView):
    authentication_classes = [JWTAuthentication]
    throttle_classes = [McpConsentThrottle]

    def initial(self, request, *args, **kwargs):
        # Sign-in and the body guard first, as every view here does; then dark with the rest
        # of the server: a 404 until it is switched on.
        super().initial(request, *args, **kwargs)
        if not mcp_oauth.enabled():
            raise Http404

    def _validated(self, request):
        """Run the authorization request through the library as the member, or answer why not.

        Returns `(error_response, None)` or `(None, (django_request, scopes, credentials))`."""
        query, why = mcp_oauth.authorize_query(request.data.get("params"))
        if why:
            return _problem("invalid_request", why), None
        fake = RequestFactory().get("/oauth/authorize/?" + query)
        fake.user = request.user
        try:
            scopes, credentials = Library().validate_authorization_request(fake)
        except OAuthToolkitError as error:
            return self._library_error(error), None
        problem = mcp_oauth.authorize_problem(
            fake.GET.get("code_challenge_method"),
            fake.GET.get("code_challenge", ""),
            fake.GET.getlist("resource"),
        )
        if problem:
            return self._library_error(refusal(credentials, *problem)), None
        return None, (fake, scopes, credentials)

    @staticmethod
    def _library_error(error: OAuthToolkitError) -> Response:
        url, code, description, _status = describe_error(error)
        # `redirect_to` only when the client's own address is trustworthy: the page sends the
        # browser there so the client learns the outcome; otherwise the page shows the error.
        return _problem(code, description, redirect_to=url)


class McpConsentContextView(_ConsentBase):
    """What the consent page shows: who is asking, for what, and which teams to offer."""

    def post(self, request):
        failure, ok = self._validated(request)
        if failure:
            return failure
        _fake, scopes, credentials = ok
        application = get_application_model().objects.get(client_id=credentials["client_id"])
        teams = _teams_of(request.user)
        existing = McpConnection.objects.filter(user=request.user, application=application).first()
        client_id = application.client_id
        redirect_host = urlsplit(credentials["redirect_uri"]).hostname or ""
        return Response(
            {
                "client": {
                    "name": application.name or "",
                    # How we know the client: it registered itself (anyone can, with any name),
                    # or it is a document at this address (the host is the proof of who).
                    "kind": "metadata_document" if is_cimd_client_id(client_id) else "registered",
                    "host": urlsplit(client_id).hostname if is_cimd_client_id(client_id) else "",
                    "redirectHost": redirect_host,
                },
                "scopes": [{"name": s, "description": SCOPES[s]} for s in SCOPES if s in scopes],
                "teams": [{"id": str(t.team_id), "name": t.team_name} for t in teams],
                "planAllowed": get_mcp_enabled(str(request.user.id)),
                "existing": (
                    {
                        "active": existing.is_active,
                        "scope": existing.scope,
                        "allowDestructive": existing.allow_destructive,
                        "defaultTeamId": str(existing.default_team_id),
                        "teamIds": list(existing.team_ids),
                    }
                    if existing
                    else None
                ),
            },
            status=status.HTTP_200_OK,
        )


class McpConsentDecisionView(_ConsentBase):
    """The member's decision. Answers `{redirect_to}`: where to send the browser."""

    def post(self, request):
        failure, ok = self._validated(request)
        if failure:
            return failure
        fake, requested, credentials = ok
        allow = request.data.get("allow")
        if not isinstance(allow, bool):
            return _problem("invalid_request", "`allow` must be true or false.")

        library = Library()
        if not allow:
            return self._outcome(library, fake, credentials, [], allow=False)

        if not get_mcp_enabled(str(request.user.id)):
            return _problem(
                "plan_required",
                "Connecting an AI app is available on the Pro plan and above.",
                http=status.HTTP_403_FORBIDDEN,
            )
        decision, why = mcp_oauth.check_decision(request.data, requested)
        if why:
            return _problem("invalid_request", why)
        # Membership (owner or active member, team not deleted) is read from the database here,
        # and again on every call.
        team_ids = decision["team_ids"]
        live = {str(t.team_id) for t in _teams_of(request.user)}
        if not all(t in live for t in team_ids):
            return _problem("invalid_teams", "You can only choose teams you belong to.")

        application = get_application_model().objects.get(client_id=credentials["client_id"])
        scopes = decision["scopes"]
        with transaction.atomic():
            connection, created = McpConnection.objects.update_or_create(
                user=request.user,
                application=application,
                defaults={
                    "scope": "write" if "mcp:write" in scopes else "read",
                    "allow_destructive": "mcp:destructive" in scopes,
                    "default_team_id": decision["default_team_id"],
                    "team_ids": team_ids,
                    "revoked_at": None,
                },
            )
            response = self._outcome(
                library, fake, credentials, scopes, allow=True, record=(connection, created)
            )
            if response.status_code != 200:
                transaction.set_rollback(True)
        return response

    @staticmethod
    def _outcome(library, fake, credentials, scopes, *, allow, record=None) -> Response:
        # Always bound to our one resource, even when the client named none: a token with no
        # resource is unrestricted, and the MCP endpoint refuses those.
        credentials = {**credentials, "resource": [mcp_oauth.resource_identifier()]}
        try:
            uri, _headers, _body, _status = library.create_authorization_response(
                request=fake, scopes=" ".join(scopes), credentials=credentials, allow=allow
            )
        except OAuthToolkitError as error:
            url, code, description, _s = describe_error(error)
            if not allow and code == "access_denied":
                return Response({"redirect_to": url}, status=status.HTTP_200_OK)
            return _problem(code, description, redirect_to=url)
        if record:
            connection, created = record
            record_security_event(
                SecurityAction.MCP_CONNECTED,
                request=None,
                actor=fake.user,
                team_id=connection.default_team_id,
                target_type="mcp_connection",
                target_id=connection.id,
                target_label=connection.application.name or connection.application.client_id,
                metadata={
                    "scope": connection.scope,
                    "allow_destructive": connection.allow_destructive,
                    "teams": len(connection.team_ids),
                    "reconsent": not created,
                },
            )
        return Response({"redirect_to": uri}, status=status.HTTP_200_OK)
