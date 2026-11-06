"""A malformed id that reaches an ORM lookup is a 400, not a 500 (MIP-1060).

MIP-1014/1020/1029 patched views one at a time with `as_int` / `as_uuid`. The same pattern
was still left on 16 route/verb combinations (a client `team_id`, `project_id` or `id`
going into a filter raw), so `AuthenticatedAPIView.handle_exception` now turns Django's own
parse errors into a 400. It matches only those two messages: a `ValueError` from our own code
is still a 500.
"""

import json
from urllib.parse import urlencode

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db import transaction
from django.test import SimpleTestCase
from rest_framework.response import Response
from rest_framework.test import APIClient, APIRequestFactory, force_authenticate

from origin.models.note.common_note_models import NoteFolderPermission, NoteFolderTag
from origin.models.note.personal_note_models import PersonalNoteFolder
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.common.base_auth_api_view import AuthenticatedAPIView
from origin.views.utils.malformed_lookup import MALFORMED_ID_ERROR, is_malformed_lookup_value
from origin.views.utils.note_role import ROLE_OWNER


class MatcherTests(SimpleTestCase):
    def test_django_parse_errors_match(self):
        from django.db.models import IntegerField, UUIDField

        for field, value in ((IntegerField(name="x"), "abc"), (IntegerField(name="x"), ["x"])):
            with self.subTest(value=value):
                try:
                    field.get_prep_value(value)
                except (ValueError, TypeError) as exc:
                    self.assertTrue(is_malformed_lookup_value(exc), str(exc))
        try:
            UUIDField().to_python("abc")
        except ValidationError as exc:
            self.assertTrue(is_malformed_lookup_value(exc), str(exc))

    def test_anything_else_does_not_match(self):
        for exc in (
            ValueError("boom"),
            TypeError("unsupported operand"),
            ValueError("invalid literal for int() with base 10: 'abc'"),
            KeyError("x"),
            ValidationError("Enter a valid email address.", code="invalid"),
            ValidationError("“x” is not a valid UUID.", code="required"),
            AttributeError("'list' object has no attribute 'strip'"),
        ):
            with self.subTest(exc=repr(exc)):
                self.assertFalse(is_malformed_lookup_value(exc))


class _Boom(AuthenticatedAPIView):
    def get(self, request):
        raise ValueError("a real bug")


class _BadId(AuthenticatedAPIView):
    def get(self, request):
        from origin.models.project.prj_models import ProjectMaster

        ProjectMaster.objects.filter(project_id=request.GET["id"]).first()
        return Response({})


class GuardTests(BaseAPITestCase):
    def test_a_real_bug_is_still_an_error(self):
        request = APIRequestFactory().get("/x/")
        force_authenticate(request, user=self.user)
        with self.assertRaises(ValueError):
            _Boom.as_view()(request)

    def test_a_malformed_id_in_a_lookup_is_a_400(self):
        request = APIRequestFactory().get("/x/", {"id": "abc"})
        force_authenticate(request, user=self.user)
        with self.assertLogs("origin.views.common.base_auth_api_view", "WARNING"):
            response = _BadId.as_view()(request)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data, {"error": MALFORMED_ID_ERROR})


BADS = ("abc", ["x"], {"a": 1}, "", 1.5, True)

# (method, url, the id-like fields that answered 500 on main after MIP-1029)
COMBOS = [
    ("delete", "/api/v2/note/personal/folder/", ("team_id",)),
    ("delete", "/api/v2/project/custom-fields/", ("project_id",)),
    ("delete", "/api/v2/project/saved-task-filters/", ("id",)),
    ("delete", "/api/v2/task/comment/reaction/", ("comment_id", "team_id")),
    ("get", "/api/v2/note/personal/folder/", ("team_id",)),
    ("get", "/api/v2/note/team/folder/", ("team_id",)),
    ("get", "/api/v2/note/team/tag/", ("team_id",)),
    ("get", "/api/v2/project/saved-task-filters/", ("team_id",)),
    ("get", "/api/v2/project/tag/", ("team_id",)),
    ("get", "/api/v2/project/task-template/", ("team_id",)),
    ("post", "/api/v2/note/personal/folder/", ("team_id",)),
    ("post", "/api/v2/note/team/folder/", ("team_id",)),
    ("post", "/api/v2/note/team/tag/", ("team_id",)),
    ("post", "/api/v2/project/custom-fields/", ("project_id",)),
    ("put", "/api/v2/project/custom-fields/", ("project_id",)),
    ("put", "/api/v2/project/saved-task-filters/", ("id",)),
]


