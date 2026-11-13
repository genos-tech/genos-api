"""Flag-gated additions to a tool's DESCRIPTION (what the model reads in the declaration).

A tool whose result gains a field behind a default-off `SEARCH_ENGINE` flag has to say so in its
description, or the model has no way to know what the field is for. The description sits on a
frozen `Tool` that the whole process shares, so it is never edited in place: the declaration
builder (`controller._build_tool_declarations`) asks `declared_description(tool)` for the text,
which is the tool's own description when the flag is off (byte for byte) and that text plus the
one sentence when it is on. The flag is read at call time, so an eval arm can flip it.

One entry per (tool, flag). Append, never reorder: with two flags on for one tool the sentences
follow the order of `ADDITIONS`.
"""

from __future__ import annotations

from django.conf import settings

from origin.search_engine.agent.tools import ancestor_rule as _rule
from origin.search_engine.agent.tools.base import Tool
from origin.search_engine.agent.tools.fetch_note import DESCRIPTION_ADDITION as _NOTE_THREADS
from origin.search_engine.agent.tools.get_current_user import DESCRIPTION_ADDITION as _TEAM
from origin.search_engine.agent.tools.get_task_history import DESCRIPTION_ADDITION as _CLIENT
from origin.search_engine.agent.tools.inbox_report import DESCRIPTION_ADDITION as _INBOX_REPORT
from origin.search_engine.agent.tools.task_anchors import DESCRIPTION_ADDITION as _ANCHORS

#: `(tool name, SEARCH_ENGINE flag, sentence appended to the description)`
ADDITIONS: tuple[tuple[str, str, str], ...] = (
    ("fetch_task", "AGENT_FETCH_TASK_ANCHORS", _ANCHORS),
    ("list_my_inbox", "AGENT_INBOX_REPORT_LABEL", _INBOX_REPORT),
    ("get_current_user", "AGENT_TEAM_IN_CURRENT_USER", _TEAM),
    ("get_task_history", "AGENT_CLIENT_IN_TASK_HISTORY", _CLIENT),
    ("fetch_note", "AGENT_NOTE_THREADS_IN_GET_NOTE", _NOTE_THREADS),
    ("get_my_focus_tasks", "AGENT_CLOSED_ANCESTOR_RULE", _rule.FOCUS_ADDITION),
    ("get_my_task_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("get_milestone_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("get_sprint_summary", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_milestones", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_my_milestones", "AGENT_CLOSED_ANCESTOR_RULE", _rule.COUNTS_ADDITION),
    ("list_tasks", "AGENT_CLOSED_ANCESTOR_RULE", _rule.ROWS_ADDITION),
    ("fetch_task", "AGENT_CLOSED_ANCESTOR_RULE", _rule.ROWS_ADDITION),
)


def declared_description(tool: Tool) -> str:
    text = tool.description
    for name, flag, addition in ADDITIONS:
        if name == tool.name and settings.SEARCH_ENGINE.get(flag):
            text += addition
    return text
