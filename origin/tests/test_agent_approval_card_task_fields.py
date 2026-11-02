"""The approval card for a start date, collaborators and custom fields (MIP-878).

A proposed `update_task` / `create_task` carries user ids (collaborators, the
value of a member field), and the card should show people. For an existing task
it also shows what each field is now, so the user sees old -> new. Both follow
the rule of `label_acl.LabelAccess`: a name is printed only for what the caller
may read, and anything else stays as proposed.
"""

from origin.models.project.prj_models import ProjectCustomField, ProjectTags
from origin.search_engine.agent import controller

from .test_agent_approval_card_label_acl import ApprovalCardAclBase


class TaskFieldCardBase(ApprovalCardAclBase):
    def setUp(self):
        super().setUp()
        self.customer = self._field("Customer", "text")
        self.reviewers = self._field(
            "Reviewers",
            "tag",
            options=[
                {"id": "opt-a", "label": "Alice", "color": "red"},
                {"id": "opt-b", "label": "Bob", "color": "blue"},
            ],
        )
        self.review_date = self._field("Review date", "date")
        self.qa = self._field("QA owner", "member")

    def _field(self, name, kind, **kwargs):
        return ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name=name,
            field_type=kind,
            sort_order=ProjectCustomField.objects.filter(project=self.project).count(),
            **kwargs,
        )

    def _update(self, user=None, task=None, **extra):
        args = {"task_id": (task or self.task).task_id, **extra}
        return self._card(args, "update_task", user or self.user)


class StartDateCardTests(TaskFieldCardBase):
    def test_the_card_shows_the_present_and_the_proposed_start_date(self):
        self.task.start_date = "2026-11-01"
        self.task.save()

        out = self._update(start_date="2026-11-10")

        self.assertEqual(out["start_date"], "2026-11-10")
        self.assertEqual(out["current"], {"start_date": "2026-11-01"})

    def test_a_task_with_no_start_date_shows_none_and_clearing_shows_what_goes(self):
        self.assertEqual(self._update(start_date="2026-11-10")["current"], {"start_date": None})

        self.task.start_date = "2026-11-01"
        self.task.save()
        out = self._update(start_date="")

        self.assertEqual(out["start_date"], "")
        self.assertEqual(out["current"], {"start_date": "2026-11-01"})

    def test_nothing_is_added_for_a_task_the_caller_cannot_read(self):
        out = self._update(user=self.user, task=self.secret_task, start_date="2026-11-10")

        self.assertNotIn("current", out)
        self.assertEqual(out["task_id"], self.secret_task.task_id)


class CollaboratorsCardTests(TaskFieldCardBase):
    def test_proposed_and_present_collaborators_are_usernames(self):
        self.task.collaborators.set([self.user2])

        out = self._update(collaborators=[str(self.user.id)])

        self.assertEqual(out["collaborators"], ["testuser"])
        self.assertEqual(out["current"]["collaborators"], ["otheruser"])

    def test_clearing_shows_who_goes(self):
        self.task.collaborators.set([self.user2])

        out = self._update(collaborators=[])

        self.assertEqual(out["collaborators"], [])
        self.assertEqual(out["current"]["collaborators"], ["otheruser"])

    def test_someone_the_caller_cannot_name_stays_a_raw_id_either_side(self):
        """`stranger` is on another team, so `get_team_members` would not list
        them to this caller, and the card must not print their username."""
        self.task.collaborators.set([self.stranger])

        out = self._update(collaborators=[str(self.stranger.id)])

        self.assertEqual(out["collaborators"], [str(self.stranger.id)])
        self.assertEqual(out["current"]["collaborators"], [str(self.stranger.id)])

    def test_a_create_card_labels_the_collaborators_and_adds_no_current(self):
        out = self._card(
            {
                "title": "New",
                "project_id": self.project.project_id,
                "collaborators": [str(self.user2.id), str(self.stranger.id)],
            },
            "create_task",
            self.user,
        )

        self.assertEqual(out["collaborators"], ["otheruser", str(self.stranger.id)])
        self.assertEqual(out["project_id"], "Website Redesign")
        self.assertNotIn("current", out)


