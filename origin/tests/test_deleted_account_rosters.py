"""MIP-1546: a deleted account leaves group and project chats and stops being offered anywhere.

Account deletion is a soft delete (the row is anonymised, never removed), so the person's
`ChannelMember` rows used to stay: they kept showing on group, multi-person-DM and project
rosters as "Deleted user", and they were still offered by the mention check and the add-member
lists. The rule now:

* GROUP (GM) and PROJECT (PM) chats: the account is no longer a participant, so its row is
  soft-deleted at deletion. A project chat mirrors the project's membership, so the project
  membership is removed through the ordinary `ProjectMembers` delete (its `post_delete` receiver
  soft-deletes the mirrored row), not by writing the channel row behind the project's back.
* DM and multi-person DM (MDM) chats: the row is KEPT. The other participants keep the
  conversation, and every line the person wrote already reads "Deleted user".
* Wherever a roster OFFERS people (the channel roster, `list_channel_members`, the mention
  check, the add-member lists) a deleted account is hidden even where its row is kept, and a
  row that was never pruned is hidden too.

Nothing here may touch another member's rows or the history other members read.
"""

from __future__ import annotations

from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command

from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.list_channel_members import LIST_CHANNEL_MEMBERS
from origin.services import channel_scope
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.services.deleted_account_memberships import pending_group_memberships
from origin.tests.test_base import BaseAPITestCase
from origin.views.chat.message_views import _split_mentions_by_membership
from origin.views.utils.mention_membership import non_member_mentions

User = get_user_model()


def _mention(user):
    return [{"type": "mention", "props": {"userId": str(user.id)}}]


class DeletedAccountRosterBase(BaseAPITestCase):
    """`self.user2` is the person who deletes their account; `self.user` and `self.carol` stay."""

    def setUp(self):
        super().setUp()
        self.carol = User.objects.create_user(
            username="carol", email="carol@example.com", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.carol)

        self.dm = Channel.objects.create(team=self.team, kind=ChannelKind.DM, owner=self.user)
        lo, hi = sorted([self.user.id, self.user2.id], key=str)
        ChannelDirectPair.objects.create(channel=self.dm, user_lo=lo, user_hi=hi)
        self.mdm = Channel.objects.create(team=self.team, kind=ChannelKind.MDM, owner=self.user)
        self.gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Group", owner=self.user
        )
        self.other_gm = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="Not hers", owner=self.user
        )
        for ch, members in (
            (self.dm, (self.user, self.user2)),
            (self.mdm, (self.user, self.user2, self.carol)),
            (self.gm, (self.user, self.user2, self.carol)),
            (self.other_gm, (self.user, self.carol)),
        ):
            for m in members:
                ChannelMember.objects.create(channel=ch, user=m)

        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Proj",
            code="PRJ",
            owner=self.user,
            project_system_user=self.user,
        )
        for m in (self.user, self.user2, self.carol):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=m)
        self.pm = Channel.objects.get(kind=ChannelKind.PM, project=self.project)

        self.old_dm_message = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=1, body=[], body_text="written before"
        )
        self.old_gm_message = Message.objects.create(
            channel=self.gm, sender=self.user2, seq=1, body=[], body_text="written before"
        )

    def row(self, channel, user):
        return ChannelMember.objects.get(channel=channel, user=user)

    def live_ids(self, channel):
        return {
            str(u)
            for u in channel_scope.chat_member_rows(channel).values_list("user_id", flat=True)
        }


