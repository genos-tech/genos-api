"""`edit_note_block` (MIP-1646): MCP-only, see `note_blocks/__init__.py`.

Replace, insert after, or delete ONE block of a note, by the `block_id` `get_note(include_blocks=true)`
returned. `content_text` is markdown (the same dialect as `update_note`), converted to blocks here and
spliced in by genos-collab on the note's live doc, so every other block, and anything a person is typing
into the note, is left alone.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from apis.request_id import log_failure
from origin.search_engine.agent.chip_syntax import CHIP_SYNTAX_HINT
from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.body_limits import check_body_size
from origin.search_engine.agent.tools.body_mentions import notify_note_mentions
from origin.search_engine.agent.tools.note_access import has_write_permission, load_readable_note
from origin.search_engine.agent.tools.note_body_hash import note_body_hash
from origin.search_engine.agent.tools.update_note import _wrap_blocknote
from origin.services import collab_push
from origin.services.archive import archived_note_refusal
from origin.views.public.mcp.note_blocks import _stored
from origin.views.utils.note_version import keep_current_state, snapshot_note_version

log = logging.getLogger(__name__)

OPS = ("replace", "insert_after", "delete")
_BLOCK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_BLOCK_HASH = re.compile(r"^[0-9a-f]{16}$")
_READ_AGAIN = "Read the note with `get_note` and `include_blocks: true` to see its current blocks."


def _block_id(raw: Any) -> str:
    text = raw.strip() if isinstance(raw, str) else ""
    if not _BLOCK_ID.match(text):
        raise ToolError(
            "`block_id` is the id of one block: take it from `blocks` in `get_note` "
            "(call it with `include_blocks: true`), or pass the block's link (the note's URL "
            "ending `?block=<id>`) as `note_id`."
        )
    return text


def _base_hash(raw: Any) -> str | None:
    if raw is None:
        return None
    text = raw.strip() if isinstance(raw, str) else ""
    if not _BLOCK_HASH.match(text):
        raise ToolError(
            "`base_block_hash` must be the `block_hash` string from `get_note`'s `blocks`."
        )
    return text


def _new_blocks(op: str, raw: Any, ctx: ToolContext) -> list[dict[str, Any]]:
    text = raw.strip() if isinstance(raw, str) else ""
    if op == "delete":
        if text:
            raise ToolError("`delete` takes no `content_text`: it removes the block.")
        return []
    if not text:
        raise ToolError(f"`content_text` is required for `{op}`: the markdown that goes in.")
    check_body_size(text, "content_text")
    blocks = _wrap_blocknote(text, ctx)
    if not blocks:
        raise ToolError("`content_text` produced no blocks.")
    return blocks


def _refusal(err: collab_push.BlockEditError, block_id: str) -> ToolError:
    """A collab "no" as the sentence the caller reads. Nothing here echoes a note's text."""
    kind = err.kind
    if kind == "block_not_found":
        return ToolError(
            f"Block {block_id} was not found in this note (it may have been deleted or "
            f"replaced). {_READ_AGAIN} Nothing was changed."
        )
    if kind == "stale":
        return ToolError(
            f"Block {block_id} changed since you read it (`base_block_hash` is stale). "
            f"{_READ_AGAIN} Nothing was changed."
        )
    if kind == "refused":
        return ToolError(f"The editor service rejected the edit: {err}. Nothing was changed.")
    if kind == "unsupported":
        return ToolError(
            "Editing one block is not available on this server yet: the editor service has not "
            "been updated. Nothing was changed."
        )
    if kind == "unknown":
        return ToolError(
            "The edit was sent but no answer came back, so it may or may not have been made. "
            "Read the note with `get_note` before editing again, or a block may appear twice."
        )
    return ToolError(
        "This note's editor service could not be reached right now. Nothing was changed; "
        "try again in a moment."
    )


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    op = args.get("op")
    if op not in OPS:
        raise ToolError(f"`op` must be one of {', '.join(OPS)}.")
    block_id = _block_id(args.get("block_id"))
    base_hash = _base_hash(args.get("base_block_hash"))
    new_blocks = _new_blocks(op, args.get("content_text"), ctx)

    ref = load_readable_note(args, ctx)
    if not has_write_permission(
        owner_id=getattr(ref.note, "owner_id", None),
        note_id=ref.note_id,
        note_type_code=ref.code,
        ctx=ctx,
    ):
        raise ToolForbidden(
            f"Not authorized to edit note {ref.note_id}. You must be the owner, have editor "
            "permission on it, or be a member of the project or chat it belongs to."
        )
    if why := archived_note_refusal(ref.family, ref.note_id):
        raise ToolError(f"{why} Nothing was changed.")
    note = ref.note

    try:
        done = collab_push.edit_note_block(
            ref.family,
            ref.note_id,
            block_id=block_id,
            op=op,
            blocks=new_blocks or None,
            base_block_hash=base_hash,
        )
        body_after, new_ids, live = done["blocks"], list(done.get("newBlockIds") or []), "applied"
    except collab_push.BlockEditError as err:
        if err.kind != "no_document":
            raise _refusal(err, block_id)
        # Nobody ever opened this note, so there is no live doc and no editor to disturb: the stored
        # body is the whole truth, and it is edited here the same way collab would.
        current = _stored.find_block(note.body, block_id)
        if current is None:
            raise _refusal(collab_push.BlockEditError("block_not_found"), block_id)
        if base_hash and _stored.block_hash(current) != base_hash:
            raise _refusal(collab_push.BlockEditError("stale"), block_id)
        spliced = _stored.splice(note.body, block_id, op, new_blocks)
        if spliced is None:  # pragma: no cover - find_block just found it
            raise _refusal(collab_push.BlockEditError("block_not_found"), block_id)
        body_after, new_ids = spliced
        live = "not_open"

    # What the edit replaces is kept first, so it can always be walked back from the note's history.
    try:
        keep_current_state(
            team=note.team,
            note_type=ref.code,
            note_id=ref.note_id,
            title=note.title,
            body=note.body,
        )
        note.body = body_after
        note.save(update_fields=["body"])
    except Exception as exc:  # noqa: BLE001
        reference = log_failure("edit_note_block save failed", exc)
        if live == "applied":
            raise ToolError(
                "The block was changed in the editor but the note could not be saved "
                f"(ref {reference}); the editor's next autosave will store it."
            )
        raise ToolError(f"Failed to update note (ref {reference}).")

    try:
        from origin.models.common.user_models import CustomUser  # noqa: PLC0415

        snapshot_note_version(
            team=note.team,
            editor=CustomUser.objects.filter(id=ctx.user_id).first(),
            note_type=ref.code,
            note_id=ref.note_id,
            title=note.title,
            body=note.body,
            force_new=True,
        )
    except Exception:  # noqa: BLE001
        log.exception("edit_note_block: version snapshot failed for note %s", ref.note_id)
    # `@` chips ping their people, diffed against the stored set (MIP-708).
    notify_note_mentions(note, note_type=ref.code, actor_id=ctx.user_id)

    verb = {"replace": "Replaced", "insert_after": "Inserted after", "delete": "Deleted"}[op]
    return {
        "note_id": ref.note_id,
        "note_type": ref.family,
        "block_id": block_id,
        "op": op,
        # What the member's "your AI app edited a note" notice reads (`mcp_notice._edited_note`).
        "changed_fields": ["body"],
        "new_block_ids": new_ids,
        "body_hash": note_body_hash(note.body),
        "live_editor": live,
        "__summary__": f"{verb} block {block_id} of {ref.family} note #{ref.note_id}.",
    }


