"""MIP-1837: import a Jira project from its JSON (REST search response) export.

What these pin: the file is parsed as text only (ADF bodies become markdown, unsafe link schemes
and mentions never become links or chips); the same preview, commit, undo and guards as the CSV
import apply; comments are kept with their author as text and make no notification; attachments
become links in the body (nothing is downloaded); status history becomes one comment; a re-import
creates nothing; an undo removes tasks that only carry imported comments and keeps one a person
commented on or edited.
"""

import json
from unittest import mock

from django.utils import timezone

from origin.models.project.prj_models import ProjectMembers
from origin.models.task.task_import_models import TaskImportRow, TaskImportRun
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.services import task_import, task_import_jira
from origin.tests.test_task_import import COMMIT, PREVIEW, TaskImportApiCase, raw

FIXTURE = "jira_search.json"
PATCH_CACHE = "origin.services.task_import.invalidate_project_tasks_cache"
UNDO = "/api/v2/task/import/runs/{}/undo/"


def issues(**fields_by_key):
    return json.dumps(
        {"issues": [{"key": k, "id": k, "fields": f} for k, f in fields_by_key.items()]}
    )


class JiraParseTests(TaskImportApiCase):
    def test_headers_make_the_jira_profile_and_map_the_fields(self):
        headers, rows, extras = task_import_jira.parse_jira_json(raw(FIXTURE))
        self.assertEqual(task_import.detect_profile(headers), "jira")
        mapping = task_import.propose_mapping(headers, "jira")
        self.assertEqual(headers[mapping["title"][0]], "Summary")
        self.assertEqual(headers[mapping["external_id"][0]], "Issue key")
        self.assertEqual(headers[mapping["parent"][0]], "Parent key")
        self.assertEqual(len(rows), 3)
        self.assertEqual(len(extras), 3)
        self.assertEqual(len(extras[0]["comments"]), 3)  # 2 comments + the status history

    def test_adf_markdown_drops_unsafe_links_and_keeps_mentions_as_text(self):
        data = json.loads(raw(FIXTURE))
        adf = data["issues"][0]["fields"]["description"]
        md = task_import_jira.adf_to_markdown(adf)
        self.assertIn("**launch**", md)
        self.assertIn("[the plan](https://example.com/plan)", md)
        self.assertIn("@Sam Example", md)
        self.assertNotIn("javascript:", md)
        self.assertIn("- first", md)
        self.assertIn("bad link", md)

    def test_v2_string_description_and_bare_list_and_page_list(self):
        one = {"key": "A-1", "id": "1", "fields": {"summary": "S", "description": "plain text"}}
        for payload in ([one], [{"issues": [one]}, {"issues": [one]}], {"issues": [one]}):
            _, rows, _ = task_import_jira.parse_jira_json(json.dumps(payload).encode())
            self.assertIn("plain text", rows[0][7])

    def test_bad_deep_and_oversized_json_are_refused_cleanly(self):
        for blob in (b"{not json", b"[]", b'{"issues": 3}', b'{"x": 1}'):
            with self.assertRaises(task_import.ImportError_) as ctx:
                task_import_jira.parse_jira_json(blob)
            self.assertIn(ctx.exception.code, ("bad_json", "empty_file"))
        deep = {"type": "doc", "content": []}
        cur = deep
        for _ in range(200):
            nxt = {"type": "blockquote", "content": []}
            cur["content"].append(nxt)
            cur = nxt
        cur["content"].append({"type": "text", "text": "deep"})
        md = task_import_jira.adf_to_markdown(deep)  # capped, never a RecursionError
        self.assertIsInstance(md, str)
        many = json.dumps(
            {
                "issues": [
                    {"key": f"A-{i}", "fields": {"summary": "s"}}
                    for i in range(task_import.MAX_ROWS + 1)
                ]
            }
        )
        with self.assertRaises(task_import.ImportError_) as ctx:
            task_import_jira.parse_jira_json(many.encode())
        self.assertEqual(ctx.exception.code, "too_many_rows")

    def test_comment_caps(self):
        many = [
            {"author": {"displayName": "a"}, "created": "2026-01-01", "body": "x" * 20000}
        ] * 300
        _, _, extras = task_import_jira.parse_jira_json(
            issues(**{"A-1": {"summary": "s", "comment": {"comments": many}}}).encode()
        )
        comments = extras[0]["comments"]
        self.assertEqual(len(comments), task_import_jira.MAX_COMMENTS_PER_ISSUE)
        self.assertLessEqual(len(comments[0]["markdown"]), task_import_jira.COMMENT_MAX_CHARS)


