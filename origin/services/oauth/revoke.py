"""Revoke the OAuth grant at Google / GitHub when a connection is removed (MIP-1790).

Deleting a `ConnectedAccount` only forgets OUR encrypted copy of the token. The grant itself stays
alive at the provider (the GitHub one with the `repo` scope, private repositories included), so a
leaked copy of an already-deleted token would still work. This module asks the provider to kill it.

Behind `OAUTH_REVOKE_ON_DISCONNECT`, default OFF (read on every call, so flipping it needs no
restart; not in `apis/settings.py`, a change to that file starts the billed `agent-evals`). With it
off nothing is read, decrypted or sent: the disconnect and the account deletion behave exactly as
before.

Shape, and why:

  1. `capture(accounts)`: BEFORE the stored row is deleted, decrypt the token(s) into plain
     `Grant` values. After the delete there is nothing left to read them from.
  2. `revoke_after_commit(grants)`: schedule the calls with `transaction.on_commit`. The disconnect
     and `delete_account` can roll back; a grant revoked for an account that is still there would
     silently break a live Google/GitHub connection. Outside a transaction (the disconnect views run
     in autocommit) `on_commit` runs at once, which is why the views call it AFTER `.delete()`.
  3. The calls run in a daemon thread (`_spawn`): the user's request never waits for Google or
     GitHub. The thread touches no database and no request state, only HTTP and logging.

Best effort, by design: a 3 second timeout, no retry, `allow_redirects=False` (the GitHub call
carries the app's client secret as basic auth), every exception caught. A token the provider no
longer knows (HTTP 400 / 404) is a success: it is what we wanted. The log carries the provider and
the outcome (and the HTTP status or exception CLASS), never a token, the client secret, the
provider's answer or an exception message (a library message can quote the request).
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import quote

import requests
from django.conf import settings
from django.db import transaction

from origin.services import crypto

log = logging.getLogger(__name__)

#: The switch. Read straight from the environment on every call, like the other default-off switches.
FLAG_ENV = "OAUTH_REVOKE_ON_DISCONNECT"
_TRUE = frozenset({"1", "true", "yes", "on"})

TIMEOUT_SECONDS = 3
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GITHUB_GRANT_URL = "https://api.github.com/applications/{client_id}/grant"

#: Answers that mean "the provider no longer has this token": the goal is met.
_ALREADY_INVALID = frozenset({400, 404})


def revoke_enabled() -> bool:
    """Is the switch on right now? Off unless the env var says so."""
    return os.environ.get(FLAG_ENV, "").strip().lower() in _TRUE


@dataclass(frozen=True)
class Grant:
    """What is needed to revoke one connection, copied out before its row is deleted."""

    provider: str
    access_token: str = field(repr=False)
    refresh_token: str | None = field(default=None, repr=False)


def capture(accounts: Iterable) -> list[Grant]:
    """Decrypt the tokens of `accounts` for a later revoke. `[]` when the switch is off.

    Never raises: a token that cannot be read is skipped (logged by provider only). `accounts` may
    be a lazy queryset; it is not evaluated when the switch is off.
    """
    if not revoke_enabled():
        return []
    grants: list[Grant] = []
    try:
        for account in accounts:
            try:
                access = crypto.decrypt(account.access_token_encrypted)
                refresh = (
                    crypto.decrypt(account.refresh_token_encrypted)
                    if account.refresh_token_encrypted
                    else None
                )
            except Exception as exc:  # noqa: BLE001 - best effort, must not reach the caller
                log.warning(
                    "[oauth-revoke] provider=%s outcome=unreadable error=%s",
                    account.provider,
                    type(exc).__name__,
                )
                continue
            grants.append(
                Grant(provider=account.provider, access_token=access, refresh_token=refresh)
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("[oauth-revoke] outcome=capture_failed error=%s", type(exc).__name__)
    return grants


def revoke_after_commit(grants: list[Grant]) -> None:
    """Revoke `grants` in the background once the surrounding transaction commits.

    A rollback drops the callback: the account is still there, so its connection must keep working.
    Never raises.
    """
    if not grants:
        return
    try:
        transaction.on_commit(lambda: _spawn(lambda: _revoke_all(grants)), robust=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("[oauth-revoke] outcome=schedule_failed error=%s", type(exc).__name__)


def _spawn(fn: Callable[[], None]) -> None:
    """Run `fn` off the request thread. Replaced by an inline runner in tests."""
    threading.Thread(target=fn, daemon=True, name="oauth-revoke").start()


def _revoke_all(grants: list[Grant]) -> None:
    for grant in grants:
        revoke_grant(grant)


def revoke_grant(grant: Grant) -> str:
    """Ask the provider to revoke one grant. Returns the outcome word; never raises."""
    try:
        outcome, detail = _call(grant)
    except Exception as exc:  # noqa: BLE001 - timeout, DNS, TLS, a bug: all the same to the caller
        outcome, detail = "failed", f"error={type(exc).__name__}"
    line = "[oauth-revoke] provider=%s outcome=%s %s"
    if outcome in ("revoked", "already_invalid"):
        log.info(line, grant.provider, outcome, detail)
    else:
        log.warning(line, grant.provider, outcome, detail)
    return outcome


def _call(grant: Grant) -> tuple[str, str]:
    if grant.provider == "google":
        # The refresh token revokes the whole grant, access tokens included; an access token alone
        # (a row without a refresh token) revokes just itself.
        token = grant.refresh_token or grant.access_token
        resp = requests.post(
            GOOGLE_REVOKE_URL,
            data={"token": token},
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    elif grant.provider == "github":
        client_id = settings.GITHUB_OAUTH_CLIENT_ID
        client_secret = settings.GITHUB_OAUTH_CLIENT_SECRET
        if not (client_id and client_secret):
            return "not_configured", ""
        # Deleting the grant (not just the token) also removes the app from the person's
        # "Authorized OAuth Apps". `safe=""` keeps a client id from changing the path.
        resp = requests.delete(
            GITHUB_GRANT_URL.format(client_id=quote(client_id, safe="")),
            auth=(client_id, client_secret),
            json={"access_token": grant.access_token},
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=TIMEOUT_SECONDS,
            allow_redirects=False,
        )
    else:
        return "unsupported", ""
    status = resp.status_code
    if status in _ALREADY_INVALID:
        return "already_invalid", f"status={status}"
    if 200 <= status < 300:
        return "revoked", f"status={status}"
    return "failed", f"status={status}"
