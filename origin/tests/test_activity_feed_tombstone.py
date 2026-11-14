"""MIP-1513: the activity feed stops carrying message content for a chat the caller lost, and
says so with a tombstone.

`GET /api/v3/activities/` used to keep returning the message (body, sender, attachments) of an
activity row after its recipient left the chat (or was taken off the project of a project chat,
or the chat was deleted). Hiding the row alone breaks the client's incremental sync: it stores
`server_time` as its next `since` and never re-fetches an older row, so a row it already cached
would stay on screen forever. So a row the caller can no longer read is answered as an id in a
separate `tombstones` list (no content), on every request without a `cursor` (the first page),
whatever `since` is: the client drops those ids from its cache.

`create_task_assign_activity` (MIP-1495's family) asks the same rule, `can_read_chat`, so it does
not write a row onto a deleted chat.
"""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.chat.unified_models import (
    Activity,
    ActivityType,
    Channel,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_task_assignment_notice import AssignmentNoticeBase

User = get_user_model()
URL = "/api/v3/activities/"
SECRET = "the-secret-body-of-a-chat-i-left"


class FeedBase(BaseAPITestCase):
    """`self.user` is the recipient; `self.user2` writes the messages."""

    def setUp(self):
        super().setUp()
        self.authenticate()

    def group_chat(self, *members, deleted=False, team=None):
        channel = Channel.objects.create(
            team=team or self.team,
            kind=ChannelKind.GM,
            title="chat",
            owner=self.user2,
            is_deleted=deleted,
        )
        for m in members:
            ChannelMember.objects.create(channel=channel, user=m)
        return channel

    def message(self, channel, text):
        return Message.objects.create(
            channel=channel,
            sender=self.user2,
            body_text=text,
            body=[],
            seq=Message.objects.filter(channel=channel).count() + 1,
        )

    def activity(self, channel, *, when=None, text=SECRET):
        message = self.message(channel, text)
        row = Activity.objects.create(
            team=channel.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            channel=channel,
            message=message,
        )
        if when is not None:
            Activity.objects.filter(pk=row.pk).update(ts_created_at=when)
        return row

    def get(self, **params):
        res = self.client.get(URL, params)
        self.assertEqual(res.status_code, 200)
        return res.json()

    def ids(self, data):
        return {a["id"] for a in data["activities"]}


class FeedKeepsNoContentOfALostChat(FeedBase):
    def test_a_chat_you_are_in_is_unchanged(self):
        row = self.activity(self.group_chat(self.user, self.user2))
        data = self.get()
        self.assertEqual(self.ids(data), {str(row.id)})
        self.assertEqual(data["tombstones"], [])
        self.assertEqual(data["activities"][0]["message"]["bodyText"], SECRET)

    def test_a_chat_you_left_returns_no_content_and_a_tombstone(self):
        channel = self.group_chat(self.user, self.user2)
        row = self.activity(channel)
        ChannelMember.objects.filter(channel=channel, user=self.user).update(is_deleted=True)
        data = self.get()
        self.assertEqual(data["activities"], [])
        self.assertEqual(data["tombstones"], [str(row.id)])
        self.assertNotIn(SECRET, str(data))

    def test_a_chat_you_never_were_in_is_the_same(self):
        row = self.activity(self.group_chat(self.user2))
        data = self.get()
        self.assertEqual(self.ids(data), set())
        self.assertEqual(data["tombstones"], [str(row.id)])
        self.assertNotIn(SECRET, str(data))

    def test_a_deleted_chat_is_gone(self):
        row = self.activity(self.group_chat(self.user, self.user2, deleted=True))
        data = self.get()
        self.assertEqual(self.ids(data), set())
        self.assertEqual(data["tombstones"], [str(row.id)])

    def test_a_project_chat_is_the_projects_people_not_the_mirror_row(self):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="TMB",
            owner=self.user2,
            project_system_user=self.user2,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user2)
        pm = Channel.objects.get(kind=ChannelKind.PM, project=project)
        # a stray channel row for someone who is not on the project opens nothing
        ChannelMember.objects.update_or_create(
            channel=pm, user=self.user, defaults={"is_deleted": False}
        )
        row = self.activity(pm)
        data = self.get()
        self.assertEqual(self.ids(data), set())
        self.assertEqual(data["tombstones"], [str(row.id)])
        # taken on to the project: readable again, no tombstone
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        data = self.get()
        self.assertEqual(self.ids(data), {str(row.id)})
        self.assertEqual(data["tombstones"], [])

    def test_a_surface_row_with_no_channel_is_judged_by_its_task_not_its_chat(self):
        # MIP-1560: no channel to ask, so the task / note it points at decides. Here the
        # recipient is the assignee of the task, so the row is listed and not a tombstone.
        from origin.models.project.prj_models import ProjectMaster
        from origin.models.task.task_models import TaskMaster

        project = ProjectMaster.objects.create(
            team=self.team,
            project_name="P",
            code="SRF",
            owner=self.user2,
            project_system_user=self.user2,
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=self.user2,
            assignee=self.user,
            title="T",
            status="Open",
            project_task_number=1,
        )
        row = Activity.objects.create(
            team=self.team,
            recipient=self.user,
            actor=self.user2,
            activity_type=ActivityType.MENTION,
            surface_type=5,
            meta={"taskId": task.task_id},
        )
        data = self.get()
        self.assertEqual(self.ids(data), {str(row.id)})
        self.assertEqual(data["tombstones"], [])

    def test_another_recipients_rows_are_never_listed_as_tombstones(self):
        channel = self.group_chat(self.user2)
        message = self.message(channel, "x")
        Activity.objects.create(
            team=self.team,
            recipient=self.user2,
            actor=self.user,
            activity_type=ActivityType.MENTION,
            channel=channel,
            message=message,
        )
        self.assertEqual(self.get()["tombstones"], [])


