"""Endpoints that took the caller's identity from the request.

A family of read endpoints derived "who is asking" from a `user_id` /
`attendee_id` query parameter instead of `request.user`, or checked no
membership at all. Naming somebody else's id returned their data; naming
any team's id returned its roster, emails included.

Two distinct shapes, fixed two different ways:

  * `user_id` IS the caller (`getMyTeams`, `inbox`, `getMyAssignedTasks`)
    — the query now keys on `request.user`, and a parameter that
    disagrees is refused rather than quietly ignored.
  * `user_id` is a TARGET and the caller was simply never checked
    (`getTeamMembers`, `getTeamMemberInfo`, `project/members`) — a
    membership guard was added.

Every test in `CallerIdentityIDORTests` fails on the pre-fix code.
"""

from django.contrib.auth import get_user_model

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class CallerIdentityIDORTests(BaseAPITestCase):
    """`self.user` + `self.user2` share `self.team`. `self.outsider` is in
    a different team entirely and must see none of it."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="idorout", email="idorout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="IDOR Outsider Team",
            team_email="idorout@example.com",
            owner=self.outsider,
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="IDOR Project", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        InboxItems.objects.create(
            team=self.team, receiver=self.user, sender=self.user2, item_type=1, item_body="secret"
        )
        TaskMaster.objects.create(
            team=self.team, project=self.project, title="Assigned", assignee=self.user
        )

    # ── `user_id` is the caller ───────────────────────────────────────

    def test_get_my_teams_cannot_name_another_user(self):
        """Returned the victim's teams AND every member's email."""
        self.authenticate(self.outsider)
        res = self.client.get("/api/v2/team/getMyTeams/", {"user_id": str(self.user.id)})
        self.assertEqual(res.status_code, 403)

    def test_get_my_teams_returns_only_the_callers_teams(self):
        self.authenticate(self.outsider)
        res = self.client.get("/api/v2/team/getMyTeams/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual({str(t["teamId"]) for t in res.data}, {str(self.outsider_team.team_id)})

    def test_inbox_cannot_name_another_user(self):
        self.authenticate(self.user2)
        res = self.client.get(
            "/api/v2/inbox/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 403)

    def test_inbox_returns_only_the_callers_items(self):
        """user2 is a legitimate teammate but must not see user's inbox."""
        self.authenticate(self.user2)
        res = self.client.get("/api/v2/inbox/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["data"]["items"], [])

    def test_assigned_tasks_cannot_name_another_user(self):
        self.authenticate(self.user2)
        res = self.client.get(
            "/api/v2/task/getMyAssignedTasks/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 403)

    # ── the caller was never checked ──────────────────────────────────

    def test_team_roster_denied_to_a_non_member(self):
        """Full roster incl. every member's email, to anyone with the id."""
        self.authenticate(self.outsider)
        res = self.client.get(
            "/api/v2/team/getTeamMembers/",
            {"team_id": str(self.team.team_id), "user_id": str(self.outsider.id)},
        )
        self.assertEqual(res.status_code, 404)

    def test_team_roster_allowed_for_a_member(self):
        self.authenticate(self.user2)
        res = self.client.get("/api/v2/team/getTeamMembers/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        emails = {m["userEmail"] for m in res.data["data"]["members"]}
        self.assertIn(self.user.email, emails)

    def test_member_info_denied_to_a_non_member(self):
        self.authenticate(self.outsider)
        res = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 404)

    def test_member_info_allowed_for_a_teammate(self):
        self.authenticate(self.user2)
        res = self.client.get(
            "/api/v2/team/getTeamMemberInfo/",
            {"team_id": str(self.team.team_id), "user_id": str(self.user.id)},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["userEmail"], self.user.email)

    def test_project_roster_denied_to_a_non_member(self):
        self.authenticate(self.outsider)
        res = self.client.get(
            "/api/v2/project/members/",
            {"project_id": self.project.project_id, "user_id": str(self.outsider.id)},
        )
        self.assertEqual(res.status_code, 404)

    def test_project_roster_denied_to_a_user_with_no_projects(self):
        """The old guard short-circuited on `len(...) == 0`, so a caller
        with no memberships at all skipped the check entirely."""
        self.assertFalse(ProjectMembers.objects.filter(attendee=self.user2).exists())
        self.authenticate(self.user2)
        res = self.client.get("/api/v2/project/members/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 404)

    def test_project_roster_allowed_for_a_member(self):
        self.authenticate(self.user)
        res = self.client.get("/api/v2/project/members/", {"project_id": self.project.project_id})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["project_members"]), 1)


class MyTeamsCacheIsolationTests(BaseAPITestCase):
    """The 60s roster cache was keyed on the caller-supplied `user_id`.
    Fixing only the query would still have let a poisoned key serve one
    user's roster to another."""

    def test_cache_key_follows_the_authenticated_user(self):
        from django.core.cache import cache

        cache.clear()
        self.authenticate(self.user)
        first = self.client.get("/api/v2/team/getMyTeams/")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(len(first.data), 1)

        loner = User.objects.create_user(
            username="cacheloner", email="cacheloner@example.com", password="pw"
        )
        self.authenticate(loner)
        second = self.client.get("/api/v2/team/getMyTeams/")
        self.assertEqual(second.status_code, 200)
        # Would be self.user's cached payload if the key were shared.
        self.assertEqual(second.data, [])
