"""`save_html_note` (MIP-2214): MCP-only, see `html_notes/__init__.py`.

Stores an HTML page as a note, or replaces the page of an HTML note, and answers with what the page
needs to be fixed. The save NEVER fails for a fixable reason: it stores and warns. The only things
refused are the size cap and the rate limit (both worded as what to do), a note that is not an HTML
note (a BlockNote note is never converted: the answer suggests a new note), and what any note write
refuses (no access, archived, wrong target).

A new note is made by `create_note`'s own helpers, so the target, the ACL, the owner row, the
first version and the monthly note quota are exactly what `create_note` gives; this module adds
the page. A replace is checked like `update_note`: owner or editor of the note.
"""

from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.db import transaction

from apis.request_id import log_failure
from origin.search_engine.agent.tools import Tool, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.create_note import (
    _create_chat_note,
    _create_personal,
    _create_task_note,
    _resolve_folder_id,
)
from origin.search_engine.agent.tools.note_access import has_write_permission, load_readable_note
from origin.search_engine.agent.tools.note_targets import normalize_note_type
from origin.search_engine.quota import (
    NOTE_CREATE_KEY,
    check_remaining_monthly,
    increment_usage,
)
from origin.services import html_notes
from origin.services.archive import archived_note_refusal
from origin.views.public.mcp.html_notes.guide import rules_text

log = logging.getLogger(__name__)

_CODE = {
    "personal": html_notes.NOTE_TYPE_PERSONAL,
    "task": html_notes.NOTE_TYPE_TASK,
    "chat": html_notes.NOTE_TYPE_CHAT,
}


def _path(family: str, note) -> str | None:
    """Where the note opens in the app, when there is a deep link for it."""
    if family == "personal":
        return f"/workspace/notes/my/{note.note_id}"
    if family == "task" and getattr(note, "project_id", None) and getattr(note, "task_id", None):
        return f"/workspace/notes/task/project/{note.project_id}/task/{note.task_id}/note/{note.note_id}"
    return None


def _feature_on() -> None:
    # Defence in depth: the catalog does not list the tool while the switch is off and `call`
    # answers as for an unpublished name, so this only matters to a direct caller.
    if not getattr(settings, "HTML_NOTES", False):
        raise ToolError("HTML notes are not available yet.")


def _prepare(args: dict[str, Any]) -> html_notes.PreparedHtml:
    try:
        return html_notes.prepare(
            args.get("html"), title=args.get("title"), summary=args.get("summary")
        )
    except html_notes.HtmlNoteRefused as refused:
        raise ToolError(str(refused)) from None


def _count_save(ctx: ToolContext) -> None:
    try:
        html_notes.check_rate(ctx.user_id)
    except html_notes.HtmlNoteRefused as refused:
        raise ToolError(str(refused)) from None


def _answer(
    *,
    prepared: html_notes.PreparedHtml,
    revision,
    family: str,
    note,
    created: bool,
    space: str | None = None,
) -> dict[str, Any]:
    warnings = prepared.scan.warnings
    verb = "Created" if created else "Updated"
    out: dict[str, Any] = {
        "note_id": note.note_id,
        "note_type": family,
        "title": note.title,
        "created": created,
        "body_format": html_notes.BODY_FORMAT_HTML,
        "html_rev": revision.rev,
        "size_bytes": prepared.size,
        "warnings": warnings,
        # What the NOTE notice reads (`mcp_notice._edited_note`): the title and the page changed.
        "changed_fields": ["title", "body"],
        "__summary__": (
            f'{verb} HTML note #{note.note_id}: "{note.title[:60]}"'
            + (f" ({len(warnings)} warning{'s' if len(warnings) != 1 else ''})" if warnings else "")
        ),
    }
    if space:
        out["space"] = space
    if (path := _path(family, note)) is not None:
        out["path"] = path
    if warnings:
        out["suggestions"] = html_notes.suggestions_for(warnings)
        out["next_step"] = (
            "The page was saved as it is. To fix these, call `save_html_note` again with "
            f"`note_id` {note.note_id} and the corrected page."
        )
    else:
        out["next_step"] = "The page was saved and the scan found nothing to fix."
    return out


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    _feature_on()
    prepared = _prepare(args)
    if args.get("note_id") not in (None, ""):
        return _replace(args, ctx, prepared)
    return _create(args, ctx, prepared)


