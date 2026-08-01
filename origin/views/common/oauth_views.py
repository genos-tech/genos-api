"""OAuth flow endpoints.

Two flows, generalized over provider:

  GET /api/v2/oauth/{provider}/initiate/?intent=login|connect&next=<path>
    → 302 to the provider's consent screen.

  GET /api/v2/oauth/{provider}/callback/?code=...&state=...
    → exchange code, fetch profile, create/sign-in user (intent=login) OR
      attach the ConnectedAccount to the current user (intent=connect),
      then 302 to the frontend bounce page.

Plus three connection-management endpoints:

  GET    /api/v2/integrations/me/            — list current user's ConnectedAccounts
  DELETE /api/v2/integrations/account/{id}/  — disconnect one specific account
  DELETE /api/v2/integrations/{provider}/    — legacy by-provider disconnect

A user may hold several accounts for a multi-account provider (Google),
so "disconnect" has to name *which* one — hence the by-id route. Either
route refuses to delete the row the user signs in with.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta
from typing import Optional
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.db import transaction
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.user_models import MULTI_ACCOUNT_PROVIDERS, ConnectedAccount
from origin.services import crypto
from origin.services.oauth.base import FlowIntent, OAuthProvider, ProviderProfile, TokenResponse
from origin.services.oauth.registry import get_provider, supported_provider_names

from .auth_views import _set_refresh_cookie

logger = logging.getLogger(__name__)
User = get_user_model()


# State token has a short TTL — the user has at most this long between
# clicking "Sign in with Google" and finishing consent.
STATE_TOKEN_MAX_AGE_SECONDS = 600
STATE_SALT = "oauth-state"


def _redirect_uri(provider_name: str) -> str:
    """Build the callback URL that must match what's registered in the
    provider's OAuth app config."""
    return f"{settings.BACKEND_BASE_URL}/api/v2/oauth/{provider_name}/callback/"


def _frontend_success_url(*, access_token: str, next_path: str) -> str:
    """Frontend bounce page. Access token in URL fragment (not query)
    so it never reaches our server logs via the Referer header.

    Default fallback is `/jointeam` — new OAuth users have no team yet
    and any `/workspace/*` destination would immediately fire team-
    scoped requests with an empty `teamId`, blowing up the backend.
    """
    next_safe = next_path if next_path.startswith("/") else "/jointeam"
    fragment = urlencode({"access": access_token, "next": next_safe})
    return f"{settings.FRONTEND_BASE_URL}/oauth/success#{fragment}"


def _frontend_failure_url(*, reason: str, primary: Optional[str] = None) -> str:
    params = {"error": reason}
    if primary:
        params["primary"] = primary
    return f"{settings.FRONTEND_BASE_URL}/oauth/success?{urlencode(params)}"


def _sign_state(*, intent: FlowIntent, user_id: Optional[str], next_path: str) -> str:
    return signing.dumps(
        {
            "intent": intent,
            "user_id": user_id,
            "next": next_path,
            "nonce": secrets.token_urlsafe(16),
        },
        salt=STATE_SALT,
    )


def _verify_state(state: str) -> dict:
    return signing.loads(state, salt=STATE_SALT, max_age=STATE_TOKEN_MAX_AGE_SECONDS)


def _provider_allows_additional_account(user, provider_name: str) -> bool:
    """True if `user` may attach one more account for this provider.

    Multi-account providers (Google) are always allowed. Everything else
    is capped at one, matching the conditional unique constraint on
    `ConnectedAccount` — checked here so hitting the cap redirects to a
    readable failure page instead of raising IntegrityError mid-callback.
    """
    if provider_name in MULTI_ACCOUNT_PROVIDERS:
        return True
    return not ConnectedAccount.objects.filter(user=user, provider=provider_name).exists()


def _client_configured(provider_name: str) -> bool:
    if provider_name == "google":
        return bool(settings.GOOGLE_OAUTH_CLIENT_ID and settings.GOOGLE_OAUTH_CLIENT_SECRET)
    if provider_name == "github":
        return bool(settings.GITHUB_OAUTH_CLIENT_ID and settings.GITHUB_OAUTH_CLIENT_SECRET)
    return False


