"""A task view that read `request.data[...]` answers a missing field with a 400 (MIP-1020).

`request.data["team_id"]` raises KeyError on a body that lacks the key, and a KeyError that
nothing catches is a 500. After MIP-1013 the body is always an object (or `{}`), but a
partial one still got through the earlier checks and reached the read: a reaction with only a
`task_id` is a task its caller can open, and then `request.data["team_id"]`.

Only `TaskCommentReactionView` had a read that could actually raise. The rest of
`task_views.py` already answered: the task create catches the KeyError and says which field is
missing, the comment-mention view's `except Exception` answers its own 400, and the other
reads sat behind a presence check. They now read through `body_object(request)` and the
shared `missing_fields` / `require_fields`, and each keeps its status and its words, which
these tests pin so the rewrite cannot change them.

Each refusal also checks that nothing was written.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskComments,
    TaskMaster,
)
from origin.tests.test_base import BaseAPITestCase
from origin.views.task.task_views import TASK_CREATE_REQUIRED

TASK = "/api/v2/task/"
COMMENT = "/api/v2/task/comment/"
REACTION = "/api/v2/task/comment/reaction/"
MENTION = "/api/v2/task/comment/mention/"


class TaskRequestDataRequiredBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.authenticate()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )
        self.comment = TaskComments.objects.create(
            task=self.task, sender=self.user, comment_id=1, comment_body=[]
        )

    def post(self, url, body):
        return self.client.post(url, body, format="json")


class TaskCommentReactionRequiredTests(TaskRequestDataRequiredBase):
    def body(self, **over):
        body = {
            "team_id": str(self.team.team_id),
            "task_id": self.task.task_id,
            "comment_id": 1,
            "reaction_emoji": "x",
        }
        body.update(over)
        return {k: v for k, v in body.items() if v is not ...}

    def assertRefused(self, body, error):
        before = TaskCommentReactionFact.objects.count()
        resp = self.post(REACTION, body)
        self.assertEqual(resp.status_code, 400, resp.content)
        self.assertEqual(resp.json(), {"error": error})
        self.assertEqual(TaskCommentReactionFact.objects.count(), before)

    def test_a_valid_reaction_is_unchanged(self):
        resp = self.post(REACTION, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["reaction_emoji"], "x")
        self.assertEqual(resp.json()["comment_id"], 1)
        self.assertEqual(TaskCommentReactionFact.objects.count(), 1)

    def test_a_comment_id_sent_as_text_still_works(self):
        resp = self.post(REACTION, self.body(comment_id="1"))
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(TaskCommentReactionFact.objects.get().comment_id, 1)

    def test_an_empty_body_is_the_missing_task_it_was(self):
        before = TaskCommentReactionFact.objects.count()
        resp = self.post(REACTION, {})
        self.assertEqual(resp.status_code, 404, resp.content)
        self.assertEqual(TaskCommentReactionFact.objects.count(), before)

    def test_a_body_with_only_the_task_names_what_is_missing(self):
        self.assertRefused(
            {"task_id": self.task.task_id},
            "team_id, comment_id and reaction_emoji are required.",
        )

    def test_each_field_omitted_is_a_400_naming_it(self):
        for field in ("team_id", "comment_id", "reaction_emoji"):
            with self.subTest(field=field):
                self.assertRefused(self.body(**{field: ...}), f"{field} is required.")

    def test_a_comment_id_that_is_not_a_number_is_a_400(self):
        for value in (None, "x", "", [], {}, [1], {"a": 1}, True, 1.5):
            with self.subTest(value=value):
                self.assertRefused(self.body(comment_id=value), "comment_id must be an integer.")

    def test_a_missing_task_is_still_a_404_before_the_fields_are_asked_for(self):
        resp = self.post(REACTION, {"team_id": str(self.team.team_id)})
        self.assertEqual(resp.status_code, 404)


class TaskCreateRequiredTests(TaskRequestDataRequiredBase):
    def body(self, **over):
        body = {
            "project": self.project.project_id,
            "assignee": str(self.user.id),
            "reporter": str(self.user.id),
            "title": "New",
            "priority": "Medium",
            "effort_level": "S",
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        body.update(over)
        return {k: v for k, v in body.items() if v is not ...}

    def test_a_valid_create_is_unchanged_and_a_null_due_date_is_a_value(self):
        before = TaskMaster.objects.count()
        resp = self.post(TASK, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["task"]["title"], "New")
        self.assertIsNone(resp.json()["task"]["due_date"])
        self.assertEqual(TaskMaster.objects.count(), before + 1)

    def test_a_null_content_is_a_value_too(self):
        resp = self.post(TASK, self.body(content=None))
        self.assertEqual(resp.status_code, 201, resp.content)

    def test_an_empty_body_is_the_missing_project_it_was(self):
        before = TaskMaster.objects.count()
        resp = self.post(TASK, {})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.json(), {"error": "Project not found."})
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_each_required_field_omitted_is_a_400_naming_it(self):
        self.assertEqual(
            TASK_CREATE_REQUIRED,
            (
                "assignee",
                "reporter",
                "title",
                "priority",
                "effort_level",
                "status",
                "content",
                "due_date",
                "links",
                "tags",
                "is_init_task",
            ),
        )
        for field in TASK_CREATE_REQUIRED:
            with self.subTest(field=field):
                before = TaskMaster.objects.count()
                resp = self.post(TASK, self.body(**{field: ...}))
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), {"error": f"Missing required field: {field}"})
                self.assertEqual(TaskMaster.objects.count(), before)

    def test_only_the_project_names_the_first_field_missing(self):
        before = TaskMaster.objects.count()
        resp = self.post(TASK, {"project": self.project.project_id})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "Missing required field: assignee"})
        self.assertEqual(TaskMaster.objects.count(), before)


class TaskCommentMentionRequiredTests(TaskRequestDataRequiredBase):
    def body(self, **over):
        body = {
            "team_id": str(self.team.team_id),
            "task_id": self.task.task_id,
            "comment_id": 1,
            "mentioned_user_ids": [str(self.user2.id)],
        }
        body.update(over)
        return {k: v for k, v in body.items() if v is not ...}

    def test_a_valid_mention_is_unchanged(self):
        resp = self.post(MENTION, self.body())
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(len(resp.json()), 1)
        self.assertEqual(TaskCommentMentionFact.objects.count(), 1)

    def test_a_field_omitted_is_this_views_own_400_and_writes_nothing(self):
        for field in ("team_id", "comment_id", "mentioned_user_ids"):
            with self.subTest(field=field):
                resp = self.post(MENTION, self.body(**{field: ...}))
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), {"error": "Failed to create mentions."})
                self.assertEqual(TaskCommentMentionFact.objects.count(), 0)

    def test_no_mention_to_save_is_a_201_whatever_else_is_missing(self):
        # What it always answered: nothing is read for the person, so nothing is missing.
        resp = self.post(MENTION, self.body(mentioned_user_ids=[], team_id=...))
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json(), [])

    def test_mentions_that_are_not_a_list_are_a_400(self):
        for value in (None, 5, True):
            with self.subTest(value=value):
                resp = self.post(MENTION, self.body(mentioned_user_ids=value))
                self.assertEqual(resp.status_code, 400, resp.content)
                self.assertEqual(resp.json(), {"error": "Failed to create mentions."})

    def test_an_empty_body_is_the_missing_task_it_was(self):
        resp = self.post(MENTION, {})
        self.assertEqual(resp.status_code, 404)


class TaskCommentEditRequiredTests(TaskRequestDataRequiredBase):
    def put(self, body):
        return self.client.put(COMMENT, body, format="json")

    def test_an_edit_is_unchanged(self):
        resp = self.put({"task_id": self.task.task_id, "comment_id": 1, "comment_body": [{"a": 1}]})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["comment_body"], [{"a": 1}])
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.comment_body, [{"a": 1}])

    def test_an_edit_with_no_body_changes_nothing_and_still_answers_200(self):
        self.comment.comment_body = [{"keep": 1}]
        self.comment.save()
        resp = self.put({"task_id": self.task.task_id, "comment_id": 1})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.comment.refresh_from_db()
        self.assertEqual(self.comment.comment_body, [{"keep": 1}])

    def test_an_empty_body_is_the_400_it_was(self):
        resp = self.put({})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.json(), {"error": "task_id and comment_id are required."})
