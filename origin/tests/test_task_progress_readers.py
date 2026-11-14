"""MIP-1361: where `last_progress_at` (MIP-1351) is read.

REST: every task payload and the milestone payload carry it, and the `since=`
delta sends a parent whose CHILD changed although its own `ts_updated_at` did not
move. MCP: `get_task`, `list_tasks`, `list_milestones` and `get_milestone_summary` carry
it, `get_progress_history` lists what moved, and the in-app agent, which shares those
registry tools, sees none of it.
"""

from datetime import timedelta

from django.utils import timezone

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.models.task.task_progress_models import TaskProgressFact
from origin.search_engine.agent.tools import MCP_ONLY_REGISTRY, REGISTRY, ToolContext
from origin.services.milestone_service import ensure_backing_task
from origin.signals.task_signals import update_tasks_recorded
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import McpBase

TASKS_URL = "/api/v2/task/getProjectTasks/"


class Tree:
    """milestone -> task -> sub-task, and a sibling task, in `self.project`."""

    def build(self):
        self.milestone = MilestoneMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Ship it",
            status="Open",
        )
        self.ms = ensure_backing_task(self.milestone)
        self.task = self._mk("Task A", self.ms)
        self.sub = self._mk("Sub A-A", self.task)
        self.sibling = self._mk("Task B", self.ms)

    def _mk(self, title, parent):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title=title,
            status="Open",
            parent_task_id=parent.task_id,
            milestone=self.milestone,
        )

    def edit(self, task, **fields):
        with self.captureOnCommitCallbacks(execute=True):
            row = TaskMaster.objects.get(pk=task.pk)
            for k, v in fields.items():
                setattr(row, k, v)
            row.save()

    def age(self, days=30):
        """Pin every value, and the history behind it, to one old time."""
        old = timezone.now() - timedelta(days=days)
        TaskProgressFact.objects.filter(ancestor_task__project=self.project).delete()
        TaskMaster.objects.filter(project=self.project).update(
            last_progress_at=old, ts_updated_at=old
        )
        return old


