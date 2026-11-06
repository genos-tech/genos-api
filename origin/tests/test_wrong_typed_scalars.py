"""A wrongly typed scalar is an answer, not a 500 (MIP-1029).

MIP-1014/1020 made every route answer a missing field, `{}` and `null`; what was left was a
field that is THERE but of the wrong type: text where a number is wanted, a list or an object
where an id is, `null` for a non-null column. Each reached an ORM filter or a `.strip()` raw and
answered 500 (`ValueError`, `TypeError`, a `ValidationError` for a UUID, an `IntegrityError`).

The rule, the one the views already follow: an id that is malformed names nothing, so it gets
exactly what an id that names nothing gets (the view's own 404 or 403), in the view's own
words, and writes nothing. A value that is not text where text is wanted is a 400 that names
the field. Valid requests keep their status and body.
"""

from rest_framework.test import APIClient

from origin.models.common.team_models import TeamMaster
from origin.models.note.common_note_models import (
    NoteFolderPermission,
    NoteFolderTag,
)
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import (
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
    ProjectTaskTemplate,
)
from origin.models.task.task_models import TaskCommentReactionFact, TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.mention_handler import extractMentionedUsers
from origin.views.utils.note_role import ROLE_OWNER

JOIN = "/api/v2/project/join/"
TAG = "/api/v2/project/tag/"
TEMPLATE = "/api/v2/project/task-template/"
REACTION = "/api/v2/task/comment/reaction/"
TASK = "/api/v2/task/"
PERSONAL_FOLDER = "/api/v2/note/personal/folder/"
TEAM_FOLDER = "/api/v2/note/team/folder/"
TEAM_TAG = "/api/v2/note/team/tag/"

# Not a number, not a UUID: text, a list, an object, an empty string, a float.
BAD_IDS = ("abc", ["x"], {"a": 1}, "", 1.5)
NOT_TEXT = ([1], {"a": 1}, 5, 1.5, True)


class Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()
        self.team_id = str(self.team.team_id)
        self.uid = str(self.user.id)
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)

    def call(self, method, url, body):
        return getattr(self.client, method)(url, body, format="json")

    def assertAnswer(self, res, status, error):
        self.assertEqual(res.status_code, status, res.content)
        self.assertEqual(res.json(), {"error": error})


# ── project/join/ ───────────────────────────────────────────────────────


class JoinProjectTests(Base):
    def body(self, **over):
        body = {
            "team_id": self.team_id,
            "project_id": self.project.project_id,
            "attendee_id": str(self.user2.id),
        }
        body.update(over)
        return body

    def test_a_valid_join_is_unchanged(self):
        res = self.client.post(JOIN, self.body(), format="json")
        self.assertEqual(res.status_code, 201, res.content)
        self.assertTrue(
            ProjectMembers.objects.filter(project=self.project, attendee=self.user2).exists()
        )

    def test_a_malformed_project_id_answers_what_a_project_that_is_not_there_answers(self):
        nothing = self.client.post(JOIN, self.body(project_id=987654), format="json")
        self.assertEqual(nothing.status_code, 404)
        before = ProjectMembers.objects.count()
        for bad in ("abc", ["x"], {"a": 1}, 1.5, True):
            with self.subTest(project_id=bad):
                res = self.client.post(JOIN, self.body(project_id=bad), format="json")
                self.assertEqual((res.status_code, res.json()), (404, nothing.json()))
        self.assertEqual(ProjectMembers.objects.count(), before)

    def test_a_malformed_team_id_on_a_project_the_caller_cannot_reach_is_the_same_404(self):
        """On a project the caller reaches, `align_team_to_object` swaps the request's team for
        the project's own, so a malformed one only reaches the view when it cannot."""
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        theirs = ProjectMaster.objects.create(
            team=other_team, project_name="Theirs", owner=self.user2, project_system_user=self.user2
        )
        ProjectMembers.objects.create(team=other_team, project=theirs, attendee=self.user2)
        before = ProjectMembers.objects.count()
        for bad in ("abc", ["x"], {"a": 1}, 5):
            with self.subTest(team_id=bad):
                res = self.client.post(
                    JOIN, self.body(team_id=bad, project_id=theirs.project_id), format="json"
                )
                self.assertAnswer(res, 404, "Project not found.")
        self.assertEqual(ProjectMembers.objects.count(), before)

    def test_a_malformed_attendee_id_answers_what_a_stranger_answers(self):
        stranger = self.client.post(
            JOIN, self.body(attendee_id="11111111-1111-4111-8111-111111111111"), format="json"
        )
        self.assertEqual(stranger.status_code, 403)
        before = ProjectMembers.objects.count()
        for bad in ("abc", ["x"], {"a": 1}, 1.5):
            with self.subTest(attendee_id=bad):
                res = self.client.post(JOIN, self.body(attendee_id=bad), format="json")
                self.assertEqual((res.status_code, res.json()), (403, stranger.json()))
        self.assertEqual(ProjectMembers.objects.count(), before)


