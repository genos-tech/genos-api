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


def resolve_group_members(group_ids):
    """Local (in-process) group→user resolver for Django views. Returns
    a set of user_ids. Soft-deleted groups silently drop out (return
    empty set for those ids). Empty input → empty set, no query."""
    if not group_ids:
        return set()
    # Lazy import — keeps this module dependency-light for tests that
    # only need the extractor.
    from origin.models.common.mention_group_models import (
        MentionGroupMaster,
        MentionGroupMembers,
    )

    live_group_ids = set(
        MentionGroupMaster.objects.filter(group_id__in=group_ids, is_deleted=False).values_list(
            "group_id", flat=True
        )
    )
    if not live_group_ids:
        return set()
    return set(
        str(uid)
        for uid in MentionGroupMembers.objects.filter(group_id__in=live_group_ids).values_list(
            "user_id", flat=True
        )
    )
