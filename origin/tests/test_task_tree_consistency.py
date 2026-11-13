"""A task's milestone follows where it hangs (MIP-1168).

MIP-598 sat under MIP-625 in the milestone "Genos secretary". Editing its
description in the app sent the whole task back, including the milestone and
parent of the browser's working copy. That copy was older than a move, so it
said "Improve Genos capability" next to the CURRENT parent in "Genos
secretary". The PUT believed the pair: it wrote the old milestone, carried
the sub-tree there, and left the parent chain behind. The rows then belonged
to one milestone by `milestone_id` and to another by `parent_task_id`, and the
table showed them under neither.

These tests pin both halves: a save can no longer write a milestone that
contradicts its parent, and `repair_task_tree` finds and fixes the rows an
earlier save already broke.
"""

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services.task_tree_consistency import (
    find_inconsistent_tasks,
    repair_inconsistent_tasks,
)

User = get_user_model()


class TreeBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="tree1168", email="tree1168@test.com", password="testpass123"
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.team = TeamMaster.objects.create(
            team_name="Tree 1168", team_email="tree1168@test.com", owner=self.user
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Tree 1168",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        # The two milestones of the report: "Improve Genos capability" (old)
        # and "Genos secretary" (new).
        self.old = self._milestone("Improve Genos capability")
        self.new = self._milestone("Genos secretary")

    def _milestone(self, title):
        backing = TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="WIP", is_milestone=True
        )
        return MilestoneMaster.objects.create(
            team=self.team, project=self.project, task=backing, title=title, reporter=self.user
        )

    def _task(self, title, **kwargs):
        # Through the ORM so `set_root_task_id` fills the root as a real create does.
        return TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="WIP", **kwargs
        )

    def _chain_in_new(self):
        """backing(new) -> personalize (625) -> scheduling (598) -> schedules page (1150),
        everything in the new milestone, as it was after the 11-06 move."""
        m = self.new
        personalize = self._task(
            "personalize", milestone_id=m.milestone_id, parent_task_id=m.task_id
        )
        scheduling = self._task(
            "scheduling", milestone_id=m.milestone_id, parent_task_id=personalize.task_id
        )
        page = self._task("page", milestone_id=m.milestone_id, parent_task_id=scheduling.task_id)
        return personalize, scheduling, page

    def _put(self, **payload):
        res = self.client.put(
            "/api/v2/task/", {"project": self.project.project_id, **payload}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        return res

    def _milestones(self, *tasks):
        out = []
        for t in tasks:
            t.refresh_from_db()
            out.append(t.milestone_id)
        return out


class StaleSaveCannotContradictTheTreeTests(TreeBase):
    def test_a_save_with_the_old_milestone_next_to_the_current_parent_changes_nothing(self):
        """The reported bug, exactly: a description edit from a stale copy."""
        personalize, scheduling, page = self._chain_in_new()

        self._put(
            task_id=scheduling.task_id,
            title="Custom Genos scheduling job for users (edited)",
            parent_task_id=personalize.task_id,
            milestone=self.old.milestone_id,
        )

        self.assertEqual(
            self._milestones(personalize, scheduling, page), [self.new.milestone_id] * 3
        )
        scheduling.refresh_from_db()
        self.assertEqual(scheduling.parent_task_id, personalize.task_id)
        self.assertEqual(scheduling.title, "Custom Genos scheduling job for users (edited)")

    def test_the_subtree_is_not_carried_away_either(self):
        personalize, scheduling, page = self._chain_in_new()
        before = TaskMaster.objects.get(pk=page.pk).ts_updated_at

        self._put(
            task_id=scheduling.task_id,
            parent_task_id=personalize.task_id,
            milestone=self.old.milestone_id,
        )

        page.refresh_from_db()
        self.assertEqual(page.milestone_id, self.new.milestone_id)
        self.assertEqual(page.root_task_id, self.new.task_id)
        # A cascade that did not run does not bump what browsers sync on.
        self.assertEqual(page.ts_updated_at, before)

    def test_sprint_and_root_stay_with_the_tree(self):
        personalize, scheduling, page = self._chain_in_new()
        self._put(
            task_id=scheduling.task_id,
            parent_task_id=personalize.task_id,
            milestone=self.old.milestone_id,
        )
        for row in (personalize, scheduling, page):
            row.refresh_from_db()
            self.assertEqual(row.root_task_id, self.new.task_id)
            self.assertEqual(row.sprint_id, self.new.sprint_id)

    def test_a_null_milestone_echo_cannot_pull_a_nested_task_out_of_its_milestone(self):
        personalize, scheduling, _page = self._chain_in_new()
        self._put(task_id=scheduling.task_id, parent_task_id=personalize.task_id, milestone=None)
        self.assertEqual(self._milestones(scheduling), [self.new.milestone_id])

    def test_a_drag_under_another_milestone_with_the_old_milestone_takes_the_new_parents(self):
        """The table's drag sends the new parent with the row's old milestone."""
        personalize, scheduling, page = self._chain_in_new()
        loose = self._task(
            "loose", milestone_id=self.old.milestone_id, parent_task_id=self.old.task_id
        )
        leaf = self._task("leaf", milestone_id=self.old.milestone_id, parent_task_id=loose.task_id)

        self._put(
            task_id=loose.task_id,
            parent_task_id=scheduling.task_id,
            milestone=self.old.milestone_id,
        )

        self.assertEqual(self._milestones(loose, leaf), [self.new.milestone_id] * 2)
        loose.refresh_from_db()
        self.assertEqual(loose.parent_task_id, scheduling.task_id)
        self.assertEqual(loose.root_task_id, self.new.task_id)

    def test_a_row_an_earlier_save_already_broke_is_healed_by_its_next_save(self):
        personalize, scheduling, page = self._chain_in_new()
        # What the old bug left behind: milestone flipped, parent chain untouched.
        TaskMaster.objects.filter(pk__in=[scheduling.pk, page.pk]).update(
            milestone_id=self.old.milestone_id, root_task_id=self.old.task_id
        )

        self._put(
            task_id=scheduling.task_id,
            parent_task_id=personalize.task_id,
            milestone=self.old.milestone_id,
        )

        self.assertEqual(self._milestones(scheduling, page), [self.new.milestone_id] * 2)
        page.refresh_from_db()
        self.assertEqual(page.root_task_id, self.new.task_id)

    def test_the_picker_still_moves_a_task_and_its_subtree(self):
        """Picking a milestone moves the parent with it (to the new backing row)."""
        personalize, scheduling, page = self._chain_in_new()

        self._put(
            task_id=personalize.task_id,
            parent_task_id=self.old.task_id,
            milestone=self.old.milestone_id,
        )

        self.assertEqual(
            self._milestones(personalize, scheduling, page), [self.old.milestone_id] * 3
        )
        page.refresh_from_db()
        self.assertEqual(page.root_task_id, self.old.task_id)

    def test_a_milestone_only_save_still_files_the_task_there(self):
        personalize, scheduling, page = self._chain_in_new()
        self._put(task_id=personalize.task_id, milestone=self.old.milestone_id)
        self.assertEqual(
            self._milestones(personalize, scheduling, page), [self.old.milestone_id] * 3
        )

    def test_an_ordinary_full_save_of_a_consistent_task_writes_no_linkage(self):
        personalize, scheduling, page = self._chain_in_new()
        before = {t.pk: TaskMaster.objects.get(pk=t.pk).ts_updated_at for t in (scheduling, page)}
        self._put(
            task_id=scheduling.task_id,
            parent_task_id=personalize.task_id,
            milestone=self.new.milestone_id,
        )
        page.refresh_from_db()
        self.assertEqual(page.ts_updated_at, before[page.pk])


class CreateCannotContradictTheTreeTests(TreeBase):
    def _payload(self, **overrides):
        body = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": self.user.id,
            "reporter": self.user.id,
            "title": "New sub-task",
            "priority": "Normal",
            "effort_level": "Moderate",
            "status": "Open",
            "content": None,
            "due_date": "2026-12-31",
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        body.update(overrides)
        return body

    def test_a_new_task_takes_its_parents_milestone(self):
        personalize, scheduling, _page = self._chain_in_new()
        res = self.client.post(
            "/api/v2/task/",
            self._payload(parent_task_id=scheduling.task_id, milestone=self.old.milestone_id),
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        created = TaskMaster.objects.get(title="New sub-task")
        self.assertEqual(created.milestone_id, self.new.milestone_id)
        self.assertEqual(created.parent_task_id, scheduling.task_id)
        self.assertEqual(created.root_task_id, self.new.task_id)


class RepairCommandTests(TreeBase):
    def _break_the_report_way(self):
        personalize, scheduling, page = self._chain_in_new()
        TaskMaster.objects.filter(pk__in=[scheduling.pk, page.pk]).update(
            milestone_id=self.old.milestone_id, root_task_id=self.old.task_id
        )
        return personalize, scheduling, page

    def test_a_consistent_tree_reports_nothing(self):
        self._chain_in_new()
        self.assertEqual(find_inconsistent_tasks(project_id=self.project.project_id), [])

    def test_finds_exactly_the_rows_that_disagree_with_their_parent(self):
        personalize, scheduling, page = self._break_the_report_way()
        found = find_inconsistent_tasks(project_id=self.project.project_id)
        self.assertEqual({f.task_id for f in found}, {scheduling.task_id, page.task_id})
        by_id = {f.task_id: f for f in found}
        self.assertEqual(
            by_id[scheduling.task_id].changes["milestone_id"],
            (self.old.milestone_id, self.new.milestone_id),
        )

    def test_repair_fixes_milestone_and_root_top_down_and_is_idempotent(self):
        personalize, scheduling, page = self._break_the_report_way()
        before = TaskMaster.objects.get(pk=page.pk).ts_updated_at

        fixes = find_inconsistent_tasks(project_id=self.project.project_id)
        repair_inconsistent_tasks(fixes)

        self.assertEqual(self._milestones(scheduling, page), [self.new.milestone_id] * 2)
        page.refresh_from_db()
        self.assertEqual(page.root_task_id, self.new.task_id)
        # Stamped, so a browser that syncs "changed since" hears about it.
        self.assertGreater(page.ts_updated_at, before)
        self.assertEqual(find_inconsistent_tasks(project_id=self.project.project_id), [])

    def _chain_with_a_deleted_middle(self):
        """A (root) <- B (soft-deleted) <- C <- D, every stored root the writers made (A)."""
        a = self._task("A")
        b = self._task("B", parent_task_id=a.task_id)
        c = self._task("C", parent_task_id=b.task_id)
        d = self._task("D", parent_task_id=c.task_id)
        TaskMaster.objects.filter(pk=b.pk).update(is_deleted=True)
        return a, b, c, d

    def test_a_deleted_ancestor_does_not_make_a_consistent_root_look_wrong(self):
        # MIP-1225: `top_of_chain` stopped at the first parent missing from the judged rows,
        # which includes a soft-deleted one, so D was "repaired" to root C while C kept A.
        from origin.services.task_reparent import resolve_root_task_id  # noqa: PLC0415

        a, _b, c, d = self._chain_with_a_deleted_middle()
        self.assertEqual(resolve_root_task_id(d.task_id, c.task_id), a.task_id)  # the writers
        self.assertEqual(
            list(
                TaskMaster.objects.filter(pk__in=[c.pk, d.pk]).values_list(
                    "root_task_id", flat=True
                )
            ),
            [a.task_id, a.task_id],
        )
        self.assertEqual(find_inconsistent_tasks(project_id=self.project.project_id), [])

    def test_a_wrong_root_below_a_deleted_ancestor_is_repaired_to_the_writers_answer(self):
        a, _b, c, d = self._chain_with_a_deleted_middle()
        TaskMaster.objects.filter(pk=d.pk).update(root_task_id=d.task_id)  # stale
        fixes = find_inconsistent_tasks(project_id=self.project.project_id)
        self.assertEqual([f.task_id for f in fixes], [d.task_id])
        self.assertEqual(fixes[0].changes["root_task_id"], (d.task_id, a.task_id))
        repair_inconsistent_tasks(fixes)
        d.refresh_from_db()
        c.refresh_from_db()
        self.assertEqual((c.root_task_id, d.root_task_id), (a.task_id, a.task_id))
        self.assertEqual(find_inconsistent_tasks(project_id=self.project.project_id), [])

    def test_a_hard_deleted_ancestor_still_stops_at_the_last_row_that_exists(self):
        # Only the soft-deleted case changes: a row nothing can load ends the walk where it did.
        a = self._task("A")
        b = self._task("B", parent_task_id=a.task_id)
        c = self._task("C", parent_task_id=b.task_id)
        d = self._task("D", parent_task_id=c.task_id)
        TaskMaster.objects.filter(pk=b.pk).delete()
        TaskMaster.objects.filter(pk=d.pk).update(root_task_id=d.task_id)
        fixes = find_inconsistent_tasks(project_id=self.project.project_id)
        self.assertEqual(
            [(f.task_id, f.changes["root_task_id"][1]) for f in fixes], [(d.task_id, c.task_id)]
        )

    def test_sprint_follows_the_milestone(self):
        from origin.models.task.sprint_models import Sprint

        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S1",
            sequence_number=1,
            start_date="2026-11-02",
            end_date="2026-11-15",
        )
        MilestoneMaster.objects.filter(pk=self.new.pk).update(sprint=sprint)
        personalize, scheduling, page = self._break_the_report_way()

        repair_inconsistent_tasks(find_inconsistent_tasks(project_id=self.project.project_id))

        for row in (scheduling, page):
            row.refresh_from_db()
            self.assertEqual(row.sprint_id, sprint.sprint_id)

    def test_leaves_milestone_rows_root_tasks_and_deleted_tasks_alone(self):
        # A root task that carries a milestone stands; so does a deleted row.
        self._task("root", milestone_id=self.old.milestone_id)
        self._task(
            "gone",
            milestone_id=self.old.milestone_id,
            parent_task_id=self.new.task_id,
            is_deleted=True,
        )
        self.assertEqual(find_inconsistent_tasks(project_id=self.project.project_id), [])

    def test_command_is_a_dry_run_unless_applied(self):
        personalize, scheduling, page = self._break_the_report_way()
        out = StringIO()
        call_command("repair_task_tree", f"--project={self.project.project_id}", stdout=out)
        self.assertIn("2 task(s) disagree", out.getvalue())
        self.assertIn("Dry run", out.getvalue())
        self.assertEqual(self._milestones(scheduling, page), [self.old.milestone_id] * 2)

        out = StringIO()
        call_command(
            "repair_task_tree", f"--project={self.project.project_id}", "--apply", stdout=out
        )
        self.assertIn("Fixed 2 task(s)", out.getvalue())
        self.assertEqual(self._milestones(scheduling, page), [self.new.milestone_id] * 2)

    def test_repair_leaves_a_history_entry_naming_the_cascade(self):
        personalize, scheduling, page = self._break_the_report_way()
        repair_inconsistent_tasks(find_inconsistent_tasks(project_id=self.project.project_id))
        self.assertTrue(
            TaskActivity.objects.filter(
                task_id=scheduling.task_id, metadata__cascade="tree_repair"
            ).exists()
        )
