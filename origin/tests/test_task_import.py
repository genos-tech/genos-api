"""MIP-1835: import tasks from a CSV file (generic, Jira export, Notion database export).

What these pin: the file is decoded (UTF-8 with BOM, Shift-JIS); the profile is detected from the
headers; Jira's repeated columns are merged; status, priority and dates are mapped with a warning
when a value is unknown or ambiguous; a preview writes nothing; a failing row is reported while
the others import; the same file imported twice creates nothing the second time; an undo removes
what the import made and keeps what a person edited since; the guards (member, role, archived,
size, rows, quota) hold.
"""

from pathlib import Path
from unittest import mock

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework import status

from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_import_models import TaskImportRow, TaskImportRun
from origin.models.task.task_models import TaskMaster
from origin.services import task_import
from origin.tests.test_base import BaseAPITestCase

FIXTURES = Path(__file__).parent / "fixtures" / "task_import"
PREVIEW = "/api/v2/task/import/preview/"
COMMIT = "/api/v2/task/import/"


def raw(name):
    return (FIXTURES / name).read_bytes()


class TaskImportPureTests(BaseAPITestCase):
    def test_decode_utf8_bom_and_shift_jis(self):
        self.assertTrue(task_import.decode(raw("jira_export.csv")).startswith("Summary,"))
        self.assertIn("日本語のタスク", task_import.decode(raw("generic_sjis.csv")))

    def test_profile_detection(self):
        def headers(name):
            return task_import.parse(task_import.decode(raw(name)))[0]

        self.assertEqual(task_import.detect_profile(headers("jira_export.csv")), "jira")
        self.assertEqual(task_import.detect_profile(headers("notion_export.csv")), "notion")
        self.assertEqual(task_import.detect_profile(headers("generic.csv")), "generic")

    def test_jira_repeated_labels_are_both_mapped(self):
        headers, _ = task_import.parse(task_import.decode(raw("jira_export.csv")))
        mapping = task_import.propose_mapping(headers, "jira")
        self.assertEqual(len(mapping["tags"]), 2)
        self.assertEqual(headers[mapping["title"][0]], "Summary")
        self.assertEqual(headers[mapping["external_id"][0]], "Issue key")

    def test_status_priority_and_dates(self):
        self.assertEqual(task_import.map_status("To Do"), ("Open", None))
        self.assertEqual(task_import.map_status("In Progress")[0], "WIP")
        self.assertEqual(task_import.map_status("完了")[0], "Closed")
        value, warning = task_import.map_status("Weird Status")
        self.assertEqual(value, "Open")
        self.assertIsNotNone(warning)
        self.assertEqual(task_import.map_priority("Highest")[0], "Critical")
        self.assertEqual(task_import.map_priority("Medium")[0], "Normal")
        self.assertIsNone(task_import.map_priority("Mystery")[0])
        d = task_import.parse_date
        self.assertEqual(str(d("21/Nov/26 9:15 AM")[0]), "2026-11-21")
        self.assertEqual(str(d("November 30, 2026")[0]), "2026-11-30")
        self.assertEqual(str(d("2026/12/02")[0]), "2026-12-02")
        self.assertEqual(str(d("2026年12月5日")[0]), "2026-12-05")
        self.assertEqual(str(d("25/12/2026")[0]), "2026-12-25")
        value, warning = d("03/12/2026")  # day-first or month-first: not guessed
        self.assertIsNone(value)
        self.assertIsNotNone(warning)

    def test_title_formula_prefix_is_stripped(self):
        self.assertEqual(task_import.clean_title('=HYPERLINK("x")'), 'HYPERLINK("x")')
        self.assertEqual(task_import.clean_title("  +1 call"), "1 call")


class TaskImportApiCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Import target",
            code="IMP",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.dev = self.user2
        self.dev.email = "dev1@example.com"
        self.dev.save()
        ProjectMembers.objects.create(
            team=self.team, project=self.project, attendee=self.dev, member_role="editor"
        )
        ProjectTags.objects.create(
            team=self.team,
            project=self.project,
            tag_id=1,
            tag_name="Backend",
            tag_color="#111111",
            tag_text_color="#ffffff",
        )
        self.authenticate()

    def post(self, url, name="jira_export.csv", data=None, content=None, **extra):
        payload = {
            "file": SimpleUploadedFile(name, content if content is not None else raw(name)),
            "project_id": str(self.project.project_id),
        }
        payload.update(data or {})
        return self.client.post(url, payload, format="multipart", **extra)

    def count(self):
        return TaskMaster.objects.filter(project=self.project).count()