class AccountDeletionEndsGroupMembershipTests(DeletedAccountRosterBase):
    def test_gm_and_pm_rows_are_soft_deleted_and_dm_mdm_rows_are_kept(self):
        delete_account(self.user2)

        self.assertTrue(self.row(self.gm, self.user2).is_deleted)
        self.assertTrue(self.row(self.pm, self.user2).is_deleted)
        self.assertFalse(self.row(self.dm, self.user2).is_deleted)
        self.assertFalse(self.row(self.mdm, self.user2).is_deleted)

    def test_the_project_membership_goes_through_the_project_row(self):
        delete_account(self.user2)
        self.assertFalse(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )
        # And the chat row is the mirror's, so a later save of the project cannot bring it back.
        self.project.project_name = "Renamed"
        self.project.save()
        self.assertTrue(self.row(self.pm, self.user2).is_deleted)

    def test_nobody_elses_rows_change(self):
        before = {
            (c.id, m.id): m.is_deleted
            for c in (self.dm, self.mdm, self.gm, self.other_gm, self.pm)
            for m in ChannelMember.objects.filter(channel=c).exclude(user=self.user2)
        }
        delete_account(self.user2)
        after = {
            (c.id, m.id): m.is_deleted
            for c in (self.dm, self.mdm, self.gm, self.other_gm, self.pm)
            for m in ChannelMember.objects.filter(channel=c).exclude(user=self.user2)
        }
        self.assertEqual(before, after)
        # A group the person was never in is untouched, and its roster is whole.
        self.assertEqual(self.live_ids(self.other_gm), {str(self.user.id), str(self.carol.id)})
        # The other project members stay members.
        self.assertEqual(
            set(
                ProjectMembers.objects.filter(project=self.project).values_list(
                    "attendee_id", flat=True
                )
            ),
            {self.user.id, self.carol.id},
        )

    def test_a_failed_deletion_leaves_the_rows(self):
        # Ownership blocks the deletion: nothing may have been dropped on the way.
        from origin.services.account_deletion import OwnershipTransferRequired

        with self.assertRaises(OwnershipTransferRequired):
            delete_account(self.user)
        self.assertFalse(self.row(self.gm, self.user).is_deleted)
        self.assertTrue(ProjectMembers.objects.filter(attendee=self.user).exists())

    def test_old_messages_read_the_same_for_the_other_members(self):
        self.authenticate(self.user)
        before = self.client.get(f"/api/v3/channels/{self.dm.id}/messages/").json()
        delete_account(self.user2)
        after = self.client.get(f"/api/v3/channels/{self.dm.id}/messages/").json()
        self.assertEqual(len(after["data"]["messages"]), len(before["data"]["messages"]))
        sent = [m for m in after["data"]["messages"] if m["bodyText"] == "written before"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["sender"]["userName"], DELETED_USERNAME)
        # The group's history too, even though she is no longer in it.
        gm_after = self.client.get(f"/api/v3/channels/{self.gm.id}/messages/").json()
        self.assertEqual([m["bodyText"] for m in gm_after["data"]["messages"]], ["written before"])

    def test_the_dm_still_lists_for_the_other_person_as_deleted_user(self):
        self.authenticate(self.user)
        delete_account(self.user2)
        res = self.client.get("/api/v3/channels/", {"team_id": str(self.team.team_id)})
        self.assertEqual(res.status_code, 200, res.content)
        rows = {c["id"]: c for c in res.json()["channels"]}
        self.assertIn(str(self.dm.id), rows)
        self.assertEqual(rows[str(self.dm.id)]["dmPartner"]["userName"], DELETED_USERNAME)

    def test_a_person_with_no_group_chats_deletes_normally(self):
        ChannelMember.objects.filter(user=self.carol).delete()
        ProjectMembers.objects.filter(attendee=self.carol).delete()
        delete_account(self.carol)
        self.carol.refresh_from_db()
        self.assertTrue(self.carol.is_deleted)


