"""GitHub pass-through endpoints (read-only).

Two endpoints:
  GET /api/v2/github/pulls/                              — list my open PRs
  GET /api/v2/github/pulls/<owner>/<repo>/<number>/      — single PR detail + status

GitHub OAuth App tokens don't expire, so the token helper just decrypts
the stored value. We send only GETs here; the `repo` scope grants more
than that on paper but the code-side discipline never writes.
"""

from __future__ import annotations

import logging

import requests
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount
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
