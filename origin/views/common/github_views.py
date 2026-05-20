"""GitHub pass-through endpoints (read-only) + webhook receiver.

Three endpoints:
  GET  /api/v2/github/pulls/                              — list my open PRs
  GET  /api/v2/github/pulls/<owner>/<repo>/<number>/      — single PR detail + status
  POST /api/v2/github/webhook/                            — repo-side webhook for PR events

GitHub OAuth App tokens don't expire, so the token helper just decrypts
the stored value. We send only GETs here; the `repo` scope grants more
than that on paper but the code-side discipline never writes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import requests
from django.conf import settings
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount
from origin.models.task.task_models import TaskMaster
from origin.services.oauth.tokens import get_valid_access_token

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def _connected_account(user) -> ConnectedAccount | None:
    return ConnectedAccount.objects.filter(user=user, provider="github").first()


def _not_connected() -> Response:
    return Response({"detail": "github_not_connected"}, status=status.HTTP_400_BAD_REQUEST)


def _github_get(account: ConnectedAccount, path: str, params: dict | None = None):
    token = get_valid_access_token(account)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return requests.get(
        f"{GITHUB_API_BASE}{path}", headers=headers, params=params or {}, timeout=15
    )


class GithubMyPullsView(APIView):
    """List PRs authored by the signed-in user across all accessible
    repos. Uses the Search API so we don't have to enumerate repos."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
        state = request.GET.get("state", "open")  # open | closed | all
        q = f"is:pr author:@me state:{state}"
        resp = _github_get(
            account,
            "/search/issues",
            params={"q": q, "sort": "updated", "order": "desc", "per_page": 50},
        )
        if not resp.ok:
            logger.warning("GitHub search failed: %s %s", resp.status_code, resp.text)
            return Response(
                {"detail": "github_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        data = resp.json()
        return Response(
            {
                "pulls": [
                    {
                        "title": item["title"],
                        "number": item["number"],
                        "html_url": item["html_url"],
                        "state": item["state"],
                        "draft": item.get("draft", False),
                        "repository_url": item.get("repository_url"),
                        "updated_at": item.get("updated_at"),
                        "created_at": item.get("created_at"),
                        # repo path (owner/name) parsed from the API URL
                        # for convenient display.
                        "repo": item.get("repository_url", "").rsplit("/repos/", 1)[-1],
                    }
                    for item in data.get("items", [])
                ],
                "total_count": data.get("total_count", 0),
            }
        )


class GithubPullDetailView(APIView):
    """Single PR: metadata + combined status of the head commit so the
    UI can show a green/yellow/red CI badge."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request, owner: str, repo: str, number: str):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
        pr_resp = _github_get(account, f"/repos/{owner}/{repo}/pulls/{number}")
        if not pr_resp.ok:
            logger.warning("GitHub PR fetch failed: %s %s", pr_resp.status_code, pr_resp.text)
            return Response(
                {"detail": "github_api_error", "upstream_status": pr_resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        pr = pr_resp.json()

        # Combined commit status (legacy) + check-runs (new) together
        # give the most complete CI picture. Both endpoints return 200
        # even when empty.
        head_sha = pr.get("head", {}).get("sha")
        combined = None
        checks = None
        if head_sha:
            cs = _github_get(account, f"/repos/{owner}/{repo}/commits/{head_sha}/status")
            if cs.ok:
                combined = cs.json()
            ck = _github_get(account, f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs")
            if ck.ok:
                checks = ck.json()
        return Response({"pull": pr, "combined_status": combined, "check_runs": checks})


# ---------------------------------------------------------------------------
# Webhook: PR merged → linked task auto-transitions to "Closed"
# ---------------------------------------------------------------------------

# When a task transitions because a linked PR was merged, we move it
# into this status. Mirrors the default Genos taxonomy (Open / WIP /
# Pending / Closed). If a team uses a different "done" label, they'll
# need to map this constant.
DONE_STATUS = "Closed"

# Statuses we treat as "already done" — no-op if the task is in one of
# these to avoid clobbering a manual close or re-firing on webhook
# replays.
DONE_STATUSES = {"Closed"}


def _verify_signature(raw_body: bytes, signature_header: str | None) -> bool:
    """Constant-time HMAC verification of GitHub's payload signature.

    GitHub signs every webhook payload with the shared secret using
    HMAC-SHA256 and sends the result in `X-Hub-Signature-256: sha256=…`.
    Returns False when the secret is unset (refuses to accept any
    request rather than silently trusting all senders).
    """
    secret = settings.GITHUB_WEBHOOK_SECRET
    if not secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    received = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, received)


def _close_tasks_linked_to_pr(pr_url: str) -> int:
    """Find tasks whose `links` array references this PR URL and flip
    their status to DONE_STATUS. Returns the count of tasks updated."""
    # `links` is a JSONField holding a list of {id, url, title, isGitHub}
    # objects. icontains over a JSON dump matches both straight and
    # encoded forms; PR URLs are ASCII so encoding differences don't
    # bite us here.
    candidates = TaskMaster.objects.filter(links__icontains=pr_url, is_deleted=False)
    updated = 0
    for task in candidates:
        # Belt-and-suspenders verify (icontains can match substrings):
        # walk the JSON list and check exact URL equality.
        if not isinstance(task.links, list):
            continue
        if not any(isinstance(link, dict) and link.get("url") == pr_url for link in task.links):
            continue
        if task.status in DONE_STATUSES:
            continue
        task.status = DONE_STATUS
        task.save(update_fields=["status", "ts_updated_at"])
        updated += 1
    return updated


@method_decorator(csrf_exempt, name="dispatch")
class GithubWebhookView(APIView):
    """Receive GitHub repo webhooks (`pull_request` events).

    Auto-transition: when a PR is merged, every task whose `links`
    references the PR's `html_url` gets bumped to "Closed". No-ops for
    tasks already closed or otherwise out of scope.

    GitHub retries failed webhooks for ~3 days, so we always return 200
    once signature verification passes — even when no tasks matched.
    Signature failures get 401 so GitHub stops sending obvious garbage.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request: Request):
        raw_body = request.body
        sig = request.headers.get("X-Hub-Signature-256") or request.META.get(
            "HTTP_X_HUB_SIGNATURE_256"
        )
        if not _verify_signature(raw_body, sig):
            logger.warning("GitHub webhook: bad / missing signature")
            return Response({"detail": "invalid_signature"}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.headers.get("X-GitHub-Event") or request.META.get("HTTP_X_GITHUB_EVENT")
        # Acknowledge ping (sent once when the webhook is created) so the
        # GitHub UI shows a green check. No body action needed.
        if event == "ping":
            return Response({"detail": "pong"}, status=status.HTTP_200_OK)

        if event != "pull_request":
            return Response({"detail": "ignored"}, status=status.HTTP_200_OK)

        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except ValueError:
            return Response({"detail": "bad_json"}, status=status.HTTP_400_BAD_REQUEST)

        pr = payload.get("pull_request") or {}
        action = payload.get("action")
        merged = bool(pr.get("merged"))
        html_url = pr.get("html_url")

        # MVP: only the merge transition fires. Other action types
        # (opened, ready_for_review, closed-unmerged, etc.) are
        # acknowledged but not acted on. See AskUserQuestion answer.
        if action == "closed" and merged and html_url:
            updated = _close_tasks_linked_to_pr(html_url)
            logger.info(
                "GitHub webhook: PR merged %s → updated %d task(s)",
                html_url,
                updated,
            )
            return Response({"detail": "ok", "updated": updated}, status=status.HTTP_200_OK)

        return Response({"detail": "noop"}, status=status.HTTP_200_OK)
