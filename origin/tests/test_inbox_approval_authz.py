"""Answering an inbox request needs a real request (MIP-940).

An approval adds a person to a team or a project. It used to trust the item
that authorized it: the lookup was `receiver=request.user` and nothing else, and
`POST /inbox/` let the caller choose the receiver and the type. So anyone could
address a "request" to themselves and approve it, into any project in their own
team, or into another team given its id.

These tests drive the real endpoints, in the order the app does: the requester
asks through the request endpoint, the owner approves through `fromInbox/`.
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
APPROVE_PRJ = "/api/v2/project/join/fromInbox/"
APPROVE_TEAM = "/api/v2/team/join/fromInbox/"

PENDING = "pending"


class ApprovalBase(BaseAPITestCase):
    """`self.user` owns the team and a project; `self.user2` is an ordinary
    member (the attacker). A second team belongs to someone else entirely."""

    def setUp(self):
        super().setUp()
        self.attacker = self.user2
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Own team private",
            code="OWN",
            owner=self.user,
            project_system_user=self.user,
            is_private=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

        self.victim = User.objects.create_user(
            username="victim", email="victim@example.com", password="pw-victim-123"
        )
        self.victim_team = TeamMaster.objects.create(
            team_name="Victim Team", team_email="victim@team.example.com", owner=self.victim
        )
        TeamMembers.objects.create(team=self.victim_team, attendee=self.victim)
        self.victim_project = ProjectMaster.objects.create(
            team=self.victim_team,
            project_name="Victim secret",
            code="VIC",
            owner=self.victim,
            project_system_user=self.victim,
            is_private=True,
        )
        ProjectMembers.objects.create(
            team=self.victim_team, project=self.victim_project, attendee=self.victim
        )

    # -- helpers ---------------------------------------------------------

    def forge(self, *, item_type, team, optionals=None, sender=None, receiver=None, **extra):
        """A request row written straight to the database, for the cases the
        API now refuses to create: what the approval must cope with if one
        ever exists."""
        sender = sender or self.attacker
        return InboxItems.objects.create(
            team=team,
            sender=sender,
            receiver=receiver or sender,
            item_body="please",
            item_type=item_type,
            item_optionals=optionals,
            **extra,
        )

    def is_project_member(self, project, user):
        return ProjectMembers.objects.filter(project=project, attendee=user).exists()

    def is_team_member(self, team, user):
        return TeamMembers.objects.filter(team=team, attendee=user, is_deleted=False).exists()

    def request_to_join_project(self, project, *, as_user=None):
        """The requester's side of the real flow."""
        self.authenticate(as_user or self.attacker)
        res = self.client.post(
            JOIN_PRJ_REQ,
            {
                "team_id": str(project.team_id),
                "item_body": "let me in",
                "item_type": 2,
                "item_optionals": {
                    "project_id": project.project_id,
                    "project_name": project.project_name,
                },
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.data["data"]["itemId"]

    def approve_project(self, item_id, *, as_user, team=None):
        self.authenticate(as_user)
        return self.client.post(
            APPROVE_PRJ,
            {"team_id": str((team or self.team).team_id), "item_id": item_id},
            format="json",
        )

    def approve_team(self, item_id, *, as_user, team):
        self.authenticate(as_user)
        return self.client.post(
            APPROVE_TEAM,
            {"team_id": str(team.team_id), "team_name": team.team_name, "item_id": item_id},
            format="json",
        )


class NoticeEndpointOnlyMakesNoticesTests(ApprovalBase):
    """`POST /inbox/` is for notices. Requests have their own endpoints."""

    def test_a_request_cannot_be_created_through_the_notice_endpoint(self):
        self.authenticate(self.attacker)
        for item_type in (1, 2, 3, 4, 5, 6, 7, 8, 9):
            with self.subTest(item_type=item_type):
                res = self.client.post(
                    INBOX,
                    {
                        "team_id": str(self.team.team_id),
                        "receiver_id": str(self.attacker.id),
                        "item_body": "approve me",
                        "item_type": item_type,
                        "item_optionals": {"project_id": self.project.project_id},
                    },
                    format="json",
                )
                self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(InboxItems.objects.exists())

    def test_the_item_type_may_not_be_left_out(self):
        self.authenticate(self.attacker)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "receiver_id": str(self.attacker.id),
                "item_body": "no type",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 400, res.content)
        self.assertFalse(InboxItems.objects.exists())

    def test_a_notice_is_still_created(self):
        """What the app and sockets send: item_type 0, to anyone."""
        self.authenticate(self.attacker)
        res = self.client.post(
            INBOX,
            {
                "team_id": str(self.team.team_id),
                "receiver_id": str(self.attacker.id),
                "item_body": "hello",
                "item_type": 0,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)

    def test_the_request_endpoints_decide_the_type_themselves(self):
        self.authenticate(self.attacker)
        res = self.client.post(
            JOIN_PRJ_REQ,
            {
                "team_id": str(self.team.team_id),
                "item_body": "x",
                "item_type": 1,
                "item_optionals": {"project_id": self.project.project_id},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(InboxItems.objects.get().item_type, 2)

    def test_asking_to_join_something_that_does_not_exist_is_a_404(self):
        self.authenticate(self.attacker)
        missing_project = self.client.post(
            JOIN_PRJ_REQ,
            {
                "team_id": str(self.team.team_id),
                "item_body": "x",
                "item_type": 2,
                "item_optionals": {"project_id": 999999},
            },
            format="json",
        )
        self.assertEqual(missing_project.status_code, 404, missing_project.content)
        missing_team = self.client.post(
            JOIN_TEAM_REQ,
            {
                "team_id": "00000000-0000-0000-0000-000000000000",
                "item_body": "x",
                "item_type": 1,
                "item_optionals": {},
            },
            format="json",
        )
        self.assertEqual(missing_team.status_code, 404, missing_team.content)


class ProjectApprovalTests(ApprovalBase):
    def test_a_request_addressed_to_yourself_does_not_get_you_in(self):
        item = self.forge(
            item_type=2,
            team=self.team,
            optionals={"project_id": self.project.project_id, "project_name": "x"},
        )
        res = self.approve_project(item.item_id, as_user=self.attacker)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_a_project_in_another_team_cannot_be_reached(self):
        """The item's team is the caller's, the project is the victim's."""
        item = self.forge(
            item_type=2,
            team=self.team,
            optionals={"project_id": self.victim_project.project_id, "project_name": "x"},
        )
        res = self.approve_project(item.item_id, as_user=self.attacker)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.victim_project, self.attacker))

    def test_claiming_the_victims_team_id_does_not_help(self):
        item = self.forge(
            item_type=2,
            team=self.team,
            optionals={"project_id": self.victim_project.project_id, "project_name": "x"},
        )
        res = self.approve_project(item.item_id, as_user=self.attacker, team=self.victim_team)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.victim_project, self.attacker))

    def test_an_item_filed_under_the_victims_team_does_not_help_either(self):
        item = self.forge(
            item_type=2,
            team=self.victim_team,
            optionals={"project_id": self.victim_project.project_id, "project_name": "x"},
        )
        res = self.approve_project(item.item_id, as_user=self.attacker, team=self.victim_team)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.victim_project, self.attacker))

    def test_only_a_notice_is_not_a_request(self):
        item = self.forge(
            item_type=0,
            team=self.team,
            sender=self.attacker,
            receiver=self.user,
            optionals={"project_id": self.project.project_id, "project_name": "x"},
        )
        res = self.approve_project(item.item_id, as_user=self.user)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_someone_who_cannot_manage_the_project_cannot_approve(self):
        """A request that reached a plain member is not theirs to answer."""
        item = self.forge(
            item_type=2,
            team=self.team,
            sender=self.victim,
            receiver=self.attacker,
            optionals={"project_id": self.project.project_id, "project_name": "x"},
        )
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.victim)
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.attacker, member_role="viewer"
        )
        res = self.approve_project(item.item_id, as_user=self.attacker)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.project, self.victim))

    def test_the_owner_approves_a_real_request(self):
        item_id = self.request_to_join_project(self.project)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

        res = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["projectName"], self.project.project_name)
        self.assertTrue(self.is_project_member(self.project, self.attacker))

    def test_approving_twice_is_harmless(self):
        item_id = self.request_to_join_project(self.project)
        self.approve_project(item_id, as_user=self.user)
        again = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(again.status_code, 201, again.content)
        self.assertEqual(
            ProjectMembers.objects.filter(project=self.project, attendee=self.attacker).count(), 1
        )

    def test_a_rejected_request_cannot_be_approved_later(self):
        item_id = self.request_to_join_project(self.project)
        InboxItems.objects.filter(item_id=item_id).update(request_status="rejected")
        res = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_an_old_approval_does_not_put_someone_back(self):
        """Approved once, the requester left, and the item is still in the inbox."""
        item_id = self.request_to_join_project(self.project)
        self.approve_project(item_id, as_user=self.user)
        InboxItems.objects.filter(item_id=item_id).update(request_status="approved")
        ProjectMembers.objects.filter(project=self.project, attendee=self.attacker).delete()

        res = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_a_deleted_request_cannot_be_approved(self):
        item_id = self.request_to_join_project(self.project)
        InboxItems.objects.filter(item_id=item_id).update(is_deleted=True)
        res = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_someone_else_cannot_approve_the_owners_request(self):
        item_id = self.request_to_join_project(self.project)
        res = self.approve_project(item_id, as_user=self.victim)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_a_requester_who_left_the_team_is_not_added(self):
        item_id = self.request_to_join_project(self.project)
        TeamMembers.objects.filter(team=self.team, attendee=self.attacker).update(is_deleted=True)
        res = self.approve_project(item_id, as_user=self.user)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(self.is_project_member(self.project, self.attacker))

    def test_a_missing_item_id_is_a_400_not_a_crash(self):
        self.authenticate(self.user)
        res = self.client.post(APPROVE_PRJ, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 400, res.content)


