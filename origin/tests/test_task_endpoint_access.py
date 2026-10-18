"""Task sub-resources that resolved a task from a walkable id alone (MIP-712).

`TaskMaster.task_id` is a `BigAutoField`, so every task in the install is
reachable by counting. `test_task_object_scoping` closed that for the task
row itself; the endpoints hanging off it — attachments, comments,
reactions, mentions, body uploads and dependency edges — took the id and
acted on it with no access check at all. The comment and reaction writes
also took their author from a client-supplied `sender_id`, so anyone could
post, or retract, as anyone.

And `TaskMasterView.post` filed a new task into whatever project id the
payload named. The base view's `align_team_to_object` already moves `team`
onto the project's own team, but only for a caller who reaches the
project, which is exactly the caller this did not need to stop.

Access follows `scope_guards.can_access_task` (project members plus the
assignee and the reporter) everywhere, and every refusal is the 404 a
missing task gets, so the id space is not an existence oracle.
"""

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import (
    TaskAttachments,
    TaskBodyAttachmentFact,
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskDependency,
    TaskMaster,
)
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()

ATTACHMENT = "/api/v2/task/attachment/"
BODY_ATTACHMENT = "/api/v2/task/body/attachment/"
COMMENT = "/api/v2/task/comment/"
REACTION = "/api/v2/task/comment/reaction/"
MENTION = "/api/v2/task/comment/mention/"
DEPENDENCY = "/api/v2/task/dependency/"
DEPENDENCY_LIST = "/api/v2/task/dependency/list/"
DEPENDENCY_BATCH = "/api/v2/task/dependency/list-for-tasks/"
TASK = "/api/v2/task/"


def _body(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]


class TaskEndpointAccessBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        # Team A's private project. user and user2 are both on it, so a
        # comment one of them wrote is someone else's for the other.
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Private", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="Private task",
            status="Open",
            reporter=self.user,
        )

        # A separate tenant: the attacker, with a project of their own.
        self.outsider = User.objects.create_user(
            username="subout", email="subout@example.com", password="pw"
        )
        self.outsider_team = TeamMaster.objects.create(
            team_name="Sub Outsider", team_email="subout@example.com", owner=self.outsider
        )
        TeamMembers.objects.create(team=self.outsider_team, attendee=self.outsider)
        self.outsider_project = ProjectMaster.objects.create(
            team=self.outsider_team,
            project_name="Outsider's own",
            owner=self.outsider,
            project_system_user=self.outsider,
        )
        ProjectMembers.objects.create(
            team=self.outsider_team, project=self.outsider_project, attendee=self.outsider
        )
        self.outsider_task = TaskMaster.objects.create(
            team=self.outsider_team,
            project=self.outsider_project,
            title="Outsider task",
            status="Open",
            reporter=self.outsider,
        )

    def _comment(self, sender, comment_id=1, text="original"):
        return TaskComments.objects.create(
            task=self.task, sender=sender, comment_id=comment_id, comment_body=_body(text)
        )


