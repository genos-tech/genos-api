"""Adding someone to an EXISTING milestone's assignees tells them (MIP-1464).

At creation the milestone's card @-mentions everyone on it, which tells them. Adding a person
later only made the browser rewrite the card (no activity), and `manage_milestone` update wrote
the rows and nothing else. `notify_milestone_assigned` is the one rule, called from the app's
milestone PATCH, the per-person endpoint and `manage_milestone`. It is never called from creation.
"""

from unittest import mock

from origin.models.chat.unified_models import Activity, ActivityType, Message
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMembers
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.search_engine.agent.tools.manage_milestone import MANAGE_MILESTONE
from origin.services import realtime_push, task_assignment_notice, v3_activity, webpush_dispatch
from origin.services.milestone_service import sync_milestone_assignees

from .test_task_card_post import CardPostBase

MILESTONE_URL = "/api/v2/milestone/"


class MilestoneNoticeBase(CardPostBase):
    def setUp(self):
        super().setUp()
        self.user3 = CustomUser.objects.create_user(
            email="third@example.com", username="thirduser", password="x"
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user3)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user3)
        out = MANAGE_MILESTONE.run(
            {
                "action": "create",
                "project_id": self.project.project_id,
                "title": "Launch",
                "assignee_ids": [str(self.user2.id)],
            },
            self.ctx,
        )
        self.milestone = MilestoneMaster.objects.get(milestone_id=out["milestone_id"])
        self.card = Message.objects.get(channel=self.channel, task_id=self.milestone.task_id)
        # What creation told people is not what these tests are about.
        Activity.objects.all().delete()

    def _set(self, *users):
        return MANAGE_MILESTONE.run(
            {
                "action": "update",
                "milestone_id": self.milestone.milestone_id,
                "assignee_ids": [str(u.id) for u in users],
            },
            self.ctx,
        )

    def _told(self, user):
        return Activity.objects.filter(recipient=user, activity_type=ActivityType.MENTION)


class ManageMilestoneNoticeTests(MilestoneNoticeBase):
    def test_a_person_added_later_is_told_on_the_milestones_card(self):
        self._set(self.user2, self.user3)

        (activity,) = self._told(self.user3)
        self.assertEqual(activity.message_id, self.card.id)
        self.assertEqual(activity.actor_id, self.user.id)
        self.assertEqual(activity.meta, {"taskAssign": True})

    def test_someone_already_on_it_is_not_told_again(self):
        self._set(self.user2, self.user3)

        self.assertFalse(self._told(self.user2).exists())

    def test_replacing_the_set_tells_only_the_new_person(self):
        self._set(self.user3)  # user2 out, user3 in

        self.assertEqual(self._told(self.user3).count(), 1)
        self.assertFalse(self._told(self.user2).exists())

    def test_nobody_is_told_for_a_removal_an_unchanged_set_or_adding_yourself(self):
        self._set()  # everyone off
        self._set(self.user2)  # user2 back: told, so clear it
        Activity.objects.all().delete()

        self._set(self.user2)  # unchanged
        self._set(self.user2, self.user)  # the actor adds themselves
        self._set()  # removal

        self.assertFalse(Activity.objects.exists())

    def test_the_new_assignees_open_app_and_phone_are_told_at_once(self):
        with (
            mock.patch.object(realtime_push, "push_activity") as live,
            mock.patch.object(webpush_dispatch, "schedule_push_for_activities") as push,
        ):
            self._set(self.user2, self.user3)

        (activity,) = self._told(self.user3)
        live.assert_called_once_with(activity)
        push.assert_called_once_with([activity])

    def test_someone_outside_the_project_chat_is_not_told(self):
        ProjectMembers.objects.filter(project=self.project, attendee=self.user3).delete()

        self._set(self.user2, self.user3)

        self.assertFalse(self._told(self.user3).exists())
        self.assertTrue(MilestoneAssignees.objects.filter(user=self.user3).exists())

    def test_someone_reading_that_chat_right_now_is_not_told(self):
        with mock.patch.object(v3_activity, "_drop_active_viewers", return_value=[]):
            self._set(self.user2, self.user3)

        self.assertFalse(self._told(self.user3).exists())

    def test_a_milestone_with_no_card_is_given_one_that_tells_everyone_on_it(self):
        """With no card nobody was ever told they were on it, so its mentions are the first
        notice for the whole set, not only the person just added."""
        Message.objects.filter(pk=self.card.pk).delete()

        self._set(self.user2, self.user3)

        (card,) = Message.objects.filter(channel=self.channel, task_id=self.milestone.task_id)
        self.assertEqual(self._told(self.user2).get().message_id, card.id)
        self.assertEqual(self._told(self.user3).get().message_id, card.id)

    def test_a_failing_notice_never_undoes_the_assignment(self):
        with mock.patch.object(
            v3_activity, "create_task_assign_activity", side_effect=RuntimeError("boom")
        ):
            self._set(self.user2, self.user3)

        self.assertTrue(
            MilestoneAssignees.objects.filter(milestone=self.milestone, user=self.user3).exists()
        )

    def test_the_notice_itself_never_raises(self):
        with mock.patch.object(v3_activity, "create_task_assign_activity", side_effect=ValueError):
            self.assertEqual(
                task_assignment_notice.notify_milestone_assigned(
                    self.milestone, actor_id=self.user.id, added_user_ids=[str(self.user3.id)]
                ),
                [],
            )