def _replace(
    args: dict[str, Any], ctx: ToolContext, prepared: html_notes.PreparedHtml
) -> dict[str, Any]:
    ref = load_readable_note(args, ctx)
    note = ref.note
    if (
        getattr(note, "body_format", html_notes.BODY_FORMAT_BLOCKNOTE)
        != html_notes.BODY_FORMAT_HTML
    ):
        # Never convert: a BlockNote note's blocks, comments and history would be lost.
        raise ToolError(
            f"Note {ref.note_id} is an ordinary note, and `save_html_note` does not convert "
            "one, because its text, comments and history would be lost. Nothing was changed. "
            "To make an HTML page, call `save_html_note` WITHOUT `note_id` to create a new HTML "
            "note (same `note_type`, folder or project), and link the two in the summary."
        )
    if not has_write_permission(
        owner_id=getattr(note, "owner_id", None),
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
    _count_save(ctx)

    from origin.models.common.user_models import CustomUser  # noqa: PLC0415

    user = CustomUser.objects.filter(id=ctx.user_id).first()
    try:
        revision = html_notes.replace_page(
            note_type=ref.code,
            note=note,
            user=user,
            prepared=prepared,
            keep_title=not (args.get("title") or "").strip(),
        )
    except Exception as exc:  # noqa: BLE001
        reference = log_failure("save_html_note failed", exc)
        raise ToolError(f"Failed to save the page (ref {reference}). Nothing was changed.")
    return _answer(
        prepared=prepared, revision=revision, family=ref.family, note=note, created=False
    )


def _create(
    args: dict[str, Any], ctx: ToolContext, prepared: html_notes.PreparedHtml
) -> dict[str, Any]:
    note_type = normalize_note_type(args.get("note_type"))
    if note_type == "shared":
        raise ToolError(
            "A shared note is someone else's note shared with you, so it can't be created. Use "
            "note_type='personal' for a note private to you, or 'team' for one in a Team Notes "
            "folder."
        )
    folder_id = _resolve_folder_id(args, note_type, ctx)
    allowed, _used, note_limit = check_remaining_monthly(ctx.user_id, NOTE_CREATE_KEY)
    if not allowed:
        raise ToolError(
            f"You've created all {note_limit} notes for this month. Upgrade your plan to keep going."
        )
    _count_save(ctx)

    # The note, its owner row and first version come from `create_note`'s helpers; the page is
    # added in the same transaction, so a failure leaves no note without a page.
    with transaction.atomic():
        if note_type in ("personal", "team"):
            made = _create_personal(
                title=prepared.title,
                body=prepared.body,
                folder_id=folder_id,
                space=note_type,
                ctx=ctx,
            )
        elif note_type == "task":
            made = _create_task_note(args=args, title=prepared.title, body=prepared.body, ctx=ctx)
        else:
            made = _create_chat_note(args=args, title=prepared.title, body=prepared.body, ctx=ctx)
        family = made["note_type"]
        code = _CODE[family]
        try:
            html_notes.mark_html(code, made["note_id"])
            revision = html_notes.append_revision(code, made["note_id"], ctx.user_id, prepared)
        except Exception as exc:  # noqa: BLE001
            reference = log_failure("save_html_note failed", exc)
            raise ToolError(f"Failed to save the page (ref {reference}). Nothing was created.")
    increment_usage(ctx.user_id, NOTE_CREATE_KEY)

    note = html_notes.NOTE_MODELS[code].objects.get(note_id=made["note_id"])
    result = _answer(
        prepared=prepared,
        revision=revision,
        family=family,
        note=note,
        created=True,
        space=made.get("space"),
    )
    for key in ("folder_id", "project_id", "task_id", "parent_context"):
        if made.get(key) is not None:
            result[key] = made[key]
    return result


SAVE_HTML_NOTE = Tool(
    name="save_html_note",
    description=(
        "Save a self-contained HTML page as a read-only note (a dashboard, report, diagram or "
        "explainer). REQUIRES USER APPROVAL. Pass `html` (the whole page) and `note_type` "
        "with `title`, as for `create_note`."
    ),
    parameters_schema={
        "type": "OBJECT",
        "properties": {
            "html": {
                "type": "STRING",
                "description": "The whole page, starting with <!doctype html>. At most 512 KB.",
            },
            "title": {
                "type": "STRING",
                "description": (
                    "The note's title. Defaults to the page's <title>; required if the page has "
                    "none. On a replace it renames the note; omit it to keep the title."
                ),
            },
            "summary": {
                "type": "STRING",
                "description": (
                    "1 to 3 sentences saying what the page shows. It is what search finds, so "
                    "always pass it."
                ),
            },
            "note_id": {
                "type": "INTEGER",
                "description": (
                    "Only to REPLACE the page of an existing HTML note (with `note_type`). "
                    "Omit it to create a new note."
                ),
            },
            "note_type": {
                "type": "STRING",
                "enum": ["personal", "team", "task", "chat"],
                "description": (
                    "Which kind of note: 'personal' (private), 'team' (a Team Notes folder), "
                    "'task' (a project) or 'chat'. Required to create, and with `note_id`."
                ),
            },
            "folder_id": {
                "type": "INTEGER",
                "description": (
                    "A personal note: one of your My-Notes folders (optional). A team note: the "
                    "Team Notes folder (required)."
                ),
            },
            "project_id": {
                "type": "INTEGER",
                "description": "Required when note_type='task': the project the note belongs to.",
            },
            "task_id": {
                "type": "INTEGER",
                "description": "Optional, with note_type='task': attach the note to one task.",
            },
            "chat_id": {
                "type": "STRING",
                "description": "Required when note_type='chat': the chat's id or Genos URL.",
            },
            "thread_id": {
                "type": "STRING",
                "description": "Optional, with note_type='chat': attach to one thread.",
            },
        },
        "required": ["html"],
    },
    run=_run,
    requires_approval=True,
    mcp_only=True,
)


def mcp_description() -> str:
    """The published description: what the tool does, then the rules for writing the page."""
    return (
        "Save an HTML page as a read-only note in Genos: a dashboard, a report, a diagram, an "
        "explainer. Use it when the user asks you to show, draw or describe something visually "
        "and keep it. It creates a note of any kind `create_note` does ('personal', 'team' with "
        "`folder_id`, 'task' with `project_id`, 'chat' with `chat_id`) under the same sharing "
        "rules as every note, or, with `note_id` and `note_type`, REPLACES the page of an HTML "
        "note you can edit (the earlier page is kept as a revision). It stores the page and "
        "warns: the save is refused only when the page is over 512 KB or you have saved 30 "
        "pages this hour. Anything else the page needs is returned as `warnings`, each with a "
        "fix in `suggestions`: read them and save again with `note_id` if you can improve the "
        "page. The page is shown in a sandboxed frame with no network, so follow these rules. "
        + rules_text()
        + " An ordinary note is never converted into an HTML note: to make a page, create a "
        "new note. `update_note` and `edit_note_block` do not change an HTML note's page: use "
        "this tool with `note_id`. Read a page back with `get_note` and `include_html: true`. "
        "Requires a write-scoped API key."
    )