class OAuthInitiateView(APIView):
    """Build the consent URL and dispatch the browser to the provider.

    Two methods on the same URL because they have different auth
    requirements:

      GET  — `intent=login` only. Unauthenticated top-level navigation
             (the user isn't signed in yet). Returns a 302 directly to
             the provider's consent screen so the browser can follow it
             without involving JS.

      POST — `intent=connect` only. Requires a valid Bearer token (the
             user IS signed in and wants to attach a new provider for
             API access). Returns `{"url": ...}` JSON; the frontend
             then sets `window.location.href` to that URL. This split
             exists because `window.location.href = ...` can't carry
             the Authorization header, so the connect flow has to go
             through an authenticated XHR first.
    """

    permission_classes = [permissions.AllowAny]
    _COMMON_VALIDATIONS = ("provider_name", "client_configured")

    def _validate_common(self, provider_name: str):
        if provider_name not in supported_provider_names():
            return Response(
                {"detail": f"Unknown provider: {provider_name}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not _client_configured(provider_name):
            return Response(
                {"detail": f"{provider_name} OAuth is not configured on this server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return None

    def get(self, request: Request, provider_name: str):
        err = self._validate_common(provider_name)
        if err is not None:
            return err

        # GET path is for login only — it returns a 302 with no auth.
        # `intent=connect` over GET would silently leak the lack of
        # Authorization header (browsers can't attach one on top-level
        # navigations), so reject it explicitly with a hint to use POST.
        intent = request.GET.get("intent", "login")
        if intent != "login":
            return Response(
                {
                    "detail": (
                        "Use POST for intent=connect (a Bearer-authenticated "
                        "XHR), then redirect to the returned URL."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        next_path = request.GET.get("next", "/jointeam")

        provider: OAuthProvider = get_provider(provider_name)
        state = _sign_state(intent="login", user_id=None, next_path=next_path)
        url = provider.authorize_url(
            state=state, intent="login", redirect_uri=_redirect_uri(provider_name)
        )
        return HttpResponseRedirect(url)

    def post(self, request: Request, provider_name: str):
        err = self._validate_common(provider_name)
        if err is not None:
            return err

        if not request.user or not request.user.is_authenticated:
            return Response(
                {"detail": "Must be signed in to connect a provider."},
                status=status.HTTP_401_UNAUTHORIZED,
            )

        intent = request.data.get("intent", "connect")
        if intent != "connect":
            return Response(
                {"detail": "POST only supports intent=connect."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        next_path = request.data.get("next", "/workspace/integrations")

        provider: OAuthProvider = get_provider(provider_name)
        state = _sign_state(intent="connect", user_id=str(request.user.id), next_path=next_path)
        url = provider.authorize_url(
            state=state, intent="connect", redirect_uri=_redirect_uri(provider_name)
        )
        return Response({"url": url})


class OAuthCallbackView(APIView):
    """Exchange code, look up or create user/connection, redirect."""

    permission_classes = [permissions.AllowAny]

    def get(self, request: Request, provider_name: str):
        if provider_name not in supported_provider_names():
            return HttpResponseRedirect(_frontend_failure_url(reason="unknown_provider"))

        error = request.GET.get("error")
        if error:
            # User clicked "deny" on Google/GitHub's consent screen, or
            # the provider returned an error. Surface a clean failure.
            return HttpResponseRedirect(_frontend_failure_url(reason="consent_denied"))

        code = request.GET.get("code")
        state = request.GET.get("state")
        if not code or not state:
            return HttpResponseRedirect(_frontend_failure_url(reason="bad_callback"))

        try:
            state_payload = _verify_state(state)
        except signing.BadSignature:
            return HttpResponseRedirect(_frontend_failure_url(reason="invalid_state"))

        intent: FlowIntent = state_payload["intent"]
        next_path: str = state_payload.get("next") or "/jointeam"

        provider: OAuthProvider = get_provider(provider_name)
        try:
            token_response: TokenResponse = provider.exchange_code(
                code=code, redirect_uri=_redirect_uri(provider_name)
            )
            profile: ProviderProfile = provider.fetch_profile(
                access_token=token_response.access_token
            )
        except Exception as exc:  # noqa: BLE001 — network / provider failures vary
            logger.exception(
                "OAuth %s callback failed during exchange/profile: %s", provider_name, exc
            )
            return HttpResponseRedirect(_frontend_failure_url(reason="provider_error"))

        if intent == "login":
            return self._handle_login(
                provider_name=provider_name,
                profile=profile,
                token_response=token_response,
                next_path=next_path,
            )
        return self._handle_connect(
            provider_name=provider_name,
            profile=profile,
            token_response=token_response,
            initiating_user_id=state_payload.get("user_id"),
            next_path=next_path,
        )

    # ---- helpers ----------------------------------------------------

    @transaction.atomic
    def _handle_login(
        self,
        *,
        provider_name: str,
        profile: ProviderProfile,
        token_response: TokenResponse,
        next_path: str,
    ) -> HttpResponseRedirect:
        existing = (
            ConnectedAccount.objects.select_for_update()
            .filter(provider=provider_name, provider_user_id=profile.provider_user_id)
            .first()
        )

        if existing:
            # Connecting an account for its CALENDAR must not silently
            # turn it into a way to sign in. This lookup is keyed only on
            # the provider identity, so without this gate any account the
            # user attached for API access would authenticate a login —
            # somebody adding their personal Gmail to see one more
            # calendar would be handing that Google account the keys to
            # their work Genos account.
            #
            # Latent before multi-account (a user could only ever hold
            # one Google row, and it was their signup row), reachable the
            # moment a second row can exist.
            if not _can_sign_in_with(existing):
                return HttpResponseRedirect(_frontend_failure_url(reason="not_a_login_account"))
            user = existing.user
            # Deactivated accounts must not sign in. The password path
            # gets this free (`authenticate()` → `user_can_authenticate`),
            # but OAuth mints via `RefreshToken.for_user` and never calls
            # `authenticate()`, so without this check a deactivated or
            # DELETED user completes the whole flow and receives tokens —
            # tokens SimpleJWT then rejects on first use, i.e. a signed-in
            # session that 401s on everything. Refuse at the door instead.
            if not user.is_active:
                return HttpResponseRedirect(_frontend_failure_url(reason="account_inactive"))
            self._save_tokens(existing, token_response)
        else:
            # First time this provider identity has signed in. If our
            # CustomUser table already has a row with the same email,
            # block — the user has another account they should sign
            # into instead.
            if profile.email and User.objects.filter(email__iexact=profile.email).exists():
                other = User.objects.filter(email__iexact=profile.email).first()
                return HttpResponseRedirect(
                    _frontend_failure_url(
                        reason="email_in_use", primary=other.primary_auth_provider
                    )
                )

            email = profile.email or f"{provider_name}_{profile.provider_user_id}@noemail.local"
            display_name = profile.display_name or email.split("@")[0]
            user = User(
                email=email,
                username=display_name,
                primary_auth_provider=provider_name,
                # Provider verified the email before issuing the OAuth
                # token, so the user doesn't need to re-prove ownership.
                is_email_verified=True,
            )
            user.set_unusable_password()
            user.save()
            existing = ConnectedAccount(
                user=user,
                provider=provider_name,
                provider_user_id=profile.provider_user_id,
                provider_email=profile.email,
                scopes=token_response.granted_scopes,
                # This row is what the user signs in with, so it can
                # never be disconnected. Recorded as a flag rather than
                # inferred from `provider == primary_auth_provider`,
                # which stops being a valid test once the user connects
                # a second account for the same provider.
                is_login_identity=True,
            )
            self._save_tokens(existing, token_response, save=False)
            existing.save()

        access_jwt, refresh_jwt = self._issue_jwt(user)
        return self._redirect_with_session(
            access_jwt=access_jwt, refresh_jwt=refresh_jwt, next_path=next_path
        )

    @transaction.atomic
    def _handle_connect(
        self,
        *,
        provider_name: str,
        profile: ProviderProfile,
        token_response: TokenResponse,
        initiating_user_id: Optional[str],
        next_path: str,
    ) -> HttpResponseRedirect:
        if not initiating_user_id:
            return HttpResponseRedirect(_frontend_failure_url(reason="not_authenticated"))
        try:
            user = User.objects.get(id=initiating_user_id)
        except User.DoesNotExist:
            return HttpResponseRedirect(_frontend_failure_url(reason="not_authenticated"))

        # Keyed on the *provider identity*, not on (user, provider) —
        # which is what makes connecting a second Google account work:
        # a different Google identity simply doesn't match here and
        # falls through to the insert branch. Re-running connect with an
        # account the user already holds still lands on the update
        # branch and just refreshes its tokens/scopes.
        existing = (
            ConnectedAccount.objects.select_for_update()
            .filter(provider=provider_name, provider_user_id=profile.provider_user_id)
            .first()
        )
        if existing and existing.user_id != user.id:
            return HttpResponseRedirect(
                _frontend_failure_url(reason="already_connected_to_other_user")
            )

        if existing is not None and existing.user_id == user.id:
            pass
        elif not _provider_allows_additional_account(user, provider_name):
            # Single-account provider (GitHub) that the user has already
            # connected under a different identity. Without this check
            # the insert would trip the DB constraint and surface as a
            # 500 on the callback; this turns it into a clean redirect.
            return HttpResponseRedirect(_frontend_failure_url(reason="provider_already_connected"))

        if existing is None:
            existing = ConnectedAccount(
                user=user,
                provider=provider_name,
                provider_user_id=profile.provider_user_id,
                provider_email=profile.email,
                scopes=token_response.granted_scopes,
            )
            self._save_tokens(existing, token_response, save=False)
            existing.save()
        else:
            existing.provider_email = profile.email or existing.provider_email
            existing.scopes = token_response.granted_scopes or existing.scopes
            self._save_tokens(existing, token_response)

        # User is already signed in; no new JWT needed. Just bounce
        # them back to wherever they came from (typically the
        # integrations page).
        return HttpResponseRedirect(f"{settings.FRONTEND_BASE_URL}{next_path}")

    def _save_tokens(
        self, account: ConnectedAccount, tokens: TokenResponse, *, save: bool = True
    ) -> None:
        account.access_token_encrypted = crypto.encrypt(tokens.access_token)
        if tokens.refresh_token:
            account.refresh_token_encrypted = crypto.encrypt(tokens.refresh_token)
        if tokens.expires_in_seconds is not None:
            account.access_token_expires_at = timezone.now() + timedelta(
                seconds=tokens.expires_in_seconds
            )
        else:
            # Non-expiring (e.g. GitHub OAuth App) — clear any stale
            # expiry from a prior provider rotation.
            account.access_token_expires_at = None
        if save:
            account.save(
                update_fields=[
                    "access_token_encrypted",
                    "refresh_token_encrypted",
                    "access_token_expires_at",
                    "provider_email",
                    "scopes",
                    "ts_updated_at",
                ]
            )

    @staticmethod
    def _issue_jwt(user) -> tuple[str, str]:
        refresh = RefreshToken.for_user(user)
        return str(refresh.access_token), str(refresh)

    @staticmethod
    def _redirect_with_session(
        *, access_jwt: str, refresh_jwt: str, next_path: str
    ) -> HttpResponseRedirect:
        url = _frontend_success_url(access_token=access_jwt, next_path=next_path)
        response = HttpResponseRedirect(url)
        _set_refresh_cookie(response, refresh_jwt)
        return response


# ---- connection management -----------------------------------------


class IntegrationsListView(APIView):
    """Return the signed-in user's ConnectedAccounts (without tokens)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        # Ordered by (provider, then the model's own deterministic
        # ordering) so a user's Google accounts always come back in a
        # stable sequence — the frontend renders them as a picker and a
        # list that reshuffles between loads is unusable.
        accounts = ConnectedAccount.objects.filter(user=request.user).order_by(
            "provider", "-is_login_identity", "ts_created_at", "id"
        )
        return Response(
            {
                "primary_auth_provider": request.user.primary_auth_provider,
                "connections": [
                    {
                        # `id` is what every per-account operation keys
                        # on (calendar reads/writes, disconnect). Without
                        # it a client holding two Google accounts has no
                        # way to address either one.
                        "id": str(a.id),
                        "provider": a.provider,
                        "provider_email": a.provider_email,
                        "label": a.label,
                        "scopes": a.scopes,
                        "connected_at": a.ts_created_at,
                        # Read from the flag, not from a provider-string
                        # comparison: with two Google rows the old
                        # comparison marked both as primary and made
                        # both undeletable.
                        "is_primary": a.is_login_identity,
                    }
                    for a in accounts
                ],
            }
        )


class IntegrationsDisconnectView(APIView):
    """Delete a ConnectedAccount by provider.

    Legacy route, kept so older clients keep working. It refuses when
    the user holds more than one account for the provider: this endpoint
    can't express *which* one to drop, and deleting all of them because
    the caller couldn't say otherwise would be a silent data-loss bug.
    Those callers get pointed at the by-id route below.
    """

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request: Request, provider_name: str):
        if provider_name not in supported_provider_names():
            return Response(
                {"detail": f"Unknown provider: {provider_name}"},
                status=status.HTTP_404_NOT_FOUND,
            )
        accounts = list(ConnectedAccount.objects.filter(user=request.user, provider=provider_name))
        if not accounts:
            return Response({"detail": "Not connected."}, status=status.HTTP_404_NOT_FOUND)
        if len(accounts) > 1:
            return Response(
                {
                    "detail": "multiple_accounts_connected",
                    "message": (
                        "Several accounts are connected for this provider. "
                        "Disconnect a specific one by id instead."
                    ),
                },
                status=status.HTTP_409_CONFLICT,
            )
        return _disconnect_account(accounts[0])


class IntegrationsDisconnectAccountView(APIView):
    """DELETE /api/v2/integrations/account/<account_id>/ — drop one
    specific connected account. This is the route the UI uses; it's the
    only one that can express "disconnect my personal Google, keep my
    work Google"."""

    permission_classes = [permissions.IsAuthenticated]

    def delete(self, request: Request, account_id: str):
        # Scoped to the requesting user: an account id belonging to
        # somebody else must look identical to one that doesn't exist.
        account = ConnectedAccount.objects.filter(user=request.user, id=account_id).first()
        if account is None:
            return Response({"detail": "Not connected."}, status=status.HTTP_404_NOT_FOUND)
        return _disconnect_account(account)


def _can_sign_in_with(account: ConnectedAccount) -> bool:
    """Whether this row may authenticate a LOGIN.

    True only for the row the user established as their sign-in route —
    their signup row. An account attached later for API access (a second
    Google account added to see its calendar) is not a credential and
    must never be accepted here.

    `_is_last_signin_route` is the fallback for the same reason it guards
    deletion: if `is_login_identity` were ever wrong, a provider-signup
    user has an unusable password and would be locked out with no way
    back in. It only widens the gate for a user whose *only* row for that
    provider is their signup row, so a second, calendar-only account
    still can't slip through.
    """
    return account.is_login_identity or _is_last_signin_route(account)


def _is_last_signin_route(account: ConnectedAccount) -> bool:
    """True if deleting this row would leave the user unable to sign in.

    Belt-and-braces alongside the `is_login_identity` flag. That flag is
    populated by a data migration; had the backfill ever failed to match,
    an OAuth-signup user's row would look ordinary — and therefore
    deletable — while being the only credential they have, since signup
    calls `set_unusable_password()`. The result is an unrecoverable
    lockout, which is worth deriving the same fact a second, independent
    way to prevent.
    """
    if account.provider != account.user.primary_auth_provider:
        return False
    return (
        not ConnectedAccount.objects.filter(user=account.user, provider=account.provider)
        .exclude(pk=account.pk)
        .exists()
    )


def _disconnect_account(account: ConnectedAccount) -> Response:
    """Shared delete + login-identity guard for both disconnect routes."""
    if account.is_login_identity or _is_last_signin_route(account):
        return Response(
            {
                "detail": (
                    "Cannot disconnect the account you signed up with. Delete the account instead."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    account.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)
