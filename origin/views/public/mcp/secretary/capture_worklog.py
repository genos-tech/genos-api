"""`capture_worklog` (MIP-1071): MCP-only, see `secretary/__init__.py`.

Saves what the member said they did, will do, wait on or decided, and the items a
model pulled out of it. The logic is in `origin/services/worklog.py`.
"""

from __future__ import annotations

from typing import Any

from origin.models.common.worklog_models import SOURCE_MCP
from origin.search_engine.agent.tools import Tool, ToolContext
from origin.services import worklog

_ITEM_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "kind": {
            "type": "STRING",
            "enum": list(worklog.KINDS),
            "description": (
                "done = finished; next = something the member will do; waiting = "
                "something they wait on others for; decision = something decided; "
                "note = anything else worth keeping."
            ),
        },
        "summary": {
            "type": "STRING",
            "description": f"One short line in the member's words, at most {worklog.MAX_SUMMARY_CHARS} characters.",
        },
        "evidence": {
            "type": "STRING",
            "description": (
                "The exact words of `text` this item comes from, copied, not "
                "reworded. An item whose evidence is not in `text` is kept and marked "
                "`evidence_matched: false`."
            ),
        },
        "refs": {
            "type": "ARRAY",
            "description": (
                "Things in Genos this item is about: {type: task|milestone|note|project, "
                "ref: a display id such as PRJ-12, a Genos URL, or an id; for a note "
                "given as a bare id also note_type: personal|task|chat}. Only things "
                "the member can open are linked; the rest come back in `unresolved_refs`."
            ),
            "items": {
                "type": "OBJECT",
                "properties": {
                    "type": {"type": "STRING", "enum": list(worklog.REF_TYPES)},
                    "ref": {"type": "STRING"},
                    "note_type": {"type": "STRING", "enum": ["personal", "task", "chat"]},
                },
                "required": ["type", "ref"],
            },
        },
        "due_hint": {
            "type": "STRING",
            "description": "When it is for, in the member's words (Friday, next week). Not a date.",
        },
        "person_hint": {
            "type": "STRING",
            "description": "Who it involves, as the member named them. Not looked up.",
        },
    },
    "required": ["kind", "summary"],
}


def _capture(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    return worklog.capture(
        ctx,
        text=args.get("text"),
        items=args.get("items"),
        occurred_at=args.get("occurred_at"),
        reviewed=args.get("reviewed", False),
        source=SOURCE_MCP,
    )


CAPTURE_WORKLOG = Tool(
    name="capture_worklog",
    description=(
        "Save text in the member's private worklog scratchpad, only when the member asks for that."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "STRING",
                "description": (
                    f"What the member said, as they said it, at most {worklog.MAX_TEXT_CHARS} "
                    "characters. Kept exactly as written."
                ),
            },
            "items": {
                "type": "ARRAY",
                "description": (
                    f"The things in `text`, at most {worklog.MAX_ITEMS_PER_ENTRY}. Leave out "
                    "to save the note alone."
                ),
                "items": _ITEM_SCHEMA,
            },
            "occurred_at": {
                "type": "STRING",
                "description": (
                    "When it happened, an ISO date-time (the member's own time zone if it "
                    "carries no offset). Default now. Never in the future."
                ),
            },
            "reviewed": {
                "type": "BOOLEAN",
                "description": (
                    "true only if the member has seen the items and confirmed them in this "
                    "conversation. Otherwise they are saved as suggestions."
                ),
            },
        },
        "required": ["text"],
    },
    run=_capture,
    # An MCP-only tool is never asked for approval (scope is the gate there); this marks
    # it as a write, which the catalog contract test checks against the catalog's flag.
    requires_approval=True,
    mcp_only=True,
)
