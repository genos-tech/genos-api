"""One Genos comment on a task when a linked PR merges (MIP-2016).

Decision (Ken, 2026-11-22, "Decision: Genos guides agents with suggestions in tool replies, not hard
rejections (board hygiene)", MIP-2014): a merged PR whose branch carries a task id closes the task
even when work remains (it happened to MIP-1992 twice on 2026-11-22), and a merged PR with no id leaves
the task WIP. Genos does not change that. It leaves a visible record on the task saying which PR
merged and what is still open, so the agent or person who reads the task next corrects the status:

  * the task is Closed and it still has open sub-tasks or other open linked PRs: they are listed;
  * the task is not Closed and no other linked PR is open: "all linked PRs merged: close it or say
    what is left" (open sub-tasks are listed too, they are the likeliest "what is left");
  * the task is not Closed and other linked PRs are open: they are listed, the task waits for them;
  * the task is Closed and nothing is open: one line saying the PR merged.

WHO WROTE IT. Nobody: `sender` is null, as for `task_close_notice`'s closing line, so no member is
impersonated and no invented user is on the team. The text starts with the bold label "Genos" so
every reader (the task, the PM thread, an MCP `get_task`) can tell it is automatic. (The web app
shows a null sender as "Deleted user": a "Genos" author for such comments is a follow-up.)

NO NOTIFICATION. It does not go through `fan_out_task_comment`: no mention, participant activity,
push, e-mail or Inbox item, and no outbound integration webhook. The comment is mirrored into the
project's task thread (that is where the app renders a comment) and the project room is told a
comment was added, so an open task shows it; that is a refresh, not an alert.

ONCE PER PR AND TASK. The key is the id of the comment's first block, `genos-pr-merged:<canonical
PR URL>`, on a null-sender comment of that task (NOT a link in the text: the comment also links the
task's other open PRs, and when one of those merges later it must post its own, which is the one that
says "all linked PRs merged"). A webhook redelivery, a `reconcile_pr_deliveries` run, or a second
event for the same merge finds it and posts nothing. The check runs under the task row's lock (the one
`next_task_comment_id` takes), so two deliveries at once cannot both post. A comment a person deleted
stays counted (the row is kept, soft-deleted), so it is not posted again. No migration.

PR TITLES are GitHub text, so they are stripped of markdown syntax before they go into the body, and
every task chip is built here from rows this module read, never from the text.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterable
from typing import Any

from django.db import transaction
from django.db.models import TextField
from django.db.models.functions import Cast

from origin.models.task.task_delivery_models import TaskDelivery, TaskDeliveryState
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools.blocknote_md import markdown_to_blocks
from origin.services import realtime_push, unified_writer
from origin.services.task_comment_ids import next_task_comment_id
from origin.services.task_status_values import FINISHED_STATUSES

logger = logging.getLogger("origin.pr_merge_comment")

#: Statuses that are no longer open work, for a sub-task.
_DONE = FINISHED_STATUSES
#: Open sub-tasks and other open PRs named in the comment; the rest is "and N more".
MAX_LISTED = 5
_TITLE_CHARS = 120

#: Characters that would start markdown (a link, emphasis, code, an HTML tag) in GitHub's PR title.
_MD_SYNTAX = re.compile(r"[\[\]()`*_~<>\\|]|\r|\n")


def _plain(text: str, limit: int = _TITLE_CHARS) -> str:
    """GitHub-supplied text with markdown syntax and line breaks removed, cut to `limit`."""
    cleaned = " ".join(_MD_SYNTAX.sub(" ", text or "").split())
    return cleaned[: limit - 1].rstrip() + "…" if len(cleaned) > limit else cleaned


def _pr_label(owner: str, repo: str, number: Any) -> str:
    """`owner/repo #N`, with only the characters a GitHub owner or repo name can have."""
    safe = re.compile(r"[^A-Za-z0-9._-]")
    return f"{safe.sub('', owner)}/{safe.sub('', repo)} #{int(number)}"


def _paragraph(text: str) -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "type": "paragraph",
        "props": {"textColor": "default", "textAlignment": "left", "backgroundColor": "default"},
        "content": ([{"text": text, "type": "text", "styles": {}}] if text else []),
        "children": [],
    }


#: The id of the comment's FIRST block carries the key. Not a link in the text: the same comment
#: links the task's OTHER open PRs, and a later merge of one of those must still post its own.
MARKER_PREFIX = "genos-pr-merged:"


def marker(pr_url: str) -> str:
    return f"{MARKER_PREFIX}{pr_url}"


def already_posted(task_id: int, pr_url: str) -> bool:
    """A Genos comment of this task (null sender) was already written for this PR (`pr_url` is the
    canonical URL): its first block's id is `marker(pr_url)`.

    Looked for with the closing quote, so `.../pull/98` does not match `.../pull/982`. Deleted
    comments count: someone removed it on purpose."""
    return (
        TaskComments.objects.filter(task_id=task_id, sender__isnull=True)
        .annotate(_text=Cast("comment_body", TextField()))
        .filter(_text__contains=f'"{marker(pr_url)}"')
        .exists()
    )


def _open_subtasks(task: TaskMaster) -> tuple[list[TaskMaster], int]:
    """Up to `MAX_LISTED` open sub-tasks (and how many there are in all)."""
    qs = (
        TaskMaster.objects.filter(parent_task_id=task.task_id, is_deleted=False, is_milestone=False)
        .exclude(status__in=_DONE)
        .select_related("project")
    )
    total = qs.count()
    return list(qs.order_by("project_task_number", "task_id")[:MAX_LISTED]), total


def _other_open_prs(task: TaskMaster, pr_url: str) -> list[TaskDelivery]:
    """Delivery rows of the task whose PR is still open, apart from this one (drafts count: they are
    work in flight, as in `github_views._other_open_prs`)."""
    return list(
        TaskDelivery.objects.filter(task=task, state=TaskDeliveryState.OPEN)
        .exclude(pr_url=pr_url)
        .exclude(pr_url__isnull=True)
        .order_by("pr_url")
    )


def rule_line(why: Iterable[str]) -> str:
    """Which rule linked the PR to the task (MIP-2055): `why` holds "branch", "closes", "part_of"."""
    why = set(why)
    parts = []
    if "branch" in why:
        parts.append("its branch name carries the task id")
    if "closes" in why:
        parts.append('its description says "Closes" this task')
    if "part_of" in why:
        parts.append('its description says "Part of" this task, so it does not close it')
    return "Linked because " + "; ".join(parts) + "." if parts else ""


def build_markdown(
    *,
    task: TaskMaster,
    status: str,
    pr_url: str,
    label: str,
    title: str,
    subtasks: list[TaskMaster],
    subtask_total: int,
    other_prs: list[TaskDelivery],
    why: frozenset[str] | set[str] = frozenset(),
) -> str:
    """The comment as markdown. `[#ID](task:<id>)` links become chips through `markdown_to_blocks`."""
    shown = f' "{_plain(title)}"' if _plain(title) else ""
    paras = [f"**Genos** (automatic note): [{label}]({pr_url}){shown} merged."]
    if why:
        paras.append(rule_line(why))

    def prs_line() -> str:
        links = ", ".join(
            f"[{_pr_label(d.repo_owner, d.repo_name, d.pr_number or 0)}]({d.pr_url})"
            for d in other_prs[:MAX_LISTED]
        )
        more = len(other_prs) - MAX_LISTED
        return links + (f" and {more} more" if more > 0 else "")

    def subtasks_line() -> str:
        chips = ", ".join(f"[#{t.display_id}](task:{t.task_id})" for t in subtasks)
        more = subtask_total - len(subtasks)
        return chips + (f" and {more} more" if more > 0 else "")

    open_subtasks = f"Open sub-tasks ({subtask_total}): {subtasks_line()}" if subtasks else ""
    open_prs = f"Other linked PRs still open ({len(other_prs)}): {prs_line()}" if other_prs else ""
    if status == "Closed":
        if subtasks or other_prs:
            paras.append("The task is Closed, but it may not be finished:")
            paras.append("\n".join(f"- {item}" for item in (open_subtasks, open_prs) if item))
            paras.append("Reopen it, or finish those first.")
        else:
            paras.append("The task is Closed. No other linked PR or sub-task is open.")
    elif status in ("Rejected", "Deleted"):
        paras.append(f"The task is {status}, so the merge did not close it.")
        if open_prs:
            paras.append(open_prs)
    elif other_prs:
        paras.append(f"The task stays open and waits for the other linked PRs. {open_prs}")
        if open_subtasks:
            paras.append(open_subtasks)
    else:
        paras.append(
            f"All linked PRs merged, and the task is {status}: close it or say what is left."
        )
        if open_subtasks:
            paras.append(open_subtasks)
    return "\n\n".join(paras)


def _chip_resolver(tasks: Iterable[TaskMaster]):
    """`task:<id>` -> a `hashTask` chip, for the tasks this module already read and no others."""
    known = {
        f"task:{t.task_id}": {
            "type": "hashTask",
            "props": {
                "projectId": str(t.project_id or ""),
                "taskId": str(t.task_id),
                "displayId": str(t.display_id),
                "title": str(t.title or ""),
            },
        }
        for t in tasks
    }

    def resolve(token: str) -> dict[str, Any] | None:
        chip = known.get((token or "").strip())
        return {"type": chip["type"], "props": dict(chip["props"])} if chip else None

    return resolve


def post_merge_comment(
    task: TaskMaster,
    *,
    owner: str,
    repo: str,
    number: int,
    title: str,
    pr_url: str,
    why: Iterable[str] = (),
) -> bool:
    """Post the one comment for `task` and this merged PR. True when it was posted, False when it was
    already there (or the task is gone). `pr_url` is the canonical lowercase URL."""
    current = (
        TaskMaster.objects.filter(task_id=task.task_id, is_deleted=False)
        .select_related("project")
        .first()
    )
    if current is None or current.project_id is None or current.project.is_deleted:
        return False
    subtasks, subtask_total = _open_subtasks(current)
    other_prs = _other_open_prs(current, pr_url)
    markdown = build_markdown(
        task=current,
        status=current.status,
        pr_url=pr_url,
        label=_pr_label(owner, repo, number),
        title=title,
        subtasks=subtasks,
        subtask_total=subtask_total,
        other_prs=other_prs,
        why=set(why),
    )
    blocks = markdown_to_blocks(markdown, entity_chip_resolver=_chip_resolver(subtasks))
    blocks[0]["id"] = marker(pr_url)  # the once-per-PR key (`already_posted`)
    blocks.append(_paragraph(""))  # BlockNote's trailing sentinel, as `add_comment` leaves it
    with transaction.atomic():
        comment_id = next_task_comment_id(current.task_id)  # locks the task row
        if already_posted(current.task_id, pr_url):
            return False
        comment = TaskComments.objects.create(
            task_id=current.task_id, sender=None, comment_id=comment_id, comment_body=blocks
        )
    # After the commit: show it in the project's task thread and in an open task. Neither notifies.
    unified_writer.write_task_comment_as_thread_reply(
        task_id=current.task_id,
        comment_id=comment.comment_id,
        sender_id=None,
        body=blocks,
        project_id=current.project_id,
        bypass_flag=True,
    )
    realtime_push.push_task_comment(current.task_id, comment.comment_id, realtime_push.CREATED)
    return True


def post_merge_comments(
    tasks: Iterable[TaskMaster],
    *,
    owner: str,
    repo: str,
    number: int,
    title: str,
    pr_url: str,
    why: dict[int, set[str]] | None = None,
) -> int:
    """`post_merge_comment` for each task, each in its own failure boundary: a webhook must answer
    200 whatever happens here (a 5xx is not retried by GitHub, MIP-861). Returns how many were posted."""
    posted = 0
    for task in tasks:
        try:
            if post_merge_comment(
                task,
                owner=owner,
                repo=repo,
                number=number,
                title=title,
                pr_url=pr_url,
                why=(why or {}).get(task.task_id, ()),
            ):
                posted += 1
        except Exception:  # noqa: BLE001 — the merge already did what it does; this is a note
            logger.exception("PR merge comment failed: task=%s pr=%s", task.task_id, pr_url)
    return posted
