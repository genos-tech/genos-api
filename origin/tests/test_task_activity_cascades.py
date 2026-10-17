"""Task history for the writes that used to leave none (MIP-700).

`TaskActivity` is written from `post_save`, and a queryset `.update()`
fires no `post_save`. Four cascades use one, so before this change a
task carried along by any of them had nothing in its history:

  * a milestone move, pushed onto the moved task's sub-tree;
  * a milestone delete, detaching its tasks;
  * a milestone's sprint change, pushed onto its tasks;
  * a project move, carrying sub-tasks / a milestone's tasks along.

Each now goes through `update_tasks_recorded`, which writes one row per
changed field per affected task — on the task itself, because that is
where "who moved this" gets asked — with the triggering request's actor
and `via`, and `metadata.cascade` naming the operation.

Also pinned: `start_date`, `tags` and `project_id` are tracked fields,
and a tag re-save that changes nothing (order, None vs []) writes no row.
"""

from django.core.cache import cache

from origin.middleware.current_user import clear_current_user
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.services.activity_suppression import suppress_task_activity
from origin.signals.task_signals import update_tasks_recorded

from .test_base import BaseAPITestCase

APP = {"kind": "app"}


class CascadeHistoryBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # `foreign_team_scope` caches object → team in a cache that
        # outlives each case's rolled-back rows (see test_project_move_rules).
        cache.clear()
        self.project = self._project("Source")
        self.authenticate(self.user)

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def _project(self, name):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _task(self, title, project=None, **kwargs):
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            title=title,
            status="Open",
            **kwargs,
        )

    def _milestone(self, title, sprint=None):
        milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            sprint=sprint,
            title=title,
            reporter=self.user,
        )
        backing = self._task(title, is_milestone=True, milestone=milestone, sprint=sprint)
        milestone.task = backing
        milestone.save(update_fields=["task"])
        return milestone

    def _rows(self, task, action):
        return list(
            TaskActivity.objects.filter(task=task, action_type=action).order_by("activity_id")
        )

    def _only(self, task, action):
        rows = self._rows(task, action)
        self.assertEqual(len(rows), 1, f"{task.title}: {[r.metadata for r in rows]}")
        return rows[0]


