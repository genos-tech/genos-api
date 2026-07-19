"""Project-scoped task/milestone field rules (required + defaults).

Covers the `ProjectTaskFieldRulesView` contract: any project member may
READ the rules (plus ownerUserId, which gates the customize UI), only
the project OWNER may write them, the blob is whitelist-validated, and
— the load-bearing invariant — the rules are UI-only: task creation is
never rejected by them.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase

FULL_RULES = {
    "dueDate": {"required": True, "defaultOffsetDays": 0},
    "priority": {"default": "High"},
    "effortLevel": {"required": True, "default": None},
    "tags": {"required": True, "defaultTagNames": ["debug"]},
    "reporter": {"default": "creator"},
    "assignee": {"default": "some-user-id"},
}


class ProjectTaskFieldRulesTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Rules Project",
            owner=self.user,
            project_system_user=self.user,
        )
        # self.user is the owner+member; self.user2 (a team member) is a
        # project member but NOT the owner — exercises the owner gate.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate()

    def get_rules(self, project=None):
        pid = (project or self.project).project_id
        return self.client.get(f"{reverse('project_task_field_rules')}?project_id={pid}")

    def put_rules(self, rules, project=None):
        return self.client.put(
            reverse("project_task_field_rules"),
            {"project_id": (project or self.project).project_id, "rules": rules},
            format="json",
        )


class ReadWriteTests(ProjectTaskFieldRulesTestBase):
    def test_rules_start_empty_with_owner_id(self):
        resp = self.get_rules()
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data, {"rules": {}, "ownerUserId": str(self.user.id)})

    def test_owner_put_round_trips(self):
        put = self.put_rules(FULL_RULES)
        self.assertEqual(put.status_code, status.HTTP_200_OK)
        self.assertEqual(put.data["rules"], FULL_RULES)

        got = self.get_rules()
        self.assertEqual(got.status_code, status.HTTP_200_OK)
        self.assertEqual(got.data["rules"], FULL_RULES)
        self.assertEqual(got.data["ownerUserId"], str(self.user.id))

    def test_status_is_not_a_configurable_field(self):
        # Status is always auto-set at creation — not customizable.
        resp = self.put_rules({"status": {"required": True}})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.put_rules({"status": {"default": "Open"}})
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_empty_dict_clears_rules(self):
        self.put_rules(FULL_RULES)
        cleared = self.put_rules({})
        self.assertEqual(cleared.status_code, status.HTTP_200_OK)
        self.assertEqual(cleared.data["rules"], {})
        self.project.refresh_from_db()
        self.assertEqual(self.project.task_field_rules, {})

    def test_missing_project_id_is_400(self):
        resp = self.client.get(reverse("project_task_field_rules"))
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.client.put(
            reverse("project_task_field_rules"), {"rules": {}}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_project_is_404(self):
        resp = self.client.put(
            reverse("project_task_field_rules"),
            {"project_id": 999999, "rules": {}},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)


class PermissionTests(ProjectTaskFieldRulesTestBase):
    def test_member_non_owner_can_read_but_not_write(self):
        self.authenticate(self.user2)
        self.assertEqual(self.get_rules().status_code, status.HTTP_200_OK)
        resp = self.put_rules({"priority": {"required": True}})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_member_cannot_read(self):
        outsider_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="No Members Here",
            owner=self.user2,
            project_system_user=self.user2,
        )
        resp = self.get_rules(project=outsider_project)
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_ownerless_project_rejects_all_writes(self):
        # owner is SET_NULL — an orphaned project must not become
        # writable by arbitrary members.
        self.project.owner = None
        self.project.save(update_fields=["owner"])
        resp = self.put_rules({"priority": {"required": True}})
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class ValidationTests(ProjectTaskFieldRulesTestBase):
    def assert_rejected(self, rules):
        resp = self.put_rules(rules)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_unknown_field_keys_rejected(self):
        # "sprint" was dropped from the feature, "status" is always
        # auto-set, and "project" is always-required/never stored —
        # all must 400.
        self.assert_rejected({"sprint": {"required": True}})
        self.assert_rejected({"status": {"required": True}})
        self.assert_rejected({"project": {"required": True}})
        self.assert_rejected({"milestone": {"required": True}})

    def test_unknown_config_keys_rejected(self):
        self.assert_rejected({"status": {"defaultOffsetDays": 3}})
        self.assert_rejected({"dueDate": {"default": "Open"}})
        self.assert_rejected({"tags": {"default": "debug"}})

    def test_non_dict_shapes_rejected(self):
        self.assert_rejected("not-a-dict")
        self.assert_rejected({"priority": "High"})

    def test_required_must_be_boolean(self):
        self.assert_rejected({"priority": {"required": "yes"}})

    def test_due_offset_type_and_range(self):
        self.assert_rejected({"dueDate": {"defaultOffsetDays": "7"}})
        # bool is an int subclass — must still be rejected.
        self.assert_rejected({"dueDate": {"defaultOffsetDays": True}})
        self.assert_rejected({"dueDate": {"defaultOffsetDays": -1}})
        self.assert_rejected({"dueDate": {"defaultOffsetDays": 4000}})
        ok = self.put_rules({"dueDate": {"defaultOffsetDays": 3650}})
        self.assertEqual(ok.status_code, status.HTTP_200_OK)

    def test_vocabulary_defaults_validated(self):
        self.assert_rejected({"priority": {"default": "Urgent"}})
        self.assert_rejected({"effortLevel": {"default": "Huge"}})

    def test_tag_defaults_must_be_string_list(self):
        self.assert_rejected({"tags": {"defaultTagNames": "debug"}})
        self.assert_rejected({"tags": {"defaultTagNames": [""]}})
        self.assert_rejected({"tags": {"defaultTagNames": [1]}})

    def test_people_defaults_validated(self):
        self.assert_rejected({"assignee": {"default": ""}})
        self.assert_rejected({"reporter": {"default": 5}})
        ok = self.put_rules({"assignee": {"default": "creator"}})
        self.assertEqual(ok.status_code, status.HTTP_200_OK)


class UiOnlyEnforcementTests(ProjectTaskFieldRulesTestBase):
    def test_rules_never_block_task_creation(self):
        """The rules are enforced by the app UI only — the create
        endpoint must accept a violating payload (agent and internal
        creation paths depend on this)."""
        self.put_rules({"dueDate": {"required": True}, "tags": {"required": True}})
        resp = self.client.post(
            reverse("task_create"),
            {
                "team": str(self.team.team_id),
                "project": self.project.project_id,
                "assignee": None,
                "reporter": self.user.id,
                "title": "No due date, no tags",
                "priority": None,
                "effort_level": None,
                "status": "Open",
                "content": [],
                "due_date": None,
                "links": [],
                "tags": [],
                "is_init_task": False,
            },
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
