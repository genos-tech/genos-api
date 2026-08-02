"""Mention groups: the team's org chart, previously readable by anyone.

`MentionGroupMaster.group_id` is a sequential integer and every handler
in this file resolved one — or trusted a `team_id` — without asking
whether the caller belongs to the team. A mention group's whole purpose
is to name a set of colleagues, so its member list is an org chart:
`{group_id: [user_id, …]}` for any team, by counting.

`MentionGroupResolveView` is the worst of them because it takes a *list*
of ids and answers wholesale — one request enumerates several teams.

Guests are refused outright rather than narrowed. A mention group is a
team-wide directory construct, and handing an external collaborator the
org chart is precisely the enumeration the guest model exists to
prevent.
"""

from django.contrib.auth import get_user_model

from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import GUEST
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

GROUPS = "/api/v2/mention-group/"
MEMBERS = "/api/v2/mention-group/members/"
RESOLVE = "/api/v2/mention-group/resolve/"


class MentionGroupScopingTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.group = MentionGroupMaster.objects.create(
            team=self.team, group_name="engineering", created_by=self.user
        )
        MentionGroupMembers.objects.create(
            team=self.team, group=self.group, user=self.user, added_by=self.user
        )

        self.outsider = User.objects.create_user(
            username="mgout", email="mgout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="MG Outsider", team_email="mgout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    # ── reads ─────────────────────────────────────────────────────────

    def test_outsider_cannot_list_a_teams_groups(self):
        self.authenticate(self.outsider)
        res = self.client.get(GROUPS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_member_can_list(self):
        self.authenticate(self.user2)
        res = self.client.get(GROUPS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["mentionGroups"]), 1)

    def test_outsider_cannot_read_a_groups_members(self):
        self.authenticate(self.outsider)
        res = self.client.get(MEMBERS, {"group_id": self.group.group_id})
        self.assertEqual(res.status_code, 404)

    def test_bulk_resolve_leaks_no_members_for_a_foreign_group(self):
        """One request, several teams' org charts — the worst shape here.

        The key still appears with an EMPTY list, which is deliberate:
        that is exactly how this endpoint already reports a soft-deleted
        group, so a foreign group is now indistinguishable from one that
        no longer exists. No membership is disclosed either way.
        """
        self.authenticate(self.outsider)
        res = self.client.post(RESOLVE, {"group_ids": [self.group.group_id]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["resolved"].get(str(self.group.group_id), []), [])

    def test_bulk_resolve_still_works_for_a_member(self):
        self.authenticate(self.user)
        res = self.client.post(RESOLVE, {"group_ids": [self.group.group_id]}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["resolved"][str(self.group.group_id)], [str(self.user.id)])

    # ── writes ────────────────────────────────────────────────────────

    def test_outsider_cannot_create_a_group_in_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            GROUPS,
            {"team_id": str(self.team.team_id), "group_name": "planted"},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(MentionGroupMaster.objects.filter(group_name="planted").exists())

    def test_outsider_cannot_rename_a_group(self):
        self.authenticate(self.outsider)
        res = self.client.put(
            GROUPS, {"group_id": self.group.group_id, "group_name": "pwned"}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        self.group.refresh_from_db()
        self.assertEqual(self.group.group_name, "engineering")

    def test_outsider_cannot_delete_a_group(self):
        self.authenticate(self.outsider)
        res = self.client.delete(f"{GROUPS}?group_id={self.group.group_id}")
        self.assertEqual(res.status_code, 404)
        self.group.refresh_from_db()
        self.assertFalse(self.group.is_deleted)

    def test_outsider_cannot_add_members(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            MEMBERS,
            {"group_id": self.group.group_id, "user_ids": [str(self.outsider.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(MentionGroupMembers.objects.filter(group=self.group).count(), 1)

    def test_outsider_cannot_remove_members(self):
        self.authenticate(self.outsider)
        res = self.client.delete(f"{MEMBERS}?group_id={self.group.group_id}&user_id={self.user.id}")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(MentionGroupMembers.objects.filter(group=self.group).count(), 1)

    def test_member_can_still_manage(self):
        self.authenticate(self.user2)
        res = self.client.post(
            MEMBERS,
            {"group_id": self.group.group_id, "user_ids": [str(self.user2.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 201)


class GuestsAreRefusedTests(BaseAPITestCase):
    """A guest is in the team's projects, not its directory."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="MG Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.guest = User.objects.create_user(
            username="mgguest", email="mgguest@agency.example", password="pw"
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.guest, member_role=GUEST
        )
        self.group = MentionGroupMaster.objects.create(
            team=self.team, group_name="internal", created_by=self.user
        )

    def test_a_guest_cannot_list_groups(self):
        self.authenticate(self.guest)
        res = self.client.get(GROUPS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 404)

    def test_a_guest_cannot_read_a_groups_members(self):
        self.authenticate(self.guest)
        res = self.client.get(MEMBERS, {"group_id": self.group.group_id})
        self.assertEqual(res.status_code, 404)
