"""`save_conversation_summary` (MIP-1622): MCP-only, see `secretary/__init__.py`.

A member can keep a SHORT summary of a conversation they had in Claude or ChatGPT: a few
sentences and done / next / waiting / decision items, never the conversation. It is an
ordinary private note in a folder of the member's own My Notes, so there is no new table: the
note is exported with their other notes, deleted with their account, and searchable (by its
author only: a My Notes note is indexed with its owner as the one reader, and a folder in My
Notes is never shared).

Why a tool and not `create_note` plus a rule: the rule can say "put it in the folder", but a
model has to look the folder up, create it once and not make a second one, and it has to
avoid writing a second note each time it offers. The server does both here, without trusting
the client:

* **The folder** is found by name among the CALLER's own My Notes folders, or created, on
  every call. If the member renamed it, the next save makes a new one; if they deleted it,
  the same. Nothing is ever filed in a Team Notes folder.
* **De-duplication.** The model passes a `conversation_ref` it makes up once per
  conversation. The note ends with a line carrying it; a second save with the same ref by
  the same author, inside the folder and within 12 hours of the first, REPLACES that note
  (its old text stays in the note's version history). The ref is not trusted for anything
  else: it is a label, looked up only among the caller's own notes.

The writes themselves go through the `create_note` and `update_note` tools, so the quota, the
size cap, the version snapshot and the chip handling are theirs and cannot drift.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.db.models import F
from django.utils import timezone

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent.tools import Tool, ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import CREATE_NOTE
from origin.search_engine.agent.tools.update_note import UPDATE_NOTE
from origin.services.note_folder_manage import create_personal_folder

FOLDER_NAME = "AI chat summaries"
TITLE_PREFIX = "AI chat summary: "
MARKER_PREFIX = "Conversation ref: "
# A second save with the same ref inside this window replaces the first.
REPLACE_WINDOW = timedelta(hours=12)

TOPIC_MAX = 80
SUMMARY_MAX = 1200
ITEM_MAX = 240
ITEMS_MAX = 12
REF_MAX = 64
# How many of the author's recent notes in the folder are looked at for a matching ref.
_LOOKBACK = 50

KINDS = ("done", "next", "waiting", "decision")
_HEADINGS = {"done": "Done", "next": "Next", "waiting": "Waiting", "decision": "Decision"}

# A chip that would notify someone (`[@Aki](user:<uuid>)`, `[@design](group:3)`): a private
# summary must not ping anyone, so it is cut back to its label.
_NOTIFYING_CHIP = re.compile(r"\[([^\]]*)\]\((?:user|group):[^)]*\)")
_MARKER_LINE = re.compile(r"^" + re.escape(MARKER_PREFIX) + r"([a-z0-9-]+)$")


def _text(value: Any, field: str, limit: int, *, required: bool = True) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise ToolError(f"`{field}` is required. Nothing was saved.")
        return ""
    if not isinstance(value, str):
        raise ToolError(f"`{field}` must be text. Nothing was saved.")
    text = _NOTIFYING_CHIP.sub(lambda m: m.group(1), value).strip()
    if len(text) > limit:
        raise ToolError(
            f"`{field}` is {len(text)} characters; the limit is {limit}. This is a SHORT "
            "summary (a few sentences and short items), never the conversation: shorten it "
            "and call again. Nothing was saved."
        )
    return text


def normalize_ref(raw: Any) -> str:
    """The ref as stored: lower-case letters, digits and hyphens, at most `REF_MAX`."""
    if not isinstance(raw, str) or not raw.strip():
        raise ToolError(
            "`conversation_ref` is required: a short label you make up once for this "
            "conversation and reuse if you save again. Nothing was saved."
        )
    ref = re.sub(r"[^a-z0-9]+", "-", raw.strip().lower()).strip("-")
    if not ref:
        raise ToolError("`conversation_ref` needs letters or digits. Nothing was saved.")
    return ref[:REF_MAX].strip("-")


def _items(raw: Any) -> list[tuple[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ToolError("`items` must be a list. Nothing was saved.")
    if len(raw) > ITEMS_MAX:
        raise ToolError(
            f"`items` has {len(raw)} entries; the limit is {ITEMS_MAX}. Keep the ones that "
            "matter. Nothing was saved."
        )
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ToolError("Each item needs a `kind` and a `text`. Nothing was saved.")
        kind = entry.get("kind")
        if kind not in KINDS:
            raise ToolError(f"An item's `kind` must be one of {list(KINDS)}. Nothing was saved.")
        text = _text(entry.get("text"), "an item's `text`", ITEM_MAX)
        out.append((kind, " ".join(text.split())))
    return out


def build_body(summary: str, items: list[tuple[str, str]], ref: str) -> str:
    """The note's markdown: the summary, the items under their headings, then the ref line."""
    parts = [summary]
    for kind in KINDS:
        texts = [text for k, text in items if k == kind]
        if texts:
            parts.append(f"**{_HEADINGS[kind]}**\n" + "\n".join(f"- {t}" for t in texts))
    parts.append(f"{MARKER_PREFIX}{ref}")
    return "\n\n".join(parts)


