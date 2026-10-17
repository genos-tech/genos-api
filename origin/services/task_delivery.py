"""Write side of `TaskDelivery`: one upsert, and the webhook's state sync.

Two writers share this so they cannot disagree about what a duplicate is:
`GithubWebhookView` (a PR on a branch that carries a task's display id)
and the `add_task_delivery` tool (any branch, including the ones whose
name carries no display id, which nothing could connect before).

The dedup rules, which the table's two conditional unique constraints
back up:

* One row per task + PR. The PR url is rebuilt from lowercased parts,
  because GitHub names are case-insensitive and a pasted url's casing
  need not match the webhook's.
* One row per task + repo + branch while no PR exists. When a PR opens
  on that branch it is attached to the row, not added beside it, so a
  link recorded early ends up as one row and never two.
"""

from __future__ import annotations

import logging
import re

from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from origin.models.task.task_delivery_models import (
    TaskDelivery,
    TaskDeliverySource,
    TaskDeliveryState,
)

logger = logging.getLogger(__name__)

_PR_TITLE_MAX = 500


def canonical_pr_url(owner: str, repo: str, number: int) -> str:
    return f"https://github.com/{owner.lower()}/{repo.lower()}/pull/{int(number)}"


def pr_state(pr: dict) -> str:
    """The delivery state a GitHub PR object (REST or webhook) is in.

    A draft is OPEN: it is work in flight, which is the question the
    state answers.
    """
    if pr.get("merged") or pr.get("merged_at"):
        return TaskDeliveryState.MERGED
    if pr.get("state") == "closed":
        return TaskDeliveryState.CLOSED
    return TaskDeliveryState.OPEN


def parse_merged_at(value):
    return parse_datetime(value) if isinstance(value, str) and value else None


def record_delivery(
    *,
    task,
    owner: str,
    repo: str,
    branch: str = "",
    pr_number: int | None = None,
    pr_title: str = "",
    state: str | None = None,
    merged_at=None,
    source: str,
    created_by_id=None,
) -> tuple[TaskDelivery, str]:
    """Create or update the link between `task` and one branch / PR.

    Returns `(row, outcome)`, outcome one of "created", "updated",
    "unchanged". A state of None or UNKNOWN never overwrites a known one:
    not having looked is not news about the PR.
    """
    owner, repo = owner.lower(), repo.lower()
    branch = branch or ""
    pr_url = canonical_pr_url(owner, repo, pr_number) if pr_number is not None else None
    if state is None:
        state = TaskDeliveryState.UNKNOWN if pr_url else TaskDeliveryState.BRANCH
    pr_title = (pr_title or "")[:_PR_TITLE_MAX]

    if pr_url is None:
        # A branch with no PR. If any link for this branch exists already,
        # with or without its PR, there is nothing new to record.
        existing = TaskDelivery.objects.filter(
            task=task, repo_owner=owner, repo_name=repo, branch=branch
        ).order_by("pr_url")
        row = existing.first()
        if row is not None:
            return row, "unchanged"
        return _create(
            task=task,
            repo_owner=owner,
            repo_name=repo,
            branch=branch,
            state=state,
            source=source,
            created_by_id=created_by_id,
        )

    row = TaskDelivery.objects.filter(task=task, pr_url=pr_url).first()
    branch_only = TaskDelivery.objects.filter(
        task=task, repo_owner=owner, repo_name=repo, branch=branch, pr_url__isnull=True
    )
    if row is None and branch:
        # The branch was linked before its PR existed: attach the PR.
        row = branch_only.first()
    elif row is not None and branch:
        # Both exist (the PR was linked from elsewhere first). Keep the
        # PR row; the branch-only one says nothing it doesn't.
        branch_only.delete()

    if row is None:
        return _create(
            task=task,
            repo_owner=owner,
            repo_name=repo,
            branch=branch,
            pr_url=pr_url,
            pr_number=pr_number,
            pr_title=pr_title,
            state=state,
            merged_at=merged_at,
            source=source,
            created_by_id=created_by_id,
        )

    changed = []
    if row.pr_url != pr_url:
        row.pr_url, row.pr_number = pr_url, pr_number
        changed += ["pr_url", "pr_number"]
    if branch and row.branch != branch:
        row.branch = branch
        changed.append("branch")
    if pr_title and row.pr_title != pr_title:
        row.pr_title = pr_title
        changed.append("pr_title")
    if state != TaskDeliveryState.UNKNOWN and row.state != state:
        row.state = state
        changed.append("state")
    if state == TaskDeliveryState.MERGED and merged_at and row.merged_at != merged_at:
        row.merged_at = merged_at
        changed.append("merged_at")
    if not changed:
        return row, "unchanged"
    row.save(update_fields=[*changed, "ts_updated_at"])
    return row, "updated"


