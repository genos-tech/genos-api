"""What `get_note` adds for an HTML note on MCP (MIP-2214), only while `HTML_NOTES` is on.

`body_format` is on every answer ("html" or "blocknote") so a client learns what kind of note it
holds; an HTML note also says its `html_rev`, `html_size_bytes` and the saved scan's
`html_warnings`. The page itself comes only on request (`include_html: true`): it can be 512 KB, and
`body_markdown` is the readable placeholder that search sees. Access was already checked by the
registry tool that built the payload, so this reads a note the caller may read.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings

from origin.search_engine.agent.tools.base import ToolContext
from origin.search_engine.agent.tools.note_access import load_readable_note
from origin.services import html_notes


def with_html_fields(
    payload: dict[str, Any], args: dict[str, Any], mcp_args: dict[str, Any], ctx: ToolContext
) -> dict[str, Any]:
    if not getattr(settings, "HTML_NOTES", False):
        return payload
    try:
        note_id = int(args.get("note_id"))
    except (TypeError, ValueError):
        return payload
    try:
        ref = load_readable_note(
            {"note_type": str(args.get("note_type") or ""), "note_id": note_id}, ctx
        )
    except Exception:  # noqa: BLE001 - the registry tool already answered; add nothing on a miss
        return payload
    fmt = getattr(ref.note, "body_format", None) or html_notes.BODY_FORMAT_BLOCKNOTE
    out = {**payload, "body_format": fmt}
    if fmt != html_notes.BODY_FORMAT_HTML:
        return out
    revision = html_notes.latest_revision(ref.code, ref.note_id)
    if revision is None:
        return out
    out.update(
        {
            "html_rev": revision.rev,
            "html_size_bytes": revision.size,
            "html_warnings": revision.warnings,
        }
    )
    wanted = mcp_args.get("include_html")
    if wanted is True or str(wanted).lower() == "true":
        out["html"] = revision.html
    else:
        out["html_note"] = (
            "This is an HTML note: `body_markdown` is only the readable text made from its page. "
            "Pass `include_html: true` to read the page, and change it with `save_html_note`."
        )
    return out
