"""Per-project statuses (MIP-2492): the built-in rows, the project endpoints, `custom_status` on
tasks and milestones, and that every automation which writes `status` keeps working.

The rule under test everywhere: `status` keeps its system values, and a custom status survives
only while its category equals that `status` (and its project is the row's project).
"""

from unittest import mock

from django.urls import reverse

from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectStatus
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services import task_ancestry
from origin.services.milestone_rollup import milestone_work_counts
from origin.services.milestone_service import create_milestone
from origin.services.project_statuses import ensure_project_statuses
from origin.services.webhook_enqueue import task_payload
from origin.signals.task_signals import update_tasks_recorded
from origin.tests.test_base import BaseAPITestCase

STATUS = TaskActivityActionType.STATUS
CUSTOM = TaskActivityActionType.CUSTOM_STATUS


class StatusTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self.make_project("Statuses", "ST")
        # self.user owns the project; self.user2 is a plain (viewer) member.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate()

    def make_project(self, name, code):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def url(self, project=None, status_id=None):
        pid = (project or self.project).project_id
        if status_id is None:
            return reverse("project_statuses", args=[pid])
        return reverse("project_status_detail", args=[pid, status_id])

    def builtin(self, key, project=None):
        ensure_project_statuses(project or self.project)
        return ProjectStatus.objects.get(project=project or self.project, builtin_key=key)

    def add(self, name, category, project=None, **extra):
        response = self.client.post(
            self.url(project), {"name": name, "category": category, **extra}, format="json"
        )
        self.assertEqual(response.status_code, 201, response.data)
        return ProjectStatus.objects.get(status_id=response.data["statusId"])

    def task(self, status="Open", custom=None, **extra):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=extra.pop("title", "T"),
            status=status,
            custom_status=custom,
            assignee=self.user,
            reporter=self.user,
            **extra,
        )

    def fresh(self, row):
        row.refresh_from_db()
        return row

    def rows(self, task, action):
        return list(TaskActivity.objects.filter(task=task, action_type=action))


