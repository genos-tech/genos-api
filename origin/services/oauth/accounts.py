"""Resolving *which* ConnectedAccount a request should act on.

Before multi-account support every call site could write
`ConnectedAccount.objects.filter(user=u, provider="google").first()` and
be certain of the answer — a unique constraint guaranteed at most one
row. That constraint is gone for Google (see `MULTI_ACCOUNT_PROVIDERS`),
so a bare `.first()` on an unordered queryset now picks an arbitrary row:
Postgres makes no promise about row order without an ORDER BY, and the
winner can change between two identical queries. Silent, and the damage
is real — task auto-sync would PATCH an event id against the wrong
account, get a 404, and clear the task's link permanently.

Every resolution therefore goes through here:

  - `accounts_for` — all of a user's accounts for a provider, in the
    model's deterministic order.
  - `default_account_for` — the one to use when the caller has no
    opinion (background jobs, agent tools, legacy single-account
    clients). Stable across calls: login identity first, else oldest.
  - `resolve_account_for` — the one the caller explicitly asked for by
    id, scoped to the user so an id from another account 404s rather
    than leaking someone else's calendar.
"""

from __future__ import annotations

from typing import Optional

from origin.models.common.user_models import ConnectedAccount


def accounts_for(user, provider: str):
    """All the user's accounts for `provider`, deterministically ordered.

    Ordering comes from `ConnectedAccount.Meta.ordering`; it is stated
    explicitly here too so a future edit to Meta can't silently make
    every caller nondeterministic again.
    """
    if user is None:
        return ConnectedAccount.objects.none()
    return ConnectedAccount.objects.filter(user=user, provider=provider).order_by(
        "-is_login_identity", "ts_created_at", "id"
    )


def default_account_for(user, provider: str) -> Optional[ConnectedAccount]:
    """The account to act on when the caller didn't name one.

    This is the back-compat path: for a user with exactly one account it
    returns the same row the old `.first()` did, so nothing about
    single-account behaviour changes. For a user with several it returns
    their login identity (or their oldest connection), which is the one
    that already owns any pre-existing task↔event links.
    """
    return accounts_for(user, provider).first()


def resolve_account_for(user, provider: str, account_id: Optional[str]):
    """Resolve an explicitly-requested account id, or fall back to the
    default when `account_id` is empty.

    Returns `(account, error_code)` where exactly one side is set:
      - `("<account>", None)` on success
      - `(None, "not_connected")`      — user has no account for provider
      - `(None, "account_not_found")`  — the id isn't one of this user's
        accounts. Deliberately not distinguished from "id doesn't exist
        at all": both mean "not yours", and telling the caller which is
        which would confirm the existence of another user's connection.

    Filtering by `user=` (not just by id) is the whole authorization
    check for the calendar endpoints — an attacker who learns an account
    UUID must not be able to read its calendar.
    """
    if not account_id:
        account = default_account_for(user, provider)
        return (account, None) if account is not None else (None, "not_connected")

    account = accounts_for(user, provider).filter(id=account_id).first()
    if account is not None:
        return account, None
    # Distinguish "you have nothing connected" from "that id isn't
    # yours" so the frontend can show a connect prompt in the first
    # case rather than a confusing not-found on a stale saved id.
    if not accounts_for(user, provider).exists():
        return None, "not_connected"
    return None, "account_not_found"
