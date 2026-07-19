"""Project-scoped custom task/milestone body templates.

Covers the `ProjectTaskTemplateView` CRUD contract: any project member
manages shared templates, non-members are refused, names are unique per
project, and — the load-bearing invariant — editing or deleting a
template never mutates tasks that were created from it (a template body
is copied into the task at creation, not referenced).
"""

from django.urls import reverse
from rest_framework import status

from origin.models.project.prj_models import (
    ProjectMaster,
    ProjectMembers,
    ProjectTaskTemplate,
)
from origin.models.task.task_models import TaskMaster
from origin.tests.test_base import BaseAPITestCase

BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "hi", "styles": {}}]}]
OTHER_BODY = [{"type": "heading", "content": [{"type": "text", "text": "yo", "styles": {}}]}]


class ProjectTaskTemplateTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Template Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # self.user is a member; self.user2 (a team member) is deliberately
        # NOT added to the project — used to exercise the 403 path.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.authenticate()

    def create_template(self, name="Design doc", body=None, project=None):
        return self.client.post(
            reverse("project_task_template"),
            {
                "team_id": str(self.team.team_id),
                "project_id": (project or self.project).project_id,
                "template_name": name,
                "body": body if body is not None else BODY,
            },
            format="json",
        )

    def list_templates(self, project=None):
        pid = (project or self.project).project_id
        query = f"team_id={self.team.team_id}&project_id={pid}"
        return self.client.get(f"{reverse('project_task_template')}?{query}")


class CrudTests(ProjectTaskTemplateTestBase):
    def test_create_list_update_delete_round_trip(self):
        created = self.create_template()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)
        template_id = created.data["id"]
        self.assertEqual(created.data["templateName"], "Design doc")
        self.assertEqual(created.data["body"], BODY)
        self.assertEqual(created.data["createdBy"], self.user.id)

        listed = self.list_templates()
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertEqual([t["id"] for t in listed.data], [template_id])

        updated = self.client.put(
            reverse("project_task_template"),
            {
                "id": template_id,
                "project_id": self.project.project_id,
                "template_name": "Design doc v2",
                "body": OTHER_BODY,
            },
            format="json",
        )
        self.assertEqual(updated.status_code, status.HTTP_200_OK)
        self.assertEqual(updated.data["templateName"], "Design doc v2")
        self.assertEqual(updated.data["body"], OTHER_BODY)

        deleted = self.client.delete(
            reverse("project_task_template"),
            {"id": template_id, "project_id": self.project.project_id},
            format="json",
        )
        self.assertEqual(deleted.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProjectTaskTemplate.objects.filter(id=template_id).exists())

    def test_duplicate_name_in_same_project_is_rejected(self):
        self.assertEqual(self.create_template().status_code, status.HTTP_201_CREATED)
        dupe = self.create_template()
        self.assertEqual(dupe.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rename_onto_existing_name_is_rejected(self):
        self.create_template(name="A")
        second = self.create_template(name="B").data["id"]
        resp = self.client.put(
            reverse("project_task_template"),
            {"id": second, "project_id": self.project.project_id, "template_name": "A"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_requires_name_and_body(self):
        resp = self.client.post(
            reverse("project_task_template"),
            {"team_id": str(self.team.team_id), "project_id": self.project.project_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class MembershipTests(ProjectTaskTemplateTestBase):
    def test_non_member_cannot_create(self):
        self.authenticate(self.user2)
        self.assertEqual(self.create_template().status_code, status.HTTP_403_FORBIDDEN)

    def test_non_member_cannot_list(self):
        self.authenticate(self.user2)
        self.assertEqual(self.list_templates().status_code, status.HTTP_403_FORBIDDEN)

    def test_non_member_cannot_delete(self):
        template_id = self.create_template().data["id"]
        self.authenticate(self.user2)
        resp = self.client.delete(
            reverse("project_task_template"),
            {"id": template_id, "project_id": self.project.project_id},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(ProjectTaskTemplate.objects.filter(id=template_id).exists())


class TaskIsolationTests(ProjectTaskTemplateTestBase):
    def make_task_from_template_body(self):
        # A task whose body was copied from a template at creation. Its
        # content must survive any later edit/delete of that template.
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            assignee=self.user,
            reporter=self.user,
            title="Seeded from template",
            status="Open",
            content=BODY,
        )

    def test_editing_a_template_does_not_touch_existing_tasks(self):
        template_id = self.create_template().data["id"]
        task = self.make_task_from_template_body()

        self.client.put(
            reverse("project_task_template"),
            {
                "id": template_id,
                "project_id": self.project.project_id,
                "template_name": "Renamed",
                "body": OTHER_BODY,
            },
            format="json",
        )
        task.refresh_from_db()
        self.assertEqual(task.content, BODY)

    def test_deleting_a_template_does_not_touch_existing_tasks(self):
        template_id = self.create_template().data["id"]
        task = self.make_task_from_template_body()

        self.client.delete(
            reverse("project_task_template"),
            {"id": template_id, "project_id": self.project.project_id},
            format="json",
        )
        task.refresh_from_db()
        self.assertEqual(task.content, BODY)


class CrossProjectIsolationTests(ProjectTaskTemplateTestBase):
    def test_templates_are_scoped_to_their_project(self):
        other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=other_project, attendee=self.user)

        self.create_template(name="In A")
        self.create_template(name="In B", project=other_project)

        names_a = [t["templateName"] for t in self.list_templates().data]
        names_b = [t["templateName"] for t in self.list_templates(project=other_project).data]
        self.assertEqual(names_a, ["In A"])
        self.assertEqual(names_b, ["In B"])
