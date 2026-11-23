"""Recover the PR events a deploy swallowed (MIP-861).

    python manage.py reconcile_pr_deliveries
    python manage.py reconcile_pr_deliveries --dry-run

GitHub delivers a `pull_request` event once. One that lands while
`backend-django` is redeploying gets the platform's 500, GitHub does not
retry it, and nothing in Genos noticed: the delivery row stayed `open`,
the task never auto-closed, and (because `_other_open_prs` waits for every
linked PR) the stale row held the task's LATER merges open too.

For every registered repo this asks GitHub about each PR that Genos still
believes is open and, if it has closed, applies what the webhook would have
done, through the same `apply_pull_request`: the delivery rows take the PR's
state, and a merge closes the tasks it finished (same tenant scope, same
`auto_close_on_pr_merge` opt-in, same wait for every linked PR).

Deliberately narrow, so it only ever acts on a transition:

* Only `open` rows are read. A "close whatever is merged" sweep would
  re-close, every run, a task somebody reopened.
* `unknown` rows (backfilled, or linked by a user with no GitHub
  connection) are left alone: they would close old tasks in bulk.
* With `GITHUB_APP_ENABLED`, a repo the GitHub App covers (an active installation linked to a
  team lists it, MIP-2194) is read with a read-only installation token for that one repo, and
  needs no `GithubWebhookRegistration` row: the repos watched are the registrations plus the
  repositories of the linked installations. Any other repo, and a covered one whose token cannot
  be minted, is read with the registrar's token (`hook_admin_headers`), the one the hook was
  installed with. A repo with neither is skipped with a WARNING, not an ERROR, or one revoked token
  would fail the cron on every run; its tasks stay open until someone reconnects.
* The teams a repo's PR state is applied for are the teams the App serves for it plus, while a
  registration row exists, the registrar's teams (what the legacy hook wrote for): each is a scope
  its own path already authorizes.
* GitHub answering 401 / 403 / 429 (or erroring, or not answering) stops
  that repo for this run; the next run tries again. Other repos go on.

With `GITHUB_APP_ENABLED` it first asks the GitHub App's deliveries API to redeliver the App
webhook's transient failures (`github_app_redeliver`, MIP-2088; needs the App's secrets on this
service) and prunes the replay-guard rows.

With `PR_NUDGES_ENABLED` it also hands each PR that is still open to
`pr_merge_readiness.check_open_pr` (MIP-2056): the "ready to merge" and "no activity" task comments
and the cached `merge_readiness` that `get_task` shows. No new service: this cron has the token and
the open PRs already.

Runs every 15 minutes (`.railway/railway.ts`, service
`reconcile-pr-deliveries`), which bounds a missed close to about that.
"""

from __future__ import annotations

import logging

import requests

from origin.management.cron_command import CronCommand
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.services import github_app
from origin.services import pr_merge_readiness as readiness
from origin.services.github_app_redeliver import redeliver_failed_deliveries
from origin.services.github_app_webhook import app_team_ids_for_repo, prune_deliveries
from origin.services.github_credential import installation_token
from origin.services.github_watched_repos import app_repos
from origin.services.github_webhooks import GITHUB_API_BASE, github_api_headers, hook_admin_headers
from origin.views.common.github_views import (
    _invalidate_pr_caches,
    _repo_team_ids,
    apply_pull_request,
)

log = logging.getLogger(__name__)

# GitHub answers these when the token or the quota is the problem; asking
# the same repo again in this run would only repeat it.
_STOP_STATUSES = frozenset({401, 403, 429})

# Most merge-readiness comments one run posts (MIP-2056), so switching the nudges on over a board
# with many old PRs cannot flood it; the rest follow on the next runs.
MAX_NUDGES_PER_RUN = 25


