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
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils.decorators import method_decorator
from django.views.decorators.csrf import csrf_exempt
from rest_framework import permissions, status
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apis.request_id import describe_upstream
from origin.models.common.user_models import (
    ConnectedAccount,
    GithubUnregisteredDelivery,
    GithubWebhookRegistration,
)
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.services.github_webhooks import (
    forget_unregistered_delivery,
    is_valid_repo_ref,
    parse_pr_url,
    parse_pr_url_full,
    record_unregistered_delivery,
    register_repo_webhook,
)
from origin.services.oauth.tokens import get_valid_access_token
from origin.services.task_delivery import canonical_pr_url, sync_pull_request
from origin.views.common.object_body import ObjectBodyMixin
from origin.views.utils.scope_guards import can_access_task, team_ids_for_user

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"


def _connected_account(user) -> ConnectedAccount | None:
    return ConnectedAccount.objects.filter(user=user, provider="github").first()


def _not_connected() -> Response:
    return Response({"detail": "github_not_connected"}, status=status.HTTP_400_BAD_REQUEST)


class _GithubCredential:
    """A connected GitHub account as ONE request uses it: what the cache keys
    need (`id`, `ts_updated_at`), the access token, and a tally of the GitHub
    calls made, for the `gh.links` log line.

    The token is resolved once, by the thread that built this object (the
    request thread). `get_valid_access_token` can refresh a token and save the
    account row, which is database work a pool worker must never do; a worker
    only reads `.token`, and trying to resolve from another thread is an error
    rather than a silent decrypt (or a save) off the request thread.
    """

    def __init__(self, account: ConnectedAccount):
        self.id = account.id
        self.ts_updated_at = account.ts_updated_at
        self._account = account
        self._owner_thread = threading.get_ident()
        self._token: str | None = None
        self.upstream = 0
        # Lookups that failed for a reason that is NOT about access (a 5xx, a rate
        # limit, a 401), and the repos whose branch list failed the same way. The
        # batched PR route names the tasks it could not fully look at (MIP-1412);
        # a refusal is not counted, it is the answer for this account.
        self.failed_lookups = 0
        self.failed_repos: set[tuple[str, str]] = set()

    def resolve(self) -> "_GithubCredential":
        if self._token is None:
            if threading.get_ident() != self._owner_thread:
                raise RuntimeError("GitHub token must be resolved in the request thread")
            self._token = get_valid_access_token(self._account)
        return self

    @property
    def token(self) -> str:
        if self._token is None:
            raise RuntimeError("GitHub token not resolved")
        return self._token


def _credential(account) -> _GithubCredential:
    return account if isinstance(account, _GithubCredential) else _GithubCredential(account)


def _github_get(
    account: ConnectedAccount | _GithubCredential, path: str, params: dict | None = None
):
    if isinstance(account, _GithubCredential):
        token = account.resolve().token
    else:
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
            logger.warning("GitHub search failed: %s", describe_upstream(resp))
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


class GithubAccessibleReposView(APIView):
    """GET /api/v2/github/accessible-repos/

    What can Genos actually reach on GitHub right now, grouped by owner.

    This exists because our GitHub integration is a classic **OAuth App**
    with the account-wide `repo` scope, not a GitHub App. There is no
    per-repo selection to expose: a repo the user creates in their own
    account is reachable immediately. ORGANIZATION repos are the
    exception — an OAuth App only sees an org's repos once that org has
    granted (or an owner has approved) access, and that grant is made on
    GitHub, at authorization time or from the app's settings page.

    So "add another repo" is really "confirm what's reachable, and grant
    the org that isn't". This endpoint answers the first half; the
    `manage_url` it returns is where the second half happens.

    `manage_url` is built server-side because the OAuth client id lives
    in backend settings and the frontend has no other way to construct
    the per-application settings link.
    """

    permission_classes = [permissions.IsAuthenticated]

    # One page is plenty to answer "can Genos see this org at all?", and
    # bounds the response for accounts with hundreds of repos. Sorted by
    # recent activity so a freshly-added org's repos surface at the top.
    _PER_PAGE = 100

    def get(self, request: Request):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()

        resp = _github_get(
            account,
            "/user/repos",
            params={
                "per_page": self._PER_PAGE,
                "sort": "updated",
                # Everything the token can reach, including repos the user
                # only has org-membership access to — which is exactly the
                # set this endpoint is meant to reveal.
                "affiliation": "owner,collaborator,organization_member",
            },
        )
        if not resp.ok:
            logger.warning("GitHub repo list failed: %s", describe_upstream(resp))
            return Response(
                {"detail": "github_api_error", "upstream_status": resp.status_code},
                status=status.HTTP_502_BAD_GATEWAY,
            )

        repos = []
        owners: dict[str, dict] = {}
        for item in resp.json() or []:
            owner_login = (item.get("owner") or {}).get("login") or ""
            owner_type = (item.get("owner") or {}).get("type") or "User"
            repos.append(
                {
                    "full_name": item.get("full_name"),
                    "name": item.get("name"),
                    "owner": owner_login,
                    "private": item.get("private", False),
                    "html_url": item.get("html_url"),
                    "updated_at": item.get("updated_at"),
                }
            )
            entry = owners.setdefault(
                owner_login,
                {"login": owner_login, "type": owner_type, "repo_count": 0},
            )
            entry["repo_count"] += 1

        client_id = settings.GITHUB_OAUTH_CLIENT_ID
        return Response(
            {
                "repos": repos,
                # Sorted so organizations read as a stable list rather than
                # in GitHub's activity order.
                "owners": sorted(owners.values(), key=lambda o: o["login"].lower()),
                "truncated": len(repos) >= self._PER_PAGE,
                "manage_url": (
                    f"https://github.com/settings/connections/applications/{client_id}"
                    if client_id
                    else None
                ),
            }
        )


def _upstream_error(upstream_status: int) -> Response:
    return Response(
        {"detail": "github_api_error", "upstream_status": upstream_status},
        status=status.HTTP_502_BAD_GATEWAY,
    )


