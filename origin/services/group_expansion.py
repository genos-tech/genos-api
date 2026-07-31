"""Expand an existing group of people into user ids.

Inviting 100 people to a team-note folder by clicking 100 names is a
non-starter, and the product already has three concepts that mean "this
set of people": mention groups, project membership, and GM channel
membership. This resolves any of them to a flat set of user ids so a
folder invite can take a group as its unit.

Expansion is a SNAPSHOT, not a live binding. The caller writes one
`NoteFolderPermission` row per resolved user and records which group it
came from (`via_group_type` / `via_group_id`), so someone who joins the
group later does NOT silently gain folder access. That is deliberate:
search permissions are materialized into OpenSearch at index time, so a
live binding would hand out access the search index doesn't know about,
with no event to trigger a re-index. The same snapshot contract already
governs @group mentions (`MessageMention.via_group_id`).

The provenance is what makes the snapshot workable rather than lossy —
the UI can say "invited via @eng-team" and offer a deliberate re-sync.

Every expansion is TEAM-SCOPED: a group id from another team resolves
empty rather than leaking its roster.
"""

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.note.common_note_models import NoteFolderPermission
from origin.models.project.prj_models import ProjectMaster, ProjectMembers

GROUP_MENTION = NoteFolderPermission.VIA_MENTION_GROUP
GROUP_PROJECT = NoteFolderPermission.VIA_PROJECT
GROUP_GM = NoteFolderPermission.VIA_GM

VALID_GROUP_TYPES = {GROUP_MENTION, GROUP_PROJECT, GROUP_GM}


def _mention_group_user_ids(team_id, group_id):
    # Soft-deleted groups resolve empty, matching MentionGroupResolveView.
    if not MentionGroupMaster.objects.filter(
        group_id=group_id, team=team_id, is_deleted=False
    ).exists():
        return set()
    return {
        str(uid)
        for uid in MentionGroupMembers.objects.filter(group_id=group_id).values_list(
            "user_id", flat=True
        )
        if uid is not None
    }


def _project_user_ids(team_id, project_id):
    if not ProjectMaster.objects.filter(project_id=project_id, team=team_id).exists():
        return set()
    return {
        str(uid)
        for uid in ProjectMembers.objects.filter(project=project_id).values_list(
            "attendee_id", flat=True
        )
        if uid is not None
    }


def _gm_user_ids(team_id, channel_id):
    if not Channel.objects.filter(id=channel_id, team=team_id, kind=ChannelKind.GM).exists():
        return set()
    return {
        str(uid)
        for uid in ChannelMember.objects.filter(
            channel_id=channel_id, is_deleted=False
        ).values_list("user_id", flat=True)
        if uid is not None
    }


def resolve_group_user_ids(team_id, group_type, group_id):
    """User ids in one group, or an empty set for an unknown/foreign id.

    Never raises on a bad id: a stale group in a request should drop out
    of the invite, not 500 the whole call.
    """
    if group_type not in VALID_GROUP_TYPES or group_id in (None, ""):
        return set()
    try:
        if group_type == GROUP_MENTION:
            return _mention_group_user_ids(team_id, int(group_id))
        if group_type == GROUP_PROJECT:
            return _project_user_ids(team_id, int(group_id))
        return _gm_user_ids(team_id, group_id)
    except (TypeError, ValueError):
        # Non-numeric id for a numeric group kind (or a malformed UUID).
        return set()


def expand_groups(team_id, groups):
    """Expand ``[{"type": ..., "id": ...}, ...]`` to
    ``{user_id: (group_type, group_id)}``.

    When a user appears in several groups the FIRST one wins, so the
    recorded provenance is stable and the caller writes one row per user.
    """
    out = {}
    for entry in groups or []:
        if not isinstance(entry, dict):
            continue
        group_type = entry.get("type")
        group_id = entry.get("id")
        for user_id in resolve_group_user_ids(team_id, group_type, group_id):
            out.setdefault(user_id, (group_type, str(group_id)))
    return out
