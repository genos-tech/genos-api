"""A soft-deleted task is refused like a missing one (MIP-1572).

`can_access_task` read `TaskMaster` with no `is_deleted` check, so every endpoint behind
it treated a task that was deleted as live: a member who knew the id could still read it,
comment on it, attach to it or edit it. The guard now refuses a soft-deleted row unless
the call site says `include_deleted=True`; one site does, on purpose (the task-to-milestone
conversion, left to its own task). The attachment delete was the other until MIP-1582 decided it:
a deleted task answers there as a missing one.

What the tests pin, per endpoint:

  * a deleted task answers EXACTLY as a task that never existed (status and body), so
    the refusal is no existence oracle for ids that once were;
  * a live task still does not answer that way (the gate refuses nothing it did not);
  * the write endpoints change nothing on the deleted row.

The guard itself: same query count for a live task, and a pin on every remaining
`can_access_task(` caller so a new one has to say which it means.
"""

import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from origin.models.common.user_models import ConnectedAccount, GithubWebhookRegistration
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.tests.test_base import BaseAPITestCase
from origin.views.utils.scope_guards import can_access_task

MISSING = 987_654_321
_MEDIA_ROOT = tempfile.mkdtemp(prefix="test-deleted-task-")


class DeletedTaskBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="DelTaskProj",
            owner=self.user,
            project_system_user=self.user,
            code="DEL",
        )
        # The caller is a plain project member: not the owner, not the assignee or reporter,
        # so membership is the only thing that lets them in while the task is live.
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        self.authenticate(self.user2)

    def task(self, *, deleted=False, **extra):
        fields = {
            "team": self.team,
            "project": self.project,
            "title": "T",
            "status": "Open",
            "assignee": self.user,
            "reporter": self.user,
        }
        fields.update(extra)
        row = TaskMaster.objects.create(**fields)
        if deleted:
            TaskMaster.objects.filter(task_id=row.task_id).update(is_deleted=True)
            row.refresh_from_db()
        return row


def _answer(res):
    data = res.data if hasattr(res, "data") else None
    return res.status_code, data


