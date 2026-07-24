"""Per-project custom task fields.

Covers the `ProjectCustomFieldsView` contract (member-gated GET with
`canManage`, owner/editor-gated mutations, tag-option validation,
reorder), the value round-trip on the task endpoints
(`custom_field_values` on POST/PUT + `customFieldValues` on the reads),
the milestone path (values live on the BACKING task row and survive
`_sync_backing_task`), and the load-bearing no-cascade invariant:
deleting a field definition never touches task rows.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.project.prj_models import (
    ProjectCustomField,
    ProjectMaster,
    ProjectMembers,
)
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.services.custom_fields import sanitize_custom_field_values
from origin.tests.test_base import BaseAPITestCase

TAG_OPTIONS = [
    {"id": "opt-a", "label": "Alpha", "color": "#ff2323", "textColor": "white"},
    {"id": "opt-b", "label": "Beta", "color": "#0044c2", "textColor": "white"},
]


class CustomFieldsTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="CF Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # self.user is owner+member; self.user2 is a plain member whose
        # ProjectMembers row keeps the "viewer" default — exercises the
        # can_manage gate.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate()

    # -- endpoint helpers ------------------------------------------------

    def list_fields(self, project=None):
        pid = (project or self.project).project_id
        return self.client.get(f"{reverse('project_custom_fields')}?project_id={pid}")

    def create_field(self, name, field_type, options=None, project=None):
        payload = {
            "project_id": (project or self.project).project_id,
            "field_name": name,
            "field_type": field_type,
        }
        if options is not None:
            payload["options"] = options
        return self.client.post(reverse("project_custom_fields"), payload, format="json")

    def update_field(self, field_id, **kwargs):
        payload = {"project_id": self.project.project_id, "field_id": field_id, **kwargs}
        return self.client.put(reverse("project_custom_fields"), payload, format="json")

    def delete_field(self, field_id):
        return self.client.delete(
            reverse("project_custom_fields"),
            {"project_id": self.project.project_id, "field_id": field_id},
            format="json",
        )

    def create_task(self, **overrides):
        payload = {
            "team": self.team.team_id,
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "A task",
            "priority": "High",
            "effort_level": "Low",
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
            **overrides,
        }
        return self.client.post(reverse("task_create"), payload, format="json")


class DefinitionCrudTests(CustomFieldsTestBase):
    def test_list_starts_empty_with_can_manage_flag(self):
        resp = self.list_fields()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {"fields": [], "canManage": True})

    def test_owner_creates_all_four_types(self):
        for name, ftype in [
            ("Customer", "tag"),
            ("Notes", "text"),
            ("Review date", "date"),
            ("QA owner", "member"),
        ]:
            resp = self.create_field(name, ftype, TAG_OPTIONS if ftype == "tag" else None)
            self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
            self.assertEqual(resp.data["fieldName"], name)
            self.assertEqual(resp.data["fieldType"], ftype)

        listed = self.list_fields()
        self.assertEqual([f["fieldName"] for f in listed.data["fields"]],
                         ["Customer", "Notes", "Review date", "QA owner"])
        self.assertEqual(listed.data["fields"][0]["options"], TAG_OPTIONS)

    def test_viewer_member_reads_but_cannot_mutate(self):
        created = self.create_field("Customer", "tag", TAG_OPTIONS)
        field_id = created.data["fieldId"]

        self.authenticate(self.user2)
        listed = self.list_fields()
        self.assertEqual(listed.status_code, status.HTTP_200_OK)
        self.assertFalse(listed.data["canManage"])
        self.assertEqual(len(listed.data["fields"]), 1)

        self.assertEqual(
            self.create_field("Nope", "text").status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertEqual(
            self.update_field(field_id, field_name="Nope").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(self.delete_field(field_id).status_code, status.HTTP_403_FORBIDDEN)

    def test_editor_member_can_mutate(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).update(
            member_role="editor"
        )
        self.authenticate(self.user2)
        resp = self.create_field("Customer", "text")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertTrue(self.list_fields().data["canManage"])

    def test_non_member_cannot_read(self):
        lonely = ProjectMaster.objects.create(
            team=self.team,
            project_name="No Members Here",
            owner=self.user2,
            project_system_user=self.user2,
        )
        self.assertEqual(self.list_fields(project=lonely).status_code, status.HTTP_403_FORBIDDEN)

    def test_duplicate_name_and_bad_type_are_400(self):
        self.create_field("Customer", "text")
        self.assertEqual(
            self.create_field("Customer", "text").status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertEqual(
            self.create_field("Weird", "checkbox").status_code, status.HTTP_400_BAD_REQUEST
        )
        # Options belong to tag fields only.
        self.assertEqual(
            self.create_field("Texty", "text", TAG_OPTIONS).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_option_validation(self):
        dup_ids = [
            {"id": "opt-a", "label": "One", "color": "#fff"},
            {"id": "opt-a", "label": "Two", "color": "#000"},
        ]
        self.assertEqual(
            self.create_field("Customer", "tag", dup_ids).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.create_field("Customer", "tag", [{"label": "no id"}]).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_field_type_is_immutable(self):
        created = self.create_field("Customer", "text")
        resp = self.update_field(created.data["fieldId"], field_type="tag")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_rename_and_option_edit_do_not_touch_tasks(self):
        created = self.create_field("Customer", "tag", TAG_OPTIONS)
        field_id = created.data["fieldId"]
        task_resp = self.create_task(custom_field_values={str(field_id): ["opt-a"]})
        task_id = task_resp.data["task"]["task_id"]

        renamed = self.update_field(
            field_id,
            field_name="Client",
            options=[{"id": "opt-a", "label": "Renamed Alpha", "color": "#00ff00"}],
        )
        self.assertEqual(renamed.status_code, status.HTTP_200_OK)

        # The stored value still references the option ID — no rewrite.
        task = TaskMaster.objects.get(task_id=task_id)
        self.assertEqual(task.custom_field_values, {str(field_id): ["opt-a"]})

    def test_delete_leaves_task_values_orphaned(self):
        created = self.create_field("Customer", "text")
        field_id = created.data["fieldId"]
        task_resp = self.create_task(custom_field_values={str(field_id): "Acme"})
        task_id = task_resp.data["task"]["task_id"]

        self.assertEqual(self.delete_field(field_id).status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(ProjectCustomField.objects.filter(field_id=field_id).exists())
        # The task row is untouched; readers drop the orphaned key.
        task = TaskMaster.objects.get(task_id=task_id)
        self.assertEqual(task.custom_field_values, {str(field_id): "Acme"})

    def test_reorder(self):
        ids = [
            self.create_field(name, "text").data["fieldId"]
            for name in ["One", "Two", "Three"]
        ]
        resp = self.client.put(
            reverse("project_custom_fields"),
            {"project_id": self.project.project_id, "order": [ids[2], ids[0], ids[1]]},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [f["fieldId"] for f in resp.data["fields"]], [ids[2], ids[0], ids[1]]
        )


class TaskValueRoundTripTests(CustomFieldsTestBase):
    def setUp(self):
        super().setUp()
        self.tag_field = ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name="Customer",
            field_type="tag",
            options=TAG_OPTIONS,
        )
        self.text_field = ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name="Notes",
            field_type="text",
            sort_order=1,
        )

    def values(self):
        return {
            str(self.tag_field.field_id): ["opt-a", "opt-b"],
            str(self.text_field.field_id): "hello",
        }

    def test_create_with_values_and_read_back(self):
        resp = self.create_task(custom_field_values=self.values())
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        task_id = resp.data["task"]["task_id"]

        got = self.client.get(
            f"{reverse('get_task')}?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}&task_id={task_id}"
        )
        self.assertEqual(got.status_code, status.HTTP_200_OK)
        self.assertEqual(got.data[0]["customFieldValues"], self.values())

        rows = self.client.get(
            f"{reverse('get_project_tasks')}?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}"
        )
        row = next(
            r for r in rows.data["data"]["tasks"] if str(r["id"]) == str(task_id)
        )
        self.assertEqual(row["customFieldValues"], self.values())

        # Literal path: the URL name "get_team_tasks" is (pre-existing)
        # duplicated between GetTeamTasksView and GetTeamTasksByTagView,
        # and reverse() resolves to the LAST registration — the ByTag
        # view — which serves a different shape entirely.
        team_rows = self.client.get(f"/api/v2/task/getTeamTasks/?team_id={self.team.team_id}")
        team_row = next(r for r in team_rows.data if str(r["id"]) == str(task_id))
        self.assertEqual(team_row["customFieldValues"], self.values())

    def test_create_without_key_stores_null_reads_empty(self):
        resp = self.create_task()
        task_id = resp.data["task"]["task_id"]
        task = TaskMaster.objects.get(task_id=task_id)
        self.assertIsNone(task.custom_field_values)
        got = self.client.get(
            f"{reverse('get_task')}?team_id={self.team.team_id}"
            f"&project_id={self.project.project_id}&task_id={task_id}"
        )
        self.assertEqual(got.data[0]["customFieldValues"], {})

    def test_put_replaces_and_absent_key_preserves(self):
        task_id = self.create_task(custom_field_values=self.values()).data["task"]["task_id"]

        # PUT without the key: stored values untouched.
        untouched = self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "title": "Renamed"},
            format="json",
        )
        self.assertEqual(untouched.status_code, status.HTTP_200_OK)
        self.assertEqual(
            TaskMaster.objects.get(task_id=task_id).custom_field_values, self.values()
        )

        # PUT with a new map: replaced wholesale.
        new_values = {str(self.text_field.field_id): "only text now"}
        replaced = self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "custom_field_values": new_values},
            format="json",
        )
        self.assertEqual(replaced.status_code, status.HTTP_200_OK)
        self.assertEqual(
            TaskMaster.objects.get(task_id=task_id).custom_field_values, new_values
        )

        # PUT with {} clears.
        cleared = self.client.put(
            reverse("task_create"),
            {"task_id": task_id, "custom_field_values": {}},
            format="json",
        )
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        self.assertEqual(TaskMaster.objects.get(task_id=task_id).custom_field_values, {})

    def test_bad_shapes_rejected_or_dropped(self):
        self.assertEqual(
            self.create_task(custom_field_values="not a dict").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        task_id = self.create_task(
            custom_field_values={
                str(self.text_field.field_id): "ok",
                "junk-number": 42,
                "junk-dict": {"nested": True},
                "junk-mixed-list": ["fine", 3, None],
                "junk-empty": "",
            }
        ).data["task"]["task_id"]
        task = TaskMaster.objects.get(task_id=task_id)
        self.assertEqual(
            task.custom_field_values,
            {str(self.text_field.field_id): "ok", "junk-mixed-list": ["fine"]},
        )


class MilestoneValueTests(CustomFieldsTestBase):
    def setUp(self):
        super().setUp()
        self.text_field = ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name="Notes",
            field_type="text",
        )

    def test_patch_writes_backing_task_and_survives_sync(self):
        created = self.client.post(
            reverse("milestone_create"),
            {"project_id": self.project.project_id, "title": "M1"},
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        milestone_id = created.data["milestone"]["milestoneId"]
        values = {str(self.text_field.field_id): "milestone note"}

        patched = self.client.patch(
            reverse("milestone_detail", args=[milestone_id]),
            {"custom_field_values": values},
            format="json",
        )
        self.assertEqual(patched.status_code, status.HTTP_200_OK, patched.data)
        self.assertEqual(patched.data["milestone"]["customFieldValues"], values)

        m = MilestoneMaster.objects.get(milestone_id=milestone_id)
        self.assertEqual(m.task.custom_field_values, values)

        # A later metadata patch runs _sync_backing_task (which rewrites
        # the backing row from milestone fields) — values must survive.
        repatched = self.client.patch(
            reverse("milestone_detail", args=[milestone_id]),
            {"title": "M1 renamed", "status": "WIP"},
            format="json",
        )
        self.assertEqual(repatched.status_code, status.HTTP_200_OK)
        self.assertEqual(repatched.data["milestone"]["customFieldValues"], values)
        m.refresh_from_db()
        self.assertEqual(m.task.custom_field_values, values)

    def test_create_with_values_seeds_backing_task(self):
        values = {str(self.text_field.field_id): "seeded"}
        created = self.client.post(
            reverse("milestone_create"),
            {
                "project_id": self.project.project_id,
                "title": "M2",
                "custom_field_values": values,
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED, created.data)
        self.assertEqual(created.data["milestone"]["customFieldValues"], values)
        m = MilestoneMaster.objects.get(
            milestone_id=created.data["milestone"]["milestoneId"]
        )
        self.assertEqual(m.task.custom_field_values, values)


class SanitizerUnitTests(BaseAPITestCase):
    def test_non_dict_is_none(self):
        self.assertIsNone(sanitize_custom_field_values(["list"]))
        self.assertIsNone(sanitize_custom_field_values("str"))
        self.assertIsNone(sanitize_custom_field_values(None))

    def test_null_and_empty_entries_are_dropped(self):
        self.assertEqual(
            sanitize_custom_field_values({"1": None, "2": "", "3": [], "4": "keep"}),
            {"4": "keep"},
        )

    def test_long_text_is_truncated(self):
        cleaned = sanitize_custom_field_values({"1": "x" * 5000})
        self.assertEqual(len(cleaned["1"]), 4000)
