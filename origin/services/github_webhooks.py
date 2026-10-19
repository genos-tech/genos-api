"""Auto-register GitHub repo webhooks on behalf of users.

The task-save handler calls `ensure_webhooks_for_links` after a successful
save. For every distinct (owner, repo) referenced by a PR URL in the
task's `links`, we look up whether we've already registered our PR
webhook on that repo. If not, we use the saving user's stored GitHub
OAuth token (already has `repo` scope via the "Connect GitHub" flow) to
call GitHub's `POST /repos/{owner}/{repo}/hooks`.

Best-effort throughout: every failure path returns None / swallows the
exception. Auto-status-sync is a "nice to have" — if a user lacks repo
admin, we fall back to the manual webhook URL the operator can paste in
the integrations page.

Secrets (MIP-618): each hook is signed with its own random secret, stored
encrypted on its `GithubWebhookRegistration`, so one leaking forges
events for one repo. A hook from before that (or adopted from a hand-made
one) signs with the shared GITHUB_WEBHOOK_SECRET until `rekey_hook`
gives it its own; `ensure_repo_webhook` does that on the next save that
links the repo, and `manage.py rekey_github_webhooks` does it for all.
"""

from __future__ import annotations

import logging
import re
import secrets
from typing import Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError
from django.db.models import Q

from apis.request_id import describe_upstream
from origin.models.common.user_models import (
    ConnectedAccount,
    GithubWebhookRegistration,
)
from origin.services import crypto
from origin.services.oauth.tokens import get_valid_access_token

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# Same shape the frontend's parsePrUrl uses — keep them in sync.
_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")

# Canonical event list our webhook handler dispatches on. The backend's
# `GithubWebhookView` knows how to deal with each of these:
#   - `pull_request`          → merge → auto-close, also drives PR state
#                                detection used elsewhere.
#   - `issue_comment`         → top-level PR comments (the payload's
#                                `issue.pull_request` distinguishes a PR
#                                comment from a plain issue comment).
#   - `pull_request_review_comment` → inline code-review comments.
#
# When this list changes, bump `_HOOK_EVENTS_VERSION` below so existing
# registrations get re-PATCHed on the next task save.
_HOOK_EVENTS = ["pull_request", "issue_comment", "pull_request_review_comment"]
_HOOK_EVENTS_VERSION = 2  # v1 was [pull_request] only.


def parse_pr_url(url) -> Optional[tuple[str, str]]:
    """Return (owner, repo) for a valid PR URL, None otherwise."""
    if not isinstance(url, str):
        return None
    m = _PR_URL_RE.match(url)
    return (m.group(1), m.group(2)) if m else None


def parse_pr_url_full(url) -> Optional[tuple[str, str, int]]:
    """Return (owner, repo, number) for a valid PR URL, None otherwise.
    Used when the PR number is needed in addition to (owner, repo) —
    e.g. to fetch the PR's detail by id without scraping it back out
    of the URL elsewhere."""
    if not isinstance(url, str):
        return None
    m = _PR_URL_RE.match(url)
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _webhook_payload_url() -> str:
    base = (settings.BACKEND_BASE_URL or "").rstrip("/")
    return f"{base}/api/v2/github/webhook/"


def _hooks_api_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _new_secret() -> str:
    return secrets.token_urlsafe(32)


def hook_admin_headers(user, registration) -> Optional[dict]:
    """API headers to change `registration`'s hook with, or None.

    The current user's GitHub connection first (matches the registration
    path's convention), then the registration's original `registered_by`
    user's, for when the current user has no GitHub link. `user` may be
    None (a management command): then only the latter.
    """
    account = None
    if user is not None:
        account = ConnectedAccount.objects.filter(user=user, provider="github").first()
    if account is None and registration.registered_by_id is not None:
        account = ConnectedAccount.objects.filter(
            user_id=registration.registered_by_id, provider="github"
        ).first()
    if account is None:
        return None
    try:
        return _hooks_api_headers(get_valid_access_token(account))
    except Exception:
        logger.exception(
            "Could not get valid GitHub token for the hook on %s/%s",
            registration.owner,
            registration.repo,
        )
        return None


