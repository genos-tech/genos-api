"""What account deletion does to the Activity feed (MIP-1542).

`Activity.recipient` is a CASCADE foreign key to the user, but an account is SOFT-deleted
(`is_deleted=True`, identity overwritten), so the cascade never fires. Rows FOR the member
(`recipient = the account`, every kind) must be deleted with the account; rows ABOUT the member
in other people's feeds (`actor = the account`) stay untouched and show the anonymised name.
`prune_deleted_account_activity` finds and removes rows left by accounts deleted before this.
"""

import json
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.serializers.chat.unified_serializers import ActivitySerializer
from origin.services import account_export
from origin.services import deleted_account_activity as gone
from origin.services import v3_activity as va
from origin.services.account_deletion import DELETED_USERNAME, delete_account
from origin.services.v3_activity import SURFACE_AI_APP
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
SECRET = "the layoff list is attached"
ALL_TYPES = [t for t in ActivityType]


class ActivityBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.channel = Channel.objects.create(
            team=self.team, kind=ChannelKind.GM, title="General", owner=self.user
        )
        self._seq = 0

    def message(self, sender):
        self._seq += 1
        return Message.objects.create(
            channel=self.channel,
            sender=sender,
            seq=self._seq,
            body={"text": SECRET},
            body_text=SECRET,
        )

    def row(self, recipient, actor, activity_type, *, team=None, with_message=True, **kw):
        message = self.message(actor) if with_message else None
        return Activity.objects.create(
            team=team or self.team,
            recipient=recipient,
            actor=actor,
            activity_type=activity_type,
            channel=self.channel if message else None,
            message=message,
            meta={"preview": SECRET, "items": [{"title": SECRET}]},
            **kw,
        )

    def alive(self):
        return set(Activity.objects.values_list("pk", flat=True))