class CustomFieldsCardTests(TaskFieldCardBase):
    def test_each_field_shows_its_present_value_by_the_name_the_call_used(self):
        self.task.custom_field_values = {
            str(self.customer.field_id): "Acme",
            str(self.reviewers.field_id): ["opt-a", "opt-b"],
            str(self.review_date.field_id): "2026-11-01",
            str(self.qa.field_id): str(self.user2.id),
        }
        self.task.save()

        out = self._update(
            custom_fields=[
                {"name": "customer", "value": "Globex"},
                {"name": "Reviewers", "values": ["Bob"]},
                {"name": "Review date", "value": ""},
                {"name": "QA owner", "value": str(self.user.id)},
            ]
        )

        self.assertEqual(
            out["current"]["custom_fields"],
            {
                "customer": "Acme",
                "Reviewers": ["Alice", "Bob"],
                "Review date": "2026-11-01",
                "QA owner": "otheruser",
            },
        )
        proposed = {e["name"]: e for e in out["custom_fields"]}
        self.assertEqual(proposed["customer"]["value"], "Globex")
        self.assertEqual(proposed["Reviewers"]["values"], ["Bob"])
        self.assertEqual(proposed["QA owner"]["value"], "testuser")

    def test_a_field_with_no_value_shows_none(self):
        out = self._update(custom_fields=[{"name": "Customer", "value": "Acme"}])

        self.assertEqual(out["current"]["custom_fields"], {"Customer": None})

    def test_a_name_that_is_no_field_is_left_as_proposed_and_not_looked_up(self):
        out = self._update(custom_fields=[{"name": "Budget", "value": "1"}])

        self.assertEqual(out["custom_fields"], [{"name": "Budget", "value": "1"}])
        self.assertNotIn("custom_fields", out.get("current", {}))

    def test_a_reader_who_is_not_on_the_project_gets_no_field_names_and_raw_member_ids(self):
        """The assignee can read the task without being on its project. The
        project's fields are for its members, so the card neither resolves a
        name nor shows a present value for them, and a member id stays raw."""
        self.task.assignee = self.user2
        self.task.custom_field_values = {str(self.customer.field_id): "Acme"}
        self.task.save()

        out = self._update(
            user=self.user2,
            custom_fields=[
                {"name": "Customer", "value": "Globex"},
                {"name": "QA owner", "value": str(self.user.id)},
            ],
            start_date="2026-11-10",
        )

        self.assertNotIn("custom_fields", out["current"])
        self.assertEqual(out["current"]["start_date"], None)
        self.assertEqual(out["custom_fields"][1]["value"], str(self.user.id))

    def test_a_create_card_labels_a_member_value_and_adds_no_current(self):
        out = self._card(
            {
                "title": "New",
                "project_id": self.project.project_id,
                "custom_fields": [
                    {"name": "QA owner", "value": str(self.user2.id)},
                    {"name": "Customer", "value": "Acme"},
                ],
            },
            "create_task",
            self.user,
        )

        self.assertEqual(out["custom_fields"][0]["value"], "otheruser")
        self.assertEqual(out["custom_fields"][1], {"name": "Customer", "value": "Acme"})
        self.assertNotIn("current", out)

    def test_a_create_card_for_a_project_the_caller_is_not_in_does_not_resolve_fields(self):
        out = self._card(
            {
                "title": "New",
                "project_id": self.secret.project_id,
                "custom_fields": [{"name": "QA owner", "value": str(self.user2.id)}],
            },
            "create_task",
            self.user,
        )

        self.assertEqual(out["project_id"], self.secret.project_id)
        self.assertEqual(out["custom_fields"][0]["value"], str(self.user2.id))

    def test_malformed_arguments_never_break_the_card(self):
        for bad in ({"Customer": "x"}, ["Customer"], [None], [{"value": 1}], "x", 7):
            with self.subTest(bad=bad):
                out = self._update(custom_fields=bad, collaborators=bad)

                self.assertEqual(out["custom_fields"], bad)

    def test_the_callers_arguments_are_not_modified(self):
        args = {
            "task_id": self.task.task_id,
            "collaborators": [str(self.user2.id)],
            "custom_fields": [{"name": "QA owner", "value": str(self.user2.id)}],
        }
        before = {k: (list(v) if isinstance(v, list) else v) for k, v in args.items()}

        controller._friendly_arguments(args, "update_task", ctx=self._ctx(self.user))

        self.assertEqual(args["collaborators"], before["collaborators"])
        self.assertEqual(args["custom_fields"], [{"name": "QA owner", "value": str(self.user2.id)}])


class BulkCardTests(TaskFieldCardBase):
    def test_a_bulk_row_shows_the_present_start_date_and_tags(self):
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="bug",
            tag_color="red",
            tag_text_color="white",
        )
        self.task.start_date = "2026-11-01"
        self.task.tags = [{"tagName": "bug", "tagColor": "red", "tagTextColor": "white"}]
        self.task.save()

        out = self._card(
            {
                "updates": [
                    {
                        "task_id": self.task.task_id,
                        "start_date": "2026-11-10",
                        "tags": [],
                        "rationale": "x",
                    }
                ]
            },
            "update_tasks_bulk",
            self.user,
        )

        row = out["updates"][0]
        self.assertEqual(row["current"]["start_date"], "2026-11-01")
        self.assertEqual(row["current"]["tags"], ["bug"])
        self.assertEqual(row["start_date"], "2026-11-10")
        self.assertEqual(row["tags"], [])

    def test_a_row_for_a_task_the_caller_cannot_read_gets_no_snapshot(self):
        out = self._card(
            {
                "updates": [
                    {
                        "task_id": self.secret_task.task_id,
                        "start_date": "2026-11-10",
                        "rationale": "x",
                    }
                ]
            },
            "update_tasks_bulk",
            self.user,
        )

        self.assertNotIn("current", out["updates"][0])
