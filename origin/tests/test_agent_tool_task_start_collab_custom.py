"""`update_task` / `create_task` / `update_tasks_bulk`: start date, collaborators,
custom fields and tags (MIP-878).

Before this an agent could read a task's start date, collaborators and custom
fields (MIP-877) but not change them. The writes have to leave what the UI's
own PUT leaves, so the core tests drive the same change through the REST
endpoint and through the tool on twin tasks and compare where each ends up:
the stored columns, and the history rows the signals write. The rest pin each
guard: a refused call changes nothing, even when the same call carried other
valid changes.
"""

from datetime import date

from django.contrib.auth import get_user_model
from django.urls import reverse

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMembers, ProjectTags
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolError
from origin.search_engine.agent.tools.create_task import CREATE_TASK
from origin.search_engine.agent.tools.update_task import UPDATE_TASK
from origin.search_engine.agent.tools.update_tasks_bulk import UPDATE_TASKS_BULK
from origin.services.milestone_service import ensure_backing_task

from .test_agent_tool_task_read_fields import ReadFieldsBase, fenced


def _activity(task, action_type):
    """What a history row says, minus who/when/how it arrived (`via` differs
    between the UI and the tool by design)."""
    return [
        (field, old, new, {k: v for k, v in (meta or {}).items() if k != "via"})
        for field, old, new, meta in TaskActivity.objects.filter(task=task, action_type=action_type)
        .order_by("activity_id")
        .values_list("field_name", "old_value", "new_value", "metadata")
    ]


class WriteBase(ReadFieldsBase):
    def setUp(self):
        super().setUp()
        self.outsider = get_user_model().objects.create_user(
            username="outsider", email="out@example.com", password="x"
        )
        self.guest = get_user_model().objects.create_user(
            username="guest", email="guest@example.com", password="x"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)

    def _set(self, task, **extra):
        return UPDATE_TASK.run({"task_id": task.task_id, **extra}, self.ctx)

    def _ui_put(self, task, **payload):
        self.authenticate()
        res = self.client.put("/api/v2/task/", {"task_id": task.task_id, **payload}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _tag(self, name):
        return ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=ProjectTags.objects.filter(project=self.project).count() + 1,
            tag_name=name,
            tag_color="red",
            tag_text_color="white",
        )

    def _snapshot(self, task):
        task.refresh_from_db()
        return (
            task.title,
            task.start_date,
            task.custom_field_values,
            sorted(str(u) for u in task.collaborators.values_list("id", flat=True)),
            task.tags,
            task.ts_updated_at,
        )

    def _refused_and_unchanged(self, task, message, **extra):
        """The call is refused, names `message`, and changes nothing, even though
        it also carried a title, which is valid."""
        before = self._snapshot(task)
        with self.assertRaises(ToolError) as caught:
            self._set(task, title="A new title", **extra)
        self.assertIn(message, str(caught.exception))
        self.assertEqual(self._snapshot(task), before)
        return str(caught.exception)