class StatusListApiTests(StatusTestBase):
    def test_a_project_nobody_opened_has_no_rows(self):
        self.assertFalse(ProjectStatus.objects.filter(project=self.project).exists())

    def test_first_read_writes_the_built_ins_once(self):
        first = self.client.get(self.url())
        second = self.client.get(self.url())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data, second.data)
        self.assertEqual(
            [(r["name"], r["category"], r["isDefault"]) for r in first.data["statuses"]],
            [
                ("Open", "Open", True),
                ("In Review", "Open", False),
                ("WIP", "WIP", True),
                ("Blocked", "Blocked", True),
                ("Pending", "Pending", True),
                ("Closed", "Closed", True),
            ],
        )
        self.assertTrue(first.data["canManage"])

    def test_who_may_read_and_change(self):
        self.authenticate(self.user2)
        self.assertFalse(self.client.get(self.url()).data["canManage"])
        refused = self.client.post(self.url(), {"name": "X", "category": "Open"}, format="json")
        self.assertEqual(refused.status_code, 403)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).update(
            member_role="editor"
        )
        self.add("X", "Open")

    def test_a_stranger_sees_no_project(self):
        other = self.make_project("Mine only", "MO")
        self.authenticate(self.user2)
        for response in (
            self.client.get(self.url(other)),
            self.client.post(self.url(other), {"name": "X", "category": "Open"}, format="json"),
            self.client.delete(self.url(other, 1)),
        ):
            self.assertEqual(response.status_code, 404)
        self.assertFalse(ProjectStatus.objects.filter(project=other).exists())

    def test_another_projects_status_cannot_be_changed_through_mine(self):
        other = self.make_project("Other", "OT")
        theirs = self.builtin("WIP", other)
        response = self.client.patch(
            self.url(status_id=theirs.status_id), {"name": "Mine"}, format="json"
        )
        self.assertEqual(response.status_code, 404)

    def test_an_archived_project_is_read_only(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(archived_at="2026-01-01T00:00:00Z")
        self.assertEqual(self.client.get(self.url()).data["statuses"], [])
        refused = self.client.post(self.url(), {"name": "X", "category": "Open"}, format="json")
        self.assertGreaterEqual(refused.status_code, 400)
        self.assertFalse(ProjectStatus.objects.filter(project=self.project).exists())

    def test_create_keeps_the_list_grouped_by_category(self):
        self.add("Doing", "WIP")
        self.add("Ready", "Open", color="#aabbcc")
        names = [r["name"] for r in self.client.get(self.url()).data["statuses"]]
        self.assertEqual(
            names,
            ["Open", "In Review", "Ready", "WIP", "Doing", "Blocked", "Pending", "Closed"],
        )

    def test_create_refusals(self):
        self.add("Doing", "WIP")
        for body, code in (
            ({"name": "doing", "category": "Open"}, "name_taken"),
            ({"name": "Rejected", "category": "Closed"}, "name_reserved"),
            ({"name": "closed", "category": "Open"}, "name_reserved"),
            ({"name": "x" * 25, "category": "Open"}, "name_too_long"),
            ({"name": " ", "category": "Open"}, "name_required"),
            ({"name": "Gone", "category": "Deleted"}, "bad_category"),
            ({"name": "C", "category": "Open", "color": "url(x);"}, "bad_color"),
        ):
            response = self.client.post(self.url(), body, format="json")
            self.assertEqual((response.status_code, response.data["code"]), (400, code), body)

    def test_only_some_built_ins_can_be_turned_off_and_none_deleted(self):
        for key, allowed in (
            ("Open", False),
            ("In Review", True),
            ("WIP", True),
            ("Blocked", False),
            ("Pending", True),
            ("Closed", False),
        ):
            row = self.builtin(key)
            off = self.client.patch(
                self.url(status_id=row.status_id), {"archived": True}, format="json"
            )
            self.assertEqual(off.status_code, 200 if allowed else 400, key)
            self.assertEqual(self.fresh(row).archived_at is not None, allowed, key)
            gone = self.client.delete(self.url(status_id=row.status_id))
            self.assertEqual((gone.status_code, gone.data["code"]), (400, "builtin"), key)

    def test_turning_a_default_off_and_on_moves_the_default(self):
        wip = self.builtin("WIP")
        self.client.patch(self.url(status_id=wip.status_id), {"archived": True}, format="json")
        self.assertFalse(ProjectStatus.objects.filter(category="WIP", is_default=True).exists())
        self.client.patch(self.url(status_id=wip.status_id), {"archived": False}, format="json")
        self.assertTrue(self.fresh(wip).is_default)

    def test_one_default_per_category_and_it_returns_to_the_built_in(self):
        ready = self.add("Ready", "Open", is_default=True)
        self.assertFalse(self.builtin("Open").is_default)
        self.assertTrue(self.fresh(ready).is_default)
        self.client.delete(self.url(status_id=ready.status_id))
        self.assertTrue(self.builtin("Open").is_default)

    def test_rename_and_category_fixed(self):
        wip = self.builtin("WIP")
        url = self.url(status_id=wip.status_id)
        self.assertEqual(
            self.client.patch(url, {"name": "In Progress"}, format="json").status_code, 200
        )
        self.assertEqual(self.client.patch(url, {"name": "WIP"}, format="json").status_code, 200)
        moved = self.client.patch(url, {"category": "Open"}, format="json")
        self.assertEqual(moved.data["code"], "category_fixed")

    def test_reorder_stays_grouped(self):
        ready = self.add("Ready", "Open")
        response = self.client.put(self.url(), {"order": [ready.status_id]}, format="json")
        names = [r["name"] for r in response.data["statuses"]]
        self.assertEqual(names[:3], ["Ready", "Open", "In Review"])
        self.assertEqual(
            self.client.put(self.url(), {"order": "x"}, format="json").status_code, 400
        )

    def test_delete_in_use_needs_a_replacement_of_the_same_category(self):
        doing = self.add("Doing", "WIP")
        testing = self.add("Testing", "WIP")
        ready = self.add("Ready", "Open")
        task = self.task("WIP", doing)
        url = self.url(status_id=doing.status_id)

        refused = self.client.delete(url)
        self.assertEqual((refused.data["code"], refused.data["inUse"]), ("status_in_use", 1))
        wrong = self.client.delete(url, {"replacement_status_id": ready.status_id}, format="json")
        self.assertEqual(wrong.data["code"], "bad_replacement")

        done = self.client.delete(url, {"replacement_status_id": testing.status_id}, format="json")
        self.assertEqual(done.status_code, 200, done.data)
        self.assertEqual(self.fresh(task).custom_status_id, testing.status_id)
        self.assertEqual(task.status, "WIP")
        (row,) = self.rows(task, CUSTOM)
        self.assertEqual((row.metadata["oldLabel"], row.metadata["newLabel"]), ("Doing", "Testing"))
        self.assertEqual(self.rows(task, STATUS), [])


class TaskCustomStatusTests(StatusTestBase):
    def create_task(self, **overrides):
        payload = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "T",
            "priority": "Normal",
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

    def put(self, task, **body):
        return self.client.put(
            reverse("task_create"), {"task_id": task.task_id, **body}, format="json"
        )

    def test_create_with_a_custom_status(self):
        review = self.builtin("In Review")
        response = self.create_task(custom_status=review.status_id)
        self.assertEqual(response.status_code, 201, response.data)
        task = TaskMaster.objects.get(task_id=response.data["task"]["task_id"])
        self.assertEqual((task.status, task.custom_status_id), ("Open", review.status_id))

    def test_create_picking_a_status_picks_its_category(self):
        doing = self.add("Doing", "WIP")
        response = self.create_task(custom_status=doing.status_id)
        self.assertEqual(response.data["task"]["status"], "WIP", response.data)

    def test_put_picks_clears_and_ignores_a_restated_value(self):
        doing = self.add("Doing", "WIP")
        task = self.task()
        self.assertEqual(self.put(task, custom_status=doing.status_id).status_code, 200)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("WIP", doing.status_id))

        # A full save restates both: nothing changes.
        self.put(task, status="WIP", custom_status=doing.status_id, title="Renamed")
        self.assertEqual(self.fresh(task).custom_status_id, doing.status_id)

        # A status change with the old value still in the payload drops the label.
        self.put(task, status="Closed", custom_status=doing.status_id)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("Closed", None))

        self.put(task, custom_status=doing.status_id)
        self.put(task, custom_status=None)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("WIP", None))

    def test_put_without_the_key_keeps_the_label(self):
        review = self.builtin("In Review")
        task = self.task("Open", review)
        self.put(task, title="Only the title")
        self.assertEqual(self.fresh(task).custom_status_id, review.status_id)

    def test_put_refusals(self):
        doing = self.add("Doing", "WIP")
        other = self.make_project("Other", "OT")
        theirs = self.builtin("WIP", other)
        off = self.add("Old", "WIP")
        self.client.patch(self.url(status_id=off.status_id), {"archived": True}, format="json")
        task = self.task()
        for body, code in (
            ({"custom_status": theirs.status_id}, "bad_status"),
            ({"custom_status": "abc"}, "bad_status"),
            ({"custom_status": True}, "bad_status"),
            ({"custom_status": off.status_id}, "archived"),
            ({"custom_status": doing.status_id, "status": "Closed"}, "category_mismatch"),
        ):
            response = self.put(task, **body)
            self.assertEqual((response.status_code, response.data["code"]), (400, code), body)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("Open", None))

    def test_a_task_keeps_a_status_that_was_turned_off(self):
        review = self.builtin("In Review")
        task = self.task("Open", review)
        self.client.patch(self.url(status_id=review.status_id), {"archived": True}, format="json")
        self.assertEqual(self.put(task, custom_status=review.status_id, title="x").status_code, 200)
        self.assertEqual(self.fresh(task).custom_status_id, review.status_id)

    def test_moving_to_another_project_clears_it(self):
        review = self.builtin("In Review")
        task = self.task("Open", review)
        other = self.make_project("Other", "OT")
        response = self.put(task, project=other.project_id, custom_status=review.status_id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual((self.fresh(task).project_id, task.custom_status_id), (other.pk, None))

    def test_history_rows(self):
        review = self.builtin("In Review")
        doing = self.add("Doing", "WIP")
        task = self.task()

        self.put(task, custom_status=review.status_id)
        (row,) = self.rows(task, CUSTOM)
        self.assertEqual((row.old_value, row.new_value), (None, review.status_id))
        self.assertEqual(row.metadata["newLabel"], "In Review")
        self.assertEqual(self.rows(task, STATUS), [])

        # Category and label together: ONE status row, so stats count it once.
        self.put(task, custom_status=doing.status_id)
        (status_row,) = self.rows(task, STATUS)
        self.assertEqual((status_row.old_value, status_row.new_value), ("Open", "WIP"))
        self.assertEqual(status_row.metadata["customStatus"]["oldLabel"], "In Review")
        self.assertEqual(status_row.metadata["customStatus"]["newLabel"], "Doing")
        self.assertEqual(len(self.rows(task, CUSTOM)), 1)

    def test_a_partial_save_of_an_old_copy_records_no_clearing(self):
        review = self.builtin("In Review")
        task = self.task()
        stale = TaskMaster.objects.get(pk=task.pk)
        task.custom_status = review
        task.save(update_fields=["custom_status"])
        stale.title = "x"
        stale.save(update_fields=["title"])
        self.assertEqual(len(self.rows(task, CUSTOM)), 1)
        self.assertEqual(self.fresh(task).custom_status_id, review.status_id)

    def test_every_read_carries_the_id(self):
        review = self.builtin("In Review")
        parent = self.task(title="Parent")
        task = self.task(
            "Open",
            review,
            parent_task_id=parent.task_id,
            chat_id="c",
            thread_id="th",
        )
        team, pid = str(self.team.team_id), self.project.project_id
        reads = {
            "project": ("get_project_tasks", {}),
            "team": ("get_team_tasks", {}),
            "detail": ("get_task", {"task_id": task.task_id}),
            "children": ("child_tasks", {"current_task_id": parent.task_id}),
        }
        for name, (route, extra) in reads.items():
            data = self.client.get(
                reverse(route), {"team_id": team, "project_id": pid, **extra}
            ).data
            if name == "project":
                data = data["data"]["tasks"]
            rows = data
            found = [r for r in rows if str(r["id"]) == str(task.task_id)]
            self.assertEqual(found[0]["customStatusId"], review.status_id, name)

    def test_sub_tasks_sort_by_category_then_the_projects_order(self):
        parent = self.task(title="Parent")
        review = self.builtin("In Review")
        for title, status, custom in (
            ("closed", "Closed", None),
            ("review", "Open", review),
            ("plain", "Open", None),
            ("wip", "WIP", None),
        ):
            self.task(status, custom, title=title, parent_task_id=parent.task_id)
        query = {
            "team_id": str(self.team.team_id),
            "project_id": self.project.project_id,
            "current_task_id": parent.task_id,
        }
        titles = [r["title"] for r in self.client.get(reverse("child_tasks"), query).data]
        self.assertEqual(titles, ["plain", "review", "wip", "closed"])

    def test_webhook_payload(self):
        self.assertIsNone(task_payload(self.task())["custom_status"])
        review = self.builtin("In Review")
        payload = task_payload(self.task("Open", review))
        self.assertEqual(
            payload["custom_status"],
            {"id": review.status_id, "name": "In Review", "category": "Open"},
        )
        self.assertEqual(payload["status"], "Open")

    def test_a_row_that_skipped_save_is_not_reported(self):
        review = self.builtin("In Review")
        task = self.task("Open", review)
        TaskMaster.objects.filter(pk=task.pk).update(status="Closed")
        self.assertIsNone(task_payload(self.fresh(task))["custom_status"])


class AutomationsKeepWorkingTests(StatusTestBase):
    def depend(self, blocker, blocked):
        return TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=blocked, team=self.team, created_by=self.user
        )

    def close(self, task):
        task.status = "Closed"
        task.save(update_fields=["status", "ts_updated_at"])

    def test_auto_blocked_and_back_to_open(self):
        doing = self.add("Doing", "WIP")
        blocker, task = self.task(), self.task("WIP", doing)
        self.depend(blocker, task)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("Blocked", None))
        (row,) = self.rows(task, STATUS)
        self.assertEqual(row.metadata["customStatus"]["oldLabel"], "Doing")
        self.close(blocker)
        self.assertEqual((self.fresh(task).status, task.custom_status_id), ("Open", None))

    def test_a_blocker_with_a_custom_open_status_still_blocks(self):
        review = self.builtin("In Review")
        blocker, task = self.task("Open", review), self.task()
        self.depend(blocker, task)
        self.assertEqual(self.fresh(task).status, "Blocked")
        self.close(blocker)
        self.assertEqual(self.fresh(task).status, "Open")
        self.assertIsNone(self.fresh(blocker).custom_status_id)

    def test_a_milestone_is_blocked_with_its_backing_task(self):
        review = self.builtin("In Review")
        milestone = create_milestone(
            self.project, reporter_id=self.user.id, title="M", custom_status=review
        )
        backing = TaskMaster.objects.get(pk=milestone.task_id)
        self.assertEqual(backing.custom_status_id, review.status_id)
        self.depend(self.task(), backing)
        self.assertEqual(
            (self.fresh(milestone).status, milestone.custom_status_id), ("Blocked", None)
        )
        self.assertEqual((self.fresh(backing).status, backing.custom_status_id), ("Blocked", None))

    def test_closed_by_ancestor_reads_the_category(self):
        accepted = self.add("Accepted", "Closed")
        review = self.builtin("In Review")
        parent = self.task("Closed", accepted)
        child = self.task("Open", review, parent_task_id=parent.task_id)
        found = task_ancestry.effective_statuses([child.task_id], self.team.team_id)
        self.assertEqual(found[child.task_id].status, "Closed")
        # Nothing is written to the child.
        self.assertEqual(self.fresh(child).custom_status_id, review.status_id)

    def test_milestone_progress_counts_a_custom_closed_status(self):
        accepted = self.add("Accepted", "Closed")
        milestone = create_milestone(self.project, reporter_id=self.user.id, title="M")
        review = self.builtin("In Review")
        for status, custom in (("Closed", accepted), ("Open", review)):
            self.task(status, custom, milestone=milestone, parent_task_id=milestone.task_id)
        self.assertEqual(
            milestone_work_counts([milestone], ancestor_rule=True)[milestone.milestone_id], (2, 1)
        )

    def test_a_bulk_cascade_clears_what_no_longer_fits(self):
        review = self.builtin("In Review")
        task = self.task("Open", review)
        update_tasks_recorded(
            TaskMaster.objects.filter(pk=task.pk), cascade="test", status="Deleted"
        )
        self.assertIsNone(self.fresh(task).custom_status_id)
        (row,) = self.rows(task, STATUS)
        self.assertEqual(row.metadata["customStatus"]["oldLabel"], "In Review")

        other = self.make_project("Other", "OT")
        task = self.task("Open", review)
        update_tasks_recorded(
            TaskMaster.objects.filter(pk=task.pk), cascade="test", project_id=other.project_id
        )
        self.assertIsNone(self.fresh(task).custom_status_id)
        self.assertEqual(len(self.rows(task, CUSTOM)), 1)

    def test_the_close_notice_still_hears_a_close(self):
        accepted = self.add("Accepted", "Closed")
        task = self.task()
        with mock.patch("origin.services.task_close_notice.post_close_notice") as notice:
            task.status = "Closed"
            task.custom_status = accepted
            task.save()
        notice.assert_called_once()
        self.assertEqual(self.fresh(task).custom_status_id, accepted.status_id)


