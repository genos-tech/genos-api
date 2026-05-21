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
import re

import requests
from django.conf import settings
from django.core.cache import cache
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from rest_framework import permissions, status
from rest_framework.response import Response
from rest_framework.request import Request
from rest_framework.views import APIView

from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
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


def _close_tasks_for_merged_pr(head_ref: str) -> int:
    """Auto-close tasks referenced by a merged PR's head branch name.

    Resolution path: walk every task that has a project-scoped display
    ID (`project.code` + `project_task_number`) and check whether the
    head branch name contains that ID with the same word-boundary regex
    used for branch auto-linking. Tasks that match AND whose assignee
    has opted in via `auto_close_on_pr_merge` get bumped to DONE_STATUS.

    Returns the count of tasks actually closed.

    Why not `task.links`: the source of truth for "what does this PR
    belong to" is the branch naming convention (PR column, branch
    chips), not whether someone pasted the URL into Links. Using the
    same resolution path everywhere keeps the system coherent —
    automatic for users who follow the convention, no-op for users who
    don't.
    """
    if not head_ref:
        return 0
    # Pre-filter to tasks that *could* have a display ID matched.
    # `select_related("project", "assignee")` keeps the per-row lookup
    # cheap; for a workspace with thousands of tasks this iterates in
    # SQL once, then the regex pass runs in memory.
    candidates = (
        TaskMaster.objects.select_related("project", "assignee")
        .filter(
            is_deleted=False,
            project__code__isnull=False,
            project_task_number__isnull=False,
        )
        .exclude(status__in=DONE_STATUSES)
    )
    updated = 0
    # We don't know up front which task's display ID matches — but for
    # any given head ref the matching display IDs are bounded by the
    # branch name's content, so this loop is fast in practice (most
    # tasks fail the regex check immediately).
    for task in candidates:
        display_id = task.display_id
        if not display_id or display_id.startswith("#"):
            continue
        if not _branch_match_re(display_id).search(head_ref):
            continue
        # Per-user opt-in. Assignee's preference wins; reporter's
        # preference is intentionally ignored (the assignee owns the
        # task's "done" decision).
        assignee = task.assignee
        if assignee is None or not getattr(assignee, "auto_close_on_pr_merge", False):
            continue
        task.status = DONE_STATUS
        task.save(update_fields=["status", "ts_updated_at"])
        updated += 1
    return updated