class StartDateTests(WriteBase):
    def test_setting_a_start_date_matches_the_uis_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")

        out = self._set(by_tool, start_date="2026-11-10")
        self._ui_put(by_ui, start_date="2026-11-10")

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()
        self.assertEqual(by_tool.start_date, date(2026, 11, 10))
        self.assertEqual(by_tool.start_date, by_ui.start_date)
        self.assertEqual(out["changed_fields"], ["start_date"])
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.START_DATE),
            _activity(by_ui, TaskActivityActionType.START_DATE),
        )
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.START_DATE)[0][1:3], (None, "2026-11-10")
        )

    def test_a_changed_start_date_records_the_old_and_the_new(self):
        task = self._task("t", start_date=date(2026, 11, 1))

        self._set(task, start_date="2026-11-10")

        self.assertEqual(
            [row[1:3] for row in _activity(task, TaskActivityActionType.START_DATE)],
            [("2026-11-01", "2026-11-10")],
        )

    def test_the_same_date_is_a_no_op_for_both(self):
        by_tool = self._task("by tool", start_date=date(2026, 11, 10))
        by_ui = self._task("by ui", start_date=date(2026, 11, 10))

        out = self._set(by_tool, start_date="2026-11-10")
        self._ui_put(by_ui, start_date="2026-11-10")

        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(_activity(by_tool, TaskActivityActionType.START_DATE), [])
        self.assertEqual(_activity(by_ui, TaskActivityActionType.START_DATE), [])

    def test_an_empty_string_clears_it(self):
        task = self._task("t", start_date=date(2026, 11, 1))

        out = self._set(task, start_date="")

        task.refresh_from_db()
        self.assertIsNone(task.start_date)
        self.assertEqual(out["changed_fields"], ["start_date(cleared)"])
        self.assertEqual(
            [row[1:3] for row in _activity(task, TaskActivityActionType.START_DATE)],
            [("2026-11-01", None)],
        )

    def test_clearing_an_already_empty_date_is_a_no_op(self):
        task = self._task("t")

        self.assertEqual(self._set(task, start_date="")["changed_fields"], [])

    def test_a_start_date_after_the_due_date_is_accepted_as_in_the_app(self):
        by_tool = self._task("by tool", due_date="2026-11-05")
        by_ui = self._task("by ui", due_date="2026-11-05")

        self._set(by_tool, start_date="2026-12-01")
        self._ui_put(by_ui, start_date="2026-12-01")

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()
        self.assertEqual(by_tool.start_date, by_ui.start_date)
        self.assertEqual(by_tool.start_date, date(2026, 12, 1))

    def test_a_malformed_date_is_refused_and_nothing_changes(self):
        task = self._task("t", start_date=date(2026, 11, 1))
        for bad in ("11/20/2026", "20261120", "2026-W47-5", "tomorrow", "2026-02-30", 20261120):
            with self.subTest(bad=bad):
                self._refused_and_unchanged(task, "YYYY-MM-DD", start_date=bad)

    def test_a_milestones_backing_row_keeps_the_milestone_in_step(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="MS", reporter=self.user
        )
        backing = ensure_backing_task(milestone)

        self._set(backing, start_date="2026-11-10")

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.start_date, date(2026, 11, 10))
        self.assertEqual(backing.start_date, date(2026, 11, 10))

        self._set(backing, start_date="")
        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertIsNone(milestone.start_date)
        self.assertIsNone(backing.start_date)


