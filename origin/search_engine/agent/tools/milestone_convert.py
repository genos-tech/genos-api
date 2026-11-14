"""What `manage_milestone` says about converting and moving, behind `AGENT_PARITY_TOOLS` (MIP-1564).

Two things the app's milestone page does that the agent and MCP could not:

* CONVERT a root task into a milestone, and a milestone back into a task (`action: "convert"`, with
  `task_id` for the first and `milestone_id` for the second). The data changes are the app's own
  (`services/task_milestone_conversion.py`, which `POST /task/convert-to-milestone/` and
  `POST /milestone/convert-to-task/` call), so the row, its display id, its body and its history are
  what a person's click leaves.
* MOVE a milestone to another project (`update` with `project_id`), through the PATCH's own code
  (`services/milestone_manage.update_milestone(project_move=...)`).

This module holds only the TEXT: what the declaration says (the parameters, the one added sentence,
the two replaced ones) and the sentence a model reads for each refusal code of the conversion service.
The tool, `manage_milestone`, applies them only while the flag is on (`parity.parity_on`); with it off
the declaration is its own text, byte for byte, and `convert` / `task_id` / a `project_id` on update
are refused with the words they always were.

The refusal sentences are built from the SERVER's code and message (`ConversionRefused`), not from the
app's strings: the message says what is wrong, and `_NEXT` says what to try instead, in terms of the
tools a model has. A code the table does not know still gets the server's own message.
"""

from __future__ import annotations

from typing import Any

#: The enum value the flag adds to `action`, in the order it is appended.
PARITY_ENUM_VALUES: dict[str, tuple[str, ...]] = {"action": ("convert",)}

#: A parameter that exists only while the flag is on. The in-app declaration turns it into a task
#: reference (`MCP catalog: parity_task_ref_args`), so a model may send PRJ-12, a URL or an id.
PARITY_PROPERTIES: dict[str, dict[str, Any]] = {
    "task_id": {
        "type": "STRING",
        "description": (
            "For convert: the ROOT task to turn into a milestone (display id like PRJ-12, its URL "
            "or its id)."
        ),
    },
}

#: A parameter the base schema already has, worded for the flag: `project_id` also names the
#: destination of a move.
PARITY_DESCRIPTIONS: dict[str, str] = {
    "project_id": (
        "Project (from list_projects). For create and create_folder; for update it MOVES the "
        "milestone to this project."
    ),
}

#: Added to the description while the flag is on (`tool_flag_text`), in-app and over MCP.
DESCRIPTION_ADDITION = (
    " 'convert' (exactly one of `task_id`, `milestone_id`) turns a ROOT task (no parent, not in a "
    "milestone) into a milestone, or a milestone into a task, on the SAME row: display id, body, "
    "comments and history stay; a milestone's tasks stay under it as sub-tasks; its first assignee "
    "becomes the task's assignee and the others collaborators; its folder and sprint are dropped. "
    "'update' with `project_id` MOVES the milestone to another project of the team (no sprint_id, "
    "milestone_folder_id or tags in that call): its tasks go with it and get new display ids; the "
    "sprint, folder and tags are cleared. Neither can be undone."
)

#: The base sentence the flag stops the description from saying, and what replaces it (the app's
#: wording, `tool_flag_text.REPLACEMENTS`): an appended sentence would sit after "not supported"
#: and the model would keep obeying the first.
MOVE_SENTENCE = "Moving a milestone to another project is not supported. "
MOVE_SENTENCE_ON = ""
#: The same, in the MCP catalog's wording (`tool_flag_text.MCP_REPLACEMENTS`).
MCP_MOVE_SENTENCE = (
    "A milestone can't be moved to another project, and its status can't be 'Deleted': use "
    "`delete_milestone`."
)
MCP_MOVE_SENTENCE_ON = "A milestone's status can't be 'Deleted': use `delete_milestone`."

# --------------------------------------------------------------------------------------------- #
# The refusals                                                                                   #
# --------------------------------------------------------------------------------------------- #

#: What to try next, by the service's refusal code. `{ref}` is how the caller named the item.
_NEXT: dict[str, str] = {
    "not_a_root_task": (
        "Take it out from under its parent first (`update_task` with `parent_task_id` ''), or "
        "convert the top-level task it hangs under."
    ),
    "task_in_milestone": (
        "Take it out of the milestone first (`update_task` with `milestone_id` ''), then convert it."
    ),
    "already_a_milestone": (
        "To turn it back into a task, call `convert` with its `milestone_id` instead "
        "(`list_milestones` shows it)."
    ),
    "task_deleted": "Nothing to convert; find the task you mean with `list_tasks`.",
    "task_not_saved": "It is an empty draft, not a task yet; pick a saved task with `list_tasks`.",
    "task_not_found": "Find it with `list_tasks` or `search_workspace`.",
    "milestone_not_found": "`list_milestones` shows the project's milestones.",
    "milestone_row_deleted": "Nothing to convert.",
}


def refusal_sentence(code: str, message: str) -> str:
    """The sentence a model gets for a conversion the service refused: the service's own message,
    what to try next when there is something to try, and that nothing was changed."""
    text = (message or "").strip() or "The conversion was refused."
    nxt = _NEXT.get(code)
    return f"{text} {nxt + ' ' if nxt else ''}Nothing was changed."


def converted_hint(display_id: str | None, task_id: int, still_milestone_id: int | None) -> str:
    """A milestone that was already converted: where it went, for the model to carry on from."""
    where = f"the task {display_id or task_id} (task_id {task_id})"
    if still_milestone_id is not None:
        return (
            f"was already converted to {where}, which is a milestone again: milestone "
            f"{still_milestone_id} (`list_milestones`)."
        )
    return f"was already converted to {where}. Read it with `get_task`."
