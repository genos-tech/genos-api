"""MIP-1693 (Ken, 2026-11-22): in an archived project, access management stays allowed and content
changes and deletion are refused.

* Deleting an archived project is refused with the archived reason on every path: the page's
  `DELETE /api/v2/project/`, and the in-app agent's `delete_project`. MCP never publishes a delete
  (`manage_project` has `create` and `update` only), pinned here too. Unarchive, then delete.
* Leaving, removing a member, approving a join request and adding members to the project's group
  chats keep working in an archived project.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError
from origin.services.archive import ARCHIVED_MESSAGE
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_inbox_approval_authz import APPROVE_PRJ, JOIN_PRJ_REQ

User = get_user_model()

PROJECT = "/api/v2/project/"
LEAVE = "/api/v2/project/leave/"
REMOVE = "/api/v2/project/member/remove/"


class ArchivedProjectBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Done",
            code="DON",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.archive()

    def archive(self):
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(
            archived_at=timezone.now()
        )
        self.project.refresh_from_db()

    def is_member(self, user):
        return ProjectMembers.objects.filter(project=self.project, attendee=user).exists()


class DeleteArchivedProject(ArchivedProjectBase):
    def delete_page(self):
        self.authenticate(self.user)
        return self.client.delete(
            f"{PROJECT}?team_id={self.team.team_id}&project_id={self.project.project_id}"
        )

    def test_the_page_refuses_with_the_archived_reason(self):
        res = self.delete_page()
        self.assertEqual(res.status_code, 409, res.content)
        self.assertEqual(res.data["code"], "archived")
        self.assertTrue(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_unarchive_then_delete_works(self):
        ProjectMaster.objects.filter(project_id=self.project.project_id).update(archived_at=None)
        res = self.delete_page()
        self.assertEqual(res.status_code, 204, res.content)
        self.assertFalse(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_a_non_owner_still_gets_the_owner_message_not_the_archive_one(self):
        self.authenticate(self.user2)
        res = self.client.delete(
            f"{PROJECT}?team_id={self.team.team_id}&project_id={self.project.project_id}"
        )
        self.assertNotEqual(res.status_code, 204)
        self.assertTrue(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_the_agent_tool_refuses_and_deletes_nothing(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolError) as caught:
            REGISTRY["delete_project"].run(
                {"project_id": self.project.project_id, "project_name": "Done"}, ctx
            )
        self.assertIn(ARCHIVED_MESSAGE, str(caught.exception))
        self.assertTrue(ProjectMaster.objects.filter(project_id=self.project.project_id).exists())

    def test_mcp_publishes_no_project_delete(self):
        schema = REGISTRY["manage_project"].parameters_schema
        self.assertNotIn("delete", schema["properties"]["action"].get("enum", []))


class AccessManagementStaysAllowed(ArchivedProjectBase):
    def test_a_member_can_leave(self):
        self.authenticate(self.user2)
        res = self.client.post(
            LEAVE,
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self.is_member(self.user2))

    def test_the_owner_can_remove_a_member(self):
        self.authenticate(self.user)
        res = self.client.post(
            REMOVE,
            {"project_id": self.project.project_id, "user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertFalse(self.is_member(self.user2))

    def test_the_owner_can_approve_a_join_request(self):
        newcomer = User.objects.create_user(
            username="newcomer", email="newcomer@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=newcomer)
        self.authenticate(newcomer)
        res = self.client.post(
            JOIN_PRJ_REQ,
            {
                "team_id": str(self.team.team_id),
                "item_body": "let me in",
                "item_type": 2,
                "item_optionals": {
                    "project_id": self.project.project_id,
                    "project_name": self.project.project_name,
                },
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        item_id = res.data["data"]["itemId"]
        self.authenticate(self.user)
        res = self.client.post(
            APPROVE_PRJ, {"team_id": str(self.team.team_id), "item_id": item_id}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(self.is_member(newcomer))

    def test_members_can_be_added_to_the_projects_group_chat(self):
        gm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Ops",
            owner=self.user,
            is_private=True,
            project=self.project,
        )
        ChannelMember.objects.create(channel=gm, user=self.user)
        self.authenticate(self.user)
        res = self.client.post(
            f"/api/v3/channels/{gm.id}/members/",
            {"user_ids": [str(self.user2.id)]},
            format="json",
        )
        self.assertIn(res.status_code, (200, 201), res.content)
        self.assertTrue(ChannelMember.objects.filter(channel=gm, user=self.user2).exists())
