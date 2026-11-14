"""The twin tests of MIP-1564: the app's convert and project move against the tool's, every channel.

"The tool does what the app does" is the contract, so it is proved the way #862 and #867 proved theirs:
two IDENTICAL worlds (twin tasks or twin milestones, each with its sub-tasks, people and a card in the
project chat), titled `... a` and `... b`. The app's endpoint (`POST /task/convert-to-milestone/`,
`POST /milestone/convert-to-task/`, `PATCH /milestone/<id>/ {project_id}`) acts on `a`, the tool
(`manage_milestone`, as the in-app agent's approve path runs it) on `b`, and what each leaves is
recorded on every channel and compared:

  * the rows (each task's and milestone's place, kind, people, sprint, folder, tags, display id prefix);
  * the history of every task involved, with the person, the cascade and how the write arrived (`via`);
  * the project chat's cards, and what was pushed to open chats;
  * every way the app tells a person something (`watching_notices`: feed rows, inbox, chat mentions,
    e-mail, Web Push, webhooks);
  * the search index and the change feed;
  * a row count of EVERY model.

The one difference that is expected, and so asserted, is `via` (the app against the agent). A difference
that is not on that list fails the test.
"""

from __future__ import annotations

from unittest import mock

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.cache import cache

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.chat.unified_models import Message
from origin.models.common.change_models import ChangeVersion
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine.agent.tools import ToolError
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.services import change_feed, task_card_post

from .notice_watch import watching_notices
from .test_agent_parity_milestones import ToolBase

User = get_user_model()
MILESTONE_URL = "/api/v2/milestone/"
APP, TOOL = "a", "b"


def row_counts() -> dict[str, int]:
    """How many rows every model holds: a change that quietly wrote anywhere shows up."""
    return {model._meta.label: model._default_manager.count() for model in apps.get_models()}