class TaskImportPreviewTests(TaskImportApiCase):
    def test_preview_writes_nothing_and_reports_rows(self):
        before = self.count()
        resp = self.post(PREVIEW)
        self.assertEqual(resp.status_code, status.HTTP_200_OK, resp.data)
        self.assertEqual(self.count(), before)
        self.assertEqual(TaskImportRun.objects.count(), 0)
        self.assertEqual(resp.data["profile"], "jira")
        self.assertEqual(resp.data["counts"]["rows"], 6)
        self.assertEqual(resp.data["counts"]["errors"], 1)  # DEMO-5 has no summary
        by_row = {r["row"]: r for r in resp.data["rows"]}
        self.assertEqual(by_row[2]["title"], "Set up the pipeline")
        self.assertEqual(by_row[2]["tags"], ["Backend", "infra"])
        self.assertEqual(by_row[3]["assignee_email"], "dev1@example.com")
        self.assertTrue(by_row[4]["warnings"])  # unknown assignee e-mail
        self.assertEqual(by_row[5]["title"], 'HYPERLINK("http://evil.test")')
        self.assertTrue(by_row[6]["errors"])
        self.assertIn("Infra", " ".join(resp.data["new_tags"]).title())

    def test_preview_notion_name_assignee_is_a_warning(self):
        resp = self.post(PREVIEW, "notion_export.csv")
        self.assertEqual(resp.data["profile"], "notion")
        by_row = {r["row"]: r for r in resp.data["rows"]}
        self.assertTrue(by_row[2]["warnings"])  # "Rita Example" is a name, not an e-mail
        self.assertEqual(by_row[3]["parent_row"], 2)

    def test_ambiguous_date_is_a_warning_in_generic(self):
        resp = self.post(PREVIEW, "generic.csv")
        by_row = {r["row"]: r for r in resp.data["rows"]}
        self.assertIsNone(by_row[4]["due_date"])
        self.assertTrue(by_row[4]["warnings"])


class TaskImportCommitTests(TaskImportApiCase):
    def commit(self, name="jira_export.csv", **extra):
        with mock.patch("origin.services.task_import.invalidate_project_tasks_cache"):
            return self.post(COMMIT, name, **extra)

    def test_commit_creates_good_rows_reports_bad_and_numbers_consecutively(self):
        before = self.count()
        resp = self.commit()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["created"], 5)
        self.assertEqual(resp.data["failed"], 1)
        self.assertEqual(self.count(), before + 5)
        tasks = TaskMaster.objects.filter(project=self.project).order_by("project_task_number")
        numbers = [t.project_task_number for t in tasks]
        self.assertEqual(numbers, list(range(numbers[0], numbers[0] + 5)))
        epic = tasks.get(title="Set up the pipeline")
        self.assertEqual(epic.status, "Open")
        self.assertEqual(epic.priority, "High")
        self.assertEqual(str(epic.due_date), "2026-11-30")
        self.assertEqual(epic.reporter_id, self.user.id)
        self.assertEqual([t["tagName"] for t in epic.tags], ["Backend"])
        login = tasks.get(title="Write the login page")
        self.assertEqual(login.assignee_id, self.dev.id)
        self.assertEqual(login.status, "WIP")
        self.assertEqual(login.priority, "Critical")
        self.assertEqual(login.parent_task_id, epic.task_id)
        self.assertEqual(login.root_task_id, epic.task_id)
        self.assertIsNone(tasks.get(title="Fix typo in footer").assignee_id)
        orphan = tasks.get(title="Orphan child")
        self.assertIsNone(orphan.parent_task_id)
        self.assertEqual(TaskImportRow.objects.filter(project=self.project).count(), 5)
        run = TaskImportRun.objects.get()
        self.assertEqual((run.created_count, run.failed_count), (5, 1))
        self.assertEqual(run.profile, "jira")
        self.assertNotIn("pipeline", str(run.report))  # row numbers and codes, never text

    def test_commit_notion_parent_by_title(self):
        resp = self.commit("notion_export.csv")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        parent = TaskMaster.objects.get(title="Plan the launch")
        child = TaskMaster.objects.get(title="Draft the press note")
        self.assertEqual(child.parent_task_id, parent.task_id)
        self.assertEqual(child.assignee_id, self.dev.id)

    def test_reimport_creates_nothing(self):
        self.commit()
        before = self.count()
        resp = self.commit()
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertEqual(resp.data["created"], 0)
        self.assertEqual(resp.data["skipped"], 5)
        self.assertEqual(self.count(), before)

    def test_reimport_without_external_ids_creates_nothing(self):
        self.commit("notion_export.csv")
        before = self.count()
        resp = self.commit("notion_export.csv")
        self.assertEqual(resp.data["created"], 0)
        self.assertEqual(self.count(), before)

    def test_identical_rows_in_one_file_both_import(self):
        content = b"title\nSame\nSame\n"
        resp = self.commit("two.csv", content=content)
        self.assertEqual(resp.data["created"], 2)
        again = self.commit("two.csv", content=content)
        self.assertEqual(again.data["created"], 0)

    def test_create_tags_option_adds_unknown_tags(self):
        resp = self.commit(data={"create_tags": "true"})
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        self.assertTrue(ProjectTags.objects.filter(project=self.project, tag_name="infra").exists())

    def test_commit_is_charged_to_the_quota_once_per_created_task(self):
        with mock.patch("origin.services.task_import.increment_usage") as inc:
            self.commit()
        self.assertEqual(inc.call_args.args[2], 5)


