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

from django.db import transaction
from rest_framework import status
from rest_framework.response import Response

from origin.models.common.user_models import CustomUser
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTagLink,
    NotePermissionMaster,
)
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
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


class TeamNoteFolderView(AuthenticatedAPIView):
    def get(self, request):
        """Every team folder the caller can reach.

        Resolution runs off one shared in-memory index, so the cost is
        flat in the number of folders rather than per-folder tree walks.
        """
        request_user_id = request.user.id

        data = {"team_id": request.GET.get("team_id"), "user_id": request.GET.get("user_id")}

        if res := validate_request_data(data):
            return res

        if res := validate_request_user(str(request_user_id), str(data["user_id"])):
            return res

        roles = readable_team_folder_roles(request_user_id, data["team_id"])
        if not roles:
            return Response([], status=status.HTTP_200_OK)

        folders = load_team_folder_index(data["team_id"])
        return Response(
            _serialize_folders(list(roles.keys()), roles, folders),
            status=status.HTTP_200_OK,
        )

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

        if not is_team_member(data["team_id"], request_user_id):
            return Response(
                {"error": "You are not a member of this team."},
                status=status.HTTP_403_FORBIDDEN,
            )

        name = str(data["name"]).strip()
        if name == "" or len(name) > 255:
            return Response(
                {"error": "Folder name must be 1-255 characters."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        parent_folder_id = request.data.get("parent_folder_id")
        visibility = request.data.get("visibility")

        if parent_folder_id is None:
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
            if not _can_write(get_folder_role(request_user_id, parent_folder_id, data["team_id"])):
                return Response(
                    {"error": "You do not have permission to add folders here."},
                    status=status.HTTP_403_FORBIDDEN,
                )

        with transaction.atomic():
            folder = PersonalNoteFolder.objects.create(
                team_id=data["team_id"],
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
                team_id=data["team_id"],
                folder=folder,
                user_id=request_user_id,
                role_id=ROLE_OWNER,
                granted_by_id=request_user_id,
            )
            _grant_members(
                folder=folder,
                team_id=data["team_id"],
                granted_by_id=request_user_id,
                user_ids=request.data.get("user_ids") or [],
                groups=request.data.get("groups") or [],
                role_id=request.data.get("role_id") or ROLE_EDITOR,
            )
            sync_folder_tags(folder, data["team_id"], request.data.get("tag_ids"))

        folders = load_team_folder_index(data["team_id"])
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

        try:
            folder = PersonalNoteFolder.objects.get(
                folder_id=data["folder_id"], team=data["team_id"], scope=SCOPE
            )
        except PersonalNoteFolder.DoesNotExist:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, data["team_id"])):
            return Response(
                {"error": "You do not have permission to modify this folder."},
                status=status.HTTP_403_FORBIDDEN,
            )

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
            narrowed = (
                folder.visibility == PersonalNoteFolder.VISIBILITY_PUBLIC
                and visibility != PersonalNoteFolder.VISIBILITY_PUBLIC
            )
            folder.visibility = visibility

        if "parent_folder_id" in request.data:
            target_parent_id = request.data.get("parent_folder_id")
            if target_parent_id is not None:
                if not PersonalNoteFolder.objects.filter(
                    folder_id=target_parent_id, team=data["team_id"], scope=SCOPE
                ).exists():
                    return Response(
                        {"error": "Target folder not found."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if not _can_write(
                    get_folder_role(request_user_id, target_parent_id, data["team_id"])
                ):
                    return Response(
                        {"error": "You do not have permission to move it there."},
                        status=status.HTTP_403_FORBIDDEN,
                    )
                if _creates_cycle(data["team_id"], folder.folder_id, target_parent_id):
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
            sync_folder_tags(folder, data["team_id"], request.data.get("tag_ids") or [])

        if narrowed:
            resync_folder_subtree(folder.folder_id, data["team_id"])

        folders = load_team_folder_index(data["team_id"])
        role = get_folder_role(request_user_id, folder.folder_id, data["team_id"])
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

        try:
            folder = PersonalNoteFolder.objects.get(
                folder_id=data["folder_id"], team=data["team_id"], scope=SCOPE
            )
        except PersonalNoteFolder.DoesNotExist:
            return Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)

        if get_folder_role(request_user_id, folder.folder_id, data["team_id"]) != ROLE_OWNER:
            return Response(
                {"error": "Only the folder owner can delete it."},
                status=status.HTTP_403_FORBIDDEN,
            )

        folder_ids = collect_folder_subtree_ids(folder.folder_id, data["team_id"])
        note_ids = collect_note_ids_in_folders(folder_ids, data["team_id"])

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
        roles = readable_team_folder_roles(request_user_id, data["team_id"])
        if not roles:
            return Response([], status=status.HTTP_200_OK)

        notes = (
            PersonalNoteMaster.objects.filter(
                team=data["team_id"], folder_id__in=list(roles.keys())
            )
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

        return Response(
            [
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
            ],
            status=status.HTTP_200_OK,
        )


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
        try:
            folder = PersonalNoteFolder.objects.get(
                folder_id=data["folder_id"], team=data["team_id"], scope=SCOPE
            )
        except PersonalNoteFolder.DoesNotExist:
            return None, Response({"error": "Folder not found."}, status=status.HTTP_404_NOT_FOUND)
        return folder, None

    def get(self, request):
        request_user_id = request.user.id

        data = {
            "team_id": request.GET.get("team_id"),
            "folder_id": request.GET.get("folder_id"),
        }
        if res := validate_request_data(data):
            return res

        folder, err = self._load(request, data)
        if err:
            return err

        if get_folder_role(request_user_id, folder.folder_id, data["team_id"]) is None:
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

        folder, err = self._load(request, data)
        if err:
            return err

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, data["team_id"])):
            return Response(
                {"error": "You do not have permission to manage this folder's members."},
                status=status.HTTP_403_FORBIDDEN,
            )

        role_id = request.data.get("role_id") or ROLE_EDITOR
        try:
            role_id = int(role_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid role_id."}, status=status.HTTP_400_BAD_REQUEST)
        if role_id not in VALID_ROLE_IDS:
            return Response({"error": "Invalid role_id."}, status=status.HTTP_400_BAD_REQUEST)

        _grant_members(
            folder=folder,
            team_id=data["team_id"],
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

        folder, err = self._load(request, data)
        if err:
            return err

        if not _can_write(get_folder_role(request_user_id, folder.folder_id, data["team_id"])):
            return Response(
                {"error": "You do not have permission to manage this folder's members."},
                status=status.HTTP_403_FORBIDDEN,
            )

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
        resync_folder_subtree(folder.folder_id, data["team_id"])

        return Response({"message": "Member removed."}, status=status.HTTP_200_OK)
