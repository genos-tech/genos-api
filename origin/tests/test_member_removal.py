"""An owner or editor can remove another member (MIP-1371).

Before: a team and a project had no way to remove someone else (only Leave), a group chat
let an editor remove the channel's owner, and a removed person kept finding the tasks they
were on in search. Now:

* `can_remove_member`: the owner removes anyone but themselves; an editor only viewers and
  guests; nobody removes the owner or themselves (Leave is its own action).
* Team: `POST team/member/remove/`; project: `POST project/member/remove/`; group chat:
  the existing DELETE, now with the same rule and an owner that cannot leave.
* The cleanup: the open work in their name is unassigned when asked, their pending join
  requests are withdrawn, search stops listing them on tasks they were only on as assignee or
  reporter, and the calendar stops following their tasks.
* The agent / MCP tool `manage_project_members` gains `remove` on the same service.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.api_key_models import ApiKey
from origin.models.common.inbox_models import InboxItems
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.acl_dirty import claim
from origin.search_engine.agent.acl import task_acl_user_ids
from origin.search_engine.agent.tools.base import ToolContext, ToolError, ToolForbidden
from origin.search_engine.agent.tools.manage_project_members import MANAGE_PROJECT_MEMBERS
from origin.search_engine.models import SearchAclDirty
from origin.services.member_removal import active_team_member_ids, count_open_work
from origin.services.member_roles import (
    EDITOR,
    GUEST,
    OWNER,
    VIEWER,
    can_remove_member,
)

User = get_user_model()

REMOVE_TEAM = "/api/v2/team/member/remove/"
REMOVE_PROJECT = "/api/v2/project/member/remove/"


class CanRemoveMemberTests(TestCase):
    def test_the_matrix(self):
        cases = {
            (OWNER, OWNER): False,  # nobody removes the owner (a transfer is the way)
            (OWNER, EDITOR): True,
            (OWNER, VIEWER): True,
            (OWNER, GUEST): True,
            (EDITOR, OWNER): False,
            (EDITOR, EDITOR): False,  # a dispute between peers, with nobody above to appeal to
            (EDITOR, VIEWER): True,
            (EDITOR, GUEST): True,
            (VIEWER, OWNER): False,
            (VIEWER, EDITOR): False,
            (VIEWER, VIEWER): False,
            (GUEST, VIEWER): False,
            (None, VIEWER): False,
            (EDITOR, None): False,
        }
        for (actor, target), expected in cases.items():
            self.assertEqual(can_remove_member(actor, target), expected, (actor, target))


class Base(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = self._user("owner")
        self.editor = self._user("editor")
        self.editor2 = self._user("editor2")
        self.viewer = self._user("viewer")
        self.outsider = self._user("outsider")
        self.team = TeamMaster.objects.create(
            team_name="Removal Team", team_email="removal@test.com", owner=self.owner
        )
        TeamMembers.objects.create(team=self.team, attendee=self.owner)
        TeamMembers.objects.create(team=self.team, attendee=self.editor, member_role=EDITOR)
        TeamMembers.objects.create(team=self.team, attendee=self.editor2, member_role=EDITOR)
        TeamMembers.objects.create(team=self.team, attendee=self.viewer)
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Removal Project",
            owner=self.owner,
            project_system_user=self.owner,
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Other Project",
            owner=self.owner,
            project_system_user=self.owner,
        )
        for u, role in (
            (self.owner, VIEWER),
            (self.editor, EDITOR),
            (self.editor2, EDITOR),
            (self.viewer, VIEWER),
        ):
            for project in (self.project, self.other_project):
                ProjectMembers.objects.create(
                    team=self.team, project=project, attendee=u, member_role=role
                )

    @staticmethod
    def _user(name):
        return User.objects.create_user(
            username=f"rm{name}", email=f"rm{name}@test.com", password="pw12345678"
        )

    def auth(self, user):
        token = RefreshToken.for_user(user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def task(self, title, *, assignee=None, reporter=None, status="Open", project=None, **kw):
        return TaskMaster.objects.create(
            team=self.team,
            project=project or self.project,
            title=title,
            status=status,
            assignee=assignee,
            reporter=reporter,
            **kw,
        )

    def remove_team(self, user, **extra):
        return self.client.post(
            REMOVE_TEAM,
            {"team_id": str(self.team.team_id), "user_id": str(user.id), **extra},
            format="json",
        )

    def remove_project(self, user, project=None, **extra):
        return self.client.post(
            REMOVE_PROJECT,
            {
                "project_id": (project or self.project).project_id,
                "user_id": str(user.id),
                **extra,
            },
            format="json",
        )

    def on_team(self, user):
        return TeamMembers.objects.filter(team=self.team, attendee=user, is_deleted=False).exists()


class TeamRemovalTests(Base):
    def test_the_owner_removes_a_viewer_and_the_cascade_runs(self):
        ProjectMembers.objects.filter(attendee=self.viewer).update(member_role=VIEWER)
        key = ApiKey.objects.create(
            user=self.viewer, team=self.team, name="k", key_hash="h" * 64, prefix="gk_test"
        )
        personal = ApiKey.objects.create(
            user=self.viewer, team=None, name="p", key_hash="p" * 64, prefix="gk_pers"
        )
        self.auth(self.owner)
        res = self.remove_team(self.viewer)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self.on_team(self.viewer))
        self.assertFalse(ProjectMembers.objects.filter(attendee=self.viewer).exists())
        # The row is soft-deleted, so a re-invite can bring them back.
        self.assertTrue(TeamMembers.objects.filter(team=self.team, attendee=self.viewer).exists())
        key.refresh_from_db()
        personal.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)  # a team key ends with the membership
        self.assertIsNone(personal.revoked_at)  # a personal token spans other teams

    def test_the_owner_removes_an_editor(self):
        self.auth(self.owner)
        self.assertEqual(self.remove_team(self.editor).status_code, 200)
        self.assertFalse(self.on_team(self.editor))

    def test_an_editor_removes_a_viewer(self):
        self.auth(self.editor)
        self.assertEqual(self.remove_team(self.viewer).status_code, 200)
        self.assertFalse(self.on_team(self.viewer))

    def test_an_editor_cannot_remove_another_editor_or_the_owner(self):
        self.auth(self.editor)
        res = self.remove_team(self.editor2)
        self.assertEqual(res.status_code, 403)
        self.assertTrue(self.on_team(self.editor2))
        res = self.remove_team(self.owner)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(self.on_team(self.owner))

    def test_nobody_removes_the_owner_not_even_the_owner(self):
        self.auth(self.owner)
        self.assertEqual(self.remove_team(self.owner).status_code, 400)
        self.assertTrue(self.on_team(self.owner))

    def test_you_cannot_remove_yourself_here_leave_is_for_that(self):
        self.auth(self.editor)
        res = self.remove_team(self.editor)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Leave", res.json()["error"])
        self.assertTrue(self.on_team(self.editor))

    def test_a_viewer_and_a_stranger_get_the_answer_of_a_team_that_is_not_there(self):
        missing = {
            "team_id": "00000000-0000-0000-0000-000000000000",
            "user_id": str(self.editor.id),
        }
        self.auth(self.viewer)
        for payload in (
            {"team_id": str(self.team.team_id), "user_id": str(self.editor2.id)},
            missing,
        ):
            res = self.client.post(REMOVE_TEAM, payload, format="json")
            self.assertEqual(res.status_code, 404, payload)
        self.auth(self.outsider)
        res = self.remove_team(self.viewer)
        self.assertEqual(res.status_code, 404)
        self.assertTrue(self.on_team(self.viewer))

    def test_someone_not_on_the_team_is_a_404(self):
        self.auth(self.owner)
        self.assertEqual(self.remove_team(self.outsider).status_code, 404)

    def test_an_already_removed_member_is_a_404_not_a_second_event(self):
        self.auth(self.owner)
        self.assertEqual(self.remove_team(self.viewer).status_code, 200)
        self.assertEqual(self.remove_team(self.viewer).status_code, 404)
        self.assertEqual(
            SecurityEvent.objects.filter(
                action=SecurityAction.MEMBER_REMOVED, target_id=str(self.viewer.id)
            ).count(),
            1,
        )

    def test_missing_ids_are_a_400(self):
        self.auth(self.owner)
        res = self.client.post(REMOVE_TEAM, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_unauthenticated_is_refused(self):
        res = self.client.post(REMOVE_TEAM, {}, format="json")
        self.assertIn(res.status_code, (401, 403))

    def test_the_security_event_says_who_removed_whom_and_that_it_was_not_a_leave(self):
        self.auth(self.owner)
        self.remove_team(self.viewer)
        event = SecurityEvent.objects.get(
            action=SecurityAction.MEMBER_REMOVED, target_id=str(self.viewer.id)
        )
        self.assertEqual(event.metadata["via"], "removed")
        self.assertEqual(event.metadata["scope"], "team")
        self.assertEqual(str(event.actor_id), str(self.owner.id))

    def test_leaving_is_still_a_leave(self):
        self.auth(self.viewer)
        res = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.viewer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        event = SecurityEvent.objects.get(action=SecurityAction.MEMBER_REMOVED)
        self.assertEqual(event.metadata["via"], "left")

    def test_the_preview_counts_what_the_dialog_names(self):
        self.task("open", assignee=self.viewer)
        self.task("closed", assignee=self.viewer, status="Closed")
        self.task("other", assignee=self.editor)
        self.auth(self.owner)
        res = self.client.get(
            REMOVE_TEAM, {"team_id": str(self.team.team_id), "user_id": str(self.viewer.id)}
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["openWork"], {"tasks": 1, "milestones": 0})
        # The preview is the same gate: an editor asking about another editor learns 403.
        self.auth(self.editor)
        res = self.client.get(
            REMOVE_TEAM, {"team_id": str(self.team.team_id), "user_id": str(self.editor2.id)}
        )
        self.assertEqual(res.status_code, 403)


class CleanupTests(Base):
    def test_open_work_is_unassigned_when_asked_and_history_is_kept(self):
        open_task = self.task("open", assignee=self.viewer)
        closed = self.task("closed", assignee=self.viewer, status="Closed")
        deleted = self.task("deleted", assignee=self.viewer, is_deleted=True)
        elsewhere = self.task("someone else's", assignee=self.editor)
        self.auth(self.owner)
        res = self.remove_team(self.viewer, unassign_open_tasks=True)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["unassigned"], {"tasks": 1, "milestones": 0})
        for t in (open_task, closed, deleted, elsewhere):
            t.refresh_from_db()
        self.assertIsNone(open_task.assignee_id)
        self.assertEqual(closed.assignee_id, self.viewer.id)
        self.assertEqual(deleted.assignee_id, self.viewer.id)
        self.assertEqual(elsewhere.assignee_id, self.editor.id)

    def test_without_the_flag_nothing_is_unassigned(self):
        open_task = self.task("open", assignee=self.viewer)
        self.auth(self.owner)
        res = self.remove_team(self.viewer)
        self.assertEqual(res.json()["unassigned"], {"tasks": 0, "milestones": 0})
        open_task.refresh_from_db()
        self.assertEqual(open_task.assignee_id, self.viewer.id)
        # Only a literal true counts: a truthy string is not a yes.
        held = self.task("held", assignee=self.editor)
        self.auth(self.owner)
        res = self.remove_team(self.editor, unassign_open_tasks="true")
        self.assertEqual(res.json()["unassigned"], {"tasks": 0, "milestones": 0})
        held.refresh_from_db()
        self.assertEqual(held.assignee_id, self.editor.id)

    def test_a_milestone_assignment_is_removed_too(self):
        backing = self.task("Milestone", is_milestone=True)
        ms = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="M", task=backing
        )
        row = MilestoneAssignees.objects.create(team=self.team, milestone=ms, user=self.viewer)
        self.assertEqual(count_open_work(self.team.team_id, self.viewer.id)["milestones"], 1)
        self.auth(self.owner)
        res = self.remove_team(self.viewer, unassign_open_tasks=True)
        self.assertEqual(res.json()["unassigned"]["milestones"], 1)
        self.assertFalse(MilestoneAssignees.objects.filter(pk=row.pk).exists())

    def test_the_unassign_goes_through_save_so_the_activity_feed_sees_it(self):
        from origin.models.task.task_activity_models import TaskActivity

        t = self.task("open", assignee=self.viewer)
        before = TaskActivity.objects.filter(task=t).count()
        self.auth(self.owner)
        self.remove_team(self.viewer, unassign_open_tasks=True)
        self.assertGreater(TaskActivity.objects.filter(task=t).count(), before)

    def test_pending_join_requests_are_withdrawn_only_theirs_and_only_pending(self):
        def request(sender, status="pending", item_type=2, project_id=None):
            return InboxItems.objects.create(
                team=self.team,
                sender=sender,
                receiver=self.owner,
                item_type=item_type,
                request_status=status,
                item_optionals={"project_id": project_id} if project_id else None,
            )

        mine = request(self.viewer, project_id=self.project.project_id)
        mine_team = request(self.viewer, item_type=1)
        done = request(self.viewer, status="approved")
        theirs = request(self.editor, project_id=self.project.project_id)
        self.auth(self.owner)
        self.remove_team(self.viewer)
        for item in (mine, mine_team, done, theirs):
            item.refresh_from_db()
        self.assertEqual(mine.request_status, "rejected")
        self.assertEqual(mine_team.request_status, "rejected")
        self.assertEqual(done.request_status, "approved")
        self.assertEqual(theirs.request_status, "pending")

    def test_the_projects_they_were_only_a_task_assignee_on_are_marked_for_a_search_refresh(self):
        lonely = ProjectMaster.objects.create(
            team=self.team, project_name="Lonely", owner=self.owner, project_system_user=self.owner
        )
        self.task("only assigned", assignee=self.viewer, project=lonely)
        SearchAclDirty.objects.all().delete()
        self.auth(self.owner)
        self.remove_team(self.viewer)
        self.assertIn(lonely.project_id, claim().project_ids)

    def test_a_removed_member_leaves_the_task_acl_but_a_current_one_stays(self):
        # Neither is a project member of `lonely`: the assignee/reporter route is the only way in.
        lonely = ProjectMaster.objects.create(
            team=self.team, project_name="Lonely", owner=self.owner, project_system_user=self.owner
        )
        both = {str(self.viewer.id), str(self.editor.id)}
        self.assertEqual(task_acl_user_ids(lonely.project_id, self.viewer.id, self.editor.id), both)
        self.auth(self.owner)
        self.remove_team(self.viewer)
        self.assertEqual(
            task_acl_user_ids(lonely.project_id, self.viewer.id, self.editor.id),
            {str(self.editor.id)},
        )

    def test_the_team_owner_always_counts_as_on_the_team(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.owner).delete()
        self.assertEqual(
            active_team_member_ids(self.team.team_id, [self.owner.id, self.outsider.id]),
            {str(self.owner.id)},
        )

    def test_the_search_chunker_drops_a_removed_assignee_and_reporter(self):
        from origin.search_engine.chunkers.task_chunker import iter_task_chunks

        lonely = ProjectMaster.objects.create(
            team=self.team, project_name="Lonely", owner=self.owner, project_system_user=self.owner
        )
        t = self.task("T", assignee=self.viewer, reporter=self.viewer, project=lonely)

        def acl():
            for entity in iter_task_chunks():
                if entity.entity_id == f"task:{t.task_id}":
                    return set(entity.chunks[0].acl_user_ids)
            self.fail("task not chunked")

        self.assertIn(str(self.viewer.id), acl())
        TeamMembers.objects.filter(team=self.team, attendee=self.viewer).update(is_deleted=True)
        self.assertNotIn(str(self.viewer.id), acl())

    def test_calendar_sync_stops_for_an_assignee_who_left(self):
        from origin.signals import task_signals

        self.viewer.auto_sync_tasks_to_calendar = True
        self.viewer.save(update_fields=["auto_sync_tasks_to_calendar"])
        t = self.task("due", assignee=self.viewer, due_date="2026-12-01")
        with (
            mock.patch.object(task_signals, "get_google_connected_account", return_value=object()),
            mock.patch.object(task_signals, "sync_task_event", return_value="patched") as sync,
        ):
            task_signals._run_upsert(t.pk)
            self.assertEqual(sync.call_count, 1)
            TeamMembers.objects.filter(team=self.team, attendee=self.viewer).update(is_deleted=True)
            task_signals._run_upsert(t.pk)
            self.assertEqual(sync.call_count, 1)


class ProjectRemovalTests(Base):
    def test_the_owner_removes_a_member_and_only_from_that_project(self):
        self.auth(self.owner)
        res = self.remove_project(self.viewer)
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.viewer).exists()
        )
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.other_project, attendee=self.viewer).exists()
        )
        self.assertTrue(self.on_team(self.viewer))

    def test_an_editor_removes_a_viewer_and_a_guest_but_not_an_editor_or_the_owner(self):
        guest = self._user("guest")
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=guest, member_role=GUEST
        )
        self.auth(self.editor)
        self.assertEqual(self.remove_project(self.viewer).status_code, 200)
        self.assertEqual(self.remove_project(guest).status_code, 200)
        self.assertEqual(self.remove_project(self.editor2).status_code, 403)
        self.assertEqual(self.remove_project(self.owner).status_code, 400)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.editor2).exists()
        )

    def test_yourself_is_leave_and_a_viewer_gets_a_404(self):
        self.auth(self.editor)
        self.assertEqual(self.remove_project(self.editor).status_code, 400)
        self.auth(self.viewer)
        self.assertEqual(self.remove_project(self.editor2).status_code, 404)

    def test_unassign_is_limited_to_this_project(self):
        here = self.task("here", assignee=self.viewer)
        there = self.task("there", assignee=self.viewer, project=self.other_project)
        self.auth(self.owner)
        res = self.remove_project(self.viewer, unassign_open_tasks=True)
        self.assertEqual(res.json()["unassigned"], {"tasks": 1, "milestones": 0})
        here.refresh_from_db()
        there.refresh_from_db()
        self.assertIsNone(here.assignee_id)
        self.assertEqual(there.assignee_id, self.viewer.id)

    def test_their_pending_request_for_this_project_is_withdrawn_but_not_another(self):
        def req(pid):
            return InboxItems.objects.create(
                team=self.team,
                sender=self.viewer,
                receiver=self.owner,
                item_type=2,
                request_status="pending",
                item_optionals={"project_id": pid},
            )

        here = req(self.project.project_id)
        there = req(str(self.other_project.project_id))  # stored as a string by some senders
        # A request of ANOTHER kind that happens to name this project's id is not a project
        # join request, and a project removal leaves it alone.
        other_kind = InboxItems.objects.create(
            team=self.team,
            sender=self.viewer,
            receiver=self.owner,
            item_type=3,
            request_status="pending",
            item_optionals={"project_id": self.project.project_id},
        )
        self.auth(self.owner)
        self.remove_project(self.viewer)
        for item in (here, there, other_kind):
            item.refresh_from_db()
        self.assertEqual(here.request_status, "rejected")
        self.assertEqual(there.request_status, "pending")
        self.assertEqual(other_kind.request_status, "pending")

    def test_the_event_names_the_project_and_the_actor(self):
        self.auth(self.owner)
        self.remove_project(self.viewer)
        event = SecurityEvent.objects.get(
            action=SecurityAction.MEMBER_REMOVED, target_id=str(self.viewer.id)
        )
        self.assertEqual(event.metadata["scope"], "project")
        self.assertEqual(event.metadata["via"], "removed")
        self.assertEqual(event.metadata["project_id"], self.project.project_id)

    def test_the_preview_counts_open_work_in_this_project_only(self):
        self.task("here", assignee=self.viewer)
        self.task("there", assignee=self.viewer, project=self.other_project)
        self.auth(self.owner)
        res = self.client.get(
            REMOVE_PROJECT,
            {"project_id": self.project.project_id, "user_id": str(self.viewer.id)},
        )
        self.assertEqual(res.json()["openWork"], {"tasks": 1, "milestones": 0})

    def test_not_a_member_and_missing_project_are_404(self):
        self.auth(self.owner)
        self.assertEqual(self.remove_project(self.outsider).status_code, 404)
        res = self.client.post(
            REMOVE_PROJECT, {"project_id": 999999, "user_id": str(self.viewer.id)}, format="json"
        )
        self.assertEqual(res.status_code, 404)


class GroupChatRemovalTests(Base):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="GM", owner=self.owner
        )
        ChannelMember.objects.create(channel=self.channel, user=self.owner, role="owner")
        ChannelMember.objects.create(channel=self.channel, user=self.editor, role="admin")
        ChannelMember.objects.create(channel=self.channel, user=self.editor2, role="admin")
        ChannelMember.objects.create(channel=self.channel, user=self.viewer, role="member")

    def remove(self, user):
        return self.client.delete(f"/api/v3/channels/{self.channel.id}/members/{user.id}/")

    def active(self, user):
        return ChannelMember.objects.filter(
            channel=self.channel, user=user, is_deleted=False
        ).exists()

    def test_an_editor_can_no_longer_remove_the_owner_or_another_editor(self):
        self.auth(self.editor)
        self.assertEqual(self.remove(self.owner).status_code, 403)
        self.assertEqual(self.remove(self.editor2).status_code, 403)
        self.assertTrue(self.active(self.owner))
        self.assertTrue(self.active(self.editor2))

    def test_an_editor_removes_a_viewer_and_the_owner_removes_an_editor(self):
        self.auth(self.editor)
        self.assertEqual(self.remove(self.viewer).status_code, 204)
        self.assertFalse(self.active(self.viewer))
        self.auth(self.owner)
        self.assertEqual(self.remove(self.editor2).status_code, 204)
        self.assertFalse(self.active(self.editor2))

    def test_the_owner_cannot_leave_their_own_channel(self):
        self.auth(self.owner)
        res = self.remove(self.owner)
        self.assertEqual(res.status_code, 400)
        self.assertTrue(self.active(self.owner))

    def test_a_member_can_still_leave_and_leaving_leaves_no_removal_event(self):
        self.auth(self.viewer)
        self.assertEqual(self.remove(self.viewer).status_code, 204)
        self.assertFalse(self.active(self.viewer))
        self.assertFalse(
            SecurityEvent.objects.filter(action=SecurityAction.MEMBER_REMOVED).exists()
        )

    def test_a_removal_by_a_manager_leaves_a_trace(self):
        self.auth(self.owner)
        self.remove(self.viewer)
        event = SecurityEvent.objects.get(action=SecurityAction.MEMBER_REMOVED)
        self.assertEqual(event.metadata["scope"], "chat")
        self.assertEqual(event.metadata["channel_id"], str(self.channel.id))
        self.assertEqual(str(event.actor_id), str(self.owner.id))

    def test_a_plain_member_cannot_remove_anyone(self):
        self.auth(self.viewer)
        self.assertEqual(self.remove(self.editor).status_code, 403)


class ToolRemovalTests(Base):
    def ctx(self, user):
        return ToolContext(team_id=str(self.team.team_id), user_id=str(user.id))

    def run_tool(self, user, target, **extra):
        return MANAGE_PROJECT_MEMBERS.run(
            {
                "action": "remove",
                "project_id": self.project.project_id,
                "user_id": str(target.id),
                **extra,
            },
            self.ctx(user),
        )

    def test_the_owner_removes_a_member_through_the_tool(self):
        t = self.task("open", assignee=self.viewer)
        out = self.run_tool(self.owner, self.viewer, unassign_open_tasks=True)
        self.assertEqual(out["unassigned"], {"tasks": 1, "milestones": 0})
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.viewer).exists()
        )
        t.refresh_from_db()
        self.assertIsNone(t.assignee_id)

    def test_without_the_flag_the_tool_unassigns_nothing(self):
        t = self.task("open", assignee=self.viewer)
        out = self.run_tool(self.owner, self.viewer)
        self.assertEqual(out["unassigned"], {"tasks": 0, "milestones": 0})
        t.refresh_from_db()
        self.assertEqual(t.assignee_id, self.viewer.id)
        # And only a literal true counts there too.
        t2 = self.task("open2", assignee=self.editor)
        self.run_tool(self.owner, self.editor, unassign_open_tasks="yes")
        t2.refresh_from_db()
        self.assertEqual(t2.assignee_id, self.editor.id)

    def test_the_tool_enforces_the_same_rule(self):
        with self.assertRaises(ToolError) as cm:
            self.run_tool(self.editor, self.editor2)
        self.assertIn("Nothing was changed", str(cm.exception))
        with self.assertRaises(ToolError):
            self.run_tool(self.editor, self.owner)
        with self.assertRaises(ToolError):
            self.run_tool(self.editor, self.editor)  # yourself is Leave
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.editor2).exists()
        )

    def test_a_viewer_is_refused_and_so_is_a_stranger(self):
        with self.assertRaises((ToolError, ToolForbidden)):
            self.run_tool(self.viewer, self.editor2)
        with self.assertRaises((ToolError, ToolForbidden)):
            self.run_tool(self.outsider, self.viewer)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.viewer).exists()
        )

    def test_the_description_says_remove_exists_and_cannot_be_undone(self):
        self.assertNotIn("no way to remove", MANAGE_PROJECT_MEMBERS.description)
        self.assertIn("remove", MANAGE_PROJECT_MEMBERS.description)
        self.assertIn("can't be undone", MANAGE_PROJECT_MEMBERS.description)
        self.assertIn(
            "remove",
            MANAGE_PROJECT_MEMBERS.parameters_schema["properties"]["action"]["enum"],
        )
        self.assertIn("unassign_open_tasks", MANAGE_PROJECT_MEMBERS.parameters_schema["properties"])
