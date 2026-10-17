"""`find_task_by_delivery` tool — MIP-687.

Branch or PR URL → the task it delivers. The reverse of every other task
lookup, and the one an agent resuming work mid-branch needs: it has the
branch it is on and wants to confirm which task that is before writing
to it.

Two sources, in order, and nothing else:

  1. A stored delivery link (`TaskDelivery`) — made by the GitHub
     webhook or `add_task_delivery`. Authoritative: someone, or the
     convention, actually tied the two together.
  2. A display id in the branch name (`feature/PRJ-42-x` → PRJ-42),
     resolved within the team. This is the same rule the webhook links
     by, applied to a branch that never got a PR.

Anything else is **no match**, said plainly. A branch like
`feat/mcp-publish-built-writes` names no task, and the honest answer
is "not linked", not the nearest title. An agent that gets a guess
back writes to the wrong task.

ACL: team-scoped, then filtered to tasks the caller may read (project
members + assignee + reporter, as `fetch_task`). A match the caller
can't read is dropped, so it's reported the same as no match. Saying a
hidden task exists would leak it.
"""

from __future__ import annotations

import re
from typing import Any

from origin.models.task.task_delivery_models import TaskDelivery
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import (
    Tool,
    ToolContext,
    ToolError,
    wrap_workspace_content,
)
from origin.services.github_webhooks import parse_pr_url_full
from origin.services.task_delivery import (
    canonical_pr_url,
    clean_branch,
    delivery_as_dict,
    parse_repo,
)

# A display id inside a branch name: a project code (a letter, then 1–5
# letters/digits — `project_code.py`'s rule) and a number, each bounded
# by a non-alphanumeric character. Case-insensitive, like the webhook's
# `_branch_match_re`.
_DISPLAY_ID_IN_BRANCH = re.compile(
    r"(?:^|[^A-Za-z0-9])([A-Za-z][A-Za-z0-9]{1,5})-(\d+)(?=[^A-Za-z0-9]|$)"
)
_MAX_MATCHES = 10


def _readable(task: TaskMaster, ctx: ToolContext) -> bool:
    if task.is_deleted or str(task.team_id or "") != ctx.team_id:
        return False
    return ctx.user_id in task_acl_user_ids(task.project_id, task.assignee_id, task.reporter_id)


def _task_row(task: TaskMaster, matched_by: str, deliveries: list[dict]) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "title": wrap_workspace_content(task.title or ""),
        "status": task.status,
        "matched_by": matched_by,
        "deliveries": deliveries,
    }


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    pr_url_arg = (args.get("pr_url") or "").strip()
    branch_arg = args.get("branch")
    repo_arg = args.get("repo")
    if not pr_url_arg and not branch_arg:
        raise ToolError("Give a `branch` or a `pr_url`.")

    stored = TaskDelivery.objects.filter(team_id=ctx.team_id).select_related("task__project")
    query: dict[str, Any] = {}
    branch = ""
    if pr_url_arg:
        ref = parse_pr_url_full(pr_url_arg)
        if ref is None:
            raise ToolError(
                f"Not a GitHub PR URL: {pr_url_arg!r}. Expected "
                "https://github.com/<owner>/<repo>/pull/<number>."
            )
        stored = stored.filter(pr_url=canonical_pr_url(*ref))
        query["pr_url"] = canonical_pr_url(*ref)
    else:
        branch = clean_branch(branch_arg) or ""
        if not branch:
            raise ToolError(f"Not a valid git branch name: {branch_arg!r}.")
        stored = stored.filter(branch=branch)
        query["branch"] = branch
        if repo_arg:
            repo = parse_repo(repo_arg)
            if repo is None:
                raise ToolError(f"`repo` must be owner/name (got {repo_arg!r}).")
            stored = stored.filter(repo_owner=repo[0].lower(), repo_name=repo[1].lower())
            query["repo"] = f"{repo[0].lower()}/{repo[1].lower()}"

    # Source 1 — stored links. Group per task so a task delivered by the
    # same branch in two repos is one match carrying both links.
    by_task: dict[int, tuple[TaskMaster, list[dict]]] = {}
    for row in stored.order_by("-ts_updated_at")[:50]:
        if not _readable(row.task, ctx):
            continue
        entry = by_task.setdefault(row.task_id, (row.task, []))
        entry[1].append(delivery_as_dict(row, wrap_workspace_content))
    matches = [_task_row(t, "delivery_link", d) for t, d in by_task.values()]

    # Source 2 — a display id in the branch name, only when nothing is
    # stored. A stored link is the stronger statement, and mixing in a
    # name match beside it would offer the caller a choice it didn't ask
    # for. A PR URL carries no branch, so it has no second source.
    if not matches and branch:
        seen: set[int] = set()
        for m in _DISPLAY_ID_IN_BRANCH.finditer(branch):
            code, number = m.group(1), int(m.group(2))
            task = (
                TaskMaster.objects.select_related("project")
                .filter(
                    team_id=ctx.team_id,
                    project__code__iexact=code,
                    project_task_number=number,
                    is_deleted=False,
                )
                .first()
            )
            if task is None or task.task_id in seen or not _readable(task, ctx):
                continue
            seen.add(task.task_id)
            matches.append(_task_row(task, "display_id_in_branch", []))

    matches = matches[:_MAX_MATCHES]
    if not matches:
        what = query.get("pr_url") or f"branch {branch}"
        return {
            "query": query,
            "matches": [],
            "__summary__": (
                f"No task is linked to {what}, and its name carries no display id "
                "of a task you can see."
                if branch
                else f"No task is linked to {what}."
            ),
        }
    head = ", ".join(m["display_id"] for m in matches)
    return {
        "query": query,
        "matches": matches,
        "__summary__": f"{len(matches)} task(s) for {query.get('pr_url') or branch}: {head}",
    }


FIND_TASK_BY_DELIVERY = Tool(
    name="find_task_by_delivery",
    description=(
        "Find the task a GitHub branch or pull request is delivering. Pass "
        "`branch` (optionally with `repo` as owner/name) or `pr_url`. Checks "
        "recorded delivery links first, then a display id in the branch name "
        "(feature/PRJ-42-x → PRJ-42). Returns an empty `matches` list when "
        "neither applies — it never guesses from titles, so an empty result "
        "means the branch is not linked; record it with `add_task_delivery` "
        "once you know the task. Each match says how it matched "
        "(`delivery_link` or `display_id_in_branch`)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "branch": {
                "type": "STRING",
                "description": "Head branch name, e.g. MIP-687 or feat/mcp-publish-built-writes.",
            },
            "repo": {
                "type": "STRING",
                "description": (
                    "Optional repository, owner/name, to narrow a branch name used in "
                    "several repos."
                ),
            },
            "pr_url": {
                "type": "STRING",
                "description": "GitHub PR URL, https://github.com/<owner>/<repo>/pull/<n>.",
            },
        },
        "required": [],
    },
    run=_run,
)
