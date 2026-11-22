"""A hidden project's name and code cannot be probed (MIP-1935 part 2)."""

from __future__ import annotations

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.project_manage import ProjectRefused, create_project, set_project_name
from origin.tests.test_base import BaseAPITestCase

PROJECT = "/api/v2/project/"
EXISTS = "/api/v2/project/exist/"


class DuplicateNameTests(BaseAPITestCase):
    """self.user2 is a team member outside the hidden project; self.user owns it."""

    def setUp(self):
        super().setUp()
        self.hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Codename Falcon",
            code="FAL",
            owner=self.user,
            project_system_user=self.user,
            is_private=True,
            is_hidden=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.hidden, attendee=self.user)
        self.visible = ProjectMaster.objects.create(
            team=self.team, project_name="Open", code="OPN", owner=self.user
        )
        self.as_outsider()

    def as_outsider(self):
        self.client.force_authenticate(self.user2)

    def create(self, **body):
        body = {"team": str(self.team.team_id), "owner": self.user2.id, **body}
        return self.client.post(PROJECT, body, format="json")

    def test_outsider_can_reuse_a_hidden_name_and_code_without_an_error(self):
        res = self.create(project_name="Codename Falcon", code="FAL")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertNotEqual(res.data["code"], "FAL")
        self.assertEqual(res.data["project_name"], "Codename Falcon")

    def test_display_ids_stay_unique(self):
        for _ in range(2):
            self.create(project_name=f"x{_}", code="FAL")
        codes = list(ProjectMaster.objects.filter(team=self.team).values_list("code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))

    def test_visible_duplicate_name_still_refused(self):
        res = self.create(project_name="Open")
        self.assertEqual(res.status_code, 400)
        self.assertIn("hint", res.data)

    def test_visible_duplicate_code_still_refused(self):
        res = self.create(project_name="Fresh", code="OPN")
        self.assertEqual(res.status_code, 400)

    def test_member_of_the_hidden_project_is_refused_its_name(self):
        self.client.force_authenticate(self.user)
        res = self.create(project_name="Codename Falcon", owner=self.user.id)
        self.assertEqual(res.status_code, 400)

    def test_exists_is_false_for_a_hidden_name_and_true_for_a_visible_one(self):
        q = {"team_id": str(self.team.team_id)}
        res = self.client.get(EXISTS, {**q, "project_name": "Codename Falcon"})
        self.assertFalse(res.data["project_exists"])
        res = self.client.get(EXISTS, {**q, "project_name": "Open"})
        self.assertTrue(res.data["project_exists"])
        self.client.force_authenticate(self.user)
        res = self.client.get(EXISTS, {**q, "project_name": "Codename Falcon"})
        self.assertTrue(res.data["project_exists"])

    def test_service_create_and_rename(self):
        mine = create_project(team=self.team, owner=self.user2, name="Codename Falcon")
        self.assertNotEqual(mine.code, "FAL")
        with self.assertRaises(ProjectRefused):  # now visible to the caller: refused
            create_project(team=self.team, owner=self.user2, name="Codename Falcon")
        set_project_name(mine, "Mine2", self.user2.id)
        with self.assertRaises(ProjectRefused):
            create_project(team=self.team, owner=self.user2, name="Open")
        other = create_project(team=self.team, owner=self.user2, name="Other")
        set_project_name(other, "Codename Falcon", self.user2.id)
        with self.assertRaises(ProjectRefused):
            set_project_name(other, "Open", self.user2.id)

    def test_rename_via_chat_title_and_code_via_settings(self):
        mine = create_project(team=self.team, owner=self.user2, name="Mine")
        from origin.models.chat.unified_models import Channel

        ch = Channel.objects.filter(project_id=mine.project_id).first()
        res = self.client.patch(
            f"/api/v3/channels/{ch.id}/", {"title": "Codename Falcon"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        res = self.client.patch(f"/api/v3/channels/{ch.id}/", {"title": "Open"}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_a_trailing_space_does_not_get_past_the_visible_check(self):
        res = self.create(project_name="Open ")
        self.assertEqual(res.status_code, 400)
        res = self.client.get(EXISTS, {"team_id": str(self.team.team_id), "project_name": "Open "})
        self.assertTrue(res.data["project_exists"])
