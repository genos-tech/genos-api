"""Team Notes — the shared "general" note space.

My Notes, Task Notes and Chat Notes are each anchored to something that
already exists (your account, a task, a channel). Team Notes is the
place for a note that just belongs to the TEAM: a handbook page, a
meeting record, onboarding docs.

Notes here are ordinary `PersonalNoteMaster` rows (`note_type=1`) — the
FOLDER carries the ACL. That is what lets team notes reuse the entire
personal-note stack unchanged: the same editor, Yjs room, version
history, favorites, recents, markdown import/export and IndexedDB store,
and the same `get_effective_role` gate that the Hocuspocus collab server
consults. See `origin/views/utils/note_folder_role.py` for the
inheritance model (visibility NULL = inherit, non-NULL = override).

Why these handlers exist rather than reusing `personal_note_folder_views`:

  - Every query there is owner-scoped, which is exactly wrong for a
    space whose whole point is that other people's content lives in it.
    An owner-filtered subtree walk stops at the first folder someone
    else created.
  - Its DELETE is an unconditional recursive hard-delete. Reused here it
    would destroy colleagues' notes — and, because the folder delete is
    NOT owner-filtered while the note collection IS, it would orphan the
    ones it failed to collect. Delete here REFUSES instead.

Cycle checks and subtree walks in this module therefore key on TEAM
alone, never on owner.
"""

from collections import defaultdict

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.team_models import ExternalGrant, TeamMaster
from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTagLink,
    NotePermissionMaster,
)
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.services.external_grants import external_objects_for_member, externally_shared
from origin.services.group_expansion import expand_groups
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.note.note_folder_tag_views import sync_folder_tags
from origin.views.utils.note_acl_resync import (
    collect_folder_subtree_ids,
    collect_note_ids_in_folders,
    purge_notes_from_index,
    resync_folder_subtree,
)
from origin.views.utils.note_folder_role import (
    get_folder_role,
    is_team_member,
    load_team_folder_index,
    readable_team_folder_roles,
)
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_OWNER, ROLE_VIEWER
from origin.views.utils.request_validators import validate_request_data, validate_request_user

NOTE_TYPE = 1  # Team notes are personal notes; the folder holds the ACL.
SCOPE = PersonalNoteFolder.SCOPE_TEAM

VALID_VISIBILITIES = {
    PersonalNoteFolder.VISIBILITY_PUBLIC,
    PersonalNoteFolder.VISIBILITY_PRIVATE,
}
VALID_ROLE_IDS = {ROLE_OWNER, ROLE_EDITOR, ROLE_VIEWER}


def _effective_visibility(folder_id, folders):
    """The visibility a folder actually behaves as, resolved up the
    chain. Distinct from its own `visibility`, which is NULL when the
    folder inherits."""
    current = folder_id
    visited = set()
    while current is not None and current not in visited:
        visited.add(current)
        folder = folders.get(current)
        if folder is None:
            return None
        if folder.visibility is not None:
            return folder.visibility
        current = folder.parent_folder_id
    return None


def _folder_dict(folder, *, role_id, folders, owner_names, member_counts, tags_by_folder):
    """CamelCase wire shape, mirroring the meta endpoints' manual style."""
    return {
        "folderId": folder.folder_id,
        "parentFolderId": folder.parent_folder_id,
        "name": folder.name,
        "visibility": folder.visibility,
        "effectiveVisibility": _effective_visibility(folder.folder_id, folders),
        "myRoleId": role_id,
        "ownerId": folder.owner_id,
        "ownerName": owner_names.get(str(folder.owner_id)),
        "memberCount": member_counts.get(folder.folder_id, 0),
        "tags": tags_by_folder.get(folder.folder_id, []),
        "tsCreated": folder.ts_created_at,
        "tsUpdated": folder.ts_updated_at,
    }


