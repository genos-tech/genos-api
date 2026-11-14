"""A group / project chat owned by a deleted account gets a live owner (MIP-1550).

Rules pinned here (see `services/deleted_account_channel_owners.py`): a group goes to its
longest-standing live member of the chat's own team; none left means ownerless, never a deleted
chat; a project chat follows its project, which goes to the team owner only when they are already
on it; DMs are untouched; nobody who was not already in the chat gains a seat; and another
person's chat is never touched.
"""

from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.utils import timezone

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import deleted_account_channel_owners as service
from origin.services.account_deletion import delete_account
from origin.tests.cross_team_fixtures import CrossTeamTestCase


class _Fixtures:
    def group(self, owner, members, *, team=None, title="Crew", external=False):
        """A group owned by `owner`; `members` join in the order given (oldest first)."""
        channel = Channel.objects.create(
            team=team or self.team_a,
            kind=ChannelKind.GM,
            title=title,
            owner=owner,
            is_external=external,
        )
        base = timezone.now() - timedelta(days=30)
        for i, user in enumerate(members):
            row = ChannelMember.objects.create(
                channel=channel, user=user, role="owner" if user == owner else "member"
            )
            ChannelMember.objects.filter(pk=row.pk).update(ts_joined_at=base + timedelta(days=i))
        return channel

    def owner_of(self, channel):
        return Channel.objects.get(pk=channel.pk).owner_id

    def role_of(self, channel, user):
        return ChannelMember.objects.get(channel=channel, user=user).role

    def project(self, owner, members, name="Chat project"):
        project = ProjectMaster.objects.create(
            team=self.team_a, project_name=name, owner=owner, project_system_user=owner
        )
        for user in members:
            ProjectMembers.objects.get_or_create(
                team=self.team_a, project=project, attendee=user, defaults={"member_role": "editor"}
            )
        return project


class GroupOwnerFollowsDeletionTests(_Fixtures, CrossTeamTestCase):
    """`self.a_editor` owns the groups and is the one deleted."""

    def setUp(self):
        super().setUp()
        self.leaver = self.a_editor

    def test_the_longest_standing_remaining_member_becomes_owner(self):
        gm = self.group(self.leaver, [self.leaver, self.a_viewer, self.a_owner])
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(gm), self.a_viewer.id)
        self.assertEqual(self.role_of(gm, self.a_viewer), "owner")
        self.assertEqual(self.role_of(gm, self.a_owner), "member")

    def test_the_order_is_by_when_they_joined_not_by_row_order(self):
        gm = self.group(self.leaver, [self.leaver, self.a_viewer, self.a_owner])
        base = timezone.now() - timedelta(days=90)
        ChannelMember.objects.filter(channel=gm, user=self.a_owner).update(ts_joined_at=base)
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(gm), self.a_owner.id)

    def test_the_chat_is_not_deleted_and_keeps_its_members_and_title(self):
        gm = self.group(self.leaver, [self.leaver, self.a_viewer, self.a_owner], title="Keepers")
        delete_account(self.leaver)
        gm.refresh_from_db()
        self.assertFalse(gm.is_deleted)
        self.assertEqual(gm.title, "Keepers")
        live = ChannelMember.objects.filter(channel=gm, is_deleted=False, user__is_deleted=False)
        self.assertEqual({m.user_id for m in live}, {self.a_viewer.id, self.a_owner.id})

    def test_a_member_who_left_the_chat_is_not_picked(self):
        gm = self.group(self.leaver, [self.leaver, self.a_viewer, self.a_owner])
        ChannelMember.objects.filter(channel=gm, user=self.a_viewer).update(is_deleted=True)
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(gm), self.a_owner.id)
        # The one who left gains nothing.
        self.assertTrue(ChannelMember.objects.get(channel=gm, user=self.a_viewer).is_deleted)

    def test_a_member_who_is_not_on_the_team_any_more_is_not_picked(self):
        gm = self.group(self.leaver, [self.leaver, self.a_viewer, self.a_owner])
        TeamMembers.objects.filter(team=self.team_a, attendee=self.a_viewer).update(is_deleted=True)
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(gm), self.a_owner.id)

    def test_a_guest_team_member_is_never_handed_the_hosts_chat(self):
        """The earliest member is a guest of another team: the host side keeps the chat."""
        gm = self.group(self.leaver, [self.b_viewer, self.leaver, self.a_viewer], external=True)
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(gm), self.a_viewer.id)

    def test_nobody_left_on_the_host_side_leaves_it_ownerless_not_deleted(self):
        gm = self.group(self.leaver, [self.leaver, self.b_viewer], external=True)
        delete_account(self.leaver)
        gm.refresh_from_db()
        self.assertIsNone(gm.owner_id)
        self.assertFalse(gm.is_deleted)
        # Nobody gained a seat and nobody was removed by this.
        self.assertFalse(ChannelMember.objects.get(channel=gm, user=self.b_viewer).is_deleted)

    def test_a_chat_with_only_the_owner_is_left_ownerless(self):
        gm = self.group(self.leaver, [self.leaver])
        delete_account(self.leaver)
        gm.refresh_from_db()
        self.assertIsNone(gm.owner_id)
        self.assertFalse(gm.is_deleted)

    def test_another_persons_chat_is_untouched(self):
        mine = self.group(self.leaver, [self.leaver, self.a_viewer])
        theirs = self.group(
            self.a_viewer, [self.a_viewer, self.a_owner, self.leaver], title="Theirs"
        )
        delete_account(self.leaver)
        self.assertEqual(self.owner_of(theirs), self.a_viewer.id)
        self.assertEqual(self.role_of(theirs, self.a_viewer), "owner")
        self.assertNotEqual(self.owner_of(mine), self.leaver.id)

    def test_a_direct_message_is_untouched(self):
        dm = Channel.objects.create(
            team=self.team_a, kind=ChannelKind.DM, title="", owner=self.leaver
        )
        for user in (self.leaver, self.a_viewer):
            ChannelMember.objects.create(channel=dm, user=user)
        delete_account(self.leaver)
        dm.refresh_from_db()
        self.assertFalse(dm.is_deleted)
        self.assertEqual(dm.owner_id, self.leaver.id)
        self.assertEqual(ChannelMember.objects.filter(channel=dm, is_deleted=False).count(), 2)


