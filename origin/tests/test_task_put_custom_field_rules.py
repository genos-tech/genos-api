"""The task PUT judges custom field values by what they mean (MIP-913).

`sanitize_custom_field_values` only checks SHAPE, so the PUT stored any string
for a date, tag or member field and silently dropped numbers and booleans, while
the agent tools refuse what a picker could not produce (a tag option the field
doesn't have, a date that isn't `YYYY-MM-DD`, a person who may not be on the
task). The rules now live in `services/custom_fields` and the PUT applies them
to values that CHANGED.

"Changed" is the point. The app re-sends the whole value map on every save, so
an option deleted since, or a person who has left the team since, must not fail
every later save of the task.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.urls import reverse
from rest_framework import status

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectCustomField, ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.update_task import UPDATE_TASK

from .test_base import BaseAPITestCase

User = get_user_model()


class CustomFieldRuleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Fields",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.guest = User.objects.create_user(
            username="guest", email="guest@example.com", password="guestpass123"
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.guest)
        self.outsider = User.objects.create_user(
            username="outsider", email="outsider@example.com", password="outsiderpass123"
        )
        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=elsewhere, attendee=self.outsider)

        self.text = self._field("Customer", "text", 0)
        self.date = self._field("Review date", "date", 1)
        self.member = self._field("QA owner", "member", 2)
        self.tag = self._field(
            "Area",
            "tag",
            3,
            options=[
                {"id": "opt-ui", "label": "UI", "color": "#111"},
                {"id": "opt-api", "label": "API", "color": "#222"},
            ],
        )
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Task",
            status="Open",
            assignee=self.user,
            reporter=self.user,
        )
        self.authenticate(self.user)

    def _field(self, name, kind, order, **kwargs):
        return ProjectCustomField.objects.create(
            team=self.team,
            project=self.project,
            field_name=name,
            field_type=kind,
            sort_order=order,
            **kwargs,
        )

    @staticmethod
    def key(field):
        return str(field.field_id)

    def put(self, values, **extra):
        return self.client.put(
            reverse("task_create"),
            {"task_id": self.task.task_id, "custom_field_values": values, **extra},
            format="json",
        )

    def stored(self):
        return TaskMaster.objects.get(task_id=self.task.task_id).custom_field_values or {}

    def set_stored(self, values):
        TaskMaster.objects.filter(pk=self.task.pk).update(custom_field_values=values)

    def assertRefused(self, res, field_name):
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)
        self.assertIn(field_name, res.data["error"])

    # ---- what a picker could not produce is refused ---------------------

    def test_a_date_that_is_not_a_real_iso_date_is_refused(self):
        for bad in ("11/20/2026", "20261120", "2026-W47-5", "2026-02-30", "soon"):
            with self.subTest(bad=bad):
                self.assertRefused(self.put({self.key(self.date): bad}), "Review date")
        self.assertEqual(self.stored(), {})

    def test_a_real_date_is_accepted(self):
        res = self.put({self.key(self.date): "2026-11-20"})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {self.key(self.date): "2026-11-20"})

    def test_a_tag_option_the_field_does_not_have_is_refused(self):
        res = self.put({self.key(self.tag): ["opt-ui", "opt-zzz"]})
        self.assertRefused(res, "Area")
        self.assertIn("opt-zzz", res.data["error"])
        self.assertEqual(self.stored(), {})

    def test_the_fields_own_options_are_accepted(self):
        res = self.put({self.key(self.tag): ["opt-ui", "opt-api"]})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored()[self.key(self.tag)], ["opt-ui", "opt-api"])

    def test_a_member_who_may_not_be_on_the_task_is_refused(self):
        for bad in (str(self.outsider.id), "12345678-1234-5678-1234-567812345678", "alice"):
            with self.subTest(bad=bad):
                self.assertRefused(self.put({self.key(self.member): bad}), "QA owner")
        self.assertEqual(self.stored(), {})

    def test_teammates_and_project_only_guests_are_accepted_as_members(self):
        for person in (self.user2, self.guest):
            with self.subTest(person=person.username):
                res = self.put({self.key(self.member): str(person.id)})
                self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
                self.assertEqual(self.stored()[self.key(self.member)], str(person.id))

    def test_a_member_who_deleted_their_account_is_refused(self):
        User.objects.filter(pk=self.user2.pk).update(is_deleted=True)
        self.assertRefused(self.put({self.key(self.member): str(self.user2.id)}), "QA owner")

    def test_what_sanitizing_would_drop_silently_is_refused_instead(self):
        for bad in (20261120, True, {"a": 1}, [1, 2], ["opt-ui", 3]):
            with self.subTest(bad=bad):
                res = self.put({self.key(self.date): bad})
                self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST, res.data)

    def test_junk_that_names_no_field_of_the_project_is_still_just_dropped(self):
        res = self.put({"junk-number": 42, "junk-mixed": ["fine", 3], self.key(self.text): "ok"})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {self.key(self.text): "ok", "junk-mixed": ["fine"]})

    def test_a_list_for_a_single_value_field_and_text_for_a_tag_are_refused(self):
        self.assertRefused(self.put({self.key(self.text): ["a"]}), "Customer")
        self.assertRefused(self.put({self.key(self.tag): "opt-ui"}), "Area")

    def test_a_refused_value_changes_nothing_else_in_the_save(self):
        res = self.put({self.key(self.date): "soon"}, title="renamed")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Task")

    # ---- an old value that did not change never fails a save -------------

    def test_a_value_that_is_unchanged_passes_however_old_and_bad(self):
        old = {
            self.key(self.date): "garbage",
            self.key(self.member): str(self.outsider.id),
            self.key(self.tag): ["opt-gone"],
        }
        self.set_stored(old)

        res = self.put({**old, self.key(self.text): "Acme"}, title="renamed")

        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {**old, self.key(self.text): "Acme"})

    def test_a_tag_list_keeping_a_deleted_option_may_add_a_valid_one_but_not_a_bad_one(self):
        self.set_stored({self.key(self.tag): ["opt-gone"]})

        added = self.put({self.key(self.tag): ["opt-gone", "opt-ui"]})
        self.assertEqual(added.status_code, status.HTTP_200_OK, added.data)
        self.assertEqual(self.stored()[self.key(self.tag)], ["opt-gone", "opt-ui"])

        self.assertRefused(self.put({self.key(self.tag): ["opt-gone", "opt-zzz"]}), "Area")

    def test_a_bad_stored_value_can_be_replaced_or_cleared(self):
        self.set_stored({self.key(self.date): "garbage"})
        self.assertRefused(self.put({self.key(self.date): "also garbage"}), "Review date")
        self.assertEqual(
            self.put({self.key(self.date): "2026-11-20"}).status_code, status.HTTP_200_OK
        )
        self.set_stored({self.key(self.date): "garbage", self.key(self.member): "x"})
        res = self.put({self.key(self.date): None, self.key(self.member): ""})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {})

    def test_changing_a_member_field_that_holds_a_non_id_does_not_500(self):
        # The old PUT stored any string there; the history row looks the person
        # up, and one malformed id in the lookup used to raise.
        self.set_stored({self.key(self.member): "not-an-id"})
        res = self.put({self.key(self.member): str(self.guest.id)})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        res = self.put({self.key(self.member): None})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {})

    def test_a_value_for_a_field_the_project_does_not_have_is_ignored_as_before(self):
        res = self.put({"999999": "whatever", self.key(self.text): "Acme"})
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored()["999999"], "whatever")

    def test_an_absent_key_leaves_the_stored_map_alone(self):
        self.set_stored({self.key(self.date): "garbage"})
        res = self.client.put(
            reverse("task_create"), {"task_id": self.task.task_id, "title": "x"}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        self.assertEqual(self.stored(), {self.key(self.date): "garbage"})

    # ---- the create path holds the same line -----------------------------

    def test_creating_a_task_with_a_bad_value_is_refused(self):
        payload = {
            "team": self.team.team_id,
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "New",
            "priority": "High",
            "effort_level": "Low",
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        bad = self.client.post(
            reverse("task_create"),
            {**payload, "custom_field_values": {self.key(self.date): "soon"}},
            format="json",
        )
        self.assertRefused(bad, "Review date")
        self.assertFalse(TaskMaster.objects.filter(title="New").exists())

        good = self.client.post(
            reverse("task_create"),
            {**payload, "custom_field_values": {self.key(self.date): "2026-11-20"}},
            format="json",
        )
        self.assertEqual(good.status_code, status.HTTP_201_CREATED, good.data)

    # ---- the tools and the PUT agree --------------------------------------

    def test_the_tools_refuse_exactly_what_the_put_refuses(self):
        """One table of values, by name for the tool and by id for the PUT."""
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        bad = [
            (self.date, "20261120", "20261120"),
            (self.date, "soon", "soon"),
            (self.member, str(self.outsider.id), str(self.outsider.id)),
            (self.tag, ["Nope"], ["opt-nope"]),
        ]
        for field, by_name, by_id in bad:
            with self.subTest(field=field.field_name, value=by_name):
                key = "values" if field.field_type == "tag" else "value"
                with self.assertRaises(ToolError):
                    UPDATE_TASK.run(
                        {
                            "task_id": self.task.task_id,
                            "custom_fields": [{"name": field.field_name, key: by_name}],
                        },
                        ctx,
                    )
                self.assertRefused(self.put({self.key(field): by_id}), field.field_name)
        self.assertEqual(self.stored(), {})