class GithubPullDetailView(APIView):
    """Single PR: metadata + combined status of the head commit so the
    UI can show a green/yellow/red CI badge.

    Cached in Redis per GitHub account (see "Caching" above
    `_BRANCH_CACHE_TTL`, and `_pull_detail_ttl` for how long). The task open
    and the table's PR column call this once per linked PR; it used to cost
    three live GitHub calls in a row every time (pull, then commit status,
    then check runs), about 1.7 s at the median. A miss now costs two round
    trips (the pull, then the two CI calls together) and a hit none.

    `?fresh=1` skips the READ (also of a remembered refusal) and still writes
    the answer, as it does on the lookups routes: the PR column's Refresh click
    (MIP-1410). An open PR's CI finishing sends no webhook, so without it the
    CI dot is only as new as `_PULL_DETAIL_TTL`. A fresh request that fails
    stores nothing, so the entry it skipped is still there for the next plain
    request.
    """

    permission_classes = [permissions.IsAuthenticated]

    def get(self, request: Request, owner: str, repo: str, number: str):
        account = _connected_account(request.user)
        if account is None:
            return _not_connected()
        credential = _GithubCredential(account)
        # Read before GitHub is asked, so a webhook that lands while the
        # fetch is in flight orphans the key this answer is written under.
        key = _pull_detail_cache_key(credential, owner, repo, number)
        bypass_cache = request.GET.get("fresh") == "1"
        if key is not None and not bypass_cache:
            cached = cache.get(key)
            if cached is not None:
                _log_links("pull_detail", credential)
                if _is_denied(cached):
                    return _upstream_error(cached["upstream_status"])
                return Response(cached)
        response = self._fetch(credential, owner, repo, number, key)
        _log_links("pull_detail", credential)
        return response

    def _fetch(
        self, credential: _GithubCredential, owner: str, repo: str, number: str, key: str | None
    ) -> Response:
        pr_resp = _call_github(credential, f"/repos/{owner}/{repo}/pulls/{number}")
        if not pr_resp.ok:
            logger.warning("GitHub PR fetch failed: %s", describe_upstream(pr_resp))
            if key is not None and _is_refusal(pr_resp):
                # Same answer for this account until the TTL, a reconnect
                # (new key) or `_DENIED_CACHE_TTL`. A 5xx, a timeout or a
                # rate limit says nothing about access and is not cached.
                refusal = {**_DENIED, "upstream_status": pr_resp.status_code}
                cache.set(key, refusal, _DENIED_CACHE_TTL)
            return _upstream_error(pr_resp.status_code)
        pr = pr_resp.json()

        # Combined commit status (legacy) + check-runs (new) together
        # give the most complete CI picture. Both endpoints return 200
        # even when empty. They depend only on the head sha, so they run
        # together once the pull has named it.
        head_sha = (pr.get("head") or {}).get("sha")
        combined = None
        checks = None
        complete = True
        if head_sha:
            cs, ck = _get_all(
                credential,
                [
                    (f"/repos/{owner}/{repo}/commits/{head_sha}/status", None),
                    (f"/repos/{owner}/{repo}/commits/{head_sha}/check-runs", None),
                ],
            )
            if cs.ok:
                combined = cs.json()
            if ck.ok:
                checks = ck.json()
            # A CI call that failed leaves a half-empty badge. It is still
            # served (as it always was) but never stored: a hiccup must not
            # freeze a degraded answer for the whole TTL.
            complete = cs.ok and ck.ok
        body = {"pull": pr, "combined_status": combined, "check_runs": checks}
        if key is not None and complete:
            cache.set(key, body, _pull_detail_ttl(pr, combined, checks))
        return Response(body)


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


def _payload_repo(payload) -> tuple[str, str] | None:
    """(owner, repo) a delivery claims to come from: its `repository`.

    Read before the signature is checked, so it is only a claim. It
    picks which secret to check against; the delivery is trusted only
    once that secret verifies, and then only for that repo
    (`_pr_url_repo_mismatch`).
    """
    if not isinstance(payload, dict):
        return None
    full_name = (payload.get("repository") or {}).get("full_name")
    if not isinstance(full_name, str):
        return None
    owner, _, repo = full_name.partition("/")
    if not owner or not repo or "/" in repo:
        return None
    return owner, repo


def _signing_secrets(repo_ref: tuple[str, str] | None) -> list[str]:
    """The secrets a delivery for `repo_ref` may be signed with.

    A repo's own secret (MIP-618), plus the shared GITHUB_WEBHOOK_SECRET
    while its registration still accepts it: a hook registered before
    per-repo secrets, or one mid-way through getting its own
    (`github_webhooks.rekey_hook`). Once a repo has its own, the shared
    secret no longer speaks for it, so a leaked one can't forge its
    events. An unregistered repo gets only the shared one; it authorizes
    nothing beyond the ping a hand-made hook sends (`_repo_team_ids`).
    """
    # Every case-insensitive match: rows that differ only in casing can
    # exist (the unique constraint is case-sensitive), and each could be
    # the one whose secret GitHub holds.
    registrations = []
    if repo_ref is not None:
        registrations = list(
            GithubWebhookRegistration.objects.filter(
                owner__iexact=repo_ref[0], repo__iexact=repo_ref[1]
            )
        )
    candidates = [r.secret for r in registrations if r.secret]
    if not registrations or any(r.accepts_shared_secret for r in registrations):
        if settings.GITHUB_WEBHOOK_SECRET:
            candidates.append(settings.GITHUB_WEBHOOK_SECRET)
    return candidates


def _verify_signature(raw_body: bytes, signature_header: str | None, secrets: list[str]) -> bool:
    """Constant-time HMAC verification of GitHub's payload signature.

    GitHub signs every webhook payload with the hook's secret using
    HMAC-SHA256 and sends the result in `X-Hub-Signature-256: sha256=…`.
    Returns False when there is no secret to check against (refuses to
    accept any request rather than silently trusting all senders).
    """
    if not secrets or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    received = signature_header.split("=", 1)[1]
    return any(
        hmac.compare_digest(
            hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest(), received
        )
        for secret in secrets
    )


def _pr_url_repo_mismatch(event: str | None, payload: dict, repo_ref) -> bool:
    """Whether the PR a verified delivery is about lives in another repo.

    The handlers resolve the tenant and the tasks from the PR's url, not
    from `repository`. A delivery signed with repo A's secret naming a PR
    in repo B would otherwise act on B: anyone holding A's secret could
    forge B's events, which is what per-repo secrets exist to prevent.
    GitHub never sends one.
    """
    if event == "issue_comment":
        pr_url = ((payload.get("issue") or {}).get("pull_request") or {}).get("html_url")
    else:
        pr_url = (payload.get("pull_request") or {}).get("html_url")
    ref = parse_pr_url(pr_url)
    if ref is None:
        return False  # Nothing to act on; the handlers no-op.
    if repo_ref is None:
        return True
    return (ref[0].lower(), ref[1].lower()) != (repo_ref[0].lower(), repo_ref[1].lower())


def _repo_team_ids(owner: str, repo: str) -> list:
    """Team ids a webhook for `owner/repo` is allowed to write into.

    The webhook is authenticated by a repo signature, not a session, so
    it arrives with no team context — and every task-resolution helper
    below used to answer that by scanning `TaskMaster` across the whole
    install. Branch names carry a project's display id (`code` +
    number), and project codes are short, human, guessable strings, so
    a branch named after ANOTHER tenant's task closed or annotated that
    tenant's task. One shared `GITHUB_WEBHOOK_SECRET` signed every
    registered repo's payloads, so any repo connected by any customer
    produced a valid signature. (Each repo now has its own secret,
    MIP-618, but a repo's own secret still says nothing about which
    tenant its branch names may reach, so this scope stays.)

    `GithubWebhookRegistration` records who registered the hook, not
    where its tasks live — there is no `team` FK — so the best scope the
    current model supports is the registering user's teams. That is a
    real narrowing: reaching another tenant now requires the registrar
    to already be a member there, where they can act anyway. **A `team`
    FK on the registration is the durable fix** and is the reason this
    function is one lookup rather than a join.

    Empty list = write nothing. An unregistered repo authorizes nothing,
    and neither does a registration whose user has been deleted
    (`registered_by` is `SET_NULL`) — fail closed, and say so in the log
    rather than silently reverting to install-wide.
    """
    # `__iexact` because GitHub owner/repo names are case-insensitive and
    # the payload's casing need not match what was stored —
    # `github_webhooks.register_repo_webhook` already looks its own rows
    # up this way. An exact match would silently yield "no scope", i.e.
    # quietly disable auto-close for a repo whose URL casing drifted.
    registered_by = (
        GithubWebhookRegistration.objects.filter(owner__iexact=owner, repo__iexact=repo)
        .values_list("registered_by_id", flat=True)
        .first()
    )
    if not registered_by:
        logger.warning(
            "GitHub webhook: no usable registration for %s/%s — no tasks in scope", owner, repo
        )
        return []
    return team_ids_for_user(registered_by)