class AttachmentAccessTests(TaskEndpointAccessBase):
    def setUp(self):
        super().setUp()
        self.attachment = TaskAttachments.objects.create(
            task=self.task,
            attachment_id=1,
            attached_file=SimpleUploadedFile("secret.txt", b"secret"),
            attached_type="text/plain",
            original_filename="secret.txt",
        )

    def tearDown(self):
        for row in TaskAttachments.objects.all():
            row.attached_file.delete(save=False)
        super().tearDown()

    def _upload(self, task_id):
        return self.client.post(
            ATTACHMENT,
            {
                "task": str(task_id),
                "attachment_id": "-1",
                "attached_type": "text/plain",
                "attached_file": SimpleUploadedFile("planted.txt", b"planted"),
            },
            format="multipart",
        )

    def test_outsider_cannot_list_attachments(self):
        self.authenticate(self.outsider)
        res = self.client.get(ATTACHMENT, {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 404)

    def test_member_lists_the_tasks_attachments(self):
        """It queried `TaskMaster` by a field TaskMaster does not have, so
        it was a 500 for everyone, member or not."""
        self.authenticate(self.user)
        res = self.client.get(ATTACHMENT, {"task_id": self.task.task_id})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([a["attachment_id"] for a in res.data], [1])
        self.assertNotIn("title", res.data[0])

    def test_a_malformed_task_id_is_a_400_not_a_500(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get(ATTACHMENT, {"task_id": "abc"}).status_code, 400)

    def test_outsider_cannot_upload_onto_the_task(self):
        self.authenticate(self.outsider)
        self.assertEqual(self._upload(self.task.task_id).status_code, 404)
        self.assertEqual(TaskAttachments.objects.filter(task=self.task).count(), 1)

    def test_member_can_still_upload(self):
        self.authenticate(self.user2)
        self.assertEqual(self._upload(self.task.task_id).status_code, 201)
        self.assertEqual(TaskAttachments.objects.filter(task=self.task).count(), 2)

    def test_outsider_cannot_delete_an_attachment(self):
        self.authenticate(self.outsider)
        res = self.client.delete(f"{ATTACHMENT}?task={self.task.task_id}&attachment_id=1")
        self.assertEqual(res.status_code, 404)
        self.assertTrue(TaskAttachments.objects.filter(pk=self.attachment.pk).exists())

    def test_member_can_still_delete_an_attachment(self):
        self.authenticate(self.user2)
        res = self.client.delete(f"{ATTACHMENT}?task={self.task.task_id}&attachment_id=1")
        self.assertEqual(res.status_code, 204)
        self.assertFalse(TaskAttachments.objects.filter(pk=self.attachment.pk).exists())


class BodyAttachmentAccessTests(TaskEndpointAccessBase):
    def tearDown(self):
        for row in TaskBodyAttachmentFact.objects.all():
            row.body_attachment_url.delete(save=False)
        super().tearDown()

    def _upload(self, task_id, uploader):
        return self.client.post(
            BODY_ATTACHMENT,
            {
                "task_id": task_id,
                "uploader": str(uploader.id),
                "body_attachment_file": SimpleUploadedFile(
                    "pic.png", b"x" * 64, content_type="image/png"
                ),
            },
            format="multipart",
        )

    def test_outsider_cannot_upload_into_a_tasks_body(self):
        """`uploader` was checked against the caller; the task never was."""
        self.authenticate(self.outsider)
        self.assertEqual(self._upload(self.task.task_id, self.outsider).status_code, 404)
        self.assertFalse(TaskBodyAttachmentFact.objects.filter(task=self.task).exists())

    def test_member_can_still_upload(self):
        self.authenticate(self.user)
        self.assertEqual(self._upload(self.task.task_id, self.user).status_code, 200)


class CommentAccessTests(TaskEndpointAccessBase):
    def test_outsider_cannot_comment(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            COMMENT,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.outsider.id),
                "comment_body": _body("hi"),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskComments.objects.filter(task=self.task).exists())

    def test_the_author_is_the_caller_whatever_sender_id_says(self):
        """`sender_id` came off the request body, so a member could post
        as any colleague, and the activity feed and push said it was them."""
        self.authenticate(self.user2)
        res = self.client.post(
            COMMENT,
            {
                "task_id": self.task.task_id,
                "sender_id": str(self.user.id),
                "comment_body": _body("I am user"),
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(
            str(TaskComments.objects.get(task=self.task).sender_id), str(self.user2.id)
        )

    def test_sender_id_may_be_omitted(self):
        self.authenticate(self.user2)
        res = self.client.post(
            COMMENT, {"task_id": self.task.task_id, "comment_body": _body("hi")}, format="json"
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_outsider_cannot_edit_a_comment(self):
        comment = self._comment(self.user)
        self.authenticate(self.outsider)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("pwned")},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        comment.refresh_from_db()
        self.assertEqual(comment.comment_body, _body("original"))

    def test_a_teammate_cannot_edit_someone_elses_comment(self):
        """The UI only offers edit on your own comments; the API offered it
        on everyone's."""
        comment = self._comment(self.user)
        self.authenticate(self.user2)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("rewritten")},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        comment.refresh_from_db()
        self.assertEqual(comment.comment_body, _body("original"))

    def test_the_author_can_edit_their_comment(self):
        comment = self._comment(self.user)
        self.authenticate(self.user)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("edited")},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        comment.refresh_from_db()
        self.assertEqual(comment.comment_body, _body("edited"))

    def test_an_edit_cannot_reassign_the_comment(self):
        """The whole body went to a partial serializer, so an edit could
        hand the comment to another author or move it to another task."""
        comment = self._comment(self.user)
        self.authenticate(self.user)
        res = self.client.put(
            COMMENT,
            {
                "task_id": self.task.task_id,
                "comment_id": 1,
                "comment_body": _body("edited"),
                "sender": str(self.user2.id),
                "task": self.outsider_task.task_id,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        comment.refresh_from_db()
        self.assertEqual(str(comment.sender_id), str(self.user.id))
        self.assertEqual(comment.task_id, self.task.task_id)

    def test_editing_a_missing_comment_is_a_404_not_a_500(self):
        self.authenticate(self.user)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 99, "comment_body": _body("x")},
            format="json",
        )
        self.assertEqual(res.status_code, 404)

    def test_outsider_cannot_delete_a_comment(self):
        comment = self._comment(self.user)
        self.authenticate(self.outsider)
        res = self.client.delete(f"{COMMENT}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(res.status_code, 404)
        comment.refresh_from_db()
        self.assertFalse(comment.is_deleted)

    def test_a_teammate_cannot_delete_someone_elses_comment(self):
        comment = self._comment(self.user)
        self.authenticate(self.user2)
        res = self.client.delete(f"{COMMENT}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(res.status_code, 404)
        comment.refresh_from_db()
        self.assertFalse(comment.is_deleted)

    def test_the_author_can_delete_their_comment(self):
        comment = self._comment(self.user2)
        self.authenticate(self.user2)
        res = self.client.delete(f"{COMMENT}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(res.status_code, 200, res.data)
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)

    def test_an_author_taken_off_the_project_can_no_longer_touch_their_comment(self):
        """Authorship is not access: off the project, the task is closed to
        them, comments included."""
        comment = self._comment(self.user2)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.authenticate(self.user2)
        res = self.client.put(
            COMMENT,
            {"task_id": self.task.task_id, "comment_id": 1, "comment_body": _body("after")},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        res = self.client.delete(f"{COMMENT}?task_id={self.task.task_id}&comment_id=1")
        self.assertEqual(res.status_code, 404)
        comment.refresh_from_db()
        self.assertEqual(comment.comment_body, _body("original"))
        self.assertFalse(comment.is_deleted)


class ReactionAccessTests(TaskEndpointAccessBase):
    def setUp(self):
        super().setUp()
        self._comment(self.user)

    def _react(self, sender_id):
        return self.client.post(
            REACTION,
            {
                "team_id": str(self.team.team_id),
                "task_id": self.task.task_id,
                "comment_id": 1,
                "reaction_emoji": "👍",
                "sender_id": str(sender_id),
            },
            format="json",
        )

    def _unreact(self, sender_id):
        return self.client.delete(
            f"{REACTION}?team_id={self.team.team_id}&sender_id={sender_id}"
            f"&task_id={self.task.task_id}&comment_id=1&reaction_emoji=%F0%9F%91%8D"
        )

    def _existing_reaction(self, sender):
        return TaskCommentReactionFact.objects.create(
            team=self.team,
            task=self.task,
            comment_id=1,
            reaction_id=1,
            reaction_emoji="👍",
            sender=sender,
        )

    def test_outsider_cannot_react(self):
        self.authenticate(self.outsider)
        self.assertEqual(self._react(self.outsider.id).status_code, 404)
        self.assertFalse(TaskCommentReactionFact.objects.exists())

    def test_the_reactor_is_the_caller_whatever_sender_id_says(self):
        self.authenticate(self.user2)
        self.assertEqual(self._react(self.user.id).status_code, 201)
        self.assertEqual(str(TaskCommentReactionFact.objects.get().sender_id), str(self.user2.id))

    def test_outsider_cannot_remove_a_reaction(self):
        reaction = self._existing_reaction(self.user)
        self.authenticate(self.outsider)
        self.assertEqual(self._unreact(self.user.id).status_code, 404)
        self.assertTrue(TaskCommentReactionFact.objects.filter(pk=reaction.pk).exists())

    def test_a_teammate_cannot_remove_someone_elses_reaction(self):
        """`sender_id` named whose reaction to delete."""
        reaction = self._existing_reaction(self.user)
        self.authenticate(self.user2)
        self.assertEqual(self._unreact(self.user.id).status_code, 404)
        self.assertTrue(TaskCommentReactionFact.objects.filter(pk=reaction.pk).exists())

    def test_you_can_remove_your_own_reaction(self):
        reaction = self._existing_reaction(self.user2)
        self.authenticate(self.user2)
        self.assertEqual(self._unreact(self.user2.id).status_code, 204)
        self.assertFalse(TaskCommentReactionFact.objects.filter(pk=reaction.pk).exists())

    def test_a_reactor_taken_off_the_project_can_no_longer_unreact(self):
        reaction = self._existing_reaction(self.user2)
        ProjectMembers.objects.filter(project=self.project, attendee=self.user2).delete()
        self.authenticate(self.user2)
        self.assertEqual(self._unreact(self.user2.id).status_code, 404)
        self.assertTrue(TaskCommentReactionFact.objects.filter(pk=reaction.pk).exists())


class MentionAccessTests(TaskEndpointAccessBase):
    def setUp(self):
        super().setUp()
        self._comment(self.user)

    def test_outsider_cannot_create_mentions(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            MENTION,
            {
                "team_id": str(self.team.team_id),
                "task_id": self.task.task_id,
                "comment_id": 1,
                "mentioned_user_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskCommentMentionFact.objects.exists())

    def test_outsider_cannot_delete_mentions(self):
        mention = TaskCommentMentionFact.objects.create(
            team=self.team, task=self.task, comment_id=1, mentioned_user=self.user2
        )
        self.authenticate(self.outsider)
        res = self.client.delete(
            f"{MENTION}?team_id={self.team.team_id}&task_id={self.task.task_id}"
            f"&comment_id=1&mentioned_user_ids={self.user2.id}"
        )
        self.assertEqual(res.status_code, 404)
        self.assertTrue(TaskCommentMentionFact.objects.filter(pk=mention.pk).exists())

    def test_member_can_still_create_and_delete_mentions(self):
        self.authenticate(self.user)
        res = self.client.post(
            MENTION,
            {
                "team_id": str(self.team.team_id),
                "task_id": self.task.task_id,
                "comment_id": 1,
                "mentioned_user_ids": [str(self.user2.id)],
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        res = self.client.delete(
            f"{MENTION}?team_id={self.team.team_id}&task_id={self.task.task_id}"
            f"&comment_id=1&mentioned_user_ids={self.user2.id}"
        )
        self.assertEqual(res.status_code, 204)


class DependencyAccessTests(TaskEndpointAccessBase):
    def setUp(self):
        super().setUp()
        self.other = TaskMaster.objects.create(
            team=self.team, project=self.project, title="Also private", status="Open"
        )
        # Same team, a project user2 is NOT on: what a teammate must not
        # learn about through an edge.
        self.hidden_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Leadership",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(
            team=self.team, project=self.hidden_project, attendee=self.user
        )
        self.hidden = TaskMaster.objects.create(
            team=self.team, project=self.hidden_project, title="Layoff plan", status="Open"
        )

    def test_outsider_cannot_link_two_tasks_they_cannot_see(self):
        self.authenticate(self.outsider)
        res = self.client.post(
            DEPENDENCY,
            {"blocker_task_id": self.task.task_id, "blocked_task_id": self.other.task_id},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskDependency.objects.exists())

    def test_a_member_cannot_link_to_a_task_they_cannot_see(self):
        """The 201 echoes the other end's title, project and assignee."""
        self.authenticate(self.user2)
        res = self.client.post(
            DEPENDENCY,
            {"blocker_task_id": self.task.task_id, "blocked_task_id": self.hidden.task_id},
            format="json",
        )
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("Layoff plan", str(res.data))
        self.assertFalse(TaskDependency.objects.exists())

    def test_member_can_still_link_tasks_they_can_see(self):
        self.authenticate(self.user2)
        res = self.client.post(
            DEPENDENCY,
            {"blocker_task_id": self.task.task_id, "blocked_task_id": self.other.task_id},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)

    def test_outsider_cannot_delete_an_edge(self):
        dep = TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.other, team=self.team
        )
        self.authenticate(self.outsider)
        self.assertEqual(self.client.delete(f"{DEPENDENCY}{dep.id}/").status_code, 404)
        self.assertTrue(TaskDependency.objects.filter(pk=dep.pk).exists())

    def test_a_member_cannot_delete_an_edge_into_a_task_they_cannot_see(self):
        dep = TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.hidden, team=self.team
        )
        self.authenticate(self.user2)
        self.assertEqual(self.client.delete(f"{DEPENDENCY}{dep.id}/").status_code, 404)
        self.assertTrue(TaskDependency.objects.filter(pk=dep.pk).exists())

    def test_member_can_still_delete_an_edge(self):
        dep = TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.other, team=self.team
        )
        self.authenticate(self.user2)
        self.assertEqual(self.client.delete(f"{DEPENDENCY}{dep.id}/").status_code, 204)

    def test_the_batch_list_says_nothing_about_tasks_the_caller_cannot_see(self):
        """500 ids per call, each answered with the other end's title,
        project and assignee."""
        TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.other, team=self.team
        )
        self.authenticate(self.outsider)
        res = self.client.get(
            DEPENDENCY_BATCH, {"task_ids": f"{self.task.task_id},{self.other.task_id}"}
        )
        self.assertEqual(res.status_code, 200)
        for tid in (self.task.task_id, self.other.task_id):
            self.assertEqual(
                res.data["dependencies_by_task"][str(tid)], {"blocking": [], "blockedBy": []}
            )

    def test_an_edge_into_a_hidden_task_is_left_out(self):
        """The visible end is fine to ask about; the hidden end's title is
        not part of the answer."""
        TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.hidden, team=self.team
        )
        TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.other, team=self.team
        )
        self.authenticate(self.user2)
        batch = self.client.get(DEPENDENCY_BATCH, {"task_ids": str(self.task.task_id)})
        single = self.client.get(DEPENDENCY_LIST, {"task_id": self.task.task_id})
        for blocking in (
            batch.data["dependencies_by_task"][str(self.task.task_id)]["blocking"],
            single.data["blocking"],
        ):
            self.assertEqual([d["otherTaskId"] for d in blocking], [self.other.task_id])

    def test_asking_the_batch_about_a_hidden_task_says_nothing(self):
        """The other end is one the caller can open, so filtering the other
        end alone would still answer for the hidden id they asked about."""
        TaskDependency.objects.create(
            blocker_task=self.hidden, blocked_task=self.task, team=self.team
        )
        TaskDependency.objects.create(
            blocker_task=self.other, blocked_task=self.hidden, team=self.team
        )
        self.authenticate(self.user2)
        res = self.client.get(DEPENDENCY_BATCH, {"task_ids": str(self.hidden.task_id)})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.data["dependencies_by_task"][str(self.hidden.task_id)],
            {"blocking": [], "blockedBy": []},
        )

    def test_whoever_can_see_both_ends_still_sees_the_edge(self):
        TaskDependency.objects.create(
            blocker_task=self.task, blocked_task=self.hidden, team=self.team
        )
        self.authenticate(self.user)
        res = self.client.get(DEPENDENCY_BATCH, {"task_ids": str(self.task.task_id)})
        blocking = res.data["dependencies_by_task"][str(self.task.task_id)]["blocking"]
        self.assertEqual([d["otherTaskId"] for d in blocking], [self.hidden.task_id])


