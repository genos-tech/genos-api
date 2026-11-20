"""MIP-1708: people who join a team are put into its default chat and their department's chat.

What these pin, with teams made by the real create view (`team_setup`), never by hand:

* the three real join paths (an accepted invite, a direct join, an approved inbox request) put the
  person into the team's `general`, as a plain `member`; a bulk or fixture member creation does
  not (nothing is wired as a signal), and neither does a join that was no join (the team switch the
  app calls on every visit);
* a person who LEFT the chat is not put back, whether they stay in the team or leave it and
  rejoin (a team departure soft-deletes every channel row, so it cannot be told from leaving the
  chat; both stay out);
* a department chat takes the members of its mention group when they join the team and when they
  are added to the group later; removal from the group keeps them in the chat;
* never added: a deleted account, a system user, a guest, someone already in, a deleted or private
  or external chat, a deleted group; a team with no default chat is fine; and the sample
  department's chat is not marked;
* a failure while adding never fails the join (and is logged with ids); the live notice goes to
  the people added only, after the commit, and not when the sockets relay already announced it;
* the `adopt_general_chats` command for teams made before this existed.
"""

import hashlib
import json
import secrets
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import transaction
from django.test import override_settings
from django.utils import timezone
from rest_framework import status

from origin.models.chat.auto_join_models import ChannelAutoJoin
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember, Message
from origin.models.common.inbox_models import InboxItems
from origin.models.common.invite_models import TeamInvite
from origin.models.common.mention_group_models import MentionGroupMaster, MentionGroupMembers
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import realtime_push
from origin.services.member_roles import GUEST
from origin.services.realtime_push import CHANNEL_APPEARED_PATH
from origin.services.team_default_chats import (
    add_group_members_to_chat,
    adopt_general_chats,
    join_default_chats,
)
from origin.services.team_membership import add_team_member
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

SOCKETS = "http://sockets.internal:8889"
SECRET = "test-only-sockets-internal-secret"
RELAY = {"HTTP_X_GENOS_RELAY": "sockets"}


class DefaultChatBase(BaseAPITestCase):
    """`self.user` owns a team made through the create view, with one department, Sales."""

    def setUp(self):
        super().setUp()
        # The guide-note seeding of a self-join kicks off a search reindex: not what is tested.
        reindex = mock.patch("origin.services.demo_seeder.kick_off_demo_reindex")
        reindex.start()
        self.addCleanup(reindex.stop)
        self.carol = User.objects.create_user(
            username="carol", email="carol@example.com", password="pw"
        )
        self.dave = User.objects.create_user(
            username="dave", email="dave@example.com", password="pw"
        )
        self.authenticate(self.user)
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            response = self.client.post(
                "/api/v2/team/create/",
                {
                    "team_name": "Acme",
                    "team_email": "acme@example.com",
                    "owner_id": str(self.user.id),
                    "departments": ["Sales"],
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.acme = TeamMaster.objects.get(team_id=response.data["teamDetails"]["teamId"])
        self.general = Channel.objects.get(team=self.acme, title="general")
        self.sales_chat = Channel.objects.get(team=self.acme, title="sales · general")
        self.sales_group = MentionGroupMaster.objects.get(team=self.acme, group_name="sales")

    # ---- helpers ----

    def row(self, channel, user):
        return ChannelMember.objects.filter(channel=channel, user=user).first()

    def in_chat(self, channel, user) -> bool:
        row = self.row(channel, user)
        return row is not None and not row.is_deleted

    def direct_join(self, person, *, as_user=None, **extra):
        self.authenticate(as_user or self.user)
        return self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.acme.team_id), "attendee_id": str(person.id)},
            format="json",
            **extra,
        )

    def invite(self, person, *, member_role="viewer", project=None):
        raw = secrets.token_urlsafe(32)
        TeamInvite.objects.create(
            team=self.acme,
            invited_email=person.email.lower(),
            invited_by=self.user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=7),
            status="pending",
            member_role=member_role,
            project=project,
        )
        return raw

    def accept(self, person, raw, **extra):
        self.authenticate(person)
        return self.client.post(
            "/api/v2/team/invite/accept/", {"token": raw}, format="json", **extra
        )

    def request_to_join(self, person):
        return InboxItems.objects.create(
            team=self.acme, sender=person, receiver=self.user, item_type=1, item_body="hi"
        )

    def approve(self, item, **extra):
        self.authenticate(self.user)
        return self.client.post(
            "/api/v2/team/join/fromInbox/",
            {"team_id": str(self.acme.team_id), "item_id": item.item_id},
            format="json",
            **extra,
        )

    def join_group(self, person, **extra):
        self.authenticate(self.user)
        return self.client.post(
            "/api/v2/mention-group/members/",
            {"group_id": self.sales_group.group_id, "user_ids": [str(person.id)]},
            format="json",
            **extra,
        )

    def leave_chat(self, channel, person):
        self.authenticate(person)
        response = self.client.delete(f"/api/v3/channels/{channel.id}/members/{person.id}/")
        self.assertIn(response.status_code, (200, 204), getattr(response, "data", None))

    def leave_team(self, person):
        self.authenticate(person)
        response = self.client.post(
            "/api/v2/team/leave/",
            {"team_id": str(self.acme.team_id), "attendee_id": str(person.id)},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)


