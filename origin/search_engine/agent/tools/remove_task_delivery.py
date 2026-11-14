"""`remove_task_delivery` write tool — MIP-834.

The inverse of `add_task_delivery`: take back a branch / PR link that was
recorded against the wrong task. Before this a wrong link stayed on the
task for good (there is no REST delete either), and it is not harmless:
an open PR holds the task's auto-close, and `find_task_by_delivery`
answers from it. The case that asked for it: a PR number guessed before
`gh pr create` returned it, so an unrelated, merged PR sat on another
task.

Only a link someone added by hand (`TaskDelivery.source == manual`)
can be removed. A `webhook` link records that a PR's branch names the
task, which stays true, so the next PR event would write it again and
the removal would look like it worked and then undo itself. It is
refused, and the refusal says why and what to do instead. `backfill`
rows (the webhook era's PR links, carried over) are refused the same way.

Not destructive in the MCP sense (`McpTool.destructive`): re-adding the
link with `add_task_delivery` is the undo, and the history row written
here (`PR_UNLINKED`) keeps who removed which PR so it can be re-added.

ACL: identical to `add_task_delivery` — the same set as `update_task`,
project members + assignee + reporter.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.user_models import CustomUser
from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    ToolForbidden,
    wrap_workspace_content,
)
from origin.services.github_webhooks import parse_pr_url_full
from origin.services.task_delivery import (
    DeliveryNotRemovable,
    canonical_pr_url,
    clean_branch,
    delivery_as_dict,
    parse_repo,
    remove_delivery,
)


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
        raise ToolError("Give the `pr_url` to remove, or a `branch` with its `repo`.")

    lookup: dict[str, Any]
    what: str
    if pr_url_arg:
        ref = parse_pr_url_full(pr_url_arg)
        if ref is None:
            raise ToolError(
                f"Not a GitHub PR URL: {pr_url_arg!r}. Expected "
                "https://github.com/<owner>/<repo>/pull/<number>."
            )
        what = canonical_pr_url(*ref)
        lookup = {"pr_url": what}
    else:
        branch = clean_branch(branch_arg)
        if branch is None:
            raise ToolError(f"Not a valid git branch name: {branch_arg!r}.")
        parsed = parse_repo(repo_arg) if repo_arg else None
        if parsed is None:
            raise ToolError(
                f"A `branch` without a `pr_url` needs `repo`, as owner/name (got {repo_arg!r})."
            )
        what = f"{parsed[0].lower()}/{parsed[1].lower()} branch {branch}"
        lookup = {"repo_owner": parsed[0].lower(), "repo_name": parsed[1].lower(), "branch": branch}

    try:
        task = TaskMaster.objects.select_related("project").get(task_id=task_id)
    except TaskMaster.DoesNotExist:
        raise ToolError(f"Task {task_id} not found.")
    if str(getattr(task, "team_id", "") or "") != ctx.team_id:
        # The words of a missing one, so a guessed id does not say which tasks exist in other
        # teams (MIP-1531); the class still counts toward the MCP refusal alarm.
        raise ToolForbidden(f"Task {task_id} not found.")
    if task.is_deleted:
        raise ToolError(f"Task {task_id} has been deleted.")

    # Tenant + ACL: identical to add_task_delivery. A user who can't edit
    # the task can't take back what is delivering it.
    allowed = task_acl_user_ids(
        getattr(task, "project_id", None),
        getattr(task, "assignee_id", None),
        getattr(task, "reporter_id", None),
    )
    if ctx.user_id not in allowed:
        raise ToolForbidden(f"Not authorized to update task {task_id}.")

    rows = list(TaskDelivery.objects.filter(task=task, **lookup).order_by("delivery_id")[:5])
    if not rows:
        raise ToolError(f"{task.display_id} has no delivery link for {what}; nothing to remove.")
    if len(rows) > 1:
        raise ToolError(
            f"{task.display_id} has {len(rows)} links on {what}. Pass the `pr_url` of the "
            "one to remove."
        )
    row = rows[0]

    user = CustomUser.objects.filter(id=ctx.user_id).first()
    if user is None:
        raise ToolForbidden("Not authorized.")
    try:
        remove_delivery(row=row, removed_by=user)
    except DeliveryNotRemovable as exc:
        raise ToolError(exc.reason)

    out = {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "removed": delivery_as_dict(row, wrap_workspace_content),
        "__summary__": f"Removed the link to {what} from {task.display_id}",
    }
    return out


REMOVE_TASK_DELIVERY = Tool(
    name="remove_task_delivery",
    description=(
        "Remove a GitHub branch / pull request link that was recorded on the "
        "WRONG task with `add_task_delivery`. REQUIRES USER APPROVAL. Pass the "
        "task and the `pr_url`, or `branch` + `repo` (owner/name) for a link "
        "with no PR. Only a link someone added by hand can be removed: one "
        "the GitHub webhook made (the PR's branch name carries the task's "
        "display id) is refused, because the next PR event would add it "
        "back — rename the branch instead. The removal is written to the "
        "task's history; `add_task_delivery` puts the link back. Removing "
        "an open PR's link also stops it holding the task's auto-close."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric id of the task the link is wrongly on.",
            },
            "pr_url": {
                "type": "STRING",
                "description": "GitHub PR URL, https://github.com/<owner>/<repo>/pull/<n>.",
            },
            "branch": {
                "type": "STRING",
                "description": "Branch name of a link that has no PR yet.",
            },
            "repo": {
                "type": "STRING",
                "description": "Repository as owner/name. Needed with `branch`.",
            },
        },
        "required": ["task_id"],
    },
    run=_run,
    requires_approval=True,
)
