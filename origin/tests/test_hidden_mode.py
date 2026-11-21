"""Hidden mode for private projects and group chats (MIP-1699).

A Hidden item is private and its name reaches only its members: not the project list (Join list,
Spotlight `#`, mentions), not the chat search box, not a join request. One fixture, one outsider,
one member; every listing path asserts both sides so a new path has to be added here.
"""

from __future__ import annotations

from datetime import timedelta

from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.invite_models import TeamInvite
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.hidden_scope import visible_channels_q, visible_projects_q
from origin.tests.test_base import BaseAPITestCase

PROJECTS = "/api/v2/project/projects/"
PROJECT = "/api/v2/project/"
SEARCH = "/api/v3/search/teamMembersAndGroups/"
JOIN_PROJECT_REQ = "/api/v2/inbox/joinProjectRequest/"
JOIN_GM_REQ = "/api/v2/inbox/joinGMRequest/"


class HiddenBase(BaseAPITestCase):
    """self.user owns and belongs to both hidden items; self.user2 is a team member outside."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Codename Falcon",
            code="FAL",
            owner=self.user,
            project_system_user=self.user,
            is_private=True,
            is_hidden=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.public = ProjectMaster.objects.create(
            team=self.team, project_name="Open", code="OPN", owner=self.user
        )
        self.gm = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Secret Chat",
            owner=self.user,
            is_private=True,
            is_hidden=True,
        )
        ChannelMember.objects.create(channel=self.gm, user=self.user, role="owner")
        self.private_visible = Channel.objects.create(
            team=self.team,
            kind=ChannelKind.GM,
            title="Locked Chat",
            owner=self.user,
            is_private=True,
        )
        ChannelMember.objects.create(channel=self.private_visible, user=self.user, role="owner")

    def names(self, res):
        return {r["projectName"] for r in res.json()}


class ListingPathTests(HiddenBase):
    def test_project_list_hides_it_from_an_outsider_and_shows_it_to_members(self):
        self.authenticate(self.user2)
        res = self.client.get(PROJECTS, {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("Codename Falcon", self.names(res))
        self.assertIn("Open", self.names(res))
        self.authenticate(self.user)
        res = self.client.get(PROJECTS, {"team_id": str(self.team.team_id)})
        rows = {r["projectName"]: r for r in res.json()}
        self.assertTrue(rows["Codename Falcon"]["isHidden"])

    def test_the_owner_without_a_member_row_still_sees_it(self):
        ProjectMembers.objects.filter(project=self.project).delete()
        self.authenticate(self.user)
        res = self.client.get(PROJECTS, {"team_id": str(self.team.team_id)})
        self.assertIn("Codename Falcon", self.names(res))

    def test_the_archived_list_hides_it_too_and_the_toggle_clears_that_cache(self):
        self.authenticate(self.user2)
        url = PROJECTS
        params = {"team_id": str(self.team.team_id), "include_archived": "1"}
        self.assertNotIn("Codename Falcon", self.names(self.client.get(url, params)))
        # A private (visible) project made hidden disappears at once from the cached archived list.
        other = ProjectMaster.objects.create(
            team=self.team, project_name="Later", code="LAT", owner=self.user, is_private=True
        )
        self.assertIn("Later", self.names(self.client.get(url, params)))
        other.is_hidden = True
        other.save()
        self.assertNotIn("Later", self.names(self.client.get(url, params)))

    def test_chat_search_hides_it_from_an_outsider_and_shows_it_to_members(self):
        self.authenticate(self.user2)
        res = self.client.get(SEARCH, {"team_id": str(self.team.team_id)})
        names = {r["name"] for r in res.json()["results"] if r["type"] == "Group"}
        self.assertNotIn("Secret Chat", names)
        self.assertIn("Locked Chat", names)
        self.authenticate(self.user)
        res = self.client.get(SEARCH, {"team_id": str(self.team.team_id)})
        names = {r["name"] for r in res.json()["results"] if r["type"] == "Group"}
        self.assertIn("Secret Chat", names)

    def test_self_join_of_a_hidden_chat_is_a_404_not_a_403(self):
        self.authenticate(self.user2)
        res = self.client.post(f"/api/v3/channels/{self.gm.id}/join/")
        self.assertEqual(res.status_code, 404)
        res = self.client.post(f"/api/v3/channels/{self.private_visible.id}/join/")
        self.assertEqual(res.status_code, 403)

    def test_join_requests_for_hidden_items_are_refused_and_write_nothing(self):
        self.authenticate(self.user2)
        body = {
            "team_id": str(self.team.team_id),
            "item_body": [{"type": "paragraph", "content": []}],
            "item_type": 3,
        }
        res = self.client.post(
            JOIN_PROJECT_REQ,
            {**body, "item_optionals": {"project_id": self.project.project_id}},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        res = self.client.post(
            JOIN_GM_REQ, {**body, "item_optionals": {"gm_id": str(self.gm.id)}}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(InboxItems.objects.exists())

    def test_self_join_of_a_hidden_project_is_a_404(self):
        self.authenticate(self.user2)
        res = self.client.post(
            "/api/v2/project/join/",
            {
                "team_id": str(self.team.team_id),
                "project_id": self.project.project_id,
                "attendee_id": str(self.user2.id),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_pending_invite_list_masks_a_hidden_project_name_for_a_non_member_manager(self):
        TeamInvite.objects.create(
            team=self.team,
            invited_email="new@example.com",
            member_role="member",
            project=self.project,
            invited_by=self.user,
            token_hash="x" * 64,
            expires_at=timezone.now() + timedelta(days=7),
            status="pending",
        )
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(member_role="editor")
        self.authenticate(self.user2)
        res = self.client.get("/api/v2/team/invites/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["invites"][0]["projectName"], "Hidden project")
        self.authenticate(self.user)
        res = self.client.get("/api/v2/team/invites/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.json()["invites"][0]["projectName"], "Codename Falcon")

    def test_the_directory_never_lists_a_hidden_chat(self):
        # The directory (MIP-1694) lists public chats only; this pins that a hidden chat, which is
        # always private, can never satisfy its filter.
        self.assertFalse(Channel.objects.filter(is_private=False, is_hidden=True).exists())
        self.assertEqual(
            set(
                Channel.objects.filter(team=self.team, kind=ChannelKind.GM)
                .filter(visible_channels_q(self.user2.id))
                .values_list("title", flat=True)
            ),
            {"Locked Chat"},
        )
        self.assertEqual(
            set(
                ProjectMaster.objects.filter(team=self.team)
                .filter(visible_projects_q(self.user2.id))
                .values_list("project_name", flat=True)
            ),
            {"Open"},
        )


class AlreadySafePathTests(HiddenBase):
    """Paths that were members-only before Hidden: pinned so they stay that way."""

    def test_the_mcp_project_list_is_members_only(self):
        from types import SimpleNamespace

        from origin.views.public.mcp.list_paging import run_list_projects

        ctx = SimpleNamespace(user_id=self.user2.id, team_id=self.team.team_id)
        ProjectMembers.objects.create(team=self.team, project=self.public, attendee=self.user2)
        names = {p["project_name"] for p in run_list_projects({}, ctx)["projects"]}
        self.assertNotIn("Codename Falcon", names)


class ModelRuleTests(HiddenBase):
    def test_save_clears_hidden_when_not_private(self):
        self.project.is_private = False
        self.project.save()
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_hidden)

    def test_the_database_refuses_hidden_without_private(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            ProjectMaster.objects.filter(pk=self.project.pk).update(is_private=False)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Channel.objects.filter(pk=self.gm.pk).update(is_private=False)


class ToggleTests(HiddenBase):
    def patch_gm(self, gm, **body):
        return self.client.patch(f"/api/v3/channels/{gm.id}/", body, format="json")

    def test_gm_owner_turns_hidden_on_and_off(self):
        self.authenticate(self.user)
        res = self.patch_gm(self.private_visible, is_hidden=True)
        self.assertEqual(res.status_code, 200, res.content)
        self.private_visible.refresh_from_db()
        self.assertTrue(self.private_visible.is_hidden)
        self.assertTrue(res.json()["channel"]["isHidden"])
        res = self.patch_gm(self.private_visible, is_hidden=False)
        self.private_visible.refresh_from_db()
        self.assertFalse(self.private_visible.is_hidden)

    def test_gm_hidden_needs_private_and_public_clears_hidden(self):
        self.authenticate(self.user)
        public = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Pub", owner=self.user
        )
        ChannelMember.objects.create(channel=public, user=self.user, role="owner")
        self.assertEqual(self.patch_gm(public, is_hidden=True).status_code, 400)
        self.assertEqual(self.patch_gm(self.gm, is_hidden="yes").status_code, 400)
        res = self.patch_gm(self.gm, is_private=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.gm.refresh_from_db()
        self.assertFalse(self.gm.is_hidden)

    def test_a_plain_gm_member_cannot_switch_it(self):
        ChannelMember.objects.create(channel=self.gm, user=self.user2, role="member")
        self.authenticate(self.user2)
        self.assertEqual(self.patch_gm(self.gm, is_hidden=False).status_code, 403)

    def test_create_gm_hidden_needs_private(self):
        self.authenticate(self.user)
        base = {"kind": 2, "team_id": str(self.team.team_id), "title": "New"}
        res = self.client.post("/api/v3/channels/", {**base, "is_hidden": True}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            "/api/v3/channels/", {**base, "is_private": True, "is_hidden": True}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(Channel.objects.get(title="New").is_hidden)

    def put(self, **body):
        return self.client.put(
            PROJECT, {"project_id": self.project.project_id, **body}, format="json"
        )

    def test_project_owner_switches_private_and_hidden(self):
        self.authenticate(self.user)
        res = self.put(is_hidden=False)
        self.assertEqual(res.status_code, 200, res.content)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_hidden)
        self.assertTrue(self.project.is_private)
        self.assertEqual(self.put(is_hidden=True).status_code, 200)
        self.assertEqual(self.put(is_private=False).status_code, 200)
        self.project.refresh_from_db()
        self.assertFalse(self.project.is_private)
        self.assertFalse(self.project.is_hidden)
        self.assertEqual(self.put(is_hidden=True).status_code, 400)
        self.assertEqual(self.put(is_private="x").status_code, 400)

    def test_only_the_project_owner_may_switch(self):
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.user2, member_role="editor"
        )
        self.authenticate(self.user2)
        self.assertEqual(self.put(is_hidden=False).status_code, 403)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_hidden)

    def test_create_project_hidden_needs_private(self):
        self.authenticate(self.user)
        base = {"team": str(self.team.team_id), "project_name": "Fresh"}
        res = self.client.post(PROJECT, {**base, "is_hidden": True}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post(
            PROJECT, {**base, "is_private": True, "is_hidden": True}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(ProjectMaster.objects.get(project_name="Fresh").is_hidden)

    def test_a_non_member_put_with_only_a_project_id_learns_nothing(self):
        self.authenticate(self.user2)
        res = self.put()
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("Codename Falcon", res.content.decode())

    def test_a_refused_rename_does_not_apply_the_privacy_change(self):
        self.authenticate(self.user)
        res = self.put(is_private=False, project_name="Open")  # name taken by another project
        self.assertGreaterEqual(res.status_code, 400)
        self.project.refresh_from_db()
        self.assertTrue(self.project.is_private)
        self.assertTrue(self.project.is_hidden)