class AppMilestoneNoticeTests(MilestoneNoticeBase):
    def _patch(self, **payload):
        self.authenticate()
        res = self.client.patch(
            f"{MILESTONE_URL}{self.milestone.milestone_id}/", payload, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)

    def test_adding_an_assignee_in_the_app_tells_them(self):
        self._patch(assignee_ids=[str(self.user2.id), str(self.user3.id)])

        (activity,) = self._told(self.user3)
        self.assertEqual(activity.message_id, self.card.id)
        self.assertFalse(self._told(self.user2).exists())

    def test_a_patch_that_leaves_the_assignees_alone_tells_nobody(self):
        self._patch(title="Launch v2", status="WIP")

        self.assertFalse(Activity.objects.exists())

    def test_adding_one_person_through_the_per_person_endpoint_tells_them(self):
        self.authenticate()
        res = self.client.post(
            f"{MILESTONE_URL}{self.milestone.milestone_id}/assignees/",
            {"user_id": str(self.user3.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        self.assertEqual(self._told(self.user3).count(), 1)

        # The same person again is not a new assignment.
        self.client.post(
            f"{MILESTONE_URL}{self.milestone.milestone_id}/assignees/",
            {"user_id": str(self.user3.id)},
            format="json",
        )
        self.assertEqual(self._told(self.user3).count(), 1)

    def test_creating_a_milestone_in_the_app_tells_nobody_from_the_server(self):
        """The browser posts the creation card, whose mentions tell the assignees; the server
        adding a notice on top would ping them twice."""
        self.authenticate()
        res = self.client.post(
            MILESTONE_URL,
            {
                "project_id": self.project.project_id,
                "title": "From the app",
                "assignee_ids": [str(self.user2.id), str(self.user3.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

        self.assertFalse(Activity.objects.exists())


class SyncMilestoneAssigneesTests(MilestoneNoticeBase):
    def test_it_returns_only_the_people_it_added(self):
        added = sync_milestone_assignees(
            self.milestone, [str(self.user2.id), str(self.user3.id), ""]
        )

        self.assertEqual(added, [str(self.user3.id)])

    def test_it_returns_nothing_for_an_unchanged_or_shrinking_set(self):
        self.assertEqual(sync_milestone_assignees(self.milestone, [str(self.user2.id)]), [])
        self.assertEqual(sync_milestone_assignees(self.milestone, []), [])
