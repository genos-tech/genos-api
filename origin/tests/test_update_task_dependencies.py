"""`update_task_dependencies`: link and unlink existing tasks (MIP-730).

What has to hold, in the order the task's acceptance criteria put it:

* The UI's rules: no self-dependency, same team (cross-project fine), no
  duplicate, no reverse edge — and, beyond the UI, no longer cycle.
* The access check the UI view does not make: the caller has to be able
  to see BOTH tasks. A foreign or invisible task reads as "not found".
* Edges can be removed, and a batch is all-or-nothing.
* Over MCP, both ends of an edge take a display id, and a read key is
  refused.
* Writes go through the dependency signals, so Blocked follows.
"""

from unittest import mock

from origin.models.common.api_key_models import SCOPE_READ
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.search_engine.agent.controller import (
    _friendly_arguments,
    _ui_sources_from_tool_result,
)
from origin.search_engine.agent.tools import ToolContext, ToolError
from origin.search_engine.agent.tools.update_task_dependencies import UPDATE_TASK_DEPENDENCIES

from .test_base import BaseAPITestCase, User
from .test_mcp_endpoint import McpBase


def _edge(blocker, blocked):
    return {"blocker_task_id": blocker.task_id, "blocked_task_id": blocked.task_id}


class DependencyBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = self._project(self.team, "Platform", "HK")
        self.a = self._task(1)
        self.b = self._task(2)
        self.c = self._task(3)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def _project(self, team, name, code, owner=None):
        owner = owner or self.user
        project = ProjectMaster.objects.create(
            team=team, project_name=name, code=code, owner=owner, project_system_user=owner
        )
        ProjectMembers.objects.create(team=team, project=project, attendee=owner)
        return project

    def _task(self, number, *, project=None, team=None, owner=None, status="Open"):
        owner = owner or self.user
        return TaskMaster.objects.create(
            team=team or self.team,
            project=project or self.project,
            assignee=owner,
            reporter=owner,
            title=f"Task {number}",
            status=status,
            project_task_number=number,
        )

    def _link(self, blocker, blocked):
        return TaskDependency.objects.create(
            blocker_task=blocker, blocked_task=blocked, team=self.team
        )

    def _run(self, add=None, remove=None, ctx=None):
        args = {}
        if add is not None:
            args["add"] = add
        if remove is not None:
            args["remove"] = remove
        return UPDATE_TASK_DEPENDENCIES.run(args, ctx or self.ctx)

    def _edges(self):
        return set(TaskDependency.objects.values_list("blocker_task_id", "blocked_task_id"))


class AddTests(DependencyBase):
    def test_it_links_two_existing_tasks(self):
        result = self._run(add=[_edge(self.a, self.b)])
        self.assertEqual(self._edges(), {(self.a.task_id, self.b.task_id)})
        dep = TaskDependency.objects.get()
        self.assertEqual(str(dep.team_id), str(self.team.team_id))
        self.assertEqual(str(dep.created_by_id), str(self.user.id))
        self.assertEqual(result["added"][0]["blocked_display_id"], "HK-2")

    def test_the_blocked_task_turns_blocked(self):
        """Through `objects.create`, so post_save re-derives the status —
        the reason this tool does not bulk_create."""
        self._run(add=[_edge(self.a, self.b)])
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "Blocked")

    def test_cross_project_in_the_same_team_is_allowed(self):
        other = self._project(self.team, "Web", "WB")
        d = self._task(1, project=other)
        self._run(add=[_edge(self.a, d)])
        self.assertIn((self.a.task_id, d.task_id), self._edges())

    def test_a_batch_adds_every_edge(self):
        self._run(add=[_edge(self.a, self.b), _edge(self.b, self.c)])
        self.assertEqual(len(self._edges()), 2)


