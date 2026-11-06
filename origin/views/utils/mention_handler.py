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
    from origin.models.common.team_models import TeamMembers
    from origin.views.utils.request_validators import as_int, as_uuid

    team = as_uuid(team_id)
    # Block editors store prop values as text; a malformed one names no group.
    keys = {key for key in (as_int(g) for g in group_ids) if key is not None}
    if team is None or not keys:
        return set()
    live_groups = MentionGroupMaster.objects.filter(
        group_id__in=keys, team_id=team, is_deleted=False
    ).values("group_id")
    current_members = TeamMembers.objects.filter(team_id=team, is_deleted=False).values(
        "attendee_id"
    )
    return {
        str(uid)
        for uid in MentionGroupMembers.objects.filter(
            group_id__in=live_groups, user_id__in=current_members
        ).values_list("user_id", flat=True)
    }