class TaskImportGuardTests(TaskImportApiCase):
    def test_non_member_gets_404(self):
        self.authenticate(self.user2)
        ProjectMembers.objects.filter(attendee=self.user2).delete()
        self.assertEqual(self.post(PREVIEW).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.post(COMMIT).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(TaskImportRun.objects.count(), 0)

    def test_viewer_is_refused(self):
        ProjectMembers.objects.filter(attendee=self.user2).update(member_role="viewer")
        self.authenticate(self.user2)
        self.assertEqual(self.post(COMMIT).status_code, status.HTTP_403_FORBIDDEN)

    def test_archived_project_is_refused(self):
        ProjectMaster.objects.filter(pk=self.project.pk).update(archived_at=timezone.now())
        resp = self.post(COMMIT)
        self.assertEqual(resp.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(self.post(PREVIEW).status_code, status.HTTP_409_CONFLICT)

    def test_too_many_rows_refused(self):
        content = ("title\n" + "\n".join(f"t{i}" for i in range(task_import.MAX_ROWS + 1))).encode()
        resp = self.post(PREVIEW, "big.csv", content=content)
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(resp.data["code"], "too_many_rows")

    def test_big_file_refused(self):
        with mock.patch.object(task_import, "MAX_BYTES", 10):
            resp = self.post(PREVIEW)
        self.assertEqual(resp.status_code, status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    def test_not_a_csv_is_a_400(self):
        resp = self.post(PREVIEW, "x.csv", content=b"")
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        resp = self.post(PREVIEW, "x.csv", content=b"just one column\nno title header\n")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["counts"]["errors"], 1)

    def test_quota_exhausted_is_429_and_creates_nothing(self):
        limited = mock.Mock(return_value=(False, 50, 50))
        with mock.patch("origin.views.utils.quota_guards.check_remaining_monthly", limited):
            resp = self.post(COMMIT)
        self.assertEqual(resp.status_code, status.HTTP_429_TOO_MANY_REQUESTS)
        self.assertEqual(TaskImportRun.objects.count(), 0)

    def test_unauthenticated(self):
        self.client.credentials()
        self.assertIn(
            self.post(PREVIEW).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )


class TaskImportUndoTests(TaskImportApiCase):
    def setUp(self):
        super().setUp()
        with mock.patch("origin.services.task_import.invalidate_project_tasks_cache"):
            self.post(COMMIT, "notion_export.csv")
        self.run_obj = TaskImportRun.objects.get()
        self.url = f"/api/v2/task/import/runs/{self.run_obj.run_id}/undo/"

    def test_runs_list_and_detail(self):
        listed = self.client.get(
            "/api/v2/task/import/runs/", {"project_id": self.project.project_id}
        )
        self.assertEqual(listed.status_code, 200, listed.data)
        self.assertEqual(listed.data[0]["run_id"], str(self.run_obj.run_id))
        self.assertEqual(listed.data[0]["created"], 3)

    def test_undo_deletes_untouched_keeps_edited_and_is_idempotent(self):
        TaskMaster.objects.filter(title="Book the venue").update(title="Book the venue (edited)")
        preview = self.client.get(self.url)
        self.assertEqual(preview.data["will_remove"], 2)
        self.assertEqual(preview.data["kept"], 1)
        resp = self.client.post(self.url)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["removed"], 2)
        self.assertEqual(
            sorted(
                TaskMaster.objects.filter(project=self.project).values_list("status", flat=True)
            ),
            ["Closed", "Deleted", "Deleted"],
        )
        self.run_obj.refresh_from_db()
        self.assertIsNotNone(self.run_obj.undone_at)
        again = self.client.post(self.url)
        self.assertEqual(again.data["removed"], 0)

    def test_undo_frees_the_rows_so_the_file_can_be_imported_again(self):
        self.client.post(self.url)
        with mock.patch("origin.services.task_import.invalidate_project_tasks_cache"):
            resp = self.post(COMMIT, "notion_export.csv")
        self.assertEqual(resp.data["created"], 3)

    def test_undo_by_a_non_member_is_404(self):
        ProjectMembers.objects.filter(attendee=self.user2).delete()
        self.authenticate(self.user2)
        self.assertEqual(self.client.post(self.url).status_code, 404)
