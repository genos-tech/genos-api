"""A milestone only takes in people who could be put on it (MIP-915).

The UI's milestone POST and PATCH, and the per-person assignee endpoint, took
any user id for `assignee_ids`, `collaborators` and `reporter_id`. A milestone
assignee is a reader (`milestone_acl_user_ids`, `LabelAccess.milestone_readable`),
and the collaborators and the reporter ride on the backing task, where they are
the same grants as on any task, so a project member could attach an outsider and
widen who can read the milestone. Same gap as the task PUT (MIP-911), same rule
(`may_name_person`: a member of the project or of its team), same care: only
people being ADDED are judged, because the app re-sends the whole lists on a
save and someone who has since left must not block it.
"""

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()


class MilestonePeopleBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="People",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.teammate = self.user2
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="guestpass123"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="outsiderpass123"
        )
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=self.outsider)
        self.authenticate()

    def create(self, **extra):
        return self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "M1", **extra},
            format="json",
        )

    def make(self):
        res = self.create()
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        return MilestoneMaster.objects.get(milestone_id=res.data["milestone"]["milestoneId"])

    def patch(self, milestone, **body):
        return self.client.patch(
            reverse("milestone_detail", args=[milestone.milestone_id]), body, format="json"
        )

    def assignees(self, milestone):
        return {a.user_id for a in MilestoneAssignees.objects.filter(milestone=milestone)}

    def collaborators(self, milestone):
        milestone.refresh_from_db()
        return {u.id for u in milestone.task.collaborators.all()}

    def assertRefused(self, res, label):
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn(label, res.data["error"])