class Command(CronCommand):
    help = "Apply closed/merged PR state that the webhook missed to still-open deliveries."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Ask GitHub and report which PRs have closed, but change nothing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        self._redeliver_app_failures(dry_run=dry_run)
        totals = {"repos": 0, "checked": 0, "still_open": 0, "closed": 0, "tasks": 0, "nudges": 0}
        self._nudge_budget = [MAX_NUDGES_PER_RUN]
        seen: set[tuple[str, str]] = set()
        registrations = {}
        for registration in GithubWebhookRegistration.objects.order_by("owner", "repo"):
            registrations.setdefault(
                (registration.owner.lower(), registration.repo.lower()), registration
            )
        # The registered repos first (their stored spelling), then the repos only the App lists.
        watched = [(r.owner, r.repo, r) for r in registrations.values()]
        watched += [
            (owner, repo, None) for owner, repo in app_repos() if (owner, repo) not in registrations
        ]
        for owner, repo, registration in watched:
            key = (owner.lower(), repo.lower())
            if key in seen:
                continue
            seen.add(key)
            self._reconcile_repo(owner, repo, registration, totals, dry_run=dry_run)
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}reconcile_pr_deliveries: repos={totals['repos']} "
            f"checked={totals['checked']} still_open={totals['still_open']} "
            f"closed_on_github={totals['closed']} tasks_closed={totals['tasks']}"
            + (f" nudges={totals['nudges']}" if readiness.enabled() else "")
        )

    def _redeliver_app_failures(self, *, dry_run: bool) -> None:
        """First step (MIP-2088): ask GitHub to redeliver the App webhook's transient failures, and
        drop the replay-guard rows past their retention. Off with `GITHUB_APP_ENABLED`. A failure
        here is logged at WARNING and never stops the reconcile below, which recovers PR state
        from the PR itself."""
        if not github_app.is_enabled():
            return
        try:
            counts = redeliver_failed_deliveries(dry_run=dry_run)
            pruned = 0 if dry_run else prune_deliveries()
        except github_app.GithubAppError as exc:
            log.warning("reconcile_pr_deliveries: App redelivery skipped: %s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            # A recovery aid: whatever goes wrong (a database error while pruning), the legacy
            # reconcile below still runs. The type only: a message can quote a query.
            log.warning(
                "reconcile_pr_deliveries: App redelivery step failed: %s", type(exc).__name__
            )
            return
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}app redelivery: listed={counts['listed']} failed={counts['failed']} "
            f"redelivered={counts['redelivered']} pruned={pruned}"
        )

    @staticmethod
    def _team_ids(owner: str, repo: str, registration) -> tuple[list, set]:
        """The teams to apply this repo's PR state for, and the teams the App serves for it."""
        app_teams = app_team_ids_for_repo(owner, repo)
        team_ids = list(app_teams)
        if registration is not None:
            for team_id in _repo_team_ids(owner, repo):
                if team_id not in app_teams:
                    team_ids.append(team_id)
        return team_ids, app_teams

    @staticmethod
    def _headers(owner: str, repo: str, registration, app_teams: set):
        """The headers to read this repo with: an installation token when the App covers it, else
        (or when no token can be minted) the registrar's token. None = nothing to read it with."""
        if app_teams:
            try:
                token = installation_token(owner, repo, app_teams)
            except Exception as exc:  # noqa: BLE001 - a bad App key or a DB error must not stop the run
                log.warning(
                    "reconcile_pr_deliveries: installation token unavailable for %s/%s (%s)",
                    owner,
                    repo,
                    type(exc).__name__,
                )
                token = None
            if token:
                return github_api_headers(token)
        if registration is None:
            return None
        return hook_admin_headers(None, registration)

    def _reconcile_repo(
        self, owner: str, repo: str, registration, totals: dict, *, dry_run: bool
    ) -> None:
        team_ids, app_teams = self._team_ids(owner, repo, registration)
        if not team_ids:
            return
        numbers = list(
            TaskDelivery.objects.filter(
                team_id__in=team_ids,
                repo_owner=owner.lower(),
                repo_name=repo.lower(),
                state=TaskDeliveryState.OPEN,
                pr_number__isnull=False,
            )
            .order_by("pr_number")
            .values_list("pr_number", flat=True)
            .distinct()
        )
        if not numbers:
            return
        totals["repos"] += 1
        headers = self._headers(owner, repo, registration, app_teams)
        if headers is None:
            log.warning(
                "reconcile_pr_deliveries: no usable GitHub token for %s/%s; "
                "%d open PR(s) not checked",
                owner,
                repo,
                len(numbers),
            )
            return

        for number in numbers:
            try:
                resp = requests.get(
                    f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{number}",
                    headers=headers,
                    timeout=15,
                )
            except requests.RequestException as exc:
                # The type only: a request error's text can quote the URL.
                log.warning(
                    "reconcile_pr_deliveries: GitHub unreachable for %s/%s (%s); "
                    "stopping this repo",
                    owner,
                    repo,
                    type(exc).__name__,
                )
                return
            if resp.status_code in _STOP_STATUSES or resp.status_code >= 500:
                log.warning(
                    "reconcile_pr_deliveries: GitHub answered %s for %s/%s; stopping this repo",
                    resp.status_code,
                    owner,
                    repo,
                )
                return
            if not resp.ok:
                # 404: the PR (or the registrar's access to it) is gone.
                log.warning(
                    "reconcile_pr_deliveries: GitHub answered %s for %s/%s#%s; skipped",
                    resp.status_code,
                    owner,
                    repo,
                    number,
                )
                continue
            try:
                pr = resp.json()
            except ValueError:
                pr = None
            if not isinstance(pr, dict):
                log.warning(
                    "reconcile_pr_deliveries: unreadable answer for %s/%s#%s; skipped",
                    owner,
                    repo,
                    number,
                )
                continue

            totals["checked"] += 1
            if pr.get("state") != "closed":
                totals["still_open"] += 1
                if readiness.enabled() and not dry_run:
                    totals["nudges"] += readiness.check_open_pr(
                        owner=owner,
                        repo=repo,
                        pr=pr,
                        team_ids=team_ids,
                        get=lambda path: requests.get(
                            f"{GITHUB_API_BASE}{path}", headers=headers, timeout=15
                        ),
                        budget=self._nudge_budget,
                    )
                continue
            totals["closed"] += 1
            merged = bool(pr.get("merged"))
            if dry_run:
                self.stdout.write(
                    f"[dry-run] {owner}/{repo}#{number} is {'merged' if merged else 'closed'} "
                    "on GitHub"
                )
                continue
            # The registration, not the REST body, names the repo: a renamed
            # repo answers with its new name in `html_url`, and the delivery
            # rows and the tenant scope are keyed by the registered one.
            pr["html_url"] = f"https://github.com/{owner}/{repo}/pull/{number}"
            try:
                applied = apply_pull_request(owner, repo, pr, "closed", team_ids)
                _invalidate_pr_caches(owner, repo, number, (pr.get("head") or {}).get("ref"))
            except Exception:  # noqa: BLE001
                # One PR's failure must not stop the others; ERROR fails the
                # run (CronCommand) so it is seen.
                log.exception(
                    "reconcile_pr_deliveries: could not apply %s/%s#%s", owner, repo, number
                )
                continue
            closed = applied["updated"] or 0
            totals["tasks"] += closed
            log.info(
                "reconcile_pr_deliveries: %s/%s#%s %s on GitHub -> %d task(s) closed",
                owner,
                repo,
                number,
                "merged" if merged else "closed",
                closed,
            )
