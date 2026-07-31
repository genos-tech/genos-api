"""Keep OpenSearch honest when a team folder's ACL narrows.

Search visibility is MATERIALIZED into each chunk's `acl_user_ids` at
index time, and the incremental reindexer only revisits notes whose
window matched (`chunkers/note_chunker.py`). So a permission change is
invisible to search until the next pass — up to ~10 minutes.

For BROADENING changes (granting access, private → public) that lag is
harmless: the new reader simply can't find the note yet, and the widened
reindex window picks it up.

For NARROWING changes (revoking, public → private, pulling a note out of
the team space) the same lag is a security-shaped bug: someone who just
lost access can still find the content in Spotlight. So narrowing purges
the affected notes' chunks SYNCHRONOUSLY. They vanish from search at
once and the reindexer restores them, with correct ACL, on its next
pass — failing closed rather than open.

Purging is best-effort: a note's permissions must change even when the
search cluster is unreachable, so callers are never blocked by it.
"""

import logging

from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster

logger = logging.getLogger(__name__)

NOTE_LABEL_PERSONAL = "personal"


def collect_folder_subtree_ids(folder_id, team_id):
    """Folder ids of `folder_id` and every team-scoped descendant.

    Walks by TEAM only — never by owner. A team folder's subtree crosses
    owners by design, so an owner-filtered walk would silently stop at
    the first folder someone else created.
    """
    folder_ids = {folder_id}
    frontier = [folder_id]
    while frontier:
        child_ids = list(
            PersonalNoteFolder.objects.filter(
                team=team_id,
                scope=PersonalNoteFolder.SCOPE_TEAM,
                parent_folder_id__in=frontier,
            )
            .exclude(folder_id__in=folder_ids)
            .values_list("folder_id", flat=True)
        )
        if not child_ids:
            break
        folder_ids.update(child_ids)
        frontier = child_ids
    return folder_ids


def collect_note_ids_in_folders(folder_ids, team_id):
    """Root notes filed anywhere in `folder_ids`, plus their child-note
    subtrees (children carry folder_id NULL and hang off parent_note_id).
    Not owner-filtered — team folders hold many people's notes."""
    note_ids = set(
        PersonalNoteMaster.objects.filter(team=team_id, folder_id__in=folder_ids).values_list(
            "note_id", flat=True
        )
    )
    frontier = list(note_ids)
    while frontier:
        child_notes = list(
            PersonalNoteMaster.objects.filter(team=team_id, parent_note_id__in=frontier)
            .exclude(note_id__in=note_ids)
            .values_list("note_id", flat=True)
        )
        if not child_notes:
            break
        note_ids.update(child_notes)
        frontier = child_notes
    return note_ids


def collect_note_subtree_ids(note_id, team_id):
    """One note plus its `parent_note_id` descendant chain — the unit
    that moves together, since child notes follow their root."""
    note_ids = {note_id}
    frontier = [note_id]
    while frontier:
        child_notes = list(
            PersonalNoteMaster.objects.filter(team=team_id, parent_note_id__in=frontier)
            .exclude(note_id__in=note_ids)
            .values_list("note_id", flat=True)
        )
        if not child_notes:
            break
        note_ids.update(child_notes)
        frontier = child_notes
    return note_ids


def purge_notes_from_index(note_ids):
    """Drop these personal notes' chunks from OpenSearch. Best-effort."""
    if not note_ids:
        return
    try:
        from origin.search_engine.purge import purge_note  # noqa: PLC0415

        for note_id in note_ids:
            purge_note(NOTE_LABEL_PERSONAL, note_id)
    except Exception:
        # Never let a search-cluster problem fail the permission write —
        # the ACL change itself has already been committed, and the
        # reindexer reconciles the index either way.
        logger.exception("Failed to purge note chunks after an ACL change")


def resync_folder_subtree(folder_id, team_id):
    """Purge every note under a team folder whose access just narrowed."""
    folder_ids = collect_folder_subtree_ids(folder_id, team_id)
    purge_notes_from_index(collect_note_ids_in_folders(folder_ids, team_id))