class JiraPreviewCommitTests(TaskImportApiCase):
    def post_json(self, url, **kw):
        return self.post(url, FIXTURE, **kw)

    def test_preview_writes_nothing_and_shows_comment_counts(self):
        resp = self.post_json(PREVIEW)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["profile"], "jira_json")
        self.assertEqual(TaskMaster.objects.filter(project=self.project).count(), 0)
        self.assertEqual(TaskComments.objects.count(), 0)
        counts = [r["comment_count"] for r in resp.data["rows"]]
        self.assertEqual(counts, [3, 0, 1])
        self.assertNotIn("_comments", resp.data["rows"][0])
        values = {v["value"] for v in resp.data["status_values"]}
        self.assertEqual(values, {"In Progress", "To Do", "Done"})

    def test_commit_creates_tasks_parents_comments_and_attachment_links(self):
        with mock.patch(PATCH_CACHE):
            resp = self.post_json(COMMIT, data={"create_tags": "true"})
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["created"], 3)
        self.assertEqual(resp.data["profile"], "jira_json")
        epic = TaskMaster.objects.get(title="Launch epic")
        story = TaskMaster.objects.get(title="Write the announcement")
        sub = TaskMaster.objects.get(title="Proofread")
        self.assertEqual(story.parent_task_id, epic.task_id)
        self.assertEqual(sub.parent_task_id, story.task_id)
        self.assertEqual(epic.status, "WIP")
        self.assertEqual(sub.status, "Closed")
        body = json.dumps(epic.content)
        self.assertIn("plan.pdf", body)
        self.assertIn("https://example.atlassian.net/secure/attachment/1/plan.pdf", body)
        self.assertNotIn("javascript:", body)
        comments = list(TaskComments.objects.filter(task=epic).order_by("comment_id"))
        self.assertEqual(len(comments), 3)
        first = json.dumps(comments[1].comment_body)
        self.assertIn("Dev One", first)
        self.assertIn("Kicked off.", first)
        self.assertIn("Jira", first)
        self.assertIn("To Do", json.dumps(comments[0].comment_body))  # the status history
        self.assertEqual(comments[0].sender_id, self.user.id)
        created = [e for e in resp.data["report"] if e["outcome"] == "created"]
        self.assertEqual(created[0]["comments"], 3)

    def test_comments_send_no_notification_or_webhook(self):
        with (
            mock.patch(PATCH_CACHE),
            mock.patch(
                "origin.services.task_comment_fanout.fan_out_task_comment", create=True
            ) as fan,
        ):
            self.post_json(COMMIT)
        fan.assert_not_called()

    def test_reimport_creates_nothing_even_after_the_csv_of_the_same_keys(self):
        with mock.patch(PATCH_CACHE):
            self.post_json(COMMIT)
            again = self.post_json(COMMIT)
        self.assertEqual(again.data["created"], 0)
        self.assertEqual(again.data["skipped"], 3)
        self.assertEqual(TaskComments.objects.count(), 4)
        self.assertEqual(self.count(), 3)

    def test_csv_import_of_the_same_issue_key_is_skipped_for_json(self):
        csv = b"Summary,Issue key,Status\nLaunch epic,DEMO-1,To Do\n"
        with mock.patch(PATCH_CACHE):
            self.post(COMMIT, "a.csv", content=csv)
            resp = self.post_json(COMMIT)
        self.assertEqual(resp.data["created"], 2)
        self.assertEqual(resp.data["skipped"], 1)

    def test_a_bad_json_file_is_a_400(self):
        resp = self.post(PREVIEW, "x.json", content=b"{nope")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.data["code"], "bad_json")
        self.assertEqual(TaskMaster.objects.filter(project=self.project).count(), 0)

    def test_guards_hold_for_json(self):
        ProjectMembers.objects.filter(attendee=self.dev).update(member_role="viewer")
        self.authenticate(self.dev)
        self.assertEqual(self.post_json(COMMIT).status_code, 403)

    def test_a_failing_comment_fails_only_its_row(self):
        real = TaskComments.objects.create
        calls = {"n": 0}

        def boom(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("x")
            return real(*a, **kw)

        with mock.patch(PATCH_CACHE), mock.patch.object(TaskComments.objects, "create", boom):
            resp = self.post_json(COMMIT)
        self.assertEqual(resp.data["failed"], 1)
        self.assertEqual(resp.data["created"], 2)


class JiraUndoTests(TaskImportApiCase):
    def setUp(self):
        super().setUp()
        with mock.patch(PATCH_CACHE):
            self.post(COMMIT, FIXTURE)
        self.run_obj = TaskImportRun.objects.get()
        self.url = UNDO.format(self.run_obj.run_id)

    def test_undo_removes_tasks_that_only_have_imported_comments(self):
        self.assertEqual(self.client.get(self.url).data["will_remove"], 3)
        resp = self.client.post(self.url)
        self.assertEqual(resp.data["removed"], 3)
        self.assertEqual(TaskImportRow.objects.count(), 0)

    def test_undo_keeps_a_task_someone_commented_on(self):
        story = TaskMaster.objects.get(title="Write the announcement")
        sub = TaskMaster.objects.get(title="Proofread")
        TaskComments.objects.create(
            task=sub, sender=self.user, comment_id=2, comment_body=[{"type": "paragraph"}]
        )
        plan = self.client.get(self.url).data
        # the sub-task is kept (a new comment) and so are its parents, which now have a child that stays
        self.assertEqual(plan["will_remove"], 0)
        self.assertEqual(plan["kept"], 3)
        self.assertEqual(story.parent_task_id, TaskMaster.objects.get(title="Launch epic").task_id)

    def test_undo_keeps_a_task_whose_imported_comment_was_edited(self):
        sub = TaskMaster.objects.get(title="Proofread")
        TaskComments.objects.filter(task=sub).update(
            ts_updated_at=timezone.now() + timezone.timedelta(minutes=5)
        )
        self.assertGreaterEqual(self.client.get(self.url).data["kept"], 1)
        self.assertEqual(self.client.post(self.url).data["removed"], 0)