class MilestoneCreateTests(MilestonePeopleBase):
    def test_an_outsider_cannot_be_named_on_a_create(self):
        for label, body in (
            ("assignee_ids", {"assignee_ids": [self.outsider.id]}),
            ("collaborators", {"collaborators": [self.outsider.id]}),
            ("reporter_id", {"reporter_id": self.outsider.id}),
        ):
            with self.subTest(label=label):
                self.assertRefused(self.create(**body), label)
        self.assertFalse(MilestoneMaster.objects.filter(title="M1").exists())

    def test_teammates_and_project_members_are_accepted(self):
        res = self.create(
            assignee_ids=[self.teammate.id, self.guest.id],
            collaborators=[self.guest.id],
            reporter_id=self.teammate.id,
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        milestone = MilestoneMaster.objects.get(milestone_id=res.data["milestone"]["milestoneId"])
        self.assertEqual(self.assignees(milestone), {self.teammate.id, self.guest.id})
        self.assertEqual(self.collaborators(milestone), {self.guest.id})
        self.assertEqual(milestone.reporter_id, self.teammate.id)

    def test_a_malformed_id_is_refused_not_dropped(self):
        self.assertRefused(self.create(assignee_ids=["not-an-id"]), "assignee_ids")

    def test_no_people_at_all_still_creates(self):
        self.assertEqual(self.create().status_code, status.HTTP_201_CREATED)


class MilestonePatchTests(MilestonePeopleBase):
    def test_an_outsider_cannot_be_added_and_nothing_else_in_the_patch_applies(self):
        milestone = self.make()
        for label, body in (
            ("assignee_ids", {"assignee_ids": [self.outsider.id]}),
            ("collaborators", {"collaborators": [self.outsider.id]}),
            ("reporter_id", {"reporter_id": self.outsider.id}),
        ):
            with self.subTest(label=label):
                self.assertRefused(self.patch(milestone, title="renamed", **body), label)
        milestone.refresh_from_db()
        self.assertEqual(milestone.title, "M1")
        self.assertEqual(self.assignees(milestone), set())
        self.assertEqual(self.collaborators(milestone), set())

    def test_teammates_and_project_members_can_be_added(self):
        milestone = self.make()
        res = self.patch(
            milestone,
            assignee_ids=[self.teammate.id, self.guest.id],
            collaborators=[self.guest.id],
            reporter_id=self.guest.id,
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.assignees(milestone), {self.teammate.id, self.guest.id})
        self.assertEqual(self.collaborators(milestone), {self.guest.id})

    def test_someone_already_on_it_who_left_does_not_block_a_save(self):
        milestone = self.make()
        MilestoneAssignees.objects.create(
            milestone=milestone, team=milestone.team, user=self.outsider
        )
        milestone.task.collaborators.add(self.outsider)
        MilestoneMaster.objects.filter(pk=milestone.pk).update(reporter=self.outsider)

        res = self.patch(
            milestone,
            title="renamed",
            assignee_ids=[self.outsider.id, self.teammate.id],
            collaborators=[self.outsider.id],
            reporter_id=self.outsider.id,
        )

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.assignees(milestone), {self.outsider.id, self.teammate.id})
        self.assertEqual(self.collaborators(milestone), {self.outsider.id})

    def test_a_person_who_left_can_still_be_removed(self):
        milestone = self.make()
        MilestoneAssignees.objects.create(
            milestone=milestone, team=milestone.team, user=self.outsider
        )
        res = self.patch(milestone, assignee_ids=[self.teammate.id])
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.assignees(milestone), {self.teammate.id})

    def test_a_new_outsider_is_still_refused_next_to_a_kept_one(self):
        milestone = self.make()
        MilestoneAssignees.objects.create(
            milestone=milestone, team=milestone.team, user=self.outsider
        )
        other = User.objects.create_user(
            username="other2", email="other2@example.com", password="otherpass123"
        )
        res = self.patch(milestone, assignee_ids=[self.outsider.id, other.id])
        self.assertRefused(res, "assignee_ids")
        self.assertEqual(self.assignees(milestone), {self.outsider.id})

    def test_absent_and_empty_lists_still_work(self):
        milestone = self.make()
        MilestoneAssignees.objects.create(
            milestone=milestone, team=milestone.team, user=self.outsider
        )
        self.assertEqual(self.patch(milestone, title="a").status_code, status.HTTP_200_OK)
        self.assertEqual(self.assignees(milestone), {self.outsider.id})
        self.assertEqual(self.patch(milestone, assignee_ids=[]).status_code, status.HTTP_200_OK)
        self.assertEqual(self.assignees(milestone), set())

    def test_a_project_move_judges_people_against_the_destination(self):
        milestone = self.make()
        destination = ProjectMaster.objects.create(
            team=TeamMaster.objects.get(owner=self.outsider),
            project_name="Dest",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=destination.team, project=destination, attendee=self.user
        )

        refused = self.patch(
            milestone, project_id=destination.project_id, assignee_ids=[self.teammate.id]
        )
        self.assertRefused(refused, "assignee_ids")
        milestone.refresh_from_db()
        self.assertEqual(milestone.project_id, self.project.project_id)

        allowed = self.patch(
            milestone, project_id=destination.project_id, assignee_ids=[self.outsider.id]
        )
        self.assertEqual(allowed.status_code, status.HTTP_200_OK, allowed.data)


class MilestoneAssigneeEndpointTests(MilestonePeopleBase):
    def add(self, milestone, user):
        return self.client.post(
            reverse("milestone_assignees_add", args=[milestone.milestone_id]),
            {"user_id": str(user.id)},
            format="json",
        )

    def test_an_outsider_cannot_be_made_an_assignee(self):
        milestone = self.make()
        self.assertEqual(self.add(milestone, self.outsider).status_code, 400)
        self.assertEqual(self.assignees(milestone), set())

    def test_a_teammate_and_a_project_member_can(self):
        milestone = self.make()
        for person in (self.teammate, self.guest):
            self.assertEqual(self.add(milestone, person).status_code, 200)
        self.assertEqual(self.assignees(milestone), {self.teammate.id, self.guest.id})

    def test_adding_someone_already_assigned_is_still_fine(self):
        milestone = self.make()
        MilestoneAssignees.objects.create(
            milestone=milestone, team=milestone.team, user=self.outsider
        )
        self.assertEqual(self.add(milestone, self.outsider).status_code, 200)
        self.assertEqual(self.assignees(milestone), {self.outsider.id})
