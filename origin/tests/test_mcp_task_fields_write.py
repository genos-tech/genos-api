"""Over MCP, a task's start date, collaborators and custom fields are written
by the names people use (MIP-878).

The rules are in `test_agent_tool_task_start_collab_custom`. This is what the
endpoint adds: the arguments are published with schemas a client accepts, a
nested key the schema does not define is refused (the top-level check only sees
the first level), what `get_task` shows can be sent straight back, and a
read-only key cannot write.
"""

from __future__ import annotations

from datetime import date

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.project.prj_models import ProjectCustomField, ProjectTags
from origin.models.task.task_models import TaskMaster
from origin.tests.test_mcp_endpoint import McpBase


class McpWriteFieldsBase(McpBase):
    def setUp(self):
        super().setUp()
        self.customer = ProjectCustomField.objects.create(
            team=self.team, project=self.project, field_name="Customer", field_type="text"
        )
        self.reviewers = ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name="Reviewers",
            field_type="tag",
            sort_order=1,
            options=[{"id": "opt-a", "label": "Alice", "color": "red"}],
        )
        self.bug = ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="bug",
            tag_color="red",
            tag_text_color="white",
        )
        self._key(scope=SCOPE_WRITE)

    def _text(self, name, arguments):
        return self._tool_text(self._call(name, arguments))

    def _listed(self):
        return {t["name"]: t for t in self._result(self._rpc("tools/list"))["tools"]}


class UpdateTaskOverMcpTests(McpWriteFieldsBase):
    def test_start_date_collaborators_and_custom_fields_are_written_by_name(self):
        text, is_error = self._text(
            "update_task",
            {
                "task_id": "WRD-7",
                "start_date": "2026-11-10",
                "collaborators": [str(self.user2.id)],
                "custom_fields": [
                    {"name": "customer", "value": "Acme"},
                    {"name": "Reviewers", "values": ["alice"]},
                ],
            },
        )

        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.start_date, date(2026, 11, 10))
        self.assertEqual(
            [str(u) for u in self.task.collaborators.values_list("id", flat=True)],
            [str(self.user2.id)],
        )
        self.assertEqual(
            self.task.custom_field_values,
            {str(self.customer.field_id): "Acme", str(self.reviewers.field_id): ["opt-a"]},
        )

    def test_what_get_task_returns_can_be_sent_back_to_another_task(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(
            custom_field_values={
                str(self.customer.field_id): "Acme",
                str(self.reviewers.field_id): ["opt-a"],
            }
        )
        other = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Other",
            status="Open",
            project_task_number=8,
        )
        shown = self._result(self._call("get_task", {"task_id": "WRD-7"}))["structuredContent"][
            "custom_fields"
        ]

        text, is_error = self._text(
            "update_task",
            {
                "task_id": "WRD-8",
                "custom_fields": [
                    {
                        "name": row["name"],
                        **(
                            {"values": row["values"]}
                            if "values" in row
                            else {"value": row["value"]}
                        ),
                    }
                    for row in shown
                ],
            },
        )

        self.assertFalse(is_error, text)
        other.refresh_from_db()
        self.assertEqual(
            other.custom_field_values,
            {str(self.customer.field_id): "Acme", str(self.reviewers.field_id): ["opt-a"]},
        )

    def test_the_fields_are_cleared_with_the_documented_spellings(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(
            start_date=date(2026, 11, 1),
            custom_field_values={str(self.customer.field_id): "Acme"},
        )
        self.task.collaborators.set([self.user2])

        text, is_error = self._text(
            "update_task",
            {
                "task_id": "WRD-7",
                "start_date": "",
                "collaborators": [],
                "custom_fields": [{"name": "Customer", "value": ""}],
            },
        )

        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.start_date)
        self.assertEqual(self.task.custom_field_values, {})
        self.assertEqual(self.task.collaborators.count(), 0)

    def test_a_key_the_entry_does_not_define_is_refused_and_nothing_changes(self):
        text, is_error = self._text(
            "update_task",
            {
                "task_id": "WRD-7",
                "title": "renamed",
                "custom_fields": [{"name": "Customer", "val": "Acme"}],
            },
        )

        self.assertTrue(is_error)
        self.assertIn("has no `val`", text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Fix the login redirect")
        self.assertFalse(self.task.custom_field_values)

    def test_a_refusal_lists_what_is_valid(self):
        text, is_error = self._text(
            "update_task",
            {"task_id": "WRD-7", "custom_fields": [{"name": "Budget", "value": "1"}]},
        )

        self.assertTrue(is_error)
        self.assertIn("'Customer'", text)
        self.assertIn("'Reviewers'", text)

    def test_a_collaborator_outside_the_team_is_refused(self):
        outsider = type(self.user).objects.create_user(
            username="outsider", email="out@example.com", password="x"
        )

        text, is_error = self._text(
            "update_task", {"task_id": "WRD-7", "collaborators": [str(outsider.id)]}
        )

        self.assertTrue(is_error)
        self.assertIn("not a member", text)
        self.assertEqual(self.task.collaborators.count(), 0)

    def test_a_read_key_cannot_write_them(self):
        self._key(scope=SCOPE_READ)

        text, is_error = self._text("update_task", {"task_id": "WRD-7", "start_date": "2026-11-10"})

        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.start_date)

    def test_the_arguments_are_published_with_schemas_and_no_approval_promise(self):
        update = self._listed()["update_task"]
        properties = update["inputSchema"]["properties"]

        self.assertEqual(properties["start_date"]["type"], "string")
        self.assertEqual(properties["collaborators"]["type"], "array")
        self.assertEqual(properties["collaborators"]["items"], {"type": "string"})
        custom = properties["custom_fields"]
        self.assertEqual(custom["type"], "array")
        self.assertEqual(custom["items"]["type"], "object")
        self.assertEqual(custom["items"]["required"], ["name"])
        self.assertEqual(set(custom["items"]["properties"]), {"name", "value", "values"})
        self.assertEqual(custom["items"]["properties"]["values"]["type"], "array")
        for word in ("start_date", "collaborators", "custom_fields", "list_project_tags"):
            self.assertIn(word, update["description"])
        self.assertNotIn("approval", update["description"].lower())


