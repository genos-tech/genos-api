"""Flag-gated additions to a tool's DESCRIPTION (what the model reads in the declaration).

A tool whose result gains a field behind a default-off `SEARCH_ENGINE` flag has to say so in its
description, or the model has no way to know what the field is for. The description sits on a
frozen `Tool` that the whole process shares, so it is never edited in place: the declaration
builder (`controller._build_tool_declarations`) asks `declared_description(tool)` for the text,
which is the tool's own description when the flag is off (byte for byte) and that text plus the
one sentence when it is on. The flag is read at call time, so an eval arm can flip it.

One entry per (tool, flag). Append, never reorder: with two flags on for one tool the sentences
follow the order of `ADDITIONS`.

`REPLACEMENTS` is for the one case an appended sentence cannot do: the description already SAYS
the opposite ("Status is always created as 'Open'"), and a model that reads both keeps obeying the
first. While the flag is on the base sentence is swapped for its new wording, in place; with the
flag off the description is the tool's own, byte for byte. A test holds each `old` text to the
tool's description, so an edit to the base sentence cannot turn the replacement into a no-op.
The parameters themselves are `tool_flag_params`.
"""

from __future__ import annotations

from django.conf import settings

from origin.search_engine.agent.parity import FOLLOWUPS_FLAG, PARITY_FLAG
from origin.search_engine.agent.tool_flag_params import (
    ON_IT_CLEAR_SENTENCE,
    ON_IT_CLEAR_SENTENCE_ON,
)
from origin.search_engine.agent.tools import ancestor_rule as _rule
from origin.search_engine.agent.tools import create_calendar_event as _create_calendar_event
from origin.search_engine.agent.tools import create_task as _create_task
from origin.search_engine.agent.tools import create_task_plan as _create_task_plan
from origin.search_engine.agent.tools import delete_calendar_event as _delete_calendar_event
from origin.search_engine.agent.tools import list_calendar_events as _list_calendar_events
from origin.search_engine.agent.tools import list_calendars as _list_calendars
from origin.search_engine.agent.tools import list_project_tags as _list_project_tags
from origin.search_engine.agent.tools import milestone_convert as _milestone_convert
from origin.search_engine.agent.tools import task_attachments as _task_attachments
from origin.search_engine.agent.tools import todo_source as _todo_source
from origin.search_engine.agent.tools import update_comment as _update_comment
from origin.search_engine.agent.tools import update_task as _update_task
from origin.search_engine.agent.tools.base import Tool
from origin.search_engine.agent.tools.fetch_note import DESCRIPTION_ADDITION as _NOTE_THREADS
from origin.search_engine.agent.tools.get_current_user import DESCRIPTION_ADDITION as _TEAM
from origin.search_engine.agent.tools.inbox_report import DESCRIPTION_ADDITION as _INBOX_REPORT
from origin.search_engine.agent.tools.task_anchors import DESCRIPTION_ADDITION as _ANCHORS