def rekey_hook(registration: GithubWebhookRegistration, headers: dict) -> bool:
    """Give a hook that still signs with the shared secret its own.

    The order keeps every delivery verifiable. The secret is stored
    first, while the shared one is still accepted, so the receiver takes
    either during the switch; GitHub is told; only then does the shared
    one stop working for this repo. A failure anywhere leaves both
    accepted and the next call retries with the same stored secret.

    The store is a compare-and-swap on "no secret yet", so two concurrent
    calls send GitHub the same value and can't leave it holding one the
    row doesn't.
    """
    GithubWebhookRegistration.objects.filter(
        Q(secret__isnull=True) | Q(secret=""), pk=registration.pk
    ).update(secret=_new_secret())
    secret = GithubWebhookRegistration.objects.values_list("secret", flat=True).get(
        pk=registration.pk
    )
    owner, repo = registration.owner, registration.repo
    try:
        resp = requests.patch(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/hooks/{registration.hook_id}/config",
            json={"secret": secret},
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("GitHub hook re-key network error on %s/%s: %s", owner, repo, exc)
        return False
    if not resp.ok:
        logger.info(
            "ensure_repo_webhook: hook re-key %d on %s/%s — still on the shared secret",
            resp.status_code,
            owner,
            repo,
        )
        return False
    GithubWebhookRegistration.objects.filter(pk=registration.pk).update(accepts_shared_secret=False)
    registration.secret = secret
    registration.accepts_shared_secret = False
    logger.info("ensure_repo_webhook: %s/%s now signs with its own secret", owner, repo)
    return True


def _delete_hook(headers: dict, owner: str, repo: str, hook_id: int) -> None:
    """Best-effort: remove a hook we created but have no row for."""
    try:
        requests.delete(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/hooks/{hook_id}",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("GitHub hook delete network error on %s/%s: %s", owner, repo, exc)


def ensure_repo_webhook(user, owner: str, repo: str) -> Optional[GithubWebhookRegistration]:
    """Make sure our PR webhook is registered on `owner/repo`, signing
    with a secret of its own.

    Returns the registration row on success, None on any failure mode
    (not connected, no token, no admin, 4xx, network error, no
    encryption key). Never raises — this runs on the task-save hot path.

    Logs at every silent early-return path so prod debugging can answer
    "why didn't this register?" by grepping for `ensure_repo_webhook`.
    """
    try:
        crypto.require_keyring()
    except (crypto.KeyringMissing, ValueError):
        # The hook's secret is stored encrypted. Creating the hook first
        # would leave one on GitHub that nothing here can verify.
        logger.warning(
            "ensure_repo_webhook: FIELD_ENCRYPTION_KEYS is unset or invalid; skipping %s/%s",
            owner,
            repo,
        )
        return None

    existing = GithubWebhookRegistration.objects.filter(
        owner__iexact=owner, repo__iexact=repo
    ).first()
    if existing:
        # Sync the events list on the GitHub side in case this hook was
        # registered before we added the new event subscriptions (e.g.
        # PR comment events). No-op when already in sync — cheap, cached.
        _sync_existing_hook_events_if_needed(user, owner, repo, existing.hook_id)
        if existing.accepts_shared_secret:
            headers = hook_admin_headers(user, existing)
            if headers is not None:
                rekey_hook(existing, headers)
        logger.info(
            "ensure_repo_webhook: %s/%s already cached (hook_id=%s); short-circuit",
            owner,
            repo,
            existing.hook_id,
        )
        return existing

    account = ConnectedAccount.objects.filter(user=user, provider="github").first()
    if account is None:
        logger.info(
            "ensure_repo_webhook: user %s has no GitHub ConnectedAccount; skipping %s/%s",
            user.id,
            owner,
            repo,
        )
        return None  # User hasn't connected GitHub at all.

    try:
        token = get_valid_access_token(account)
    except Exception:
        logger.exception("Could not get valid GitHub token for user %s", user.id)
        return None

    logger.info("ensure_repo_webhook: posting hook to %s/%s (user=%s)", owner, repo, user.id)
    payload_url = _webhook_payload_url()
    secret = _new_secret()
    body = {
        "name": "web",
        "active": True,
        "events": list(_HOOK_EVENTS),
        "config": {
            "url": payload_url,
            "content_type": "json",
            "secret": secret,
            "insecure_ssl": "0",
        },
    }
    headers = _hooks_api_headers(token)

    try:
        resp = requests.post(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/hooks",
            json=body,
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("GitHub hook create network error on %s/%s: %s", owner, repo, exc)
        return None

    if resp.status_code == 201:
        try:
            hook_id = int(resp.json().get("id"))
        except (ValueError, TypeError, AttributeError):
            logger.warning(
                "GitHub hook create returned 201 but unparseable id on %s/%s", owner, repo
            )
            return None
        try:
            return GithubWebhookRegistration.objects.create(
                owner=owner,
                repo=repo,
                hook_id=hook_id,
                registered_by=user,
                secret=secret,
                accepts_shared_secret=False,
            )
        except IntegrityError:
            # Lost a race — another concurrent task save just created
            # the row. Return whatever's there now.
            winner = GithubWebhookRegistration.objects.filter(
                owner__iexact=owner, repo__iexact=repo
            ).first()
            if winner is not None and winner.hook_id != hook_id:
                # Our hook signs with a secret no row holds, so every
                # delivery it makes would be refused; don't leave it.
                _delete_hook(headers, owner, repo, hook_id)
            return winner

    if resp.status_code == 422:
        # 422 = unprocessable. Most common cause: a hook with our
        # payload URL already exists on this repo (e.g. left over from
        # a prior install). Look it up so we don't fight the API.
        return _adopt_existing_hook(user, owner, repo, headers, payload_url)

    if resp.status_code in (401, 403, 404):
        # 401/403: user lacks admin on this repo (token doesn't grant
        # admin scope for that org/repo). 404: user can't see this repo
        # at all. Either way, can't register; fall back to manual setup.
        logger.info(
            "GitHub hook create %d on %s/%s — user %s lacks admin",
            resp.status_code,
            owner,
            repo,
            user.id,
        )
        return None

    logger.warning(
        "GitHub hook create unexpected on %s/%s: %s", owner, repo, describe_upstream(resp)
    )
    return None


def _events_synced_cache_key(owner: str, repo: str) -> str:
    return f"gh:hook_events_synced:{owner.lower()}:{repo.lower()}:v{_HOOK_EVENTS_VERSION}"


def _sync_existing_hook_events_if_needed(user, owner: str, repo: str, hook_id: int) -> None:
    """Best-effort: PATCH the existing webhook's events list to match our
    current `_HOOK_EVENTS`.

    Why: a `GithubWebhookRegistration` row registered before we added the
    `issue_comment` / `pull_request_review_comment` subscriptions will
    still only receive `pull_request` deliveries on GitHub's side — the
    DB row doesn't know that. Without this sync, PR-comment activity
    rows never get created because GitHub never delivers the events.

    Cached via Redis (keyed by repo + a hand-bumped events version) so
    we don't PATCH on every task save. Bump `_HOOK_EVENTS_VERSION` when
    you change `_HOOK_EVENTS` so existing repos re-sync on the next
    save. Never raises — silent best-effort, just like
    `ensure_repo_webhook` itself.
    """
    cache_key = _events_synced_cache_key(owner, repo)
    if cache.get(cache_key):
        return

    reg = GithubWebhookRegistration.objects.filter(owner__iexact=owner, repo__iexact=repo).first()
    if reg is None:
        return
    headers = hook_admin_headers(user, reg)
    if headers is None:
        return
    try:
        resp = requests.patch(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/hooks/{hook_id}",
            json={"events": list(_HOOK_EVENTS)},
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("GitHub hook PATCH network error on %s/%s: %s", owner, repo, exc)
        return

    if resp.ok:
        logger.info(
            "ensure_repo_webhook: synced events on %s/%s (hook_id=%s) → %s",
            owner,
            repo,
            hook_id,
            _HOOK_EVENTS,
        )
        # Long TTL — the events list is stable across the lifetime of
        # the deploy. Restart / version bump invalidates via the
        # version-suffixed key.
        cache.set(cache_key, "1", 60 * 60 * 24 * 30)  # 30 days
    elif resp.status_code in (401, 403, 404):
        # User lacks admin on this repo, or hook was deleted on GitHub
        # side. Either way we can't fix it from here. Log so the user
        # can take manual action.
        logger.info(
            "ensure_repo_webhook: hook events PATCH %d on %s/%s — manual fix needed",
            resp.status_code,
            owner,
            repo,
        )
    else:
        logger.warning(
            "ensure_repo_webhook: hook events PATCH unexpected on %s/%s: %s",
            owner,
            repo,
            describe_upstream(resp),
        )


def _adopt_existing_hook(
    user, owner: str, repo: str, headers: dict, payload_url: str
) -> Optional[GithubWebhookRegistration]:
    """When the create API returned 422, scan the repo's hooks for one
    that already points at our payload URL and store its id locally.

    A hook made by hand in GitHub's settings page rarely subscribes to
    exactly `_HOOK_EVENTS` (missing `issue_comment`, extra `push`), so an
    adopted hook whose events differ is PATCHed into line straight away
    rather than waiting for the next `ensure_repo_webhook` call (MIP-709).
    Its secret is replaced with one of its own the same way (MIP-618).
    """
    try:
        resp = requests.get(
            f"{GITHUB_API_BASE}/repos/{owner}/{repo}/hooks",
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("GitHub hook list network error on %s/%s: %s", owner, repo, exc)
        return None
    if not resp.ok:
        return None
    for hook in resp.json() or []:
        if (hook.get("config") or {}).get("url") == payload_url:
            try:
                hook_id = int(hook.get("id"))
            except (ValueError, TypeError):
                continue
            try:
                registration = GithubWebhookRegistration.objects.create(
                    owner=owner, repo=repo, hook_id=hook_id, registered_by=user
                )
            except IntegrityError:
                return GithubWebhookRegistration.objects.filter(
                    owner__iexact=owner, repo__iexact=repo
                ).first()
            if set(hook.get("events") or []) != set(_HOOK_EVENTS):
                # A "synced" flag left from an earlier registration of
                # this repo would make the sync a no-op.
                cache.delete(_events_synced_cache_key(owner, repo))
                _sync_existing_hook_events_if_needed(user, owner, repo, hook_id)
            # Its secret is whatever was pasted by hand, which the manual
            # steps say is the shared one; give it its own now.
            rekey_hook(registration, headers)
            return registration
    return None


def ensure_webhooks_for_links(user, links) -> None:
    """Walk a task's `links` list, extract PR URLs, dedupe by (owner,
    repo), and call `ensure_repo_webhook` once per unique repo. Best-
    effort: any exception from a single repo doesn't stop the loop."""
    if not isinstance(links, list):
        logger.info(
            "ensure_webhooks_for_links: `links` is not a list (type=%s); skipping",
            type(links).__name__,
        )
        return
    seen: set[tuple[str, str]] = set()
    for link in links:
        if not isinstance(link, dict):
            continue
        ref = parse_pr_url(link.get("url"))
        if not ref or ref in seen:
            continue
        seen.add(ref)
        try:
            ensure_repo_webhook(user, ref[0], ref[1])
        except Exception:
            logger.exception("ensure_repo_webhook crashed on %s/%s (swallowed)", ref[0], ref[1])
    if not seen:
        logger.info(
            "ensure_webhooks_for_links: 0 PR URLs in %d link(s); nothing to register",
            len(links),
        )