def _block_text(block: Any) -> str:
    if not isinstance(block, dict):
        return ""
    content = block.get("content")
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict)).strip()
    return ""


def ref_of(body: Any) -> str | None:
    """The ref a saved note carries: its last line that is a ref line, or None."""
    if not isinstance(body, list):
        return None
    for block in reversed(body):
        match = _MARKER_LINE.match(_block_text(block))
        if match:
            return match.group(1)
    return None


def find_or_create_folder(ctx: ToolContext) -> PersonalNoteFolder:
    """The caller's own My Notes folder for summaries. Only the caller's personal-scope
    folders are looked at, so no one else's folder and no Team Notes folder can be returned."""
    folder = (
        PersonalNoteFolder.objects.filter(
            team=ctx.team_id,
            owner=ctx.user_id,
            scope=PersonalNoteFolder.SCOPE_PERSONAL,
            name=FOLDER_NAME,
        )
        # The top level first, then the oldest: one folder keeps getting the notes.
        .order_by(F("parent_folder_id").asc(nulls_first=True), "folder_id")
        .first()
    )
    if folder is not None:
        return folder
    return create_personal_folder(team_id=ctx.team_id, owner_id=ctx.user_id, name=FOLDER_NAME)


def _earlier_note(folder: PersonalNoteFolder, ref: str, ctx: ToolContext):
    recent = PersonalNoteMaster.objects.filter(
        team=ctx.team_id,
        owner=ctx.user_id,
        folder_id=folder.folder_id,
        parent_note_id__isnull=True,
        ts_created_at__gte=timezone.now() - REPLACE_WINDOW,
    ).order_by("-ts_created_at")[:_LOOKBACK]
    return next((n for n in recent if ref_of(n.body) == ref), None)


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    topic = " ".join(_text(args.get("topic"), "topic", TOPIC_MAX).split())
    summary = _text(args.get("summary"), "summary", SUMMARY_MAX)
    items = _items(args.get("items"))
    ref = normalize_ref(args.get("conversation_ref"))

    title = f"{TITLE_PREFIX}{topic}"
    body = build_body(summary, items, ref)
    folder = find_or_create_folder(ctx)

    earlier = _earlier_note(folder, ref, ctx)
    if earlier is not None:
        done = UPDATE_NOTE.run(
            {
                "note_type": "personal",
                "note_id": earlier.note_id,
                "title": title,
                "content_text": body,
            },
            ctx,
        )
        replaced = True
    else:
        done = CREATE_NOTE.run(
            {
                "note_type": "personal",
                "title": title,
                "content_text": body,
                "folder_id": folder.folder_id,
            },
            ctx,
        )
        replaced = False

    verb = "Replaced the earlier summary of this conversation" if replaced else "Saved"
    return {
        "note_id": done["note_id"],
        "note_type": "personal",
        "title": title,
        "folder_id": folder.folder_id,
        "folder": FOLDER_NAME,
        "replaced_earlier_summary": replaced,
        "conversation_ref": ref,
        "__summary__": f'{verb}: "{title[:60]}" in My Notes / {FOLDER_NAME}',
    }


SAVE_CONVERSATION_SUMMARY = Tool(
    name="save_conversation_summary",
    description=(
        "Save a SHORT summary of this conversation as a private note in the member's My Notes "
        "folder 'AI chat summaries' (made if it does not exist)."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "topic": {
                "type": "STRING",
                "description": f"What the conversation was about, a few words (at most {TOPIC_MAX} "
                f"characters). The note is titled '{TITLE_PREFIX}<topic>'.",
            },
            "summary": {
                "type": "STRING",
                "description": f"1 to 5 sentences (at most {SUMMARY_MAX} characters). Never a "
                "transcript or a quote of the conversation.",
            },
            "items": {
                "type": "ARRAY",
                "description": f"Optional, at most {ITEMS_MAX}: what was done, what comes next, "
                "what is waited on and what was decided. Name a task by its display id "
                "(PRJ-123) only when you saw it in a tool result.",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "kind": {
                            "type": "STRING",
                            "enum": list(KINDS),
                            "description": "done, next, waiting or decision.",
                        },
                        "text": {
                            "type": "STRING",
                            "description": f"One short line (at most {ITEM_MAX} characters).",
                        },
                    },
                    "required": ["kind", "text"],
                },
            },
            "conversation_ref": {
                "type": "STRING",
                "description": "A short label (letters, digits, hyphens) that you make up the "
                "first time you save in this conversation, and reuse if you save again in it: "
                "a second save with the same label within 12 hours REPLACES the first note "
                "instead of adding another.",
            },
        },
        "required": ["topic", "summary", "conversation_ref"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
