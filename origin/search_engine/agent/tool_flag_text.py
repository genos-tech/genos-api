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

from origin.search_engine.agent.parity import PARITY_FLAG
from origin.search_engine.agent.tools import ancestor_rule as _rule
from origin.search_engine.agent.tools import create_task as _create_task
from origin.search_engine.agent.tools import create_task_plan as _create_task_plan
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
)

#: `(tool name, SEARCH_ENGINE flag, sentence in the description, its wording while the flag is on)`
REPLACEMENTS: tuple[tuple[str, str, str, str], ...] = (
    ("create_task", PARITY_FLAG, _create_task.STATUS_SENTENCE, _create_task.STATUS_SENTENCE_ON),
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
