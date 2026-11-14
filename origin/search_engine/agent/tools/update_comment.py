"""`update_comment` write tool (MIP-880): fix what you said in one of your comments.

`add_comment` is one-way: an agent that mistyped a comment, or posted it on the
wrong task's thread, could only add another. This changes the text of one of the
caller's own comments, through the one implementation the page's edit shares
(`services/task_comment_manage.edit_comment`): the comment, its copy in the PM
task thread, and a notification to anyone the edit newly @-mentions.

Only the author can edit, as on the page. The markdown is turned into the same
blocks `add_comment` writes, with the same chip syntax.

Approval flow: `requires_approval=True`. The user sees the new text before it
replaces the old.

MIP-1562 (flag `AGENT_PARITY_TOOLS`, default OFF; with it off none of this exists, see
`parity.py`): the comment bubble's two other actions go through the same tool instead of a new one,
because a write tool cannot be flag-gated and every parameter is one fewer declaration.

  * `reaction` (an emoji or a team `:name:`) adds the caller's reaction, `remove_reaction: true`
    takes it back; `services/task_comment_reactions`, the REST route's rules;
  * `flag` ("flag" / "unflag" / "done") is the bubble's Flag for later, Remove flag and Mark done on
    the comment's mirror message; `services/message_flags`, the REST route's rules.

Both act on ANY live comment of a task the caller may open, not only their own: the edit keeps its
own-comment rule, and a call carries one of `body_text`, `reaction` or `flag`. A reaction or a flag
never touches the comment itself (no edit row, no "edited" label, nobody mentioned).
"""

from __future__ import annotations

from typing import Any

from apis.request_id import log_failure
from origin.models.common.user_models import CustomUser
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.parity import ignore_unless_parity, parity_on
from origin.search_engine.agent.tools.add_comment import _wrap_blocknote
from origin.search_engine.agent.tools.base import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.task_access import load_comment, load_own_comment, load_task
from origin.services import message_flags, task_comment_reactions
from origin.services.task_comment_manage import edit_comment


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    args = ignore_unless_parity(args, PARITY_PROPERTIES)
    if any(args.get(name) is not None for name in PARITY_PROPERTIES):
        return _mark(args, ctx)
    check_body_size(args.get("body_text"), "body_text")
    body_text = (args.get("body_text") or "").strip()
    if not body_text:
        raise ToolError(
            "Send `body_text` to edit your comment, or `reaction` or `flag`."
            if parity_on()
            else "`body_text` is required and must be non-empty."
        )

    task = load_task(args.get("task_id"), ctx, action="edit a comment on")
    comment = load_own_comment(task, args.get("comment_id"), ctx)

    try:
        newly_mentioned = edit_comment(comment, _wrap_blocknote(body_text, ctx))
    except Exception as e:  # noqa: BLE001
        ref = log_failure("update_comment tool failed", e)
        raise ToolError(f"Failed to edit the comment (ref {ref}).")

    summary = f"Edited comment #{comment.comment_id} on {task.display_id}"
    if newly_mentioned:
        summary += f"; {len(newly_mentioned)} newly mentioned person(s) were notified"
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "comment_id": comment.comment_id,
        "newly_mentioned": len(newly_mentioned),
        "__summary__": summary,
    }


