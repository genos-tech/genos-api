"""The OAuth 2.1 authorization server's endpoints for MCP (MIP-1077).

django-oauth-toolkit supplies the protocol views. What is here is what Genos has to do
differently, and the guard that keeps all of it dark until `MCP_OAUTH_ENABLED`:

  * `AuthorizeView`: DOT's own authorization view needs a Django login, and Genos has none (the
    app holds a JWT in memory). This one VALIDATES the request exactly as DOT does, then sends
    the browser to the consent page of the Genos app with the same parameters. The page asks the
    member, and posts the decision to the API with their JWT (MIP-1079); the server re-validates
    everything there, so nothing the page says is trusted.
  * `RegisterView`: wraps DOT's registration endpoint with a rate limit and `check_registration`
    (what a stranger may ask for), and gives DOT only the cleaned document.
  * Token and revoke are DOT's, behind the same flag and a per-address rate limit.

Every route answers 404 while the flag is off, so deploying this changes nothing until it is
turned on.
"""

from __future__ import annotations

import functools
import json
import logging
from urllib.parse import urlencode

from django.http import HttpResponseRedirect, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.http import OAuth2ResponseRedirect
from oauth2_provider.models import get_access_token_model, get_application_model
from oauth2_provider.settings import oauth2_settings
from oauth2_provider.views import (
    OAuthProtectedResourceMetadataView,
    OAuthServerMetadataView,
    RevokeTokenView,
    TokenView,
)
from oauth2_provider.views.dynamic_client_registration import DynamicClientRegistrationView
from oauth2_provider.views.mixins import OAuthLibMixin
from oauthlib.oauth2.rfc6749.errors import CustomOAuth2Error

from origin.services import mcp_oauth
from origin.services.throttles import AuthThrottle


class OAuthRegisterThrottle(AuthThrottle):
    scope = "mcp_oauth_register"


class OAuthTokenThrottle(AuthThrottle):
    scope = "mcp_oauth_token"


class OAuthAuthorizeThrottle(AuthThrottle):
    scope = "mcp_oauth_authorize"


def _json_error(error: str, description: str, status: int = 400, **headers) -> JsonResponse:
    response = JsonResponse({"error": error, "error_description": description}, status=status)
    for name, value in headers.items():
        response[name.replace("_", "-")] = value
    return response


def _not_found() -> JsonResponse:
    return JsonResponse({"error": "not_found"}, status=404)


def gated(view):
    """404 unless OAuth is switched on (and has an address to anchor to)."""

    @functools.wraps(view)
    def inner(request, *args, **kwargs):
        if not mcp_oauth.enabled():
            return _not_found()
        return view(request, *args, **kwargs)

    return inner


def throttled(throttle_class):
    """A per-address rate limit that REFUSES when the cache is down (a security control)."""

    def decorate(view):
        @functools.wraps(view)
        def inner(request, *args, **kwargs):
            throttle = throttle_class()
            if not throttle.allow_request(request, None):
                return _json_error(
                    "temporarily_unavailable",
                    "Too many requests. Try again shortly.",
                    429,
                    Retry_After=str(int(throttle.wait() or 60)),
                )
            return view(request, *args, **kwargs)

        return inner

    return decorate


log = logging.getLogger(__name__)


class Library(OAuthLibMixin):
    """The library's authorization machinery, for the views that are not its own views."""


def refusal(credentials, error: str, description: str) -> OAuthToolkitError:
    """An OAuth error to send back to the client's (already validated) redirect address."""
    return OAuthToolkitError(
        error=CustomOAuth2Error(
            error=error, description=description, state=credentials.get("state")
        ),
        redirect_uri=credentials["redirect_uri"],
    )


def describe_error(error: OAuthToolkitError) -> tuple[str | None, str, str, int]:
    """`(redirect_url, error, description, status)`. The URL is None when the client's address
    cannot be trusted (an unknown client, a redirect that is not registered): nothing is sent
    there, the person is told. RFC 9207: an error response names its issuer too, so a client
    can tell which server it came from (the library adds `iss` only to a success)."""
    redirect, payload = Library().error_response(error)
    oauthlib_error = payload["error"]
    if not redirect:
        return (
            None,
            oauthlib_error.error,
            oauthlib_error.description or "",
            oauthlib_error.status_code,
        )
    url = payload["url"]
    url += ("&" if "?" in url else "?") + urlencode({"iss": mcp_oauth.public_base()})
    return url, oauthlib_error.error, oauthlib_error.description or "", oauthlib_error.status_code


