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
import re

from django.http import HttpResponseRedirect, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from oauth2_provider.exceptions import OAuthToolkitError
from oauth2_provider.http import OAuth2ResponseRedirect
from oauth2_provider.models import get_access_token_model
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

# RFC 7636 §4.2: 43 to 128 characters of the unreserved set (an S256 challenge is 43).
_CHALLENGE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


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


class AuthorizeView(OAuthLibMixin, View):
    """`GET /oauth/authorize/`: validate, then hand the browser to the consent page."""

    http_method_names = ["get", "options"]

    def get(self, request):
        try:
            _scopes, credentials = self.validate_authorization_request(request)
        except OAuthToolkitError as error:
            return self._error(error)

        # PKCE with S256 only, checked HERE and not left to the token step: DOT treats a missing
        # method as `plain` and refuses that only when the code is issued, after the member has
        # been asked. A client that cannot do S256 is told now.
        challenge = request.GET.get("code_challenge", "")
        if request.GET.get("code_challenge_method") != "S256" or not _CHALLENGE.fullmatch(
            challenge
        ):
            return self._refuse(
                credentials, "invalid_request", "PKCE with code_challenge_method=S256 is required."
            )

        # RFC 8707: a token is for one resource, ours. A client that names another is refused
        # with `invalid_target`; one that names none gets ours (there is only one to give).
        for resource in request.GET.getlist("resource"):
            if resource != mcp_oauth.resource_identifier():
                return self._refuse(
                    credentials,
                    "invalid_target",
                    "This server only issues tokens for its MCP endpoint.",
                )

        response = HttpResponseRedirect(
            f"{mcp_oauth.consent_url()}?{request.META.get('QUERY_STRING', '')}"
        )
        response["Cache-Control"] = "no-store"
        return response

    def _refuse(self, credentials, error: str, description: str):
        return self._error(
            OAuthToolkitError(
                error=CustomOAuth2Error(
                    error=error, description=description, state=credentials.get("state")
                ),
                redirect_uri=credentials["redirect_uri"],
            )
        )

    def _error(self, error: OAuthToolkitError):
        redirect, payload = self.error_response(error)
        if redirect:
            return OAuth2ResponseRedirect(
                payload["url"], oauth2_settings.ALLOWED_REDIRECT_URI_SCHEMES
            )
        oauthlib_error = payload["error"]
        return _json_error(
            oauthlib_error.error, oauthlib_error.description or "", oauthlib_error.status_code
        )


@method_decorator(csrf_exempt, name="dispatch")
class RegisterView(View):
    """`POST /oauth/register/`: RFC 7591, narrowed to what Genos allows a stranger to ask for."""

    http_method_names = ["post", "options"]

    def post(self, request):
        body = request.body
        if len(body) > mcp_oauth.MAX_BODY_BYTES:
            return _json_error(
                "invalid_client_metadata", "The registration request is too large.", 413
            )
        try:
            data = json.loads(body or b"null")
        except (ValueError, UnicodeDecodeError):
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


def registration_management_view(request, client_id):
    """Only here so the library can name this route when it builds its response; see
    `RegisterView._without_management`. There is nothing to manage."""
    return _not_found()


authorize_view = gated(throttled(OAuthAuthorizeThrottle)(never_cache(AuthorizeView.as_view())))
register_view = gated(throttled(OAuthRegisterThrottle)(RegisterView.as_view()))
token_view = gated(throttled(OAuthTokenThrottle)(never_cache(TokenView.as_view())))
revoke_view = gated(throttled(OAuthTokenThrottle)(never_cache(RevokeTokenView.as_view())))
server_metadata_view = gated(OAuthServerMetadataView.as_view())
resource_metadata_view = gated(OAuthProtectedResourceMetadataView.as_view())
