"""The sender of an inbox item is who sent it.

Every create path in `inbox_views` read `request.data["sender_id"]`
verbatim, so any authenticated user could post an item claiming to be
from anyone, to anyone, in any team.

That is not a disclosure bug. Inbox items render as *"<name> wants to
join your project"* with an **Approve** button, so a forged sender is a
phishing message delivered inside the product, wearing a colleague's
name, in the surface people trust precisely because it is inside the
product. Approving it then performs a real membership write.

`receiver` stays caller-supplied — addressing a message to somebody is
the point. `team` stays caller-supplied too, but is now checked, because
addressing one into a team you have nothing to do with is not.

The team-JOIN request is the deliberate exception: asking to join a team
you are not in is exactly what that endpoint is for.
"""

from django.contrib.auth import get_user_model

from origin.models.common.inbox_models import InboxItems
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

INBOX = "/api/v2/inbox/"
JOIN_TEAM_REQ = "/api/v2/inbox/joinTeamRequest/"
JOIN_PRJ_REQ = "/api/v2/inbox/joinProjectRequest/"


class SenderCannotBeForgedTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.victim = User.objects.create_user(
            username="isfvictim", email="isfvictim@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.victim)

    def test_the_sender_is_the_authenticated_user(self):
        """`self.user2` claims to be `self.user`."""
        self.authenticate(self.user2)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user.id),
                "receiver_id": str(self.victim.id),
                "item_body": "trust me",
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        item = InboxItems.objects.get(receiver=self.victim)
        self.assertEqual(item.sender_id, self.user2.id)
        self.assertNotEqual(item.sender_id, self.user.id)

    def test_a_join_project_request_cannot_be_forged_either(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Forge", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        self.authenticate(self.user2)
        res = self.client.post(
            JOIN_PRJ_REQ,
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.victim.id),
                "receiver_id": str(self.user.id),
                "item_body": "let me in",
                "item_type": 2,
                "item_optionals": {
                    "project_id": project.project_id,
                    "project_name": "Forge",
                },
            },
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))
        item = InboxItems.objects.filter(item_type=2).first()
        self.assertIsNotNone(item)
        self.assertEqual(item.sender_id, self.user2.id)


class TeamScopeOnCreateTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="isfout", email="isfout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="ISF Outsider", team_email="isfout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)

    def test_an_outsider_cannot_post_into_a_foreign_team(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.outsider.id),
                "receiver_id": str(self.user.id),
                "item_body": "hello",
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertEqual(InboxItems.objects.filter(receiver=self.user).count(), 0)

    def test_a_member_can_still_post(self):
        self.authenticate(self.user2)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.user2.id),
                "receiver_id": str(self.user.id),
                "item_body": "hello",
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)

    def test_asking_to_join_a_team_you_are_not_in_still_works(self):
        """The deliberate exception — that request is the whole point of
        the endpoint, so it must NOT require membership."""
        self.authenticate(self.outsider)
        res = self.client.post(
            JOIN_TEAM_REQ,
            {
                "team_id": str(self.team.team_id),
                "sender_id": str(self.outsider.id),
                "item_body": "may I join",
                "item_type": 1,
                "item_optionals": {},
            },
            format="json",
        )
        self.assertIn(res.status_code, (200, 201))
        item = InboxItems.objects.filter(item_type=1).first()
        self.assertIsNotNone(item)
        # ...and even here the sender is still the caller, not a claim.
        self.assertEqual(item.sender_id, self.outsider.id)


class BatchAndProjectScopingTests(BaseAPITestCase):
    """The remaining `medium` rows that are really cross-tenant reads.

    All four take a client-supplied id (or list of ids) and were filtered
    on nothing else — the same walkable-id shape as
    `TaskDependencyBatchListView`, which was rated critical and closed in
    #255. The batch endpoints NARROW rather than refuse: one stray id
    should not blank a whole chart.
    """

    def setUp(self):
        super().setUp()
        from origin.models.task.task_models import TaskMaster

        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Batch", owner=self.user2
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.hidden = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Hidden",
            status="Open",
            reporter=self.user2,
        )
        # `self.user` is in the team, not in that project.

    def test_burndown_excludes_tasks_i_cannot_see(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/task/burndown/",
            {"task_ids": str(self.hidden.task_id), "start": "2026-01-01", "end": "2026-12-31"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["total"], 0)

    def test_velocity_excludes_tasks_i_cannot_see(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/task/velocity/",
            {
                "team_id": str(self.team.team_id),
                "task_ids": str(self.hidden.task_id),
                "start": "2026-01-01",
                "end": "2026-12-31",
            },
        )
        self.assertEqual(res.status_code, 200)

    def test_project_tasks_refuse_a_project_i_am_not_in(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/task/getProjectTasks/",
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_comment_mentions_refuse_a_task_i_cannot_see(self):
        self.authenticate(self.user)
        res = self.client.get(
            "/api/v2/task/comment/mention/",
            {
                "team_id": str(self.team.team_id),
                "task_id": self.hidden.task_id,
                "comment_id": 1,
            },
        )
        self.assertEqual(res.status_code, 404)

    def test_github_branches_refuse_a_task_i_cannot_see(self):
        self.authenticate(self.user)
        res = self.client.get("/api/v2/github/branches/for-task/", {"task_id": self.hidden.task_id})
        self.assertEqual(res.status_code, 404)