class AccountDeletionRemovesTheMembersFeedTests(ActivityBase):
    def test_every_kind_of_row_for_the_member_is_deleted(self):
        mine = [self.row(self.user2, self.user, t) for t in ALL_TYPES]
        # surface rows (no channel/message): a task-body mention, a comment notification
        mine.append(
            Activity.objects.create(
                team=self.team,
                recipient=self.user2,
                actor=self.user,
                activity_type=ActivityType.MENTION,
                surface_type=5,
                meta={"taskId": 1, "isComment": True, "isCommentParticipant": True},
            )
        )
        mine.append(
            Activity.objects.create(
                team=self.team,
                recipient=self.user2,
                actor=self.user2,
                activity_type=ActivityType.AI_APP,
                surface_type=SURFACE_AI_APP,
                meta={"aiApp": True},
            )
        )
        delete_account(self.user2)
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk for a in mine]).exists())
        self.assertFalse(Activity.objects.filter(recipient=self.user2).exists())

    def test_the_covered_kinds_are_every_activity_type(self):
        # A new ActivityType must be added to ALL_TYPES (and so to the tests above), not
        # silently skipped by the deletion: it is filtered by recipient only, never by kind.
        self.assertEqual(len(ALL_TYPES), 6)

    def test_rows_in_every_team_of_the_member_go(self):
        other_team = TeamMaster.objects.create(
            team_name="Second", team_email="two@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=other_team, attendee=self.user2)
        a = self.row(self.user2, self.user, ActivityType.MENTION)
        b = self.row(self.user2, self.user, ActivityType.REACTION, team=other_team)
        delete_account(self.user2)
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk, b.pk]).exists())

    def test_another_members_rows_are_untouched(self):
        theirs = [self.row(self.user, self.user2, t) for t in ALL_TYPES]
        delete_account(self.user2)
        self.assertEqual(self.alive(), {a.pk for a in theirs})

    def test_rows_about_the_member_in_other_feeds_stay_unchanged(self):
        """`actor = the deleted member` rows are the OTHER person's feed: kept, and not
        rewritten (meta, message and read state exactly as they were)."""
        about = self.row(self.user, self.user2, ActivityType.REACTION, is_read=True)
        before = {
            "meta": dict(about.meta),
            "message_id": about.message_id,
            "is_read": about.is_read,
            "recipient_id": about.recipient_id,
            "ts": about.ts_created_at,
        }
        delete_account(self.user2)
        about = Activity.objects.get(pk=about.pk)
        self.assertEqual(about.meta, before["meta"])
        self.assertEqual(about.message_id, before["message_id"])
        self.assertEqual(about.is_read, before["is_read"])
        self.assertEqual(about.recipient_id, before["recipient_id"])
        self.assertEqual(about.ts_created_at, before["ts"])
        self.assertEqual(about.actor_id, self.user2.id)

    def test_the_other_feed_shows_the_anonymised_name(self):
        about = self.row(self.user, self.user2, ActivityType.MENTION)
        delete_account(self.user2)
        data = ActivitySerializer(Activity.objects.get(pk=about.pk)).data
        self.assertEqual(data["actor"]["userName"], DELETED_USERNAME)
        self.assertTrue(data["actor"]["userEmail"].endswith("@deleted.invalid"))
        self.assertNotIn("otheruser", json.dumps(data, default=str))
        self.assertNotIn("other@example.com", json.dumps(data, default=str))
        # and through the endpoint the other member reads
        self.authenticate(self.user)
        resp = self.client.get("/api/v3/activities/", {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 200)
        (entry,) = resp.json()["activities"]
        self.assertEqual(entry["actor"]["userName"], DELETED_USERNAME)

    def test_the_messages_themselves_are_not_deleted(self):
        # Deleting the member's feed rows never deletes the chat messages they pointed at.
        row = self.row(self.user2, self.user, ActivityType.MESSAGE)
        message_id = row.message_id
        delete_account(self.user2)
        self.assertTrue(Message.objects.filter(pk=message_id).exists())

    def test_a_large_feed_goes_in_batches(self):
        rows = [self.row(self.user2, self.user, ActivityType.MENTION, with_message=False)]
        rows += [
            self.row(self.user2, self.user, ActivityType.REACTION, with_message=False)
            for _ in range(6)
        ]
        keep = self.row(self.user, self.user2, ActivityType.MENTION, with_message=False)
        with mock.patch.object(gone, "_BATCH", 3):
            delete_account(self.user2)
        self.assertEqual(self.alive(), {keep.pk})

    def test_the_rows_are_deleted_in_the_same_transaction_as_the_erasure(self):
        # A failed erasure rolls the deletion of the feed back too.
        row = self.row(self.user2, self.user, ActivityType.MENTION)
        with mock.patch(
            "origin.services.account_deletion._delete_invites_to", side_effect=RuntimeError("x")
        ):
            with self.assertRaises(RuntimeError):
                delete_account(self.user2)
        self.assertIn(row.pk, self.alive())
        self.user2.refresh_from_db()
        self.assertFalse(self.user2.is_deleted)


class ExportIsTakenBeforeDeletionTests(ActivityBase):
    """The export reads the member's own AI-app notices. Mention / reply / reaction / message
    rows are notifications about OTHER people's content and have never been in the export: the
    document is the same with or without them, and deletion does not change what it lists."""

    def notice(self):
        return Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user2,
            activity_type=ActivityType.AI_APP,
            surface_type=SURFACE_AI_APP,
            meta={"aiApp": True, "app": "Claude Code", "items": [{"title": "Plan the launch"}]},
        )

    def test_the_export_still_lists_the_notices_before_the_account_is_deleted(self):
        self.notice()
        for t in ALL_TYPES:
            if t != ActivityType.AI_APP:
                self.row(self.user2, self.user, t)
        doc = account_export.build_export(self.user2)
        self.assertEqual(doc["counts"]["ai_app_notices"], 1)
        self.assertEqual(doc["ai_app_notices"][0]["notice"]["items"][0]["title"], "Plan the launch")
        # the export is a pure read: taking it deletes nothing
        self.assertEqual(Activity.objects.filter(recipient=self.user2).count(), len(ALL_TYPES))

    def test_feed_rows_for_the_member_are_not_in_the_export_at_all(self):
        before = account_export.build_export(self.user2)
        for t in ALL_TYPES:
            if t != ActivityType.AI_APP:
                self.row(self.user2, self.user, t)
        after = account_export.build_export(self.user2)
        before.pop("generated_at", None)
        after.pop("generated_at", None)
        self.assertEqual(before, after)
        self.assertNotIn(SECRET, json.dumps(after))

    def test_the_download_endpoint_lists_the_notices_then_deletion_removes_them(self):
        self.notice()
        self.authenticate(self.user2)
        body = self.client.get("/api/v2/user/account/export/").json()
        self.assertEqual(body["counts"]["ai_app_notices"], 1)
        delete_account(self.user2)
        self.assertFalse(Activity.objects.filter(recipient=self.user2).exists())