class TaskCreateAccessTests(TaskEndpointAccessBase):
    def _payload(self, **overrides):
        payload = {
            "team": str(self.team.team_id),
            "project": self.project.project_id,
            "assignee": None,
            "reporter": str(self.user.id),
            "title": "New task",
            "priority": "medium",
            "effort_level": "medium",
            "status": "Open",
            "content": None,
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        payload.update(overrides)
        return payload

    def _create(self, **overrides):
        return self.client.post(TASK, self._payload(**overrides), format="json")

    def test_outsider_cannot_file_a_task_into_a_foreign_project(self):
        self.authenticate(self.outsider)
        res = self._create(team=str(self.outsider_team.team_id), reporter=str(self.outsider.id))
        self.assertEqual(res.status_code, 404)
        self.assertFalse(TaskMaster.objects.filter(title="New task").exists())

    def test_a_teammate_cannot_file_into_a_project_they_are_not_on(self):
        self.project_members_only = ProjectMaster.objects.create(
            team=self.team, project_name="Closed", owner=self.user, project_system_user=self.user
        )
        self.authenticate(self.user2)
        res = self._create(
            project=self.project_members_only.project_id, reporter=str(self.user2.id)
        )
        self.assertEqual(res.status_code, 404)

    def test_member_can_still_create(self):
        self.authenticate(self.user2)
        res = self._create(reporter=str(self.user2.id), assignee=str(self.user.id))
        self.assertEqual(res.status_code, 201, res.data)

    def test_the_team_follows_the_project(self):
        """A member naming their own project under another team's id."""
        self.authenticate(self.outsider)
        res = self._create(
            team=str(self.team.team_id),
            project=self.outsider_project.project_id,
            reporter=str(self.outsider.id),
        )
        self.assertEqual(res.status_code, 201, res.data)
        created = TaskMaster.objects.get(title="New task")
        self.assertEqual(str(created.team_id), str(self.outsider_team.team_id))

    def test_the_team_follows_the_project_on_a_multipart_body_too(self):
        """`align_team_to_object` leaves multipart bodies alone, so on this
        encoding the view's own choice of team is all there is."""
        self.authenticate(self.outsider)
        payload = self._payload(
            team=str(self.team.team_id),
            project=self.outsider_project.project_id,
            reporter=str(self.outsider.id),
            assignee="",
            content="",
            due_date="2026-12-01",
            links="[]",
            tags="[]",
        )
        res = self.client.post(TASK, payload, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        created = TaskMaster.objects.get(title="New task")
        self.assertEqual(str(created.team_id), str(self.outsider_team.team_id))

    def test_a_stranger_cannot_be_named_reporter(self):
        """The task lists the reporter as its author, and can_access_task
        lets them open it: a reporter is a grant."""
        self.authenticate(self.outsider)
        res = self._create(
            team=str(self.outsider_team.team_id),
            project=self.outsider_project.project_id,
            reporter=str(self.user.id),
        )
        self.assertEqual(res.status_code, 400)

    def test_a_stranger_cannot_be_named_assignee(self):
        self.authenticate(self.outsider)
        res = self._create(
            team=str(self.outsider_team.team_id),
            project=self.outsider_project.project_id,
            reporter=str(self.outsider.id),
            assignee=str(self.user.id),
        )
        self.assertEqual(res.status_code, 400)

    def test_strangers_are_dropped_from_collaborators(self):
        self.authenticate(self.user2)
        res = self._create(
            reporter=str(self.user2.id), collaborators=[str(self.user.id), str(self.outsider.id)]
        )
        self.assertEqual(res.status_code, 201, res.data)
        created = TaskMaster.objects.get(title="New task")
        self.assertEqual(
            {str(u) for u in created.collaborators.values_list("id", flat=True)},
            {str(self.user.id)},
        )

    def test_a_foreign_milestone_is_refused(self):
        foreign = MilestoneMaster.objects.create(
            team=self.outsider_team, project=self.outsider_project, title="Theirs"
        )
        self.authenticate(self.user2)
        res = self._create(reporter=str(self.user2.id), milestone=foreign.milestone_id)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(TaskMaster.objects.filter(title="New task").exists())

    def test_a_foreign_parent_is_refused(self):
        self.authenticate(self.user2)
        res = self._create(reporter=str(self.user2.id), parent_task_id=self.outsider_task.task_id)
        self.assertEqual(res.status_code, 400)
        self.assertFalse(TaskMaster.objects.filter(title="New task").exists())

    def test_a_parent_in_the_same_project_still_works(self):
        self.authenticate(self.user2)
        res = self._create(reporter=str(self.user2.id), parent_task_id=self.task.task_id)
        self.assertEqual(res.status_code, 201, res.data)
