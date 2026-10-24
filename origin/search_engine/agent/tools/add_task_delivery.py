"""`add_task_delivery` write tool — MIP-687.

Record which branch and/or PR is delivering a task. The GitHub webhook
already links a PR on its own when the branch name carries the task's
display id (`feature/PRJ-42-x`); this is for everything else — a branch
named before the convention, or one that names no task at all — and for
recording a branch before its PR exists.

Idempotent: adding the same PR twice changes nothing, and a branch added
before its PR gets the PR attached rather than a second row
(`origin.services.task_delivery.record_delivery`).

ACL: the same set as `update_task` — project members + assignee +
reporter. Linking a PR to a task is writing to that task.

State: when the user has GitHub connected, the PR's state is read from
GitHub so the link is right the moment it exists. Without a connection,
or with GitHub unreachable, the link is still recorded, as state
`unknown`, and the webhook corrects it on the PR's next event. A link
that could not be checked is still worth having; a guessed state is not.

Webhook (MIP-709): the state only stays right if the repo's webhook
delivers, and until now only a PR link saved through the task UI
installed it. Every recorded link now calls `ensure_repo_webhook` with
the writer's GitHub connection — a no-op past the first time for a repo
we already know. Failing to install it never fails the link; the result
says so instead, because a link that silently stays `unknown` forever is
worse than one that tells you why.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from origin.models.common.user_models import CustomUser
from origin.models.task.task_delivery_models import TaskDeliverySource, TaskDeliveryState
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.services.github_webhooks import ensure_repo_webhook, parse_pr_url_full
from origin.services.oauth.tokens import ReauthRequired
from origin.services.task_delivery import (
    clean_branch,
    delivery_as_dict,
    parse_merged_at,
    parse_repo,
    pr_state,
    record_delivery,
)

logger = logging.getLogger(__name__)


def _github_pr(user_id: str, owner: str, repo: str, number: int) -> dict | None:
    """The PR as GitHub has it, or None when it can't be read. Never
    raises: the link is recorded either way."""
    from origin.views.common.github_views import (  # noqa: PLC0415 — views import models
        _connected_account,
        _fetch_pr_by_number,
    )

    user = CustomUser.objects.filter(id=user_id).first()
    account = _connected_account(user) if user else None
    if account is None:
        return None
    try:
        return _fetch_pr_by_number(account, owner, repo, number, bypass_cache=True)
    except (ReauthRequired, requests.RequestException) as exc:
        logger.info("add_task_delivery: couldn't read %s/%s#%s: %s", owner, repo, number, exc)
        return None


def _ensure_webhook(user_id: str, owner: str, repo: str) -> bool:
    """Install (or adopt) our webhook on the repo. True when it is in
    place. Never raises: the link is recorded either way."""
    user = CustomUser.objects.filter(id=user_id).first()
    if user is None:
        return False
    try:
        return ensure_repo_webhook(user, owner, repo) is not None
    except Exception:
        logger.exception("add_task_delivery: ensure_repo_webhook crashed on %s/%s", owner, repo)
        return False


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    raw_task_id = args.get("task_id")
    try:
        task_id = int(raw_task_id)
    except (TypeError, ValueError):
        raise ToolError(f"`task_id` must be an integer (got {raw_task_id!r}).")

    pr_url_arg = (args.get("pr_url") or "").strip()
    branch_arg = args.get("branch")
    repo_arg = args.get("repo")
    if not pr_url_arg and not branch_arg:
        raise ToolError("Give a `pr_url`, or a `branch` with its `repo`, or both.")

    pr_ref = None
    if pr_url_arg:
        pr_ref = parse_pr_url_full(pr_url_arg)
        if pr_ref is None:
            raise ToolError(
                f"Not a GitHub PR URL: {pr_url_arg!r}. Expected "
                "https://github.com/<owner>/<repo>/pull/<number>."
            )

    branch = ""
    if branch_arg:
        branch = clean_branch(branch_arg)
        if branch is None:
            raise ToolError(f"Not a valid git branch name: {branch_arg!r}.")

    if pr_ref is not None:
        owner, repo, number = pr_ref
        if repo_arg:
            given = parse_repo(repo_arg)
            if given is None or (given[0].lower(), given[1].lower()) != (
                owner.lower(),
                repo.lower(),
            ):
                raise ToolError(
                    f"`repo` {repo_arg!r} does not match the PR URL's repo {owner}/{repo}. "
                    "Omit `repo` when you pass a `pr_url`."
                )
    else:
        number = None
        parsed = parse_repo(repo_arg) if repo_arg else None
        if parsed is None:
            raise ToolError(
                f"A `branch` without a `pr_url` needs `repo`, as owner/name (got {repo_arg!r})."
            )
        owner, repo = parsed

    try:
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Tenant + ACL: identical to update_task. A user who can't edit the
    # task can't record what is delivering it.
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        raise ToolForbidden("Not authorized: task is in a different team.")
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to update task {task_id}.")

    state = None
    merged_at = None
    title = ""
    checked = False
    if number is not None:
        pr = _github_pr(ctx.user_id, owner, repo, number)
        if pr:
            checked = True
            state = pr_state(pr)
            merged_at = parse_merged_at(pr.get("merged_at"))
            title = pr.get("title") or ""
            # The PR's own head branch, unless the caller named one.
            branch = branch or pr.get("branch") or ""

    row, outcome = record_delivery(
        task=task,
        owner=owner,
        repo=repo,
        branch=branch,
        pr_number=number,
        pr_title=title,
        state=state,
        merged_at=merged_at,
        source=TaskDeliverySource.MANUAL,
        created_by_id=ctx.user_id,
    )

    webhook_registered = _ensure_webhook(ctx.user_id, owner, repo)

    what = row.pr_url or f"{row.repo_owner}/{row.repo_name} branch {row.branch}"
    verb = {"created": "Linked", "updated": "Updated the link to", "unchanged": "Already linked:"}
    summary = f"{verb[outcome]} {what} on {task.display_id}"
    if row.state == TaskDeliveryState.UNKNOWN:
        summary += " (PR state unknown until GitHub reports it)"
    out = {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "outcome": outcome,
        "state_checked_on_github": checked,
        "webhook_registered": webhook_registered,
        "delivery": delivery_as_dict(row, wrap_workspace_content),
    }
    if not webhook_registered:
        out["webhook_warning"] = (
            f"Couldn't install the Genos webhook on {owner}/{repo}, so this link's "
            "PR state won't update and PRs on branches named after a task won't "
            "link themselves until it is. A repo admin can fix it by connecting "
            "GitHub in Genos and recording any PR or branch of the repo."
        )
        summary += f"; couldn't install the Genos webhook on {owner}/{repo}"
    out["__summary__"] = summary
    return out


ADD_TASK_DELIVERY = Tool(
    name="add_task_delivery",
    description=(
        "Record which GitHub branch and/or pull request is delivering a "
        "task. REQUIRES USER APPROVAL. A PR whose branch name contains the "
        "task's display id (e.g. feature/PRJ-42-x) is linked automatically "
        "when it opens — use this for a branch that does NOT name the task, "
        "or to record a branch before its PR exists. Pass `pr_url`, or "
        "`branch` + `repo` (owner/name), or all three. Safe to repeat: the "
        "same PR is never linked twice, and a PR opened later on a linked "
        "branch attaches to that link."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id the work belongs to.",
            },
            "pr_url": {
                "type": "STRING",
                "description": "GitHub PR URL, https://github.com/<owner>/<repo>/pull/<n>.",
            },
            "branch": {
                "type": "STRING",
                "description": "Head branch name, e.g. feat/mcp-publish-built-writes.",
            },
            "repo": {
                "type": "STRING",
                "description": (
                    "Repository as owner/name (e.g. genos-tech/genos-api). Needed "
                    "with `branch` when there is no `pr_url`."
                ),
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    requires_approval=True,
)