class PruneBase(ActivityBase):
    def setUp(self):
        super().setUp()
        self.user3 = User.objects.create_user(
            username="third", email="third@example.com", password="x"
        )
        # an account deleted BEFORE this fix: soft-deleted, feed left behind
        self.old_gone = User.objects.create_user(
            username="old", email="old@example.com", password="x"
        )
        User.objects.filter(pk=self.old_gone.pk).update(is_deleted=True, is_active=False)

    def left_behind(self):
        return [
            self.row(self.old_gone, self.user, ActivityType.MENTION),
            self.row(self.old_gone, self.user, ActivityType.REACTION, with_message=False),
            Activity.objects.create(
                team=self.team,
                recipient=self.old_gone,
                actor=self.old_gone,
                activity_type=ActivityType.AI_APP,
                surface_type=SURFACE_AI_APP,
                meta={"items": [{"title": SECRET}]},
            ),
        ]


class PruneServiceTests(PruneBase):
    def test_dry_run_counts_and_writes_nothing(self):
        rows = self.left_behind()
        result = gone.prune()
        self.assertEqual((result.matched, result.deleted, result.dry_run), (3, 0, True))
        self.assertEqual(result.accounts, 1)
        self.assertEqual(result.by_type, {"mention": 1, "reaction": 1, "ai_app": 1})
        self.assertTrue(set(a.pk for a in rows) <= self.alive())

    def test_apply_deletes_only_rows_of_deleted_accounts(self):
        gone_rows = self.left_behind()
        live = [
            self.row(self.user, self.old_gone, ActivityType.MENTION),  # ABOUT the old account
            self.row(self.user2, self.user, ActivityType.MENTION),
            self.row(self.user3, self.user, ActivityType.REACTION),
        ]
        result = gone.prune(apply=True)
        self.assertEqual((result.matched, result.deleted, result.dry_run), (3, 3, False))
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk for a in gone_rows]).exists())
        self.assertEqual(self.alive(), {a.pk for a in live})

    def test_an_inactive_but_not_deleted_account_is_kept(self):
        User.objects.filter(pk=self.user3.pk).update(is_active=False)
        kept = self.row(self.user3, self.user, ActivityType.MENTION)
        gone.prune(apply=True)
        self.assertIn(kept.pk, self.alive())

    def test_a_deleted_account_row_made_by_the_service_is_nothing_left(self):
        self.row(self.user2, self.user, ActivityType.MENTION)
        delete_account(self.user2)
        self.assertEqual(gone.prune().matched, 0)

    def test_rows_about_a_deleted_account_are_never_matched(self):
        about = self.row(self.user, self.old_gone, ActivityType.MENTION)
        self.assertEqual(gone.prune().matched, 0)
        gone.prune(apply=True)
        self.assertIn(about.pk, self.alive())

    def test_batches(self):
        rows = [
            self.row(self.old_gone, self.user, ActivityType.REACTION, with_message=False)
            for _ in range(7)
        ]
        live = self.row(self.user2, self.user, ActivityType.MENTION)
        with mock.patch.object(gone, "_BATCH", 3):
            result = gone.prune(apply=True)
        self.assertEqual((result.matched, result.deleted), (7, 7))
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk for a in rows]).exists())
        self.assertIn(live.pk, self.alive())

    def test_a_second_apply_finds_nothing(self):
        self.left_behind()
        gone.prune(apply=True)
        again = gone.prune(apply=True)
        self.assertEqual((again.matched, again.deleted), (0, 0))

    def test_a_hard_deleted_account_leaves_no_rows_by_cascade(self):
        """`recipient` is CASCADE and `actor` SET_NULL: a row of an account that no longer
        exists at all cannot survive, so the command only has to look at soft-deleted ones."""
        for_it = self.row(self.user3, self.user, ActivityType.MENTION)
        about_it = self.row(self.user, self.user3, ActivityType.MENTION)
        self.user3.delete()
        self.assertNotIn(for_it.pk, self.alive())
        self.assertIn(about_it.pk, self.alive())
        self.assertIsNone(Activity.objects.get(pk=about_it.pk).actor_id)
        self.assertEqual(gone.prune().matched, 0)


