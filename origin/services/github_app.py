"""GitHub App client: the App JWT and installation access tokens (MIP-2086, design MIP-2057).

A GitHub App authenticates three ways; this module is the first two:

  1. **As the App**: an RS256 JWT signed with the App's private key (`GITHUB_APP_PRIVATE_KEY`),
     10 minutes at most. Minted per use, never stored. It only talks to `/app/...` endpoints.
  2. **As an installation**: a token GitHub hands out in exchange for that JWT, valid 1 hour,
     narrowable to one repository and a subset of the App's permissions. This is what background
     work in a linked team uses (never a person's request: that uses the person's user token, the
     `github_app` provider in `oauth/`).
  3. As a person (user-to-server): `origin/services/oauth/github_app.py` and `oauth/tokens.py`.

Security rules this module keeps (genos-security, design section 8):

  * **An installation token is never persisted and never returned.** It lives only in the cache,
    encrypted with the `crypto` keyring, keyed by installation, repositories and permission set,
    with a TTL shorter than its life. No model has a column for it; no view or tool may return
    it, and none must put it in a log line, an exception message or an LLM prompt.
  * **Read-only, least privilege.** `ALLOWED_PERMISSIONS` is the App's read set; a request for
    anything else (a write level, another permission) raises before any call is made, so a bug
    elsewhere cannot widen what a minted token can do.
  * **Known, active installations only.** A token is minted only for an installation Genos has a
    row for and that is not suspended. The row, not a webhook payload or a URL, is the tenant
    anchor; an `installation_id` from outside is never trusted by itself.
  * **Off means off.** With `GITHUB_APP_ENABLED` false every entry point raises
    `GithubAppDisabled` before touching the key or the network.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

import jwt
import requests
from django.conf import settings
from django.core.cache import cache

from origin.models.common.github_app_models import GithubAppInstallation
from origin.services import crypto

log = logging.getLogger(__name__)

API_ROOT = "https://api.github.com"
API_VERSION = "2022-11-28"
TIMEOUT_SECONDS = 10

#: The App's permissions, all read (design section 2). No Contents, no write of any kind.
ALLOWED_PERMISSIONS: Mapping[str, str] = {
    "metadata": "read",
    "pull_requests": "read",
    "checks": "read",
    "statuses": "read",
    "issues": "read",
}

#: What the App registration on GitHub must grant, beyond the installation-token read set above:
#: "Members: read" is the org-membership check on the person's own user token (MIP-2169).
REGISTRATION_PERMISSIONS: Mapping[str, str] = {**ALLOWED_PERMISSIONS, "members": "read"}

#: The webhook events the registration must subscribe to. GitHub only sends an event the App
#: subscribed to (Settings -> Permissions & events), and nothing in a delivery says one is
#: missing, so an unticked box is silent: on 2026-11-23 "Pull request" was not ticked, only
#: check_run / status arrived, and no PR was ever linked. `origin.services.github_app_webhook`
#: handles exactly these (a test pins the two lists together); `check_github_app_registration`
#: compares this set with what GitHub reports for the App.
REQUIRED_EVENTS: frozenset[str] = frozenset(
    {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_run",
        "status",
    }
)

_LEVELS = {"read": 1, "write": 2, "admin": 3}

#: The JWT lives 9 minutes (GitHub's maximum is 10) and is dated 60 s in the past to absorb
#: clock drift, as GitHub's docs advise.
_JWT_LIFETIME_SECONDS = 540
_JWT_BACKDATE_SECONDS = 60

#: A cached installation token is dropped this long before it expires, so a request that starts
#: with it finishes before GitHub stops honouring it.
CACHE_SAFETY_SECONDS = 300

_CACHE_PREFIX = "ghapp:itok:"


class GithubAppError(Exception):
    """A GitHub App call failed. The message carries the HTTP status or a fixed reason, never a
    token, the key, or GitHub's answer."""


class GithubAppDisabled(GithubAppError):
    """`GITHUB_APP_ENABLED` is off."""


class InstallationUnavailable(GithubAppError):
    """No token can be minted: the installation is unknown to Genos, suspended, deleted at GitHub,
    or its suspension is reported by GitHub."""


class PermissionNotAllowed(GithubAppError, ValueError):
    """The caller asked for a permission the App never uses, or at a level above read."""


