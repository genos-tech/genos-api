"""Tests for `origin/views/utils/mention_handler.py`.

Distinct from `test_mention_extractor.py`, which covers the *functional*
extractor in `origin/services/mention_extractor.py`. This module exercises
the *class-based* `extractMentionedUsers` walker used by the task-body
save path (`task_views.post`) plus `resolve_group_members`, the in-process
group→member DB resolver.

The extractor itself is pure-Python (no DB); `resolve_group_members`
queries `MentionGroupMaster` / `MentionGroupMembers`, so those tests use
`BaseAPITestCase` for the team + user fixtures.
"""

from django.test import SimpleTestCase

from origin.models.common.mention_group_models import (
    MentionGroupMaster,
    MentionGroupMembers,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.mention_handler import (
    extractMentionedUsers,
    resolve_group_members,
)


class ExtractMentionedUsersTests(SimpleTestCase):
    """The BlockNote walker: user mentions, group mentions, recursion,
    and malformed-node tolerance."""

    def test_collects_user_and_group_ids_together(self):
        h = extractMentionedUsers()
        h.extract(
            [
                {
                    "content": [
                        {"type": "text", "text": "hi"},
                        {"type": "mention", "props": {"userId": "u-1"}},
                        {"type": "mentionGroup", "props": {"groupId": 7}},
                    ]
                }
            ]
        )
        self.assertEqual(h.mentioned_user_ids, {"u-1"})
        # groupId is coerced to str even though it arrived as int.
        self.assertEqual(h.mentioned_group_ids, {"7"})

    def test_group_id_none_or_missing_dropped_zero_kept(self):
        # `if gid is not None` -> 0 is kept (coerced "0"); None / missing dropped.
        h = extractMentionedUsers()
        h.extract(
            [
                {
                    "content": [
                        {"type": "mentionGroup", "props": {"groupId": 0}},
                        {"type": "mentionGroup", "props": {"groupId": None}},
                        {"type": "mentionGroup", "props": {}},  # missing groupId
                        {"type": "mentionGroup", "props": None},  # null props
                    ]
                }
            ]
        )
        self.assertEqual(h.mentioned_group_ids, {"0"})

    def test_falsy_user_id_dropped(self):
        # `if uid:` -> empty/None are not collected.
        h = extractMentionedUsers()
        h.extract(
            [
                {
                    "content": [
                        {"type": "mention", "props": {"userId": ""}},
                        {"type": "mention", "props": {"userId": None}},
                        {"type": "mention", "props": {}},
                        {"type": "mention", "props": {"userId": "keep"}},
                    ]
                }
            ]
        )
        self.assertEqual(h.mentioned_user_ids, {"keep"})

    def test_non_dict_content_children_skipped(self):
        # _check guards each content child with isinstance(c, dict).
        h = extractMentionedUsers()
        h.extract(
            [
                {
                    "content": [
                        "raw text",
                        None,
                        42,
                        {"type": "mention", "props": {"userId": "survivor"}},
                    ]
                }
            ]
        )
        self.assertEqual(h.mentioned_user_ids, {"survivor"})

    def test_recurses_into_children(self):
        h = extractMentionedUsers()
        h.extract(
            [
                {
                    "content": [{"type": "mention", "props": {"userId": "top"}}],
                    "children": [
                        {
                            "content": [
                                {"type": "mention", "props": {"userId": "kid"}},
                                {"type": "mentionGroup", "props": {"groupId": 3}},
                            ]
                        }
                    ],
                }
            ]
        )
        self.assertEqual(h.mentioned_user_ids, {"top", "kid"})
        self.assertEqual(h.mentioned_group_ids, {"3"})

    def test_empty_and_malformed_input_is_noop(self):
        h = extractMentionedUsers()
        h.extract([])
        h.extract(["a string", 123, None, {"no_content": True}, {"content": "not-a-list"}])
        self.assertEqual(h.mentioned_user_ids, set())
        self.assertEqual(h.mentioned_group_ids, set())


class ResolveGroupMembersTests(BaseAPITestCase):
    """The in-process group→member resolver (DB-backed)."""

    def _make_group(self, name="design-team", *, is_deleted=False):
        return MentionGroupMaster.objects.create(
            team=self.team,
            group_name=name,
            created_by=self.user,
            is_deleted=is_deleted,
        )

    def _add_member(self, group, user):
        MentionGroupMembers.objects.create(
            team=self.team, group=group, user=user, added_by=self.user
        )

    def test_empty_input_returns_empty_without_query(self):
        # Empty/falsy short-circuits before touching the DB.
        self.assertEqual(resolve_group_members(set()), set())
        self.assertEqual(resolve_group_members([]), set())
        self.assertEqual(resolve_group_members(None), set())

    def test_resolves_live_group_to_member_user_ids(self):
        group = self._make_group()
        self._add_member(group, self.user)
        self._add_member(group, self.user2)
        # Mirrors production: the extractor hands group ids as strings.
        result = resolve_group_members({str(group.group_id)})
        self.assertEqual(result, {str(self.user.id), str(self.user2.id)})
        # ids are returned as strings.
        self.assertTrue(all(isinstance(u, str) for u in result))

    def test_soft_deleted_group_drops_out(self):
        # is_deleted=True -> no live group -> empty set (members ignored).
        group = self._make_group(name="gone", is_deleted=True)
        self._add_member(group, self.user)
        self.assertEqual(resolve_group_members({str(group.group_id)}), set())

    def test_unknown_group_id_returns_empty(self):
        self.assertEqual(resolve_group_members({"99999999"}), set())

    def test_mixes_live_and_deleted_groups(self):
        live = self._make_group(name="live")
        dead = self._make_group(name="dead", is_deleted=True)
        self._add_member(live, self.user)
        self._add_member(dead, self.user2)
        result = resolve_group_members({str(live.group_id), str(dead.group_id)})
        # Only the live group's member survives.
        self.assertEqual(result, {str(self.user.id)})