class PruneCommandTests(PruneBase):
    def run_command(self, *args):
        out = StringIO()
        call_command("prune_deleted_account_activity", *args, stdout=out)
        return out.getvalue()

    def test_the_default_is_a_dry_run(self):
        rows = self.left_behind()
        text = self.run_command()
        self.assertIn("DRY RUN", text)
        self.assertIn("would delete 3", text)
        self.assertIn("1 deleted account", text)
        self.assertIn("--apply", text)
        self.assertTrue(set(a.pk for a in rows) <= self.alive())

    def test_apply_deletes(self):
        rows = self.left_behind()
        keep = self.row(self.user2, self.user, ActivityType.MENTION)
        text = self.run_command("--apply")
        self.assertIn("APPLY", text)
        self.assertIn("deleted 3", text)
        self.assertEqual(self.alive(), {keep.pk})
        self.assertFalse(Activity.objects.filter(pk__in=[a.pk for a in rows]).exists())

    def test_output_is_counts_only(self):
        for args in ((), ("--apply",)):
            rows = self.left_behind()
            text = self.run_command(*args)
            for needle in (
                SECRET,
                "old@example.com",
                "other@example.com",
                "test@example.com",
                str(self.old_gone.id),
                str(self.user.id),
                str(rows[0].pk),
                str(self.team.team_id),
            ):
                self.assertNotIn(needle, text)

    def test_nothing_to_do_says_so(self):
        text = self.run_command("--apply")
        self.assertIn("deleted 0", text)
        self.assertIn("0 deleted account", text)


