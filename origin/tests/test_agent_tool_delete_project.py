"""`delete_project` (MIP-882): the in-app agent can delete a project, and nothing else can.

A project delete cascades and has no undo: its tasks (with comments, attachments and notes),
milestones, sprints, members and tags go. The page puts a confirm dialog in front of it. The
tool is stricter, because an agent can be wrong about which project:

* **The owner only**, as on the page; an editor, a plain member or an outsider is refused.
* **The name as well as the id.** A wrong id, or a project looked up under an earlier name,
  is caught before anything is deleted.
* **In the app only.** Over MCP there is no approval card to put in front of this, so the
  tool is never published (Ken's decision 3).

The page's delete now calls the service the tool calls; `test_project_views` pins what the
page answers. The last class drives both on twin projects and compares what is left.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import override_settings

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.sprint_models import Sprint
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent import controller
from origin.search_engine.agent.controller import PENDING_APPROVAL_MARKER, resume_agent
from origin.search_engine.agent.model_fence import fence_for_model
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden, wrap_workspace_content
from origin.search_engine.agent.tools.delete_project import DELETE_PROJECT
from origin.search_engine.models import AgentRun, AgentStep
from origin.services.milestone_service import ensure_backing_task
from origin.services.project_delete import delete_project, project_contents
from origin.views.public.mcp import tools as mcp_tools

from .test_base import BaseAPITestCase

User = get_user_model()

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "project-delete-tests",
    }
}


class DeleteBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = self.make_project("Doomed", "DOO")
        self.ctx = self.ctx_of(self.user)
        self.stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@example.com", owner=self.stranger
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.stranger)

    # ---- fixtures --------------------------------------------------------

    def make_project(self, name, code, *, owner=None):
        owner = owner or self.user
        project = ProjectMaster.objects.create(
            team=self.team, project_name=name, code=code, owner=owner, project_system_user=owner
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=owner)
        return project

    def fill(self, project, *, tasks=2, milestones=1, sprints=1):
        """Everything the delete is meant to take with it."""
        for n in range(tasks):
            TaskMaster.objects.create(
                team=self.team,
                project=project,
                reporter=self.user,
                title=f"Task {n}",
                status="Open",
                project_task_number=n + 1,
            )
        for n in range(milestones):
            ensure_backing_task(
                MilestoneMaster.objects.create(
                    team=self.team, project=project, reporter=self.user, title=f"Milestone {n}"
                )
            )
        for n in range(sprints):
            Sprint.objects.create(
                team=self.team,
                project=project,
                name=f"Sprint {n}",
                sequence_number=n + 1,
                start_date=date(2026, 11, 1 + n * 10),
                end_date=date(2026, 11, 7 + n * 10),
            )
        ProjectTags.objects.create(
            team=self.team,
            project=project,
            tag_id=1,
            tag_name="urgent",
            tag_color="#ff2323",
            tag_text_color="white",
        )

    def person(self, name, role):
        user = User.objects.create_user(username=name, email=f"{name}@example.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=user)
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=user, member_role=role
        )
        return user

    def ctx_of(self, user, team=None):
        return ToolContext(team_id=str((team or self.team).team_id), user_id=str(user.id))

    def delete(self, project=None, ctx=None, **args):
        project = project or self.project
        args = {"project_id": project.project_id, "project_name": project.project_name, **args}
        return DELETE_PROJECT.run(args, ctx or self.ctx)

    def refused(self, call, message, kind=ToolError):
        with self.assertRaises(kind) as caught:
            call()
        self.assertIn(message, str(caught.exception))

    def exists(self, project=None):
        return ProjectMaster.objects.filter(
            project_id=(project or self.project).project_id
        ).exists()


class TheToolIsAnApprovalGatedWrite(DeleteBase):
    def test_it_needs_approval_and_is_registered(self):
        self.assertTrue(DELETE_PROJECT.requires_approval)
        self.assertIs(REGISTRY["delete_project"], DELETE_PROJECT)

    def test_the_description_leads_with_the_read_and_says_what_goes(self):
        text = DELETE_PROJECT.description
        self.assertIn("list_projects", text)
        self.assertIn("for good", text)
        self.assertIn("owner", text)
        for what in ("tasks", "milestones", "sprints", "members", "tags", "chat"):
            self.assertIn(what, text)

    def test_both_arguments_are_required(self):
        self.assertEqual(
            DELETE_PROJECT.parameters_schema["required"], ["project_id", "project_name"]
        )


class DeletingAProject(DeleteBase):
    def test_the_owner_deletes_it_and_everything_in_it(self):
        self.fill(self.project)
        out = self.delete()
        self.assertFalse(self.exists())
        self.assertFalse(TaskMaster.objects.filter(project_id=self.project.project_id).exists())
        self.assertFalse(Sprint.objects.filter(project_id=self.project.project_id).exists())
        self.assertFalse(
            MilestoneMaster.objects.filter(project_id=self.project.project_id).exists()
        )
        self.assertFalse(ProjectTags.objects.filter(project_id=self.project.project_id).exists())
        self.assertFalse(ProjectMembers.objects.filter(project_id=self.project.project_id).exists())
        self.assertTrue(out["deleted"])
        self.assertEqual(out["project_id"], self.project.project_id)
        self.assertEqual(out["project_name"], "Doomed")

    def test_the_result_says_how_much_went(self):
        self.fill(self.project, tasks=3, milestones=2, sprints=2)
        before = project_contents(self.project)
        out = self.delete()
        self.assertEqual(out["contents"], before)
        self.assertEqual(before["tasks"], 3 + 2)  # a milestone has a backing task of its own
        self.assertEqual((before["milestones"], before["sprints"], before["members"]), (2, 2, 1))
        self.assertEqual(
            out["__summary__"],
            f'Deleted the project "Doomed" with {before["tasks"]} task(s), 2 milestone(s) '
            "and 2 sprint(s). This cannot be undone.",
        )

    def test_the_project_chat_goes_but_its_messages_stay(self):
        channel = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        message = Message.objects.create(
            channel=channel, sender=self.user, seq=1, body={"text": "hi"}, body_text="hi"
        )
        self.delete()
        channel.refresh_from_db()
        self.assertTrue(channel.is_deleted)
        self.assertIsNone(channel.project_id)
        self.assertTrue(Message.objects.filter(id=message.id).exists())

    def test_another_projects_things_are_left_alone(self):
        sibling = self.make_project("Safe", "SAF")
        self.fill(sibling, tasks=2, milestones=1, sprints=1)
        self.fill(self.project)
        sibling_channel = Channel.objects.get(project=sibling, kind=ChannelKind.PM)
        self.delete()
        self.assertTrue(self.exists(sibling))
        self.assertEqual(TaskMaster.objects.filter(project_id=sibling.project_id).count(), 3)
        self.assertEqual(Sprint.objects.filter(project_id=sibling.project_id).count(), 1)
        self.assertTrue(ProjectTags.objects.filter(project_id=sibling.project_id).exists())
        sibling_channel.refresh_from_db()
        self.assertFalse(sibling_channel.is_deleted)
        self.assertEqual(sibling_channel.project_id, sibling.project_id)
        self.assertTrue(
            ChannelMember.objects.filter(channel=sibling_channel, user=self.user).exists()
        )

    def test_the_cached_project_list_is_dropped(self):
        """`/project/projects/` is cached for a minute; a deleted project must not linger."""
        key = f"project:list:{self.team.team_id}:{self.user2.id}"
        with override_settings(CACHES=_LOCMEM_CACHE):
            cache.set(key, {"stale": True})
            self.delete()
            self.assertIsNone(cache.get(key))

    def test_an_empty_project_is_deleted_too(self):
        self.assertTrue(self.delete()["deleted"])
        self.assertFalse(self.exists())

    def test_the_result_names_the_project_under_a_fenced_key(self):
        out = self.delete()
        fenced = fence_for_model(out, "delete_project")
        self.assertEqual(fenced["project_name"], wrap_workspace_content("Doomed"))
        self.assertEqual(fenced["project_id"], self.project.project_id)


class TheNameMustMatch(DeleteBase):
    def test_a_wrong_name_deletes_nothing_and_says_which_project_it_is(self):
        self.fill(self.project)
        with self.assertRaises(ToolError) as caught:
            self.delete(project_name="Something else")
        message = str(caught.exception)
        self.assertIn("must be the project's name exactly", message)
        self.assertIn(f"project {self.project.project_id} is named", message)
        # The real name is user text, so it comes back fenced.
        self.assertIn(wrap_workspace_content("Doomed"), message)
        self.assertIn("Nothing was deleted", message)
        self.assertTrue(self.exists())
        self.assertTrue(TaskMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_the_match_is_exact(self):
        for given in ("doomed", "DOOMED", "Doomed ", " Doomed", "Doomed!", "", "Doom"):
            with self.subTest(project_name=given):
                self.refused(lambda: self.delete(project_name=given), "name exactly")
                self.assertTrue(self.exists())

    def test_a_name_that_is_not_text_is_refused(self):
        for given in (None, 7, ["Doomed"], {"name": "Doomed"}, True):
            with self.subTest(project_name=given):
                self.refused(lambda: self.delete(project_name=given), "name exactly")
        self.refused(
            lambda: DELETE_PROJECT.run({"project_id": self.project.project_id}, self.ctx),
            "name exactly",
        )
        self.assertTrue(self.exists())

    def test_a_name_with_odd_characters_still_matches_itself(self):
        weird = self.make_project('He said "hi" <b>& 日本語', "WEI")
        self.delete(project=weird)
        self.assertFalse(self.exists(weird))

    def test_the_right_name_of_another_project_does_not_delete_this_one(self):
        other = self.make_project("Keep me", "KEE")
        self.refused(
            lambda: DELETE_PROJECT.run(
                {"project_id": self.project.project_id, "project_name": other.project_name},
                self.ctx,
            ),
            "name exactly",
        )
        self.assertTrue(self.exists())
        self.assertTrue(self.exists(other))


class WhoMayDelete(DeleteBase):
    def test_an_editor_may_not(self):
        editor = self.person("ed", "editor")
        self.refused(
            lambda: self.delete(ctx=self.ctx_of(editor)),
            "Only the project owner can delete the project.",
            ToolForbidden,
        )
        self.assertTrue(self.exists())

    def test_a_viewer_may_not(self):
        viewer = self.person("vi", "viewer")
        self.refused(
            lambda: self.delete(ctx=self.ctx_of(viewer)),
            "Only the project owner can delete the project.",
            ToolForbidden,
        )
        self.assertTrue(self.exists())

    def test_someone_outside_the_project_is_refused_before_anything_is_said_about_owners(self):
        outsider = User.objects.create_user(username="out", email="out@example.com", password="pw")
        TeamMembers.objects.create(team=self.team, attendee=outsider)
        self.refused(
            lambda: self.delete(ctx=self.ctx_of(outsider)),
            "not a member of this project",
            ToolForbidden,
        )
        self.assertTrue(self.exists())

    def test_a_project_of_another_team_is_out_of_reach(self):
        self.refused(
            lambda: self.delete(ctx=self.ctx_of(self.stranger, self.other_team)),
            "different team",
            ToolForbidden,
        )
        self.assertTrue(self.exists())

    def test_being_the_owner_of_the_team_is_not_being_the_owner_of_the_project(self):
        project = self.make_project("Theirs", "THE", owner=self.user2)
        ProjectMembers.objects.get_or_create(team=self.team, project=project, attendee=self.user)
        self.assertEqual(self.team.owner_id, self.user.id)
        self.refused(
            lambda: self.delete(project=project),
            "Only the project owner",
            ToolForbidden,
        )
        self.assertTrue(self.exists(project))

    def test_a_project_with_no_owner_cannot_be_deleted_by_a_member(self):
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(owner=None)
        self.refused(lambda: self.delete(), "Only the project owner", ToolForbidden)
        self.assertTrue(self.exists())

    def test_the_owner_is_the_run_user_not_anything_in_the_arguments(self):
        # There is no owner argument to trust; a stray one is ignored by the run.
        editor = self.person("ed", "editor")
        self.refused(
            lambda: self.delete(ctx=self.ctx_of(editor), owner_id=str(self.user.id)),
            "Only the project owner",
            ToolForbidden,
        )

    def test_a_missing_or_deleted_project_is_not_found(self):
        self.refused(
            lambda: DELETE_PROJECT.run({"project_id": 999999, "project_name": "x"}, self.ctx),
            "not found",
        )
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(is_deleted=True)
        self.refused(lambda: self.delete(), "not found")

    def test_the_id_must_be_a_number(self):
        self.refused(
            lambda: DELETE_PROJECT.run({"project_id": "abc", "project_name": "x"}, self.ctx),
            "project_id",
        )


class TheServiceChecksItself(DeleteBase):
    def test_contents_count_this_project_only(self):
        sibling = self.make_project("Other", "OTH")
        self.fill(sibling, tasks=4, milestones=2, sprints=3)
        self.fill(self.project, tasks=1, milestones=1, sprints=1)
        # A milestone has a backing task of its own, so one task and one milestone are
        # two task rows.
        self.assertEqual(
            project_contents(self.project),
            {"tasks": 2, "milestones": 1, "sprints": 1, "members": 1},
        )

    def test_a_failure_part_way_leaves_the_chat_attached(self):
        """The channel is released and the project deleted together: if the delete fails,
        the channel must still belong to the project, or it is stranded."""
        channel = Channel.objects.get(project=self.project, kind=ChannelKind.PM)
        with mock.patch.object(ProjectMaster, "delete", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                delete_project(self.project)
        channel.refresh_from_db()
        self.assertFalse(channel.is_deleted)
        self.assertEqual(channel.project_id, self.project.project_id)
        self.assertTrue(self.exists())

    def test_the_tool_turns_an_unexpected_failure_into_a_tool_error(self):
        with mock.patch(
            "origin.search_engine.agent.tools.delete_project.delete_project",
            side_effect=RuntimeError("boom"),
        ):
            self.refused(lambda: self.delete(), "Failed to delete the project (ref ")
        self.assertTrue(self.exists())


class WhatTheBrowserIsTold(DeleteBase):
    """The stream's `tool_call_result` carries a `project` ref for a delete, so the app can
    drop the project from the sidebar and close its page."""

    def approve(self, tool_name, result):
        run = AgentRun.objects.create(
            team_id="t1",
            user_id="u1",
            query="do it",
            status="awaiting_approval",
            pending_approval_token=uuid.uuid4(),
        )
        AgentStep.objects.create(
            run=run,
            step_index=0,
            tool_name=tool_name,
            arguments_json={},
            summary=PENDING_APPROVAL_MARKER,
        )
        fake = SimpleNamespace(
            name=tool_name,
            requires_approval=True,
            run=mock.Mock(return_value={**result, "__summary__": "done"}),
        )
        events = []
        with (
            mock.patch.object(controller, "_drive_loop", return_value=None),
            mock.patch.dict(REGISTRY, {tool_name: fake}),
        ):
            resume_agent(run, "approve", ToolContext(team_id="t1", user_id="u1"), events.append)
        (event,) = [e for e in events if e["type"] == "tool_call_result"]
        return event

    def test_a_delete_names_the_project_and_says_it_is_gone(self):
        event = self.approve("delete_project", {"project_id": 12, "project_name": "Doomed"})
        self.assertEqual(event["project"], {"project_id": 12, "deleted": True})

    def test_the_ref_leaves_out_the_name(self):
        event = self.approve("delete_project", {"project_id": 12, "project_name": "Doomed"})
        self.assertNotIn("project_name", event["project"])

    def test_no_project_id_means_no_ref(self):
        self.assertNotIn("project", self.approve("delete_project", {"deleted": True}))

    def test_another_tool_with_the_same_keys_gets_no_ref(self):
        event = self.approve("delete_sprint", {"project_id": 12, "deleted": True})
        self.assertNotIn("project", event)

    def test_the_card_names_the_project_by_the_same_rule_as_the_other_project_tools(self):
        out = controller._friendly_arguments(
            {"project_id": self.project.project_id, "project_name": "Doomed"},
            "delete_project",
            ctx=self.ctx,
        )
        self.assertEqual(out["project_id"], "Doomed")
        # A teammate not on the project is not told its name.
        out = controller._friendly_arguments(
            {"project_id": self.project.project_id, "project_name": "Doomed"},
            "delete_project",
            ctx=self.ctx_of(self.user2),
        )
        self.assertEqual(out["project_id"], self.project.project_id)


class InTheAppOnly(DeleteBase):
    def test_it_is_not_on_the_mcp_catalog(self):
        self.assertNotIn("delete_project", {t.name for t in mcp_tools.CATALOG})
        self.assertNotIn("delete_project", {t.registry_name for t in mcp_tools.CATALOG})

    def test_the_catalog_says_why(self):
        self.assertIn("delete_project", mcp_tools.__doc__)
        self.assertIn("in the app only", mcp_tools.__doc__)


class ThePageAndTheToolLeaveTheSameThingsBehind(DeleteBase):
    """Twin projects, one deleted through the page and one through the tool."""

    def test_the_same_rows_survive(self):
        twin_a = self.make_project("Twin A", "TWA")
        twin_b = self.make_project("Twin B", "TWB")
        for twin in (twin_a, twin_b):
            self.fill(twin, tasks=2, milestones=1, sprints=1)
        chats = {
            twin.project_id: Channel.objects.get(project=twin, kind=ChannelKind.PM)
            for twin in (twin_a, twin_b)
        }
        messages = {
            twin.project_id: Message.objects.create(
                channel=chats[twin.project_id],
                sender=self.user,
                seq=1,
                body={"text": "hi"},
                body_text="hi",
            )
            for twin in (twin_a, twin_b)
        }

        self.authenticate()
        page = self.client.delete(
            f"/api/v2/project/?team_id={self.team.team_id}&project_id={twin_a.project_id}"
        )
        self.assertEqual(page.status_code, 204)
        self.delete(project=twin_b)

        def what_is_left(twin):
            channel = Channel.objects.get(id=chats[twin.project_id].id)
            return {
                "project": self.exists(twin),
                "tasks": TaskMaster.objects.filter(project_id=twin.project_id).count(),
                "milestones": MilestoneMaster.objects.filter(project_id=twin.project_id).count(),
                "sprints": Sprint.objects.filter(project_id=twin.project_id).count(),
                "tags": ProjectTags.objects.filter(project_id=twin.project_id).count(),
                "members": ProjectMembers.objects.filter(project_id=twin.project_id).count(),
                "message": Message.objects.filter(id=messages[twin.project_id].id).exists(),
                "channel_deleted": channel.is_deleted,
                "channel_project": channel.project_id,
            }

        self.assertEqual(what_is_left(twin_a), what_is_left(twin_b))
        self.assertEqual(what_is_left(twin_a)["message"], True)

    def test_the_page_still_answers_what_it_did(self):
        self.authenticate()
        gone = self.make_project("Gone", "GON")
        res = self.client.delete(
            f"/api/v2/project/?team_id={self.team.team_id}&project_id={gone.project_id}"
        )
        self.assertEqual(res.status_code, 204)
        self.assertFalse(self.exists(gone))
        again = self.client.delete(
            f"/api/v2/project/?team_id={self.team.team_id}&project_id={gone.project_id}"
        )
        self.assertEqual(again.status_code, 404)
