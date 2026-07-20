"""Team permission roles: owner / editor / viewer.

The load-bearing invariant is that **ownership lives in
`TeamMaster.owner`, not in `TeamMembers.member_role`** — so the owner's
own row still reads the `viewer` default. Every gate must therefore go
through `resolve_team_role`; a check written against the raw column
would lock the owner out of their own team. `test_owner_row_still_reads
_viewer_but_owner_can_manage` pins exactly that.
"""

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.member_roles import EDITOR, OWNER, VIEWER, can_manage, resolve_team_role

User = get_user_model()

_LOCMEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "team-member-role-tests",
    }
}


@override_settings(CACHES=_LOCMEM_CACHE)
class TestTeamMemberRoles(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.owner = User.objects.create_user(
            username="roleowner", email="roleowner@test.com", password="pw"
        )
        self.editor = User.objects.create_user(
            username="roleeditor", email="roleeditor@test.com", password="pw"
        )
        self.viewer = User.objects.create_user(
            username="roleviewer", email="roleviewer@test.com", password="pw"
        )
        self.team = TeamMaster.objects.create(
            team_name="Role Team", team_email="role@test.com", owner=self.owner
        )
        self.owner_row = TeamMembers.objects.create(team=self.team, attendee=self.owner)
        self.editor_row = TeamMembers.objects.create(
            team=self.team, attendee=self.editor, member_role=EDITOR
        )
        self.viewer_row = TeamMembers.objects.create(team=self.team, attendee=self.viewer)
        self._auth(self.owner)

    def _auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}")

    def _set_role(self, target, role):
        return self.client.put(
            "/api/v2/team/member-role/",
            {"team_id": str(self.team.team_id), "user_id": str(target.id), "member_role": role},
            format="json",
        )

    def _rename(self, name="Renamed Team"):
        return self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "team_name": name},
            format="json",
        )

    def _get_team_members(self):
        """Returns the member list out of the delta envelope
        (`{server_time, data: {members: [...]}}`)."""
        res = self.client.get(
            f"/api/v2/team/getTeamMembers/?team_id={self.team.team_id}&user_id={self.owner.id}"
        )
        return res, (res.data.get("data", {}).get("members", []) if res.status_code == 200 else [])

    def _get_my_teams(self):
        return self.client.get(f"/api/v2/team/getMyTeams/?user_id={self.owner.id}")

    # ── The owner-is-not-in-the-column invariant ───────────────────

    def test_new_members_default_to_viewer(self):
        self.assertEqual(self.viewer_row.member_role, VIEWER)

    def test_owner_row_still_reads_viewer_but_owner_can_manage(self):
        """The regression this whole design hinges on.

        The owner's stored `member_role` is the `viewer` default, because
        ownership is the FK. Any gate reading the column directly would
        deny them; `resolve_team_role` must return OWNER.
        """
        self.assertEqual(self.owner_row.member_role, VIEWER)
        self.assertEqual(resolve_team_role(self.team, self.owner.id), OWNER)
        self.assertTrue(can_manage(resolve_team_role(self.team, self.owner.id)))
        # ...and end-to-end through a real gate.
        self.assertEqual(self._rename().status_code, 200)

    def test_resolve_role_for_editor_and_viewer(self):
        self.assertEqual(resolve_team_role(self.team, self.editor.id), EDITOR)
        self.assertEqual(resolve_team_role(self.team, self.viewer.id), VIEWER)

    def test_transfer_moves_owner_role_without_column_writes(self):
        """Ownership transfer needs no role-column bookkeeping."""
        self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "owner_id": str(self.editor.id)},
            format="json",
        )
        self.team.refresh_from_db()
        self.assertEqual(resolve_team_role(self.team, self.editor.id), OWNER)
        # The previous owner falls back to their stored column value.
        self.assertEqual(resolve_team_role(self.team, self.owner.id), VIEWER)

    # ── Role-set endpoint ──────────────────────────────────────────

    def test_owner_can_promote_viewer_to_editor(self):
        res = self._set_role(self.viewer, EDITOR)
        self.assertEqual(res.status_code, 200)
        self.viewer_row.refresh_from_db()
        self.assertEqual(self.viewer_row.member_role, EDITOR)

    def test_editor_can_manage_roles(self):
        """An editor promoting a viewer is the point — role management
        must not funnel back through one person."""
        self._auth(self.editor)
        self.assertEqual(self._set_role(self.viewer, EDITOR).status_code, 200)

    def test_viewer_cannot_manage_roles(self):
        self._auth(self.viewer)
        self.assertEqual(self._set_role(self.editor, VIEWER).status_code, 403)

    def test_cannot_assign_owner_role(self):
        """Minting an owner is a transfer, not a role edit."""
        res = self._set_role(self.viewer, OWNER)
        self.assertEqual(res.status_code, 400)

    def test_cannot_assign_unknown_role(self):
        self.assertEqual(self._set_role(self.viewer, "superadmin").status_code, 400)

    def test_cannot_change_the_owners_role(self):
        res = self._set_role(self.owner, VIEWER)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(resolve_team_role(self.team, self.owner.id), OWNER)

    def test_role_for_non_member_404s(self):
        outsider = User.objects.create_user(
            username="outsider", email="outsider@test.com", password="pw"
        )
        self.assertEqual(self._set_role(outsider, EDITOR).status_code, 404)

    # ── Gate relaxation ────────────────────────────────────────────

    def test_editor_can_rename_team(self):
        self._auth(self.editor)
        self.assertEqual(self._rename("Editor Renamed").status_code, 200)

    def test_viewer_cannot_rename_team(self):
        self._auth(self.viewer)
        self.assertEqual(self._rename("Viewer Renamed").status_code, 403)

    def test_editor_cannot_transfer_ownership(self):
        """Editor == owner minus the destructive powers."""
        self._auth(self.editor)
        res = self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "owner_id": str(self.viewer.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.team.refresh_from_db()
        self.assertEqual(str(self.team.owner_id), str(self.owner.id))

    def test_editor_can_invite_members(self):
        """The bottleneck this feature exists to remove."""
        self._auth(self.editor)
        res = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["newhire@test.com"]},
            format="json",
        )
        self.assertNotEqual(res.status_code, 403)

    def test_viewer_cannot_invite_members(self):
        self._auth(self.viewer)
        res = self.client.post(
            "/api/v2/team/invite/",
            {"team_id": str(self.team.team_id), "emails": ["newhire@test.com"]},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_viewer_cannot_change_team_image(self):
        """This endpoint had NO authorization check before this change."""
        self._auth(self.viewer)
        res = self.client.put(
            "/api/v2/team/profile/image/",
            {"team_id": str(self.team.team_id), "team_profile_image": ""},
            format="multipart",
        )
        self.assertIn(res.status_code, (400, 403))

    # ── Serialization: memberRole must survive every builder ───────
    #
    # The failure mode here is silent — an omitted key just renders
    # everyone as a viewer forever — so each payload site gets a test.

    def test_get_team_members_carries_member_role(self):
        res, members = self._get_team_members()
        self.assertEqual(res.status_code, 200)
        by_id = {str(m["userId"]): m for m in members}
        self.assertEqual(by_id[str(self.editor.id)]["memberRole"], EDITOR)
        self.assertEqual(by_id[str(self.viewer.id)]["memberRole"], VIEWER)

    def test_get_team_members_keeps_job_title_role_separate(self):
        """`role` (job title) and `memberRole` (permission) must not
        collide — they are different axes on the same payload."""
        self.editor.role = "Staff Engineer"
        self.editor.save(update_fields=["role"])
        _res, members = self._get_team_members()
        row = next(m for m in members if str(m["userId"]) == str(self.editor.id))
        self.assertEqual(row["role"], "Staff Engineer")
        self.assertEqual(row["memberRole"], EDITOR)

    def test_my_teams_carries_member_role(self):
        res = self._get_my_teams()
        self.assertEqual(res.status_code, 200)
        team = next(t for t in res.data if str(t["teamId"]) == str(self.team.team_id))
        by_id = {str(m["userId"]): m for m in team["teamMembers"]}
        self.assertEqual(by_id[str(self.editor.id)]["memberRole"], EDITOR)

    def test_member_info_carries_member_role(self):
        res = self.client.get(
            f"/api/v2/team/getTeamMemberInfo/?team_id={self.team.team_id}&user_id={self.editor.id}"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["memberRole"], EDITOR)

    def test_role_write_invalidates_cached_member_payloads(self):
        """The TeamMembers post_save receiver clears my_teams +
        member_info for that attendee, so a role change is visible
        immediately rather than after the 60s TTL."""
        self._get_my_teams()  # primes the cache
        self._set_role(self.viewer, EDITOR)
        res = self._get_my_teams()
        team = next(t for t in res.data if str(t["teamId"]) == str(self.team.team_id))
        by_id = {str(m["userId"]): m for m in team["teamMembers"]}
        self.assertEqual(by_id[str(self.viewer.id)]["memberRole"], EDITOR)