class NoNewRowsForADeletedAccountTests(ActivityBase):
    """The rows must not grow back: a deleted account is still a channel member and still the
    sender of old messages, so every producer must skip it (MIP-1542)."""

    def setUp(self):
        super().setUp()
        self.dm = Channel.objects.create(
            team=self.team, kind=ChannelKind.DM, title="dm", owner=self.user
        )
        for u in (self.user, self.user2):
            ChannelMember.objects.create(channel=self.dm, user=u)
        self.old = Message.objects.create(
            channel=self.channel, sender=self.user2, seq=500, body={"t": 1}, body_text="old"
        )
        self.old_dm = Message.objects.create(
            channel=self.dm, sender=self.user2, seq=501, body={"t": 1}, body_text="old dm"
        )
        delete_account(self.user2)

    def fresh(self, channel, seq, **kw):
        return Message.objects.create(
            channel=channel, sender=self.user, seq=seq, body={"t": 1}, body_text="x", **kw
        )

    def assert_nothing_for_deleted(self):
        self.assertFalse(Activity.objects.filter(recipient=self.user2).exists())

    def test_a_reaction_to_their_old_message(self):
        va.create_reaction_activity(message=self.old, emoji="x", actor=self.user)
        self.assert_nothing_for_deleted()

    def test_a_thread_reply_under_their_old_message(self):
        reply = self.fresh(self.channel, 600, thread_root=self.old, is_thread_reply=True)
        va.create_thread_reply_activity(reply=reply, parent=self.old, actor=self.user)
        self.assert_nothing_for_deleted()

    def test_a_thread_reply_in_a_dm_they_are_still_a_member_of(self):
        reply = self.fresh(self.dm, 601, thread_root=self.old_dm, is_thread_reply=True)
        va.create_thread_reply_activity(reply=reply, parent=self.old_dm, actor=self.user)
        self.assert_nothing_for_deleted()

    def test_a_dm_message(self):
        message = self.fresh(self.dm, 602)
        va.create_message_activities(
            message=message, recipient_ids=[str(self.user2.id)], actor=self.user
        )
        self.assert_nothing_for_deleted()

    def test_a_mention(self):
        message = self.fresh(self.channel, 603)
        va.create_mention_activities(
            message=message, mentioned_user_ids=[str(self.user2.id)], actor=self.user
        )
        self.assert_nothing_for_deleted()

    def test_a_task_comment_participant_notice(self):
        message = self.fresh(self.channel, 604)
        va.create_comment_participant_activities(
            message=message, recipient_ids=[str(self.user2.id)], actor=self.user
        )
        self.assert_nothing_for_deleted()

    def test_a_task_assignment(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="P",
            owner=self.user,
            project_system_user=self.user,
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            title="t",
            status="Open",
            assignee=self.user2,
            reporter=self.user,
        )
        message = self.fresh(self.dm, 605, task=task)
        va.create_task_assign_activity(message=message, recipient_id=self.user2.id, actor=self.user)
        self.assert_nothing_for_deleted()

    def test_live_members_still_get_every_kind(self):
        """The guard drops only deleted accounts: a live member is still told."""
        user3 = User.objects.create_user(username="third", email="third@example.com", password="x")
        ChannelMember.objects.create(channel=self.dm, user=user3)
        va.create_mention_activities(
            message=self.fresh(self.channel, 700),
            mentioned_user_ids=[str(user3.id), str(self.user2.id)],
            actor=self.user,
        )
        va.create_message_activities(
            message=self.fresh(self.dm, 701),
            recipient_ids=[str(user3.id), str(self.user2.id)],
            actor=self.user,
        )
        mine = Message.objects.create(
            channel=self.channel, sender=user3, seq=702, body={"t": 1}, body_text="m"
        )
        va.create_reaction_activity(message=mine, emoji="x", actor=self.user)
        self.assertEqual(
            sorted(
                Activity.objects.filter(recipient=user3).values_list("activity_type", flat=True)
            ),
            sorted([ActivityType.MENTION, ActivityType.MESSAGE, ActivityType.REACTION]),
        )
        self.assert_nothing_for_deleted()

    def test_a_task_body_mention_is_covered_by_the_ended_team_membership(self):
        # No `_drop_deleted_accounts` here: the surface producers keep only live
        # `TeamMembers`, and account deletion ends the membership. Pinned so it stays so.
        va.create_surface_mention_activities(
            team_id=self.team.team_id,
            actor=self.user,
            surface_type=va.SURFACE_TASK_BODY,
            entity_key="task:42",
            newly_mentioned_user_ids=[str(self.user2.id)],
        )
        self.assert_nothing_for_deleted()

    def test_a_surface_comment_notice_is_covered_the_same_way(self):
        # The deleted member is still the task's assignee. A live member as assignee is the
        # control: the same call does write a row for them.
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P2",
            code="P2",
            owner=self.user,
            project_system_user=self.user,
        )
        for assignee in (self.user2, self.user):
            task = TaskMaster.objects.create(
                team=self.team,
                project=project,
                title="t",
                status="Open",
                assignee=assignee,
                reporter=self.user,
            )
            va.create_surface_comment_activities(
                team_id=self.team.team_id,
                actor=None,
                surface_type=va.SURFACE_TASK_BODY,
                entity_key=f"task:{task.task_id}",
                meta={"taskId": task.task_id},
            )
        self.assertTrue(Activity.objects.filter(recipient=self.user).exists())  # control
        self.assert_nothing_for_deleted()