class RestTests(Tree, BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Rest", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        with self.captureOnCommitCallbacks(execute=True):
            self.build()

    def _get(self, **extra):
        params = {"team_id": str(self.team.team_id), "project_id": str(self.project.project_id)}
        params.update(extra)
        res = self.client.get(TASKS_URL, params)
        self.assertEqual(res.status_code, 200, res.data)
        return {t["id"]: t for t in res.data["data"]["tasks"]}

    def test_the_project_list_carries_it_and_null_when_nothing_is_recorded(self):
        TaskMaster.objects.filter(pk=self.sibling.pk).update(last_progress_at=None)
        rows = self._get()
        self.assertIsNotNone(rows[str(self.task.task_id)]["lastProgressAt"])
        self.assertIsNone(rows[str(self.sibling.task_id)]["lastProgressAt"])

    def test_it_is_in_the_same_form_as_updated_at(self):
        row = self._get()[str(self.sub.task_id)]
        self.assertEqual(
            row["lastProgressAt"], str(TaskMaster.objects.get(pk=self.sub.pk).last_progress_at)
        )

    def test_a_parent_whose_child_changed_is_in_the_delta_although_it_was_not_touched(self):
        old = self.age()
        checkpoint = timezone.now() - timedelta(days=1)
        self.edit(self.sub, status="Closed")
        # the parent and the milestone were not saved
        self.assertEqual(TaskMaster.objects.get(pk=self.ms.pk).ts_updated_at, old)

        rows = self._get(since=checkpoint.isoformat())

        self.assertEqual(
            set(rows), {str(self.sub.task_id), str(self.task.task_id), str(self.ms.task_id)}
        )
        self.assertNotIn(str(self.sibling.task_id), rows)
        self.assertGreater(rows[str(self.ms.task_id)]["lastProgressAt"], str(old))

    def test_a_task_nothing_happened_to_is_not_in_the_delta(self):
        self.age()
        self.assertEqual(self._get(since=(timezone.now() - timedelta(days=1)).isoformat()), {})

    def test_the_milestone_carries_its_backing_tasks_value(self):
        self.age()
        self.edit(self.sub, status="Closed")
        res = self.client.get(f"/api/v2/milestone/{self.milestone.milestone_id}/")
        self.assertEqual(res.status_code, 200, res.data)
        body = res.data
        for key in ("data", "milestone"):
            if isinstance(body, dict) and key in body and "lastProgressAt" not in body:
                body = body[key]
        self.assertEqual(
            body["lastProgressAt"], str(TaskMaster.objects.get(pk=self.ms.pk).last_progress_at)
        )

    def test_the_task_the_team_list_and_my_assigned_payloads_carry_it(self):
        self.age()
        self.edit(self.sub, status="Closed")
        want = str(TaskMaster.objects.get(pk=self.ms.pk).last_progress_at)
        team = {"team_id": str(self.team.team_id)}

        one = self.client.get(
            "/api/v2/task/getTask/",
            {**team, "project_id": self.project.project_id, "task_id": self.ms.task_id},
        )
        self.assertEqual(one.status_code, 200, one.data)
        task = one.data[0] if isinstance(one.data, list) else one.data
        self.assertEqual(task["lastProgressAt"], want)

        listed = self.client.get("/api/v2/task/getTeamTasks/", team)
        self.assertEqual(listed.status_code, 200, listed.data)
        rows = {str(r["id"]): r for r in listed.data}
        self.assertEqual(rows[str(self.ms.task_id)]["lastProgressAt"], want)

        mine = self.client.get("/api/v2/task/getMyAssignedTasks/", team)
        self.assertEqual(mine.status_code, 200, mine.data)
        mine_rows = mine.data
        self.assertTrue(mine_rows)
        self.assertTrue(all("lastProgressAt" in r for r in mine_rows))

    def test_the_task_of_a_chat_thread_carries_it(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(
            chat_type=1, chat_id="chat-1", thread_id="thread-1"
        )
        res = self.client.get(
            "/api/v2/task/getTaskByThreadId/",
            {
                "team_id": str(self.team.team_id),
                "chat_type": 1,
                "chat_id": "chat-1",
                "thread_id": "thread-1",
            },
        )
        self.assertEqual(res.status_code, 200, res.data)
        body = res.data
        self.assertTrue(body, "the thread's task was not found")
        row = body[0] if isinstance(body, list) else body
        self.assertEqual(
            row["lastProgressAt"], str(TaskMaster.objects.get(pk=self.task.pk).last_progress_at)
        )


class McpTests(Tree, McpBase):
    def setUp(self):
        super().setUp()
        self._key()
        self.ctx = ToolContext(
            team_id=str(self.team.team_id), user_id=str(self.user.id), surface="mcp"
        )
        with self.captureOnCommitCallbacks(execute=True):
            self.build()

    def _ok(self, name, args=None):
        result = self._result(self._call(name, args or {}))
        self.assertFalse(result.get("isError"), result)
        return result["structuredContent"]

    def _refused(self, name, args):
        text, is_error = self._tool_text(self._call(name, args))
        self.assertTrue(is_error, text)
        return text

    # ---- the shared tools: the field is added on MCP, never in the app -----------

    def test_get_task(self):
        self.assertIsNotNone(
            self._ok("get_task", {"task_id": self.task.display_id})["last_progress_at"]
        )

    def test_list_tasks_rows(self):
        rows = self._ok("list_tasks", {"project_id": self.project.project_id})["tasks"]
        self.assertTrue(rows)
        self.assertTrue(all("last_progress_at" in r for r in rows))
        by_id = {r["task_id"]: r for r in rows}
        self.assertIsNotNone(by_id[self.sub.task_id]["last_progress_at"])

    def test_list_milestones_rows(self):
        rows = self._ok("list_milestones", {"project_id": self.project.project_id})["milestones"]
        mine = next(r for r in rows if r["milestone_id"] == self.milestone.milestone_id)
        self.assertEqual(
            mine["last_progress_at"],
            TaskMaster.objects.get(pk=self.ms.pk).last_progress_at.isoformat(),
        )

    def test_get_milestone_summary(self):
        out = self._ok("get_milestone_summary", {"milestone_id": self.milestone.milestone_id})
        self.assertEqual(
            out["last_progress_at"],
            TaskMaster.objects.get(pk=self.ms.pk).last_progress_at.isoformat(),
        )

    def test_the_in_app_agent_sees_none_of_it(self):
        """The registry tools are untouched: anything that changes what the in-app model
        sees waits for the December eval."""
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.assertNotIn(
            "last_progress_at", REGISTRY["fetch_task"].run({"task_id": self.task.task_id}, ctx)
        )
        for name, args, key in (
            ("list_tasks", {"project_id": self.project.project_id}, "tasks"),
            ("list_milestones", {"project_id": self.project.project_id}, "milestones"),
        ):
            for row in REGISTRY[name].run(args, ctx)[key]:
                self.assertNotIn("last_progress_at", row, name)
        self.assertNotIn(
            "last_progress_at",
            REGISTRY["get_milestone_summary"].run(
                {"milestone_id": self.milestone.milestone_id}, ctx
            ),
        )

    # ---- get_progress_history ------------------------------------------------------

    def test_the_history_tool_is_mcp_only(self):
        self.assertIn("get_progress_history", MCP_ONLY_REGISTRY)
        self.assertNotIn("get_progress_history", REGISTRY)

    def test_a_milestones_history_lists_every_level(self):
        self.age()
        self.edit(self.sub, status="Closed")
        self.edit(self.sibling, priority="High")
        out = self._ok("get_progress_history", {"task_id": self.ms.display_id})
        by_task = {e["task_id"]: e for e in out["entries"]}
        self.assertEqual(by_task[self.sub.task_id]["depth"], 2)
        self.assertEqual(by_task[self.sub.task_id]["action_type"], "status_changed")
        self.assertEqual(by_task[self.sub.task_id]["display_id"], self.sub.display_id)
        self.assertEqual(by_task[self.sibling.task_id]["depth"], 1)
        self.assertEqual(by_task[self.sub.task_id]["actor"], None)  # no request user in a test save
        self.assertEqual(
            out["last_progress_at"],
            TaskMaster.objects.get(pk=self.ms.pk).last_progress_at.isoformat(),
        )
        stamps = [e["ts"] for e in out["entries"]]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_a_tasks_history_does_not_include_its_siblings(self):
        self.age()
        self.edit(self.sibling, priority="High")
        out = self._ok("get_progress_history", {"task_id": self.task.display_id})
        self.assertEqual(out["entries"], [])

    def test_limit_and_has_more_and_since(self):
        self.age()
        for status in ("WIP", "Pending", "Closed"):
            self.edit(self.sub, status=status)
        out = self._ok("get_progress_history", {"task_id": self.ms.display_id, "limit": 2})
        self.assertEqual((len(out["entries"]), out["has_more"]), (2, True))
        everything = self._ok("get_progress_history", {"task_id": self.ms.display_id})
        self.assertFalse(everything["has_more"])
        future = (timezone.now() + timedelta(days=1)).date().isoformat()
        self.assertEqual(
            self._ok("get_progress_history", {"task_id": self.ms.display_id, "since": future})[
                "entries"
            ],
            [],
        )
        self.assertIn(
            "since",
            self._refused(
                "get_progress_history", {"task_id": self.ms.display_id, "since": "last tuesday"}
            ),
        )
        self.assertIn(
            "limit",
            self._refused("get_progress_history", {"task_id": self.ms.display_id, "limit": 0}),
        )

    def test_a_bulk_move_is_not_listed(self):
        self.age()
        with self.captureOnCommitCallbacks(execute=True):
            update_tasks_recorded(
                TaskMaster.objects.filter(pk=self.sub.pk), cascade="milestone_move", priority="High"
            )
        self.assertEqual(
            self._ok("get_progress_history", {"task_id": self.ms.display_id})["entries"], []
        )

    def test_the_tasks_own_title_is_fenced_as_workspace_text(self):
        out = self._result(self._call("get_progress_history", {"task_id": self.ms.display_id}))
        self.assertIn(self.sub.title, out["content"][0]["text"])
        self.assertTrue(out["_meta"]["genos/untrustedContent"])

    def test_it_needs_project_membership_not_just_being_the_assignee(self):
        outsider = self.user2
        TaskMaster.objects.filter(pk=self.task.pk).update(assignee=outsider)
        self._key(user=outsider)
        # `get_task` lets an assignee in; this tool names the siblings too
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=outsider).exists()
        )
        text = self._refused("get_progress_history", {"task_id": self.task.display_id})
        self.assertIn("Not authorized", text)

    def test_another_team_is_refused_even_for_a_project_member(self):
        """Membership of the project would let this caller in; the tenant guard is
        what stops a context that names a different team."""
        from origin.models.common.team_models import TeamMaster
        from origin.search_engine.agent.tools import ToolForbidden

        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        ctx = ToolContext(team_id=str(other.team_id), user_id=str(self.user.id), surface="mcp")
        with self.assertRaises(ToolForbidden) as raised:
            MCP_ONLY_REGISTRY["get_progress_history"].run({"task_id": self.ms.task_id}, ctx)
        self.assertEqual(str(raised.exception), f"Task {self.ms.task_id} not found.")