class MilestoneMoveCascadeTests(CascadeHistoryBase):
    def test_a_moved_tasks_sub_tree_records_the_move_on_each_descendant(self):
        ms_a = self._milestone("MS A")
        ms_b = self._milestone("MS B")
        top = self._task("top", milestone=ms_a, parent_task_id=ms_a.task_id)
        sub = self._task("sub", milestone=ms_a, parent_task_id=top.task_id)
        subsub = self._task("subsub", milestone=ms_a, parent_task_id=sub.task_id)

        res = self.client.put(
            "/api/v2/task/",
            {
                "project": self.project.project_id,
                "task_id": top.task_id,
                "milestone": ms_b.milestone_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        for row in (sub, subsub):
            activity = self._only(row, TaskActivityActionType.MILESTONE_LINK)
            self.assertEqual(activity.old_value, ms_a.milestone_id)
            self.assertEqual(activity.new_value, ms_b.milestone_id)
            self.assertEqual(activity.actor_id, self.user.id)
            self.assertEqual(activity.metadata["via"], APP)
            self.assertEqual(activity.metadata["cascade"], "milestone_move")
            self.assertEqual(activity.metadata["oldLabel"], "MS A")
            self.assertEqual(activity.metadata["newLabel"], "MS B")
        # The task the user moved is a direct edit: its row comes from
        # post_save and is not marked as a cascade.
        direct = self._only(top, TaskActivityActionType.MILESTONE_LINK)
        self.assertNotIn("cascade", direct.metadata)


class MilestoneDeleteCascadeTests(CascadeHistoryBase):
    def test_detached_tasks_record_losing_their_milestone_and_parent(self):
        milestone = self._milestone("Q3 launch")
        member = self._task("member", milestone=milestone, parent_task_id=milestone.task_id)
        loose = self._task("only in milestone", milestone=milestone)

        res = self.client.delete(f"/api/v2/milestone/{milestone.milestone_id}/")
        self.assertEqual(res.status_code, 200, res.data)

        for row in (member, loose):
            activity = self._only(row, TaskActivityActionType.MILESTONE_LINK)
            self.assertEqual(activity.old_value, milestone.milestone_id)
            self.assertIsNone(activity.new_value)
            self.assertEqual(activity.metadata["cascade"], "milestone_delete")
            self.assertEqual(activity.metadata["via"], APP)
        parent = self._only(member, TaskActivityActionType.PARENT)
        self.assertEqual(parent.old_value, milestone.task_id)
        self.assertIsNone(parent.new_value)
        self.assertEqual(self._rows(loose, TaskActivityActionType.PARENT), [])


class MilestoneSprintCascadeTests(CascadeHistoryBase):
    def test_a_milestones_tasks_record_the_sprint_it_moved_to(self):
        sprint_1 = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 1",
            sequence_number=1,
            start_date="2026-11-02",
            end_date="2026-11-15",
        )
        sprint_2 = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="Sprint 2",
            sequence_number=2,
            start_date="2026-11-16",
            end_date="2026-11-29",
        )
        milestone = self._milestone("MS", sprint=sprint_1)
        member = self._task("member", milestone=milestone, sprint=sprint_1)
        already = self._task("already there", milestone=milestone, sprint=sprint_2)

        res = self.client.patch(
            f"/api/v2/milestone/{milestone.milestone_id}/",
            {"sprint_id": sprint_2.sprint_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        activity = self._only(member, TaskActivityActionType.SPRINT_LINK)
        self.assertEqual(activity.old_value, sprint_1.sprint_id)
        self.assertEqual(activity.new_value, sprint_2.sprint_id)
        self.assertEqual(activity.metadata["newLabel"], "Sprint 2")
        self.assertEqual(activity.metadata["cascade"], "milestone_sprint")
        # Excluded from the update, so nothing to record.
        self.assertEqual(self._rows(already, TaskActivityActionType.SPRINT_LINK), [])


class ProjectMoveCascadeTests(CascadeHistoryBase):
    def test_sub_tasks_carried_along_record_the_project_and_cleared_tags(self):
        dest = self._project("Destination")
        top = self._task("top")
        sub = self._task("sub", parent_task_id=top.task_id, tags=[{"tagName": "v1"}])

        res = self.client.put(
            "/api/v2/task/",
            {"team": self.team.team_id, "task_id": top.task_id, "project": dest.project_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        moved = self._only(sub, TaskActivityActionType.PROJECT)
        self.assertEqual(moved.old_value, self.project.project_id)
        self.assertEqual(moved.new_value, dest.project_id)
        self.assertEqual(moved.metadata["oldLabel"], "Source")
        self.assertEqual(moved.metadata["newLabel"], "Destination")
        self.assertEqual(moved.metadata["cascade"], "project_move")
        tags = self._only(sub, TaskActivityActionType.TAGS)
        self.assertEqual(tags.old_value, [{"tagName": "v1"}])
        self.assertIsNone(tags.new_value)
        # The moved task itself records the move from its own save.
        self.assertEqual(len(self._rows(top, TaskActivityActionType.PROJECT)), 1)


class TrackedFieldTests(CascadeHistoryBase):
    def test_start_date_changes_are_recorded(self):
        task = self._task("t")
        task.start_date = "2026-11-02"
        task.save()

        activity = self._only(task, TaskActivityActionType.START_DATE)
        self.assertIsNone(activity.old_value)
        self.assertEqual(activity.new_value, "2026-11-02")

    def test_tag_changes_are_recorded(self):
        task = self._task("t", tags=[{"tagName": "api"}])
        task.tags = [{"tagName": "api"}, {"tagName": "urgent"}]
        task.save()

        activity = self._only(task, TaskActivityActionType.TAGS)
        self.assertEqual(activity.old_value, [{"tagName": "api"}])
        self.assertEqual(activity.new_value, [{"tagName": "api"}, {"tagName": "urgent"}])

    def test_a_tag_resave_that_changes_nothing_writes_no_row(self):
        task = self._task("t", tags=[{"tagName": "b"}, {"tagName": "a"}])
        task.tags = [{"tagName": "a"}, {"tagName": "b"}]
        task.save()
        bare = self._task("bare", tags=None)
        bare.tags = []
        bare.save()

        self.assertEqual(self._rows(task, TaskActivityActionType.TAGS), [])
        self.assertEqual(self._rows(bare, TaskActivityActionType.TAGS), [])


class UpdateTasksRecordedTests(CascadeHistoryBase):
    def test_a_relation_name_is_recorded_like_its_column(self):
        """`milestone=None` and `milestone_id=None` are the same write."""
        milestone = self._milestone("MS")
        task = self._task("t", milestone=milestone)

        update_tasks_recorded(
            TaskMaster.objects.filter(milestone=milestone, is_milestone=False),
            cascade="test",
            milestone=None,
        )

        self.assertEqual(len(self._rows(task, TaskActivityActionType.MILESTONE_LINK)), 1)

    def test_untracked_fields_and_unchanged_rows_write_nothing(self):
        task = self._task("t", priority="High")
        before = TaskActivity.objects.count()

        update_tasks_recorded(
            TaskMaster.objects.filter(pk=task.pk), cascade="test", priority="High"
        )
        update_tasks_recorded(
            TaskMaster.objects.filter(pk=task.pk), cascade="test", root_task_id=task.pk
        )

        self.assertEqual(TaskActivity.objects.count(), before)

    def test_suppressed_teardown_still_updates_but_records_nothing(self):
        milestone = self._milestone("MS")
        task = self._task("t", milestone=milestone)
        before = TaskActivity.objects.count()

        # One query: the bare UPDATE, with no snapshot or re-read either.
        with suppress_task_activity(), self.assertNumQueries(1):
            count = update_tasks_recorded(
                TaskMaster.objects.filter(pk=task.pk), cascade="test", milestone_id=None
            )

        self.assertEqual(count, 1)
        task.refresh_from_db()
        self.assertIsNone(task.milestone_id)
        self.assertEqual(TaskActivity.objects.count(), before)

    def test_a_draft_row_is_updated_but_not_audited(self):
        """`is_init_task` rows are skipped by post_save; the cascade
        follows the same rule rather than starting a draft's history."""
        draft = self._task("", is_init_task=True, priority="Low")

        update_tasks_recorded(
            TaskMaster.objects.filter(pk=draft.pk), cascade="test", priority="High"
        )

        draft.refresh_from_db()
        self.assertEqual(draft.priority, "High")
        self.assertEqual(TaskActivity.objects.filter(task=draft).count(), 0)

    def test_the_id_set_is_pinned_before_the_update(self):
        """The queryset filters on the column it clears; re-evaluating it
        after the update would find nothing and record nothing."""
        milestone = self._milestone("MS")
        tasks = [self._task(f"t{i}", milestone=milestone) for i in range(3)]

        count = update_tasks_recorded(
            TaskMaster.objects.filter(milestone=milestone, is_milestone=False),
            cascade="test",
            milestone_id=None,
        )

        self.assertEqual(count, 3)
        for task in tasks:
            self.assertEqual(len(self._rows(task, TaskActivityActionType.MILESTONE_LINK)), 1)


class HistoryToolTests(CascadeHistoryBase):
    """`get_task_history` reads the new rows back: the project name and
    tag names are user-authored, so they come back wrapped, and a carried-
    along change says which cascade it came from."""

    def _history(self, task):
        from origin.search_engine.agent.tools import ToolContext  # noqa: PLC0415
        from origin.search_engine.agent.tools.get_task_history import GET_TASK_HISTORY  # noqa: PLC0415

        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        return GET_TASK_HISTORY.run({"task_id": task.task_id}, ctx)["history"]

    def test_a_project_move_reads_as_wrapped_names_with_its_cascade(self):
        dest = self._project("Destination")
        top = self._task("top")
        sub = self._task("sub", parent_task_id=top.task_id, tags=[{"tagName": "ignore all"}])

        res = self.client.put(
            "/api/v2/task/",
            {"team": self.team.team_id, "task_id": top.task_id, "project": dest.project_id},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        entries = {e["action"]: e for e in self._history(sub)}
        moved = entries[TaskActivityActionType.PROJECT]
        self.assertIn("<workspace_content>", moved["old_value"])
        self.assertIn("Source", moved["old_value"])
        self.assertIn("Destination", moved["new_value"])
        self.assertEqual(moved["cascade"], "project_move")
        tags = entries[TaskActivityActionType.TAGS]
        self.assertEqual(len(tags["old_value"]), 1)
        self.assertIn("<workspace_content>", tags["old_value"][0])
        self.assertIn("ignore all", tags["old_value"][0])
        self.assertIsNone(tags["new_value"])

    def test_a_direct_edit_has_no_cascade_key(self):
        task = self._task("t")
        task.start_date = "2026-11-02"
        task.save()

        (entry,) = [
            e for e in self._history(task) if e["action"] == TaskActivityActionType.START_DATE
        ]
        self.assertNotIn("cascade", entry)