class ProjectChatOwnerFollowsDeletionTests(_Fixtures, CrossTeamTestCase):
    def setUp(self):
        super().setUp()
        self.leaver = self.a_editor

    def _chat(self, project):
        return Channel.objects.get(project=project, kind=ChannelKind.PM)

    def test_the_team_owner_already_on_the_project_takes_it_and_its_chat(self):
        project = self.project(self.leaver, [self.leaver, self.a_owner, self.a_viewer])
        self.assertEqual(self._chat(project).owner_id, self.leaver.id)
        delete_account(self.leaver)
        project.refresh_from_db()
        self.assertEqual(project.owner_id, self.a_owner.id)
        self.assertEqual(self._chat(project).owner_id, self.a_owner.id)
        self.assertFalse(self._chat(project).is_deleted)

    def test_a_team_owner_who_is_not_on_the_project_is_not_put_on_it(self):
        project = self.project(self.leaver, [self.leaver, self.a_viewer])
        delete_account(self.leaver)
        project.refresh_from_db()
        self.assertEqual(project.owner_id, self.leaver.id)  # unchanged: a product decision
        self.assertFalse(
            ProjectMembers.objects.filter(project=project, attendee=self.a_owner).exists()
        )
        self.assertFalse(self._chat(project).is_deleted)

    def test_a_project_someone_else_owns_is_untouched(self):
        project = self.project(self.a_viewer, [self.a_viewer, self.leaver, self.a_owner])
        delete_account(self.leaver)
        project.refresh_from_db()
        self.assertEqual(project.owner_id, self.a_viewer.id)
        self.assertEqual(self._chat(project).owner_id, self.a_viewer.id)


class PruneDanglingOwnersTests(_Fixtures, CrossTeamTestCase):
    """Accounts deleted BEFORE the rule existed: their chats still name them as owner."""

    def setUp(self):
        super().setUp()
        self.dead = self.a_editor
        self.gm = self.group(
            self.dead, [self.dead, self.a_viewer, self.a_owner], title="Secret title"
        )
        self.lonely = self.group(self.dead, [self.dead], title="Lonely")
        self.live = self.group(self.a_viewer, [self.a_viewer, self.a_owner], title="Live")
        # Deleted the old way: the flag only, nothing cascaded.
        CustomUser.objects.filter(pk=self.dead.pk).update(is_deleted=True)

    def test_a_dry_run_counts_and_writes_nothing(self):
        result = service.prune(apply=False)
        self.assertEqual((result.accounts, result.chats), (1, 2))
        self.assertEqual(self.owner_of(self.gm), self.dead.id)
        self.assertEqual(self.owner_of(self.lonely), self.dead.id)

    def test_apply_hands_over_and_leaves_ownerless_where_nobody_is_left(self):
        result = service.prune(apply=True)
        self.assertEqual((result.transferred, result.ownerless, result.left), (1, 1, 0))
        self.assertEqual(self.owner_of(self.gm), self.a_viewer.id)
        self.assertIsNone(self.owner_of(self.lonely))
        self.assertFalse(Channel.objects.get(pk=self.lonely.pk).is_deleted)

    def test_a_live_owners_chat_is_untouched(self):
        service.prune(apply=True)
        self.assertEqual(self.owner_of(self.live), self.a_viewer.id)

    def test_it_is_idempotent(self):
        service.prune(apply=True)
        again = service.prune(apply=True)
        self.assertEqual(again.chats, 0)
        self.assertEqual(self.owner_of(self.gm), self.a_viewer.id)

    def test_the_command_defaults_to_a_dry_run_and_prints_counts_only(self):
        out = StringIO()
        call_command("prune_deleted_account_channel_owners", stdout=out)
        text = out.getvalue()
        self.assertIn("DRY RUN", text)
        self.assertIn("2 group / project chat(s)", text)
        self.assertNotIn("Secret title", text)
        self.assertEqual(self.owner_of(self.gm), self.dead.id)

        out = StringIO()
        call_command("prune_deleted_account_channel_owners", "--apply", stdout=out)
        self.assertIn("APPLY", out.getvalue())
        self.assertNotIn("Secret title", out.getvalue())
        self.assertEqual(self.owner_of(self.gm), self.a_viewer.id)
