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

Every OTHER table whose team FK can be NULL is only counted, never touched (`survey`): a NULL there
may be legitimate, and a person has to read it first.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from django.db import models, transaction

from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster
from origin.models.common.team_emoji_models import TeamEmojiMaster
from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import NoteFolderTag

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
    # `table label -> rows with team NULL`, for every other table that allows it. Never deleted.
    survey: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.mention_groups + self.note_folder_tags + self.invites + self.custom_emojis


def survey_other_null_team_tables() -> dict[str, int]:
    """Count `team IS NULL` rows in every other table whose team FK can be NULL. Read-only.

    Same discovery as the demo teardown's sweep (`TeamMaster._meta.related_objects`, skipping
    CASCADE). Tables this module purges are left out; a table with no such rows is left out too.
    """
    purged = {MentionGroupMaster, NoteFolderTag, TeamInvite, TeamEmojiMaster}
    found: dict[str, int] = {}
    for rel in TeamMaster._meta.related_objects:
        if getattr(rel, "on_delete", None) in (None, models.CASCADE):
            continue
        model = rel.related_model
        if model in purged or not rel.field.null:
            continue
        count = model._base_manager.filter(**{f"{rel.field.name}__isnull": True}).count()
        if count:
            found[f"{model._meta.label}.{rel.field.name}"] = count
    return dict(sorted(found.items()))


def purge_orphaned_team_rows(*, apply: bool) -> OrphanReport:
    """Find (and, with `apply`, delete) the rows a deleted team left behind.

    Dry run by default: nothing is written, the counts are what `apply` would remove. Safe to run
    twice; the second run reports 0.

    An emoji's image file goes BEFORE its row, so a storage failure leaves the row for the next
    run instead of an unreachable file nobody points at.
    """
    report = OrphanReport(survey=survey_other_null_team_tables())

    report.mention_groups = _orphan_groups().count()
    report.note_folder_tags = _orphan_tags().count()
    report.invites = _orphan_invites().count()
    report.custom_emojis = _orphan_emojis().count()
    if not apply:
        return report

    # Members / tag links go with their group / tag (CASCADE).
    with transaction.atomic():
        _orphan_groups().delete()
        _orphan_tags().delete()
        _orphan_invites().delete()

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