def _mark(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    """A reaction or a flag on a comment, not an edit of it."""
    one = "Send ONE of `body_text` (edit your own comment), `reaction` or `flag` per call."
    if (args.get("body_text") or "").strip():
        raise ToolError(one)
    reaction, flag, remove = args.get("reaction"), args.get("flag"), args.get("remove_reaction")
    if reaction is not None and flag is not None:
        raise ToolError(one)
    if remove is not None and not isinstance(remove, bool):
        raise ToolError(f"`remove_reaction` must be true or false (got {remove!r}).")
    if remove and reaction is None:
        raise ToolError("`remove_reaction` goes with `reaction`: the emoji to take back.")
    if flag is not None and flag not in message_flags.ACTIONS:
        raise ToolError(f"`flag` must be one of {', '.join(message_flags.ACTIONS)} (got {flag!r}).")

    task = load_task(args.get("task_id"), ctx, action="react to or flag a comment on")
    comment = load_comment(task, args.get("comment_id"), ctx)
    where = f"comment #{comment.comment_id} on {task.display_id}"
    if reaction is not None:
        return _react(task, comment, where, reaction, bool(remove), ctx)
    return _flag(task, comment, where, flag, ctx)


def _react(task, comment, where: str, raw: Any, remove: bool, ctx: ToolContext) -> dict[str, Any]:
    try:
        emoji = task_comment_reactions.check_emoji(raw, ctx.team_id)
    except task_comment_reactions.ReactionRefused as e:
        raise ToolError(str(e))
    ids = {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "comment_id": comment.comment_id,
        "reaction": emoji,
    }
    try:
        if remove:
            changed = bool(
                task_comment_reactions.remove_reaction(
                    task_id=task.task_id,
                    comment_id=comment.comment_id,
                    user_id=ctx.user_id,
                    emoji=emoji,
                )
            )
        else:
            changed = task_comment_reactions.add_reaction(
                team_id=str(task.team_id),
                task_id=task.task_id,
                comment_id=comment.comment_id,
                user_id=ctx.user_id,
                emoji=emoji,
            ).created
    except Exception as e:  # noqa: BLE001
        ref = log_failure("update_comment reaction failed", e)
        raise ToolError(f"Failed to update the reaction (ref {ref}).")
    if remove:
        said = (
            f"Removed your {emoji} from {where}"
            if changed
            else f"You had no {emoji} on {where}: nothing to remove"
        )
    else:
        said = f"Reacted {emoji} to {where}" if changed else f"Already reacted {emoji} to {where}"
    return {**ids, "removed": remove, "changed": changed, "__summary__": said}


def _flag(task, comment, where: str, action: str, ctx: ToolContext) -> dict[str, Any]:
    message = message_flags.comment_mirror(task.task_id, comment.comment_id, ctx.user_id)
    if message is None:
        raise ToolError(
            f"Comment {comment.comment_id} on {task.display_id} can't be flagged: a flag is kept "
            "on the project chat's copy of the comment, and you are not in that chat or it has "
            "no copy yet."
        )
    user = CustomUser.objects.get(id=ctx.user_id)
    try:
        existing = message.flags.filter(user=user).first()
        if action == message_flags.FLAG:
            state = "flagged"
            changed = existing is None or existing.completed_at is not None
            message_flags.flag_message(user, message)
            said = f"Flagged {where} for later" if changed else f"Already flagged: {where}"
        elif action == message_flags.UNFLAG:
            state = "not flagged"
            changed = bool(message_flags.unflag_message(user, message))
            said = f"Removed the flag from {where}" if changed else f"Not flagged: {where}"
        else:
            state = "done"
            changed = existing is not None and existing.completed_at is None
            if existing is None:
                state = "not flagged"
                said = f"Not flagged, so there is nothing to mark done: {where}"
            else:
                if changed:
                    message_flags.complete_flag(user, message)
                said = (
                    f"Marked the flag on {where} done"
                    if changed
                    else f"Already marked done: {where}"
                )
    except Exception as e:  # noqa: BLE001
        ref = log_failure("update_comment flag failed", e)
        raise ToolError(f"Failed to update the flag (ref {ref}).")
    return {
        "task_id": task.task_id,
        "display_id": task.display_id,
        "comment_id": comment.comment_id,
        "flag": state,
        "changed": changed,
        "__summary__": said,
    }


#: What `AGENT_PARITY_TOOLS` adds to this tool's schema (`tool_flag_params`); the tool takes these
#: only while the flag is on (`parity.ignore_unless_parity`). Kept terse: every character is paid
#: on every call.
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "reaction": {
        "type": "STRING",
        "description": "An emoji (👍) or team `:name:` to react to the comment with, instead of editing.",
    },
    "remove_reaction": {
        "type": "BOOLEAN",
        "description": "With reaction: take it back instead of adding it.",
    },
    "flag": {
        "type": "STRING",
        "enum": list(message_flags.ACTIONS),
        "description": "Instead of editing: flag the comment to read later, remove the flag, or mark it done.",
    },
}

#: The parameter the flag stops requiring: a reaction or a flag has no text.
PARITY_OPTIONAL = ("body_text",)

#: The sentence the flag adds to the description (`tool_flag_text`).
DESCRIPTION_ADDITION = (
    " `reaction` and `flag` are not edits: they work on ANY comment of a task the user can open, "
    "one of body_text, reaction or flag per call. A reaction tells the comment's author; a flag "
    "is private to the user."
)

#: Two sentences of the description that say the tool edits and nothing else, swapped for their
#: new wording while the flag is on (`tool_flag_text.REPLACEMENTS`); a model that reads both
#: keeps obeying the first. A test holds each `old` to the description.
REQUIRED_SENTENCE = "Required: task_id, comment_id, body_text."
REQUIRED_SENTENCE_ON = "Required: task_id, comment_id and one of body_text, reaction, flag."
OWN_SENTENCE = (
    "(only comments the user wrote can be changed, so match `sender_id` against their id from "
    "`get_current_user`)"
)
OWN_SENTENCE_ON = (
    "(to EDIT with body_text only comments the user wrote qualify: match `sender_id` against their "
    "id from `get_current_user`)"
)
#: The same swap in the MCP catalog's wording (`tool_flag_text.MCP_REPLACEMENTS`).
MCP_OPENING = "Replace the text of one of the caller's own comments on a task."
MCP_OPENING_ON = (
    "Replace the text of one of the caller's own comments on a task, or react to or flag any "
    "comment of a task the caller can open."
)
MCP_OWN_SENTENCE = (
    "only comments the caller wrote can be changed, so match `sender_id` against `get_current_user`"
)
MCP_OWN_SENTENCE_ON = (
    "to EDIT with `body_text` only comments the caller wrote qualify, so match `sender_id` against "
    "`get_current_user`"
)


UPDATE_COMMENT = Tool(
    name="update_comment",
    description=(
        "Fix one of the user's OWN comments on a task. Call `fetch_task` FIRST: it lists each "
        "comment's `comment_id` and `sender_id` (only comments the user wrote can be changed, so "
        "match `sender_id` against their id from `get_current_user`). REQUIRES USER APPROVAL — "
        "the user sees the new text before it replaces the old. Required: task_id, comment_id, "
        "body_text. The whole comment is replaced, so send the full new text. Use it to fix a "
        "typo or a wrong detail; to say something new, use add_comment."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "Numeric task id the comment is on.",
            },
            "comment_id": {
                "type": "INTEGER",
                "description": "The comment's id on that task, from fetch_task.",
            },
            "body_text": {
                "type": "STRING",
                "description": (
                    "The full new comment, in markdown (headings, lists, **bold**, *italic*, "
                    "`code`, fenced code blocks, tables, links)." + CHIP_SYNTAX_HINT
                ),
            },
        },
        "required": ["task_id", "comment_id", "body_text"],
    },
    run=_run,
    requires_approval=True,
)
