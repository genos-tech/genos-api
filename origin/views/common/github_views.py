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
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.services.github_webhooks import parse_pr_url, parse_pr_url_full
from origin.services.oauth.tokens import get_valid_access_token
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

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


def _close_tasks_for_merged_pr(head_ref: str, pr_url: str = "") -> int:
    """Auto-close tasks referenced by a merged PR's head branch name.

    Resolution path: walk every task that has a project-scoped display
    ID (`project.code` + `project_task_number`) and check whether the
    head branch name contains that ID with the same word-boundary regex
    used for branch auto-linking. Tasks that match AND whose assignee
    has opted in via `auto_close_on_pr_merge` get bumped to DONE_STATUS.

    `pr_url` is stashed on each task before the save so the
    `task_record_changes` signal can tag the resulting status-change
    activity as a PR-merge auto-close (the webhook is unauthenticated,
    so the row's `actor` is null and would otherwise render as an
    anonymous "Someone changed status" edit). See `task_signals.py`.

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
        # Tag the upcoming status-change audit row as a PR-merge close so
        # the activity feed can attribute it to the merged PR instead of
        # a null actor. Read back in `task_record_changes`.
        task._pr_merge_close = {"prUrl": pr_url or None}
        task.save(update_fields=["status", "ts_updated_at"])
        updated += 1
    return updated


# ---------------------------------------------------------------------------
# PR comments → task activity feed
# ---------------------------------------------------------------------------
#
# On `issue_comment` (PR comments only) and `pull_request_review_comment`
# (inline review comments), we record a `TaskActivity` row on every task
# whose `display_id` appears in the PR's head branch — same auto-link
# pattern used by the merge handler above. Only `created` actions are
# recorded for v1; edits/deletes are deferred.

# How long the head-ref lookup result is cached. The `issue_comment`
# payload doesn't carry the PR's head ref, so we fetch it via the
# GitHub API once and reuse the result across the burst of comment
# events that typically follows.
_PR_HEAD_REF_CACHE_TTL = 60


def _pr_head_ref_cache_key(owner: str, repo: str, number: int) -> str:
    return f"gh:pr_head_ref:{owner.lower()}:{repo.lower()}:{number}"


def _fetch_pr_head_ref(owner: str, repo: str, number: int) -> str | None:
    """Look up a PR's head branch ref via the GitHub API. Cached.

    Used to resolve `issue_comment` events to tasks. The webhook
    endpoint is unauthenticated, so we use a token from the repo's
    `GithubWebhookRegistration.registered_by` user — the same identity
    that auto-registered the webhook in the first place.
    """
    key = _pr_head_ref_cache_key(owner, repo, number)
    cached = cache.get(key)
    if cached is not None:
        # Empty string is the "we tried and failed" sentinel — keep it
        # cached for the negative-result TTL, but return None to callers.
        return cached or None

    reg = GithubWebhookRegistration.objects.filter(owner__iexact=owner, repo__iexact=repo).first()
    if reg is None or reg.registered_by_id is None:
        return None
    account = ConnectedAccount.objects.filter(
        user_id=reg.registered_by_id, provider="github"
    ).first()
    if account is None:
        return None
    try:
        resp = _github_get(account, f"/repos/{owner}/{repo}/pulls/{number}")
    except Exception:
        logger.exception("PR head-ref lookup crashed on %s/%s#%s", owner, repo, number)
        return None
    if not resp.ok:
        cache.set(key, "", _PR_HEAD_REF_CACHE_TTL)
        return None
    head_ref = ((resp.json() or {}).get("head") or {}).get("ref") or ""
    cache.set(key, head_ref, _PR_HEAD_REF_CACHE_TTL)
    return head_ref or None


def _record_pr_comment_activity(
    head_ref: str, pr_url: str, comment: dict, comment_kind: str
) -> int:
    """For each task whose `display_id` matches `head_ref`, create a
    `TaskActivity` row recording this PR comment. Idempotent by
    `(action_type, metadata.comment_id, task)` so webhook retries don't
    duplicate. Returns the count of rows actually created."""
    if not head_ref or not comment.get("id"):
        return 0
    candidates = TaskMaster.objects.select_related("project", "team").filter(
        is_deleted=False,
        project__code__isnull=False,
        project_task_number__isnull=False,
    )
    user = comment.get("user") or {}
    body = comment.get("body") or ""
    metadata = {
        "pr_url": pr_url,
        "comment_id": comment.get("id"),
        "comment_url": comment.get("html_url"),
        # Cap so a long comment doesn't bloat the activity row; the
        # frontend renders this as a short preview with a link out.
        "comment_excerpt": body[:280],
        "github_username": user.get("login"),
        "github_avatar_url": user.get("avatar_url"),
        # "issue" for top-level PR comments, "review" for inline.
        "comment_kind": comment_kind,
        "file_path": comment.get("path"),
        "line": comment.get("line"),
    }
    created = 0
    pattern = _branch_match_re  # forward ref; resolved at call time
    for task in candidates:
        display_id = task.display_id
        if not display_id or display_id.startswith("#"):
            continue
        if not pattern(display_id).search(head_ref):
            continue
        # Idempotency guard — webhook redeliveries (e.g. retry after a
        # transient failure) shouldn't produce duplicate activity rows.
        if TaskActivity.objects.filter(
            task=task,
            action_type=TaskActivityActionType.PR_COMMENT_ADDED,
            metadata__comment_id=metadata["comment_id"],
        ).exists():
            continue
        TaskActivity.objects.create(
            team=task.team,
            project=task.project,
            task=task,
            actor=None,
            action_type=TaskActivityActionType.PR_COMMENT_ADDED,
            metadata=metadata,
        )
        created += 1
    return created


def _record_pr_link_activity(head_ref: str, pr_url: str, number, title: str) -> int:
    """For each task whose `display_id` is contained in `head_ref`, create
    a `PR_LINKED` activity row recording that this PR (opened on a branch
    carrying the task's display id) is linked to the task. PRs whose head
    branch does NOT contain a task's display id are ignored — that's the
    same auto-link rule used for branch chips and PR-comment activity.

    Idempotent by `(action_type, metadata.pr_url, task)`: re-deliveries and
    reopen events don't duplicate. `pr_url` (not the per-repo PR number) is
    the dedup key because the number collides across repos. Returns the
    count of rows actually created."""
    if not head_ref or not pr_url:
        return 0
    candidates = TaskMaster.objects.select_related("project", "team").filter(
        is_deleted=False,
        project__code__isnull=False,
        project_task_number__isnull=False,
    )
    metadata = {
        "pr_url": pr_url,
        "pr_number": number,
        "pr_title": title or "",
        # The head branch that established the link — surfaced as a chip
        # so the activity reads "linked PR … (branch feature/GEN-42-x)".
        "branch": head_ref,
    }
    created = 0
    for task in candidates:
        display_id = task.display_id
        if not display_id or display_id.startswith("#"):
            continue
        if not _branch_match_re(display_id).search(head_ref):
            continue
        if TaskActivity.objects.filter(
            task=task,
            action_type=TaskActivityActionType.PR_LINKED,
            metadata__pr_url=pr_url,
        ).exists():
            continue
        TaskActivity.objects.create(
            team=task.team,
            project=task.project,
            task=task,
            actor=None,
            action_type=TaskActivityActionType.PR_LINKED,
            metadata=metadata,
        )
        created += 1
    return created


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

        if event not in ("pull_request", "issue_comment", "pull_request_review_comment"):
            return Response({"detail": "ignored"}, status=status.HTTP_200_OK)

        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except ValueError:
            return Response({"detail": "bad_json"}, status=status.HTTP_400_BAD_REQUEST)

        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            action = payload.get("action")
            merged = bool(pr.get("merged"))
            head_ref = ((pr.get("head") or {}).get("ref")) or ""
            html_url = pr.get("html_url") or ""

            # Drop our Redis-cached lookups of this PR / its head branch
            # so the UI sees the new state on the next fetch. Done for
            # every `pull_request` action (opened, ready_for_review,
            # closed, reopened, edited, synchronize, …) since each can
            # mutate state we cache (state, draft, merged_at, head sha).
            # Comment events skip this — they don't touch cached fields.
            ref = parse_pr_url_full(html_url)
            if ref is not None:
                owner_inv, repo_inv, number_inv = ref
                _invalidate_pr_caches(owner_inv, repo_inv, number_inv, head_ref or None)

            # MVP: only the merge transition fires. Other action types
            # (opened, ready_for_review, closed-unmerged, etc.) are
            # acknowledged but not acted on.
            if action == "closed" and merged and head_ref:
                updated = _close_tasks_for_merged_pr(head_ref, html_url)
                logger.info(
                    "GitHub webhook: PR merged %s (head=%s) → updated %d task(s)",
                    html_url,
                    head_ref,
                    updated,
                )
                return Response({"detail": "ok", "updated": updated}, status=status.HTTP_200_OK)

            # Record a link activity when a PR is opened (or reopened) on a
            # branch whose name carries a task's display id. Idempotent, so
            # a reopen after this already fired won't duplicate the row.
            if action in ("opened", "reopened") and head_ref:
                pr_number = pr.get("number")
                pr_title = pr.get("title") or ""
                linked = _record_pr_link_activity(head_ref, html_url, pr_number, pr_title)
                logger.info(
                    "GitHub webhook: PR %s %s (head=%s) → linked %d task(s)",
                    action,
                    html_url,
                    head_ref,
                    linked,
                )
                return Response({"detail": "ok", "linked": linked}, status=status.HTTP_200_OK)

            return Response({"detail": "noop"}, status=status.HTTP_200_OK)

        # Comment events — record on the auto-linked task's activity feed.
        # Only `created` actions are recorded; edits/deletes are deferred.
        if event == "pull_request_review_comment":
            return self._handle_pr_comment_event(
                payload=payload,
                head_ref_source="payload",
            )
        if event == "issue_comment":
            # Only PR comments — plain issue comments are out of scope.
            issue = payload.get("issue") or {}
            if not (issue.get("pull_request") or {}).get("html_url"):
                return Response({"detail": "ignored_non_pr"}, status=status.HTTP_200_OK)
            return self._handle_pr_comment_event(
                payload=payload,
                head_ref_source="fetch",
            )

        return Response({"detail": "noop"}, status=status.HTTP_200_OK)

    def _handle_pr_comment_event(self, *, payload: dict, head_ref_source: str) -> Response:
        """Shared handler for `issue_comment` (PR comments only) and
        `pull_request_review_comment` events. The two event types differ
        in how the PR's head ref is obtained (`head_ref_source`):

            "payload" — `pull_request_review_comment` payload already has
                        `pull_request.head.ref`; use it directly.
            "fetch"   — `issue_comment` payload has the PR URL but no
                        head ref; one cached GitHub API call resolves it.
        """
        action = payload.get("action")
        if action != "created":
            return Response({"detail": "noop"}, status=status.HTTP_200_OK)

        comment = payload.get("comment") or {}
        if not comment.get("id"):
            return Response({"detail": "noop"}, status=status.HTTP_200_OK)

        if head_ref_source == "payload":
            pr = payload.get("pull_request") or {}
            head_ref = ((pr.get("head") or {}).get("ref")) or ""
            pr_url = pr.get("html_url") or ""
            comment_kind = "review"
        else:
            issue = payload.get("issue") or {}
            pr_url = (issue.get("pull_request") or {}).get("html_url") or ""
            ref = parse_pr_url(pr_url)
            if ref is None:
                return Response({"detail": "noop"}, status=status.HTTP_200_OK)
            owner, repo = ref
            number = issue.get("number")
            head_ref = _fetch_pr_head_ref(owner, repo, int(number)) if number else None
            head_ref = head_ref or ""
            comment_kind = "issue"

        if not head_ref:
            logger.info(
                "GitHub webhook: PR comment skipped — could not resolve head ref (pr=%s)", pr_url
            )
            return Response({"detail": "noop"}, status=status.HTTP_200_OK)

        recorded = _record_pr_comment_activity(head_ref, pr_url, comment, comment_kind)
        logger.info(
            "GitHub webhook: %s comment %s (head=%s) → recorded %d activity row(s)",
            comment_kind,
            pr_url,
            head_ref,
            recorded,
        )
        return Response({"detail": "ok", "recorded": recorded}, status=status.HTTP_200_OK)


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


def _pr_by_number_cache_key(owner: str, repo: str, number: int) -> str:
    return f"gh:pr_by_number:{owner.lower()}:{repo.lower()}:{number}"


def _list_repo_branches(
    account: ConnectedAccount,
    owner: str,
    repo: str,
    *,
    bypass_cache: bool = False,
) -> list[dict] | None:
    """Cached wrapper over `GET /repos/{o}/{r}/branches`. Returns None on
    upstream error so callers can skip the repo silently.

    When `bypass_cache=True`, skips the read but still writes the fresh
    result so subsequent natural fetches within the TTL benefit.
    """
    key = _branch_cache_key(owner, repo)
    if not bypass_cache:
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
    account: ConnectedAccount,
    owner: str,
    repo: str,
    branch: str,
    *,
    bypass_cache: bool = False,
) -> dict | None:
    """Look up the PR (if any) whose head ref is `owner:branch`. Returns
    a slim PR dict or None. Cached so the table doesn't re-fetch on
    every row mount within the TTL.

    When `bypass_cache=True`, skips the read but still writes the fresh
    result so subsequent natural fetches within the TTL benefit.
    """
    key = _pull_cache_key(owner, repo, branch)
    if not bypass_cache:
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


def _fetch_pr_by_number(
    account: ConnectedAccount,
    owner: str,
    repo: str,
    number: int,
    *,
    bypass_cache: bool = False,
) -> dict | None:
    """Fetch a PR's detail by its number. Cached. Returns the same slim
    dict shape as `_find_pr_for_branch` so the two sources can be unioned
    in `GithubPullsForTaskView`.

    Used for auto-linked PR URLs persisted in `task.links` whose source
    branch has since been deleted — branch-list-based lookup can't find
    them anymore, but the PR record itself still lives on GitHub.

    When `bypass_cache=True`, skips the read but still writes the fresh
    result so subsequent natural fetches within the TTL benefit.
    """
    key = _pr_by_number_cache_key(owner, repo, number)
    if not bypass_cache:
        cached = cache.get(key)
        if cached is not None:
            # Empty dict is the sentinel for "we tried and failed" so we
            # don't keep retrying inside the TTL window.
            return cached or None
    resp = _github_get(account, f"/repos/{owner}/{repo}/pulls/{number}")
    if not resp.ok:
        cache.set(key, {}, _PULL_CACHE_TTL)
        return None
    pr = resp.json() or {}
    slim = {
        "owner": owner,
        "repo": repo,
        "branch": (pr.get("head") or {}).get("ref") or "",
        "number": pr.get("number"),
        "html_url": pr.get("html_url"),
        "title": pr.get("title"),
        "state": pr.get("state"),
        "draft": pr.get("draft", False),
        "merged_at": pr.get("merged_at"),
    }
    cache.set(key, slim, _PULL_CACHE_TTL)
    return slim


def _invalidate_pr_caches(owner: str, repo: str, number: int | None, head_ref: str | None) -> None:
    """Drop the Redis-cached GitHub lookups for this PR so the UI sees
    the new PR state on the next fetch (rather than waiting up to 60s
    for TTL).

    Called from the `pull_request` webhook dispatch. Comment events
    (`issue_comment`, `pull_request_review_comment`) don't trigger this
    because they don't mutate any of the fields we cache.

    Best-effort: a cache-delete failure logs but never propagates — a
    missed invalidation just means an extra TTL cycle of staleness.
    """
    keys: list[str] = [_branch_cache_key(owner, repo)]
    if head_ref:
        keys.append(_pull_cache_key(owner, repo, head_ref))
    if number is not None:
        keys.append(_pr_by_number_cache_key(owner, repo, number))
        keys.append(_pr_head_ref_cache_key(owner, repo, number))
    try:
        cache.delete_many(keys)
        logger.info(
            "ensure_repo_webhook: invalidated %d PR cache key(s) for %s/%s#%s",
            len(keys),
            owner,
            repo,
            number,
        )
    except Exception:
        logger.exception("PR cache invalidation failed for %s/%s#%s", owner, repo, number)


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
        bypass_cache = request.GET.get("fresh") == "1"

        matches: list[dict] = []
        for owner, repo in repos:
            branches = _list_repo_branches(account, owner, repo, bypass_cache=bypass_cache)
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

    Two sources are unioned (deduped by html_url):

      1. **Live branches** — for each repo we've registered our webhook on,
         walk branches whose name contains the task's display ID and look
         up each branch's PR. This is the "discovery" path.
      2. **Persisted auto-links** — PR URLs in `task.links` flagged with
         `isAutoLinked: true` AND whose head branch still matches this
         task's display ID. Picks up PRs whose source branch has since
         been deleted (typical post-merge cleanup) — without it the
         column would go blank the moment a merged PR's branch is
         removed. The branch-match re-validation is a backstop against
         stale/bad flags in `task.links` (e.g. from earlier frontend
         versions that auto-flagged body-pasted PR URLs).

    Manually-pasted PR links in `task.links` (no `isAutoLinked` flag) are
    intentionally NOT surfaced — auto-linking is the only source of
    truth for the PR column so stale URLs from manual entry don't leak in.

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
        task, display_id = resolved

        account = _connected_account(request.user)
        if account is None:
            return Response({"pulls": []})

        pattern = _branch_match_re(display_id)
        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()
        bypass_cache = request.GET.get("fresh") == "1"

        pulls: list[dict] = []
        seen_urls: set[str] = set()
        # --- Source 1: live branches matching display_id -------------
        for owner, repo in repos:
            branches = _list_repo_branches(account, owner, repo, bypass_cache=bypass_cache)
            if branches is None:
                continue
            for branch in branches:
                name = branch.get("name") or ""
                if not pattern.search(name):
                    continue
                pr = _find_pr_for_branch(account, owner, repo, name, bypass_cache=bypass_cache)
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

        # --- Source 2: PR URLs persisted in task.links with the
        # `isAutoLinked` marker. Picks up PRs whose source branch has
        # been deleted post-merge (Source 1's branch walk misses those
        # because the branch is gone).
        #
        # We re-validate that the fetched PR's head branch actually
        # contains the task's display_id. The flag is set by the
        # frontend and an earlier bug in the body-mirror effect could
        # mark unrelated PR URLs (referenced from the task body) as
        # auto-linked. Without this backstop those bad flags would
        # leak unrelated PRs into the column. GitHub keeps the head
        # ref on the PR record even after branch deletion, so this
        # check still works for the post-merge persistence case.
        for link in task.links or []:
            if not isinstance(link, dict) or not link.get("isAutoLinked"):
                continue
            url = link.get("url")
            if not isinstance(url, str) or url in seen_urls:
                continue
            ref = parse_pr_url_full(url)
            if ref is None:
                continue
            owner_p, repo_p, number = ref
            pr = _fetch_pr_by_number(account, owner_p, repo_p, number, bypass_cache=bypass_cache)
            if pr is None:
                continue
            head_ref = pr.get("branch") or ""
            if not head_ref or not pattern.search(head_ref):
                continue
            seen_urls.add(url)
            pulls.append(pr)

        return Response({"pulls": pulls})
