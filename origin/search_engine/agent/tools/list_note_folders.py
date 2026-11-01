"""`list_note_folders` tool — show how the user's notes are filed.

Two folder spaces live in one table (`PersonalNoteFolder.scope`):

* My-Notes folders (`scope="personal"`): the user's own, owner-scoped,
  never shared.
* Team Notes folders (`scope="team"`): shared folders whose chain carries
  the ACL (`note_folder_role`). Only the ones this user can reach are
  listed, each with their role, so the agent knows where `create_note`
  (note_type='team') may put a note.

Folders are NOT indexed in OpenSearch, so `search_knowledge_base` returns
notes with no folder context; this tool is the agent's only window into
that structure. Each folder comes WITH the notes filed directly in it (plus
the unfiled My-Notes notes), so one call yields the actual organization
tree — enough to answer "what's in my Projects folder?" and to resolve a
folder NAME to the `folder_id` that `create_note` / `update_note` need.

ACL: folders and notes are filtered by `ctx.user_id` + `ctx.team_id`
(server-trusted), never by any id in the LLM args. Read only — no
approval. Folders shared from another team are not listed: the note tools
don't write across teams.

Only ROOT notes (`parent_note_id IS NULL`) carry a meaningful
`folder_id`; child notes ride along with their root, so they're omitted
here (the tree shows roots).
"""

from __future__ import annotations

from typing import Any

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.search_engine.agent.tools.base import Tool, ToolContext
from origin.views.utils.note_folder_role import load_team_folder_index, readable_team_folder_roles
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

# Defensive cap on how many notes we enumerate per space — note counts are
# typically modest, but a pathological account shouldn't produce a huge
# tool payload. Root notes only.
_MAX_NOTES = 500

_ROLE_LABEL = {ROLE_OWNER: "owner", ROLE_EDITOR: "editor"}


def _run(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:  # noqa: ARG001
    folders = list(
        PersonalNoteFolder.objects.filter(
            team=ctx.team_id, owner=ctx.user_id, scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        .values("folder_id", "parent_folder_id", "name")
        .order_by("name")
    )
    personal_ids = {f["folder_id"] for f in folders}

    root_notes = list(
        PersonalNoteMaster.objects.filter(
            team=ctx.team_id, owner=ctx.user_id, parent_note_id__isnull=True
        )
        .exclude(folder_id__in=_team_folder_ids(ctx))
        .values("note_id", "title", "folder_id")
        .order_by("title")[:_MAX_NOTES]
    )

    notes_by_folder: dict[int, list[dict[str, Any]]] = {}
    top_level_notes: list[dict[str, Any]] = []
    for n in root_notes:
        entry = {"note_id": n["note_id"], "title": n["title"] or ""}
        if n["folder_id"] is None or n["folder_id"] not in personal_ids:
            top_level_notes.append(entry)
        else:
            notes_by_folder.setdefault(n["folder_id"], []).append(entry)

    folder_out = [
        {
            "folder_id": f["folder_id"],
            "name": f["name"],
            "parent_folder_id": f["parent_folder_id"],
            "notes": notes_by_folder.get(f["folder_id"], []),
        }
        for f in folders
    ]

    team_out = _team_folders(ctx)

    return {
        "folders": folder_out,
        "top_level_notes": top_level_notes,
        "team_folders": team_out,
        "__summary__": (
            f"{len(folder_out)} My-Notes folder(s), {len(root_notes)} note(s) "
            f"({len(top_level_notes)} unfiled); {len(team_out)} Team Notes folder(s)."
        ),
    }


def _team_folder_ids(ctx: ToolContext) -> list[int]:
    return list(
        PersonalNoteFolder.objects.filter(
            team=ctx.team_id, scope=PersonalNoteFolder.SCOPE_TEAM
        ).values_list("folder_id", flat=True)
    )


def _team_folders(ctx: ToolContext) -> list[dict[str, Any]]:
    """Team Notes folders this user can reach, with their role and the
    notes filed in each (anyone's — the folder is what shares them)."""
    roles = readable_team_folder_roles(ctx.user_id, ctx.team_id)
    if not roles:
        return []
    index = load_team_folder_index(ctx.team_id)

    notes_by_folder: dict[int, list[dict[str, Any]]] = {}
    for n in (
        PersonalNoteMaster.objects.filter(
            team=ctx.team_id, folder_id__in=list(roles), parent_note_id__isnull=True
        )
        .values("note_id", "title", "folder_id")
        .order_by("title")[:_MAX_NOTES]
    ):
        notes_by_folder.setdefault(n["folder_id"], []).append(
            {"note_id": n["note_id"], "title": n["title"] or ""}
        )

    out = []
    for folder_id, role in roles.items():
        folder = index.get(folder_id)
        if folder is None:
            continue
        out.append(
            {
                "folder_id": folder_id,
                "name": folder.name,
                "parent_folder_id": folder.parent_folder_id,
                "your_role": _ROLE_LABEL.get(role, "viewer"),
                "can_add_notes": role <= ROLE_EDITOR,
                "notes": notes_by_folder.get(folder_id, []),
            }
        )
    out.sort(key=lambda f: (f["name"] or "").lower())
    return out


LIST_NOTE_FOLDERS = Tool(
    name="list_note_folders",
    description=(
        "Show how the user's notes are organized into sidebar folders. "
        "`folders` are the user's own My-Notes folders (id, name, "
        "parent_folder_id for nesting) WITH the notes filed directly in "
        "each, plus `top_level_notes` (unfiled). `team_folders` are the "
        "Team Notes folders the user can reach, each with `your_role`, "
        "`can_add_notes` and its notes. Use this to answer questions about "
        "note organization ('what's in my Projects folder?') and to resolve "
        "a folder NAME to its numeric folder_id before create_note (a "
        "My-Notes folder for note_type='personal', a team folder with "
        "can_add_notes for note_type='team') or moving a note with "
        "update_note. Task and chat notes have no folders. Folders are NOT "
        "covered by search_knowledge_base — this tool is the only way to "
        "see them."
    ),
    parameters_schema={"type": "OBJECT", "properties": {}},
    run=_run,
    requires_approval=False,
)
