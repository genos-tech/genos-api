"""The "needs you" PR signals for the author of a PR, from the GitHub App webhook (MIP-2062).

Part 1 (MIP-2054, `pr_needs_you`) tells a reviewer that a review was asked of them. These four tell
the PR's AUTHOR what happened to their PR, through the same existing surfaces (an Inbox item of kind
0, one comment on the linked task, the briefing's `waiting_on_me`): no new UI.

  changes requested   `pull_request_review` submitted, state changes_requested
  approved            `pull_request_review` submitted, state approved
  CI failing          `check_run` completed (failure, timed_out, startup_failure), or `status`
                      failure / error, on the head commit of an open PR
  approved and green  an approval and settled green CI on the head commit, whichever came last

This module decides; `pr_needs_you` writes. Only the App webhook sends these events (the legacy
per-repo hook subscribes to `pull_request`, `issue_comment` and `pull_request_review_comment`), and
the endpoint has already verified the signature, resolved the installation and passed the TEAMS
LINKED to it as `team_ids`: an unlinked team is never in that list, so it gets nothing. Whatever a
payload says about people or repositories is used only inside those teams.

NO GITHUB READER, NO LOOKUP. `reader` is `pr_signal_reader.InstallationReader` (installation token
narrowed to the repo, read only). Without one (the legacy hook), review signals are still written
from the payload, but nothing that needs GitHub is: no "ready" and no CI signals.

COST. A CI event does nothing at all, and makes no GitHub call, unless the repo has an open PR linked
to a task in the linked teams. A passing check does nothing more unless that PR already has an
"approved" or "ready" notice or an unread "CI failing" one (a DB read). Details in
`pr_signal_reader`. Many check runs finishing together cannot make many notices: the writer's key is
per commit and "CI failing" is debounced per PR (`pr_needs_you.CI_DEBOUNCE`), both under the task lock.

A signal that cannot be decided (GitHub unreachable, an answer cut short) is dropped, never guessed.
A "ready" can therefore be missed when the last delivery is lost; nothing reconciles it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from typing import Any

from django.db.models import Q

from origin.models.common.inbox_models import InboxItems
from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.services import pr_needs_you as needs_you
from origin.services.pr_merge_comment import _plain
from origin.services.pr_signal_reader import (
    InstallationReader,
    ci_is_green,
    review_decision,
    slim_pull,
)
from origin.services.task_delivery import canonical_pr_url

logger = logging.getLogger("origin.pr_signals")

#: `find_tasks(number, head_ref)`: the tasks a PR is linked to inside the linked teams.
FindTasks = Callable[[int, str], list[TaskMaster]]

_FAILING_CONCLUSIONS = frozenset({"failure", "timed_out", "startup_failure"})
_PASSING_CONCLUSIONS = frozenset({"success", "neutral", "skipped"})
_FAILING_STATES = frozenset({"failure", "error"})


def _github_id(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def handle_review_event(
    owner: str,
    repo: str,
    payload: dict,
    team_ids: list,
    reader: InstallationReader | None,
    find_tasks: FindTasks,
) -> int:
    """A `pull_request_review`: tell the author about a submitted change request or approval.
    Returns how many task notices were written."""
    if payload.get("action") != "submitted":
        return 0
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    raw_pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), dict) else {}
    pr = slim_pull(raw_pr)
    state = str(review.get("state") or "").lower()
    review_id = _github_id(review.get("id"))
    if pr is None or pr["state"] != "open" or review_id is None:
        return 0
    if state not in ("changes_requested", "approved"):
        return 0
    actor = review.get("user") if isinstance(review.get("user"), dict) else {}
    if _github_id(actor.get("id")) is not None and actor.get("id") == pr["author_id"]:
        return 0  # nobody is told about their own review
    tasks = find_tasks(pr["number"], pr["ref"])
    if not tasks:
        return 0
    pr_url = canonical_pr_url(owner, repo, pr["number"])
    common = dict(
        author_github_id=pr["author_id"],
        owner=owner.lower(),
        repo=repo.lower(),
        number=pr["number"],
        title=pr["title"],
        pr_url=pr_url,
        actor_login=str(actor.get("login") or ""),
    )
    if state == "changes_requested":
        return needs_you.notify_author(
            tasks,
            signal=needs_you.KIND_CHANGES_REQUESTED,
            discriminator=review_id,
            extra={"review_id": review_id},
            **common,
        )
    # An approval of a commit that is already green is ONE notice ("ready"), not two.
    if reader is not None and not pr["draft"] and pr["sha"]:
        decision, green = _settled(reader, owner, repo, pr["number"], pr["sha"], ci_always=False)
        if decision == "approved" and green:
            return needs_you.notify_author(
                tasks,
                signal=needs_you.KIND_READY,
                discriminator=pr["sha"],
                extra={"sha": pr["sha"]},
                **common,
            )
    return needs_you.notify_author(
        tasks,
        signal=needs_you.KIND_APPROVED,
        discriminator=review_id,
        extra={"review_id": review_id},
        **common,
    )


def _settled(
    reader: InstallationReader, owner: str, repo: str, number: int, sha: str, *, ci_always: bool
) -> tuple[str | None, bool]:
    """(the PR's review decision, whether CI is green on `sha`), read from GitHub now. A lookup that
    failed is "unknown": `None` / False. The CI is only read when the PR is approved, unless
    `ci_always` (a passing check also retires a "CI failing" notice that needs no approval)."""
    reviews = reader.reviews(owner, repo, number)
    decision = review_decision(reviews) if reviews is not None else None
    if decision != "approved" and not ci_always:
        return decision, False
    checks = reader.check_runs(owner, repo, sha)
    combined = reader.combined_status(owner, repo, sha)
    return decision, ci_is_green(combined, checks)


def _has_open_linked_pr(owner: str, repo: str, team_ids: list) -> bool:
    return TaskDelivery.objects.filter(
        team_id__in=team_ids,
        repo_owner=owner.lower(),
        repo_name=repo.lower(),
        state=TaskDeliveryState.OPEN,
    ).exists()


def _repo_awaits_green(owner: str, repo: str, team_ids: list) -> bool:
    """Does any PR of this repo have an "approved" or "ready" notice, or an unread "CI failing" one?"""
    prefix = canonical_pr_url(owner, repo, 0).rsplit("/", 1)[0] + "/"
    rows = InboxItems.objects.filter(
        team_id__in=team_ids,
        item_type=0,
        is_deleted=False,
        item_optionals__pr_url__startswith=prefix,
    )
    return rows.filter(
        Q(item_optionals__kind__in=[needs_you.KIND_APPROVED, needs_you.KIND_READY])
        | Q(item_optionals__kind=needs_you.KIND_CI_FAILING, is_read=False)
    ).exists()


def _notices(pr_url: str, team_ids: Iterable, *kinds: str, unread: bool = False, sha: str = ""):
    rows = InboxItems.objects.filter(
        team_id__in=list(team_ids),
        item_type=0,
        is_deleted=False,
        item_optionals__pr_url=pr_url,
        item_optionals__kind__in=list(kinds),
    )
    if unread:
        rows = rows.filter(is_read=False)
    if sha:
        rows = rows.filter(item_optionals__sha=sha)
    return rows


def _ci_outcome(event: str, payload: dict) -> tuple[str, str, str] | None:
    """`(sha, "failing" | "passing", what failed)` from a `check_run` or `status`, or None when the
    event says nothing final (a run still going, a cancelled or action-required run, `pending`)."""
    if event == "check_run":
        run = payload.get("check_run") if isinstance(payload.get("check_run"), dict) else {}
        if run.get("status") != "completed":
            return None
        conclusion, sha, name = run.get("conclusion"), run.get("head_sha"), run.get("name")
        if conclusion in _FAILING_CONCLUSIONS:
            outcome = "failing"
        elif conclusion in _PASSING_CONCLUSIONS:
            outcome = "passing"
        else:
            return None
    else:
        state, sha, name = payload.get("state"), payload.get("sha"), payload.get("context")
        if state in _FAILING_STATES:
            outcome = "failing"
        elif state == "success":
            outcome = "passing"
        else:
            return None
    if not isinstance(sha, str) or not sha:
        return None
    return sha, outcome, name if isinstance(name, str) else ""


def handle_ci_event(
    event: str,
    owner: str,
    repo: str,
    payload: dict,
    team_ids: list,
    reader: InstallationReader | None,
    find_tasks: FindTasks,
) -> int:
    """A `check_run` or `status`: tell the author a check failed, or that an approved PR is green.
    Returns how many task notices were written."""
    outcome = _ci_outcome(event, payload)
    if reader is None or outcome is None or not team_ids:
        return 0
    sha, kind, name = outcome
    if not _has_open_linked_pr(owner, repo, team_ids):
        return 0  # nothing here is tracked: no GitHub call
    if kind == "passing" and not _repo_awaits_green(owner, repo, team_ids):
        return 0  # the common case, a green check on a PR nobody waits on: no GitHub call
    pulls = reader.pulls_for_commit(owner, repo, sha)
    written = 0
    for pr in pulls or ():
        if pr["draft"]:
            continue
        tasks = find_tasks(pr["number"], pr["ref"])
        if not tasks:
            continue
        pr_url = canonical_pr_url(owner, repo, pr["number"])
        common = dict(
            author_github_id=pr["author_id"],
            owner=owner.lower(),
            repo=repo.lower(),
            number=pr["number"],
            title=pr["title"],
            pr_url=pr_url,
        )
        if kind == "failing":
            what = (
                f" ({_plain(name, 60)}, commit {sha[:7]})"
                if _plain(name, 60)
                else f" (commit {sha[:7]})"
            )
            written += needs_you.notify_author(
                tasks,
                signal=needs_you.KIND_CI_FAILING,
                discriminator=sha,
                detail=what,
                extra={"sha": sha},
                **common,
            )
            continue
        written += _passing(reader, owner, repo, pr, pr_url, tasks, team_ids, sha, common)
    return written


def _passing(reader, owner, repo, pr, pr_url, tasks, team_ids, sha, common) -> int:
    """A check passed on `pr`'s head: when the PR is waiting on this (an approval is on record, or a
    "CI failing" notice is unread) and the commit is now settled green, retire the failing notice
    and, if the PR is approved, tell the author it is ready."""
    if _notices(pr_url, team_ids, needs_you.KIND_READY, sha=sha).exists():
        return 0
    waiting = _notices(pr_url, team_ids, needs_you.KIND_APPROVED, needs_you.KIND_READY).exists()
    failing = _notices(pr_url, team_ids, needs_you.KIND_CI_FAILING, unread=True).exists()
    if not (waiting or failing):
        return 0
    decision, green = _settled(reader, owner, repo, pr["number"], sha, ci_always=True)
    if green and failing:
        needs_you.resolve_items(pr_url, (needs_you.KIND_CI_FAILING,), team_ids=team_ids)
    if green and decision == "approved":
        return needs_you.notify_author(
            tasks,
            signal=needs_you.KIND_READY,
            discriminator=sha,
            extra={"sha": sha},
            **common,
        )
    return 0


def retire_closed_pr(owner: str, repo: str, number: Any, team_ids: list) -> int:
    """A PR was closed or merged: its unread signals stopped being true. Returns how many."""
    if isinstance(number, bool) or not isinstance(number, int) or not team_ids:
        return 0
    return needs_you.resolve_items(
        canonical_pr_url(owner, repo, number), needs_you.ALL_KINDS, team_ids=team_ids
    )