class RuleTests(DependencyBase):
    def assertRefused(self, fragment, **kw):
        with self.assertRaises(ToolError) as cm:
            self._run(**kw)
        self.assertIn(fragment, str(cm.exception))
        return str(cm.exception)

    def test_a_task_cannot_block_itself(self):
        self.assertRefused("cannot block itself", add=[_edge(self.a, self.a)])

    def test_a_duplicate_is_refused(self):
        self._link(self.a, self.b)
        # The tool's own message: the unique constraint's "already exists"
        # would also match, but only after an insert was attempted.
        self.assertRefused("this dependency already exists", add=[_edge(self.a, self.b)])

    def test_the_same_edge_twice_in_one_batch_is_refused(self):
        self.assertRefused("listed twice", add=[_edge(self.a, self.b), _edge(self.a, self.b)])

    def test_a_reverse_edge_is_a_cycle(self):
        self._link(self.a, self.b)
        self.assertRefused("cycle", add=[_edge(self.b, self.a)])

    def test_a_longer_cycle_through_the_existing_graph_is_refused(self):
        """The UI only refuses the 2-cycle. A → B → C exists, so C → A
        closes a loop that no one-hop check sees."""
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        message = self.assertRefused("cycle", add=[_edge(self.c, self.a)])
        self.assertIn("HK-1 -> HK-2 -> HK-3 -> HK-1", message)

    def test_a_cycle_made_only_inside_the_batch_is_refused(self):
        self.assertRefused(
            "cycle", add=[_edge(self.a, self.b), _edge(self.b, self.c), _edge(self.c, self.a)]
        )

    def test_flipping_an_edge_in_one_call_is_not_a_cycle(self):
        """Remove + add of the reverse: the check runs against the graph
        as it will be, not as it is."""
        self._link(self.a, self.b)
        self._run(add=[_edge(self.b, self.a)], remove=[_edge(self.a, self.b)])
        self.assertEqual(self._edges(), {(self.b.task_id, self.a.task_id)})

    def test_an_existing_cycle_does_not_block_unrelated_work_or_its_own_repair(self):
        """Legacy data can already hold a 3-cycle (the UI never refused
        one). Removing an edge of it, and adding an edge next to it, must
        both still work."""
        self._link(self.a, self.b)
        self._link(self.b, self.c)
        self._link(self.c, self.a)
        d = self._task(4)
        self._run(add=[_edge(d, self.a)], remove=[_edge(self.c, self.a)])
        self.assertNotIn((self.c.task_id, self.a.task_id), self._edges())
        self.assertIn((d.task_id, self.a.task_id), self._edges())

    def test_nothing_is_applied_when_one_entry_is_bad(self):
        self._link(self.a, self.b)
        self.assertRefused(
            "this dependency already exists",
            add=[_edge(self.b, self.c), _edge(self.a, self.b)],
            remove=[_edge(self.a, self.c)],
        )
        self.assertEqual(self._edges(), {(self.a.task_id, self.b.task_id)})

    def test_a_failure_while_applying_rolls_the_whole_batch_back(self):
        """Validation passed, then the second insert blew up: the removal
        that already ran has to come back."""
        self._link(self.a, self.c)
        real_create = TaskDependency.objects.create
        calls = []

        def flaky(**kw):
            calls.append(kw)
            if len(calls) == 2:
                raise RuntimeError("db went away")
            return real_create(**kw)

        with mock.patch.object(TaskDependency.objects, "create", side_effect=flaky):
            with self.assertRaises(ToolError):
                self._run(
                    add=[_edge(self.a, self.b), _edge(self.b, self.c)],
                    remove=[_edge(self.a, self.c)],
                )
        self.assertEqual(self._edges(), {(self.a.task_id, self.c.task_id)})

    def test_a_cycle_through_a_hidden_task_does_not_name_it(self):
        """The loop is real whoever can see it, so it is still refused —
        but the error must not hand over the hidden task's id or title."""
        private = self._project(self.team, "Private", "PV", owner=self.user2)
        hidden = self._task(9, project=private, owner=self.user2)
        hidden.title = "Secret acquisition"
        hidden.save()
        self._link(self.a, hidden)
        self._link(hidden, self.b)
        message = self.assertRefused("cycle", add=[_edge(self.b, self.a)])
        self.assertNotIn("PV-9", message)
        self.assertNotIn("Secret", message)
        self.assertNotIn(str(hidden.task_id), message.split("cycle", 1)[1])
        self.assertIn("a task you can't see", message)

    def test_an_empty_call_is_refused(self):
        self.assertRefused("at least one edge")

    def test_an_edge_in_both_lists_is_refused(self):
        self._link(self.a, self.b)
        self.assertRefused(
            "both `add` and `remove`", add=[_edge(self.a, self.b)], remove=[_edge(self.a, self.b)]
        )

    def test_a_deleted_task_is_not_found(self):
        TaskMaster.objects.filter(pk=self.b.pk).update(is_deleted=True)
        self.assertRefused("not found", add=[_edge(self.a, self.b)])