EDIT_NOTE_BLOCK = Tool(
    name="edit_note_block",
    description=(
        "Replace, insert after, or delete ONE block of a note by its `block_id`. REQUIRES USER "
        "APPROVAL. Pass `note_type` with `note_id`, the `block_id` from the note's `blocks`, and "
        "`op`; `content_text` is the markdown that goes in."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "shared", "task", "chat"],
                "description": "Which kind of note (personal = My Notes).",
            },
            "note_id": {"type": "INTEGER", "description": "The note's id."},
            "block_id": {"type": "STRING", "description": "The block: `block_id` from `blocks`."},
            "op": {
                "type": "STRING",
                "enum": list(OPS),
                "description": (
                    "`replace` swaps the block and its children for `content_text`; "
                    "`insert_after` adds `content_text` as the block's next siblings; "
                    "`delete` removes the block and its children."
                ),
            },
            "content_text": {
                "type": "STRING",
                "description": (
                    "The markdown for `replace` / `insert_after`, one or several blocks. Omit for "
                    "`delete`." + CHIP_SYNTAX_HINT
                ),
            },
            "base_block_hash": {
                "type": "STRING",
                "description": (
                    "The `block_hash` `get_note` returned for this block. If the block changed "
                    "since, the call is refused and nothing is written. Omit to skip the check."
                ),
            },
        },
        "required": ["note_type", "note_id", "block_id", "op"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)
