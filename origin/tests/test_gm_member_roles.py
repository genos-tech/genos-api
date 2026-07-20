"""GM (channel) permission roles: owner / editor / viewer.

GM differs from Team and Project in one structural way: `ChannelMember`
ALREADY has a `role` column, with its own `owner|admin|member|system`
vocabulary that messaging depends on. Rather than migrate it, the two
vocabularies are mapped at the API boundary — `admin` (a value that
existed but was never written) means editor, `member` means viewer.

That leaves TWO sources of truth for ownership: `Channel.owner_id` and a
row that may say `"owner"`. The FK is authoritative;
`test_stale_owner_row_does_not_grant_ownership` pins that a row claiming
ownership without the FK behind it is treated as an ordinary member, so
an ownership transfer self-heals.
"""

from django.urls import reverse
from rest_framework import status

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.services.member_roles import EDITOR, OWNER, VIEWER, resolve_gm_role
from origin.tests.test_base import BaseAPITestCase


class GMMemberRoleTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Role GM", owner=self.user
        )
        self.owner_row = ChannelMember.objects.create(
            channel=self.channel, user=self.user, role="owner"
        )
        self.viewer_row = ChannelMember.objects.create(
            channel=self.channel, user=self.user2, role="member"
        )
        self.detail_url = reverse("v3_channel_detail", args=[self.channel.id])

    def _member_url(self, user):
        return f"/api/v3/channels/{self.channel.id}/members/{user.id}/"

    def _set_role(self, target, role):
        return self.client.patch(self._member_url(target), {"member_role": role}, format="json")

    def _rename(self, title="Renamed GM"):
        return self.client.patch(self.detail_url, {"title": title}, format="json")

    # ── Vocabulary mapping ─────────────────────────────────────────

    def test_existing_member_rows_read_as_viewer(self):
        """Every current non-owner is stored as "member" — which is
        exactly the intended viewer default, hence no migration."""
        self.assertEqual(resolve_gm_role(self.channel, self.user2.id), VIEWER)

    def test_admin_column_value_reads_as_editor(self):
        self.viewer_row.role = "admin"
        self.viewer_row.save(update_fields=["role"])
        self.assertEqual(resolve_gm_role(self.channel, self.user2.id), EDITOR)

    def test_owner_resolves_from_the_fk(self):
        self.assertEqual(resolve_gm_role(self.channel, self.user.id), OWNER)

    def test_stale_owner_row_does_not_grant_ownership(self):
        """Two sources of truth; the FK wins so a transfer self-heals."""
        self.viewer_row.role = "owner"
        self.viewer_row.save(update_fields=["role"])
        self.assertEqual(resolve_gm_role(self.channel, self.user2.id), VIEWER)

    def test_serializer_exposes_member_role_without_touching_role(self):
        self.authenticate()
        res = self.client.get(f"/api/v3/channels/{self.channel.id}/members/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        by_id = {str(m["userId"]): m for m in res.data["members"]}
        # `role` keeps its own vocabulary — messaging depends on it.
        self.assertEqual(by_id[str(self.user.id)]["role"], "owner")
        self.assertEqual(by_id[str(self.user2.id)]["role"], "member")
        # ...and the shared axis is derived alongside it.
        self.assertEqual(by_id[str(self.user2.id)]["memberRole"], VIEWER)

    # ── Role endpoint ──────────────────────────────────────────────

    def test_owner_can_promote_member_to_editor(self):
        self.authenticate()
        res = self._set_role(self.user2, EDITOR)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.viewer_row.refresh_from_db()
        # Stored in the EXISTING vocabulary; no migration.
        self.assertEqual(self.viewer_row.role, "admin")
        self.assertEqual(res.data["memberRole"], EDITOR)

    def test_editor_can_manage_roles(self):
        self.viewer_row.role = "admin"
        self.viewer_row.save(update_fields=["role"])
        third = self._make_third_member()
        self.authenticate(self.user2)
        self.assertEqual(self._set_role(third, EDITOR).status_code, status.HTTP_200_OK)

    def test_viewer_cannot_manage_roles(self):
        third = self._make_third_member()
        self.authenticate(self.user2)
        self.assertEqual(self._set_role(third, EDITOR).status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_assign_owner_or_unknown_role(self):
        self.authenticate()
        self.assertEqual(self._set_role(self.user2, OWNER).status_code, 400)
        self.assertEqual(self._set_role(self.user2, "admin").status_code, 400)

    def test_cannot_change_the_owners_role(self):
        self.authenticate()
        self.assertEqual(self._set_role(self.user, VIEWER).status_code, 400)

    def test_pm_channel_roles_are_rejected(self):
        """PM membership mirrors ProjectMembers — the project is the one
        place a PM role is set, or there would be two sources of truth."""
        pm = Channel.objects.create(
            team=self.team, kind=ChannelKind.PM, title="PM", owner=self.user
        )
        ChannelMember.objects.create(channel=pm, user=self.user, role="owner")
        ChannelMember.objects.create(channel=pm, user=self.user2, role="member")
        self.authenticate()
        res = self.client.patch(
            f"/api/v3/channels/{pm.id}/members/{self.user2.id}/",
            {"member_role": EDITOR},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    # ── Gates ──────────────────────────────────────────────────────

    def test_editor_can_rename_but_viewer_cannot(self):
        self.authenticate(self.user2)
        self.assertEqual(self._rename("Viewer Rename").status_code, status.HTTP_403_FORBIDDEN)
        self.viewer_row.role = "admin"
        self.viewer_row.save(update_fields=["role"])
        self.assertEqual(self._rename("Editor Rename").status_code, status.HTTP_200_OK)

    def test_editor_cannot_transfer_ownership(self):
        """The metadata gate now admits editors; ownership must not."""
        self.viewer_row.role = "admin"
        self.viewer_row.save(update_fields=["role"])
        third = self._make_third_member()
        self.authenticate(self.user2)
        res = self.client.patch(
            self.detail_url, {"owner_user_id": str(third.id)}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.channel.refresh_from_db()
        self.assertEqual(str(self.channel.owner_id), str(self.user.id))

    def test_editor_can_remove_other_members(self):
        third = self._make_third_member()
        self.viewer_row.role = "admin"
        self.viewer_row.save(update_fields=["role"])
        self.authenticate(self.user2)
        res = self.client.delete(self._member_url(third))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

    def test_viewer_can_still_remove_themselves(self):
        """Leaving is not a management action."""
        self.authenticate(self.user2)
        res = self.client.delete(self._member_url(self.user2))
        self.assertEqual(res.status_code, status.HTTP_204_NO_CONTENT)

    def _make_third_member(self):
        from django.contrib.auth import get_user_model

        from origin.models.common.team_models import TeamMembers

        user_model = get_user_model()
        third = user_model.objects.create_user(
            username="gmthird", email="gmthird@test.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=third)
        ChannelMember.objects.create(channel=self.channel, user=third, role="member")
        return third
