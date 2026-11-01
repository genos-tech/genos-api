"""Note folders as search results (MIP-841).

Typing a folder's name finds the folder: one chunk per `PersonalNoteFolder`
(My Notes, Team Notes and cross-team shared folders), opened at
`/workspace/notes/folder/<id>`.

    entity_type  = "note_folder"
    entity_id    = "note_folder:<folder_id>"
    chunk_type   = "note_folder"
    acl_user_ids = who can read THIS folder: the owner for a My Notes
                   folder; for a Team Notes folder the grants along its
                   chain plus the `team:<id>` sentinel when it resolves
                   public (`folder_ancestor_grant_user_ids`, the rule the
                   notes inside already use).

A folder's parents are its context, on the same terms as a note's
(`note_context`): their names are searched and shown only for people who
can read every one of them. Someone granted only a subfolder finds the
subfolder, not through the name of the folder above it.

Re-emitted when the folder, or any folder above it, changes: a rename or a
move changes the path of everything beneath, and an access change on a
parent changes who can read the children.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterator, Optional

from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.search_engine.chunkers.base import Chunk, EntityChunks, iso
from origin.search_engine.chunkers.note_chunker import _folder_subtree_ids
from origin.search_engine.note_context import (
    FolderAclResolver,
    _chain,
    _clean,
    _load_folder_chains,
)


def iter_note_folder_chunks(since: Optional[datetime] = None) -> Iterator[EntityChunks]:
    qs = PersonalNoteFolder.objects.exclude(team__isnull=True)
    if since is not None:
        touched = PersonalNoteFolder.objects.filter(ts_updated_at__gte=since).values_list(
            "folder_id", flat=True
        )
        qs = qs.filter(folder_id__in=_folder_subtree_ids(touched))
    folders = list(qs)
    if not folders:
        return

    by_id = _load_folder_chains({f.folder_id for f in folders})
    resolver = FolderAclResolver()

    for folder in folders:
        name = (folder.name or "").strip()
        if not name:
            continue
        chain = _chain(folder.folder_id, by_id)
        # A broken chain gives the folder with no path rather than a
        # partial one.
        parents = chain[:-1]
        parent_names = _clean(f.name for f in parents)
        entity_id = f"note_folder:{folder.folder_id}"
        has_context = bool(parents) and len(parent_names) == len(parents)
        yield EntityChunks(
            entity_type="note_folder",
            entity_id=entity_id,
            chunks=[
                Chunk(
                    chunk_id=f"{entity_id}:name",
                    entity_type="note_folder",
                    entity_id=entity_id,
                    chunk_type="note_folder",
                    team_id=str(folder.team_id),
                    acl_user_ids=sorted(resolver.readers_of(folder)),
                    title=name,
                    search_text=name,
                    # Deliberately NOT the path: `snippet_text` is searched
                    # for everyone who can read the folder, so the parents'
                    # names there would defeat `context_acl` (found by
                    # querying a real index: someone granted only the
                    # subfolder matched the parent's name). The row shows
                    # the path from `context_path`.
                    snippet_text="",
                    folder_id=str(folder.folder_id),
                    folder_scope=folder.scope,
                    context_text=" ".join(parent_names) if has_context else None,
                    context_path=parent_names if has_context else None,
                    context_acl=sorted(resolver.readers_of_chain(parents)) if has_context else None,
                    created_at=iso(folder.ts_created_at),
                    updated_at=iso(folder.ts_updated_at),
                )
            ],
        )