def _other_open_prs(task, pr_url: str) -> list[str]:
    """PRs other than `pr_url` still open on `task` (drafts included).

    A task delivered by several PRs (api + frontend + docs) is done when
    the last one merges, not the first (MIP-710). Only OPEN holds the
    close: a PR closed unmerged was abandoned, a branch with no PR is
    not a review anyone waits on, and UNKNOWN is a row nobody could read
    — holding on either would pin the task open with nothing left to
    merge. `pr_url` is excluded by value, not by state, so the PR that
    just merged never holds itself even when the delivery sync failed
    and its row still says open.
    """
    canonical = ""
    ref = parse_pr_url_full(pr_url)
    if ref is not None:
        canonical = canonical_pr_url(*ref)
    return list(
        TaskDelivery.objects.filter(task=task, state=TaskDeliveryState.OPEN)
        .exclude(pr_url=canonical)
        .exclude(pr_url__isnull=True)
        .order_by("pr_url")
        .values_list("pr_url", flat=True)
    )


def _close_tasks_for_merged_pr(head_ref: str, team_ids: list, pr_url: str = "") -> int:
    """Auto-close tasks referenced by a merged PR's head branch name.

    Resolution path: walk every task that has a project-scoped display
    ID (`project.code` + `project_task_number`) and check whether the
    head branch name contains that ID with the same word-boundary regex
    used for branch auto-linking. Tasks that match AND whose assignee
    has opted in via `auto_close_on_pr_merge` get bumped to DONE_STATUS.

    A task linked to the PR by a recorded delivery (`add_task_delivery`
    on a branch that names no task) is a match too. Otherwise, with the
    hold below, a task whose last PR is on such a branch would never
    close at all. Either way, a task with another PR still open is left
    as it is; the last of them to merge closes it (`_other_open_prs`).

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
    if not head_ref or not team_ids:
        return 0
    linked_ids: set = set()
    ref = parse_pr_url_full(pr_url)
    if ref is not None:
        # Same tenant boundary as the branch match below.
        linked_ids = set(
            TaskDelivery.objects.filter(
                team_id__in=team_ids, pr_url=canonical_pr_url(*ref)
            ).values_list("task_id", flat=True)
        )
    # Pre-filter to tasks that *could* have a display ID matched.
    # `select_related("project", "assignee")` keeps the per-row lookup
    # cheap; for a workspace with thousands of tasks this iterates in
    # SQL once, then the regex pass runs in memory.
    #
    # `team_id__in` is the tenant boundary — see `_repo_team_ids`. It is
    # first in the filter because without it this loop closes tasks in
    # any customer's install that happens to share a project code.
    candidates = (
        TaskMaster.objects.select_related("project", "assignee")
        .filter(
            team_id__in=team_ids,
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
        if task.task_id not in linked_ids:
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
        still_open = _other_open_prs(task, pr_url)
        if still_open:
            logger.info(
                "GitHub webhook: %s merged but task %s still has %d open PR(s) %s; not closing",
                pr_url,
                task.display_id,
                len(still_open),
                still_open,
            )
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
    head_ref: str, pr_url: str, comment: dict, comment_kind: str, team_ids: list
) -> int:
    """For each task whose `display_id` matches `head_ref`, create a
    `TaskActivity` row recording this PR comment. Idempotent by
    `(action_type, metadata.comment_id, task)` so webhook retries don't
    duplicate. Returns the count of rows actually created."""
    if not head_ref or not comment.get("id") or not team_ids:
        return 0
    # `team_id__in` is the tenant boundary — see `_repo_team_ids`. The
    # comment body is attacker-controlled and lands in the task's audit
    # feed, so an unscoped match wrote into someone else's tenant.
    candidates = TaskMaster.objects.select_related("project", "team").filter(
        team_id__in=team_ids,
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


def _record_pr_link_activity(head_ref: str, pr_url: str, number, title: str, team_ids: list) -> int:
    """For each task whose `display_id` is contained in `head_ref`, create
    a `PR_LINKED` activity row recording that this PR (opened on a branch
    carrying the task's display id) is linked to the task. PRs whose head
    branch does NOT contain a task's display id are ignored — that's the
    same auto-link rule used for branch chips and PR-comment activity.

    Idempotent by `(action_type, metadata.pr_url, task)`: re-deliveries and
    reopen events don't duplicate. `pr_url` (not the per-repo PR number) is
    the dedup key because the number collides across repos. Returns the
    count of rows actually created."""
    if not head_ref or not pr_url or not team_ids:
        return 0
    # `team_id__in` is the tenant boundary — see `_repo_team_ids`. The
    # row this writes carries an attacker-controlled `pr_title` into the
    # task's audit feed, so an unscoped match was stored content in
    # someone else's tenant.
    candidates = TaskMaster.objects.select_related("project", "team").filter(
        team_id__in=team_ids,
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


# `pull_request` actions that change something a `TaskDelivery` row
# records: whether the PR exists, its state, its title. `synchronize`
# (every push) is left out — it changes none of them, and each delivery
# sync scans the tenant's tasks for a display id.
_DELIVERY_ACTIONS = frozenset({"opened", "reopened", "closed", "edited"})


def _tasks_for_branch(head_ref: str, team_ids: list) -> list:
    """Tasks in `team_ids` whose display id `head_ref` carries — the
    branch-naming auto-link rule, as `_record_pr_link_activity` applies it."""
    if not head_ref or not team_ids:
        return []
    candidates = TaskMaster.objects.select_related("project").filter(
        team_id__in=team_ids,
        is_deleted=False,
        project__code__isnull=False,
        project_task_number__isnull=False,
    )
    return [
        task
        for task in candidates
        if not task.display_id.startswith("#")
        and _branch_match_re(task.display_id).search(head_ref)
    ]


def _sync_task_deliveries(owner: str, repo: str, pr: dict, team_ids: list) -> dict | None:
    """Record / update `TaskDelivery` rows for one `pull_request` event
    (MIP-687). Never lets a failure here cost the auto-close or the link
    activity that follow it: those are the older contract. (A 500 would
    NOT make GitHub retry the delivery; it would be lost, MIP-861.)"""
    try:
        head_ref = ((pr.get("head") or {}).get("ref")) or ""
        return sync_pull_request(
            owner=owner,
            repo=repo,
            pr=pr,
            matched_tasks=_tasks_for_branch(head_ref, team_ids),
            team_ids=team_ids,
        )
    except Exception:  # noqa: BLE001
        logger.exception("GitHub webhook: task delivery sync failed for %s/%s", owner, repo)
        return None


def apply_pull_request(owner: str, repo: str, pr: dict, action: str | None, team_ids: list) -> dict:
    """What a `pull_request` event does to Genos: sync the PR's delivery
    rows, then close the tasks it finished.

    Shared by `GithubWebhookView` and `reconcile_pr_deliveries` (MIP-861),
    so a close recovered from GitHub's REST state and one made by a live
    event follow ONE rule: `_close_tasks_for_merged_pr`, with its tenant
    scope (`team_ids`, from `_repo_team_ids`), the assignee's
    `auto_close_on_pr_merge` opt-in and the wait for every linked PR.

    `pr` is a webhook `pull_request` object or the REST pull object; both
    carry the fields read here. Returns `{"deliveries": counts | None,
    "updated": closed-task count | None}` (`updated` is None unless the
    event was a merge).
    """
    deliveries = None
    if action in _DELIVERY_ACTIONS:
        deliveries = _sync_task_deliveries(owner, repo, pr, team_ids)
    updated = None
    head_ref = ((pr.get("head") or {}).get("ref")) or ""
    if action == "closed" and pr.get("merged") and head_ref:
        updated = _close_tasks_for_merged_pr(head_ref, team_ids, pr.get("html_url") or "")
    return {"deliveries": deliveries, "updated": updated}


# Why a registration did not happen, as an HTTP status for the client.
_REGISTER_FAILURE_STATUS = {
    "not_connected": status.HTTP_400_BAD_REQUEST,
    "token_unavailable": status.HTTP_400_BAD_REQUEST,
    "no_admin": status.HTTP_403_FORBIDDEN,
    "repo_not_found": status.HTTP_404_NOT_FOUND,
    "adopt_failed": status.HTTP_409_CONFLICT,
}


class GithubRegisterRepoView(ObjectBodyMixin, APIView):
    """POST /api/v2/github/register-repo/  {owner, repo}

    Install Genos's PR webhook on a repo, or take over a hook someone added by
    hand (MIP-755). A hook added in GitHub's settings answers 200 and links
    nothing, because the registration row is what says which teams its events
    may write into, and only `ensure_repo_webhook` creates it. This is the
    explicit way to make one, using the CALLER's GitHub connection, so it needs
    the same rights as always: admin on the repo. On a 422 it adopts the
    existing hook and brings its events in line.

    The failure reason comes back as `detail`, so the page can say what to do.
    """

    permission_classes = [permissions.IsAuthenticated]

    def post(self, request: Request):
        owner = str(request.data.get("owner") or "").strip()
        repo = str(request.data.get("repo") or "").strip()
        if not is_valid_repo_ref(owner, repo):
            return Response({"detail": "invalid_repo"}, status=status.HTTP_400_BAD_REQUEST)

        registration, reason = register_repo_webhook(request.user, owner, repo)
        if registration is None:
            return Response(
                {"detail": reason},
                status=_REGISTER_FAILURE_STATUS.get(reason, status.HTTP_502_BAD_GATEWAY),
            )
        forget_unregistered_delivery(registration.owner, registration.repo)
        return Response(
            {
                "registered": True,
                "reason": reason,
                "owner": registration.owner,
                "repo": registration.repo,
            }
        )


class GithubUnregisteredReposView(APIView):
    """GET /api/v2/github/unregistered-repos/

    Repos that have sent Genos webhook events but are not registered (MIP-755),
    so the integrations page can say "these send events and link nothing" and
    offer to register them.

    The record carries no tenant, so WHO may see a repo is decided here by the
    caller's own GitHub token: a row is returned only if GitHub lets that token
    read the repo. Without it, one customer would see the names of another's
    private repos. Nobody connected, nobody sees anything.
    """

    permission_classes = [permissions.IsAuthenticated]

    # Most recent first, and few: each candidate costs one GitHub call.
    _MAX_CANDIDATES = 20

    def get(self, request: Request):
        account = _connected_account(request.user)
        if account is None:
            return Response({"repos": [], "connected": False})

        repos = []
        for row in GithubUnregisteredDelivery.objects.order_by("-ts_last_seen_at")[
            : self._MAX_CANDIDATES
        ]:
            # Already registered since (by another path): not "unknown" any more.
            if GithubWebhookRegistration.objects.filter(
                owner__iexact=row.owner, repo__iexact=row.repo
            ).exists():
                forget_unregistered_delivery(row.owner, row.repo)
                continue
            try:
                resp = _github_get(account, f"/repos/{row.owner}/{row.repo}")
            except Exception:  # noqa: BLE001 - one bad lookup must not hide the rest
                logger.warning("GitHub repo lookup failed for an unregistered repo", exc_info=True)
                continue
            if not resp.ok:
                continue  # not reachable with THIS token: not theirs to see
            perms = (resp.json() or {}).get("permissions") or {}
            repos.append(
                {
                    "owner": row.owner,
                    "repo": row.repo,
                    "event_count": row.event_count,
                    "last_event": row.last_event,
                    "last_seen_at": row.ts_last_seen_at.isoformat(),
                    "can_admin": bool(perms.get("admin")),
                }
            )
        return Response({"repos": repos, "connected": True})


@method_decorator(csrf_exempt, name="dispatch")
class GithubWebhookView(APIView):
    """Receive GitHub repo webhooks (`pull_request` events).

    Auto-transition: when a PR merges, any task whose display ID
    appears in the PR's head branch name (e.g. `feature/GEN-42-foo`
    closes task `GEN-42`) is bumped to "Closed" — *but only if the
    task's assignee has opted in* via the `auto_close_on_pr_merge`
    preference. Default is opt-out.

    We answer 200 once signature verification passes — even when no
    tasks matched — so GitHub shows the delivery as healthy. Signature
    failures get 401 so GitHub stops sending obvious garbage.

    GitHub does NOT retry a failed delivery on its own (the "Redeliver"
    button and the deliveries API are manual). A delivery that lands
    while this service is redeploying and gets a platform 500 is lost,
    so `manage.py reconcile_pr_deliveries` (MIP-861) recovers the state
    from the PR itself, through `apply_pull_request` below.
    """

    permission_classes = [permissions.AllowAny]
    authentication_classes: list = []

    def post(self, request: Request):
        raw_body = request.body
        sig = request.headers.get("X-Hub-Signature-256") or request.META.get(
            "HTTP_X_HUB_SIGNATURE_256"
        )
        # Parsed before verifying only to learn which repo's secret to
        # check against; nothing in it is acted on until that verifies.
        # RecursionError: deeply nested JSON from an unsigned request
        # must be a 401, not a 500.
        try:
            payload = json.loads(raw_body.decode("utf-8") or "{}")
        except (ValueError, RecursionError):
            payload = None
        if not isinstance(payload, dict):
            payload = None
        repo_ref = _payload_repo(payload)
        if not _verify_signature(raw_body, sig, _signing_secrets(repo_ref)):
            logger.warning("GitHub webhook: bad / missing signature")
            return Response({"detail": "invalid_signature"}, status=status.HTTP_401_UNAUTHORIZED)

        event = request.headers.get("X-GitHub-Event") or request.META.get("HTTP_X_GITHUB_EVENT")
        # A verified delivery from a repo Genos holds no registration for: it
        # answers 200 and writes nothing (`_repo_team_ids` fails closed), which
        # looks healthy from GitHub's side. Remember it so the integrations
        # page can offer to register the repo (MIP-755). The tenant guard is
        # untouched: this only records that the repo exists.
        if (
            repo_ref
            and not GithubWebhookRegistration.objects.filter(
                owner__iexact=repo_ref[0], repo__iexact=repo_ref[1]
            ).exists()
        ):
            record_unregistered_delivery(repo_ref[0], repo_ref[1], event or "")

        # Acknowledge ping (sent once when the webhook is created) so the
        # GitHub UI shows a green check. No body action needed.
        if event == "ping":
            return Response({"detail": "pong"}, status=status.HTTP_200_OK)

        if event not in ("pull_request", "issue_comment", "pull_request_review_comment"):
            return Response({"detail": "ignored"}, status=status.HTTP_200_OK)

        if payload is None:
            return Response({"detail": "bad_json"}, status=status.HTTP_400_BAD_REQUEST)

        if _pr_url_repo_mismatch(event, payload, repo_ref):
            logger.warning(
                "GitHub webhook: %s delivery signed for %s names a PR in another repo",
                event,
                "/".join(repo_ref) if repo_ref else "no repo",
            )
            return Response({"detail": "repo_mismatch"}, status=status.HTTP_400_BAD_REQUEST)

        if event == "pull_request":
            pr = payload.get("pull_request") or {}
            action = payload.get("action")
            merged = bool(pr.get("merged"))
            head_ref = ((pr.get("head") or {}).get("ref")) or ""
            html_url = pr.get("html_url") or ""

            # Drop our Redis-cached lookups of this repo's PRs (and, when a
            # PR is opened or reopened, its branch list) so the UI sees the
            # new state on the next fetch. Only for the actions that change
            # what we cache (state, draft, merged_at, title); a push to an
            # existing PR (`synchronize`) changes none of it, and used to
            # throw the branch list away on every push. Comment events skip
            # this too — they don't touch cached fields.
            ref = parse_pr_url_full(html_url)
            # Which tenant this repo may write into. Derived from the PR
            # url rather than trusted from the payload's `repository`
            # block for no deep reason — this is the value already parsed
            # here — but note both are attacker-supplied and the scope is
            # only as good as the registration lookup they feed.
            team_ids = []
            if ref is not None:
                owner_inv, repo_inv, number_inv = ref
                if action in _PR_DATA_ACTIONS:
                    _invalidate_pr_caches(owner_inv, repo_inv, number_inv, head_ref or None)
                elif action in _PR_DETAIL_ONLY_ACTIONS:
                    _invalidate_pr_detail(owner_inv, repo_inv)
                if action in _BRANCH_LIST_ACTIONS:
                    _invalidate_branch_list(owner_inv, repo_inv)
                team_ids = _repo_team_ids(owner_inv, repo_inv)

            deliveries = None
            updated = None
            if ref is not None:
                # The same two steps `reconcile_pr_deliveries` runs, so a
                # recovered event and a live one close tasks by one rule.
                applied = apply_pull_request(owner_inv, repo_inv, pr, action, team_ids)
                deliveries, updated = applied["deliveries"], applied["updated"]

            # MVP: only the merge transition fires. Other action types
            # (opened, ready_for_review, closed-unmerged, etc.) are
            # acknowledged but not acted on.
            if action == "closed" and merged and head_ref:
                updated = updated or 0
                logger.info(
                    "GitHub webhook: PR merged %s (head=%s) → updated %d task(s)",
                    html_url,
                    head_ref,
                    updated,
                )
                return Response(
                    {"detail": "ok", "updated": updated, "deliveries": deliveries},
                    status=status.HTTP_200_OK,
                )

            # Record a link activity when a PR is opened (or reopened) on a
            # branch whose name carries a task's display id. Idempotent, so
            # a reopen after this already fired won't duplicate the row.
            if action in ("opened", "reopened") and head_ref:
                pr_number = pr.get("number")
                pr_title = pr.get("title") or ""
                linked = _record_pr_link_activity(head_ref, html_url, pr_number, pr_title, team_ids)
                logger.info(
                    "GitHub webhook: PR %s %s (head=%s) → linked %d task(s)",
                    action,
                    html_url,
                    head_ref,
                    linked,
                )
                return Response(
                    {"detail": "ok", "linked": linked, "deliveries": deliveries},
                    status=status.HTTP_200_OK,
                )

            if deliveries is not None:
                return Response(
                    {"detail": "ok", "deliveries": deliveries}, status=status.HTTP_200_OK
                )
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

        # The `review` branch above never parses the PR url, so resolve
        # the tenant from it here rather than in each branch — both
        # arrive with a `pr_url` and nothing else identifying the repo.
        comment_ref = parse_pr_url(pr_url)
        team_ids = _repo_team_ids(*comment_ref) if comment_ref else []

        recorded = _record_pr_comment_activity(head_ref, pr_url, comment, comment_kind, team_ids)
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
# Cache scope: ONE CONNECTED GITHUB ACCOUNT, never the repo alone.
# The repos scanned are every `GithubWebhookRegistration` in the install,
# not the repos the requesting user can see (a registration only says some
# user once pasted a PR from that repo), and every call is made with the
# REQUESTING user's token. A private repo can therefore answer 200 for one
# user and 404 for the next; a repo-only key handed the first user's branch
# names, commit SHAs and PR titles to everyone whose token could not read
# them (MIP-974). So every key carries
#
#   * the connected account's id AND its `ts_updated_at`: reconnecting
#     GitHub writes the row, so a new token gets fresh keys, and a hit
#     never has to decrypt a token;
#   * the repo's GENERATION (below).
#
# GitHub's "no" is cached per account too (404, or a 403 that is not a rate
# limit), for `_DENIED_CACHE_TTL`: the answer is stable, and every task
# open used to pay it again for each repo the user can't read. A rate
# limit, a 5xx or a timeout says nothing about access and is never cached.
#
# Invalidation. A webhook event can no longer name the keys to delete
# (it does not know which accounts have entries), so each repo carries three
# generation numbers that are part of the keys: one for its PR lookups, one
# for its branch list, one for its PR detail. A bump orphans everything cached under that number
# for that repo, whichever account wrote it. The helpers read the
# generation BEFORE calling GitHub and write under that same key, so a
# fetch that straddles a bump lands under a key nobody reads again.
#
# What an event bumps (see `_invalidate_pr_caches`, `_invalidate_branch_list`):
# a PR changing state, draft flag or title drops the PR lookups; a PR being
# opened or reopened also drops the branch list; a push (`synchronize`)
# changes neither, so a busy branch no longer throws the branch list away.
# The PR detail (`GithubPullDetailView`, with the head commit's CI) is dropped
# by all of those AND by a push (`_invalidate_pr_detail`), since a push is what
# changes it. What no event reaches is a CI run finishing: GitHub sends this
# webhook no `check_run` or `status`, so an open PR's detail is only ever as
# fresh as `_PULL_DETAIL_TTL`.
# GitHub sends no event when a branch is created or deleted, so the branch
# list is otherwise only ever refreshed by its TTL: a new branch shows up, and
# a deleted one disappears, within `_BRANCH_CACHE_TTL`, and its head
# `commit_sha` can be that old.
#
# A miss fetches the registered repos in parallel (`_get_all`), so it costs
# about one GitHub round trip however many repos are registered.

_BRANCH_CACHE_TTL = 5 * 60  # seconds; per repo and account, shared by every task
_PULL_CACHE_TTL = 60  # matches the frontend prStatusCache
_PULL_DETAIL_TTL = 60  # an open PR's detail, or one whose CI is still moving
_PULL_DETAIL_MERGED_TTL = 30 * 60  # a merged PR with finished CI
# Most repos fetched at once on a miss. Per request, so the process-wide
# ceiling is this times the concurrent requests that miss.
_FETCH_MAX_WORKERS = 6
# How long "this account can't read that" is remembered. `fresh=1` skips it.
_DENIED_CACHE_TTL = 10 * 60

# Stored in place of a payload when GitHub refused the account. A real
# answer is never a dict with this key (a branch list is a list, "no PR"
# is `{}`, a PR is a slim dict).
_DENIED = {"github_refused": True}


def _is_denied(cached) -> bool:
    return isinstance(cached, dict) and cached.get("github_refused") is True


def _account_scope(account) -> str:
    stamp = account.ts_updated_at
    return f"{account.id}.{int(stamp.timestamp() * 1_000_000) if stamp else 0}"


# The three counters a repo has: its PR lookups, its branch list, and its PR
# detail (`GithubPullDetailView`).
_PRS = "prs"
_BRANCHES = "branches"
_PR_DETAIL = "pr_detail"
# `pull_request` webhook actions that change what the PR lookups cache
# (state, draft flag, merged_at, title), and the ones that may bring a branch
# the cached branch list does not have. `synchronize` is in neither.
_PR_DATA_ACTIONS = frozenset(
    {"opened", "reopened", "closed", "edited", "ready_for_review", "converted_to_draft"}
)
_BRANCH_LIST_ACTIONS = frozenset({"opened", "reopened"})
# A push changes what the detail shows (head commit, CI, size) but not what the
# lookups cache, so it drops the detail alone. Every action in
# `_PR_DATA_ACTIONS` drops it too (`_invalidate_pr_caches`).
_PR_DETAIL_ONLY_ACTIONS = frozenset({"synchronize"})


def _generation_key(which: str, owner: str, repo: str) -> str:
    return f"gh:v2:gen:{which}:{owner.lower()}:{repo.lower()}"


def _bump_repo_generation(which: str, owner: str, repo: str) -> None:
    # A fresh timestamp rather than `incr`: it needs no read-modify-write,
    # works on a missing key, and two bumps never collide. No expiry, so the
    # key is never the first thing a `volatile-*` eviction policy drops. A
    # repo's missing counter reads as 0 (also if Redis loses the key, which
    # can only resurrect entries no older than their TTL).
    cache.set(_generation_key(which, owner, repo), time.time_ns(), None)


def _scoped_key(
    kind: str, which: str, account, owner: str, repo: str, *rest, gen: int | None = None
) -> str:
    if gen is None:
        gen = cache.get(_generation_key(which, owner, repo)) or 0
    tail = "".join(f":{part}" for part in rest)
    return f"gh:v2:{kind}:{_account_scope(account)}:{gen}:{owner.lower()}:{repo.lower()}{tail}"


def _branch_cache_key(account, owner: str, repo: str, *, gen: int | None = None) -> str:
    return _scoped_key("branches", _BRANCHES, account, owner, repo, gen=gen)


def _pull_cache_key(account, owner: str, repo: str, branch: str) -> str:
    return _scoped_key("head_pull", _PRS, account, owner, repo, branch)


def _pr_by_number_cache_key(account, owner: str, repo: str, number: int) -> str:
    return _scoped_key("pr_by_number", _PRS, account, owner, repo, number)


_PR_NUMBER = re.compile(r"[0-9]{1,9}")


def _pull_detail_cache_key(account, owner: str, repo: str, number: str) -> str | None:
    """The key of one PR's detail for this account, or None when the URL does
    not name a PR (a repo or number GitHub cannot have): the request then
    goes to GitHub as it always did, and nobody can fill Redis with keys made
    of whatever they put in the path."""
    if not is_valid_repo_ref(owner, repo) or not _PR_NUMBER.fullmatch(number):
        return None
    return _scoped_key("pr_detail", _PR_DETAIL, account, owner, repo, int(number))


def _ci_settled(combined, checks) -> bool:
    """Has CI nothing left to report for this commit? Same reading as the
    badge (`deriveCiState`): a check run that is not `completed` is pending,
    and so is a combined status whose state is `pending` when it has
    statuses. With NO statuses GitHub reports `pending` too ("pending if
    there are no statuses"), which is not a signal. Unknown counts as not
    settled: a missing answer, or check runs beyond the first page."""
    if not isinstance(combined, dict) or not isinstance(checks, dict):
        return False
    runs = checks.get("check_runs")
    total = checks.get("total_count")
    if not isinstance(runs, list) or not isinstance(total, int) or total > len(runs):
        return False
    if any(not isinstance(run, dict) or run.get("status") != "completed" for run in runs):
        return False
    return not ((combined.get("total_count") or 0) > 0 and combined.get("state") == "pending")


def _pull_detail_ttl(pr: dict, combined, checks) -> int:
    """How long a PR's detail may be served from Redis.

    Only a MERGED PR whose CI has settled is final: it cannot be reopened,
    and nothing else on it changes (a check re-run on a merged commit is the
    one exception, bounded by this TTL). Everything else is `_PULL_DETAIL_TTL`:
    an open PR's CI moves with no event at all (GitHub sends no `check_run` or
    `status` to this webhook), and a closed one can be reopened, which a lost
    delivery would not tell us."""
    if (pr.get("merged") or pr.get("merged_at")) and _ci_settled(combined, checks):
        return _PULL_DETAIL_MERGED_TTL
    return _PULL_DETAIL_TTL


def _is_rate_limited(resp) -> bool:
    """Is this failure GitHub throttling us rather than answering? Primary
    limits come as 403 (or 429) with `X-RateLimit-Remaining: 0`, secondary
    ones with `Retry-After` and/or a message saying so."""
    if resp.status_code == 429:
        return True
    headers = getattr(resp, "headers", None)
    if isinstance(headers, Mapping) and (
        headers.get("Retry-After") is not None or headers.get("X-RateLimit-Remaining") == "0"
    ):
        return True
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — not JSON: nothing more to read
        return False
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        return False
    message = message.lower()
    return "rate limit" in message or "abuse detection" in message


def _is_refusal(resp) -> bool:
    """GitHub says THIS ACCOUNT cannot read the thing: 404 (also how it
    hides a private repo) or a 403 that is not a rate limit. Anything else
    (429, rate-limit 403, 5xx, 401) is transient or not about access."""
    if resp.status_code == 404:
        return True
    return resp.status_code == 403 and not _is_rate_limited(resp)


def _note_failed_lookup(account) -> None:
    """A GitHub lookup failed in a way that says nothing about access, so its
    caller's "no PR" is really "could not look" (MIP-1412)."""
    if isinstance(account, _GithubCredential):
        account.failed_lookups += 1