# ── PUT project/tag/ ────────────────────────────────────────────────────


class ProjectTagPutTests(Base):
    def setUp(self):
        super().setUp()
        self.tag = ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="old",
            tag_color="a",
            tag_text_color="b",
        )

    def put(self, **fields):
        return self.client.put(
            TAG,
            {"project_id": self.project.project_id, "old_tag_name": "old", **fields},
            format="json",
        )

    def row(self):
        tag = ProjectTags.objects.get()
        return (tag.tag_name, tag.tag_color, tag.tag_text_color)

    def test_a_valid_edit_is_unchanged(self):
        res = self.put(tag_name="new", tag_color="c", tag_text_color="d")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.row(), ("new", "c", "d"))

    def test_null_or_a_non_text_value_is_a_400_that_names_the_field_and_writes_nothing(self):
        for field in ("tag_name", "tag_color", "tag_text_color"):
            for bad in (None, 5, True, ["x"], {"a": 1}):
                with self.subTest(field=field, value=bad):
                    res = self.put(**{field: bad})
                    self.assertAnswer(res, 400, f"{field} must be text.")
                    self.assertEqual(self.row(), ("old", "a", "b"))


# ── project/task-template/ ───────────────────────────────────────────────


class TemplateTests(Base):
    def setUp(self):
        super().setUp()
        self.template = ProjectTaskTemplate.objects.create(
            team=self.team,
            project=self.project,
            template_name="t",
            body=[{"keep": 1}],
            created_by=self.user,
        )

    def base(self, **over):
        body = {"id": self.template.id, "project_id": self.project.project_id}
        body.update(over)
        return body

    def test_a_valid_edit_is_unchanged(self):
        res = self.client.put(TEMPLATE, self.base(template_name=" t2 "), format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(ProjectTaskTemplate.objects.get().template_name, "t2")

    def test_a_malformed_id_is_the_404_of_a_template_that_is_not_there(self):
        for method in ("put", "delete"):
            for bad in ("abc", ["x"], {"a": 1}):
                with self.subTest(method=method, id=bad):
                    res = self.call(method, TEMPLATE, self.base(id=bad))
                    self.assertAnswer(res, 404, "Template not found.")
        self.assertEqual(ProjectTaskTemplate.objects.count(), 1)

    def test_a_malformed_project_id_is_the_403_of_a_project_that_is_not_theirs(self):
        for method, extra in (
            ("put", {"template_name": "x"}),
            ("delete", {}),
            ("post", {"template_name": "x", "body": []}),
        ):
            for bad in ("abc", ["x"], {"a": 1}):
                with self.subTest(method=method, project_id=bad):
                    res = self.call(method, TEMPLATE, self.base(project_id=bad, **extra))
                    self.assertAnswer(res, 403, "Not a member of this project.")
        self.assertEqual(ProjectTaskTemplate.objects.count(), 1)
        self.assertEqual(ProjectTaskTemplate.objects.get().template_name, "t")

    def test_a_name_that_is_not_text_is_a_400_on_put_and_post(self):
        for bad in NOT_TEXT:
            with self.subTest(template_name=bad):
                res = self.client.put(TEMPLATE, self.base(template_name=bad), format="json")
                self.assertAnswer(res, 400, "template_name must be text.")
                res = self.client.post(
                    TEMPLATE,
                    {"project_id": self.project.project_id, "template_name": bad, "body": []},
                    format="json",
                )
                self.assertAnswer(res, 400, "template_name must be text.")
        self.assertEqual(ProjectTaskTemplate.objects.get().template_name, "t")

    def test_an_empty_or_falsy_name_keeps_the_answers_it_had(self):
        for empty in ("", "   ", None, 0, False, []):
            with self.subTest(template_name=empty):
                res = self.client.put(TEMPLATE, self.base(template_name=empty), format="json")
                self.assertAnswer(res, 400, "template_name cannot be empty.")
                res = self.client.post(
                    TEMPLATE,
                    {"project_id": self.project.project_id, "template_name": empty, "body": []},
                    format="json",
                )
                self.assertAnswer(res, 400, "project_id, template_name and body are required.")

    def test_the_other_views_with_the_same_membership_check_answer_403_too(self):
        stranger = self.client.get(
            f"/api/v2/project/task-template/defaults/?team_id={self.team_id}&project_id=987654"
        )
        self.assertEqual(stranger.status_code, 403)
        for url in (
            "/api/v2/project/task-template/defaults/",
            "/api/v2/project/task-field-rules/",
            "/api/v2/project/saved-task-filters/",
            "/api/v2/project/custom-fields/",
        ):
            for bad in ("abc", "1.5", ""):
                with self.subTest(url=url, project_id=bad):
                    res = self.client.get(f"{url}?team_id={self.team_id}&project_id={bad}")
                    self.assertLess(res.status_code, 500, (url, bad, res.content))


# ── comment reaction + task create ────────────────────────────────────────


class ReactionAndTaskTests(Base):
    def setUp(self):
        super().setUp()
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open", reporter=self.user
        )
        TaskComments.objects.create(task=self.task, sender=self.user, comment_id=1, comment_body=[])

    def reaction(self, **over):
        body = {
            "task_id": self.task.task_id,
            "team_id": self.team_id,
            "comment_id": 1,
            "reaction_emoji": "x",
        }
        body.update(over)
        return self.client.post(REACTION, body, format="json")

    def create(self, **over):
        body = {
            "project": self.project.project_id,
            "assignee": self.uid,
            "reporter": self.uid,
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
        return self.client.post(TASK, body, format="json")

    def test_a_valid_reaction_is_unchanged(self):
        self.assertEqual(self.reaction().status_code, 201)
        self.assertEqual(TaskCommentReactionFact.objects.count(), 1)

    def test_an_empty_team_id_is_a_400_and_writes_nothing(self):
        """`align_team_to_object` swaps a non-empty team for the task's own and skips `""`,
        which `require_fields` accepts as present and the lookup then rejects as a UUID."""
        res = self.reaction(team_id="")
        self.assertAnswer(res, 400, "team_id must be a valid id.")
        self.assertEqual(TaskCommentReactionFact.objects.count(), 0)

    def test_a_valid_create_is_unchanged(self):
        before = TaskMaster.objects.count()
        res = self.create(content=[{"type": "paragraph", "content": []}])
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(TaskMaster.objects.count(), before + 1)

    def test_a_content_that_is_a_number_or_true_is_a_400_and_writes_nothing(self):
        before = TaskMaster.objects.count()
        for bad in (5, 1.5, True, False):
            with self.subTest(content=bad):
                res = self.create(content=bad)
                self.assertAnswer(res, 400, "content must be a list of blocks.")
        self.assertEqual(TaskMaster.objects.count(), before)

    def test_the_mention_walk_ignores_a_body_that_is_not_a_list(self):
        """It is shared by the task, note and agent paths: none of them may crash on it."""
        for bad in (None, 5, 1.5, True, "text", {"a": 1}):
            with self.subTest(body=bad):
                walk = extractMentionedUsers()
                walk.extract(bad)
                self.assertEqual(walk.mentioned_user_ids, set())
        walk = extractMentionedUsers()
        walk.extract(
            [
                {
                    "content": [{"type": "mention", "props": {"userId": "u1"}}],
                    "children": [{"content": [{"type": "mentionGroup", "props": {"groupId": 7}}]}],
                }
            ]
        )
        self.assertEqual((walk.mentioned_user_ids, walk.mentioned_group_ids), ({"u1"}, {"7"}))


# ── note folders and tags ─────────────────────────────────────────────────


class NoteFolderTests(Base):
    def setUp(self):
        super().setUp()
        self.pfolder = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="pf")
        self.tfolder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="tf",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="public",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=self.tfolder, user=self.user, role_id=ROLE_OWNER
        )
        self.tag = NoteFolderTag.objects.create(team=self.team, name="eng", created_by=self.user)

    def names(self):
        return (
            PersonalNoteFolder.objects.get(pk=self.pfolder.pk).name,
            PersonalNoteFolder.objects.get(pk=self.tfolder.pk).name,
            NoteFolderTag.objects.get(pk=self.tag.pk).name,
        )

    def test_valid_renames_are_unchanged(self):
        res = self.client.put(
            PERSONAL_FOLDER,
            {
                "team_id": self.team_id,
                "user_id": self.uid,
                "folder_id": self.pfolder.folder_id,
                "name": "pf2",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        res = self.client.put(
            TEAM_TAG,
            {"team_id": self.team_id, "tag_id": self.tag.tag_id, "name": "eng2"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(self.names(), ("pf2", "tf", "eng2"))

    def test_a_personal_folder_with_a_malformed_id_or_team_is_the_404_of_a_folder_not_there(self):
        good = {"user_id": self.uid, "name": "zzz"}
        for field in ("folder_id", "team_id"):
            for bad in BAD_IDS:
                with self.subTest(field=field, value=bad):
                    body = {
                        "team_id": self.team_id,
                        "folder_id": self.pfolder.folder_id,
                        **good,
                        field: bad,
                    }
                    res = self.client.put(PERSONAL_FOLDER, body, format="json")
                    self.assertAnswer(res, 404, "Folder not found.")
        self.assertEqual(self.names(), ("pf", "tf", "eng"))

    def test_a_team_folder_with_a_team_that_is_not_a_uuid_is_the_404_of_a_folder_not_there(self):
        """`align_team_to_object` swaps a non-empty team for the folder's own when the caller
        reaches the folder, so `""` (which it skips) and a folder it cannot reach are the cases."""
        other_team = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=self.user2
        )
        foreign = PersonalNoteFolder.objects.create(
            team=other_team,
            owner=self.user2,
            name="foreign",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="public",
        )
        cases = [("", self.tfolder)] + [(bad, foreign) for bad in BAD_IDS]
        for bad, folder in cases:
            with self.subTest(team_id=bad, folder=folder.name):
                res = self.client.put(
                    TEAM_FOLDER,
                    {
                        "team_id": bad,
                        "user_id": self.uid,
                        "folder_id": folder.folder_id,
                        "name": "zzz",
                    },
                    format="json",
                )
                self.assertAnswer(res, 404, "Folder not found.")
        self.assertEqual(self.names(), ("pf", "tf", "eng"))
        self.assertEqual(PersonalNoteFolder.objects.get(pk=foreign.pk).name, "foreign")

    def test_a_tag_with_a_malformed_id_or_team_is_the_404_of_a_tag_not_there(self):
        for field in ("tag_id", "team_id"):
            for bad in BAD_IDS:
                with self.subTest(field=field, value=bad):
                    body = {"team_id": self.team_id, "tag_id": self.tag.tag_id, "name": "zzz"}
                    body[field] = bad
                    res = self.client.put(TEAM_TAG, body, format="json")
                    self.assertAnswer(res, 404, "Tag not found.")
        self.assertEqual(self.names(), ("pf", "tf", "eng"))

    def test_deleting_a_tag_by_a_malformed_id_is_the_same_404(self):
        for bad in ("abc", "", "1.5"):
            with self.subTest(tag_id=bad):
                res = self.client.delete(f"{TEAM_TAG}?team_id={self.team_id}&tag_id={bad}")
                self.assertAnswer(res, 404, "Tag not found.")
        self.assertTrue(NoteFolderTag.objects.filter(pk=self.tag.pk).exists())
