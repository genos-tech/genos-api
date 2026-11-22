"""MIP-1732: characterisation pins for the three ways a person becomes a team member.

`TeamMembersView.post` (`POST /team/join/`), `JoinTeamFromInboxView` (`POST /team/join/fromInbox/`)
and `accept_invite` (`POST /team/invite/accept/`) each wrote the `TeamMembers` row their own way
before the refactor that gives them ONE function. These tests describe what each one does today,
at the edge a caller can see, so the refactor cannot change it unnoticed:

* the status code and the exact response body of a NEW row, of a REACTIVATED row (a small body
  that echoes the request, a different shape from a new row's full serializer body) and of an
  already-live member (nothing written, still a 201 / 200);
* the row itself: same id on reactivation, the role and the join times kept, `ts_updated_at`
  moved, and the exact `post_save` the row's receivers (billing seats, the roster cache, the
  change feed) see: `created=True` with no `update_fields` for a new row, `created=False` with
  `{is_deleted, ts_updated_at}` for a reactivation, none at all for a live member;
* the security events: `member_added` with its `via` for the two views, none for an invite (it
  records `invite_accepted`), none for a team switch;
* the default-chat join: put in once on a join and a reactivation, never on a live member, told
  to the person's apps unless the sockets relay already did;
* the guards: who may join, a refused request writes nothing, a 400 for a person who does not
  exist, the 409 of an answered request (and that a LIVE member skips that check), an invite that
  cannot be consumed leaves everything alone.
"""

import hashlib
import secrets
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db.models.signals import post_save
from django.utils import timezone

from origin.models.chat.auto_join_models import ChannelAutoJoin
from origin.models.chat.unified_models import Channel, ChannelKind, ChannelMember
from origin.models.common.inbox_models import InboxItems
from origin.models.common.invite_models import TeamInvite
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services.member_roles import EDITOR, GUEST
from origin.services.team_membership import add_team_member
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

TEAM_JOIN = "/api/v2/team/join/"
TEAM_INBOX = "/api/v2/team/join/fromInbox/"
INVITE_ACCEPT = "/api/v2/team/invite/accept/"
RELAY = {"HTTP_X_GENOS_RELAY": "sockets"}

# What `TeamMembersSerializer` (fields = "__all__") returns for a new row.
ROW_KEYS = {
    "id",
    "team",
    "attendee",
    "is_deleted",
    "member_role",
    "guide_seeded_at",
    "ts_joined_at",
    "ts_created_at",
    "ts_updated_at",
}
REACTIVATION_FIELDS = frozenset({"is_deleted", "ts_updated_at"})


