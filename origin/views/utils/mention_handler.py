class extractMentionedUsers:
    """BlockNote-body walker for the Django side. Originally only
    collected `type: "mention"` user ids; extended to also collect
    `type: "mentionGroup"` group ids so the task-body save path can
    fan-out group mentions to their members (see
    `resolve_group_members` below)."""

    def __init__(self):
        self.mentioned_user_ids = set()
        self.mentioned_group_ids = set()

    def _check(self, content):
        for c in content:
            if not isinstance(c, dict):
                continue
            t = c.get("type")
            if t == "mention":
                uid = (c.get("props") or {}).get("userId")
                if uid:
                    self.mentioned_user_ids.add(uid)
            elif t == "mentionGroup":
                gid = (c.get("props") or {}).get("groupId")
                if gid is not None:
                    # Coerce to string to dedupe with whatever the
                    # frontend wrote (BlockNote stores prop values as
                    # strings even when numeric).
                    self.mentioned_group_ids.add(str(gid))

    def extract(self, message):
        # A body is a list of blocks. Anything else holds no mention; a number or `true`
        # was a TypeError (500) in every view that walks a body.
        if not isinstance(message, list):
            return
        for m in message:
            if isinstance(m, dict):
                if "content" in m and isinstance(m["content"], list):
                    self._check(m["content"])
                if m.get("children"):
                    self.extract(m["children"])


def resolve_group_members(group_ids, *, team_id):
    """Local (in-process) group→user resolver for Django views. Returns
    a set of user ids (str).

    Resolved for ONE team, at the moment of sending (MIP-1030): only that
    team's live groups, and only the people who are members of that team NOW.
    A group id from another team, a soft-deleted group, a malformed id, and a
    member who was added before the team check existed or has since left the
    team all drop out, so a group mention never notifies someone the team no
    longer counts as its own. `team_id` is required on purpose: a caller that
    forgets it must fail, not silently expand any team's group.

    Empty input → empty set, no query."""
    if not group_ids:
        return set()
    # Lazy imports — keep this module dependency-light for tests that only
    # need the extractor.
    from origin.models.common.mention_group_models import (
        MentionGroupMaster,
        MentionGroupMembers,
    )
    from origin.views.utils.request_validators import as_int, as_uuid

    team = as_uuid(team_id)
    # Block editors store prop values as text; a malformed one names no group.
    keys = {key for key in (as_int(g) for g in group_ids) if key is not None}
    if team is None or not keys:
        return set()
    live_groups = MentionGroupMaster.objects.filter(
        group_id__in=keys, team_id=team, is_deleted=False
    ).values("group_id")
    in_groups = MentionGroupMembers.objects.filter(group_id__in=live_groups).values_list(
        "user_id", flat=True
    )
    return people_of_team(in_groups, team_id=team)


def people_of_team(user_ids, *, team_id, include_guests=False) -> set[str]:
    """Of `user_ids`, the ones who belong to `team_id` right now, as canonical UUID strings.

    A member (a `TeamMembers` row that is not soft-deleted) or the team's owner: the owner's
    own row can be missing from `TeamMembers` in old data, and `scope_guards.is_team_member`
    counts them, so this does too. With `include_guests`, also an external collaborator (a
    `ProjectMembers` row in the team and no member row, `scope_guards.is_guest`), who is a
    legitimate person to @mention in a shared project.

    Ids that are not UUIDs, ids of no user, and people with no tie to the team drop out, so
    a hand-written `userId` in a body cannot name a stranger (MIP-1061).
    """
    from origin.models.common.team_models import TeamMaster, TeamMembers
    from origin.models.project.prj_models import ProjectMembers
    from origin.views.utils.request_validators import as_uuid

    team = as_uuid(team_id)
    wanted = {str(u) for u in (as_uuid(raw) for raw in (user_ids or ())) if u is not None}
    if team is None or not wanted:
        return set()
    found = {
        str(uid)
        for uid in TeamMembers.objects.filter(
            team_id=team, attendee_id__in=wanted, is_deleted=False
        ).values_list("attendee_id", flat=True)
    }
    found |= {
        str(uid)
        for uid in TeamMaster.objects.filter(team_id=team, owner_id__in=wanted).values_list(
            "owner_id", flat=True
        )
    }
    if include_guests:
        found |= {
            str(uid)
            for uid in ProjectMembers.objects.filter(
                team_id=team, attendee_id__in=wanted
            ).values_list("attendee_id", flat=True)
        }
    return found


def resolve_mentions(user_ids, group_ids, *, team_id) -> set[str]:
    """Everyone a body @-mentions, for `team_id`: the directly mentioned people who belong to
    the team (members, the owner, guests) plus its groups' current members (MIP-1061).

    The editor's menu only offers the team's people, but the id in a body is whatever the
    client wrote, and each id becomes a feed row and a web push carrying the text. A mention
    of a team member who is not in the project, channel or folder still goes through (that is
    how they find out it exists); a stranger does not."""
    return people_of_team(user_ids, team_id=team_id, include_guests=True) | resolve_group_members(
        group_ids, team_id=team_id
    )