class JoinPathTests(DefaultChatBase):
    def test_team_setup_marks_general_as_the_default_and_sales_as_the_groups(self):
        general_rule = ChannelAutoJoin.objects.get(channel=self.general)
        self.assertIsNone(general_rule.mention_group_id)
        self.assertEqual(general_rule.team_id, self.acme.team_id)
        sales_rule = ChannelAutoJoin.objects.get(channel=self.sales_chat)
        self.assertEqual(sales_rule.mention_group_id, self.sales_group.group_id)
        self.assertEqual(ChannelAutoJoin.objects.filter(team=self.acme).count(), 2)

    def test_an_accepted_invite_puts_the_person_into_general_as_a_member(self):
        response = self.accept(self.carol, self.invite(self.carol))

        self.assertEqual(response.status_code, 200, response.data)
        row = self.row(self.general, self.carol)
        self.assertFalse(row.is_deleted)
        self.assertEqual(row.role, "member")
        self.assertFalse(
            self.in_chat(self.sales_chat, self.carol), "not in the group: not in Sales"
        )

    def test_a_direct_join_puts_the_person_into_general(self):
        response = self.direct_join(self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.row(self.general, self.carol).role, "member")

    def test_a_direct_rejoin_of_someone_with_no_chat_rows_puts_them_into_general(self):
        # A soft-deleted membership brought back in place, who never had a chat row.
        TeamMembers.objects.create(team=self.acme, attendee=self.carol, is_deleted=True)

        response = self.direct_join(self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(self.in_chat(self.general, self.carol))

    def test_an_approved_join_request_puts_the_person_into_general(self):
        response = self.approve(self.request_to_join(self.carol))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(self.row(self.general, self.carol).role, "member")

    def test_an_approved_request_from_a_returning_member_goes_through_the_same_door(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol, is_deleted=True)

        response = self.approve(self.request_to_join(self.carol))

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(self.in_chat(self.general, self.carol))

    def test_the_owner_is_in_general_once_and_a_second_join_changes_nothing(self):
        response = self.direct_join(self.user, as_user=self.user)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ChannelMember.objects.filter(channel=self.general).count(), 1)
        self.assertEqual(self.row(self.general, self.user).role, "owner")

    def test_a_bulk_or_fixture_member_is_not_added(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        TeamMembers.objects.bulk_create([TeamMembers(team=self.acme, attendee=self.dave)])

        self.assertIsNone(self.row(self.general, self.carol))
        self.assertIsNone(self.row(self.general, self.dave))

    def test_the_team_switch_call_for_a_current_member_adds_nobody_to_anything(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)

        response = self.direct_join(self.carol, as_user=self.carol)

        self.assertEqual(response.status_code, 201)
        self.assertIsNone(self.row(self.general, self.carol), "no join happened: nothing added")

    def test_the_service_door_returns_whether_the_person_joined(self):
        self.assertTrue(add_team_member(self.acme.team_id, self.carol.id))
        self.assertFalse(add_team_member(self.acme.team_id, self.carol.id))
        self.assertTrue(self.in_chat(self.general, self.carol))


class LeftTests(DefaultChatBase):
    def test_a_person_who_left_general_is_not_put_back_by_a_later_join_call(self):
        self.direct_join(self.carol)
        self.leave_chat(self.general, self.carol)
        self.assertTrue(self.row(self.general, self.carol).is_deleted)

        # Still in the team; the app's switch call, and the service door, change nothing.
        self.direct_join(self.carol, as_user=self.carol)
        join_default_chats(self.acme.team_id, self.carol.id)

        self.assertTrue(self.row(self.general, self.carol).is_deleted)

    def test_leaving_the_chat_then_the_team_then_rejoining_does_not_put_them_back(self):
        self.direct_join(self.carol)
        self.leave_chat(self.general, self.carol)
        self.leave_team(self.carol)

        response = self.direct_join(self.carol)

        self.assertEqual(response.status_code, 201)
        self.assertTrue(
            TeamMembers.objects.get(team=self.acme, attendee=self.carol).is_deleted is False
        )
        self.assertTrue(self.row(self.general, self.carol).is_deleted)

    def test_someone_who_left_the_TEAM_is_not_put_back_into_general_either(self):
        # Leaving the team soft-deletes every channel row of the team, which cannot be told
        # from leaving the chat: both stay out (documented decision; they find it by name).
        self.direct_join(self.carol)
        self.assertTrue(self.in_chat(self.general, self.carol))
        self.leave_team(self.carol)
        self.assertTrue(self.row(self.general, self.carol).is_deleted)

        response = self.approve(self.request_to_join(self.carol))

        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.row(self.general, self.carol).is_deleted)

    def test_a_person_who_left_the_department_chat_is_not_put_back_by_the_group(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        self.join_group(self.carol)
        self.assertTrue(self.in_chat(self.sales_chat, self.carol))
        self.leave_chat(self.sales_chat, self.carol)

        MentionGroupMembers.objects.filter(group=self.sales_group, user=self.carol).delete()
        self.join_group(self.carol)

        self.assertTrue(self.row(self.sales_chat, self.carol).is_deleted)


class DepartmentTests(DefaultChatBase):
    def test_a_member_of_the_group_joins_the_department_chat_when_they_join_the_team(self):
        # A group row can outlive a team membership (the person left; the row stays).
        MentionGroupMembers.objects.create(
            team=self.acme, group=self.sales_group, user=self.carol, added_by=self.user
        )

        self.accept(self.carol, self.invite(self.carol))

        self.assertTrue(self.in_chat(self.general, self.carol))
        self.assertEqual(self.row(self.sales_chat, self.carol).role, "member")
        self.assertEqual(self.row(self.sales_chat, self.carol).is_deleted, False)

    def test_a_person_who_is_in_no_group_gets_general_only(self):
        self.direct_join(self.carol)

        self.assertTrue(self.in_chat(self.general, self.carol))
        self.assertIsNone(self.row(self.sales_chat, self.carol))

    def test_adding_a_member_to_the_group_later_puts_them_into_the_chat(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)

        response = self.join_group(self.carol)

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["memberCount"], 2, "the response is unchanged")
        self.assertTrue(self.in_chat(self.sales_chat, self.carol))
        self.assertIsNone(self.row(self.general, self.carol), "the group is not the team default")

    def test_removing_them_from_the_group_keeps_them_in_the_chat(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        self.join_group(self.carol)
        self.authenticate(self.user)

        response = self.client.delete(
            "/api/v2/mention-group/members/",
            {"group_id": self.sales_group.group_id, "user_id": str(self.carol.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(
            MentionGroupMembers.objects.filter(group=self.sales_group, user=self.carol).exists()
        )
        self.assertTrue(self.in_chat(self.sales_chat, self.carol))

    def test_a_group_with_no_chat_adds_nobody_anywhere(self):
        plain = MentionGroupMaster.objects.create(team=self.acme, group_name="plain")
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        self.authenticate(self.user)

        response = self.client.post(
            "/api/v2/mention-group/members/",
            {"group_id": plain.group_id, "user_ids": [str(self.carol.id)]},
            format="json",
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(ChannelMember.objects.filter(user=self.carol).count(), 0)

    def test_a_deleted_group_adds_nobody(self):
        MentionGroupMembers.objects.create(
            team=self.acme, group=self.sales_group, user=self.carol, added_by=self.user
        )
        self.sales_group.is_deleted = True
        self.sales_group.save()

        self.direct_join(self.carol)

        self.assertTrue(self.in_chat(self.general, self.carol))
        self.assertIsNone(self.row(self.sales_chat, self.carol))

    def test_the_sample_departments_chat_is_not_marked(self):
        # The sample department is removable sample data whose group holds only the owner.
        self.authenticate(self.user)
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            response = self.client.post(
                "/api/v2/team/create/",
                {
                    "team_name": "Sampled",
                    "team_email": "sampled@example.com",
                    "owner_id": str(self.user.id),
                    "with_starter": True,
                },
                format="json",
            )
        team = TeamMaster.objects.get(team_id=response.data["teamDetails"]["teamId"])

        rules = ChannelAutoJoin.objects.filter(team=team)
        self.assertEqual([r.channel.title for r in rules], ["general"])
        self.assertTrue(Channel.objects.filter(team=team, title__contains="sample").exists())


class NeverAddedTests(DefaultChatBase):
    def test_a_deleted_account_is_not_added(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        self.carol.is_deleted = True
        self.carol.save()

        self.assertEqual(join_default_chats(self.acme.team_id, self.carol.id), [])
        self.assertEqual(add_group_members_to_chat(self.sales_group, [self.carol.id]), {})
        self.assertEqual(ChannelMember.objects.filter(user=self.carol).count(), 0)

    def test_a_system_user_is_not_added(self):
        bot = User.objects.create_user(username="bot", email="bot@example.com", password="pw")
        bot.is_system_user = True
        bot.save()

        response = self.direct_join(bot)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(TeamMembers.objects.filter(team=self.acme, attendee=bot).exists())
        self.assertEqual(ChannelMember.objects.filter(user=bot).count(), 0)

    def test_a_guest_invite_adds_a_project_row_and_no_chat(self):
        project = ProjectMaster.objects.create(
            team=self.acme, project_name="Client", owner=self.user
        )
        ProjectMembers.objects.create(team=self.acme, project=project, attendee=self.user)

        response = self.accept(
            self.carol, self.invite(self.carol, member_role=GUEST, project=project)
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(TeamMembers.objects.filter(team=self.acme, attendee=self.carol).exists())
        self.assertFalse(
            ChannelMember.objects.filter(
                channel__in=[self.general, self.sales_chat], user=self.carol
            ).exists()
        )

    def test_a_member_row_with_the_guest_role_is_not_added(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol, member_role=GUEST)

        self.assertEqual(join_default_chats(self.acme.team_id, self.carol.id), [])

    def test_someone_who_left_the_team_is_not_added_by_a_group_add_or_a_join_call(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol, is_deleted=True)

        self.assertEqual(add_group_members_to_chat(self.sales_group, [self.carol.id]), {})
        self.assertEqual(join_default_chats(self.acme.team_id, self.carol.id), [])
        self.assertEqual(ChannelMember.objects.filter(user=self.carol).count(), 0)

    def test_a_deleted_group_is_refused_by_the_service_itself(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        self.sales_group.is_deleted = True
        self.sales_group.save()

        self.assertEqual(add_group_members_to_chat(self.sales_group, [self.carol.id]), {})
        self.assertIsNone(self.row(self.sales_chat, self.carol))

    def test_someone_who_is_not_a_member_of_the_team_is_not_added(self):
        self.assertEqual(join_default_chats(self.acme.team_id, self.carol.id), [])
        self.assertEqual(add_group_members_to_chat(self.sales_group, [self.carol.id]), {})

    def test_someone_already_in_the_chat_is_untouched_and_not_announced(self):
        ChannelMember.objects.create(channel=self.general, user=self.carol, role="admin")

        with mock.patch("origin.services.team_default_chats.push_channel_appeared") as push:
            self.direct_join(self.carol)

        self.assertEqual(self.row(self.general, self.carol).role, "admin")
        push.assert_not_called()

    def test_a_deleted_chat_is_skipped(self):
        Channel.objects.filter(pk=self.general.pk).update(is_deleted=True)

        self.assertEqual(self.direct_join(self.carol).status_code, 201)
        self.assertIsNone(self.row(self.general, self.carol))

    def test_a_private_chat_is_skipped(self):
        Channel.objects.filter(pk=self.general.pk).update(is_private=True)

        self.assertEqual(self.direct_join(self.carol).status_code, 201)
        self.assertIsNone(self.row(self.general, self.carol))

    def test_an_external_chat_is_skipped(self):
        Channel.objects.filter(pk=self.general.pk).update(is_external=True)

        self.assertEqual(self.direct_join(self.carol).status_code, 201)
        self.assertIsNone(self.row(self.general, self.carol))

    def test_a_chat_that_is_no_longer_a_group_chat_is_skipped(self):
        Channel.objects.filter(pk=self.general.pk).update(kind=ChannelKind.MDM)

        self.assertEqual(self.direct_join(self.carol).status_code, 201)
        self.assertIsNone(self.row(self.general, self.carol))

    def test_a_deleted_team_is_skipped(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        TeamMaster.objects.filter(pk=self.acme.pk).update(is_deleted=True)

        self.assertEqual(join_default_chats(self.acme.team_id, self.carol.id), [])

    def test_a_team_with_no_default_chat_joins_without_error(self):
        # `self.team` was made by hand by the base class: no setup, no default chat.
        self.authenticate(self.user)
        response = self.client.post(
            "/api/v2/team/join/",
            {"team_id": str(self.team.team_id), "attendee_id": str(self.carol.id)},
            format="json",
        )

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(ChannelMember.objects.filter(user=self.carol).count(), 0)

    def test_a_team_whose_default_chat_was_hard_deleted_joins_without_error(self):
        Message.objects.filter(channel=self.general).delete()
        self.general.delete()

        self.assertEqual(self.direct_join(self.carol).status_code, 201)
        self.assertFalse(
            ChannelAutoJoin.objects.filter(team=self.acme, mention_group=None).exists()
        )

    def test_a_second_default_chat_for_a_team_is_refused_by_the_database(self):
        other = Channel.objects.create(team=self.acme, kind=ChannelKind.GM, title="other")
        with self.assertRaises(Exception), transaction.atomic():
            ChannelAutoJoin.objects.create(team=self.acme, channel=other, mention_group=None)


class FailureTests(DefaultChatBase):
    def test_a_failure_adding_to_the_chat_does_not_fail_the_join_and_is_logged_with_ids(self):
        with mock.patch(
            "origin.services.team_default_chats.ChannelMember.objects.get_or_create",
            side_effect=RuntimeError("boom"),
        ):
            with self.assertLogs("origin.services.team_default_chats", level="ERROR") as logs:
                response = self.direct_join(self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(
            TeamMembers.objects.filter(
                team=self.acme, attendee=self.carol, is_deleted=False
            ).exists()
        )
        message = logs.output[0]
        self.assertIn(str(self.general.pk), message)
        self.assertIn(str(self.carol.id), message)

    def test_a_failure_reading_the_rules_does_not_fail_an_invite(self):
        with mock.patch(
            "origin.services.team_default_chats._open_rules", side_effect=RuntimeError("boom")
        ):
            with self.assertLogs("origin.services.team_default_chats", level="ERROR"):
                response = self.accept(self.carol, self.invite(self.carol))

        self.assertEqual(response.status_code, 200)
        self.assertTrue(TeamMembers.objects.filter(team=self.acme, attendee=self.carol).exists())

    def test_a_failure_for_one_person_costs_only_that_person(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)
        TeamMembers.objects.create(team=self.acme, attendee=self.dave)
        real = ChannelMember.objects.get_or_create

        def flaky(*args, **kwargs):
            if str(kwargs.get("user_id")) == str(self.carol.id):
                raise RuntimeError("boom")
            return real(*args, **kwargs)

        with mock.patch(
            "origin.services.team_default_chats.ChannelMember.objects.get_or_create",
            side_effect=flaky,
        ):
            with self.assertLogs("origin.services.team_default_chats", level="ERROR"):
                result = add_group_members_to_chat(self.sales_group, [self.carol.id, self.dave.id])

        self.assertEqual(result, {str(self.sales_chat.pk): [str(self.dave.id)]})
        self.assertIsNone(self.row(self.sales_chat, self.carol))

    def test_the_membership_and_the_chat_row_are_one_transaction(self):
        # The membership is written in the same transaction as the chat rows: a rollback after the
        # join takes both.
        with self.assertRaises(RuntimeError), transaction.atomic():
            add_team_member(self.acme.team_id, self.carol.id)
            raise RuntimeError("later step failed")

        self.assertFalse(TeamMembers.objects.filter(team=self.acme, attendee=self.carol).exists())
        self.assertIsNone(self.row(self.general, self.carol))


@override_settings(SOCKETS_INTERNAL_URL=SOCKETS, SOCKETS_INTERNAL_SECRET=SECRET)
class LiveNoticeTests(DefaultChatBase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(realtime_push.requests, "post")
        self.post = patcher.start()
        self.post.return_value = mock.Mock(status_code=200)
        self.addCleanup(patcher.stop)

    def sent(self):
        """The chat-appeared notices only (other pushes, the inbox's, ride the same mock)."""
        return [
            json.loads(c.kwargs["data"])
            for c in self.post.call_args_list
            if c.args[0].endswith(CHANNEL_APPEARED_PATH)
        ]

    def test_only_the_person_added_is_told_after_the_commit(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.direct_join(self.carol)

        self.assertEqual(
            self.sent(),
            [
                {
                    "userId": str(self.carol.id),
                    "teamId": str(self.acme.team_id),
                    "channelId": str(self.general.pk),
                    "kind": int(ChannelKind.GM),
                }
            ],
            "one notice, to carol, not the owner who is already in the chat",
        )

    def test_an_invite_and_an_approval_announce_too(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.accept(self.carol, self.invite(self.carol))
            self.approve(self.request_to_join(self.dave))

        self.assertEqual(
            sorted(p["userId"] for p in self.sent()),
            sorted([str(self.carol.id), str(self.dave.id)]),
        )

    def test_a_group_add_tells_only_the_added_person(self):
        TeamMembers.objects.create(team=self.acme, attendee=self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            self.join_group(self.carol)

        self.assertEqual(
            [(p["userId"], p["channelId"]) for p in self.sent()],
            [(str(self.carol.id), str(self.sales_chat.pk))],
        )

    def test_a_joiner_in_a_group_is_told_once_per_chat(self):
        MentionGroupMembers.objects.create(
            team=self.acme, group=self.sales_group, user=self.carol, added_by=self.user
        )

        with self.captureOnCommitCallbacks(execute=True):
            self.direct_join(self.carol)

        self.assertEqual(
            sorted(p["channelId"] for p in self.sent()),
            sorted([str(self.general.pk), str(self.sales_chat.pk)]),
        )

    def test_a_rolled_back_join_announces_nothing(self):
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    add_team_member(self.acme.team_id, self.carol.id)
                    raise RuntimeError("later step failed")
            except RuntimeError:
                pass

        self.assertEqual(callbacks, [])
        self.assertEqual(self.sent(), [])

    def test_nothing_is_announced_when_the_sockets_relay_already_announced(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.direct_join(self.carol, **RELAY)
            self.accept(self.dave, self.invite(self.dave), **RELAY)

        self.assertEqual(self.sent(), [])
        self.assertTrue(self.in_chat(self.general, self.carol), "still added, just not announced")
        self.assertTrue(self.in_chat(self.general, self.dave))

    def test_nobody_is_announced_when_nobody_was_added(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.direct_join(self.carol)
        self.post.reset_mock()

        with self.captureOnCommitCallbacks(execute=True):
            self.direct_join(self.carol, as_user=self.carol)

        self.assertEqual(self.sent(), [])


class AdoptCommandTests(DefaultChatBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("adopt_general_chats", *args, stdout=out)
        return out.getvalue()

    def make_old_team(self, name="Old"):
        """A team the way it was made before this change: a `general` chat, no rule."""
        team = TeamMaster.objects.create(
            team_name=name, team_email=f"{name}@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=team, attendee=self.user)
        TeamMembers.objects.create(team=team, attendee=self.carol)
        chat = Channel.objects.create(
            team=team, kind=ChannelKind.GM, title="general", owner=self.user
        )
        ChannelMember.objects.create(channel=chat, user=self.user, role="owner")
        return team, chat

    def test_a_dry_run_writes_nothing(self):
        team, chat = self.make_old_team()

        output = self.run_command("--team", str(team.team_id))

        self.assertIn("1 team(s) would be marked", output)
        self.assertFalse(ChannelAutoJoin.objects.filter(team=team).exists())

    def test_apply_marks_the_chat_and_later_joiners_are_added_but_current_members_are_not(self):
        team, chat = self.make_old_team()

        self.run_command("--apply", "--yes", "--team", str(team.team_id))

        self.assertEqual(ChannelAutoJoin.objects.get(team=team).channel_id, chat.pk)
        self.assertIsNone(self.row(chat, self.carol))
        add_team_member(team.team_id, self.dave.id)
        self.assertTrue(self.in_chat(chat, self.dave))

    def test_add_members_puts_every_current_member_in_but_skips_who_left(self):
        team, chat = self.make_old_team()
        TeamMembers.objects.create(team=team, attendee=self.dave)
        ChannelMember.objects.create(channel=chat, user=self.dave, role="member", is_deleted=True)

        self.run_command("--apply", "--yes", "--add-members", "--team", str(team.team_id))

        self.assertTrue(self.in_chat(chat, self.carol))
        self.assertTrue(self.row(chat, self.dave).is_deleted)

    def test_apply_needs_yes(self):
        with self.assertRaises(CommandError):
            self.run_command("--apply")

    def test_it_is_safe_to_run_twice_and_skips_teams_that_already_have_a_default(self):
        team, _ = self.make_old_team()
        self.run_command("--apply", "--yes", "--team", str(team.team_id))

        output = self.run_command("--apply", "--yes", "--team", str(team.team_id))

        self.assertIn("0 team(s) marked", output)
        self.assertIn("1 team(s) already have a default chat", output)
        self.assertEqual(ChannelAutoJoin.objects.filter(team=team).count(), 1)

    def test_two_open_general_chats_are_reported_and_left_alone(self):
        team, _ = self.make_old_team()
        Channel.objects.create(team=team, kind=ChannelKind.GM, title="general", owner=self.user)

        report = adopt_general_chats(apply=True, team_id=team.team_id)

        self.assertEqual(report.ambiguous, [(str(team.team_id), 2)])
        self.assertFalse(ChannelAutoJoin.objects.filter(team=team).exists())

    def test_a_private_or_deleted_general_is_not_a_candidate(self):
        team, chat = self.make_old_team()
        Channel.objects.filter(pk=chat.pk).update(is_private=True)

        report = adopt_general_chats(apply=True, team_id=team.team_id)

        self.assertEqual((report.marked, report.without_chat), ([], 1))

    def test_a_migration_does_not_mark_or_add_anyone_in_an_existing_team(self):
        # The state after deploy, before anyone runs the command: nothing marked.
        team, chat = self.make_old_team()

        add_team_member(team.team_id, self.dave.id)

        self.assertFalse(ChannelAutoJoin.objects.filter(team=team).exists())
        self.assertIsNone(self.row(chat, self.dave))