class RostersDoNotOfferTheDeletedAccountTests(DeletedAccountRosterBase):
    def setUp(self):
        super().setUp()
        delete_account(self.user2)
        self.authenticate(self.user)

    def members(self, channel):
        res = self.client.get(f"/api/v3/channels/{channel.id}/members/")
        self.assertEqual(res.status_code, 200, res.content)
        return {m["userId"] for m in res.json()["members"]}

    def test_the_roster_endpoint_hides_her_in_every_kind(self):
        gone = str(self.user2.id)
        for channel in (self.gm, self.mdm, self.pm, self.dm):
            self.assertNotIn(gone, self.members(channel), channel.kind)
        self.assertEqual(self.members(self.gm), {str(self.user.id), str(self.carol.id)})
        self.assertEqual(self.members(self.mdm), {str(self.user.id), str(self.carol.id)})

    def test_the_channel_detail_roster_hides_her(self):
        for channel in (self.gm, self.mdm):
            res = self.client.get(f"/api/v3/channels/{channel.id}/")
            self.assertEqual(res.status_code, 200, res.content)
            self.assertNotIn(str(self.user2.id), {m["userId"] for m in res.json()["members"]})

    def test_list_channel_members_does_not_offer_her(self):
        ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        for channel in (self.gm, self.mdm, self.pm):
            out = LIST_CHANNEL_MEMBERS.run({"channel_id": str(channel.id)}, ctx)
            self.assertNotIn(str(self.user2.id), {m["user_id"] for m in out["members"]})
            self.assertNotIn(DELETED_USERNAME, repr(out))

    def test_a_mention_of_her_is_not_a_member_mention_and_is_not_offered_for_adding(self):
        valid, dropped = _split_mentions_by_membership(self.mdm, _mention(self.user2))
        self.assertEqual(valid, set())
        # No "add this person to the chat?" prompt for a person who no longer exists.
        self.assertIsNone(non_member_mentions([self.user2.id], channel_id=self.mdm.id))
        self.assertIsNone(non_member_mentions([self.user2.id], channel_id=self.gm.id))
        self.assertIsNone(non_member_mentions([self.user2.id], project_id=self.project.project_id))

    def test_a_live_mention_still_resolves(self):
        valid, _ = _split_mentions_by_membership(self.mdm, _mention(self.carol))
        self.assertEqual(valid, {str(self.carol.id)})

    def test_she_cannot_be_added_back(self):
        # The roster check is by the account, so even with a team row that outlived the deletion.
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=False)
        res = self.client.post(
            f"/api/v3/channels/{self.gm.id}/members/",
            {"user_ids": [str(self.user2.id)]},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)
        self.assertTrue(self.row(self.gm, self.user2).is_deleted)

    def test_she_cannot_be_put_in_a_new_chat(self):
        TeamMembers.objects.filter(attendee=self.user2).update(is_deleted=False)
        res = self.client.post(
            "/api/v3/channels/",
            {
                "kind": 2,
                "title": "New",
                "team_id": str(self.team.team_id),
                "member_user_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)
        res = self.client.post(
            "/api/v3/channels/",
            {"kind": 1, "team_id": str(self.team.team_id), "other_user_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 404, res.content)

    def test_a_row_that_was_never_pruned_is_hidden_anyway(self):
        # An account deleted before this change: its group row is still live.
        ChannelMember.objects.filter(channel=self.gm, user=self.user2).update(is_deleted=False)
        self.assertNotIn(str(self.user2.id), self.live_ids(self.gm))
        self.assertNotIn(str(self.user2.id), self.members(self.gm))

    def test_live_members_are_still_listed(self):
        # The filter hides one account; it must never hide a live one.
        self.assertEqual(self.live_ids(self.gm), {str(self.user.id), str(self.carol.id)})
        self.assertEqual(self.live_ids(self.pm), {str(self.user.id), str(self.carol.id)})


class PruneCommandTests(DeletedAccountRosterBase):
    def setUp(self):
        super().setUp()
        # An account deleted BEFORE this change: anonymised, rows left behind.
        User.objects.filter(pk=self.user2.pk).update(
            is_deleted=True, is_active=False, username=DELETED_USERNAME
        )

    def run_command(self, *args):
        out = StringIO()
        call_command("prune_deleted_account_memberships", *args, stdout=out)
        return out.getvalue()

    def test_pending_counts_only_group_and_project_rows_of_deleted_accounts(self):
        counts = pending_group_memberships()
        self.assertEqual(counts.accounts, 1)
        self.assertEqual(counts.group_rows, 1)
        self.assertEqual(counts.project_memberships, 1)

    def test_dry_run_is_the_default_and_writes_nothing(self):
        text = self.run_command()
        self.assertIn("DRY RUN", text)
        self.assertIn("would", text)
        self.assertFalse(self.row(self.gm, self.user2).is_deleted)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )

    def test_apply_ends_group_and_project_rows_of_deleted_accounts_only(self):
        text = self.run_command("--apply")
        self.assertIn("APPLY", text)
        self.assertTrue(self.row(self.gm, self.user2).is_deleted)
        self.assertTrue(self.row(self.pm, self.user2).is_deleted)
        # DM and MDM stay.
        self.assertFalse(self.row(self.dm, self.user2).is_deleted)
        self.assertFalse(self.row(self.mdm, self.user2).is_deleted)
        # Live accounts: not a single row moved.
        for ch in (self.dm, self.mdm, self.gm, self.other_gm, self.pm):
            for m in ChannelMember.objects.filter(channel=ch).exclude(user=self.user2):
                self.assertFalse(m.is_deleted, (ch.kind, m.user_id))
        self.assertEqual(
            set(
                ProjectMembers.objects.filter(project=self.project).values_list(
                    "attendee_id", flat=True
                )
            ),
            {self.user.id, self.carol.id},
        )

    def test_it_is_idempotent(self):
        self.run_command("--apply")
        text = self.run_command("--apply")
        self.assertIn("0 group", text)
        self.assertEqual(pending_group_memberships().group_rows, 0)

    def test_the_output_is_counts_only(self):
        for args in ((), ("--apply",)):
            text = self.run_command(*args)
            for secret in (
                "carol",
                "testuser",
                "Group",
                "Proj",
                "example.com",
                str(self.gm.id),
                str(self.user2.id),
            ):
                self.assertNotIn(secret, text)

    def test_a_live_account_in_the_same_chats_is_never_selected(self):
        ChannelMember.objects.filter(user=self.user2).delete()
        ProjectMembers.objects.filter(attendee=self.user2).delete()
        self.assertEqual(pending_group_memberships().group_rows, 0)
        self.assertEqual(pending_group_memberships().project_memberships, 0)
        self.run_command("--apply")
        self.assertFalse(self.row(self.gm, self.user).is_deleted)
        self.assertFalse(self.row(self.gm, self.carol).is_deleted)
