import os

from django.db import models

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser

# How many parent hops a note tree is followed (matches `search_engine.note_context._MAX_DEPTH`).
MAX_NOTE_DEPTH = 12


class PersonalNoteFolder(models.Model):
    """User-created folder for organizing notes in the sidebar.

    `parent_folder_id` nests folders arbitrarily deep;
    `PersonalNoteMaster.folder_id` attaches a ROOT-level note (its
    `parent_note_id` child subtree rides along implicitly). Both are
    plain BigIntegerFields per the repo's tree convention
    (`parent_note_id` / `parent_task_id`).

    ONE table serves TWO sidebar spaces, told apart by `scope`:

    * ``scope="personal"`` — "My Notes". A pure organization layer:
      owner-scoped on every handler, no permission rows, never shared.
      Deletion is a DESTRUCTIVE recursive delete of the whole subtree
      (see `personal_note_folder_views.py`).
    * ``scope="team"`` — "Team Notes", the shared general space. The
      folder is the ACL CARRIER: access flows from `visibility` plus
      `NoteFolderPermission` rows down to every note filed inside, and
      `owner` means *creator*, not sole reader. Served by
      `team_note_folder_views.py`, which refuses to delete a subtree
      holding anyone else's content.

    `scope` is therefore load-bearing, not cosmetic: EVERY query on this
    table must filter it, or team folders leak into My Notes.
    """

    SCOPE_PERSONAL = "personal"
    SCOPE_TEAM = "team"
    # The hidden per-owner folder a Genos Skill's body note is filed in (MIP-2040). Never listed
    # in My Notes or Team Notes; the `Skill` row, not the folder, decides who may read the skill.
    # For the search index the note is its owner's alone: `team_folder_acl_user_ids` answers an
    # empty set for any scope but "team", so the ACL stamped on its chunks is the owner plus
    # explicit shares and never the team (pinned by `test_skills_api`).
    SCOPE_SKILL = "skill"

    VISIBILITY_PUBLIC = "public"
    VISIBILITY_PRIVATE = "private"

    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    folder_id = models.BigAutoField(primary_key=True, unique=True)
    parent_folder_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    name = models.CharField(max_length=255)
    # Which sidebar space this folder belongs to — see the class
    # docstring. Indexed because every list query filters on it.
    scope = models.CharField(max_length=16, default=SCOPE_PERSONAL, db_index=True)
    # Team folders only. "public" = every team member gets Editor;
    # "private" = only NoteFolderPermission grantees.
    #
    # NULL means INHERIT: resolution walks up to the nearest ancestor
    # that has an opinion. That is what makes a subfolder "accessible to
    # everyone who can reach the parent" the zero-config default — an
    # inheriting subfolder simply has no access definition of its own.
    # Always NULL for personal folders.
    visibility = models.CharField(max_length=16, blank=True, null=True)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def skill_folder_ids(cls):
        """The ids of every hidden Genos-Skills folder, as a subquery (MIP-2121).

        The ONE definition of "this note is a skill's body": a note filed in one of these folders.
        Every surface that lists or indexes notes excludes `folder_id__in=` this, so the skill text
        shows up only in the Skills editor.
        """
        return cls.objects.filter(scope=cls.SCOPE_SKILL).values("folder_id")