def _serialize_folders(folder_ids, roles, folders):
    """Batch every per-folder lookup the wire shape needs, so listing the
    space stays a fixed number of queries rather than one per folder."""
    owner_ids = {str(folders[fid].owner_id) for fid in folder_ids if folders[fid].owner_id}
    owner_names = {
        str(row["id"]): row["username"]
        for row in CustomUser.objects.filter(id__in=owner_ids).values("id", "username")
    }

    member_counts = {}
    for fid, uid in NoteFolderPermission.objects.filter(folder_id__in=folder_ids).values_list(
        "folder_id", "user_id"
    ):
        if uid is not None:
            member_counts[fid] = member_counts.get(fid, 0) + 1

    tags_by_folder = {}
    for row in NoteFolderTagLink.objects.filter(folder_id__in=folder_ids).values(
        "folder_id", "tag__tag_id", "tag__name", "tag__color", "tag__text_color"
    ):
        tags_by_folder.setdefault(row["folder_id"], []).append(
            {
                "tagId": row["tag__tag_id"],
                "name": row["tag__name"],
                "color": row["tag__color"],
                "textColor": row["tag__text_color"],
            }
        )

    return [
        _folder_dict(
            folders[fid],
            role_id=roles[fid],
            folders=folders,
            owner_names=owner_names,
            member_counts=member_counts,
            tags_by_folder=tags_by_folder,
        )
        for fid in sorted(folder_ids, key=lambda f: folders[f].name.lower())
    ]


def _can_write(role_id):
    return role_id is not None and role_id <= ROLE_EDITOR


def _require_host_member(team_id, user_id):
    """Refuse folder ADMINISTRATION to anyone outside the owning team.

    Cross-team sharing put a new kind of person in this file: an external
    editor, who holds a `NoteFolderPermission` row on a folder belonging to
    a team they are not in. Editor is the right level for the content —
    writing notes in the shared folder is the whole point — but every
    handler that reaches this guard edits the FOLDER rather than a note,
    and none of that is an outsider's to do:

    * renaming, re-parenting or re-scoping the host's folder;
    * changing who else is in it. `_grant_members` would happily let an
      external editor add HOST team members (they pass its membership
      filter), and the roster DELETE would let them evict the host's own
      people from the host's folder.

    Read and write on the notes themselves stay role-based and untouched.
    Returns a Response to bail out with, or None to proceed.

    One exception, in `_guest_may_rename`: a subfolder an outsider made
    inside the share, and its name only.
    """
    if is_team_member(team_id, user_id):
        return None
    return Response(
        {"error": "Only members of the owning team can manage this folder."},
        status=status.HTTP_403_FORBIDDEN,
    )


def _guest_may_rename(folder, user_id, team_id, payload) -> bool:
    """May this outsider rename this folder, and nothing else about it?

    An editor admitted through a share can create subfolders inside it and
    can delete the ones they own (DELETE is owner-gated, not
    team-gated). Renaming was the only one of the three they could not do,
    which left a typo permanent unless they deleted the folder and its
    contents and started again.

    Narrow on purpose. It is their OWN folder (owner role), never the
    shared folder itself (which is the host's, and is the thing the grant
    names), and the request must carry nothing but a name — moving it
    would drag content out of the share, re-scoping it is refused
    everywhere else in this file, and tags belong to the host's catalog.
    """
    if folder.parent_folder_id is None:
        return False
    if externally_shared(ExternalGrant.ObjectType.NOTE_FOLDER, folder.folder_id):
        return False
    if get_folder_role(user_id, folder.folder_id, team_id) != ROLE_OWNER:
        return False
    return set(payload.keys()) <= {"team_id", "user_id", "folder_id", "name"}


def _creates_cycle(team_id, folder_id, target_parent_id) -> bool:
    """True if putting `folder_id` under `target_parent_id` would create
    a cycle. Walks by TEAM only — a team folder's ancestor chain crosses
    owners, so the owner-filtered walk used for personal folders would
    stop early and wave a real cycle through."""
    current = target_parent_id
    visited = set()
    while current is not None:
        if current == folder_id:
            return True
        if current in visited:
            return True
        visited.add(current)
        current = (
            PersonalNoteFolder.objects.filter(folder_id=current, team=team_id, scope=SCOPE)
            .values_list("parent_folder_id", flat=True)
            .first()
        )
    return False