@override_settings(MEDIA_ROOT=_MEDIA_ROOT)
class TestEndpointsRefuseADeletedTask(DeletedTaskBase):
    """Each call is `fn(task_id) -> response`; the id is a live, a deleted or a missing one."""

    def calls(self):
        c, team = self.client, str(self.team.team_id)
        proj = self.project.project_id
        other = self.task()  # a live task, for the calls that name two
        create_payload = {
            "project": proj,
            "team": team,
            "assignee": str(self.user.id),
            "reporter": str(self.user.id),
            "title": "child",
            "priority": "Medium",
            "effort_level": "Small",
            "status": "Open",
            "content": [],
            "due_date": None,
            "links": [],
            "tags": [],
            "is_init_task": False,
        }
        return {
            "task PUT": lambda t: c.put(
                "/api/v2/task/", {"task_id": t, "title": "x"}, format="json"
            ),
            "task DELETE (hard delete)": lambda t: c.delete(
                f"/api/v2/task/?team_id={team}&task_id={t}&is_init_task_boolean=0"
            ),
            "task POST under it as parent": lambda t: c.post(
                "/api/v2/task/", {**create_payload, "parent_task_id": t}, format="json"
            ),
            "getTask": lambda t: c.get(
                f"/api/v2/task/getTask/?team_id={team}&project_id={proj}&task_id={t}"
            ),
            "attachment GET": lambda t: c.get(f"/api/v2/task/attachment/?task_id={t}"),
            "attachment DELETE": lambda t: c.delete(
                f"/api/v2/task/attachment/?task={t}&attachment_id=1"
            ),
            "attachment POST": lambda t: c.post(
                "/api/v2/task/attachment/", {"task": t, "attachment_id": "-1"}, format="multipart"
            ),
            "body attachment POST": lambda t: c.post(
                "/api/v2/task/body/attachment/",
                {
                    "task_id": t,
                    "uploader": str(self.user2.id),
                    "body_attachment_file": SimpleUploadedFile(
                        "p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 32, content_type="image/png"
                    ),
                },
                format="multipart",
            ),
            "comment POST": lambda t: c.post(
                "/api/v2/task/comment/",
                {"task_id": t, "sender_id": str(self.user2.id), "comment_body": []},
                format="json",
            ),
            "comment PUT": lambda t: c.put(
                "/api/v2/task/comment/",
                {"task_id": t, "comment_id": 1, "comment_body": []},
                format="json",
            ),
            "comment DELETE": lambda t: c.delete(f"/api/v2/task/comment/?task_id={t}&comment_id=1"),
            "comment GET": lambda t: c.get(
                f"/api/v2/task/comment/?task_id={t}&user_id={self.user2.id}"
            ),
            "reaction POST": lambda t: c.post(
                "/api/v2/task/comment/reaction/",
                {
                    "task_id": t,
                    "team_id": team,
                    "comment_id": 1,
                    "reaction_emoji": "👍",
                    "sender_id": str(self.user2.id),
                },
                format="json",
            ),
            "reaction DELETE": lambda t: c.delete(
                "/api/v2/task/comment/reaction/"
                f"?team_id={team}&sender_id={self.user2.id}&task_id={t}&comment_id=1&reaction_emoji=%F0%9F%91%8D"
            ),
            "mention POST": lambda t: c.post(
                "/api/v2/task/comment/mention/",
                {"task_id": t, "team_id": team, "comment_id": 1, "mentioned_user_ids": []},
                format="json",
            ),
            "mention GET": lambda t: c.get(
                f"/api/v2/task/comment/mention/?team_id={team}&task_id={t}&comment_id=1"
            ),
            "mention DELETE": lambda t: c.delete(
                "/api/v2/task/comment/mention/"
                f"?team_id={team}&task_id={t}&comment_id=1&mentioned_user_ids={self.user.id}"
            ),
            "dependency GET": lambda t: c.get(f"/api/v2/task/dependency/list/?task_id={t}"),
            "dependency POST as blocker": lambda t: c.post(
                "/api/v2/task/dependency/",
                {"blocker_task_id": t, "blocked_task_id": other.task_id},
                format="json",
            ),
            "dependency POST as blocked": lambda t: c.post(
                "/api/v2/task/dependency/",
                {"blocker_task_id": other.task_id, "blocked_task_id": t},
                format="json",
            ),
            "activity list": lambda t: c.get(f"/api/v2/task/activity/?team_id={team}&task_id={t}"),
            "activity restore-body": lambda t: c.post(
                "/api/v2/task/activity/restore-body/",
                {"task_id": t, "activity_id": 1},
                format="json",
            ),
            "deliveries": lambda t: c.get(f"/api/v2/task/deliveries/?team_id={team}&task_id={t}"),
            "github branches": lambda t: c.get(f"/api/v2/github/branches/for-task/?task_id={t}"),
            "github pulls": lambda t: c.get(f"/api/v2/github/pulls/for-task/?task_id={t}"),
            "task note list": lambda t: c.get(
                f"/api/v2/note/task/?team_id={team}&project_id={proj}&task_id={t}"
            ),
        }

    def test_a_deleted_task_answers_exactly_as_a_missing_one(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                missing = _answer(call(MISSING))
                deleted = _answer(call(self.task(deleted=True).task_id))
                self.assertEqual(deleted, missing)
                self.assertEqual(missing[0], 404 if name != "task POST under it as parent" else 400)

    def test_a_live_task_is_not_refused_that_way(self):
        for name, call in self.calls().items():
            with self.subTest(name):
                missing = _answer(call(MISSING))
                live = _answer(call(self.task().task_id))
                self.assertNotEqual(live, missing)

    def test_a_stranger_still_gets_the_same_answer_for_a_deleted_task(self):
        """Neither deleted nor live, for someone with no part in the task: the refusal that was
        there before. (The save path answers a MISSING id differently from a refused one, as it
        did before this change.)"""
        stranger = self.user.__class__.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        self.authenticate(stranger)
        for name, call in self.calls().items():
            if name in ("task PUT", "task POST under it as parent"):
                continue
            with self.subTest(name):
                self.assertEqual(
                    _answer(call(self.task(deleted=True).task_id)),
                    _answer(call(self.task().task_id)),
                )

    def test_the_writes_leave_the_deleted_task_as_it_was(self):
        gone = self.task(deleted=True, title="before")
        self.client.put("/api/v2/task/", {"task_id": gone.task_id, "title": "after"}, format="json")
        self.client.post(
            "/api/v2/task/comment/",
            {"task_id": gone.task_id, "sender_id": str(self.user2.id), "comment_body": []},
            format="json",
        )
        self.client.delete(
            f"/api/v2/task/?team_id={self.team.team_id}&task_id={gone.task_id}&is_init_task_boolean=0"
        )
        gone.refresh_from_db()  # the hard delete did not remove it either
        self.assertEqual(gone.title, "before")
        self.assertFalse(TaskComments.objects.filter(task=gone.task_id).exists())


class TestRestoreOfTheDeletedStateIsNotBlocked(DeletedTaskBase):
    """Nothing on a deleted task's endpoints can bring it back, and the gate must not stand in
    the way of what does: the delete that made it, and the sites that name `include_deleted`."""

    def test_the_save_still_cannot_undelete(self):
        gone = self.task(deleted=True)
        res = self.client.put(
            "/api/v2/task/", {"task_id": gone.task_id, "is_deleted": False}, format="json"
        )
        self.assertEqual(res.status_code, 404)
        gone.refresh_from_db()
        self.assertTrue(gone.is_deleted)

    def test_the_attachment_delete_refuses_a_deleted_task_as_a_missing_one(self):
        """MIP-1582: it used to opt out (`include_deleted=True`) and answer about the attachment
        ("Attachment not found.") and not about the task. A file of a deleted task is left where it
        is, as the agent's tool leaves it."""
        gone = self.task(deleted=True)
        res = self.client.delete(f"/api/v2/task/attachment/?task={gone.task_id}&attachment_id=1")
        self.assertEqual(_answer(res), (404, {"error": "Task not found."}))

    def test_the_milestone_conversion_keeps_its_own_answer_for_a_deleted_task(self):
        """MIP-1583 owns it: `convert_task_to_milestone` refuses with its `task_deleted` code."""
        gone = self.task(deleted=True)
        project_owner = self.user
        self.authenticate(project_owner)
        res = self.client.post(
            "/api/v2/task/convert-to-milestone/", {"task_id": gone.task_id}, format="json"
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data.get("code"), "task_deleted")


class TestBatchEndpointsDropADeletedTask(DeletedTaskBase):
    def test_dependency_batch_list_keeps_a_deleted_task_empty(self):
        live, gone, third = self.task(), self.task(deleted=True), self.task()
        TaskDependency.objects.create(
            blocker_task=gone, blocked_task=third, team=self.team, created_by=self.user
        )
        res = self.client.get(
            f"/api/v2/task/dependency/list-for-tasks/?task_ids={live.task_id},{gone.task_id}"
        )
        self.assertEqual(res.status_code, 200)
        by_task = res.data["dependencies_by_task"]
        self.assertEqual(by_task[str(gone.task_id)], {"blocking": [], "blockedBy": []})
        # and the other direction: the deleted task as the blocked end of an edge
        TaskDependency.objects.create(
            blocker_task=third, blocked_task=gone, team=self.team, created_by=self.user
        )
        res = self.client.get(f"/api/v2/task/dependency/list-for-tasks/?task_ids={gone.task_id}")
        self.assertEqual(
            res.data["dependencies_by_task"][str(gone.task_id)], {"blocking": [], "blockedBy": []}
        )

    def test_the_dependency_delete_of_an_edge_to_a_deleted_task_is_not_found(self):
        gone, live = self.task(deleted=True), self.task()
        dep = TaskDependency.objects.create(
            blocker_task=gone, blocked_task=live, team=self.team, created_by=self.user
        )
        res = self.client.delete(f"/api/v2/task/dependency/{dep.pk}/")
        self.assertEqual(_answer(res), (404, {"error": "Dependency not found."}))
        self.assertTrue(TaskDependency.objects.filter(pk=dep.pk).exists())
        # ... and one between two live tasks still goes.
        keep = TaskDependency.objects.create(
            blocker_task=live, blocked_task=self.task(), team=self.team, created_by=self.user
        )
        self.assertEqual(self.client.delete(f"/api/v2/task/dependency/{keep.pk}/").status_code, 204)

    def test_the_same_edge_seen_from_its_other_end_is_not_found_too(self):
        gone, live = self.task(deleted=True), self.task()
        dep = TaskDependency.objects.create(
            blocker_task=live, blocked_task=gone, team=self.team, created_by=self.user
        )
        res = self.client.delete(f"/api/v2/task/dependency/{dep.pk}/")
        self.assertEqual(_answer(res), (404, {"error": "Dependency not found."}))
        self.assertTrue(TaskDependency.objects.filter(pk=dep.pk).exists())

    def test_burndown_and_velocity_drop_it(self):
        live, gone = self.task(), self.task(deleted=True)
        res = self.client.get(
            f"/api/v2/task/burndown/?task_ids={gone.task_id}&start=2026-11-01&end=2026-11-10"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["total"], 0)
        res = self.client.get(
            f"/api/v2/task/burndown/?task_ids={live.task_id}&start=2026-11-01&end=2026-11-10"
        )
        self.assertEqual(res.data["total"], 1)
        today = timezone.now().date()
        window = f"start={today - timedelta(days=2)}&end={today}"

        def velocity(task):
            res = self.client.get(
                f"/api/v2/task/velocity/?team_id={self.team.team_id}&task_ids={task.task_id}&{window}"
            )
            self.assertEqual(res.status_code, 200)
            return sum(row["created"] + row["updated"] for row in res.data["velocity"])

        self.assertGreater(velocity(live), 0)  # creating the task wrote its history row
        self.assertEqual(velocity(gone), 0)


class TestGithubBatchDropsADeletedTask(DeletedTaskBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.enterContext(
            patch("origin.views.common.github_views.get_valid_access_token", return_value="tok")
        )
        ConnectedAccount.objects.create(
            user=self.user2,
            provider="github",
            provider_user_id="123",
            scopes=["repo"],
            access_token_encrypted="placeholder",
        )
        GithubWebhookRegistration.objects.create(
            owner="acme", repo="rocket", hook_id=1, registered_by=self.user
        )

    def tearDown(self):
        cache.clear()

    def test_pulls_for_tasks_answers_nothing_for_a_deleted_task(self):
        live, gone = self.task(), self.task(deleted=True)

        def fake(_account, path, params=None):
            resp = MagicMock(spec=requests.Response)
            resp.ok, resp.status_code = True, 200
            if path.endswith("/branches"):
                resp.json.return_value = [
                    {"name": f"feature/{t.display_id}-x", "commit": {"sha": "s"}}
                    for t in (live, gone)
                ]
            else:
                head = (params or {}).get("head", "")
                resp.json.return_value = (
                    [
                        {
                            "number": 7,
                            "html_url": "https://github.com/acme/rocket/pull/7",
                            "title": "t",
                            "state": "open",
                            "draft": False,
                            "merged_at": None,
                        }
                    ]
                    if head.startswith("acme:feature/")
                    else []
                )
            return resp

        with patch("origin.views.common.github_views._github_get", side_effect=fake):
            res = self.client.get(
                f"/api/v2/github/pulls/for-tasks/?task_ids={live.task_id},{gone.task_id}"
            )
        self.assertEqual(res.status_code, 200)
        by_task = res.json()["pulls_by_task"]
        self.assertEqual(len(by_task[str(live.task_id)]), 1)
        self.assertEqual(by_task[str(gone.task_id)], [])


class TestMediaOfADeletedTask(DeletedTaskBase):
    def test_attachments_and_body_files_of_a_deleted_task_are_not_readable(self):
        from origin.views.common.media_views import _may_read_media

        live, gone = self.task(), self.task(deleted=True)
        for prefix in ("task_attachments", "tasks"):
            with self.subTest(prefix):
                self.assertTrue(_may_read_media(f"{prefix}/{live.task_id}/f.png", self.user2.id))
                self.assertFalse(_may_read_media(f"{prefix}/{gone.task_id}/f.png", self.user2.id))
                self.assertFalse(_may_read_media(f"{prefix}/{MISSING}/f.png", self.user2.id))


class TestTheGuard(DeletedTaskBase):
    def test_the_default_refuses_a_deleted_task_to_everyone_who_could_read_it_live(self):
        gone = self.task(deleted=True, assignee=self.user2, reporter=self.user2)
        for who in (self.user, self.user2):  # owner (project owner), assignee and reporter
            self.assertFalse(can_access_task(gone.task_id, who.id))
        self.assertTrue(can_access_task(gone.task_id, self.user2.id, include_deleted=True))

    def test_a_live_task_is_read_by_the_same_people_and_by_the_same_number_of_queries(self):
        live = self.task()
        outsider = self.user.__class__.objects.create_user(
            username="out", email="out@example.com", password="pw"
        )
        for who, expected in ((self.user.id, True), (self.user2.id, True), (outsider.id, False)):
            with CaptureQueriesContext(connection) as default:
                got = can_access_task(live.task_id, who)
            with CaptureQueriesContext(connection) as kept:
                kept_got = can_access_task(live.task_id, who, include_deleted=True)
            self.assertEqual((got, kept_got), (expected, expected))
            self.assertEqual(len(default), len(kept))

    def test_a_missing_or_malformed_id_is_still_a_plain_no(self):
        for raw in (None, MISSING, "abc", "", "1;2"):
            self.assertFalse(can_access_task(raw, self.user2.id))
            self.assertFalse(can_access_task(raw, self.user2.id, include_deleted=True))

    def test_every_caller_that_keeps_a_deleted_task_says_so_and_is_listed_here(self):
        """The default is the safe one; a caller that opts out names `include_deleted=True`
        at its call site. Add to this list only with the reason next to the call."""
        root = Path(__file__).resolve().parents[2]
        found = subprocess.run(
            [
                "grep",
                "-rn",
                "include_deleted=True",
                "origin",
                "--include=*.py",
                "--exclude-dir=tests",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        files = sorted(
            {
                line.split(":", 1)[0]
                for line in found
                if not line.startswith("origin/views/utils/scope_guards.py")  # its own docstring
            }
        )
        self.assertEqual(
            files,
            # force_delete_views: a deleted (not yet purged) task can still be force-deleted
            # (MIP-1584), by its creator or the project owner.
            ["origin/views/force_delete_views.py", "origin/views/task/conversion_views.py"],
            found,
        )
