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
* The registrar's GitHub token is used (`hook_admin_headers`), the same
  one the hook was installed with. A repo whose registrar has to reconnect
  is skipped with a WARNING, not an ERROR, or one revoked token would fail
  the cron on every run; its tasks stay open until someone reconnects.
* GitHub answering 401 / 403 / 429 (or erroring, or not answering) stops
  that repo for this run; the next run tries again. Other repos go on.

Runs every 15 minutes (`.railway/railway.ts`, service
`reconcile-pr-deliveries`), which bounds a missed close to about that.
"""

from __future__ import annotations

import logging

import requests

from origin.management.cron_command import CronCommand
from origin.models.common.user_models import GithubWebhookRegistration
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.services.github_webhooks import GITHUB_API_BASE, hook_admin_headers
from origin.views.common.github_views import (
    _invalidate_pr_caches,
    _repo_team_ids,
    apply_pull_request,
)

log = logging.getLogger(__name__)

# GitHub answers these when the token or the quota is the problem; asking
# the same repo again in this run would only repeat it.
_STOP_STATUSES = frozenset({401, 403, 429})


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
        totals = {"repos": 0, "checked": 0, "still_open": 0, "closed": 0, "tasks": 0}
        seen: set[tuple[str, str]] = set()
        for registration in GithubWebhookRegistration.objects.order_by("owner", "repo"):
            key = (registration.owner.lower(), registration.repo.lower())
            if key in seen:
                continue
            seen.add(key)
            self._reconcile_repo(registration, totals, dry_run=dry_run)
        prefix = "[dry-run] " if dry_run else ""
        self.stdout.write(
            f"{prefix}reconcile_pr_deliveries: repos={totals['repos']} "
            f"checked={totals['checked']} still_open={totals['still_open']} "
            f"closed_on_github={totals['closed']} tasks_closed={totals['tasks']}"
        )

    def _reconcile_repo(self, registration, totals: dict, *, dry_run: bool) -> None:
        owner, repo = registration.owner, registration.repo
        team_ids = _repo_team_ids(owner, repo)
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
        headers = hook_admin_headers(None, registration)
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