class JoinPinBase(BaseAPITestCase):
    """`self.user` owns `self.team` (with `self.user2` as a plain member), which has an open
    default chat. `self.carol` and `self.dave` are outsiders."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        # A self-join seeds guide notes and kicks off a search reindex: not what is pinned here.
        reindex = mock.patch("origin.services.demo_seeder.kick_off_demo_reindex")
        reindex.start()
        self.addCleanup(reindex.stop)
        self.carol = User.objects.create_user(
            username="carol", email="carol@example.com", password="pw"
        )
        self.dave = User.objects.create_user(
            username="dave", email="dave@example.com", password="pw"
        )
        self.general = Channel.objects.create(team=self.team, kind=ChannelKind.GM, title="general")
        ChannelAutoJoin.objects.create(team=self.team, channel=self.general, mention_group=None)

        # What the row's receivers see: one entry per save of a TeamMembers row.
        self.saves = []

        def record(sender, instance, created, update_fields, **kwargs):
            self.saves.append(
                {
                    "attendee": str(instance.attendee_id),
                    "created": created,
                    "update_fields": None if update_fields is None else frozenset(update_fields),
                }
            )

        post_save.connect(record, sender=TeamMembers, dispatch_uid="mip1732-pin", weak=False)
        self.addCleanup(post_save.disconnect, sender=TeamMembers, dispatch_uid="mip1732-pin")

        patcher = mock.patch("origin.services.stripe_billing.sync_team_subscription_quantity")
        self.seat_sync = patcher.start()
        self.addCleanup(patcher.stop)
        # The seat sync now runs on commit (MIP-1840); these pins are about WHICH
        # writes fire it, so run the callback at once (TestCase never commits).
        patcher = mock.patch(
            "origin.signals.billing_signals.transaction.on_commit", side_effect=lambda fn: fn()
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("origin.services.change_feed.bump_team")
        self.bump_team = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("origin.services.team_default_chats.push_channel_appeared")
        self.push = patcher.start()
        self.addCleanup(patcher.stop)
        self.seat_sync.reset_mock()
        self.bump_team.reset_mock()

    # ---- helpers ----

    def member_row(self, person):
        return TeamMembers.objects.filter(team=self.team, attendee=person).first()

    def in_general(self, person) -> bool:
        return ChannelMember.objects.filter(
            channel=self.general, user=person, is_deleted=False
        ).exists()

    def events(self, action):
        return list(SecurityEvent.objects.filter(action=action).order_by("event_id"))

    def soft_delete(self, person, *, role=None):
        """A member who left: the row is kept, soft-deleted, with an old `ts_updated_at`."""
        row = TeamMembers.objects.create(team=self.team, attendee=person)
        TeamMembers.objects.filter(pk=row.pk).update(
            is_deleted=True,
            ts_updated_at=timezone.now() - timedelta(days=30),
            **({"member_role": role} if role else {}),
        )
        self.saves.clear()
        self.seat_sync.reset_mock()
        self.bump_team.reset_mock()
        return TeamMembers.objects.get(pk=row.pk)

    def make_live(self, person):
        row = TeamMembers.objects.create(team=self.team, attendee=person)
        self.saves.clear()
        self.seat_sync.reset_mock()
        self.bump_team.reset_mock()
        return row

    def assert_signals_of_a_new_row(self, person):
        self.assertEqual(
            self.saves, [{"attendee": str(person.id), "created": True, "update_fields": None}]
        )
        self.seat_sync.assert_called_once_with(self.team.team_id)
        self.bump_team.assert_called_with(self.team.team_id)

    def assert_signals_of_a_reactivation(self, person):
        self.assertEqual(
            self.saves,
            [{"attendee": str(person.id), "created": False, "update_fields": REACTIVATION_FIELDS}],
        )
        self.seat_sync.assert_called_once_with(self.team.team_id)
        self.bump_team.assert_called_with(self.team.team_id)

    def assert_no_signals(self):
        self.assertEqual(self.saves, [])
        self.seat_sync.assert_not_called()
        self.bump_team.assert_not_called()

    def assert_reactivated_in_place(self, before, person):
        after = self.member_row(person)
        self.assertEqual(after.pk, before.pk, "the same row, brought back")
        self.assertFalse(after.is_deleted)
        self.assertEqual(after.member_role, before.member_role, "the role is kept")
        self.assertEqual(after.ts_joined_at, before.ts_joined_at)
        self.assertEqual(after.ts_created_at, before.ts_created_at)
        self.assertGreater(after.ts_updated_at, before.ts_updated_at, "ts_updated_at moves")
        self.assertEqual(TeamMembers.objects.filter(team=self.team, attendee=person).count(), 1)

    def assert_told_once(self, person):
        """The default-chat join announced the chat to `person`, and to nobody else."""
        self.push.assert_called_once()
        kwargs = self.push.call_args.kwargs
        # The views hand the id over as the string they were sent, the invite as the user's UUID:
        # what is pinned is who, not which type.
        self.assertEqual([str(u) for u in kwargs["user_ids"]], [str(person.id)])
        self.assertEqual(kwargs["team_id"], self.team.team_id)
        self.assertEqual(kwargs["channel_id"], self.general.pk)
        self.assertEqual(kwargs["kind"], ChannelKind.GM)


# ──────────────────────────────────────────────────────────────────────────
# POST /team/join/  (TeamMembersView)
# ──────────────────────────────────────────────────────────────────────────


class DirectJoinPins(JoinPinBase):
    def join(self, actor, person, *, team=None, **extra):
        self.authenticate(actor)
        return self.client.post(
            TEAM_JOIN,
            {"team_id": str((team or self.team).team_id), "attendee_id": str(person.id)},
            format="json",
            **extra,
        )

    def test_a_new_member_gets_a_201_with_the_full_row(self):
        with self.captureOnCommitCallbacks(execute=True):
            response = self.join(self.user, self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        row = self.member_row(self.carol)
        self.assertEqual(set(response.data), ROW_KEYS)
        self.assertEqual(response.data["id"], row.pk)
        self.assertEqual(str(response.data["team"]), str(self.team.team_id))
        self.assertEqual(str(response.data["attendee"]), str(self.carol.id))
        self.assertIs(response.data["is_deleted"], False)
        self.assertEqual(response.data["member_role"], "viewer")
        self.assertIsNone(response.data["guide_seeded_at"])
        self.assertFalse(row.is_deleted)
        self.assertEqual(row.member_role, "viewer")
        self.assert_signals_of_a_new_row(self.carol)

    def test_a_new_member_is_a_member_added_event_via_direct(self):
        self.join(self.user, self.carol)

        (event,) = self.events(SecurityAction.MEMBER_ADDED)
        self.assertEqual(event.actor_id, self.user.id)
        self.assertEqual(event.target_type, "user")
        self.assertEqual(event.target_id, str(self.carol.id))
        self.assertEqual(event.metadata, {"via": "direct"})
        self.assertEqual(str(event.team_id), str(self.team.team_id))

    def test_a_new_member_is_put_into_the_default_chat_and_told(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.join(self.user, self.carol)

        self.assertTrue(self.in_general(self.carol))
        self.assertEqual(
            ChannelMember.objects.get(channel=self.general, user=self.carol).role, "member"
        )
        self.assert_told_once(self.carol)

    def test_the_sockets_relay_silences_the_notice_but_not_the_join(self):
        self.join(self.user, self.carol, **RELAY)

        self.assertTrue(self.in_general(self.carol))
        self.push.assert_not_called()

    def test_a_reactivated_member_gets_a_201_that_echoes_the_request(self):
        before = self.soft_delete(self.carol, role=EDITOR)

        response = self.join(self.user, self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data, {"team": str(self.team.team_id), "attendee": str(self.carol.id)}
        )
        self.assert_reactivated_in_place(before, self.carol)
        self.assertEqual(self.member_row(self.carol).member_role, EDITOR)
        self.assert_signals_of_a_reactivation(self.carol)

    def test_a_reactivated_member_is_an_event_via_direct_and_goes_into_the_chat(self):
        self.soft_delete(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            self.join(self.user, self.carol)

        (event,) = self.events(SecurityAction.MEMBER_ADDED)
        self.assertEqual(event.metadata, {"via": "direct"})
        self.assertEqual(event.actor_id, self.user.id)
        self.assertTrue(self.in_general(self.carol))
        self.assert_told_once(self.carol)

    def test_rejoining_by_yourself_is_via_self(self):
        self.soft_delete(self.carol)

        response = self.join(self.carol, self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertFalse(self.member_row(self.carol).is_deleted)
        (event,) = self.events(SecurityAction.MEMBER_ADDED)
        self.assertEqual(event.metadata, {"via": "self"})
        self.assertEqual(event.actor_id, self.carol.id)

    def test_the_owner_without_a_row_joins_themselves_via_self(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user).delete()
        self.saves.clear()

        response = self.join(self.user, self.user)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data), ROW_KEYS)
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED)[0].metadata, {"via": "self"})
        # The first save is the new row; a self-join then stamps the guide notes onto it.
        self.assertEqual(
            self.saves[0], {"attendee": str(self.user.id), "created": True, "update_fields": None}
        )
        self.assertEqual(
            self.saves[1:],
            [
                {
                    "attendee": str(self.user.id),
                    "created": False,
                    "update_fields": frozenset({"guide_seeded_at", "ts_updated_at"}),
                }
            ],
        )

    def test_a_live_member_gets_a_201_and_nothing_is_written(self):
        before = self.make_live(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.join(self.carol, self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data, {"team": str(self.team.team_id), "attendee": str(self.carol.id)}
        )
        # The membership is left alone: the only save is the guide-note stamp of a self-join.
        self.assertEqual(
            self.saves,
            [
                {
                    "attendee": str(self.carol.id),
                    "created": False,
                    "update_fields": frozenset({"guide_seeded_at", "ts_updated_at"}),
                }
            ],
        )
        self.assertFalse(self.member_row(self.carol).is_deleted)
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        self.assertFalse(self.in_general(self.carol), "no join, no chat")
        self.push.assert_not_called()

    def test_adding_a_live_member_for_someone_else_is_the_same_no_op(self):
        self.make_live(self.carol)

        response = self.join(self.user, self.carol)

        self.assertEqual(response.status_code, 201, response.data)
        self.assert_no_signals()
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])

    def test_a_person_who_does_not_exist_is_a_400_and_nothing_is_written(self):
        ghost = mock.Mock(id="00000000-0000-4000-8000-000000000001")

        response = self.join(self.user, ghost)

        self.assertEqual(response.status_code, 400, response.data)
        self.assertIn("attendee", response.data)
        self.assertFalse(TeamMembers.objects.filter(attendee_id=ghost.id).exists())
        self.assert_no_signals()
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        self.push.assert_not_called()

    def test_a_refused_join_writes_nothing(self):
        stranger_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.dave
        )
        TeamMembers.objects.create(team=stranger_team, attendee=self.dave)
        self.saves.clear()
        self.seat_sync.reset_mock()
        self.bump_team.reset_mock()

        # A stranger joining a team they were never invited to.
        response = self.join(self.carol, self.carol)
        self.assertEqual(response.status_code, 403, response.data)
        # An ordinary member adding a person.
        response = self.join(self.user2, self.carol)
        self.assertEqual(response.status_code, 403, response.data)
        # Somebody who is not in the team at all adding a person.
        response = self.join(self.dave, self.carol)
        self.assertEqual(response.status_code, 404, response.data)

        self.assertFalse(TeamMembers.objects.filter(team=self.team, attendee=self.carol).exists())
        self.assert_no_signals()
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        self.push.assert_not_called()

    def test_a_refused_rejoin_of_a_departed_member_by_an_ordinary_member_writes_nothing(self):
        before = self.soft_delete(self.carol)

        response = self.join(self.user2, self.carol)

        self.assertEqual(response.status_code, 403, response.data)
        self.assertTrue(self.member_row(self.carol).is_deleted)
        self.assertEqual(self.member_row(self.carol).ts_updated_at, before.ts_updated_at)
        self.assert_no_signals()

    def test_a_system_user_is_added_without_an_event_or_a_chat(self):
        bot = User.objects.create_user(
            username="bot", email="bot@example.com", password="pw", is_system_user=True
        )

        response = self.join(self.user2, bot)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(set(response.data), ROW_KEYS)
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        self.assertFalse(self.in_general(bot))
        self.assertEqual(
            self.saves, [{"attendee": str(bot.id), "created": True, "update_fields": None}]
        )

    def test_missing_ids_are_a_400(self):
        self.authenticate(self.user)
        response = self.client.post(TEAM_JOIN, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assert_no_signals()


# ──────────────────────────────────────────────────────────────────────────
# POST /team/join/fromInbox/  (JoinTeamFromInboxView)
# ──────────────────────────────────────────────────────────────────────────


class InboxJoinPins(JoinPinBase):
    def request_to_join(self, person, *, receiver=None, **extra):
        return InboxItems.objects.create(
            team=self.team,
            sender=person,
            receiver=receiver or self.user,
            item_type=1,
            item_body="hi",
            **extra,
        )

    def approve(self, item, *, as_user=None, **extra):
        self.authenticate(as_user or self.user)
        return self.client.post(
            TEAM_INBOX,
            {"team_id": str(self.team.team_id), "item_id": item.item_id},
            format="json",
            **extra,
        )

    def test_an_approved_request_gets_a_201_with_the_full_row_and_the_team_name(self):
        item = self.request_to_join(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(item)

        self.assertEqual(response.status_code, 201, response.data)
        row = self.member_row(self.carol)
        self.assertEqual(set(response.data), ROW_KEYS | {"teamName"})
        self.assertEqual(response.data["teamName"], "Test Team")
        self.assertEqual(response.data["id"], row.pk)
        self.assertEqual(str(response.data["team"]), str(self.team.team_id))
        self.assertEqual(str(response.data["attendee"]), str(self.carol.id))
        self.assertEqual(response.data["member_role"], "viewer")
        self.assert_signals_of_a_new_row(self.carol)

    def test_an_approved_request_is_an_event_via_join_request_and_goes_into_the_chat(self):
        item = self.request_to_join(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            self.approve(item)

        (event,) = self.events(SecurityAction.MEMBER_ADDED)
        self.assertEqual(event.actor_id, self.user.id, "the approver is the actor")
        self.assertEqual(event.target_id, str(self.carol.id))
        self.assertEqual(event.metadata, {"via": "join_request"})
        self.assertTrue(self.in_general(self.carol))
        self.assert_told_once(self.carol)

    def test_the_sockets_relay_silences_the_notice_but_not_the_join(self):
        self.approve(self.request_to_join(self.carol), **RELAY)

        self.assertTrue(self.in_general(self.carol))
        self.push.assert_not_called()

    def test_a_returning_member_is_reactivated_and_gets_the_small_body(self):
        before = self.soft_delete(self.carol, role=EDITOR)
        item = self.request_to_join(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.approve(item)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data,
            {
                "team": str(self.team.team_id),
                "attendee": str(self.carol.id),
                "teamName": "Test Team",
            },
        )
        self.assert_reactivated_in_place(before, self.carol)
        self.assertEqual(self.member_row(self.carol).member_role, EDITOR)
        self.assert_signals_of_a_reactivation(self.carol)
        (event,) = self.events(SecurityAction.MEMBER_ADDED)
        self.assertEqual(event.metadata, {"via": "join_request"})
        self.assertTrue(self.in_general(self.carol))
        self.assert_told_once(self.carol)

    def test_a_live_member_gets_a_201_and_nothing_is_written(self):
        before = self.make_live(self.carol)
        item = self.request_to_join(self.carol)

        response = self.approve(item)

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            response.data,
            {
                "team": str(self.team.team_id),
                "attendee": str(self.carol.id),
                "teamName": "Test Team",
            },
        )
        self.assertEqual(self.member_row(self.carol).ts_updated_at, before.ts_updated_at)
        self.assert_no_signals()
        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        self.assertFalse(self.in_general(self.carol))
        self.push.assert_not_called()

    def test_a_live_member_is_answered_even_when_the_request_was_already_answered(self):
        # The "already a member" answer comes BEFORE the still-open check.
        self.make_live(self.carol)
        item = self.request_to_join(self.carol, request_status="accepted")

        response = self.approve(item)

        self.assertEqual(response.status_code, 201, response.data)
        self.assert_no_signals()

    def test_an_answered_request_is_a_409_and_writes_nothing(self):
        item = self.request_to_join(self.carol, request_status="accepted")

        response = self.approve(item)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertEqual(response.data, {"error": "This request was already answered."})
        self.assertIsNone(self.member_row(self.carol))
        self.assert_no_signals()

    def test_an_answered_request_does_not_bring_back_a_departed_member(self):
        before = self.soft_delete(self.carol)
        item = self.request_to_join(self.carol, request_status="declined")

        response = self.approve(item)

        self.assertEqual(response.status_code, 409, response.data)
        self.assertTrue(self.member_row(self.carol).is_deleted)
        self.assertEqual(self.member_row(self.carol).ts_updated_at, before.ts_updated_at)
        self.assert_no_signals()

    def test_only_somebody_who_may_manage_the_team_can_approve(self):
        item = self.request_to_join(self.carol, receiver=self.user2)

        response = self.approve(item, as_user=self.user2)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.member_row(self.carol))
        self.assert_no_signals()

    def test_a_request_not_addressed_to_the_caller_is_a_404(self):
        item = self.request_to_join(self.carol)

        response = self.approve(item, as_user=self.user2)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.member_row(self.carol))
        self.assert_no_signals()

    def test_a_deleted_team_is_a_404(self):
        item = self.request_to_join(self.carol)
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)

        response = self.approve(item)

        self.assertEqual(response.status_code, 404, response.data)
        self.assertIsNone(self.member_row(self.carol))
        self.assert_no_signals()

    def test_a_missing_item_id_is_a_400(self):
        self.authenticate(self.user)
        response = self.client.post(TEAM_INBOX, {"team_id": str(self.team.team_id)}, format="json")
        self.assertEqual(response.status_code, 400)


# ──────────────────────────────────────────────────────────────────────────
# POST /team/invite/accept/  (accept_invite -> add_team_member)
# ──────────────────────────────────────────────────────────────────────────


class InviteAcceptPins(JoinPinBase):
    def invite(self, person, *, member_role="viewer", project=None, **fields):
        raw = secrets.token_urlsafe(32)
        row = TeamInvite.objects.create(
            team=self.team,
            invited_email=person.email.lower(),
            invited_by=self.user,
            token_hash=hashlib.sha256(raw.encode()).hexdigest(),
            expires_at=timezone.now() + timedelta(days=7),
            status="pending",
            member_role=member_role,
            project=project,
            **fields,
        )
        return raw, row

    def accept(self, person, raw, **extra):
        self.authenticate(person)
        return self.client.post(INVITE_ACCEPT, {"token": raw}, format="json", **extra)

    def assert_consumed(self, invite, person):
        invite.refresh_from_db()
        self.assertEqual(invite.status, "accepted")
        self.assertEqual(invite.accepted_by_id, person.id)
        self.assertIsNotNone(invite.ts_accepted_at)

    def assert_untouched(self, invite):
        invite.refresh_from_db()
        self.assertEqual(invite.status, "pending")
        self.assertIsNone(invite.accepted_by_id)

    def test_a_new_member_gets_a_200_with_the_team_and_the_invite_is_consumed(self):
        raw, invite = self.invite(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.accept(self.carol, raw)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data,
            {
                "team_id": str(self.team.team_id),
                "team_name": "Test Team",
                "is_guest": False,
                "project_id": None,
            },
        )
        row = self.member_row(self.carol)
        self.assertFalse(row.is_deleted)
        self.assertEqual(row.member_role, "viewer")
        self.assert_signals_of_a_new_row(self.carol)
        self.assert_consumed(invite, self.carol)

    def test_an_accepted_invite_is_an_invite_accepted_event_and_no_member_added_event(self):
        raw, invite = self.invite(self.carol)

        self.accept(self.carol, raw)

        self.assertEqual(self.events(SecurityAction.MEMBER_ADDED), [])
        (event,) = self.events(SecurityAction.INVITE_ACCEPTED)
        self.assertEqual(event.actor_id, self.carol.id)
        self.assertEqual(event.target_id, str(invite.pk))
        self.assertEqual(event.metadata["role"], "viewer")
        self.assertEqual(event.metadata["invited_by"], str(self.user.id))
        self.assertIsNone(event.metadata["project_id"])

    def test_a_new_member_is_put_into_the_default_chat_and_told(self):
        raw, _ = self.invite(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            self.accept(self.carol, raw)

        self.assertTrue(self.in_general(self.carol))
        self.assert_told_once(self.carol)

    def test_the_sockets_relay_silences_the_notice_but_not_the_join(self):
        raw, _ = self.invite(self.carol)

        self.accept(self.carol, raw, **RELAY)

        self.assertTrue(self.in_general(self.carol))
        self.push.assert_not_called()

    def test_a_returning_member_is_reactivated_in_place(self):
        before = self.soft_delete(self.carol, role=EDITOR)
        raw, invite = self.invite(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.accept(self.carol, raw)

        self.assertEqual(response.status_code, 200, response.data)
        self.assert_reactivated_in_place(before, self.carol)
        self.assertEqual(self.member_row(self.carol).member_role, EDITOR)
        self.assert_signals_of_a_reactivation(self.carol)
        self.assert_consumed(invite, self.carol)
        self.assertTrue(self.in_general(self.carol))
        self.assert_told_once(self.carol)

    def test_a_live_member_accepting_writes_no_row_but_still_consumes_the_invite(self):
        before = self.make_live(self.carol)
        raw, invite = self.invite(self.carol)

        with self.captureOnCommitCallbacks(execute=True):
            response = self.accept(self.carol, raw)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["team_id"], str(self.team.team_id))
        self.assertEqual(self.member_row(self.carol).ts_updated_at, before.ts_updated_at)
        self.assert_no_signals()
        self.assertFalse(self.in_general(self.carol), "no join, no chat")
        self.push.assert_not_called()
        self.assert_consumed(invite, self.carol)
        self.assertEqual(len(self.events(SecurityAction.INVITE_ACCEPTED)), 1)

    def test_an_invite_that_cannot_be_consumed_leaves_everything_alone(self):
        # Already accepted.
        raw, invite = self.invite(self.carol)
        TeamInvite.objects.filter(pk=invite.pk).update(status="accepted")
        response = self.accept(self.carol, raw)
        self.assertEqual((response.status_code, response.data), (400, {"detail": "invalid"}))

        # Expired.
        raw, invite = self.invite(self.carol)
        TeamInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now())
        response = self.accept(self.carol, raw)
        self.assertEqual((response.status_code, response.data), (400, {"detail": "expired"}))

        # Another person's address.
        raw, invite = self.invite(self.dave)
        response = self.accept(self.carol, raw)
        self.assertEqual((response.status_code, response.data), (400, {"detail": "email_mismatch"}))
        self.assert_untouched(invite)

        # The team is gone.
        raw, invite = self.invite(self.carol)
        TeamMaster.objects.filter(pk=self.team.pk).update(is_deleted=True)
        response = self.accept(self.carol, raw)
        self.assertEqual(
            (response.status_code, response.data), (400, {"detail": "team_unavailable"})
        )
        self.assert_untouched(invite)

        # An unknown token.
        self.authenticate(self.carol)
        response = self.client.post(INVITE_ACCEPT, {"token": "nope"}, format="json")
        self.assertEqual((response.status_code, response.data), (400, {"detail": "invalid"}))

        self.assertIsNone(self.member_row(self.carol))
        self.assert_no_signals()
        self.assertEqual(self.events(SecurityAction.INVITE_ACCEPTED), [])
        self.push.assert_not_called()

    def test_a_departed_member_is_not_brought_back_by_an_invite_that_cannot_be_consumed(self):
        before = self.soft_delete(self.carol)
        raw, invite = self.invite(self.carol)
        TeamInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now())

        response = self.accept(self.carol, raw)

        self.assertEqual(response.status_code, 400)
        self.assertTrue(self.member_row(self.carol).is_deleted)
        self.assertEqual(self.member_row(self.carol).ts_updated_at, before.ts_updated_at)
        self.assert_no_signals()

    def test_a_guest_invite_writes_a_project_row_and_no_team_row(self):
        project = ProjectMaster.objects.create(
            team=self.team, project_name="Client", owner=self.user
        )
        raw, invite = self.invite(self.carol, member_role=GUEST, project=project)

        response = self.accept(self.carol, raw)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data,
            {
                "team_id": str(self.team.team_id),
                "team_name": "Test Team",
                "is_guest": True,
                "project_id": project.pk,
            },
        )
        self.assertIsNone(self.member_row(self.carol))
        self.assertTrue(
            ProjectMembers.objects.filter(project=project, attendee=self.carol).exists()
        )
        self.assert_no_signals()
        self.assertFalse(self.in_general(self.carol))
        self.assert_consumed(invite, self.carol)

    def test_the_membership_and_the_invite_are_one_transaction(self):
        raw, invite = self.invite(self.carol)

        with mock.patch.object(TeamInvite, "save", side_effect=RuntimeError("flip failed")):
            with self.assertRaises(RuntimeError):
                self.accept(self.carol, raw)

        self.assertIsNone(self.member_row(self.carol), "the membership went with the invite")
        self.assertFalse(self.in_general(self.carol))
        self.assert_untouched(invite)


# ──────────────────────────────────────────────────────────────────────────
# add_team_member  (the service door the invite goes through)
# ──────────────────────────────────────────────────────────────────────────


class ServiceDoorPins(JoinPinBase):
    def test_true_for_a_new_row_true_for_a_reactivation_false_for_a_live_member(self):
        self.assertIs(add_team_member(self.team.team_id, self.carol.id), True)
        self.assert_signals_of_a_new_row(self.carol)

        self.saves.clear()
        self.assertIs(add_team_member(self.team.team_id, self.carol.id), False)
        self.assertEqual(self.saves, [])

        before = self.soft_delete(self.dave, role=EDITOR)
        self.assertIs(add_team_member(self.team.team_id, self.dave.id), True)
        self.assert_reactivated_in_place(before, self.dave)
        self.assertEqual(
            self.saves,
            [
                {
                    "attendee": str(self.dave.id),
                    "created": False,
                    "update_fields": REACTIVATION_FIELDS,
                }
            ],
        )

    def test_the_join_is_in_the_callers_transaction(self):
        from django.db import transaction

        with self.assertRaises(RuntimeError), transaction.atomic():
            add_team_member(self.team.team_id, self.carol.id)
            raise RuntimeError("later step failed")

        self.assertIsNone(self.member_row(self.carol))
        self.assertFalse(self.in_general(self.carol))

    def test_announce_false_joins_without_telling(self):
        add_team_member(self.team.team_id, self.carol.id, announce=False)

        self.assertTrue(self.in_general(self.carol))
        self.push.assert_not_called()
