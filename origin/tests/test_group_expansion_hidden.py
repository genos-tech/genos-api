"""group_expansion must not list a hidden group's members to a non-member (MIP-1935)."""

from __future__ import annotations

from origin.models.chat.unified_models import ChannelMember
from origin.models.project.prj_models import ProjectMembers
from origin.services.group_expansion import (
    GROUP_GM,
    GROUP_PROJECT,
    expand_groups,
    resolve_group_user_ids,
)
from origin.tests.test_hidden_mode import HiddenBase


class HiddenGroupExpansionTests(HiddenBase):
    def setUp(self):
        super().setUp()
        self.me = str(self.user.id)
        self.outsider = str(self.user2.id)

    def _ids(self, group_type, group_id, viewer):
        return resolve_group_user_ids(self.team.team_id, group_type, group_id, viewer_id=viewer)

    def test_a_non_member_gets_the_answer_for_a_missing_hidden_project(self):
        missing = self._ids(GROUP_PROJECT, 99999999, self.outsider)
        self.assertEqual(missing, set())
        self.assertEqual(self._ids(GROUP_PROJECT, self.project.project_id, self.outsider), missing)

    def test_a_member_still_gets_the_hidden_project_roster(self):
        self.assertEqual(self._ids(GROUP_PROJECT, self.project.project_id, self.me), {self.me})

    def test_the_owner_without_a_member_row_still_expands_it(self):
        ProjectMembers.objects.filter(project=self.project).delete()
        self.assertEqual(self._ids(GROUP_PROJECT, self.project.project_id, self.me), set())
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.assertEqual(
            self._ids(GROUP_PROJECT, self.project.project_id, self.me), {self.outsider}
        )

    def test_a_non_member_gets_nothing_for_a_hidden_group_chat(self):
        self.assertEqual(self._ids(GROUP_GM, str(self.gm.id), self.outsider), set())
        self.assertEqual(self._ids(GROUP_GM, str(self.gm.id), self.me), {self.me})

    def test_a_left_member_of_a_hidden_chat_no_longer_expands_it(self):
        ChannelMember.objects.filter(channel=self.gm, user=self.user).update(is_deleted=True)
        self.assertEqual(self._ids(GROUP_GM, str(self.gm.id), self.me), set())

    def test_a_private_but_not_hidden_group_is_unchanged(self):
        # Hidden hides the NAME and roster; plain private stays as before.
        ChannelMember.objects.create(channel=self.private_visible, user=self.user2, role="member")
        self.assertEqual(
            self._ids(GROUP_GM, str(self.private_visible.id), self.outsider),
            {self.me, self.outsider},
        )

    def test_expand_groups_passes_the_viewer_through(self):
        groups = [
            {"type": GROUP_PROJECT, "id": self.project.project_id},
            {"type": GROUP_GM, "id": str(self.gm.id)},
        ]
        self.assertEqual(expand_groups(self.team.team_id, groups, viewer_id=self.outsider), {})
        self.assertEqual(
            set(expand_groups(self.team.team_id, groups, viewer_id=self.me)), {self.me}
        )