def _call_github(account, path: str, params: dict | None = None):
    """One GitHub call from the request thread, tallied on the request's
    credential (if it has one)."""
    if isinstance(account, _GithubCredential):
        account.upstream += 1
    return _github_get(account, path, params)


def _get_all(account: _GithubCredential, calls: list[tuple[str, dict | None]]):
    """GET each `(path, params)` from GitHub and return the answers in the
    order asked. More than one runs on a small pool, so N repos cost one
    round trip, not N.

    The workers only call `_github_get`, with a token resolved here first:
    no ORM, no cache, no logging off the request thread. An exception in a
    call is raised from here once the others have finished, as it was when
    the calls ran one by one."""
    account.resolve()
    account.upstream += len(calls)
    if len(calls) == 1:
        return [_github_get(account, *calls[0])]
    with ThreadPoolExecutor(
        max_workers=min(_FETCH_MAX_WORKERS, len(calls)), thread_name_prefix="gh-links"
    ) as pool:
        futures = [pool.submit(_github_get, account, path, params) for path, params in calls]
        return [future.result() for future in futures]


def _branch_lists(
    account, repos: list[tuple[str, str]], *, bypass_cache: bool = False
) -> list[list[dict] | None]:
    """Each repo's branch list, in the order of `repos`: None where GitHub
    refused this account or errored (the caller skips the repo).

    Cached lists come from two Redis round trips for all the repos together;
    the repos that miss are fetched in parallel.

    When `bypass_cache=True`, skips the read but still writes the fresh
    result so subsequent natural fetches within the TTL benefit.
    """
    if not repos:
        return []
    account = _credential(account)
    gen_keys = [_generation_key(_BRANCHES, owner, repo) for owner, repo in repos]
    gens = cache.get_many(gen_keys) or {}
    keys = [
        _branch_cache_key(account, owner, repo, gen=gens.get(gen_key) or 0)
        for (owner, repo), gen_key in zip(repos, gen_keys)
    ]
    cached = {} if bypass_cache else (cache.get_many(keys) or {})
    out: list[list[dict] | None] = [None] * len(repos)
    misses: list[int] = []
    for i, key in enumerate(keys):
        value = cached.get(key)
        if value is None:
            misses.append(i)
        elif not _is_denied(value):
            out[i] = value
    if not misses:
        return out
    answers = _get_all(
        account,
        [(f"/repos/{repos[i][0]}/{repos[i][1]}/branches", {"per_page": 100}) for i in misses],
    )
    for i, resp in zip(misses, answers):
        if resp.ok:
            out[i] = resp.json() or []
            cache.set(keys[i], out[i], _BRANCH_CACHE_TTL)
        elif _is_refusal(resp):
            cache.set(keys[i], _DENIED, _DENIED_CACHE_TTL)
        else:
            account.failed_repos.add(repos[i])
    return out