class AuthorizeView(OAuthLibMixin, View):
    """`GET /oauth/authorize/`: validate, then hand the browser to the consent page."""

    http_method_names = ["get", "options"]

    def get(self, request):
        try:
            _scopes, credentials = self.validate_authorization_request(request)
        except OAuthToolkitError as error:
            return self._error(error)

        application = get_application_model().objects.get(client_id=credentials["client_id"])
        why = mcp_oauth.client_problem(application)
        if why:
            # Its redirect addresses are not trusted, so nothing is sent there.
            return _json_error("invalid_client", why)

        problem = mcp_oauth.authorize_problem(
            request.GET.get("code_challenge_method"),
            request.GET.get("code_challenge", ""),
            request.GET.getlist("resource"),
        )
        if problem:
            return self._refuse(credentials, *problem)

        response = HttpResponseRedirect(
            f"{mcp_oauth.consent_url()}?{request.META.get('QUERY_STRING', '')}"
        )
        response["Cache-Control"] = "no-store"
        return response

    def _refuse(self, credentials, error: str, description: str):
        return self._error(refusal(credentials, error, description))

    def _error(self, error: OAuthToolkitError):
        url, code, description, status = describe_error(error)
        if url:
            return OAuth2ResponseRedirect(url, oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES)
        return _json_error(code, description, status)


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    """`POST /oauth/register/`: RFC 7591, narrowed to what Genos allows a stranger to ask for."""

    http_method_names = ["post", "options"]

    def post(self, request):
        if mcp_oauth.registrations_capped():
            log.warning("mcp_oauth: the day's client registration quota is used up")
            return _json_error(
                "temporarily_unavailable",
                "Too many clients registered today. Try again later.",
                503,
                Retry_After="3600",
            )
        body = request.body
        if len(body) > mcp_oauth.MAX_BODY_BYTES:
            return _json_error(
                "invalid_client_metadata", "The registration request is too large.", 413
            )
        try:
            data = json.loads(body or b"null")
        except (ValueError, UnicodeDecodeError, RecursionError):
            # RecursionError: a few KB of `[` nests past the interpreter's limit.
            return _json_error("invalid_client_metadata", "The request body must be valid JSON.")
        cleaned, problem = mcp_oauth.check_registration(data)
        if problem:
            return _json_error(*problem)
        # The library gets the cleaned document and nothing else.
        request._body = json.dumps(cleaned).encode()
        response = DynamicClientRegistrationView.as_view()(request)
        if response.status_code == 201:
            response = self._without_management(response)
        response["Cache-Control"] = "no-store"
        return response

    @staticmethod
    def _without_management(response):
        """RFC 7592 hands every new client a token to later EDIT its own registration, which
        would walk around `check_registration` (a client could swap in any redirect address).
        Genos offers no management endpoint, so the token is deleted and left out."""
        document = json.loads(response.content)
        document.pop("registration_access_token", None)
        document.pop("registration_client_uri", None)
        get_access_token_model().objects.filter(
            application__client_id=document["client_id"],
            scope=oauth2_settings.DCR_REGISTRATION_SCOPE,
        ).delete()
        return JsonResponse(document, status=201)


def json_errors(view):
    """RFC 6749 §5.2: an error from the token endpoint is `application/json`. The library
    sends the `invalid_target` of a resource-escalation request as JSON text labelled
    `text/html`, which a strict client refuses to parse; say what it is."""

    @functools.wraps(view)
    def inner(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        if (
            response.get("Content-Type", "").startswith("text/html")
            and response.content[:1] == b"{"
        ):
            try:
                json.loads(response.content)
            except ValueError:
                return response
            response["Content-Type"] = "application/json"
        return response

    return inner


def refresh_race_guard(view):
    """Two refreshes of one token at once: the second finds its predecessor already rotated and,
    with tokens hashed at rest, the library trips over the blank value it would hand back (a
    500). Nothing leaks and no second pair is made; the client is told what it should be told
    (`invalid_grant`: use the newer token), as for any replayed refresh token."""

    @functools.wraps(view)
    def inner(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except get_access_token_model().DoesNotExist:
            return _json_error("invalid_grant", "The refresh token was already used.")

    return inner


def registration_management_view(request, client_id):
    """Only here so the library can name this route when it builds its response; see
    `RegisterView._without_management`. There is nothing to manage."""
    return _not_found()


authorize_view = gated(throttled(OAuthAuthorizeThrottle)(never_cache(AuthorizeView.as_view())))
register_view = gated(throttled(OAuthRegisterThrottle)(RegisterView.as_view()))
token_view = gated(
    throttled(OAuthTokenThrottle)(never_cache(refresh_race_guard(json_errors(TokenView.as_view()))))
)
revoke_view = gated(
    throttled(OAuthTokenThrottle)(never_cache(json_errors(RevokeTokenView.as_view())))
)
server_metadata_view = gated(OAuthServerMetadataView.as_view())
resource_metadata_view = gated(OAuthProtectedResourceMetadataView.as_view())