def _walks_up_to(folder_id, roots: set, folders) -> bool:
    """Is `folder_id` the shared folder itself, or inside it?

    Needed because reach in the host team is broader than one share: the
    same person may hold folder rows there through a grant to a DIFFERENT
    team of theirs, or through a mention group. Those are legitimate
    access, but they do not belong in THIS team's folder list, so the
    subtree is the unit rather than "everything I can read over there".
    """
    current = folder_id
    visited = set()
    while current is not None and str(current) not in visited:
        visited.add(str(current))
        if str(current) in roots:
            return True
        folder = folders.get(current)
        if folder is None:
            return False
        current = folder.parent_folder_id
    return False


def _external_reach(user_id, viewing_team_id) -> list:
    """What this person reaches through folders shared WITH `viewing_team_id`.

    Returns one entry per host team: `(host_team_id, roots, host_folders,
    reachable_roles)`. Shared by the folder list and the note list so the
    two cannot disagree about which foreign folders are in play — a note
    visible in a folder that isn't, or the reverse, is a bug that only
    shows up as an empty folder.
    """
    shared = external_objects_for_member(
        ExternalGrant.ObjectType.NOTE_FOLDER, viewing_team_id, user_id
    )
    if not shared:
        return []

    roots_by_host = defaultdict(set)
    for folder_id, grant in shared.items():
        roots_by_host[str(grant.owner_team_id)].add(str(folder_id))

    out = []
    for host_team_id, roots in roots_by_host.items():
        host_folders = load_team_folder_index(host_team_id)
        # The host team's own resolver, so inheritance, private overrides
        # and viewer/editor roles behave over there exactly as they do at
        # home. `member=False` inside it is what keeps the host's public
        # folders out of an outsider's reach.
        reachable = {
            folder_id: role
            for folder_id, role in readable_team_folder_roles(user_id, host_team_id).items()
            if _walks_up_to(folder_id, roots, host_folders)
        }
        if reachable:
            out.append((host_team_id, roots, host_folders, reachable))
    return out


def _external_folders(user_id, viewing_team_id) -> list:
    """Folders another team shared with this one, as rows for its own list.

    Each shared folder is a subtree of the HOST team's tree, so it arrives
    with a `parentFolderId` pointing at a folder the caller cannot see.
    The share itself is re-rooted (`parentFolderId: None`) and its
    descendants keep their real parents, which makes it render as a
    top-level folder here and an ordinary subtree underneath.
    """
    reach = _external_reach(user_id, viewing_team_id)
    if not reach:
        return []
    host_names = {
        str(team_id): name
        for team_id, name in TeamMaster.objects.filter(
            team_id__in=[host_team_id for host_team_id, _, _, _ in reach]
        ).values_list("team_id", "team_name")
    }

    out = []
    for host_team_id, roots, host_folders, reachable in reach:
        for row in _serialize_folders(list(reachable.keys()), reachable, host_folders):
            row["isExternal"] = True
            row["hostTeamId"] = host_team_id
            row["hostTeamName"] = host_names.get(host_team_id, "")
            if str(row["folderId"]) in roots:
                row["parentFolderId"] = None
            out.append(row)
    return out