@method_decorator(csrf_exempt, name="dispatch")
class GithubWebhookView(APIView):
    """Receive GitHub repo webhooks (`pull_request` events).

    Auto-transition: when a PR merges, any task whose display ID
    appears in the PR's head branch name (e.g. `feature/GEN-42-foo`
    closes task `GEN-42`) is bumped to "Closed" — *but only if the
    task's assignee has opted in* via the `auto_close_on_pr_merge`
    preference. Default is opt-out.

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
        head_ref = ((pr.get("head") or {}).get("ref")) or ""
        html_url = pr.get("html_url")

        # MVP: only the merge transition fires. Other action types
        # (opened, ready_for_review, closed-unmerged, etc.) are
        # acknowledged but not acted on.
        if action == "closed" and merged and head_ref:
            updated = _close_tasks_for_merged_pr(head_ref)
            logger.info(
                "GitHub webhook: PR merged %s (head=%s) → updated %d task(s)",
                html_url,
                head_ref,
                updated,
            )
            return Response({"detail": "ok", "updated": updated}, status=status.HTTP_200_OK)

        return Response({"detail": "noop"}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Branch auto-linking
# ---------------------------------------------------------------------------
#
# Once tasks have human-readable display IDs ("GEN-42"), developers tend to
# follow a "branch per task" convention where the branch name embeds the
# display ID (e.g. "feature/GEN-42-new-thing", "GEN-42_fix"). This endpoint
# scans the repos we've already auto-registered a webhook on and returns any
# branch whose name matches the task's display ID, so the task UI can show
# them under the Links section without the user having to paste branch URLs.
#
# Scoped to repos with an existing `GithubWebhookRegistration` row — i.e.
# repos this team has already touched via a PR link. That keeps the API
# fan-out bounded and avoids querying every repo the user can see.


def _branch_match_re(display_id: str) -> re.Pattern[str]:
    """Match a branch name that contains `display_id` at a non-alnum
    boundary, case-insensitive. Avoids false positives like "GEN-4" → "GEN-42"."""
    return re.compile(
        rf"(^|[^A-Za-z0-9]){re.escape(display_id)}([^A-Za-z0-9]|$)",
        re.IGNORECASE,
    )


# ── Server-side cache for GitHub fan-out ──────────────────────────────
#
# The branches-for-task and pulls-for-task endpoints fan out one GitHub
# call per known repo and (for pulls) one call per matching branch. A
# table view of 100 tasks all calling these endpoints would otherwise
# churn 100×N calls on first paint. We cache the per-repo branch list
# and the per-branch PR lookup in Redis with a short TTL so the table
# load coalesces down to ~N calls and re-paints cost nothing.
#
# Cache scope: the data is *user-tokenable* (different users may have
# different access to the same repo), but for our case all
# webhook-registered repos are visible to the requesting user already
# (otherwise the webhook would never have been registered for them).
# Keying purely on (owner, repo) is safe and gives the highest hit rate.

_BRANCH_CACHE_TTL = 60  # seconds — matches the frontend prStatusCache
_PULL_CACHE_TTL = 60


def _branch_cache_key(owner: str, repo: str) -> str:
    return f"gh:branches:{owner.lower()}:{repo.lower()}"


def _pull_cache_key(owner: str, repo: str, branch: str) -> str:
    return f"gh:head_pull:{owner.lower()}:{repo.lower()}:{branch}"


def _list_repo_branches(account: ConnectedAccount, owner: str, repo: str) -> list[dict] | None:
    """Cached wrapper over `GET /repos/{o}/{r}/branches`. Returns None on
    upstream error so callers can skip the repo silently."""
    key = _branch_cache_key(owner, repo)
    cached = cache.get(key)
    if cached is not None:
        return cached
    resp = _github_get(account, f"/repos/{owner}/{repo}/branches", params={"per_page": 100})
    if not resp.ok:
        return None
    branches = resp.json() or []
    cache.set(key, branches, _BRANCH_CACHE_TTL)
    return branches


def _find_pr_for_branch(
    account: ConnectedAccount, owner: str, repo: str, branch: str
) -> dict | None:
    """Look up the PR (if any) whose head ref is `owner:branch`. Returns
    a slim PR dict or None. Cached so the table doesn't re-fetch on
    every row mount within the TTL."""
    key = _pull_cache_key(owner, repo, branch)
    cached = cache.get(key)
    if cached is not None:
        # Cached payload may legitimately be the sentinel `{}` meaning
        # "this branch has no PR" — normalize that back to None.
        return cached or None
    resp = _github_get(
        account,
        f"/repos/{owner}/{repo}/pulls",
        params={
            "head": f"{owner}:{branch}",
            "state": "all",
            "per_page": 1,
            "sort": "updated",
            "direction": "desc",
        },
    )
    if not resp.ok:
        return None
    items = resp.json() or []
    if not items:
        # Cache the "no PR" answer too — it's the common case for
        # branches without an associated PR.
        cache.set(key, {}, _PULL_CACHE_TTL)
        return None
    pr = items[0]
    slim = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "number": pr.get("number"),
        "html_url": pr.get("html_url"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft", False),
        "merged_at": pr.get("merged_at"),
    }
    cache.set(key, slim, _PULL_CACHE_TTL)
    return slim


def _resolve_task_display_id(task_id: str):
    """Common task lookup + display-id guard shared by both endpoints.
    Returns (task, display_id) on success or a Response on failure."""
    try:
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        return None, Response({"detail": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)
    display_id = task.display_id
    if not display_id or display_id.startswith("#"):
        return None, None  # signal "empty result"
    return (task, display_id), None


class GithubBranchesForTaskView(APIView):
    """List branches across known repos whose names reference this task's
    display ID. Returns an empty list when GitHub is not connected or no
    match is found (the UI hides the section in that case)."""

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        task_id = request.GET.get("task_id")
        if not task_id:
            return Response({"detail": "task_id_required"}, status=status.HTTP_400_BAD_REQUEST)
        resolved, err = _resolve_task_display_id(task_id)
        if err is not None:
            return err
        if resolved is None:
            return Response({"branches": []})
        _task, display_id = resolved

        account = _connected_account(request.user)
        if account is None:
            return Response({"branches": []})

        pattern = _branch_match_re(display_id)
        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()

        matches: list[dict] = []
        for owner, repo in repos:
            branches = _list_repo_branches(account, owner, repo)
            if branches is None:
                # 404 (no access) / 403 (rate limit) / etc. — skip the
                # repo silently so one bad repo doesn't poison the list.
                continue
            for branch in branches:
                name = branch.get("name") or ""
                if not pattern.search(name):
                    continue
                sha = (branch.get("commit") or {}).get("sha")
                matches.append(
                    {
                        "owner": owner,
                        "repo": repo,
                        "name": name,
                        "url": f"https://github.com/{owner}/{repo}/tree/{name}",
                        "commit_sha": sha,
                    }
                )
        return Response({"branches": matches})


class GithubPullsForTaskView(APIView):
    """List PRs auto-linked to this task via branch naming convention.

    A PR is "auto-linked" when its head ref (branch name) contains the
    task's display ID at non-alphanumeric boundaries. This is the single
    source of truth for the task table's PR column — manually-pasted PR
    links in `task.links` are intentionally not included; auto-linking
    is the only path so the column doesn't surface stale links.

    Returns an empty list when GitHub is not connected, the task has no
    project-scoped display ID, or no auto-linked PRs exist.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request):
        task_id = request.GET.get("task_id")
        if not task_id:
            return Response({"detail": "task_id_required"}, status=status.HTTP_400_BAD_REQUEST)
        resolved, err = _resolve_task_display_id(task_id)
        if err is not None:
            return err
        if resolved is None:
            return Response({"pulls": []})
        _task, display_id = resolved

        account = _connected_account(request.user)
        if account is None:
            return Response({"pulls": []})

        pattern = _branch_match_re(display_id)
        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()

        pulls: list[dict] = []
        seen_urls: set[str] = set()
        for owner, repo in repos:
            branches = _list_repo_branches(account, owner, repo)
            if branches is None:
                continue
            for branch in branches:
                name = branch.get("name") or ""
                if not pattern.search(name):
                    continue
                pr = _find_pr_for_branch(account, owner, repo, name)
                if pr is None:
                    continue
                # Same PR can appear from different matching branches
                # (rare — but a renamed-then-recreated branch could do
                # it). Dedupe by html_url so the column shows one badge
                # per PR.
                url = pr.get("html_url")
                if url and url in seen_urls:
                    continue
                if url:
                    seen_urls.add(url)
                pulls.append(pr)
        return Response({"pulls": pulls})
