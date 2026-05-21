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
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import requests
from django.conf import settings
from django.db import IntegrityError

from origin.models.common.user_models import (
    ConnectedAccount,
    GithubWebhookRegistration,
)
from origin.services.oauth.tokens import get_valid_access_token

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"

# Same shape the frontend's parsePrUrl uses — keep them in sync.
_PR_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?$")


def parse_pr_url(url) -> Optional[tuple[str, str]]:
    """Return (owner, repo) for a valid PR URL, None otherwise."""
    if not isinstance(url, str):
        return None
    m = _PR_URL_RE.match(url)
    return (m.group(1), m.group(2)) if m else None


def _webhook_payload_url() -> str:
    base = (settings.BACKEND_BASE_URL or "").rstrip("/")
    return f"{base}/api/v2/github/webhook/"


def _hooks_api_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def ensure_repo_webhook(user, owner: str, repo: str) -> Optional[GithubWebhookRegistration]:
    """Make sure our PR webhook is registered on `owner/repo`.

    Returns the registration row on success, None on any failure mode
    (not connected, no token, no admin, 4xx, network error, missing
    secret config). Never raises — this runs on the task-save hot path.

    Logs at every silent early-return path so prod debugging can answer
    "why didn't this register?" by grepping for `ensure_repo_webhook`.
    """
    if not settings.GITHUB_WEBHOOK_SECRET:
        # Without a secret, our webhook endpoint refuses requests, so
        # registering the hook on GitHub would be pointless.
        logger.warning(
            "ensure_repo_webhook: GITHUB_WEBHOOK_SECRET is unset; skipping %s/%s",
            owner,
            repo,
        )
        return None

    existing = GithubWebhookRegistration.objects.filter(
        owner__iexact=owner, repo__iexact=repo
    ).first()
    if existing:
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
    body = {
        "name": "web",
        "active": True,
        "events": ["pull_request"],
        "config": {
            "url": payload_url,
            "content_type": "json",
            "secret": settings.GITHUB_WEBHOOK_SECRET,
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
                owner=owner, repo=repo, hook_id=hook_id, registered_by=user
            )
        except IntegrityError:
            # Lost a race — another concurrent task save just created
            # the row. Return whatever's there now.
            return GithubWebhookRegistration.objects.filter(
                owner__iexact=owner, repo__iexact=repo
            ).first()

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
        "GitHub hook create unexpected %d on %s/%s: %s",
        resp.status_code,
        owner,
        repo,
        (resp.text or "")[:200],
    )
    return None


def _adopt_existing_hook(
    user, owner: str, repo: str, headers: dict, payload_url: str
) -> Optional[GithubWebhookRegistration]:
    """When the create API returned 422, scan the repo's hooks for one
    that already points at our payload URL and store its id locally."""
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
                return GithubWebhookRegistration.objects.create(
                    owner=owner, repo=repo, hook_id=hook_id, registered_by=user
                )
            except IntegrityError:
                return GithubWebhookRegistration.objects.filter(
                    owner__iexact=owner, repo__iexact=repo
                ).first()
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