class CreateTaskOverMcpTests(McpWriteFieldsBase):
    def test_the_fields_are_set_at_creation(self):
        text, is_error = self._text(
            "create_task",
            {
                "title": "Fresh",
                "project_id": self.project.project_id,
                "start_date": "2026-11-10",
                "collaborators": [str(self.user2.id)],
                "custom_fields": [{"name": "Customer", "value": "Acme"}],
            },
        )

        self.assertFalse(is_error, text)
        made = TaskMaster.objects.get(title="Fresh")
        self.assertEqual(made.start_date, date(2026, 11, 10))
        self.assertEqual(made.collaborators.count(), 1)
        self.assertEqual(made.custom_field_values, {str(self.customer.field_id): "Acme"})

    def test_a_refused_field_creates_nothing(self):
        before = TaskMaster.objects.count()

        text, is_error = self._text(
            "create_task",
            {
                "title": "Fresh",
                "project_id": self.project.project_id,
                "custom_fields": [{"name": "Customer", "value": 5}],
            },
        )

        self.assertTrue(is_error)
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_the_arguments_are_published(self):
        properties = self._listed()["create_task"]["inputSchema"]["properties"]

        for key in ("start_date", "collaborators", "custom_fields"):
            self.assertIn(key, properties)


class BulkOverMcpTests(McpWriteFieldsBase):
    def test_start_date_and_tags_per_row_with_display_ids(self):
        text, is_error = self._text(
            "update_tasks_bulk",
            {
                "updates": [
                    {
                        "task_id": "WRD-7",
                        "start_date": "2026-11-10",
                        "tags": ["bug"],
                        "rationale": "Plan it.",
                    }
                ]
            },
        )

        self.assertFalse(is_error, text)
        self.task.refresh_from_db()
        self.assertEqual(self.task.start_date, date(2026, 11, 10))
        self.assertEqual([t["tagName"] for t in self.task.tags], ["bug"])

    def test_an_unknown_tag_rejects_the_batch(self):
        text, is_error = self._text(
            "update_tasks_bulk",
            {
                "updates": [
                    {
                        "task_id": "WRD-7",
                        "start_date": "2026-11-10",
                        "tags": ["nope"],
                        "rationale": "Plan it.",
                    }
                ]
            },
        )

        self.assertTrue(is_error)
        self.assertIn("updates[0]", text)
        self.assertIn("unknown tag(s) 'nope'", text)
        self.task.refresh_from_db()
        self.assertIsNone(self.task.start_date)

    def test_the_row_arguments_are_published(self):
        row = self._listed()["update_tasks_bulk"]["inputSchema"]["properties"]["updates"]["items"][
            "properties"
        ]

        self.assertEqual(row["start_date"]["type"], "string")
        self.assertEqual(row["tags"]["type"], "array")