class CollaboratorsTests(WriteBase):
    def _collaborators(self, task):
        return sorted(str(u) for u in task.collaborators.values_list("id", flat=True))

    def test_setting_collaborators_matches_the_uis_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")
        before = {t.pk: t.ts_updated_at for t in (by_tool, by_ui)}

        out = self._set(by_tool, collaborators=[str(self.user2.id), str(self.guest.id)])
        self._ui_put(by_ui, collaborators=[str(self.user2.id), str(self.guest.id)])

        expected = sorted([str(self.user2.id), str(self.guest.id)])
        self.assertEqual(self._collaborators(by_tool), expected)
        self.assertEqual(self._collaborators(by_ui), expected)
        self.assertEqual(out["changed_fields"], ["collaborators"])
        for task in (by_tool, by_ui):
            task.refresh_from_db()
            self.assertGreater(task.ts_updated_at, before[task.pk])

    def test_an_empty_list_clears_them_like_the_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")
        for task in (by_tool, by_ui):
            task.collaborators.set([self.user2])

        out = self._set(by_tool, collaborators=[])
        self._ui_put(by_ui, collaborators=[])

        self.assertEqual(self._collaborators(by_tool), [])
        self.assertEqual(self._collaborators(by_ui), [])
        self.assertEqual(out["changed_fields"], ["collaborators(cleared)"])

    def test_the_list_replaces_the_current_one(self):
        task = self._task("t")
        task.collaborators.set([self.user2])

        self._set(task, collaborators=[str(self.guest.id)])

        self.assertEqual(self._collaborators(task), [str(self.guest.id)])

    def test_the_same_set_in_another_order_is_a_no_op_and_does_not_touch_the_task(self):
        task = self._task("t")
        task.collaborators.set([self.user2, self.guest])
        task.refresh_from_db()
        stamp = task.ts_updated_at

        out = self._set(task, collaborators=[str(self.guest.id), str(self.user2.id).upper()])

        task.refresh_from_db()
        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(task.ts_updated_at, stamp)

    def test_a_collaborators_only_call_is_not_swallowed_as_no_changes(self):
        task = self._task("t")

        out = self._set(task, collaborators=[str(self.user2.id)])

        self.assertEqual(out["changed_fields"], ["collaborators"])
        self.assertIn("collaborators", out["__summary__"])

    def test_a_team_member_a_project_member_and_the_caller_are_allowed(self):
        task = self._task("t")

        self._set(task, collaborators=[str(self.user.id), str(self.user2.id), str(self.guest.id)])

        self.assertEqual(len(self._collaborators(task)), 3)

    def test_someone_outside_the_team_and_project_is_refused_by_name(self):
        task = self._task("t")

        message = self._refused_and_unchanged(
            task, "not a member", collaborators=[str(self.outsider.id)]
        )

        self.assertIn(str(self.outsider.id), message)
        self.assertEqual(self._collaborators(task), [])

    def test_a_bad_list_is_refused_and_nothing_changes(self):
        task = self._task("t")
        task.collaborators.set([self.user2])
        cases = {
            "an id that is not a uuid": ["not-a-uuid"],
            "an unknown user": ["12345678-1234-5678-1234-567812345678"],
            "a number": [7],
            "a bare string": str(self.user2.id),
            "an object": {"id": str(self.user2.id)},
        }
        for label, bad in cases.items():
            with self.subTest(label):
                self._refused_and_unchanged(task, "`collaborators`", collaborators=bad)
        self.assertEqual(self._collaborators(task), [str(self.user2.id)])

    def test_a_deleted_account_is_refused(self):
        get_user_model().objects.filter(pk=self.guest.pk).update(is_deleted=True)

        self._refused_and_unchanged(
            self._task("t"), "not a member", collaborators=[str(self.guest.id)]
        )

    def test_a_current_collaborator_who_has_left_can_be_kept_or_removed(self):
        """The list replaces the current one, so refusing someone already on it
        would make it impossible to keep the others, or to remove them."""
        task = self._task("t")
        task.collaborators.set([self.outsider, self.user2])

        self._set(task, collaborators=[str(self.outsider.id), str(self.guest.id)])
        self.assertEqual(
            self._collaborators(task), sorted([str(self.outsider.id), str(self.guest.id)])
        )

        self._set(task, collaborators=[str(self.guest.id)])
        self.assertEqual(self._collaborators(task), [str(self.guest.id)])

    def test_the_ui_records_no_history_for_collaborators_and_neither_does_the_tool(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")
        before = {t.pk: TaskActivity.objects.filter(task=t).count() for t in (by_tool, by_ui)}

        self._set(by_tool, collaborators=[str(self.user2.id)])
        self._ui_put(by_ui, collaborators=[str(self.user2.id)])

        for task in (by_tool, by_ui):
            self.assertEqual(TaskActivity.objects.filter(task=task).count(), before[task.pk])

    def test_a_milestones_collaborators_live_on_its_backing_row_as_the_milestone_view_writes_them(
        self,
    ):
        by_tool = self._milestone_backing("MS tool")
        by_ui = self._milestone_backing("MS ui")

        self._set(by_tool, collaborators=[str(self.user2.id)])
        self.authenticate()
        res = self.client.patch(
            reverse("milestone_detail", args=[by_ui.milestone.milestone_id]),
            {"collaborators": [str(self.user2.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(self._collaborators(by_tool), [str(self.user2.id)])
        self.assertEqual(self._collaborators(by_tool), self._collaborators(by_ui))

    def _milestone_backing(self, title):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title=title, reporter=self.user
        )
        backing = ensure_backing_task(milestone)
        backing.milestone = milestone
        return backing

    def test_a_task_in_no_project_cannot_take_collaborators(self):
        task = TaskMaster.objects.create(
            team=self.team, project=None, reporter=self.user, title="t", status="Open"
        )

        self._refused_and_unchanged(task, "in no project", collaborators=[str(self.user2.id)])

    def test_the_team_membership_row_is_what_makes_a_team_member(self):
        """A user with a team row and no project row is allowed; remove the row
        and they are not."""
        task = self._task("t")
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=True)

        self._refused_and_unchanged(task, "not a member", collaborators=[str(self.user2.id)])


class CustomFieldsTests(WriteBase):
    def _values(self, task):
        task.refresh_from_db()
        return task.custom_field_values

    def _fid(self, field):
        return str(field.field_id)

    def test_each_type_matches_the_uis_put(self):
        """The same four values, by name through the tool and by field id through
        the PUT, store the same map and write the same history."""
        by_tool, by_ui = self._task("by tool"), self._task("by ui")
        member = str(self.user2.id)

        out = self._set(
            by_tool,
            custom_fields=[
                {"name": "Customer", "value": "Acme"},
                {"name": "Reviewers", "values": ["Bob", "Alice"]},
                {"name": "Review date", "value": "2026-11-20"},
                {"name": "QA owner", "value": member},
            ],
        )
        self._ui_put(
            by_ui,
            custom_field_values={
                self._fid(self.customer): "Acme",
                self._fid(self.reviewers): ["opt-b", "opt-a"],
                self._fid(self.review_date): "2026-11-20",
                self._fid(self.qa): member,
            },
        )

        self.assertEqual(self._values(by_tool), self._values(by_ui))
        self.assertEqual(out["changed_fields"], ["custom_fields"])
        tool_rows = _activity(by_tool, TaskActivityActionType.CUSTOM_FIELD)
        self.assertEqual(len(tool_rows), 4)
        self.assertEqual(tool_rows, _activity(by_ui, TaskActivityActionType.CUSTOM_FIELD))
        by_name = {row[3]["fieldName"]: row for row in tool_rows}
        self.assertEqual(by_name["Reviewers"][3]["newLabel"], ["Alice", "Bob"])
        self.assertEqual(by_name["QA owner"][3]["newLabel"], "otheruser")

    def test_only_the_fields_named_change_and_orphans_are_kept(self):
        task = self._task(
            "t",
            custom_field_values={
                self._fid(self.customer): "Acme",
                self._fid(self.review_date): "2026-11-01",
                "99999": "left behind by a deleted field",
            },
        )

        self._set(task, custom_fields=[{"name": "Review date", "value": "2026-12-01"}])

        self.assertEqual(
            self._values(task),
            {
                self._fid(self.customer): "Acme",
                self._fid(self.review_date): "2026-12-01",
                "99999": "left behind by a deleted field",
            },
        )

    def test_clearing_removes_only_that_field_by_every_empty_spelling(self):
        for entry in (
            {"name": "Customer", "value": ""},
            {"name": "Customer", "value": None},
            {"name": "Reviewers", "values": []},
            {"name": "Reviewers", "value": ""},
            {"name": "Reviewers", "values": None},
        ):
            with self.subTest(entry=entry):
                task = self._task(
                    "t",
                    custom_field_values={
                        self._fid(self.customer): "Acme",
                        self._fid(self.reviewers): ["opt-a"],
                    },
                )

                out = self._set(task, custom_fields=[entry])

                gone = self._fid(self.customer if entry["name"] == "Customer" else self.reviewers)
                kept = self._fid(self.reviewers if entry["name"] == "Customer" else self.customer)
                self.assertNotIn(gone, self._values(task))
                self.assertIn(kept, self._values(task))
                self.assertEqual(out["changed_fields"], ["custom_fields"])

    def test_clearing_matches_the_uis_put_and_its_history(self):
        stored = {self._fid(self.customer): "Acme", self._fid(self.review_date): "2026-11-01"}
        by_tool = self._task("by tool", custom_field_values=dict(stored))
        by_ui = self._task("by ui", custom_field_values=dict(stored))

        self._set(by_tool, custom_fields=[{"name": "Customer", "value": ""}])
        self._ui_put(by_ui, custom_field_values={self._fid(self.review_date): "2026-11-01"})

        self.assertEqual(self._values(by_tool), self._values(by_ui))
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.CUSTOM_FIELD),
            _activity(by_ui, TaskActivityActionType.CUSTOM_FIELD),
        )

    def test_the_same_value_is_a_no_op(self):
        task = self._task("t", custom_field_values={self._fid(self.customer): "Acme"})

        out = self._set(task, custom_fields=[{"name": "Customer", "value": "Acme"}])

        self.assertEqual(out["changed_fields"], [])
        self.assertEqual(_activity(task, TaskActivityActionType.CUSTOM_FIELD), [])

    def test_an_empty_list_changes_nothing(self):
        task = self._task("t", custom_field_values={self._fid(self.customer): "Acme"})

        self.assertEqual(self._set(task, custom_fields=[])["changed_fields"], [])
        self.assertEqual(self._values(task), {self._fid(self.customer): "Acme"})

    def test_what_get_task_shows_can_be_sent_back(self):
        """The read shape round-trips: a name and value copied from `get_task`,
        fence and all, writes the same value."""
        task = self._task(
            "t",
            custom_field_values={
                self._fid(self.customer): "Acme",
                self._fid(self.reviewers): ["opt-a", "opt-b"],
            },
        )
        shown = self._read(task)["custom_fields"]
        other = self._task("other")

        self._set(
            other,
            custom_fields=[
                {
                    "name": row["name"],
                    **({"values": row["values"]} if "values" in row else {"value": row["value"]}),
                }
                for row in shown
            ],
        )

        self.assertEqual(self._values(other), self._values(task))

    # -- naming a field --

    def test_a_name_is_matched_ignoring_case_and_a_copied_fence(self):
        task = self._task("t")

        self._set(
            task,
            custom_fields=[
                {"name": "customer", "value": "a"},
                {"name": fenced("REVIEW DATE"), "value": "2026-11-20"},
            ],
        )

        self.assertEqual(
            self._values(task),
            {self._fid(self.customer): "a", self._fid(self.review_date): "2026-11-20"},
        )

    def test_an_unknown_name_is_refused_with_the_valid_ones(self):
        message = self._refused_and_unchanged(
            self._task("t"),
            "no custom field 'Budget'",
            custom_fields=[{"name": "Budget", "value": "1"}],
        )

        self.assertIn("'Customer'", message)
        self.assertIn("'QA owner'", message)

    def test_an_ambiguous_name_is_refused(self):
        self._field("customer", "text")

        self._refused_and_unchanged(
            self._task("t"),
            "more than one field",
            custom_fields=[{"name": "CUSTOMER", "value": "x"}],
        )

    def test_another_projects_field_cannot_be_named(self):
        other = self._other_project(member=True)
        self._field("Theirs", "text", project=other)

        self._refused_and_unchanged(
            self._task("t"),
            "no custom field 'Theirs'",
            custom_fields=[{"name": "Theirs", "value": "x"}],
        )

    def test_the_same_field_twice_is_refused(self):
        self._refused_and_unchanged(
            self._task("t"),
            "listed twice",
            custom_fields=[{"name": "Customer", "value": "a"}, {"name": "customer", "value": "b"}],
        )

    # -- the shape of the argument --

    def test_a_malformed_argument_is_refused_and_nothing_changes(self):
        cases = {
            "not a list": ({"Customer": "Acme"}, "must be a list"),
            "an entry that is not an object": (["Customer"], "must be an object"),
            "no name": ([{"value": "x"}], "`name` is required"),
            "an unknown key": ([{"name": "Customer", "val": "x"}], "has no `val`"),
            "neither value nor values": ([{"name": "Customer"}], "say what to set"),
            "both value and values": (
                [{"name": "Reviewers", "value": "Bob", "values": ["Bob"]}],
                "not both",
            ),
        }
        task = self._task("t", custom_field_values={self._fid(self.customer): "Acme"})
        for label, (bad, message) in cases.items():
            with self.subTest(label):
                self._refused_and_unchanged(task, message, custom_fields=bad)

    # -- each type's own rules --

    def test_text_must_be_text_and_within_the_limit(self):
        task = self._task("t")
        for bad in (7, True, ["a"], {"a": 1}):
            with self.subTest(bad=bad):
                self._refused_and_unchanged(
                    task, "must be text", custom_fields=[{"name": "Customer", "value": bad}]
                )
        self._refused_and_unchanged(
            task, "the limit is 4000", custom_fields=[{"name": "Customer", "value": "x" * 4001}]
        )
        self._set(task, custom_fields=[{"name": "Customer", "value": "x" * 4000}])
        self.assertEqual(len(self._values(task)[self._fid(self.customer)]), 4000)

    def test_values_belongs_to_a_tag_field_only(self):
        self._refused_and_unchanged(
            self._task("t"),
            "only a tag field takes `values`",
            custom_fields=[{"name": "Customer", "values": ["a"]}],
        )

    def test_a_tag_field_takes_labels_ids_and_one_label_as_value(self):
        task = self._task("t")

        self._set(
            task, custom_fields=[{"name": "Reviewers", "values": ["alice", "opt-b", "Alice"]}]
        )
        self.assertEqual(self._values(task)[self._fid(self.reviewers)], ["opt-a", "opt-b"])

        self._set(task, custom_fields=[{"name": "Reviewers", "value": "Bob"}])
        self.assertEqual(self._values(task)[self._fid(self.reviewers)], ["opt-b"])

    def test_a_tag_option_that_does_not_exist_is_refused_with_the_options(self):
        message = self._refused_and_unchanged(
            self._task("t"),
            "not an option of the tag field 'Reviewers'",
            custom_fields=[{"name": "Reviewers", "values": ["Alice", "Zed"]}],
        )

        self.assertIn("'Zed'", message)
        self.assertIn("'Alice', 'Bob'", message)

    def test_a_tag_field_refuses_values_of_the_wrong_type(self):
        for bad in ([1], {"a": 1}, 5):
            with self.subTest(bad=bad):
                self._refused_and_unchanged(
                    self._task("t"),
                    "takes `values`",
                    custom_fields=[{"name": "Reviewers", "values": bad}],
                )

    def test_a_date_must_be_a_real_iso_date(self):
        task = self._task("t")
        for bad in ("11/20/2026", "20261120", "2026-W47-5", "2026-02-30", "soon"):
            with self.subTest(bad=bad):
                self._refused_and_unchanged(
                    task, "YYYY-MM-DD", custom_fields=[{"name": "Review date", "value": bad}]
                )
        self._refused_and_unchanged(
            task, "must be text", custom_fields=[{"name": "Review date", "value": 20261120}]
        )
        self._set(task, custom_fields=[{"name": "Review date", "value": "2020-01-02"}])
        self.assertEqual(self._values(task)[self._fid(self.review_date)], "2020-01-02")

    def test_a_member_must_be_someone_who_may_be_on_the_task(self):
        task = self._task("t")
        for bad, why in (
            (str(self.outsider.id), "not a member"),
            ("12345678-1234-5678-1234-567812345678", "not a member"),
            ("alice", "not a user id"),
        ):
            with self.subTest(bad=bad):
                self._refused_and_unchanged(
                    task, why, custom_fields=[{"name": "QA owner", "value": bad}]
                )
        self._set(task, custom_fields=[{"name": "QA owner", "value": str(self.guest.id)}])
        self.assertEqual(self._values(task)[self._fid(self.qa)], str(self.guest.id))

    # -- a refused call changes nothing --

    def test_one_bad_entry_refuses_the_whole_call_even_with_other_valid_changes(self):
        task = self._task("t")
        before = self._snapshot(task)

        with self.assertRaises(ToolError):
            self._set(
                task,
                title="renamed",
                start_date="2026-11-10",
                collaborators=[str(self.user2.id)],
                custom_fields=[
                    {"name": "Customer", "value": "Acme"},
                    {"name": "Review date", "value": "not a date"},
                ],
            )

        self.assertEqual(self._snapshot(task), before)

    def test_a_bad_collaborator_refuses_valid_custom_fields_sent_with_it(self):
        task = self._task("t")
        before = self._snapshot(task)

        with self.assertRaises(ToolError):
            self._set(
                task,
                collaborators=[str(self.outsider.id)],
                custom_fields=[{"name": "Customer", "value": "Acme"}],
            )

        self.assertEqual(self._snapshot(task), before)

    def test_all_of_it_together_is_applied(self):
        task = self._task("t")

        out = self._set(
            task,
            title="renamed",
            start_date="2026-11-10",
            collaborators=[str(self.user2.id)],
            custom_fields=[{"name": "Customer", "value": "Acme"}],
        )

        task.refresh_from_db()
        self.assertEqual(task.title, "renamed")
        self.assertEqual(task.start_date, date(2026, 11, 10))
        self.assertEqual(self._values(task), {self._fid(self.customer): "Acme"})
        self.assertEqual(
            sorted(out["changed_fields"]),
            sorted(["title", "start_date", "custom_fields", "collaborators"]),
        )

    def test_a_task_can_hold_at_most_the_stored_limit_of_values(self):
        task = self._task("t", custom_field_values={str(100000 + i): "orphan" for i in range(100)})

        self._refused_and_unchanged(
            task,
            "at most 100 custom values",
            custom_fields=[{"name": "Customer", "value": "Acme"}],
        )

    def test_a_project_move_in_the_same_call_is_refused(self):
        other = self._other_project(member=True)
        for extra in (
            {"custom_fields": [{"name": "Customer", "value": "x"}]},
            {"collaborators": [str(self.user2.id)]},
        ):
            with self.subTest(extra=list(extra)):
                self._refused_and_unchanged(
                    self._task("t"),
                    "move the task with `project_id` first",
                    project_id=other.project_id,
                    **extra,
                )

    # -- a milestone's backing row --

    def test_a_milestones_custom_fields_are_written_to_its_backing_row_as_the_milestone_view_does(
        self,
    ):
        tool_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="MS tool", reporter=self.user
        )
        ui_ms = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="MS ui", reporter=self.user
        )
        tool_backing, ui_backing = ensure_backing_task(tool_ms), ensure_backing_task(ui_ms)

        self._set(
            tool_backing,
            custom_fields=[{"name": "Customer", "value": "Acme"}],
            title="MS tool renamed",
        )
        self.authenticate()
        res = self.client.patch(
            reverse("milestone_detail", args=[ui_ms.milestone_id]),
            {"custom_field_values": {self._fid(self.customer): "Acme"}, "title": "MS ui renamed"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(self._values(tool_backing), self._values(ui_backing))
        self.assertEqual(self._values(tool_backing), {self._fid(self.customer): "Acme"})
        tool_ms.refresh_from_db()
        self.assertEqual(tool_ms.title, "MS tool renamed")

    def test_custom_fields_alone_on_a_milestone_survive_the_next_milestone_sync(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="MS", reporter=self.user
        )
        backing = ensure_backing_task(milestone)

        self._set(backing, custom_fields=[{"name": "Customer", "value": "Acme"}])
        self._set(backing, title="renamed")

        self.assertEqual(self._values(backing), {self._fid(self.customer): "Acme"})

    # -- history --

    def test_one_history_row_per_changed_field(self):
        task = self._task("t", custom_field_values={self._fid(self.customer): "Acme"})

        self._set(
            task,
            custom_fields=[
                {"name": "Customer", "value": "Globex"},
                {"name": "Review date", "value": "2026-11-20"},
            ],
        )

        rows = _activity(task, TaskActivityActionType.CUSTOM_FIELD)
        self.assertEqual(
            {(r[3]["fieldName"], r[1], r[2]) for r in rows},
            {("Customer", "Acme", "Globex"), ("Review date", None, "2026-11-20")},
        )


class CreateTaskFieldTests(WriteBase):
    def _create(self, **extra):
        return CREATE_TASK.run(
            {"title": "New", "project_id": self.project.project_id, **extra}, self.ctx
        )

    def _snap(self, task):
        task.refresh_from_db()
        return (
            task.start_date,
            task.custom_field_values,
            sorted(str(u) for u in task.collaborators.values_list("id", flat=True)),
        )

    def test_a_created_task_matches_the_uis_post(self):
        member = str(self.user2.id)
        out = self._create(
            start_date="2026-11-10",
            collaborators=[member, str(self.guest.id)],
            custom_fields=[
                {"name": "Customer", "value": "Acme"},
                {"name": "Reviewers", "values": ["Bob"]},
                {"name": "QA owner", "value": member},
            ],
        )
        self.authenticate()
        res = self.client.post(
            "/api/v2/task/",
            {
                "project": self.project.project_id,
                "assignee": None,
                "reporter": str(self.user.id),
                "title": "New via ui",
                "priority": None,
                "effort_level": None,
                "status": "Open",
                "content": None,
                "due_date": None,
                "start_date": "2026-11-10",
                "links": [],
                "tags": [],
                "is_init_task": False,
                "collaborators": [member, str(self.guest.id)],
                "custom_field_values": {
                    self._fid(self.customer): "Acme",
                    self._fid(self.reviewers): ["opt-b"],
                    self._fid(self.qa): member,
                },
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        by_tool = TaskMaster.objects.get(task_id=out["task_id"])
        by_ui = TaskMaster.objects.get(task_id=res.data["task"]["task_id"])
        self.assertEqual(self._snap(by_tool), self._snap(by_ui))
        self.assertEqual(out["start_date"], "2026-11-10")
        self.assertEqual(sorted(out["collaborators"]), sorted([member, str(self.guest.id)]))

    def _fid(self, field):
        return str(field.field_id)

    def test_nothing_is_created_when_any_of_it_is_refused(self):
        before = TaskMaster.objects.count()
        cases = {
            "start_date": {"start_date": "tomorrow"},
            "collaborators": {"collaborators": [str(self.outsider.id)]},
            "custom_fields": {"custom_fields": [{"name": "Budget", "value": "1"}]},
            "a bad value": {"custom_fields": [{"name": "Review date", "value": "soon"}]},
        }
        for label, extra in cases.items():
            with self.subTest(label), self.assertRaises(ToolError):
                self._create(**extra)
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_a_task_created_without_them_has_none(self):
        out = self._create()

        task = TaskMaster.objects.get(task_id=out["task_id"])
        self.assertIsNone(task.start_date)
        self.assertEqual(self._snap(task)[2], [])
        self.assertIsNone(out["start_date"])
        self.assertEqual(out["collaborators"], [])

    def test_an_empty_custom_fields_list_stores_nothing(self):
        out = self._create(custom_fields=[])

        self.assertFalse(TaskMaster.objects.get(task_id=out["task_id"]).custom_field_values)


class BulkStartDateAndTagsTests(WriteBase):
    def setUp(self):
        super().setUp()
        self.bug = self._tag("bug")
        self.ui = self._tag("ui")

    def _bulk(self, *rows):
        return UPDATE_TASKS_BULK.run(
            {"updates": [{"rationale": "because", **row} for row in rows]}, self.ctx
        )

    def _stored_tags(self, *names):
        return [{"tagName": n, "tagColor": "red", "tagTextColor": "white"} for n in names]

    def test_start_date_matches_the_uis_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")

        out = self._bulk({"task_id": by_tool.task_id, "start_date": "2026-11-10"})
        self._ui_put(by_ui, start_date="2026-11-10")

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()
        self.assertEqual(by_tool.start_date, by_ui.start_date)
        self.assertEqual(out["updated"][0]["changed_fields"], ["start_date"])
        self.assertEqual(
            _activity(by_tool, TaskActivityActionType.START_DATE),
            _activity(by_ui, TaskActivityActionType.START_DATE),
        )

    def test_tags_match_the_uis_put(self):
        by_tool, by_ui = self._task("by tool"), self._task("by ui")

        out = self._bulk({"task_id": by_tool.task_id, "tags": ["bug", "ui"]})
        self._ui_put(by_ui, tags=self._stored_tags("bug", "ui"))

        by_tool.refresh_from_db()
        by_ui.refresh_from_db()
        self.assertEqual(
            sorted(t["tagName"] for t in by_tool.tags), sorted(t["tagName"] for t in by_ui.tags)
        )
        self.assertEqual(out["updated"][0]["changed_fields"], ["tags"])
        self.assertEqual(
            len(_activity(by_tool, TaskActivityActionType.TAGS)),
            len(_activity(by_ui, TaskActivityActionType.TAGS)),
        )

    def test_clearing_start_date_and_tags(self):
        task = self._task("t", start_date=date(2026, 11, 1), tags=self._stored_tags("bug"))

        out = self._bulk({"task_id": task.task_id, "start_date": "", "tags": []})

        task.refresh_from_db()
        self.assertIsNone(task.start_date)
        self.assertIsNone(task.tags)
        self.assertEqual(
            sorted(out["updated"][0]["changed_fields"]), ["start_date(cleared)", "tags(cleared)"]
        )

    def test_the_same_values_are_no_ops_and_tag_order_does_not_matter(self):
        task = self._task("t", start_date=date(2026, 11, 1), tags=self._stored_tags("bug", "ui"))

        out = self._bulk(
            {"task_id": task.task_id, "start_date": "2026-11-01", "tags": ["ui", "bug"]}
        )

        self.assertEqual(out["updated"], [])
        self.assertEqual(out["noops"], [task.task_id])

    def test_one_bad_row_applies_nothing_and_names_every_offender(self):
        good = self._task("good")
        bad_date = self._task("bad date")
        bad_tag = self._task("bad tag")

        with self.assertRaises(ToolError) as caught:
            self._bulk(
                {"task_id": good.task_id, "start_date": "2026-11-10", "tags": ["bug"]},
                {"task_id": bad_date.task_id, "start_date": "11/10/2026"},
                {"task_id": bad_tag.task_id, "tags": ["nope"]},
            )

        message = str(caught.exception)
        self.assertIn("updates[1]", message)
        self.assertIn("YYYY-MM-DD", message)
        self.assertIn("updates[2]", message)
        self.assertIn("unknown tag(s) 'nope'", message)
        self.assertIn("'bug'", message)
        self.assertIn("'ui'", message)
        good.refresh_from_db()
        self.assertIsNone(good.start_date)
        self.assertIsNone(good.tags)

    def test_a_tag_must_be_in_the_tasks_own_project(self):
        other = self._other_project(member=True)
        elsewhere = self._task("elsewhere", project=other)

        with self.assertRaises(ToolError) as caught:
            self._bulk({"task_id": elsewhere.task_id, "tags": ["bug"]})

        self.assertIn("unknown tag(s) 'bug'", str(caught.exception))

    def test_a_row_with_only_these_fields_is_not_called_empty(self):
        task = self._task("t")

        out = self._bulk({"task_id": task.task_id, "tags": ["bug"]})

        self.assertEqual(len(out["updated"]), 1)

    def test_a_row_with_nothing_to_change_names_the_new_fields_in_the_error(self):
        task = self._task("t")

        with self.assertRaises(ToolError) as caught:
            UPDATE_TASKS_BULK.run(
                {"updates": [{"task_id": task.task_id, "rationale": "x"}]}, self.ctx
            )

        self.assertIn("start_date / tags", str(caught.exception))

    def test_a_milestones_backing_row_is_mirrored_onto_the_milestone(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="MS", reporter=self.user
        )
        backing = ensure_backing_task(milestone)

        self._bulk({"task_id": backing.task_id, "start_date": "2026-11-10", "tags": ["bug"]})

        milestone.refresh_from_db()
        backing.refresh_from_db()
        self.assertEqual(milestone.start_date, date(2026, 11, 10))
        self.assertEqual(backing.start_date, date(2026, 11, 10))
        self.assertEqual([t["tagName"] for t in milestone.tags], ["bug"])
        self.assertEqual([t["tagName"] for t in backing.tags], ["bug"])

    def test_the_summary_counts_the_new_fields(self):
        a, b = self._task("a"), self._task("b")

        out = self._bulk(
            {"task_id": a.task_id, "start_date": "2026-11-10", "tags": ["bug"]},
            {"task_id": b.task_id, "start_date": "2026-11-11"},
        )

        self.assertIn("2 start_date", out["__summary__"])
        self.assertIn("1 tags", out["__summary__"])