def _resolve_folder(folder_id, viewing_team_id, user_id):
    """One team folder by id, reached from the team the caller is VIEWING.

    The client sends the team whose Team Notes it is showing, and every
    handler here used to scope its lookup by it — sound while a folder in
    this list always belonged to that team. Sharing broke that: a folder
    another team shared belongs to the HOST, so the scoped lookup answered
    "not found" for every write on it, including on the subfolders the
    caller had just been allowed to create inside it.

    Resolve by id instead and take the owning team from the folder, then
    confirm the caller reaches it: their own team's folder, or one inside a
    subtree shared with the team they are viewing. Anything else reads as
    missing rather than forbidden — the same answer the team-scoped lookup
    gave, so a guessed id still can't confirm that somebody else's folder
    exists.

    Returns `(folder, owning_team_id)`, or `(None, None)`.
    """
    try:
        folder = PersonalNoteFolder.objects.get(folder_id=folder_id, scope=SCOPE)
    except (PersonalNoteFolder.DoesNotExist, ValueError, TypeError):
        return None, None

    owning_team_id = str(folder.team_id)
    if owning_team_id == str(viewing_team_id):
        return folder, owning_team_id
    reach = _external_reach(user_id, viewing_team_id)
    for host_team_id, _roots, _host_folders, reachable in reach:
        if host_team_id == owning_team_id and folder.folder_id in reachable:
            return folder, owning_team_id
    return None, None