class AccessTests(DependencyBase):
    """The check the UI's TaskDependencyView.post does not make."""

    def setUp(self):
        super().setUp()
        self.outsider = User.objects.create_user(
            username="out", email="out@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.outsider)
        other_project = self._project(self.other_team, "Theirs", "TH", owner=self.outsider)
        self.foreign = self._task(
            1, project=other_project, team=self.other_team, owner=self.outsider
        )

    def test_a_task_in_another_team_reads_as_not_found(self):
        with self.assertRaises(ToolError) as cm:
            self._run(add=[_edge(self.a, self.foreign)])
        self.assertIn("not found", str(cm.exception))
        self.assertNotIn("team", str(cm.exception).lower())
        self.assertFalse(TaskDependency.objects.exists())

    def test_my_own_task_in_another_team_is_not_this_teams(self):
        """The caller is in both teams and assigned a task in the other.
        Assignee access alone would pass; the key's team still decides."""
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        self.foreign.assignee = self.user
        self.foreign.save()
        with self.assertRaises(ToolError) as cm:
            self._run(add=[_edge(self.a, self.foreign)])
        self.assertIn("not found", str(cm.exception))
        self.assertFalse(TaskDependency.objects.exists())

    def test_an_outsider_cannot_link_into_this_team(self):
        """Their key's team is theirs; our tasks don't resolve for them."""
        ctx = ToolContext(team_id=str(self.other_team.team_id), user_id=str(self.outsider.id))
        with self.assertRaises(ToolError):
            self._run(add=[_edge(self.foreign, self.a)], ctx=ctx)
        self.assertFalse(TaskDependency.objects.exists())

    def test_a_teammate_off_the_project_cannot_see_either_end(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolError) as cm:
            self._run(add=[_edge(self.a, self.b)], ctx=ctx)
        self.assertIn("not found", str(cm.exception))

    def test_seeing_one_end_is_not_enough(self):
        """user2 owns a task on a private project of theirs, and can't see
        HK-1. Linking would show them HK-1's title in their task's
        dependency list — so it is refused."""
        theirs = self._project(self.team, "Private", "PV", owner=self.user2)
        mine = self._task(1, project=theirs, owner=self.user2)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolError):
            self._run(add=[_edge(self.a, mine)], ctx=ctx)
        self.assertFalse(TaskDependency.objects.exists())

    def test_the_assignee_off_the_project_may_link(self):
        """The ACL set is project members + assignee + reporter."""
        self.b.assignee = self.user2
        self.b.save()
        self.a.assignee = self.user2
        self.a.save()
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        self._run(add=[_edge(self.a, self.b)], ctx=ctx)
        self.assertEqual(len(self._edges()), 1)

    def test_removing_needs_access_too(self):
        self._link(self.a, self.b)
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))
        with self.assertRaises(ToolError):
            self._run(remove=[_edge(self.a, self.b)], ctx=ctx)
        self.assertEqual(len(self._edges()), 1)


class RemoveTests(DependencyBase):
    def test_it_removes_an_edge_and_unblocks(self):
        self._link(self.a, self.b)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "Blocked")
        result = self._run(remove=[_edge(self.a, self.b)])
        self.assertEqual(self._edges(), set())
        self.assertEqual(len(result["removed"]), 1)
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "Open")

    def test_removing_a_missing_edge_is_reported_not_an_error(self):
        result = self._run(remove=[_edge(self.a, self.b)])
        self.assertEqual(result["removed"], [])
        self.assertEqual(result["not_found"][0]["blocker_display_id"], "HK-1")


class ApprovalCardTests(DependencyBase):
    def test_the_card_names_both_ends(self):
        friendly = _friendly_arguments(
            {"add": [_edge(self.a, self.b)], "remove": [_edge(self.b, self.c)]},
            "update_task_dependencies",
        )
        self.assertEqual(friendly["add"][0]["blocker_task_id"], "HK-1 Task 1")
        self.assertEqual(friendly["remove"][0]["blocked_task_id"], "HK-3 Task 3")

    def test_the_answer_chips_every_touched_task_once(self):
        result = self._run(add=[_edge(self.a, self.b), _edge(self.a, self.c)])
        sources = _ui_sources_from_tool_result("update_task_dependencies", result)
        self.assertEqual(sorted(s["task_display_id"] for s in sources), ["HK-1", "HK-2", "HK-3"])
        self.assertTrue(all(s["operated"] for s in sources))


class McpDependencyTests(McpBase):
    def setUp(self):
        super().setUp()
        self.blocker = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Ship the keyring",
            status="Open",
            project_task_number=8,
        )

    def _args(self):
        return {"add": [{"blocker_task_id": "WRD-8", "blocked_task_id": "WRD-7"}]}

    def test_a_write_key_links_by_display_id(self):
        self._key()
        text, is_error = self._tool_text(self._call("update_task_dependencies", self._args()))
        self.assertFalse(is_error, text)
        self.assertTrue(
            TaskDependency.objects.filter(
                blocker_task=self.blocker, blocked_task=self.task
            ).exists()
        )
        removed = self._result(
            self._call(
                "update_task_dependencies",
                {"remove": [{"blocker_task_id": "WRD-8", "blocked_task_id": "WRD-7"}]},
            )
        )
        self.assertEqual(len(removed["structuredContent"]["removed"]), 1)
        self.assertFalse(TaskDependency.objects.exists())

    def test_a_read_key_is_refused(self):
        self._key(scope=SCOPE_READ)
        text, is_error = self._tool_text(self._call("update_task_dependencies", self._args()))
        self.assertTrue(is_error)
        self.assertIn("read-only", text)
        self.assertFalse(TaskDependency.objects.exists())

    def test_the_schema_takes_a_display_id_on_both_ends(self):
        from origin.views.public.mcp import tools

        schema = tools.input_schema(tools.BY_NAME["update_task_dependencies"])
        for key in ("add", "remove"):
            items = schema["properties"][key]["items"]["properties"]
            self.assertEqual(items["blocker_task_id"]["type"], "string")
            self.assertEqual(items["blocked_task_id"]["type"], "string")