def deltas(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {label: after[label] - before[label] for label in after if after[label] != before[label]}


def role(title: str) -> str:
    """A title without its world's tag: `Launch a` and `Launch b` are the same role."""
    return title[:-2] if title[-2:] in (f" {APP}", f" {TOOL}") else title


class TwinBase(ToolBase):
    """Two worlds, `a` for the app and `b` for the tool, built by the same function."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.auth(self.owner)

    # ---- the worlds -----------------------------------------------------------------------

    def root_world(self, tag):
        """A root task with a comment, a collaborator, a sub-task and a grandchild, and its card."""
        root = self.task(
            f"Plan the launch {tag}",
            status="WIP",
            priority="High",
            priority_code=3,
            due_date="2026-12-01",
        )
        root.collaborators.add(self.bob)
        child = self.task(f"Step one {tag}", parent_task_id=root.task_id, root_task_id=root.task_id)
        self.task(f"Detail {tag}", parent_task_id=child.task_id, root_task_id=root.task_id)
        TaskComments.objects.create(
            task=root,
            sender=self.owner,
            comment_id=1,
            comment_body=[{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
        )
        task_card_post.post_task_created_card(root, actor_id=str(self.owner.id))
        return root

    def milestone_world(self, tag):
        m = self.roadmap_milestone(f"Launch {tag}", suffix=f" {tag}")
        task_card_post.post_milestone_created_card(m, actor_id=str(self.owner.id))
        return m

    # ---- the app and the tool ---------------------------------------------------------------

    def app_to_milestone(self, task):
        return self.client.post(
            "/api/v2/task/convert-to-milestone/", {"task_id": task.task_id}, format="json"
        )

    def app_to_task(self, milestone):
        return self.client.post(
            "/api/v2/milestone/convert-to-task/",
            {"milestone_id": milestone.milestone_id},
            format="json",
        )

    def app_move(self, milestone, project=None):
        return self.client.patch(
            f"{MILESTONE_URL}{milestone.milestone_id}/",
            {"project_id": (project or self.dest).project_id},
            format="json",
        )

    def tool(self, args, user=None):
        """As the app's approve path runs it: the person acting, the write `via` the agent."""
        set_current_user(user or self.owner, via={"kind": "agent"})
        try:
            return MANAGE_MILESTONE.run(args, self.ctx(user))
        finally:
            clear_current_user()
            # The end of the request the approval runs in: the change feed is written then.
            change_feed.flush()

    # ---- what a world looks like --------------------------------------------------------------

    def tasks_of(self, tag):
        return TaskMaster.objects.select_related("project", "sprint", "assignee").filter(
            title__endswith=f" {tag}"
        )

    def shape(self, tag):
        """Every task and milestone of the world `tag`, by role: the place, kind, people, sprint,
        tags and display-id prefix, with the ids that differ between worlds turned into roles."""
        tasks = list(self.tasks_of(tag))
        by_id = {t.task_id: t for t in tasks}
        milestones = {
            m.milestone_id: m
            for m in MilestoneMaster.objects.select_related(
                "project", "folder", "sprint", "converted_to_task"
            ).filter(title__endswith=f" {tag}")
        }
        out_tasks = {}
        for t in tasks:
            out_tasks[role(t.title)] = {
                "project": t.project.code,
                "display_prefix": t.display_id.split("-")[0],
                "is_milestone": t.is_milestone,
                "milestone": (
                    role(milestones[t.milestone_id].title) if t.milestone_id in milestones else None
                ),
                "sprint": t.sprint.name if t.sprint_id else None,
                "assignee": t.assignee.username if t.assignee_id else None,
                "collaborators": sorted(c.username for c in t.collaborators.all()),
                "parent": role(by_id[t.parent_task_id].title)
                if t.parent_task_id in by_id
                else None,
                "root": role(by_id[t.root_task_id].title) if t.root_task_id in by_id else None,
                "tags": t.tags,
                "status": t.status,
                "is_deleted": t.is_deleted,
            }
        out_milestones = {}
        for m in milestones.values():
            out_milestones[role(m.title)] = {
                "project": m.project.code,
                "is_deleted": m.is_deleted,
                "folder": m.folder.name if m.folder_id else None,
                "sprint": m.sprint.name if m.sprint_id else None,
                "assignees": sorted(
                    MilestoneAssignees.objects.filter(milestone=m).values_list(
                        "user__username", flat=True
                    )
                ),
                "converted_to": role(m.converted_to_task.title) if m.converted_to_task_id else None,
                "has_row": m.task_id is not None,
                "tags": m.tags,
            }
        return {"tasks": out_tasks, "milestones": out_milestones}

    def cards(self, tag):
        """The project chats' cards of the world's tasks: where, whose, what they say."""
        ids = {t.task_id: role(t.title) for t in self.tasks_of(tag)}
        return sorted(
            (
                m.channel.legacy_chat_id,
                ids[m.task_id],
                role(m.body_text),
                m.edited_at is not None,
                (m.metadata or {}).get("taskStatus"),
            )
            for m in Message.objects.select_related("channel").filter(
                task_id__in=ids, is_thread_reply=False
            )
        )

    def record(self, act, tag):
        """Run `act` and return everything it left. What was there before it is not counted."""
        patches = {
            "index_client": mock.patch("origin.search_engine.opensearch_client.get_client"),
            "purge_client": mock.patch("origin.search_engine.purge.get_client"),
            "push": mock.patch("origin.services.realtime_push._post"),
            "push_message": mock.patch("origin.services.realtime_push.push_message"),
        }
        started = {name: p.start() for name, p in patches.items()}
        before_history = set(
            TaskActivity.objects.filter(task__title__endswith=f" {tag}").values_list(
                "pk", flat=True
            )
        )
        try:
            change_feed.reset_pending()
            versions_before = dict(ChangeVersion.objects.values_list("key", "version"))
            counts_before = row_counts()
            with watching_notices() as notices, self.captureOnCommitCallbacks(execute=True):
                result = act()
            counts_after = row_counts()
            versions_after = dict(ChangeVersion.objects.values_list("key", "version"))
        finally:
            for p in patches.values():
                p.stop()
        new_history = (
            TaskActivity.objects.filter(task__title__endswith=f" {tag}")
            .exclude(pk__in=before_history)
            .select_related("task")
        )
        # A milestone id in a history row is that milestone's role, so the worlds compare.
        milestone_roles = {
            str(m.milestone_id): f"milestone {role(m.title)}"
            for m in MilestoneMaster.objects.filter(title__endswith=f" {tag}")
        }

        def shown(a, value):
            return (
                milestone_roles.get(str(value), str(value))
                if a.field_name in ("milestone", "milestone_id")
                else str(value)
            )

        return {
            "result": result,
            "shape": self.shape(tag),
            "cards": self.cards(tag),
            # (who changed, what, from, to, which cascade); `via` is kept apart so it can differ
            "history": sorted(
                (
                    role(a.task.title),
                    a.action_type,
                    a.field_name,
                    shown(a, a.old_value),
                    shown(a, a.new_value),
                    str(a.actor_id),
                    (a.metadata or {}).get("cascade"),
                )
                for a in new_history
            ),
            "via": sorted({((a.metadata or {}).get("via") or {}).get("kind") for a in new_history}),
            "row_deltas": deltas(counts_before, counts_after),
            "notices": notices,
            "index_calls": started["index_client"].call_count + started["purge_client"].call_count,
            "pushes": started["push"].call_count + started["push_message"].call_count,
            # which keys of the change feed (what other open browsers poll) were bumped
            "change_feed": sorted(
                k for k, v in versions_after.items() if versions_before.get(k) != v
            ),
        }

    def assert_twins(self, app, tool, *, card_refreshed=False):
        """Everything either one left is the same, but for how the write arrived.

        `card_refreshed` is the one documented difference of the convert to a milestone: a write
        that has no browser gets its project-chat card rewritten by the server (MIP-1460), and the
        app's own conversion does not rewrite it (the browser does not call `updateTaskCard` after a
        conversion). So the tool's card is the same text but edited, and one live push more."""
        self.assertEqual(tool["shape"], app["shape"], "the rows")
        self.assertEqual(tool["history"], app["history"], "the history")
        self.assertEqual(app["via"], ["app"])
        self.assertEqual(tool["via"], ["agent"])
        if card_refreshed:
            self.assertEqual(
                [(*c[:3], False, c[4]) for c in tool["cards"]],
                app["cards"],
                "the card's text",
            )
            self.assertTrue(all(c[3] for c in tool["cards"]), "rewritten, so edited")
            self.assertEqual(tool["pushes"], app["pushes"] + 1, "one live push for the card")
        else:
            self.assertEqual(tool["cards"], app["cards"], "the project chat's cards")
            self.assertEqual(tool["pushes"], app["pushes"], "live pushes")
        # `ChangeVersion` rows are created the first time a key is bumped: which keys were bumped is
        # compared below, so the first writer's extra rows are not a difference.
        for record in (app, tool):
            record["row_deltas"].pop("origin.ChangeVersion", None)
        self.assertEqual(tool["row_deltas"], app["row_deltas"], "a row count of every model")
        self.assertEqual(tool["notices"], app["notices"], "who was told, and how")
        self.assertEqual(tool["index_calls"], app["index_calls"], "the search index")
        self.assertEqual(tool["change_feed"], app["change_feed"], "the change feed")


# --------------------------------------------------------------------------------------------
# the twins
# --------------------------------------------------------------------------------------------


class ConvertTwinTests(TwinBase):
    def test_a_root_task_into_a_milestone_is_what_the_app_does_and_no_more(self):
        app_root, tool_root = self.root_world(APP), self.root_world(TOOL)

        app = self.record(lambda: self.app_to_milestone(app_root), APP)
        tool = self.record(
            lambda: self.tool({"action": "convert", "task_id": tool_root.task_id}), TOOL
        )

        self.assertEqual(app["result"].status_code, 200)
        self.assertEqual(tool["result"]["direction"], "task_to_milestone")
        self.assert_twins(app, tool, card_refreshed=True)
        # and the change itself is the one the app's contract names
        shape = tool["shape"]
        self.assertTrue(shape["tasks"]["Plan the launch"]["is_milestone"])
        self.assertEqual(shape["tasks"]["Step one"]["milestone"], "Plan the launch")
        self.assertEqual(shape["tasks"]["Detail"]["milestone"], "Plan the launch")
        self.assertEqual(shape["tasks"]["Step one"]["parent"], "Plan the launch")
        self.assertEqual(shape["milestones"]["Plan the launch"]["assignees"], ["alice"])

    def test_a_milestone_into_a_task_is_what_the_app_does_and_no_more(self):
        app_ms, tool_ms = self.milestone_world(APP), self.milestone_world(TOOL)

        app = self.record(lambda: self.app_to_task(app_ms), APP)
        tool = self.record(
            lambda: self.tool({"action": "convert", "milestone_id": tool_ms.milestone_id}), TOOL
        )

        self.assertEqual(app["result"].status_code, 200)
        self.assertEqual(tool["result"]["direction"], "milestone_to_task")
        self.assert_twins(app, tool)
        shape = tool["shape"]
        row = shape["tasks"]["Launch"]
        self.assertFalse(row["is_milestone"])
        self.assertEqual((row["assignee"], row["collaborators"]), ("alice", ["bob"]))
        self.assertIsNone(row["sprint"])
        self.assertTrue(shape["milestones"]["Launch"]["is_deleted"])
        self.assertEqual(shape["milestones"]["Launch"]["converted_to"], "Launch")

    def test_the_old_link_of_a_converted_milestone_redirects_the_same_after_either(self):
        app_ms, tool_ms = self.milestone_world(APP), self.milestone_world(TOOL)
        self.app_to_task(app_ms)
        self.tool({"action": "convert", "milestone_id": tool_ms.milestone_id})

        app_link = self.client.get(f"{MILESTONE_URL}{app_ms.milestone_id}/")
        tool_link = self.client.get(f"{MILESTONE_URL}{tool_ms.milestone_id}/")

        self.assertEqual((app_link.status_code, tool_link.status_code), (410, 410))
        self.assertEqual(tool_link.data["code"], app_link.data["code"])
        app_to, tool_to = app_link.data["convertedToTask"], tool_link.data["convertedToTask"]
        self.assertEqual(app_to["taskId"], app_ms.task_id)
        self.assertEqual(tool_to["taskId"], tool_ms.task_id)
        self.assertEqual(
            {k: v for k, v in tool_to.items() if k not in ("taskId", "displayId")},
            {k: v for k, v in app_to.items() if k not in ("taskId", "displayId")},
        )

    def test_the_tools_answer_names_what_the_apps_does(self):
        # The app's answer: `{milestone, task: {taskId, displayId, projectId, isMilestone, milestoneId}}`
        # and `{task, convertedMilestoneId, subTasksKept}`. The tool's carries the same facts.
        app_root, tool_root = self.root_world(APP), self.root_world(TOOL)
        app = self.app_to_milestone(app_root).data
        tool = self.tool({"action": "convert", "task_id": tool_root.task_id})
        self.assertEqual(tool["milestone_id"], app["task"]["milestoneId"] + 1)
        self.assertEqual(tool["task_id"], tool_root.task_id)
        self.assertEqual(tool["display_id"], self.fresh(tool_root).display_id)
        self.assertEqual(tool["project_id"], app["task"]["projectId"])

        app_ms, tool_ms = self.milestone_world(APP), self.milestone_world(TOOL)
        app = self.app_to_task(app_ms).data
        tool = self.tool({"action": "convert", "milestone_id": tool_ms.milestone_id})
        self.assertEqual(tool["sub_tasks_kept"], app["subTasksKept"])
        self.assertEqual(tool["converted_milestone_id"], tool_ms.milestone_id)
        self.assertEqual(tool["task_id"], tool_ms.task_id)


class MoveTwinTests(TwinBase):
    def test_a_milestone_move_is_what_the_apps_patch_does_and_no_more(self):
        app_ms, tool_ms = self.milestone_world(APP), self.milestone_world(TOOL)
        # tags on the milestone and every task, which a move clears
        tag = [{"tagName": "Feature"}]
        for world, ms in ((APP, app_ms), (TOOL, tool_ms)):
            MilestoneMaster.objects.filter(pk=ms.pk).update(tags=tag)
            self.tasks_of(world).update(tags=tag)

        app = self.record(lambda: self.app_move(app_ms), APP)
        tool = self.record(
            lambda: self.tool(
                {
                    "action": "update",
                    "milestone_id": tool_ms.milestone_id,
                    "project_id": self.dest.project_id,
                }
            ),
            TOOL,
        )

        self.assertEqual(app["result"].status_code, 200)
        self.assertEqual(tool["result"]["project_id"], self.dest.project_id)
        self.assert_twins(app, tool)
        shape = tool["shape"]
        for name, row in shape["tasks"].items():
            self.assertEqual(row["project"], "DST", name)
            self.assertEqual(row["display_prefix"], "DST", name)
            self.assertIsNone(row["sprint"], name)
            self.assertFalse(row["tags"], name)
        ms = shape["milestones"]["Launch"]
        self.assertEqual((ms["project"], ms["folder"], ms["sprint"]), ("DST", None, None))
        self.assertFalse(ms["tags"])

    def test_both_projects_task_lists_are_dropped_by_each(self):
        # The table the app reads is cached per project; a move empties one and fills the other, so
        # both have to be forgotten, by the PATCH and by the tool alike.
        from origin.services.task_cache import (  # noqa: PLC0415
            get_cached_project_tasks,
            set_cached_project_tasks,
        )

        app_ms, tool_ms = self.milestone_world(APP), self.milestone_world(TOOL)
        moves = (
            ("app", lambda: self.app_move(app_ms)),
            (
                "tool",
                lambda: self.tool(
                    {
                        "action": "update",
                        "milestone_id": tool_ms.milestone_id,
                        "project_id": self.dest.project_id,
                    }
                ),
            ),
        )
        for name, run in moves:
            with self.subTest(path=name):
                for project in (self.project, self.dest):
                    set_cached_project_tasks(self.team.team_id, project.project_id, {"x": 1})
                with self.captureOnCommitCallbacks(execute=True):
                    run()
                for project in (self.project, self.dest):
                    self.assertIsNone(
                        get_cached_project_tasks(self.team.team_id, project.project_id),
                        f"{project.code} still cached after the {name}'s move",
                    )
                # put the milestone back for the next path's own world: not needed, worlds are twins


# --------------------------------------------------------------------------------------------
# the role matrix: the same person through the app's endpoint and through the tool
# --------------------------------------------------------------------------------------------


class RoleMatrixTests(TwinBase):
    """Each role acts on a world of its own through the app and through the tool: the answer is the
    same allow or deny, and a deny changes nothing. The one row where the two differ ON PURPOSE (a
    project of another team) is its own test and is asserted."""

    def person(self, name, *, team=True, project=None):
        user = User.objects.create_user(
            username=name, email=f"{name}@test.com", password="pw123456"
        )
        if team:
            TeamMembers.objects.create(team=self.team, attendee=user)
        if project is not None:
            ProjectMembers.objects.create(team=self.team, project=project, attendee=user)
        return user

    def roles(self):
        member = self.person("rm_member", project=self.project)
        assignee = self.person("rm_assignee")
        reporter = self.person("rm_reporter")
        bystander = self.person("rm_bystander")
        removed = self.person("rm_removed", project=self.project)
        ProjectMembers.objects.filter(attendee=removed).delete()
        TeamMembers.objects.filter(attendee=removed).delete()
        outsider = User.objects.create_user(
            username="rm_outsider", email="rm_out@test.com", password="pw123456"
        )
        other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@test.com", owner=outsider
        )
        TeamMembers.objects.create(team=other_team, attendee=outsider)
        # (role, user, the team the tool runs in, fields of the task the role acts on, allowed)
        return [
            ("project member", member, self.team, {}, True),
            ("project owner", self.owner, self.team, {}, True),
            (
                "assignee who is not on the project",
                assignee,
                self.team,
                {"assignee": assignee},
                False,
            ),
            (
                "reporter who is not on the project",
                reporter,
                self.team,
                {"reporter": reporter},
                False,
            ),
            ("team member on no project", bystander, self.team, {}, False),
            ("a member who was removed", removed, self.team, {}, False),
            ("a user of another team", outsider, other_team, {}, False),
        ]

    def act_app(self, url, body, user):
        self.auth(user)
        return self.client.post(url, body, format="json")

    def test_a_root_task_into_a_milestone(self):
        for role_name, user, team, fields, allowed in self.roles():
            with self.subTest(role=role_name):
                rest_task = self.task(f"rest {role_name}", **fields)
                tool_task = self.task(f"tool {role_name}", **fields)
                response = self.act_app(
                    "/api/v2/task/convert-to-milestone/", {"task_id": rest_task.task_id}, user
                )
                try:
                    self.tool_as(
                        user,
                        team,
                        {"action": "convert", "task_id": tool_task.task_id},
                    )
                    tool_allowed = True
                except ToolError:
                    tool_allowed = False
                self.assertEqual(response.status_code == 200, allowed, response.status_code)
                self.assertEqual(tool_allowed, allowed)
                for row in (rest_task, tool_task):
                    self.assertEqual(self.fresh(row).is_milestone, allowed)

    def test_a_milestone_into_a_task(self):
        for role_name, user, team, _fields, allowed in self.roles():
            with self.subTest(role=role_name):
                rest_ms = self.milestone(f"rest {role_name}")
                tool_ms = self.milestone(f"tool {role_name}")
                response = self.act_app(
                    "/api/v2/milestone/convert-to-task/",
                    {"milestone_id": rest_ms.milestone_id},
                    user,
                )
                try:
                    self.tool_as(
                        user, team, {"action": "convert", "milestone_id": tool_ms.milestone_id}
                    )
                    tool_allowed = True
                except ToolError:
                    tool_allowed = False
                self.assertEqual(response.status_code == 200, allowed, response.status_code)
                self.assertEqual(tool_allowed, allowed)
                for ms in (rest_ms, tool_ms):
                    self.assertEqual(MilestoneMaster.objects.get(pk=ms.pk).is_deleted, allowed)

    def test_a_milestone_move(self):
        # The roles are on the SOURCE project; every one that is a member of it is also put on the
        # destination, so what is being tested is the source rule (the destination has its own test).
        for role_name, user, team, _fields, allowed in self.roles():
            with self.subTest(role=role_name):
                if allowed and user != self.owner:
                    ProjectMembers.objects.get_or_create(
                        team=self.team, project=self.dest, attendee=user
                    )
                rest_ms = self.milestone(f"rest {role_name}")
                tool_ms = self.milestone(f"tool {role_name}")
                self.auth(user)
                response = self.client.patch(
                    f"{MILESTONE_URL}{rest_ms.milestone_id}/",
                    {"project_id": self.dest.project_id},
                    format="json",
                )
                try:
                    self.tool_as(
                        user,
                        team,
                        {
                            "action": "update",
                            "milestone_id": tool_ms.milestone_id,
                            "project_id": self.dest.project_id,
                        },
                    )
                    tool_allowed = True
                except ToolError:
                    tool_allowed = False
                self.assertEqual(response.status_code == 200, allowed, response.status_code)
                self.assertEqual(tool_allowed, allowed)
                for ms in (rest_ms, tool_ms):
                    moved = MilestoneMaster.objects.get(pk=ms.pk).project_id == self.dest.project_id
                    self.assertEqual(moved, allowed)

    def tool_as(self, user, team, args):
        set_current_user(user, via={"kind": "agent"})
        try:
            return MANAGE_MILESTONE.run(args, self.ctx(user, team))
        finally:
            clear_current_user()

    def test_a_soft_deleted_task_is_refused_by_both(self):
        gone_rest = self.task("rest gone", is_deleted=True, status="Deleted")
        gone_tool = self.task("tool gone", is_deleted=True, status="Deleted")
        response = self.act_app(
            "/api/v2/task/convert-to-milestone/", {"task_id": gone_rest.task_id}, self.owner
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["code"], "task_deleted")
        with self.assertRaisesRegex(ToolError, "has been deleted"):
            self.tool_as(self.owner, self.team, {"action": "convert", "task_id": gone_tool.task_id})
        for row in (gone_rest, gone_tool):
            self.assertFalse(self.fresh(row).is_milestone)

    def test_a_deleted_milestone_is_refused_by_both(self):
        rest_ms, tool_ms = self.milestone("rest del"), self.milestone("tool del")
        MilestoneMaster.objects.filter(pk__in=[rest_ms.pk, tool_ms.pk]).update(is_deleted=True)
        response = self.act_app(
            "/api/v2/milestone/convert-to-task/", {"milestone_id": rest_ms.milestone_id}, self.owner
        )
        self.assertEqual(response.status_code, 404)
        with self.assertRaisesRegex(ToolError, "has been deleted"):
            self.tool_as(
                self.owner, self.team, {"action": "convert", "milestone_id": tool_ms.milestone_id}
            )

    # ---- the destination of a move -------------------------------------------------------------

    def test_the_destination_rule_is_the_same_but_for_another_teams_project(self):
        # owner is on the source (CNV) and, below, on each kind of destination.
        rows = []
        # a project owner does not need a ProjectMembers row; the rest need one on the destination
        not_a_member = ProjectMaster.objects.create(
            team=self.team,
            project_name="Closed",
            code="CLS",
            owner=self.alice,
            project_system_user=self.alice,
        )
        deleted = ProjectMaster.objects.create(
            team=self.team,
            project_name="Gone",
            code="GON",
            owner=self.owner,
            project_system_user=self.owner,
            is_deleted=True,
        )
        rows.append(("a project the person is on", self.dest, True))
        rows.append(("a project the person is not on", not_a_member, False))
        rows.append(("a deleted project", deleted, False))
        for name, project, allowed in rows:
            with self.subTest(destination=name):
                rest_ms, tool_ms = self.milestone(f"rest {name}"), self.milestone(f"tool {name}")
                self.auth(self.owner)
                response = self.client.patch(
                    f"{MILESTONE_URL}{rest_ms.milestone_id}/",
                    {"project_id": project.project_id},
                    format="json",
                )
                try:
                    self.tool_as(
                        self.owner,
                        self.team,
                        {
                            "action": "update",
                            "milestone_id": tool_ms.milestone_id,
                            "project_id": project.project_id,
                        },
                    )
                    tool_allowed = True
                except ToolError:
                    tool_allowed = False
                self.assertEqual(response.status_code == 200, allowed, response.status_code)
                self.assertEqual(tool_allowed, allowed)

    def test_a_project_of_another_team_is_where_the_tool_is_stricter_on_purpose(self):
        # `update_task` answers "not found" for a project of another team (`project_move_error`); the
        # tool follows it. The PATCH has no team in the request and lets a person who is on both
        # projects move a milestone between them.
        guest_team = TeamMaster.objects.create(
            team_name="Partner", team_email="partner@test.com", owner=self.owner
        )
        TeamMembers.objects.create(team=guest_team, attendee=self.owner)
        shared = ProjectMaster.objects.create(
            team=guest_team,
            project_name="Shared",
            code="SHR",
            owner=self.owner,
            project_system_user=self.owner,
        )
        ProjectMembers.objects.create(team=guest_team, project=shared, attendee=self.owner)
        rest_ms, tool_ms = self.milestone("rest other team"), self.milestone("tool other team")

        self.auth(self.owner)
        response = self.client.patch(
            f"{MILESTONE_URL}{rest_ms.milestone_id}/",
            {"project_id": shared.project_id},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        with self.assertRaisesRegex(ToolError, f"Project {shared.project_id} not found"):
            self.tool_as(
                self.owner,
                self.team,
                {
                    "action": "update",
                    "milestone_id": tool_ms.milestone_id,
                    "project_id": shared.project_id,
                },
            )
        self.assertEqual(
            MilestoneMaster.objects.get(pk=tool_ms.pk).project_id, self.project.project_id
        )
        self.assertEqual(MilestoneMaster.objects.get(pk=rest_ms.pk).project_id, shared.project_id)


class MovePeopleTwinTests(TwinBase):
    def test_people_named_by_a_move_are_judged_against_the_destination_by_both(self):
        # A guest holds a project row and no team row. Naming one as an assignee is fine in the
        # project they are on (the source) and refused in a project they are not on (the destination),
        # and a move is judged by where the milestone ENDS UP, by the PATCH and by the tool.
        guest = User.objects.create_user(username="guest", email="guest@test.com", password="x")
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=guest)
        rest_ms, tool_ms = self.milestone("rest guest"), self.milestone("tool guest")

        self.auth(self.owner)
        response = self.client.patch(
            f"{MILESTONE_URL}{rest_ms.milestone_id}/",
            {"project_id": self.dest.project_id, "assignee_ids": [str(guest.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        with self.assertRaises(ToolError):
            self.tool_move(tool_ms, assignee_ids=[str(guest.id)])
        for ms in (rest_ms, tool_ms):
            self.assertEqual(
                MilestoneMaster.objects.get(pk=ms.pk).project_id, self.project.project_id
            )
        # without a move the same person is accepted by both (the source rule)
        response = self.client.patch(
            f"{MILESTONE_URL}{rest_ms.milestone_id}/",
            {"assignee_ids": [str(guest.id)]},
            format="json",
        )
        self.assertEqual(response.status_code, 200)
        out = self.tool(
            {
                "action": "update",
                "milestone_id": tool_ms.milestone_id,
                "assignee_ids": [str(guest.id)],
            }
        )
        self.assertEqual(out["assignee_ids"], [str(guest.id)])

    def tool_move(self, milestone, **extra):
        return self.tool(
            {
                "action": "update",
                "milestone_id": milestone.milestone_id,
                "project_id": self.dest.project_id,
                **extra,
            }
        )


class SharedMoveCodeTests(TwinBase):
    """The move now lives in `update_milestone(project_move=...)`, which the PATCH and the tool share.
    What only a cross-team move shows (the tool refuses that destination, the PATCH allows it) is
    pinned through the PATCH, so a break in the shared code is caught where only one path reaches."""

    def test_a_cross_team_patch_move_takes_the_assignee_rows_to_the_new_team(self):
        guest_team = TeamMaster.objects.create(
            team_name="Partner", team_email="partner2@test.com", owner=self.owner
        )
        TeamMembers.objects.create(team=guest_team, attendee=self.owner)
        shared = ProjectMaster.objects.create(
            team=guest_team,
            project_name="Shared",
            code="SH2",
            owner=self.owner,
            project_system_user=self.owner,
        )
        ProjectMembers.objects.create(team=guest_team, project=shared, attendee=self.owner)
        ms = self.milestone("Crossing", assignees=(self.owner,))
        self.auth(self.owner)

        response = self.client.patch(
            f"{MILESTONE_URL}{ms.milestone_id}/", {"project_id": shared.project_id}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        rows = MilestoneAssignees.objects.filter(milestone=ms)
        self.assertEqual({str(r.team_id) for r in rows}, {str(guest_team.team_id)})
        ms.refresh_from_db()
        self.assertEqual(str(ms.team_id), str(guest_team.team_id))

    def test_the_tool_drops_the_source_task_list_as_well_as_the_destinations(self):
        from origin.search_engine.agent.tools import manage_milestone as module  # noqa: PLC0415

        ms = self.milestone_world(TOOL)
        with mock.patch.object(module, "invalidate_project_tasks_cache") as dropped:
            self.tool(
                {
                    "action": "update",
                    "milestone_id": ms.milestone_id,
                    "project_id": self.dest.project_id,
                }
            )
        called = {tuple(map(str, c.args)) for c in dropped.call_args_list}
        self.assertIn((str(self.team.team_id), str(self.project.project_id)), called)
        self.assertIn((str(self.team.team_id), str(self.dest.project_id)), called)