class TombstoneReachesAnIncrementalSync(FeedBase):
    """The reason for the tombstone: `since` only returns newer rows."""

    def test_an_old_row_is_tombstoned_though_it_is_older_than_since(self):
        channel = self.group_chat(self.user, self.user2)
        old = self.activity(channel, when=timezone.now() - timedelta(days=10))
        ChannelMember.objects.filter(channel=channel, user=self.user).update(is_deleted=True)
        since = (timezone.now() - timedelta(days=1)).isoformat()
        data = self.get(since=since)
        self.assertEqual(data["activities"], [])
        self.assertEqual(data["tombstones"], [str(old.id)])

    def test_a_row_past_the_30_day_window_is_not_tombstoned(self):
        channel = self.group_chat(self.user2)
        self.activity(channel, when=timezone.now() - timedelta(days=45))
        self.assertEqual(self.get(since=timezone.now().isoformat())["tombstones"], [])

    def test_a_cursor_page_carries_no_tombstones(self):
        channel = self.group_chat(self.user2)
        self.activity(channel)
        keep = self.group_chat(self.user, self.user2)
        self.activity(keep)
        first = self.get()
        self.assertEqual(len(first["tombstones"]), 1)
        # a cursor request is a later page of the same sync: the ids were already sent
        from origin.views.chat.activity_views_v3 import _encode_cursor

        row = Activity.objects.filter(channel=keep).first()
        later = self.get(cursor=_encode_cursor(row))
        self.assertEqual(later["tombstones"], [])

    def test_the_team_narrowing_applies_to_tombstones(self):
        other = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user2
        )
        TeamMembers.objects.create(team=other, attendee=self.user)
        TeamMembers.objects.create(team=other, attendee=self.user2)
        here = self.activity(self.group_chat(self.user2))
        there = self.activity(self.group_chat(self.user2, team=other))
        self.assertEqual(self.get(team_id=str(self.team.team_id))["tombstones"], [str(here.id)])
        self.assertEqual(self.get(team_id=str(other.team_id))["tombstones"], [str(there.id)])
        self.assertEqual(set(self.get()["tombstones"]), {str(here.id), str(there.id)})

    def test_an_unparseable_team_still_answers_the_new_key(self):
        self.assertEqual(self.get(team_id="nope")["tombstones"], [])

    def test_a_tombstone_does_not_use_up_a_page(self):
        from origin.views.chat import activity_views_v3

        lost = self.group_chat(self.user2)
        for _ in range(3):
            self.activity(lost)
        kept = self.group_chat(self.user, self.user2)
        rows = [self.activity(kept) for _ in range(2)]
        old = activity_views_v3._MAX_ROWS
        activity_views_v3._MAX_ROWS = 2
        try:
            data = self.get()
        finally:
            activity_views_v3._MAX_ROWS = old
        self.assertEqual(self.ids(data), {str(r.id) for r in rows})
        self.assertFalse(data["has_more"])
        self.assertEqual(len(data["tombstones"]), 3)


class TaskAssignFollowsTheLiveChat(AssignmentNoticeBase):
    """`self.channel` is the PM chat of `self.project`; user, user2, user3 are on the project."""

    def make(self):
        from origin.services import v3_activity

        return v3_activity.create_task_assign_activity(
            message=self.card, recipient_id=self.user2.id, actor=self.user
        )

    def test_a_member_of_a_live_chat_is_told(self):
        self.assertEqual(len(self.make()), 1)

    def test_a_member_of_a_deleted_chat_is_not_told(self):
        Channel.objects.filter(pk=self.channel.pk).update(is_deleted=True)
        self.channel.refresh_from_db()
        self.card.refresh_from_db()
        self.assertEqual(self.make(), [])
        self.assertFalse(Activity.objects.exists())

    def test_someone_off_the_project_is_not_told(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.assertEqual(self.make(), [])
