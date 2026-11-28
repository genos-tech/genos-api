"""MIP-2549: a multi-person DM holds at most 10 people; only its creator removes others;
anyone may leave, and a leaving creator hands it on. Group chats have no limit."""

import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.mdm_members import MDM_LIMIT_CODE, MDM_LIMIT_MESSAGE, MDM_MAX_MEMBERS

User = get_user_model()


class _Base(APITestCase):
    def setUp(self):
        self.owner = self._user("owner")
        self.team = TeamMaster.objects.create(team_name="mdm-team", owner=self.owner)
        TeamMembers.objects.create(team=self.team, attendee=self.owner)
        self.others = [self._teammate(f"m{i}") for i in range(14)]
        self.client.force_authenticate(self.owner)

    def _user(self, name):
        return User.objects.create_user(
            username=f"{name}-{uuid.uuid4().hex[:6]}",
            email=f"{name}-{uuid.uuid4().hex[:8]}@example.test",
            password="x-pass-12345",
        )

    def _teammate(self, name):
        user = self._user(name)
        TeamMembers.objects.create(team=self.team, attendee=user)
        return user

    def _create(self, others, kind=ChannelKind.MDM, **extra):
        return self.client.post(
            "/api/v3/channels/",
            {
                "kind": kind,
                "team_id": str(self.team.team_id),
                "member_user_ids": [str(u.id) for u in others],
                **extra,
            },
            format="json",
        )

    def _channel(self, others):
        res = self._create(others)
        self.assertEqual(res.status_code, 201, res.data)
        return Channel.objects.get(id=res.data["channel"]["id"])

    def _add(self, channel, users):
        return self.client.post(
            f"/api/v3/channels/{channel.id}/members/",
            {"user_ids": [str(u.id) for u in users]},
            format="json",
        )

    def _remove(self, channel, user):
        return self.client.delete(f"/api/v3/channels/{channel.id}/members/{user.id}/")

    def _live(self, channel):
        return ChannelMember.objects.filter(channel=channel, is_deleted=False).count()


class MdmLimitTests(_Base):
    def test_create_with_ten_people_is_allowed(self):
        channel = self._channel(self.others[: MDM_MAX_MEMBERS - 1])
        self.assertEqual(self._live(channel), 10)

    def test_create_with_eleven_people_is_refused_and_writes_nothing(self):
        before = Channel.objects.count()
        res = self._create(self.others[:10])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], MDM_LIMIT_MESSAGE)
        self.assertEqual(res.data["code"], MDM_LIMIT_CODE)
        self.assertEqual(Channel.objects.count(), before)

    def test_the_tenth_may_be_added_and_the_eleventh_not(self):
        channel = self._channel(self.others[:8])
        self.assertEqual(self._add(channel, [self.others[8]]).status_code, 201)
        res = self._add(channel, [self.others[9]])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], MDM_LIMIT_MESSAGE)
        self.assertEqual(self._live(channel), 10)

    def test_a_list_that_would_pass_the_limit_adds_nobody(self):
        channel = self._channel(self.others[:7])
        res = self._add(channel, self.others[7:10])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._live(channel), 8)

    def test_re_adding_a_member_of_a_full_chat_is_not_refused(self):
        channel = self._channel(self.others[:9])
        self.assertEqual(self._add(channel, [self.others[0]]).status_code, 201)

    def test_a_removed_member_frees_a_seat_and_may_come_back(self):
        channel = self._channel(self.others[:9])
        self.assertEqual(self._remove(channel, self.others[0]).status_code, 204)
        self.assertEqual(self._add(channel, [self.others[10]]).status_code, 201)
        self.assertEqual(self._add(channel, [self.others[0]]).status_code, 400)

    def test_a_deleted_account_takes_no_seat(self):
        channel = self._channel(self.others[:9])
        User.objects.filter(id=self.others[0].id).update(is_deleted=True)
        self.assertEqual(self._add(channel, [self.others[10]]).status_code, 201)

    def test_a_chat_already_above_the_limit_keeps_everyone_and_takes_nobody(self):
        channel = self._channel(self.others[:9])
        for user in self.others[9:11]:
            ChannelMember.objects.create(channel=channel, user=user, role="member")
        self.assertEqual(self._add(channel, [self.others[11]]).status_code, 400)
        self.assertEqual(self._live(channel), 12)
        self.assertEqual(self._remove(channel, self.others[0]).status_code, 204)

    def test_a_group_chat_has_no_limit(self):
        res = self._create(self.others, kind=ChannelKind.GM, title="big group")
        self.assertEqual(res.status_code, 201)
        extra = self._teammate("extra")
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        self.assertEqual(self._add(channel, [extra]).status_code, 201)
        self.assertEqual(self._live(channel), 16)


