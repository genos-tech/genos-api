"""Read-only GitHub lookups for the "needs you" PR signals through the GitHub App (MIP-2062).

A `check_run` / `status` delivery names a commit, not the pull request, and "approved and green"
needs the reviews and the CI of the head commit. This reads them with an installation token narrowed
to the ONE repository and to the three read permissions the lookups need (`pull_requests`, `checks`,
`statuses`: all inside the App's read set, `github_app.ALLOWED_PERMISSIONS`). The token is used for
the request and dropped. Nothing is written to GitHub.

Every method returns None when GitHub could not answer (no token, a timeout, a non-2xx, a body that
is not what was asked for). A caller treats None as "unknown": it writes nothing and says nothing,
so a GitHub hiccup can cost a signal but never invent one.

REQUESTS PER EVENT (bounded, no loops over a list of PRs beyond the PRs the event itself names):
  * a failing check on a repo with an open linked PR, and a passing check on a PR that already has
    an "approved" notice or an unread "CI failing" one: `pulls_for_commit` (1, cached a minute per
    commit); a passing check then also `reviews`, `check_runs`, `combined_status` (3);
  * a submitted approval: `reviews`, `check_runs`, `combined_status` (3; the PR is in the payload).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import requests
from django.core.cache import cache

from origin.services import github_app

logger = logging.getLogger("origin.pr_signal_reader")

_PERMISSIONS = {"pull_requests": "read", "checks": "read", "statuses": "read"}
_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
#: How long the PRs of one commit are remembered (a burst of `status` events names one commit).
_PULLS_FOR_COMMIT_TTL = 60
_REVIEWS_PAGE = 100
#: Review states that count (GitHub reports them upper case through REST).
_APPROVED = "APPROVED"
_CHANGES = "CHANGES_REQUESTED"
#: Check conclusions that are not a failure of the commit.
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})


class InstallationReader:
    """The lookups for ONE installation. Build it once per delivery."""

    def __init__(self, installation_id: int):
        self.installation_id = installation_id

    # -- plumbing -------------------------------------------------------------------------------

    def _get(self, owner: str, repo: str, path: str, params: dict | None = None) -> Any | None:
        if not (_NAME.match(owner or "") and _NAME.match(repo or "")):
            return None
        try:
            token = github_app.get_installation_token(
                self.installation_id, repositories=[repo], permissions=_PERMISSIONS
            )
            resp = requests.get(
                f"{github_app.API_ROOT}/repos/{owner}/{repo}{path}",
                params=params,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": github_app.API_VERSION,
                },
                timeout=github_app.TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except github_app.GithubAppError as exc:
            logger.warning("PR signal lookup: token failed: %s", exc)
            return None
        except requests.RequestException as exc:
            logger.warning("PR signal lookup failed: %s", type(exc).__name__)
            return None
        if not resp.ok:
            logger.info("PR signal lookup: GitHub answered %s for %s", resp.status_code, path)
            return None
        try:
            return resp.json()
        except ValueError:
            return None

    # -- lookups --------------------------------------------------------------------------------

    def reviews(self, owner: str, repo: str, number: int) -> list | None:
        body = self._get(owner, repo, f"/pulls/{int(number)}/reviews", {"per_page": _REVIEWS_PAGE})
        # A full page may have more behind it (oldest first): unknown, never guessed from a prefix.
        return body if isinstance(body, list) and len(body) < _REVIEWS_PAGE else None

    def check_runs(self, owner: str, repo: str, sha: str) -> dict | None:
        if not _SHA.match(sha or ""):
            return None
        body = self._get(owner, repo, f"/commits/{sha}/check-runs", {"per_page": 100})
        return body if isinstance(body, dict) else None

    def combined_status(self, owner: str, repo: str, sha: str) -> dict | None:
        if not _SHA.match(sha or ""):
            return None
        body = self._get(owner, repo, f"/commits/{sha}/status")
        return body if isinstance(body, dict) else None

    def pulls_for_commit(self, owner: str, repo: str, sha: str) -> list[dict] | None:
        """The OPEN pull requests whose head is `sha`, slimmed to what the signals read (number,
        title, draft, author id, head sha and branch). A `check_run` of a fork PR names no PR and a
        `status` never does, so every CI event asks here. Remembered for a minute: a burst of events
        names one commit."""
        if not _SHA.match(sha or ""):
            return None
        key = f"pr-signal:pulls:{owner.lower()}/{repo.lower()}:{sha.lower()}"
        cached = cache.get(key)
        if cached is not None:
            return list(cached)
        body = self._get(owner, repo, f"/commits/{sha}/pulls", {"per_page": 30})
        if not isinstance(body, list):
            return None
        pulls = [slim_pull(pr) for pr in body if isinstance(pr, dict)]
        pulls = [pr for pr in pulls if pr and pr["state"] == "open" and pr["sha"] == sha]
        cache.set(key, pulls, _PULLS_FOR_COMMIT_TTL)
        return pulls


def slim_pull(pr: dict) -> dict | None:
    """The fields of a GitHub PR object (REST or webhook) the signals read, or None when it is not
    usable."""
    number = pr.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    user = pr.get("user") if isinstance(pr.get("user"), dict) else {}
    return {
        "number": number,
        "title": str(pr.get("title") or ""),
        "state": "merged" if pr.get("merged") else str(pr.get("state") or ""),
        "draft": bool(pr.get("draft")),
        "author_id": user.get("id"),
        "sha": str(head.get("sha") or ""),
        "ref": str(head.get("ref") or ""),
    }


# ---- reading what GitHub answered ---------------------------------------------------------------


def review_decision(reviews: list) -> str | None:
    """`approved`, `changes_requested` or None, from a PR's reviews: each reviewer's LAST review that
    is an approval, a change request or a dismissal counts (a comment does not replace a verdict);
    any outstanding change request wins; at least one approval is needed to be `approved`."""
    verdict: dict[Any, str] = {}
    for review in reviews:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        reviewer = (review.get("user") or {}).get("id")
        if reviewer is None:
            continue
        if state in (_APPROVED, _CHANGES):
            verdict[reviewer] = state
        elif state == "DISMISSED":
            verdict.pop(reviewer, None)
    if _CHANGES in verdict.values():
        return "changes_requested"
    if _APPROVED in verdict.values():
        return "approved"
    return None


def ci_is_green(combined: Any, checks: Any) -> bool:
    """CI has settled green for the commit: every check run is completed with a passing conclusion,
    the combined status is `success` when it has statuses, and there is at least ONE check run or
    status (a commit nothing ran on is not "green"). Unknown, truncated (more check runs than were
    read) or pending is not green: a missing answer never produces the signal."""
    if not isinstance(combined, dict) or not isinstance(checks, dict):
        return False
    runs, total = checks.get("check_runs"), checks.get("total_count")
    if not isinstance(runs, list) or not isinstance(total, int) or total > len(runs):
        return False
    for run in runs:
        if not isinstance(run, dict) or run.get("status") != "completed":
            return False
        if run.get("conclusion") not in _PASSING_CONCLUSIONS:
            return False
    statuses = combined.get("total_count") or 0
    if statuses and combined.get("state") != "success":
        return False
    return bool(runs) or bool(statuses)