def _create(**fields) -> tuple[TaskDelivery, str]:
    # A webhook redelivery can race the original. The constraint decides;
    # the loser reads the winner's row back.
    try:
        with transaction.atomic():
            return TaskDelivery.objects.create(team_id=fields["task"].team_id, **fields), "created"
    except IntegrityError:
        task = fields["task"]
        if fields.get("pr_url"):
            row = TaskDelivery.objects.get(task=task, pr_url=fields["pr_url"])
        else:
            row = TaskDelivery.objects.filter(
                task=task,
                repo_owner=fields["repo_owner"],
                repo_name=fields["repo_name"],
                branch=fields["branch"],
            ).first()
        return row, "unchanged"


def sync_pull_request(
    *,
    owner: str,
    repo: str,
    pr: dict,
    matched_tasks,
    team_ids: list,
) -> dict[str, int]:
    """Bring every delivery for one PR in line with a webhook payload.

    `matched_tasks` are the tasks whose display id the head branch
    carries; each gets a row. A branch linked by hand before its PR
    existed gets the PR attached. Then every row for this PR, however it
    was made, takes the PR's current state. Everything is limited to
    `team_ids`, the registrar's teams (`_repo_team_ids`): the payload is
    attacker-supplied, and that is the tenant boundary.
    """
    counts = {"linked": 0, "attached": 0, "synced": 0}
    number = pr.get("number")
    if not team_ids or number is None:
        return counts
    branch = ((pr.get("head") or {}).get("ref")) or ""
    state = pr_state(pr)
    merged_at = parse_merged_at(pr.get("merged_at"))
    title = pr.get("title") or ""
    common = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "pr_number": int(number),
        "pr_title": title,
        "state": state,
        "merged_at": merged_at,
        "source": TaskDeliverySource.WEBHOOK,
    }

    for task in matched_tasks:
        # The caller already scoped its candidates; checked again here
        # because a row written into the wrong tenant is the one mistake
        # this function must not be able to make.
        if task.team_id not in team_ids:
            continue
        _row, outcome = record_delivery(task=task, **common)
        if outcome == "created":
            counts["linked"] += 1

    if branch:
        pending = TaskDelivery.objects.select_related("task").filter(
            team_id__in=team_ids,
            repo_owner=owner.lower(),
            repo_name=repo.lower(),
            branch=branch,
            pr_url__isnull=True,
        )
        for row in pending:
            record_delivery(task=row.task, **common)
            counts["attached"] += 1

    # Every row for this PR, including ones made by hand on a branch no
    # display id names, and backfilled rows whose state was unknown.
    rows = TaskDelivery.objects.filter(
        team_id__in=team_ids, pr_url=canonical_pr_url(owner, repo, number)
    )
    counts["synced"] = rows.exclude(state=state).count()
    # `update()` skips auto_now, so the timestamp is set by hand.
    fields = {"state": state, "pr_number": int(number), "ts_updated_at": timezone.now()}
    if state == TaskDeliveryState.MERGED and merged_at:
        fields["merged_at"] = merged_at
    if title:
        fields["pr_title"] = title[:_PR_TITLE_MAX]
    if branch:
        fields["branch"] = branch
    rows.update(**fields)
    return counts


# `owner/repo`, or the repo's GitHub URL.
_REPO_RE = re.compile(
    r"^(?:https://github\.com/)?([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100}?)"
    r"(?:\.git)?/?$"
)


def parse_repo(value) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    m = _REPO_RE.match(value.strip())
    return (m.group(1), m.group(2)) if m else None


# Git forbids these in a ref name (`git check-ref-format`); refusing them
# here keeps a typo'd or pasted-sentence branch out of the table.
_BAD_BRANCH = re.compile(r"[\s~^:?*\[\\\x00-\x1f\x7f]|\.\.|@\{|^[-/.]|[/.]$|\.lock$")


def clean_branch(value) -> str | None:
    if not isinstance(value, str):
        return None
    branch = value.strip()
    if branch.startswith("refs/heads/"):
        branch = branch[len("refs/heads/") :]
    if not branch or len(branch) > 255 or _BAD_BRANCH.search(branch):
        return None
    return branch


def delivery_as_dict(row: TaskDelivery, wrap=lambda s: s) -> dict:
    """The shape every tool returns a delivery in. `wrap` marks the PR
    title, which comes from GitHub, as data."""
    return {
        "repo": f"{row.repo_owner}/{row.repo_name}",
        "branch": row.branch or None,
        "pr_url": row.pr_url,
        "pr_number": row.pr_number,
        "pr_title": wrap(row.pr_title) if row.pr_title else None,
        "state": row.state,
        "merged_at": row.merged_at.isoformat() if row.merged_at else None,
        "source": row.source,
    }
