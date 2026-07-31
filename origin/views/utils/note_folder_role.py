"""Role resolution for TEAM folders (`PersonalNoteFolder.scope="team"`).

Team Notes is the shared "general" space, and the FOLDER is the ACL
carrier: a note filed in a team folder derives its role from that
folder rather than from a per-note grant. `get_effective_role` in
`note_role.py` delegates its personal-note branch here — which is what
makes team notes work everywhere at once (REST reads/writes, and the
Hocuspocus collab gate via `NoteRoleCheckView`) with no per-surface code.

Inheritance model
-----------------
A folder either INHERITS or OVERRIDES, and `visibility` is the switch:

* ``visibility IS NULL`` — inherits. Resolution continues to the parent.
  Permission rows on an inheriting folder are ADDITIVE: they let you
  widen access to a subfolder without cutting off anyone who reaches it
  from above.
* ``visibility IS NOT NULL`` — overrides, and resolution STOPS here.
  ``"public"`` grants Editor to every team member; ``"private"`` grants
  only the folder's own permission rows.

So the zero-config subfolder ("accessible to everyone who can access the
parent") is simply a folder with no opinion of its own, and narrowing a
subfolder is the deliberate act of setting its visibility. Encoding
"inherit" as NULL rather than as an absence of rows is what keeps those
two intents distinguishable — a folder that merely ADDS a member must
not thereby evict everyone who had access through the parent.

Everything here resolves from a flat in-memory index of the team's
folders, so cost is 3 queries regardless of tree depth, and the bulk
resolver (`readable_team_folder_roles`) shares that index rather than
re-walking per folder.
"""

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER

SCOPE_TEAM = PersonalNoteFolder.SCOPE_TEAM
VISIBILITY_PUBLIC = PersonalNoteFolder.VISIBILITY_PUBLIC
VISIBILITY_PRIVATE = PersonalNoteFolder.VISIBILITY_PRIVATE


def is_team_member(team_id, user_id) -> bool:
    """True if the user belongs to the team.

    Checks the owner FK as well as the membership table: the team owner
    is identified by `TeamMaster.owner` and may not carry a `TeamMembers`
    row at all (see `origin/services/member_roles.py`), so a bare
    membership query would deny the one person who can never be denied.
    """
    if user_id is None or team_id is None:
        return False
    if TeamMaster.objects.filter(team_id=team_id, owner=user_id).exists():
        return True
    return TeamMembers.objects.filter(team=team_id, attendee=user_id, is_deleted=False).exists()


def load_team_folder_index(team_id):
    """All team-scoped folders for a team, as {folder_id: folder}."""
    return {
        f.folder_id: f
        for f in PersonalNoteFolder.objects.filter(team=team_id, scope=SCOPE_TEAM).only(
            "folder_id", "parent_folder_id", "name", "visibility", "owner_id", "team_id"
        )
    }


def load_my_folder_grants(team_id, user_id):
    """This user's folder grants in a team, as {folder_id: role_id}."""
    return dict(
        NoteFolderPermission.objects.filter(team=team_id, user=user_id).values_list(
            "folder_id", "role_id"
        )
    )


def resolve_role_in_index(folder_id, *, folders, grants, member, user_id):
    """Walk `folder_id` upward through `folders`, applying the
    inheritance model in this module's docstring. Returns a role_id or
    None. The visited set guards against a corrupt parent cycle."""
    current = folder_id
    visited = set()
    while current is not None and current not in visited:
        visited.add(current)
        folder = folders.get(current)
        if folder is None:
            # Parent missing or not team-scoped — the chain is broken, so
            # there is nothing further to inherit from.
            return None

        granted = grants.get(current)
        if granted is not None:
            return granted
        if folder.owner_id is not None and str(folder.owner_id) == str(user_id):
            # Creator fallback, so a folder can never be orphaned by a
            # revoked owner row.
            return ROLE_OWNER

        if folder.visibility is not None:
            # Override point: resolution stops here whether or not it
            # granted anything.
            if folder.visibility == VISIBILITY_PUBLIC and member:
                return ROLE_EDITOR
            return None

        current = folder.parent_folder_id

    return None


def get_folder_role(user_id, folder_id, team_id=None):
    """Effective role_id on one team folder, or None."""
    if folder_id is None:
        return None

    if team_id is None:
        team_id = (
            PersonalNoteFolder.objects.filter(folder_id=folder_id, scope=SCOPE_TEAM)
            .values_list("team_id", flat=True)
            .first()
        )
        if team_id is None:
            return None

    folders = load_team_folder_index(team_id)
    if folder_id not in folders:
        return None

    return resolve_role_in_index(
        folder_id,
        folders=folders,
        grants=load_my_folder_grants(team_id, user_id),
        member=is_team_member(team_id, user_id),
        user_id=user_id,
    )


def readable_team_folder_roles(user_id, team_id):
    """Every team folder this user can reach → {folder_id: role_id}.

    Resolved from a single shared index so listing the whole space costs
    the same 3 queries as resolving one folder. Callers that then need
    the folders' NOTES must filter on these ids in one query — resolving
    per note would be an N+1 across the space.
    """
    folders = load_team_folder_index(team_id)
    grants = load_my_folder_grants(team_id, user_id)
    member = is_team_member(team_id, user_id)

    out = {}
    for folder_id in folders:
        role = resolve_role_in_index(
            folder_id, folders=folders, grants=grants, member=member, user_id=user_id
        )
        if role is not None:
            out[folder_id] = role
    return out


def folder_ancestor_grant_user_ids(folder_id, folders_by_id):
    """User ids that reach `folder_id` through folder grants, plus a
    ``team:<team_id>`` sentinel when it resolves public.

    Used to build the search index's `acl_user_ids`. The sentinel keeps
    a public folder's chunks from carrying one entry per team member —
    the query side matches `[user_id, "team:<team_id>"]` instead. See
    `origin/search_engine/agent/acl.py`.
    """
    out = set()
    current = folder_id
    visited = set()
    while current is not None and current not in visited:
        visited.add(current)
        folder = folders_by_id.get(current)
        if folder is None:
            break

        out.update(
            str(uid)
            for uid in NoteFolderPermission.objects.filter(folder_id=current).values_list(
                "user_id", flat=True
            )
            if uid is not None
        )
        if folder.owner_id is not None:
            out.add(str(folder.owner_id))

        if folder.visibility is not None:
            if folder.visibility == VISIBILITY_PUBLIC and folder.team_id is not None:
                out.add(f"team:{folder.team_id}")
            break

        current = folder.parent_folder_id

    return out
