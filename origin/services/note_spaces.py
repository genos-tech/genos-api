"""Which of a person's notes are theirs alone, and which belong to a team (MIP-1782).

`PersonalNoteMaster` and `PersonalNoteFolder` are ONE table each for two spaces: My Notes
(`scope="personal"`, private to the owner) and Team Notes (`scope="team"`, shared; the folder is
the ACL carrier and `owner` means creator, see `personal_note_models.py`). Account deletion and
the data export both have to tell the two apart, and used to do it differently: erasure deleted
everything the leaver owned in both spaces (destroying what teammates read), the export filtered
only the folder NAMES by scope.

One rule, here, for both:

* A note is a **team note** when its root (the note itself, or the ancestor at the top of its
  `parent_note_id` chain: a child has no folder of its own) is filed in a team folder.
  `collect_note_ids_in_folders` is that rule, the one the search resync uses.
* Everything else is **personal**: filed in a My Notes folder, or in none.

`plan_erasure` decides what the erasure of one person does with their rows, per team:

* in a team that closes with them (nobody else is left), nothing is kept: the whole team goes
  dark and the notes are only theirs, as before;
* in a team that goes on, a team folder is KEPT when anybody else can still read it (a live
  account that the folder chain reaches, or an external share) or when it holds somebody else's
  note or folder. A team folder only the leaver could reach, with only their own content in it,
  is their private data and is deleted with them. A kept folder keeps its ancestors, or the
  chain every teammate's note resolves its access through would break;
* a note is kept when it is a team note that is not inside a folder being deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from origin.models.common.team_models import TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.views.utils.note_acl_resync import (
    collect_folder_subtree_ids,
    collect_note_ids_in_folders,
)
from origin.views.utils.note_folder_role import (
    get_folder_role,
    load_team_folder_index,
    team_folder_reader_user_ids,
)

SPACE_PERSONAL = "personal"
SPACE_TEAM = "team"


def team_space_note_ids(team_ids) -> set[int]:
    """Every note of these teams that lives in the Team Notes space: filed in a team folder, or a
    descendant (`parent_note_id`) of a note that is. Children are included: they carry no folder.
    """
    out: set[int] = set()
    for team_id in {t for t in team_ids if t is not None}:
        folder_ids = list(
            PersonalNoteFolder.objects.filter(
                team=team_id, scope=PersonalNoteFolder.SCOPE_TEAM
            ).values_list("folder_id", flat=True)
        )
        if folder_ids:
            out |= collect_note_ids_in_folders(folder_ids, team_id)
    return out


@dataclass
class ErasurePlan:
    """What erasing one person does with the notes and folders they own."""

    delete_note_ids: set[int] = field(default_factory=set)
    keep_note_ids: set[int] = field(default_factory=set)
    delete_folder_ids: set[int] = field(default_factory=set)
    # Team folders the person created that stay, and so need a live owner.
    handover_folder_ids: list[int] = field(default_factory=list)


def _live_account_ids(user_ids) -> set[str]:
    ids = {str(u) for u in user_ids if u}
    if not ids:
        return set()
    return {
        str(pk)
        for pk in CustomUser.objects.filter(
            id__in=ids, is_active=True, is_deleted=False
        ).values_list("id", flat=True)
    }


def _someone_else_needs(folder_id, team_id, index, subtree_folders, subtree_notes, user_id) -> bool:
    """True when the team folder still matters to anybody but `user_id`."""
    from origin.services.external_grants import externally_shared  # noqa: PLC0415

    readers = team_folder_reader_user_ids(folder_id) - {str(user_id)}
    if _live_account_ids(readers):
        return True
    # The team owner may carry no membership row, which a public folder's reader set is built
    # from (`is_team_member` checks the owner FK for the same reason).
    owner = team_owner_id(team_id)
    if (
        owner is not None
        and str(owner) != str(user_id)
        and _live_account_ids([owner])
        and get_folder_role(owner, folder_id, team_id) is not None
    ):
        return True
    if externally_shared("note_folder", folder_id):
        return True
    # Somebody else's folder or note under it: not the leaver's to take away, and it would be
    # left hanging off a deleted parent.
    if any(
        index[f].owner_id is not None and str(index[f].owner_id) != str(user_id)
        for f in subtree_folders
        if f in index
    ):
        return True
    return (
        PersonalNoteMaster.objects.filter(note_id__in=subtree_notes).exclude(owner=user_id).exists()
    )


def plan_erasure(user, closing_team_ids=()) -> ErasurePlan:
    """Sort the notes and folders `user` owns into kept and deleted. Read-only.

    `closing_team_ids` are the teams that die with the person (nobody else on them): nothing is
    kept in them.
    """
    closing = {str(t) for t in closing_team_ids}
    notes = list(PersonalNoteMaster.objects.filter(owner=user).values_list("note_id", "team_id"))
    folders = list(
        PersonalNoteFolder.objects.filter(owner=user).values_list(
            "folder_id", "team_id", "scope", "parent_folder_id"
        )
    )
    plan = ErasurePlan()
    all_team_ids = {t for t in {*(n[1] for n in notes), *(f[1] for f in folders)} if t is not None}
    live_team_ids = {t for t in all_team_ids if str(t) not in closing}

    team_notes = team_space_note_ids(live_team_ids)
    keep_notes = {n for n, team in notes if team in live_team_ids and n in team_notes}

    delete_folders = {
        f
        for f, team, scope, _parent in folders
        if scope != PersonalNoteFolder.SCOPE_TEAM or team is None or team not in live_team_ids
    }

    # Team folders in a team that goes on: kept when anybody else needs them.
    kept_folders: set[int] = set()
    for team_id in live_team_ids:
        mine = [
            f
            for f, team, scope, _parent in folders
            if team == team_id and scope == PersonalNoteFolder.SCOPE_TEAM
        ]
        if not mine:
            continue
        index = load_team_folder_index(team_id)
        for folder_id in mine:
            subtree = collect_folder_subtree_ids(folder_id, team_id)
            subtree_notes = collect_note_ids_in_folders(subtree, team_id)
            if _someone_else_needs(folder_id, team_id, index, subtree, subtree_notes, user.pk):
                kept_folders.add(folder_id)
        # A kept folder keeps the leaver's folders above it.
        for folder_id in list(kept_folders):
            parent = index[folder_id].parent_folder_id if folder_id in index else None
            seen = set()
            while parent is not None and parent in index and parent not in seen:
                seen.add(parent)
                if str(index[parent].owner_id) == str(user.pk):
                    kept_folders.add(parent)
                parent = index[parent].parent_folder_id
        dropped = {f for f in mine if f not in kept_folders}
        delete_folders |= dropped
        if dropped:
            # No kept note under a folder that is going.
            under = collect_note_ids_in_folders(
                set().union(*(collect_folder_subtree_ids(f, team_id) for f in dropped)), team_id
            )
            keep_notes -= under

    plan.keep_note_ids = keep_notes
    plan.delete_note_ids = {n for n, _team in notes} - keep_notes
    plan.delete_folder_ids = delete_folders
    plan.handover_folder_ids = sorted(kept_folders)
    return plan


def team_owner_id(team_id):
    """The team's owner, if the team is still open."""
    return (
        TeamMaster.objects.filter(team_id=team_id, is_deleted=False)
        .values_list("owner_id", flat=True)
        .first()
    )