class TeamNoteFolderView(AuthenticatedAPIView):
    def get(self, request):
        """Every team folder the caller can reach.

        Resolution runs off one shared in-memory index, so the cost is
        flat in the number of folders rather than per-folder tree walks.

        Plus the folders another team shared with this one. They live in
        the host's tree, but this is the list the caller's Team Notes
        renders from, and a folder only reachable by switching companies
        in the team picker was a folder nobody found.
        """
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        roles = readable_team_folder_roles(request_user_id, data["team_id"])
        folders = load_team_folder_index(data["team_id"])
        payload = _serialize_folders(list(roles.keys()), roles, folders) if roles else []
        # Appended after the home team's own folders, and deliberately not
        # merged into the sort: someone else's folder is a different kind
        # of thing and reads better grouped than interleaved by name.
        payload += _external_folders(request_user_id, data["team_id"])
        return Response(payload, status=status.HTTP_200_OK)

    def post(self, request):
        """Create a team folder.

        A ROOT folder must state its visibility — it has no ancestor to
        inherit from, so leaving it NULL would make it unreachable. A
        SUBFOLDER defaults to NULL (inherit), which is the "everyone who
        can reach the parent can reach this" behavior; passing an
        explicit visibility is how a subfolder narrows.
        """
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "name": request.data.get("name"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        name = str(data["name"]).strip()
        if name == "" or len(name) > 255:
            return Response(
                {"error": "Folder name must be 1-255 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parent_folder_id = request.data.get("parent_folder_id")
        visibility = request.data.get("visibility")

        # Which team the folder will belong to. For a SUBFOLDER that is the
        # parent's team, not the team the client happens to be viewing: a
        # folder shared with you belongs to the host, and a child filed
        # under the viewer's team instead would sit in a tree neither side
        # can see.
        owning_team_id = data["team_id"]
        if parent_folder_id is not None:
            parent_team_id = (
                PersonalNoteFolder.objects.filter(folder_id=parent_folder_id, scope=SCOPE)
                .values_list("team_id", flat=True)
                .first()
            )
            if parent_team_id is None:
                return Response(
                    {"error": "Parent folder not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            owning_team_id = str(parent_team_id)

        # Team membership gates a ROOT folder only. A subfolder is
        # authorized by the role on its parent, which is what lets an
        # editor admitted through a share organize the work inside it —
        # the parity the feature claimed and did not have: they could write
        # notes in the shared folder but not group them.
        is_host_member = is_team_member(owning_team_id, request_user_id)
        if parent_folder_id is None:
            if not is_host_member:
                return Response(
                    {"error": "You are not a member of this team."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if visibility not in VALID_VISIBILITIES:
                return Response(
                    {"error": 'A top-level folder must be "public" or "private".'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            if visibility is not None and visibility not in VALID_VISIBILITIES:
                return Response(
                    {"error": 'visibility must be "public", "private", or null to inherit.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not _can_write(get_folder_role(request_user_id, parent_folder_id, owning_team_id)):
                return Response(
                    {"error": "You do not have permission to add folders here."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if not is_host_member:
                # An outsider's subfolder inherits and grants nobody.
                # `visibility="public"` would hand every member of a
                # company they don't work for a folder inside their own
                # share, and an explicit roster would re-share the host's
                # data onward to a third party — the one thing every other
                # path in this feature refuses.
                visibility = None

        with transaction.atomic():
            folder = PersonalNoteFolder.objects.create(
                team_id=owning_team_id,
                owner_id=request_user_id,
                parent_folder_id=parent_folder_id,
                name=name,
                scope=SCOPE,
                visibility=visibility,
            )
            # The creator's OWNER row is written explicitly rather than
            # inferred, so the roster and member count tell the truth and
            # ownership can later be transferred like any other grant.
            NoteFolderPermission.objects.create(
                team_id=owning_team_id,
                folder=folder,
                user_id=request_user_id,
                role_id=ROLE_OWNER,
                granted_by_id=request_user_id,
            )
            _grant_members(
                folder=folder,
                team_id=owning_team_id,
                granted_by_id=request_user_id,
                user_ids=(request.data.get("user_ids") or []) if is_host_member else [],
                groups=(request.data.get("groups") or []) if is_host_member else [],
                role_id=request.data.get("role_id") or ROLE_EDITOR,
            )
            sync_folder_tags(folder, owning_team_id, request.data.get("tag_ids"))

        folders = load_team_folder_index(owning_team_id)
        return Response(
            _serialize_folders([folder.folder_id], {folder.folder_id: ROLE_OWNER}, folders)[0],
            status=status.HTTP_201_CREATED,
        )

    def put(self, request):
        """Rename, move, or change visibility. Editor+ on the folder (and
        on the move target). Keeps this codebase's key-presence semantics:
        `parent_folder_id` present-but-null means "move to root"."""
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "user_id": request.data.get("user_id"),
            "folder_id": request.data.get("folder_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        folder, owning_team_id = _resolve_folder(
            data["folder_id"], data["team_id"], request_user_id
        )
        if folder is None:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, owning_team_id)):
            return Response(
                {"error": "You do not have permission to modify this folder."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if not is_team_member(owning_team_id, request_user_id) and not _guest_may_rename(
            folder, request_user_id, owning_team_id, request.data
        ):
            return _require_host_member(owning_team_id, request_user_id)

        if "name" in request.data and request.data.get("name") is not None:
            name = str(request.data["name"]).strip()
            if name == "" or len(name) > 255:
                return Response(
                    {"error": "Folder name must be 1-255 characters."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            folder.name = name

        # Track whether access is being narrowed, so the search index can
        # be corrected immediately instead of at the next reindex pass.
        narrowed = False

        if "visibility" in request.data:
            visibility = request.data.get("visibility")
            if visibility is not None and visibility not in VALID_VISIBILITIES:
                return Response(
                    {"error": 'visibility must be "public", "private", or null to inherit.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if visibility is None and folder.parent_folder_id is None:
                return Response(
                    {"error": "A top-level folder cannot inherit its visibility."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # An externally shared folder must stay explicitly private for
            # as long as the share stands — the same rule `offer_grant`
            # applies up front, enforced here so it cannot be undone
            # afterwards. Public would hand every host member editor and
            # emit a `team:<id>` search sentinel, making the folder's ACL
            # mean two things at once; inherit would let an ancestor
            # silently redefine what was shared. Ending the share is the
            # way out, and it is one action.
            if (
                visibility != PersonalNoteFolder.VISIBILITY_PRIVATE
                and externally_shared(ExternalGrant.ObjectType.NOTE_FOLDER, folder.folder_id)
            ):
                return Response(
                    {
                        "error": (
                            "This folder is shared with another team, so it must stay private. "
                            "Stop sharing it first."
                        )
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            narrowed = (
                folder.visibility == PersonalNoteFolder.VISIBILITY_PUBLIC
                and visibility != PersonalNoteFolder.VISIBILITY_PUBLIC
            )
            folder.visibility = visibility

        if "parent_folder_id" in request.data:
            target_parent_id = request.data.get("parent_folder_id")
            if target_parent_id is not None:
                # Within the owning team only: a folder cannot change hands
                # by being moved, and the owning team is the one whose tree
                # this folder lives in.
                if not PersonalNoteFolder.objects.filter(
                    folder_id=target_parent_id, team=owning_team_id, scope=SCOPE
                ).exists():
                    return Response(
                        {"error": "Target folder not found."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not _can_write(
                    get_folder_role(request_user_id, target_parent_id, owning_team_id)
                ):
                    return Response(
                        {"error": "You do not have permission to move it there."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if _creates_cycle(owning_team_id, folder.folder_id, target_parent_id):
                    return Response(
                        {"error": "Cannot move a folder into its own descendant."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            elif folder.visibility is None:
                return Response(
                    {"error": "A folder that inherits its visibility cannot move to the top."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            # Re-parenting re-resolves inherited access for the whole
            # subtree; treat it as narrowing since we can't cheaply prove
            # it isn't.
            narrowed = narrowed or target_parent_id != folder.parent_folder_id
            folder.parent_folder_id = target_parent_id

        folder.save()

        # Key presence, like the structural fields: omitting `tag_ids`
        # leaves tags alone, sending `[]` clears them.
        if "tag_ids" in request.data:
            sync_folder_tags(folder, owning_team_id, request.data.get("tag_ids") or [])

        if narrowed:
            resync_folder_subtree(folder.folder_id, owning_team_id)

        folders = load_team_folder_index(owning_team_id)
        role = get_folder_role(request_user_id, folder.folder_id, owning_team_id)
        return Response(
            _serialize_folders([folder.folder_id], {folder.folder_id: role}, folders)[0],
            status=status.HTTP_200_OK,
        )

    def delete(self, request):
        """Delete a team folder — REFUSING when it holds anyone else's
        content.

        The personal-folder delete is an unconditional recursive
        hard-delete, which is fine for a private organization layer. Here
        the same behavior would let one person destroy a colleague's
        work, so instead we 409 with the counts and let the UI explain
        what has to be cleared first.
        """
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "user_id": request.GET.get("user_id"),
            "folder_id": request.GET.get("folder_id"),
        }

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        folder, owning_team_id = _resolve_folder(
            data["folder_id"], data["team_id"], request_user_id
        )
        if folder is None:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        # Owner of the folder — which an outsider is, for a subfolder they
        # made inside a share. Nothing widens here: the refusal below still
        # stands when anyone else's note or folder is inside, so this
        # deletes only their own work, and the shared folder itself is
        # owned by the host and stays out of reach.
        if get_folder_role(request_user_id, folder.folder_id, owning_team_id) != ROLE_OWNER:
            return Response(
                {"error": "Only the folder owner can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        folder_ids = collect_folder_subtree_ids(folder.folder_id, owning_team_id)
        note_ids = collect_note_ids_in_folders(folder_ids, owning_team_id)

        foreign_notes = (
            PersonalNoteMaster.objects.filter(note_id__in=note_ids)
            .exclude(owner=request_user_id)
            .count()
        )
        foreign_folders = (
            PersonalNoteFolder.objects.filter(folder_id__in=folder_ids)
            .exclude(owner=request_user_id)
            .count()
        )
        if foreign_notes or foreign_folders:
            return Response(
                {
                    "error": "This folder still holds content owned by other people.",
                    "foreignNoteCount": foreign_notes,
                    "foreignFolderCount": foreign_folders,
                },
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            PersonalNoteMaster.objects.filter(note_id__in=note_ids).delete()
            NotePermissionMaster.objects.filter(note_type=NOTE_TYPE, note_id__in=note_ids).delete()
            NoteVersionMaster.objects.filter(note_type=NOTE_TYPE, note_id__in=note_ids).delete()
            # NoteFolderPermission / NoteFolderTagLink cascade on the FK.
            PersonalNoteFolder.objects.filter(folder_id__in=folder_ids).delete()

        purge_notes_from_index(note_ids)

        return Response(
            {
                "message": "Folder and contents deleted.",
                "deletedFolderIds": sorted(folder_ids),
                "deletedNoteIds": sorted(note_ids),
            },
            status=status.HTTP_200_OK,
        )


class TeamNoteMetaView(AuthenticatedAPIView):
    """Notes filed in team folders the caller can read — ANY owner."""

    def get(self, request):
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        # Resolve the readable FOLDER set once, then fetch its notes in a
        # single query. Asking `get_folder_role` per note would be an
        # N+1 across the whole space.
        #
        # One pass per team whose folders are in play: the caller's own,
        # then each host team that shared a folder with it. Notes in a
        # shared folder carry the HOST team's id, so a single query scoped
        # to the caller's team returned the folder with nothing in it.
        scopes = [(data["team_id"], readable_team_folder_roles(request_user_id, data["team_id"]))]
        scopes += [
            (host_team_id, reachable)
            for host_team_id, _, _, reachable in _external_reach(request_user_id, data["team_id"])
        ]

        payload = []
        for team_id, roles in scopes:
            if not roles:
                continue
            notes = (
                PersonalNoteMaster.objects.filter(team=team_id, folder_id__in=list(roles.keys()))
                .select_related("owner")
                .order_by("-ts_updated_at")
                .values(
                    "note_id",
                    "parent_note_id",
                    "folder_id",
                    "title",
                    "ts_updated_at",
                    "owner__id",
                    "owner__username",
                )
            )
            payload += [
                {
                    "noteType": NOTE_TYPE,
                    "noteId": n["note_id"],
                    "parentNoteId": n["parent_note_id"],
                    "folderId": n["folder_id"],
                    "title": n["title"],
                    "tsUpdated": n["ts_updated_at"],
                    "ownerId": n["owner__id"],
                    "ownerName": n["owner__username"],
                    "roleId": roles.get(n["folder_id"]),
                }
                for n in notes
            ]

        return Response(payload, status=status.HTTP_200_OK)


def _grant_members(*, folder, team_id, granted_by_id, user_ids, groups, role_id):
    """Upsert grants for explicit users and for expanded groups.

    Group membership is snapshotted here (see
    `origin/services/group_expansion.py`); the resulting rows carry the
    provenance so the UI can show "invited via @eng-team" and offer a
    re-sync. Explicit picks win over group-derived ones for the same
    user, so a deliberate choice is never overwritten by a bulk add.
    """
    if role_id not in VALID_ROLE_IDS:
        role_id = ROLE_EDITOR

    targets = {}
    for user_id, (group_type, group_id) in expand_groups(team_id, groups).items():
        targets[str(user_id)] = (group_type, group_id)
    for user_id in user_ids or []:
        targets[str(user_id)] = (None, None)

    if not targets:
        return 0

    # Only real teammates — a stale id must not become a grant.
    #
    # This deliberately stays closed to external people even though the
    # folder can now be shared across teams. Cross-team access is written
    # by `services/external_grants.add_external_participants`, called by
    # the GUEST team's managers: the host consents to the team and the
    # folder, never to the individuals. Widening this filter would let the
    # host pick the other organization's people — the one thing the
    # one-time-approval design exists to prevent — and would write rows
    # with no grant behind them, which the revocation cascades then could
    # not find.
    valid_ids = {
        str(uid)
        for uid in targets
        if is_team_member(team_id, uid) and str(uid) != str(folder.owner_id)
    }

    granted = 0
    for user_id in valid_ids:
        group_type, group_id = targets[user_id]
        _, created = NoteFolderPermission.objects.update_or_create(
            folder=folder,
            user_id=user_id,
            defaults={
                "team_id": team_id,
                "role_id": role_id,
                "via_group_type": group_type,
                "via_group_id": group_id,
                "granted_by_id": granted_by_id,
            },
        )
        granted += 1 if created else 0
    return granted


class TeamNoteFolderMemberView(AuthenticatedAPIView):
    """The roster on one team folder."""

    def _load(self, request, data):
        """The folder, the team that OWNS it, and an error to bail out with.

        The owning team is not always the team in the request: a folder
        shared with the caller's team belongs to the host, and every check
        below has to be made against that team rather than the one the
        client is viewing.
        """
        folder, owning_team_id = _resolve_folder(
            data["folder_id"], data["team_id"], request.user.id
        )
        if folder is None:
            return (
                None,
                None,
                Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND),
            )
        return folder, owning_team_id, None

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "folder_id": request.GET.get("folder_id"),
        }
        if res := validate_request_data(data):
            return res

        folder, owning_team_id, err = self._load(request, data)
        if err:
            return err

        if get_folder_role(request_user_id, folder.folder_id, owning_team_id) is None:
            return Response(
                {"error": "You do not have access to this folder."},
                status=status.HTTP_403_FORBIDDEN,
            )

        rows = (
            NoteFolderPermission.objects.filter(folder=folder)
            .select_related("user")
            .values(
                "user__id",
                "user__username",
                "user__profile_image_url",
                "role_id",
                "via_group_type",
                "via_group_id",
                "ts_created_at",
            )
        )
        return Response(
            [
                {
                    "userId": r["user__id"],
                    "userName": r["user__username"],
                    "avatarUrl": r["user__profile_image_url"],
                    "roleId": r["role_id"],
                    "viaGroupType": r["via_group_type"],
                    "viaGroupId": r["via_group_id"],
                    "tsCreated": r["ts_created_at"],
                }
                for r in rows
            ],
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        """Grant a role to individual users and/or whole groups."""
        request_user_id = request.user.id

        data = {
            "team_id": request.data.get("team_id"),
            "folder_id": request.data.get("folder_id"),
        }
        if res := validate_request_data(data):
            return res

        folder, owning_team_id, err = self._load(request, data)
        if err:
            return err

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, owning_team_id)):
            return Response(
                {"error": "You do not have permission to manage this folder's members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if res := _require_host_member(owning_team_id, request_user_id):
            return res

        role_id = request.data.get("role_id") or ROLE_EDITOR
        try:
            role_id = int(role_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid role_id."}, status=status.HTTP_400_BAD_REQUEST)
        if role_id not in VALID_ROLE_IDS:
            return Response({"error": "Invalid role_id."}, status=status.HTTP_400_BAD_REQUEST)

        _grant_members(
            folder=folder,
            team_id=owning_team_id,
            granted_by_id=request_user_id,
            user_ids=request.data.get("user_ids") or [],
            groups=request.data.get("groups") or [],
            role_id=role_id,
        )

        # Granting only broadens access, so the widened reindex window
        # picks it up — bump the folder so that window actually matches.
        folder.save(update_fields=["ts_updated_at"])

        return Response({"message": "Members updated."}, status=status.HTTP_200_OK)

    def delete(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "folder_id": request.GET.get("folder_id"),
            "target_user_id": request.GET.get("target_user_id"),
        }
        if res := validate_request_data(data):
            return res

        folder, owning_team_id, err = self._load(request, data)
        if err:
            return err

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, owning_team_id)):
            return Response(
                {"error": "You do not have permission to manage this folder's members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        if res := _require_host_member(owning_team_id, request_user_id):
            return res

        target = NoteFolderPermission.objects.filter(
            folder=folder, user_id=data["target_user_id"]
        ).first()
        if target is None:
            return Response({"error": "Member not found."}, status=status.HTTP_404_NOT_FOUND)

        if target.role_id == ROLE_OWNER:
            remaining_owners = (
                NoteFolderPermission.objects.filter(folder=folder, role_id=ROLE_OWNER)
                .exclude(user_id=data["target_user_id"])
                .count()
            )
            if remaining_owners == 0:
                return Response(
                    {"error": "A folder must keep at least one owner."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        target.delete()

        # Revoking NARROWS access — purge now rather than leave the
        # content findable by someone who just lost it.
        resync_folder_subtree(folder.folder_id, owning_team_id)

        return Response({"message": "Member removed."}, status=status.HTTP_200_OK)