class MilestoneCustomStatusTests(StatusTestBase):
    def test_create_patch_and_mirror(self):
        review = self.builtin("In Review")
        doing = self.add("Doing", "WIP")
        created = self.client.post(
            "/api/v2/milestone/",
            {"project_id": self.project.project_id, "title": "M", "custom_status": review.pk},
            format="json",
        )
        self.assertEqual(created.status_code, 201, created.data)
        body = created.data["milestone"]
        self.assertEqual((body["status"], body["customStatusId"]), ("Open", review.status_id))
        backing = TaskMaster.objects.get(pk=body["taskId"])
        self.assertEqual(backing.custom_status_id, review.status_id)

        url = f"/api/v2/milestone/{body['milestoneId']}/"
        patched = self.client.patch(url, {"custom_status": doing.pk}, format="json")
        self.assertEqual(patched.status_code, 200, patched.data)
        self.assertEqual(patched.data["milestone"]["status"], "WIP")
        self.assertEqual(
            (self.fresh(backing).status, backing.custom_status_id), ("WIP", doing.status_id)
        )

        closed = self.client.patch(url, {"status": "Closed"}, format="json")
        self.assertIsNone(closed.data["milestone"]["customStatusId"])
        self.assertIsNone(self.fresh(backing).custom_status_id)

    def test_the_backing_task_is_not_the_place_to_set_it(self):
        review = self.builtin("In Review")
        milestone = create_milestone(self.project, reporter_id=self.user.id, title="M")
        response = self.client.put(
            reverse("task_create"),
            {"task_id": milestone.task_id, "custom_status": review.pk},
            format="json",
        )
        self.assertEqual(response.data["code"], "milestone_status")

    def test_deleting_a_milestone_clears_the_backing_task(self):
        review = self.builtin("In Review")
        milestone = create_milestone(
            self.project, reporter_id=self.user.id, title="M", custom_status=review
        )
        self.client.delete(f"/api/v2/milestone/{milestone.milestone_id}/")
        backing = TaskMaster.objects.get(pk=milestone.task_id)
        self.assertEqual((backing.status, backing.custom_status_id), ("Deleted", None))