def heir_of_folder(folder: PersonalNoteFolder, user_id):
    """Who takes over a kept team folder the leaver created, as `(user id, was_a_co_owner)`.

    1. A live co-owner already on the folder (an Owner grant): nobody's access widens.
    2. Otherwise the team's live owner, the way `deleted_account_channel_owners` hands a project
       chat to the team owner (Ken's decision there): a team owner may reach a PRIVATE folder
       they were not on. Recorded in the audit trail.
    None when neither exists: the folder is left, and an editor on it can add an owner.
    """
    from origin.models.note.common_note_models import NoteFolderPermission  # noqa: PLC0415
    from origin.views.utils.note_folder_role import is_team_member  # noqa: PLC0415
    from origin.views.utils.note_role import ROLE_OWNER  # noqa: PLC0415

    co_owners = list(
        NoteFolderPermission.objects.filter(folder=folder, role_id=ROLE_OWNER)
        .exclude(user_id=user_id)
        .order_by("ts_created_at", "id")
        .values_list("user_id", flat=True)
    )
    live = _live_account_ids(co_owners)
    for candidate in co_owners:
        if str(candidate) in live and is_team_member(folder.team_id, candidate):
            return candidate, True
    owner = team_owner_id(folder.team_id)
    if owner is not None and str(owner) != str(user_id) and _live_account_ids([owner]):
        return owner, False
    return None, False


def hand_over_folder(folder_id, user_id) -> str | None:
    """Give a kept team folder a live owner. Returns the new owner's id, or None (left as is)."""
    from django.utils import timezone  # noqa: PLC0415

    from origin.models.common.security_event_models import SecurityAction  # noqa: PLC0415
    from origin.models.note.common_note_models import NoteFolderPermission  # noqa: PLC0415
    from origin.services.security_events import record_security_event  # noqa: PLC0415
    from origin.views.utils.note_role import ROLE_OWNER  # noqa: PLC0415

    folder = PersonalNoteFolder.objects.filter(folder_id=folder_id).first()
    if folder is None:
        return None
    heir, was_co_owner = heir_of_folder(folder, user_id)
    if heir is None:
        return None
    # `ts_updated_at` is bumped by hand (a queryset update skips `auto_now`): it is what makes the
    # search reindex revisit the folder and the notes in it with the new owner on their ACL.
    PersonalNoteFolder.objects.filter(folder_id=folder.folder_id).update(
        owner=heir, ts_updated_at=timezone.now()
    )
    NoteFolderPermission.objects.update_or_create(
        folder=folder, user_id=heir, defaults={"role_id": ROLE_OWNER, "team_id": folder.team_id}
    )
    record_security_event(
        SecurityAction.OWNER_TRANSFERRED,
        actor=None,
        actor_label="system",
        team_id=folder.team_id,
        target_type="note_folder",
        target_id=folder.folder_id,
        target_label=folder.name,
        metadata={
            "from_user_id": str(user_id),
            "to_user_id": str(heir),
            "via": "account_deleted",
            "co_owner": was_co_owner,
        },
    )
    return str(heir)