#: `(tool name, SEARCH_ENGINE flag, sentence appended to the description)`
ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("fetch_task", "AGENT_FETCH_TASK_ANCHORS", _ANCHORS),
    ("list_my_inbox", "AGENT_INBOX_REPORT_LABEL", _INBOX_REPORT),
    ("get_current_user", "AGENT_TEAM_IN_CURRENT_USER", _TEAM),
    ("fetch_note", "AGENT_NOTE_THREADS_IN_GET_NOTE", _NOTE_THREADS),
    ("get_my_focus_tasks", "AGENT_CLOSED_ANCESTOR_RULE", _rule.FOCUS_ADDITION),
    ("get_my_task_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("get_milestone_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("get_sprint_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_milestones", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_my_milestones", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_tasks", "AGENT_CLOSED_ANCESTOR_RULE", _rule.ROWS_ADDITION),
    ("fetch_task", "AGENT_CLOSED_ANCESTOR_RULE", _rule.ROWS_ADDITION),
    # MIP-1561: the parity batch's parameters (`tool_flag_params`).
    ("create_task", PARITY_FLAG, _create_task.DESCRIPTION_ADDITION),
    ("update_task", PARITY_FLAG, _update_task.DESCRIPTION_ADDITION),
    ("create_task_plan", PARITY_FLAG, _create_task_plan.DESCRIPTION_ADDITION),
    # MIP-1562: reaction / flag on a comment, and a to-do that says where it came from.
    ("update_comment", PARITY_FLAG, _update_comment.DESCRIPTION_ADDITION),
    ("create_todo_item", PARITY_FLAG, _todo_source.DESCRIPTION_ADDITION),
    # MIP-1563: a task's files, as metadata, in the result of the task read.
    ("fetch_task", PARITY_FLAG, _task_attachments.DESCRIPTION_ADDITION),
    # MIP-1564 has no row here: `manage_milestone`'s sentence REPLACES the one that said a move is not
    # supported (`REPLACEMENTS` below), so it sits where that one was.
    # MIP-1059: several Google accounts. The parameter says the rest; these say what a read covers and
    # what an unknown event on the wrong account looks like.
    ("list_calendars", PARITY_FLAG, _list_calendars.DESCRIPTION_ADDITION),
    ("list_calendar_events", PARITY_FLAG, _list_calendar_events.DESCRIPTION_ADDITION),
    # A write proposes before it reads, so a member's "personal calendar" needs the account looked up first
    # (the model skipped it in Japanese, 2 of 3). On create only: the reads say `other_accounts`, an update
    # and a delete have found their event first.
    ("create_calendar_event", PARITY_FLAG, _create_calendar_event.DESCRIPTION_ADDITION),
    ("delete_calendar_event", PARITY_FLAG, _delete_calendar_event.DESCRIPTION_ADDITION),
    # MIP-1607 (AGENT_PARITY_FOLLOWUPS): `account: "all"` reads every account in one call. After the sentence above.
    ("list_calendar_events", FOLLOWUPS_FLAG, _list_calendar_events.FOLLOWUPS_DESCRIPTION_ADDITION),
    # MIP-1589 (the follow-ups flag): a project's required fields and the names of its templates.
    ("list_project_tags", FOLLOWUPS_FLAG, _list_project_tags.DESCRIPTION_ADDITION),
)

#: `(tool name, SEARCH_ENGINE flag, sentence in the description, its wording while the flag is on)`
REPLACEMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("create_task", PARITY_FLAG, _create_task.STATUS_SENTENCE, _create_task.STATUS_SENTENCE_ON),
    (
        "update_comment",
        PARITY_FLAG,
        _update_comment.REQUIRED_SENTENCE,
        _update_comment.REQUIRED_SENTENCE_ON,
    ),
    ("update_comment", PARITY_FLAG, _update_comment.OWN_SENTENCE, _update_comment.OWN_SENTENCE_ON),
    (
        "manage_milestone",
        PARITY_FLAG,
        _milestone_convert.MOVE_SENTENCE,
        _milestone_convert.MOVE_SENTENCE_ON,
    ),
)

#: `REPLACEMENTS` for the MCP catalog, whose descriptions are worded for MCP: the same swap, for a
#: sentence that says the tool only edits.
MCP_REPLACEMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("update_comment", PARITY_FLAG, _update_comment.MCP_OPENING, _update_comment.MCP_OPENING_ON),
    (
        "update_comment",
        PARITY_FLAG,
        _update_comment.MCP_OWN_SENTENCE,
        _update_comment.MCP_OWN_SENTENCE_ON,
    ),
    (
        "manage_milestone",
        PARITY_FLAG,
        _milestone_convert.MCP_MOVE_SENTENCE,
        _milestone_convert.MCP_MOVE_SENTENCE_ON,
    ),
    # MIP-1588: the MCP-only tool that keeps the member's marks offers "On it" too.
    ("set_item_importance", PARITY_FLAG, ON_IT_CLEAR_SENTENCE, ON_IT_CLEAR_SENTENCE_ON),
)


def declared_description(tool: Tool) -> str:
    text = tool.description
    for name, flag, old, new in REPLACEMENTS:
        if name == tool.name and settings.SEARCH_ENGINE.get(flag):
            text = text.replace(old, new, 1)
    for name, flag, addition in ADDITIONS:
        if name == tool.name and settings.SEARCH_ENGINE.get(flag):
            text += addition
    return text