class SixteenRoutesTests(BaseAPITestCase):
    """The combinations MIP-1029's probe still found answering 500."""

    def setUp(self):
        super().setUp()
        self.client = APIClient(raise_request_exception=False)
        self.authenticate()
        project = ProjectMaster.objects.create(
            team=self.team, project_name="P", owner=self.user, project_system_user=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        ProjectTags.objects.create(
            team=self.team,
            project=project,
            tag_id=1,
            tag_name="t",
            tag_color="a",
            tag_text_color="b",
        )
        task = TaskMaster.objects.create(
            team=self.team, project=project, title="T", status="Open", reporter=self.user
        )
        TaskComments.objects.create(task=task, sender=self.user, comment_id=1, comment_body=[])
        pfolder = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="pf")
        tfolder = PersonalNoteFolder.objects.create(
            team=self.team,
            owner=self.user,
            name="tf",
            scope=PersonalNoteFolder.SCOPE_TEAM,
            visibility="public",
        )
        NoteFolderPermission.objects.create(
            team=self.team, folder=tfolder, user=self.user, role_id=ROLE_OWNER
        )
        ntag = NoteFolderTag.objects.create(team=self.team, name="eng", created_by=self.user)
        self.valid = {
            "project_id": project.project_id,
            "team_id": str(self.team.team_id),
            "id": 1,
            "tag_id": ntag.tag_id,
            "folder_id": pfolder.folder_id,
            "attendee_id": str(self.user2.id),
            "comment_id": 1,
            "task_id": task.task_id,
            "user_id": str(self.user.id),
            "sender_id": str(self.user.id),
            "reaction_emoji": "x",
            "name": "n",
        }

    def send(self, method, url, values, where):
        """One request in a savepoint that is rolled back: a database error in one probe
        must not poison the transaction the next one runs in."""
        cache.clear()
        with transaction.atomic():
            res = self._send(method, url, values, where)
            transaction.set_rollback(True)
        return res

    def _send(self, method, url, values, where):
        if where == "query":
            flat = {k: v for k, v in values.items() if not isinstance(v, (list, dict))}
            q = urlencode({k: json.dumps(v) if isinstance(v, bool) else v for k, v in flat.items()})
            return getattr(self.client, method)(f"{url}?{q}")
        return getattr(self.client, method)(url, values, format="json")

    def test_a_malformed_id_never_answers_500(self):
        for method, url, fields in COMBOS:
            for field in fields:
                for bad in BADS:
                    for where in ("body", "query"):
                        if where == "query" and isinstance(bad, (list, dict)):
                            continue
                        with self.subTest(
                            method=method, url=url, field=field, bad=bad, where=where
                        ):
                            res = self.send(method, url, {**self.valid, field: bad}, where)
                            self.assertLess(res.status_code, 500, res.content[:200])

    def test_deleting_a_reaction_by_a_comment_id_that_is_not_a_number_is_the_posts_400(self):
        """Not the guard's job: this was our own `int()`, so the view answers it."""
        for bad in ("abc", "1.5", "true"):
            with self.subTest(comment_id=bad):
                q = urlencode({**self.valid, "comment_id": bad})
                res = self.client.delete(f"/api/v2/task/comment/reaction/?{q}")
                self.assertEqual(res.status_code, 400, res.content)
                self.assertEqual(res.json(), {"error": "comment_id must be an integer."})