def is_enabled() -> bool:
    return bool(settings.GITHUB_APP_ENABLED)


def _require_enabled() -> None:
    if not is_enabled():
        raise GithubAppDisabled("GitHub App is not enabled.")


def app_jwt(*, now: float | None = None) -> str:
    """The App's JWT (RS256): `iss` is the App's client id, `iat` 60 s back, `exp` in 9 minutes.

    Minted on every use and never stored or logged.
    """
    _require_enabled()
    return _sign_jwt(now)


def _sign_jwt(now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    claims = {
        "iat": issued - _JWT_BACKDATE_SECONDS,
        "exp": issued + _JWT_LIFETIME_SECONDS,
        "iss": settings.GITHUB_APP_CLIENT_ID,
    }
    return jwt.encode(claims, settings.GITHUB_APP_PRIVATE_KEY, algorithm="RS256")


def _checked_permissions(permissions: Mapping[str, str] | None) -> dict[str, str]:
    """The permissions to mint with: the App's whole read set by default, else the requested
    subset, every entry of which must be one the App uses at `read`."""
    if permissions is None:
        return dict(ALLOWED_PERMISSIONS)
    if not permissions:
        raise PermissionNotAllowed("An empty permission set would mint a token with every one.")
    checked: dict[str, str] = {}
    for name, level in permissions.items():
        if ALLOWED_PERMISSIONS.get(name) != level:
            raise PermissionNotAllowed(f"Permission {name!r}:{level!r} is not allowed.")
        checked[name] = level
    return checked


def _cache_key(installation_id: int, repositories: tuple[str, ...], permissions: dict) -> str:
    """Per installation, repository set and permission set: a token narrowed to one repo is never
    served for another. Hashed, so a repo name cannot shape the key."""
    scope = json.dumps(
        [int(installation_id), list(repositories), sorted(permissions.items())],
        separators=(",", ":"),
    )
    return _CACHE_PREFIX + hashlib.sha256(scope.encode()).hexdigest()


def _read_cached(key: str) -> str | None:
    sealed = cache.get(key)
    if not sealed:
        return None
    try:
        return crypto.decrypt(sealed)
    except Exception:  # noqa: BLE001 - a key rotated out, a corrupt value: treat as a miss
        return None


def _parse_expiry(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(frozen=True)
class _Minted:
    token: str
    expires_at: datetime | None


def _mint(installation_id: int, repositories: tuple[str, ...], permissions: dict) -> _Minted:
    body: dict = {"permissions": permissions}
    if repositories:
        body["repositories"] = list(repositories)
    try:
        resp = requests.post(
            f"{API_ROOT}/app/installations/{int(installation_id)}/access_tokens",
            json=body,
            headers={
                "Authorization": f"Bearer {app_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        # The class only: a library message can quote the request.
        raise GithubAppError(f"installation token request failed: {type(exc).__name__}") from None
    if resp.status_code in (403, 404):
        # 404: the installation no longer exists (uninstalled). 403: suspended, or the App was
        # blocked. Either way no token: the caller treats the installation as unusable.
        raise InstallationUnavailable(
            f"installation {int(installation_id)} (HTTP {resp.status_code})"
        )
    if resp.status_code != 201:
        raise GithubAppError(f"installation token request returned HTTP {resp.status_code}")
    try:
        data = resp.json()
        token = data["token"]
    except (ValueError, KeyError, TypeError):
        raise GithubAppError("installation token response had no token") from None
    if not isinstance(token, str) or not token:
        raise GithubAppError("installation token response had no token")
    # Do not assume a length or shape: GitHub says the format may change.
    return _Minted(token=token, expires_at=_parse_expiry(data.get("expires_at")))


def get_installation_token(
    installation_id: int,
    *,
    repositories: Iterable[str] | None = None,
    permissions: Mapping[str, str] | None = None,
) -> str:
    """A plaintext installation access token, narrowed as asked, from the cache or freshly minted.

    `repositories` are repository NAMES (no owner) the token may reach; empty or None means every
    repository the installation covers, so a caller that works on one PR should pass its repo.
    `permissions` defaults to the App's read set and can only be a subset of it.

    The caller must use the token in an `Authorization` header at once and drop it: never store,
    return, log or put it in a prompt.

    Raises `GithubAppDisabled`, `InstallationUnavailable` (unknown, suspended or gone),
    `PermissionNotAllowed`, or `GithubAppError`.
    """
    _require_enabled()
    perms = _checked_permissions(permissions)
    repos = tuple(sorted({str(r) for r in (repositories or ())}))

    installation = GithubAppInstallation.objects.filter(installation_id=installation_id).first()
    if installation is None or not installation.is_active:
        raise InstallationUnavailable(f"installation {int(installation_id)} (unknown or suspended)")

    key = _cache_key(installation_id, repos, perms)
    cached = _read_cached(key)
    if cached:
        return cached

    minted = _mint(installation_id, repos, perms)
    if minted.expires_at is not None:
        ttl = int(
            (minted.expires_at - datetime.now(timezone.utc)).total_seconds() - CACHE_SAFETY_SECONDS
        )
        if ttl > 0:
            cache.set(key, crypto.encrypt(minted.token), timeout=ttl)
    return minted.token


def forget_installation_token(
    installation_id: int,
    *,
    repositories: Iterable[str] | None = None,
    permissions: Mapping[str, str] | None = None,
) -> None:
    """Drop one cached token, for the caller that got a 401 with it (revoked early)."""
    key = _cache_key(
        installation_id,
        tuple(sorted({str(r) for r in (repositories or ())})),
        _checked_permissions(permissions),
    )
    cache.delete(key)


# ---- the App's own registration ---------------------------------------------------------------


def fetch_app_registration() -> dict:
    """What GitHub says this App is registered for: `{"events": [...], "permissions": {...}}`.

    One read-only `GET /app` as the App (the JWT). Unlike every other entry point it works with
    `GITHUB_APP_ENABLED` off, because its job is to check a registration BEFORE the switch goes on;
    it reads, and writes nothing anywhere. Only the two lists are returned, never the rest of
    GitHub's answer. Raises `GithubAppError` (status code or fixed reason only) on any failure."""
    try:
        resp = requests.get(
            f"{API_ROOT}/app",
            headers={
                "Authorization": f"Bearer {_sign_jwt()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise GithubAppError(f"GET /app failed ({type(exc).__name__})") from None
    except Exception as exc:  # noqa: BLE001 - a missing / malformed private key, nothing more said
        raise GithubAppError(f"could not sign the App JWT ({type(exc).__name__})") from None
    if resp.status_code != 200:
        raise GithubAppError(f"GET /app answered {resp.status_code}")
    try:
        data = resp.json()
    except ValueError:
        raise GithubAppError("GET /app returned no JSON") from None
    if not isinstance(data, dict):
        raise GithubAppError("GET /app returned an unexpected shape")
    events = data.get("events")
    permissions = data.get("permissions")
    return {
        "events": sorted(e for e in events if isinstance(e, str))
        if isinstance(events, list)
        else [],
        "permissions": {str(k): str(v) for k, v in permissions.items() if isinstance(v, str)}
        if isinstance(permissions, dict)
        else {},
    }


def registration_problems(registration: Mapping) -> dict:
    """Compare a registration (`fetch_app_registration`) with what Genos needs.

    `missing_events` and `missing_permissions` break the integration (an unsubscribed event never
    arrives; a permission below the needed level makes a call fail). `extra_permissions` are
    permissions beyond the design (no Contents, no write: design section 2), reported for a human to
    look at. Extra events are harmless (answered 200 and ignored) and not listed."""
    events = set(registration.get("events") or ())
    granted = registration.get("permissions") or {}
    missing_permissions: dict[str, str] = {}
    for name, needed in REGISTRATION_PERMISSIONS.items():
        if _LEVELS.get(granted.get(name), 0) < _LEVELS[needed]:
            missing_permissions[name] = needed
    extra_permissions: dict[str, str] = {}
    for name, level in granted.items():
        needed = REGISTRATION_PERMISSIONS.get(name)
        if needed is None or _LEVELS.get(level, 0) > _LEVELS[needed]:
            extra_permissions[name] = level
    return {
        "missing_events": sorted(REQUIRED_EVENTS - events),
        "missing_permissions": dict(sorted(missing_permissions.items())),
        "extra_permissions": dict(sorted(extra_permissions.items())),
    }