class MdmPermissionTests(_Base):
    def setUp(self):
        super().setUp()
        self.a, self.b, self.c = self.others[:3]
        self.channel = self._channel([self.a, self.b])
        # People added together join in no promised order: say who is the longest-standing.
        ChannelMember.objects.filter(channel=self.channel, user=self.a).update(
            ts_joined_at=self.channel.ts_created_at - timedelta(days=1)
        )

    def test_any_member_may_add(self):
        self.client.force_authenticate(self.a)
        self.assertEqual(self._add(self.channel, [self.c]).status_code, 201)

    def test_somebody_outside_the_chat_gets_404(self):
        self.client.force_authenticate(self.c)
        self.assertEqual(self._add(self.channel, [self.others[3]]).status_code, 404)
        self.assertEqual(self._remove(self.channel, self.a).status_code, 404)

    def test_only_the_creator_removes_somebody_else(self):
        self.client.force_authenticate(self.a)
        self.assertEqual(self._remove(self.channel, self.b).status_code, 403)
        self.client.force_authenticate(self.owner)
        self.assertEqual(self._remove(self.channel, self.b).status_code, 204)

    def test_roles_cannot_be_set(self):
        res = self.client.patch(
            f"/api/v3/channels/{self.channel.id}/members/{self.a.id}/",
            {"member_role": "editor"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_a_member_may_leave(self):
        self.client.force_authenticate(self.a)
        self.assertEqual(self._remove(self.channel, self.a).status_code, 204)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.owner_id, self.owner.id)

    def test_the_creator_may_leave_and_the_longest_standing_member_takes_over(self):
        with mock.patch("origin.views.chat.channel_views.push_channel_appeared") as push:
            self.assertEqual(self._remove(self.channel, self.owner).status_code, 204)
        self.channel.refresh_from_db()
        self.assertEqual(self.channel.owner_id, self.a.id)
        self.assertEqual(
            {str(u) for u in push.call_args.kwargs["user_ids"]}, {str(self.a.id), str(self.b.id)}
        )
        # The new creator may remove; nobody else may.
        self.client.force_authenticate(self.b)
        self.assertEqual(self._remove(self.channel, self.a).status_code, 403)
        self.client.force_authenticate(self.a)
        self.assertEqual(self._remove(self.channel, self.b).status_code, 204)

    def test_the_last_person_may_leave(self):
        self.assertEqual(self._remove(self.channel, self.a).status_code, 204)
        self.assertEqual(self._remove(self.channel, self.b).status_code, 204)
        self.assertEqual(self._remove(self.channel, self.owner).status_code, 204)
        self.channel.refresh_from_db()
        self.assertIsNone(self.channel.owner_id)
        self.assertEqual(self._live(self.channel), 0)

    def test_when_the_creators_account_is_gone_any_member_may_remove(self):
        User.objects.filter(id=self.owner.id).update(is_deleted=True)
        self.client.force_authenticate(self.a)
        self.assertEqual(self._remove(self.channel, self.b).status_code, 204)

    def test_a_group_owner_still_cannot_leave(self):
        res = self._create([self.a], kind=ChannelKind.GM, title="g")
        channel = Channel.objects.get(id=res.data["channel"]["id"])
        self.assertEqual(self._remove(channel, self.owner).status_code, 400)