class PersonalNoteMaster(models.Model):
    team = models.ForeignKey(
        TeamMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="team_id",
    )
    owner = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    note_id = models.BigAutoField(primary_key=True, unique=True)
    parent_note_id = models.BigIntegerField(blank=True, null=True)
    # Sidebar folder membership (PersonalNoteFolder.folder_id). Only
    # meaningful on root notes (parent_note_id IS NULL) — child notes
    # follow their root. Null = top level of "My Notes".
    folder_id = models.BigIntegerField(blank=True, null=True, db_index=True)
    title = models.CharField(max_length=255)
    body = models.JSONField(blank=True, null=True)
    # "blocknote" (the editor's JSON, always, before MIP-2214) or "html" (a saved HTML page, whose
    # text is in `NoteHtmlRevision`; `body` then only holds the readable placeholder that search,
    # exports and history work on). Set by `origin.services.html_notes`, never by a client.
    body_format = models.CharField(max_length=16, default="blocknote", db_default="blocknote")
    # Snapshot of every user mentioned in `body` after the latest save.
    # The PUT view computes a delta against this column and emits one
    # toast per newly-mentioned user; the column itself is what
    # `loadActivityHistory` filters against so prior recipients keep
    # their feed entry on next reload. Mirrors `TaskMaster.mentioned_user_ids`.
    mentioned_user_ids = models.JSONField(blank=True, default=list)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def skill_body_ids(cls, note_ids) -> set[int]:
        """The ids among `note_ids` that are a Genos Skill's body note, or a sub-note anywhere
        under one (MIP-2121, MIP-2191).

        A skill's body is a note filed in a hidden skill folder; a child note carries no folder
        of its own (`folder_id` NULL, `parent_note_id` set) and follows its root, so each id is
        walked up its parent chain to the root. One query for the folders, then one per level of
        depth (a flat set of ordinary notes costs two).
        """
        ids = {int(i) for i in note_ids}
        if not ids:
            return set()
        skill_folders = set(
            PersonalNoteFolder.skill_folder_ids().values_list("folder_id", flat=True)
        )
        if not skill_folders:
            return set()
        found: set[int] = set()
        # origin id -> the note whose folder/parent decides it (starts as the note itself)
        current = {i: i for i in ids}
        for _ in range(MAX_NOTE_DEPTH):
            rows = {
                note_id: (folder_id, parent_id)
                for note_id, folder_id, parent_id in cls.objects.filter(
                    note_id__in=set(current.values())
                ).values_list("note_id", "folder_id", "parent_note_id")
            }
            nxt: dict[int, int] = {}
            for origin, at in current.items():
                folder_id, parent_id = rows.get(at, (None, None))
                if folder_id is not None:
                    if folder_id in skill_folders:
                        found.add(origin)
                elif parent_id is not None:
                    nxt[origin] = parent_id
            if not nxt:
                break
            current = nxt
        return found

    @classmethod
    def skill_subtree_ids(cls, owner_id=None) -> set[int]:
        """Every note that is a skill body or sits under one (MIP-2191): the body notes in the
        hidden skill folders (of `owner_id` when given), then their descendants, one query per
        level. Used to leave them out of a list; empty (and one query) when nobody has a skill."""
        roots = cls.objects.filter(folder_id__in=PersonalNoteFolder.skill_folder_ids())
        if owner_id is not None:
            roots = roots.filter(owner_id=owner_id)
        found = set(roots.values_list("note_id", flat=True))
        frontier = set(found)
        for _ in range(MAX_NOTE_DEPTH):
            if not frontier:
                break
            frontier = (
                set(
                    cls.objects.filter(parent_note_id__in=frontier).values_list(
                        "note_id", flat=True
                    )
                )
                - found
            )
            found |= frontier
        return found


def personal_note_attachment_path(instance, filename):
    return os.path.join(
        "notes",
        "personal",
        str(instance.note_id),
        filename,
    )


class PersonalNoteAttachmentFact(models.Model):
    note = models.ForeignKey(
        PersonalNoteMaster,
        on_delete=models.SET_NULL,
        null=True,
        to_field="note_id",
    )
    uploader = models.ForeignKey(
        CustomUser,
        on_delete=models.SET_NULL,
        null=True,
        to_field="id",
    )
    attachment_id = models.BigAutoField(primary_key=True, unique=True)
    note_attachment_url = models.FileField(upload_to=personal_note_attachment_path)
    # Bytes of the stored file, set at upload (MIP-1962); 0 until the backfill reads old rows.
    size_bytes = models.BigIntegerField(default=0, db_default=0, editable=False)
    ts_created_at = models.DateTimeField(auto_now_add=True)
    ts_updated_at = models.DateTimeField(auto_now=True)
