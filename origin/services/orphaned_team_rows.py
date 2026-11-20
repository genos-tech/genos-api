"""Rows a deleted team left behind with `team = NULL` (MIP-1704).

`TeamMaster.delete()` only removes the rows whose team FK is CASCADE. Most team FKs are SET_NULL,
so a row survives its team with `team = NULL`. Until MIP-1702 (api #949) the demo teardown deleted
a hand-written list of tables, and four a visitor can fill but the seed never does were not on it:

    mention groups, note folder tags, invites (an invited e-mail address), custom emojis (an image)

so every demo teardown before that fix left them behind. #949 stops NEW orphans; this finds and
removes the ones already there.

"team IS NULL" is NOT enough to call a row an orphan, which is why this is an allow-list and not
a sweep over every team FK:

  * mention groups, note folder tags and invites are always made INSIDE a team (every creation path
    sets `team`), so NULL can only mean the team is gone;
  * custom emojis are different: the curated GLOBAL DEFAULTS are stored with `team = NULL` on
    purpose (`seed_default_emoji`). The creator cannot tell them apart either: deleting the demo
    user nulls `created_by`, and defaults have no creator. What does tell them apart is the image
    path the model itself builds (`team_emoji/<scope>/...`): `<scope>` is the team's uuid for a team
    emoji and the word `global` for a default. An orphan keeps its old team's uuid in its path.
    A NULL-team emoji whose path is anything else is left alone.

My Notes notes, their folders and the "recently opened" list (MIP-1713) are a different case, and
"team IS NULL" is NOT enough for them either:

  * a personal note with NO team is LEGITIMATE: `start_here_note.create_for` writes every new
    account's "Start here" note with `team = NULL` and a live owner, until `adopt_for_team` gives
    it the person's first team. A note like that is never an orphan;
  * what makes a note unreachable is that its team AND its owner are both gone. The owner FK is
    SET_NULL too, so a note whose team and owner were both hard-deleted holds its title and body
    for nobody. Even then it is only an orphan when nobody else can open it either: a live
    `NotePermissionMaster` grant or a filing in a Team Notes folder (`get_effective_role`) still
    reaches a note whose owner is gone, so those are KEPT and counted (`notes_kept_reachable`);
  * a My Notes folder is an orphan when team and owner are both NULL and the folder is `personal`
    scope; a folder that still holds a note or a folder we keep is kept (`folders_kept_in_use`);
  * a recent is an orphan when team and user are both NULL (`recent_note_views` lists by both).

Notes go through the project's own delete path (`batched_note_cleanup` + `forget_deleted_notes` +
`purge_notes_from_index`), never a bare `.delete()`: permissions, versions and summaries have no
foreign key, and neither do the search chunks.

Every OTHER table whose team FK can be NULL is only counted, never touched (`survey`): a NULL there
may be legitimate, and a person has to read it first. For the notes tables the survey lists the
rows the rule above kept (so the "Start here" notes show up there, on purpose).
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field

from django.core.files.storage import default_storage
from django.db import models, transaction
from django.db.models import Exists, OuterRef

from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
    NotePermissionMaster,
)
from origin.models.note.personal_note_models import (
    PersonalNoteAttachmentFact,
    PersonalNoteFolder,
    PersonalNoteMaster,
)
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.search_engine.purge import purge_note_folders
from origin.services.note_delete import forget_deleted_notes
from origin.services.note_satellites import batched_note_cleanup
from origin.views.utils.note_acl_resync import purge_notes_from_index
from origin.views.utils.note_role import NOTE_TYPE_PERSONAL

logger = logging.getLogger(__name__)

# `team_emoji/<team uuid>/<uuid>-<name>.<ext>`, as `_team_emoji_path` builds it. A default's path
# has the literal `global` where the team uuid is, so it can never match.
_UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
TEAM_EMOJI_PATH_REGEX = rf"^team_emoji/{_UUID}/"


def _orphan_groups():
    return MentionGroupMaster._base_manager.filter(team__isnull=True)


def _orphan_tags():
    return NoteFolderTag._base_manager.filter(team__isnull=True)


def _orphan_invites():
    return TeamInvite._base_manager.filter(team__isnull=True)


def _orphan_emojis():
    return TeamEmojiMaster._base_manager.filter(
        team__isnull=True, image__regex=TEAM_EMOJI_PATH_REGEX
    )


# --- My Notes notes, their folders, the recents (MIP-1713) ------------------------------------


def _null_team_ownerless_notes():
    return PersonalNoteMaster._base_manager.filter(team__isnull=True, owner__isnull=True)


def _orphan_notes():
    """Notes with no team, no owner, and nobody else who can open them.

    `get_effective_role` reaches a personal note by (1) its owner, (2) an explicit
    `NotePermissionMaster` grant to a live user, (3) a Team Notes folder it is filed in. (1) is
    gone by the first two conditions; (2) and (3) are checked here, so a note somebody can still
    open is not an orphan however its team and owner look.
    """
    granted = NotePermissionMaster._base_manager.filter(
        note_type=NOTE_TYPE_PERSONAL, note_id=OuterRef("note_id"), user__isnull=False
    )
    in_team_folder = PersonalNoteFolder._base_manager.filter(
        folder_id=OuterRef("folder_id"), scope=PersonalNoteFolder.SCOPE_TEAM
    )
    return _null_team_ownerless_notes().exclude(Exists(granted)).exclude(Exists(in_team_folder))


def _orphan_folders(orphan_note_ids: set[int]) -> tuple[set[int], int]:
    """`(folder ids to delete, folders kept because something still uses them)`.

    A candidate is a `personal`-scope folder with no team and no owner. It is KEPT while a note we
    are not deleting is filed in it, a folder we are not deleting sits under it, or somebody holds
    a grant on it; that keeps a surviving note from pointing at a folder that is gone.
    """
    candidates = dict(
        PersonalNoteFolder._base_manager.filter(
            team__isnull=True, owner__isnull=True, scope=PersonalNoteFolder.SCOPE_PERSONAL
        ).values_list("folder_id", "parent_folder_id")
    )
    if not candidates:
        return set(), 0
    ids = list(candidates)
    blocked = set(
        PersonalNoteMaster._base_manager.filter(folder_id__in=ids)
        .exclude(note_id__in=orphan_note_ids)
        .values_list("folder_id", flat=True)
    )
    blocked |= set(
        PersonalNoteFolder._base_manager.filter(parent_folder_id__in=ids)
        .exclude(folder_id__in=ids)
        .values_list("parent_folder_id", flat=True)
    )
    blocked |= set(
        NoteFolderPermission._base_manager.filter(folder_id__in=ids).values_list(
            "folder_id", flat=True
        )
    )
    # A kept folder keeps the whole chain above it.
    frontier = set(blocked)
    while frontier:
        parents = {candidates[f] for f in frontier if candidates.get(f) in candidates}
        frontier = parents - blocked
        blocked |= parents
    return set(ids) - blocked, len(blocked)


def _orphan_recents():
    return NoteRecentMaster._base_manager.filter(team__isnull=True, user__isnull=True)


@dataclass
class OrphanReport:
    """What was found (dry run) or removed (apply), per table, and what was only counted."""

    mention_groups: int = 0
    note_folder_tags: int = 0
    invites: int = 0
    custom_emojis: int = 0
    emoji_files_removed: int = 0
    # Emojis whose image could not be deleted: the row is KEPT so the next run retries it.
    emoji_file_errors: int = 0
    # MIP-1713: My Notes. `notes_kept_reachable` = no team and no owner, but somebody can still
    # open it (a live grant, or a Team Notes folder): KEPT. `folders_kept_in_use`: an orphan
    # folder that still holds, or sits above, something we keep. `note_attachments` = the
    # uploaded files of the deleted notes (rows plus files).
    personal_notes: int = 0
    notes_kept_reachable: int = 0
    note_attachments: int = 0
    note_attachment_file_errors: int = 0
    personal_folders: int = 0
    folders_kept_in_use: int = 0
    recent_notes: int = 0
    # `table label -> rows with team NULL`, for every other table that allows it. Never deleted.
    survey: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return (
            self.mention_groups
            + self.note_folder_tags
            + self.invites
            + self.custom_emojis
            + self.personal_notes
            + self.personal_folders
            + self.recent_notes
        )


def survey_other_null_team_tables(
    deletable: dict[type[models.Model], int] | None = None,
) -> dict[str, int]:
    """Count `team IS NULL` rows in every other table whose team FK can be NULL. Read-only.

    Same discovery as the demo teardown's sweep (`TeamMaster._meta.related_objects`, skipping
    CASCADE). Tables this module purges are left out; a table with no such rows is left out too.
    A table purged only BY A RULE (the notes, their folders, the recents) is listed with the rows
    the rule keeps: its `team IS NULL` count minus `deletable[model]`, the rows about to go.
    """
    purged = {MentionGroupMaster, NoteFolderTag, TeamInvite, TeamEmojiMaster}
    deletable = deletable or {}
    found: dict[str, int] = {}
    for rel in TeamMaster._meta.related_objects:
        if getattr(rel, "on_delete", None) in (None, models.CASCADE):
            continue
        model = rel.related_model
        if model in purged or not rel.field.null:
            continue
        count = model._base_manager.filter(**{f"{rel.field.name}__isnull": True}).count()
        count -= deletable.get(model, 0)
        if count:
            found[f"{model._meta.label}.{rel.field.name}"] = count
    return dict(sorted(found.items()))


def _delete_notes(note_ids: set[int], folder_ids: set[int]) -> tuple[list[str], int]:
    """Hard-delete these personal notes and folders the way the product does (`note_delete`).

    Returns the names of the notes' uploaded files, to delete after the commit, and how many
    attachment rows went. Run inside the
    caller's transaction. The note rows go inside `batched_note_cleanup()` so their permission,
    version and summary rows (keyed by number, no FK) go in one pass, with them (the `post_delete`
    receiver, `note_satellite_signals`); the other
    people's recents and favorites and the mention rows go through `forget_deleted_notes`.
    """
    # The attachment rows point at the note with a SET_NULL FK: once the note is gone nothing
    # finds them again, so they are taken first.
    attachments = PersonalNoteAttachmentFact._base_manager.filter(note_id__in=note_ids)
    files = [a.note_attachment_url.name for a in attachments if a.note_attachment_url.name]
    removed = attachments.delete()[0]
    with batched_note_cleanup():
        PersonalNoteMaster._base_manager.filter(note_id__in=note_ids).delete()
    forget_deleted_notes(NOTE_TYPE_PERSONAL, note_ids)
    # NoteFolderPermission and NoteFolderTagLink cascade on the folder FK.
    PersonalNoteFolder._base_manager.filter(folder_id__in=folder_ids).delete()
    return files, removed


def purge_orphaned_team_rows(*, apply: bool) -> OrphanReport:
    """Find (and, with `apply`, delete) the rows a deleted team left behind.

    Dry run by default: nothing is written, the counts are what `apply` would remove. Safe to run
    twice; the second run reports 0.

    An emoji's image file goes BEFORE its row, so a storage failure leaves the row for the next
    run instead of an unreachable file nobody points at.
    """
    report = OrphanReport()

    report.mention_groups = _orphan_groups().count()
    report.note_folder_tags = _orphan_tags().count()
    report.invites = _orphan_invites().count()
    report.custom_emojis = _orphan_emojis().count()
    note_ids = set(_orphan_notes().values_list("note_id", flat=True))
    folder_ids, report.folders_kept_in_use = _orphan_folders(note_ids)
    report.personal_notes = len(note_ids)
    report.notes_kept_reachable = _null_team_ownerless_notes().count() - len(note_ids)
    report.personal_folders = len(folder_ids)
    report.recent_notes = _orphan_recents().count()
    report.note_attachments = PersonalNoteAttachmentFact._base_manager.filter(
        note_id__in=note_ids
    ).count()
    report.survey = survey_other_null_team_tables(
        {
            PersonalNoteMaster: report.personal_notes,
            PersonalNoteFolder: report.personal_folders,
            NoteRecentMaster: report.recent_notes,
        }
    )
    if not apply:
        return report

    attachment_files: list[str] = []
    # Members / tag links go with their group / tag (CASCADE). The notes are in this transaction
    # too: either all of it goes or none of it does.
    with transaction.atomic():
        _orphan_groups().delete()
        _orphan_tags().delete()
        _orphan_invites().delete()
        # Recomputed inside the transaction: what is deleted is what is found NOW.
        note_ids = set(_orphan_notes().values_list("note_id", flat=True))
        folder_ids, _ = _orphan_folders(note_ids)
        attachment_files, report.note_attachments = _delete_notes(note_ids, folder_ids)
        report.personal_notes = len(note_ids)
        report.personal_folders = len(folder_ids)
        report.recent_notes = _orphan_recents().delete()[0]
    # After the commit: a file delete cannot be rolled back, and the index purge is best-effort
    # (the reindex cron's orphan sweep retries it).
    purge_notes_from_index(note_ids)
    purge_note_folders(folder_ids)
    for name in attachment_files:
        try:
            default_storage.delete(name)
        except Exception:  # noqa: BLE001 - storage errors vary; the row is gone, so say so
            # The directory only (`notes/personal/<note id>/`): a file name is the uploader's text.
            logger.exception(
                "could not delete an attachment file of an orphaned note: %s",
                posixpath.dirname(name),
            )
            report.note_attachment_file_errors += 1

    removed = 0
    # The ids once, up front: there are few, and a row whose file failed must not be fetched again.
    for emoji_id in list(_orphan_emojis().order_by("emoji_id").values_list("emoji_id", flat=True)):
        emoji = _orphan_emojis().filter(emoji_id=emoji_id).first()
        if emoji is None:  # gone since the ids were read
            continue
        try:
            if emoji.image.name:
                emoji.image.delete(save=False)
                report.emoji_files_removed += 1
        except Exception:  # noqa: BLE001 — storage errors vary; keep the row, retry next run
            logger.exception("could not delete the image of orphaned emoji %s", emoji_id)
            report.emoji_file_errors += 1
            continue
        emoji.delete()
        removed += 1
    report.custom_emojis = removed
    return report