class TeamApprovalTests(ApprovalBase):
    def request_to_join_team(self, team, *, as_user):
        self.authenticate(as_user)
        res = self.client.post(
            JOIN_TEAM_REQ,
            {
                "team_id": str(team.team_id),
                "item_body": "let me in",
                "item_type": 1,
                "item_optionals": {"team_name": team.team_name},
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        return res.data["data"]["itemId"]

    def test_a_request_addressed_to_yourself_does_not_get_you_into_a_team(self):
        item = self.forge(item_type=1, team=self.victim_team, optionals={"team_name": "x"})
        res = self.approve_team(item.item_id, as_user=self.attacker, team=self.victim_team)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_team_member(self.victim_team, self.attacker))

    def test_an_item_filed_under_your_own_team_does_not_get_you_into_another(self):
        item = self.forge(item_type=1, team=self.team, optionals={"team_name": "x"})
        res = self.approve_team(item.item_id, as_user=self.attacker, team=self.victim_team)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_team_member(self.victim_team, self.attacker))

    def test_a_notice_is_not_a_request(self):
        item = self.forge(
            item_type=0,
            team=self.victim_team,
            sender=self.attacker,
            receiver=self.victim,
            optionals={"team_name": "x"},
        )
        res = self.approve_team(item.item_id, as_user=self.victim, team=self.victim_team)
        self.assertEqual(res.status_code, 404, res.content)
        self.assertFalse(self.is_team_member(self.victim_team, self.attacker))

    def test_the_owner_approves_a_real_request(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="pw-outsider-123"
        )
        item_id = self.request_to_join_team(self.victim_team, as_user=outsider)
        self.assertFalse(self.is_team_member(self.victim_team, outsider))

        res = self.approve_team(item_id, as_user=self.victim, team=self.victim_team)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data["teamName"], self.victim_team.team_name)
        self.assertTrue(self.is_team_member(self.victim_team, outsider))

    def test_a_rejected_request_cannot_be_approved_later(self):
        outsider = User.objects.create_user(
            username="outsider2", email="outsider2@example.com", password="pw-outsider-123"
        )
        item_id = self.request_to_join_team(self.victim_team, as_user=outsider)
        InboxItems.objects.filter(item_id=item_id).update(request_status="rejected")
        res = self.approve_team(item_id, as_user=self.victim, team=self.victim_team)
        self.assertEqual(res.status_code, 409, res.content)
        self.assertFalse(self.is_team_member(self.victim_team, outsider))

    def test_a_left_member_comes_back_once_and_only_while_pending(self):
        """The re-join path: a soft-deleted membership is revived by approval."""
        outsider = User.objects.create_user(
            username="outsider3", email="outsider3@example.com", password="pw-outsider-123"
        )
        TeamMembers.objects.create(team=self.victim_team, attendee=outsider, is_deleted=True)
        item_id = self.request_to_join_team(self.victim_team, as_user=outsider)
        res = self.approve_team(item_id, as_user=self.victim, team=self.victim_team)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(self.is_team_member(self.victim_team, outsider))