def _list_repo_branches(
    account,
    owner: str,
    repo: str,
    *,
    bypass_cache: bool = False,
) -> list[dict] | None:
    """Cached `GET /repos/{o}/{r}/branches` for one repo. Returns None on
    upstream error so callers can skip the repo silently."""
    return _branch_lists(account, [(owner, repo)], bypass_cache=bypass_cache)[0]


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
    key = _pull_cache_key(account, owner, repo, branch)
    if not bypass_cache:
        cached = cache.get(key)
        if cached is not None:
            if _is_denied(cached):
                return None
            # Cached payload may legitimately be the sentinel `{}` meaning
            # "this branch has no PR" — normalize that back to None.
            return cached or None
    resp = _call_github(
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
        if _is_refusal(resp):
            cache.set(key, _DENIED, _DENIED_CACHE_TTL)
        else:
            _note_failed_lookup(account)
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
    key = _pr_by_number_cache_key(account, owner, repo, number)
    if not bypass_cache:
        cached = cache.get(key)
        if cached is not None:
            # A refusal is stored as a marker (see `_DENIED`), so a
            # transient failure is never mistaken for one and is not
            # cached at all.
            return None if _is_denied(cached) else (cached or None)
    resp = _call_github(account, f"/repos/{owner}/{repo}/pulls/{number}")
    if not resp.ok:
        if _is_refusal(resp):
            cache.set(key, _DENIED, _DENIED_CACHE_TTL)
        else:
            _note_failed_lookup(account)
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
    """Make the next PR lookup for this repo go to GitHub (rather than
    waiting up to the TTL for the new PR state to show). The branch list is
    NOT touched: see `_invalidate_branch_list`.

    Called from the `pull_request` webhook dispatch for the actions that
    change what we cache (state, draft, title: not `synchronize`) and from
    `reconcile_pr_deliveries`. Comment events (`issue_comment`,
    `pull_request_review_comment`) don't trigger this because they don't
    mutate any of the fields we cache.

    The lookups are cached per GitHub account, so their keys cannot be
    named from here; bumping the repo's PR generation orphans all of them at
    once. That is repo-wide: an event for one PR refreshes the repo's other
    PR lookups too, once. `head_ref` is kept for the callers' sake; nothing
    is keyed by it any more. The one per-PR key we still delete is the
    webhook's own head-ref lookup (`_pr_head_ref_cache_key`), which is not
    per-user and is never served to a user.

    Best-effort: a cache failure logs but never propagates — a missed
    invalidation just means an extra TTL cycle of staleness.
    """
    try:
        _bump_repo_generation(_PRS, owner, repo)
        _bump_repo_generation(_PR_DETAIL, owner, repo)
        if number is not None:
            cache.delete(_pr_head_ref_cache_key(owner, repo, number))
        logger.info("ensure_repo_webhook: invalidated PR caches for %s/%s#%s", owner, repo, number)
    except Exception:
        logger.exception("PR cache invalidation failed for %s/%s#%s", owner, repo, number)


def _invalidate_pr_detail(owner: str, repo: str) -> None:
    """Drop the repo's cached PR details (every account's) and nothing else.
    Called for a push to a PR (`synchronize`): the head commit and its CI
    changed, the state the PR lookups cache did not."""
    try:
        _bump_repo_generation(_PR_DETAIL, owner, repo)
    except Exception:
        logger.exception("PR detail invalidation failed for %s/%s", owner, repo)


def _invalidate_branch_list(owner: str, repo: str) -> None:
    """Drop the repo's cached branch lists (every account's). Called when a
    PR is opened or reopened, which usually means a branch was just pushed;
    never for a push to an existing PR (`synchronize`)."""
    try:
        _bump_repo_generation(_BRANCHES, owner, repo)
    except Exception:
        logger.exception("Branch list invalidation failed for %s/%s", owner, repo)


def _collect_repo_branches(
    account,
    repos,
    *,
    bypass_cache: bool = False,
) -> list[tuple[str, str, list[dict]]]:
    """Fetch each registered repo's branch list once (Redis-cached; the
    misses in parallel). Repos whose branch listing errors (404 no access /
    403 rate limit) are skipped silently so one bad repo doesn't poison the
    result. Output is in the order of `repos`."""
    repos = list(repos)
    lists = _branch_lists(account, repos, bypass_cache=bypass_cache)
    return [
        (owner, repo, branches)
        for (owner, repo), branches in zip(repos, lists)
        if branches is not None
    ]


def _log_links(route: str, account: _GithubCredential) -> None:
    """One line per request that reached the cache: `hit` when every answer
    came from Redis, `miss` when GitHub was called, and how many calls."""
    logger.info(
        "gh.links route=%s outcome=%s upstream=%d",
        route,
        "miss" if account.upstream else "hit",
        account.upstream,
    )


def _pulls_for_task(
    account: ConnectedAccount,
    task,
    display_id: str,
    repo_branches: list[tuple[str, str, list[dict]]],
    *,
    bypass_cache: bool = False,
) -> list[dict]:
    """Resolve the auto-linked PRs for one task against pre-fetched
    branch lists. Shared by the single and batched endpoints so the
    two sources (live branch walk + persisted `task.links` auto-links)
    and the html_url dedupe behave identically — see
    `GithubPullsForTaskView`'s docstring for the source semantics."""
    pattern = _branch_match_re(display_id)
    pulls: list[dict] = []
    seen_urls: set[str] = set()
    # --- Source 1: live branches matching display_id -------------
    for owner, repo, branches in repo_branches:
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

    return pulls


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
        if not can_access_task(task_id, request.user.id):
            return Response({"detail": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)
        resolved, err = _resolve_task_display_id(task_id)
        if err is not None:
            return err
        if resolved is None:
            return Response({"branches": []})
        _task, display_id = resolved

        account = _connected_account(request.user)
        if account is None:
            return Response({"branches": []})
        account = _GithubCredential(account)

        pattern = _branch_match_re(display_id)
        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()
        bypass_cache = request.GET.get("fresh") == "1"

        matches: list[dict] = []
        # A repo that answers 404 (no access) / 403 (rate limit) / etc. is
        # left out, so one bad repo doesn't poison the list.
        for owner, repo, branches in _collect_repo_branches(
            account, repos, bypass_cache=bypass_cache
        ):
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
        _log_links("branches_for_task", account)
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
        # The task id is a walkable integer, and the response carries the
        # task's display id — which is the project code — plus PR titles
        # and branch names.
        #
        # 404, matching what this endpoint already returns for an unknown
        # id, so "no such task" and "not yours" are indistinguishable.
        # Answering `{"pulls": []}` here instead would have been an
        # existence oracle: 200 for a real task, 404 for a made-up one.
        if not can_access_task(task_id, request.user.id):
            return Response({"detail": "task_not_found"}, status=status.HTTP_404_NOT_FOUND)
        resolved, err = _resolve_task_display_id(task_id)
        if err is not None:
            return err
        if resolved is None:
            return Response({"pulls": []})
        task, display_id = resolved

        account = _connected_account(request.user)
        if account is None:
            return Response({"pulls": []})
        account = _GithubCredential(account)

        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()
        bypass_cache = request.GET.get("fresh") == "1"
        repo_branches = _collect_repo_branches(account, repos, bypass_cache=bypass_cache)
        pulls = _pulls_for_task(account, task, display_id, repo_branches, bypass_cache=bypass_cache)
        _log_links("pulls_for_task", account)
        return Response({"pulls": pulls})


class GithubPullsForTasksView(APIView):
    """Batched variant of `GithubPullsForTaskView`:
    `GET /api/v2/github/pulls/for-tasks/?task_ids=1,2,3`.

    One request resolves the PR column for a whole table paint instead
    of one request per row. The repo/branch walk is hoisted out of the
    per-task loop — each registered repo's branch list is fetched once
    per request (Redis-cached), then matched against every task's
    display ID. Per-task semantics (source union, branch-match
    backstop, html_url dedupe) are shared with the single view via
    `_pulls_for_task`.

    Response: `{"pulls_by_task": {"<taskId>": [slim pull dicts]}, "incomplete": ["<taskId>", ...]}`.
    Every requested id gets a key; unknown ids, tasks without a
    project-scoped display ID, and tasks with no PRs all map to `[]`
    (unlike the single view's 404) so one bad id can't fail the batch
    and the client can cache negatives uniformly.

    `incomplete` (MIP-1412) names the tasks whose list may be SHORT because a
    GitHub call failed in a way that says nothing about access: a 5xx, a rate
    limit, a 401, or a repo whose branch list could not be read (that one makes
    every task incomplete: the branch we missed may be theirs). For those, `[]`
    means "could not look", not "no PR". A REFUSAL (404, a 403 that is not a rate
    limit) is not in it: it is the true answer for this account, and is
    remembered for `_DENIED_CACHE_TTL`. A failed lookup is never cached, so the
    next request retries it. The key is on every answer, empty when all is
    well, so a client can tell a server that reports it from one that cannot.
    """

    permission_classes = [permissions.IsAuthenticated]

    # Lower than burndown's 500: each task can trigger per-branch PR
    # lookups on a cold cache, so the worst-case upstream fan-out per
    # request is larger.
    MAX_TASK_IDS = 200

    def get(self, request: Request):
        raw_ids = request.GET.get("task_ids") or ""
        try:
            task_ids = sorted({int(p) for p in raw_ids.split(",") if p.strip()})
        except ValueError:
            return Response(
                {"detail": "task_ids must be a comma-separated list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(task_ids) > self.MAX_TASK_IDS:
            return Response(
                {"detail": f"Too many task_ids (max {self.MAX_TASK_IDS})."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        pulls_by_task: dict[str, list[dict]] = {str(tid): [] for tid in task_ids}
        if not task_ids:
            return Response({"pulls_by_task": pulls_by_task, "incomplete": []})

        account = _connected_account(request.user)
        if account is None:
            return Response({"pulls_by_task": pulls_by_task, "incomplete": []})

        with_display: list[tuple[TaskMaster, str]] = []
        # Same walkable-id batch as the task-dependency endpoint closed
        # in #255: narrow rather than refuse, so one stray id does not
        # blank the whole PR column.
        task_ids = [t for t in task_ids if can_access_task(t, request.user.id)]
        for task in TaskMaster.objects.filter(task_id__in=task_ids).select_related("project"):
            display_id = task.display_id
            if not display_id or display_id.startswith("#"):
                continue
            with_display.append((task, display_id))
        if not with_display:
            return Response({"pulls_by_task": pulls_by_task, "incomplete": []})
        account = _GithubCredential(account)

        repos = GithubWebhookRegistration.objects.values_list("owner", "repo").distinct()
        bypass_cache = request.GET.get("fresh") == "1"
        repo_branches = _collect_repo_branches(account, repos, bypass_cache=bypass_cache)

        incomplete: list[str] = []
        for task, display_id in with_display:
            failed_before = account.failed_lookups
            pulls_by_task[str(task.task_id)] = _pulls_for_task(
                account, task, display_id, repo_branches, bypass_cache=bypass_cache
            )
            # A repo whose branch list could not be read may hold this task's
            # branch, so then no task can be called complete.
            if account.failed_repos or account.failed_lookups > failed_before:
                incomplete.append(str(task.task_id))
        _log_links("pulls_for_tasks", account)
        return Response({"pulls_by_task": pulls_by_task, "incomplete": sorted(incomplete, key=int)})
