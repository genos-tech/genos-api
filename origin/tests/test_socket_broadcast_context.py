"""What genos-sockets broadcasts must come from here, not from its client (MIP-715).

The socket layer relays task comments and reactions to `prj-{project_id}`
and heartbeats to team rooms. It used to take the room and the content
from the client's payload: the `project_id` a comment event was sent to,
the comment author and first line a reaction activity quoted, and the
whole profile a heartbeat announced. Each of these endpoints is the
socket handler's only server-side source, so each now returns what the
broadcast needs, read from the task, the comment or the caller.
"""

from origin.models.common.team_models import TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase

COMMENT = "/api/v2/task/comment/"
REACTION = "/api/v2/task/comment/reaction/"
PRESENCE_TEAMS = "/api/v2/user/presence/teams/"


def _body(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class CommentBroadcastContextBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Broadcast",
            owner=self.user,
            project_system_user=self.user,
            is_private=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )
        # A project the caller also belongs to, which is the room a forged
        # `project_id` would have aimed at.
        self.other_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Elsewhere",
            owner=self.user,
            project_system_user=self.user,
        )

    def assert_task_context(self, data):
        self.assertEqual(data["projectId"], self.project.project_id)
        self.assertEqual(data["projectName"], "Broadcast")
        self.assertIs(data["isPrivate"], True)
        self.assertEqual(data["displayId"], self.task.display_id)


class CommentResponsesNameTheTasksProjectTests(CommentBroadcastContextBase):
    def test_a_new_comment_reports_the_tasks_project_not_the_one_the_body_names(self):
        self.authenticate(self.user)
        res = self.client.post(
            COMMENT,
            {
                "task_id": self.task.task_id,
                "comment_body": _body("hi"),
                "project_id": self.other_project.project_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assert_task_context(res.data)

    def test_an_edit_reports_the_tasks_project(self):
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=_body("a")
        )
        self.authenticate(self.user)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("b")},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assert_task_context(res.data)

    def test_a_delete_reports_the_tasks_project(self):
        TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=_body("a")
        )
        self.authenticate(self.user)
        res = self.client.delete(f"{COMMENT}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(res.status_code, 200, res.data)
        self.assert_task_context(res.data)
        self.assertEqual(res.data["taskCommentCount"], 0)


class ReactionResponsesCarryTheCommentTests(CommentBroadcastContextBase):
    def setUp(self):
        super().setUp()
        # user2 wrote the comment; user reacts to it.
        TaskComments.objects.create(
            task=self.task, sender=self.user2, comment_id=1, comment_body=_body("the real one")
        )

    def _react(self, **extra):
        return self.client.post(
            REACTION,
            {
                "team_id": str(self.team.team_id),
                "task_id": self.task.task_id,
                "comment_id": 1,
                "reaction_emoji": "👍",
                "sender_id": str(self.user.id),
                **extra,
            },
            format="json",
        )

    def _unreact(self):
        return self.client.delete(
            f"{REACTION}?team_id={self.team.team_id}&sender_id={self.user.id}"
            f"&task_id={self.task.task_id}&comment_id=1&reaction_emoji=%F0%9F%91%8D"
        )

    def test_a_reaction_reports_the_comments_real_author_and_body(self):
        self.authenticate(self.user)
        res = self._react(comment_sender_id=str(self.user.id), comment_body=_body("forged"))
        self.assertEqual(res.status_code, 201, res.data)
        self.assert_task_context(res.data)
        self.assertEqual(res.data["commentSenderId"], str(self.user2.id))
        self.assertEqual(res.data["commentBody"], _body("the real one"))
        self.assertEqual(res.data["senderName"], self.user.username)
        self.assertEqual(res.data["reaction_id"], 1)

    def test_a_reaction_to_a_deleted_comment_names_no_author(self):
        TaskComments.objects.filter(task=self.task, comment_id=1).update(is_deleted=True)
        self.authenticate(self.user)
        res = self._react()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertIsNone(res.data["commentSenderId"])
        self.assertEqual(res.data["commentBody"], [])

    def test_removing_a_reaction_answers_with_a_body_the_socket_layer_can_read(self):
        """It answered 204, which the socket handler couldn't parse."""
        self.authenticate(self.user)
        self.assertEqual(self._react().status_code, 201)
        res = self._unreact()
        self.assertEqual(res.status_code, 200)
        self.assert_task_context(res.data)
        self.assertEqual(res.data["reaction_id"], 1)
        self.assertEqual(res.data["comment_id"], 1)
        self.assertEqual(res.data["commentSenderId"], str(self.user2.id))
        self.assertEqual(res.data["sender"], str(self.user.id))
        self.assertFalse(TaskCommentReactionFact.objects.exists())


class PresenceProfileTests(BaseAPITestCase):
    """The card a heartbeat is announced with."""

    def setUp(self):
        super().setUp()
        TeamMembers.objects.get_or_create(team=self.team, attendee=self.user)
        self.user.phone_number = "+81-90-0000-0000"
        self.user.current_location = "Asia/Tokyo"
        self.user.role = "Engineer"
        self.user.about_me = "hello"
        self.user.save()

    def _profile(self):
        self.authenticate(self.user)
        res = self.client.get(PRESENCE_TEAMS, {"user_id": str(self.user.id)})
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["profile"]

    def test_the_card_is_the_callers_own(self):
        profile = self._profile()
        self.assertEqual(profile["userId"], str(self.user.id))
        self.assertEqual(profile["userName"], self.user.username)
        self.assertEqual(profile["role"], "Engineer")
        self.assertEqual(profile["aboutMe"], "hello")
        self.assertEqual(profile["currentLocation"], "Asia/Tokyo")

    def test_the_card_carries_no_contact_details(self):
        """The audience includes the far side of every cross-team share."""
        profile = self._profile()
        self.assertNotIn("phoneNumber", profile)
        self.assertNotIn("userEmail", profile)
        self.assertNotIn("+81-90-0000-0000", str(profile))
        self.assertNotIn(self.user.email, str(profile))

    def test_an_unshared_location_is_blank_on_the_card(self):
        self.user.location_shared = False
        self.user.save()
        self.assertEqual(self._profile()["currentLocation"], "")

    def test_someone_elses_audience_is_still_refused(self):
        self.authenticate(self.user)
        res = self.client.get(PRESENCE_TEAMS, {"user_id": str(self.user2.id)})
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("profile", res.data)
