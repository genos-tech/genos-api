"""Who changed a project, a sprint, a label or a tag, and how it arrived (MIP-1758).

Every change path writes exactly one row, with the right actor and source, and nothing it must not
keep (no name, no value, no key name). A failing write fails the change (the task-history rule).
"""

from __future__ import annotations

import json
from datetime import date
from unittest import mock

from django.db import DatabaseError, IntegrityError
from django.test import TestCase

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.common.api_key_models import SCOPE_WRITE
from origin.models.common.team_models import TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.project.workspace_change_models import WorkspaceChange
from origin.models.task.sprint_models import Sprint
from origin.services import retention
from origin.services.account_export import build_export
from origin.services.project_catalog import CatalogRefused, add_project_tag, create_label
from origin.services.project_manage import set_project_code, set_project_name
from origin.services.sprint_manage import create_sprint, delete_sprint, update_sprint
from origin.services.workspace_changes import record
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import McpBase


def _rows(**filters):
    return list(WorkspaceChange.objects.filter(**filters).order_by("change_id"))


class ChangeBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def only_row(self):
        rows = _rows()
        self.assertEqual(len(rows), 1, [(r.entity_kind, r.action, r.fields) for r in rows])
        return rows[0]


class RestPathTests(ChangeBase):
    def test_rename_is_one_row_with_the_field_name_and_no_value(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "project_name": "A Long Secret Name"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.only_row()
        self.assertEqual((row.entity_kind, row.action), ("project", "update"))
        self.assertEqual(row.fields, ["project_name"])
        self.assertEqual(row.actor_id, self.user.id)
        self.assertEqual((row.source_kind, row.source_app), ("app", ""))
        self.assertNotIn("Secret", json.dumps(row.fields))

    def test_code_change_is_one_row(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "code": "NEW"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        row = self.only_row()
        self.assertEqual((row.entity_kind, row.action, row.fields), ("project", "update", ["code"]))

    def test_a_refused_rename_writes_no_row(self):
        self.authenticate()
        ProjectMaster.objects.create(
            team=self.team,
            project_name="Taken",
            code="TKN",
            owner=self.user,
            project_system_user=self.user2,
        )
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "project_name": "Taken"},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(_rows(), [])

    def test_tag_create_rename_and_delete_are_one_row_each(self):
        self.authenticate()
        url = "/api/v2/project/tag/"
        body = {
            "team_id": self.team.team_id,
            "project_id": self.project.project_id,
            "tag_name": "urgent",
            "tag_color": "#ff0000",
            "tag_text_color": "#ffffff",
        }
        self.assertEqual(self.client.post(url, body, format="json").status_code, 201)
        row = self.only_row()
        self.assertEqual((row.entity_kind, row.action), ("tag", "create"))
        self.assertEqual(row.project_id, self.project.project_id)

        res = self.client.put(
            url,
            {
                "project_id": self.project.project_id,
                "old_tag_name": "urgent",
                "tag_name": "critical",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        upd = [r for r in _rows() if r.action == "update"]
        self.assertEqual(len(upd), 1)
        self.assertEqual(upd[0].fields, ["tag_name"])

        res = self.client.generic(
            "DELETE",
            url,
            json.dumps({"project_id": self.project.project_id, "tag_name": "critical"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 204, getattr(res, "data", ""))
        self.assertEqual([r.action for r in _rows()], ["create", "update", "delete"])

    def test_label_create_rename_delete_are_one_row_each(self):
        self.authenticate()
        url = "/api/v2/project/label/"
        res = self.client.post(
            url, {"project_id": self.project.project_id, "name": "Client"}, format="json"
        )
        self.assertEqual(res.status_code, 201, getattr(res, "data", ""))
        created = self.only_row()
        self.assertEqual((created.entity_kind, created.action), ("label", "create"))
        self.assertIsNone(created.project_id)

        res = self.client.put(
            url,
            {
                "project_id": self.project.project_id,
                "label_id": created.entity_id,
                "name": "Customer",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", ""))
        self.assertEqual(
            [(r.action, r.fields) for r in _rows(entity_kind="label", action="update")],
            [("update", ["name"])],
        )
        res = self.client.generic(
            "DELETE",
            url,
            json.dumps({"project_id": self.project.project_id, "label_id": created.entity_id}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 204, getattr(res, "data", ""))
        self.assertEqual(
            [r.action for r in _rows(entity_kind="label")], ["create", "update", "delete"]
        )

    def test_project_create_over_rest_is_one_row(self):
        self.authenticate()
        res = self.client.post(
            "/api/v2/project/",
            {"project_name": "Brand New", "team": self.team.team_id},
            format="json",
        )
        self.assertEqual(res.status_code, 201, getattr(res, "data", ""))
        row = self.only_row()
        self.assertEqual((row.entity_kind, row.action), ("project", "create"))
        self.assertEqual(row.fields, [])
        self.assertEqual(row.actor_id, self.user.id)


class ServicePathTests(ChangeBase):
    def test_sprint_create_update_delete_each_write_one_row_with_changed_names(self):
        sprint = create_sprint(
            project=self.project, name="S1", start=date(2026, 1, 1), end=date(2026, 1, 14)
        )
        update_sprint(sprint, name="S1 renamed", status="active")
        update_sprint(sprint, name="S1 renamed")  # nothing changed: no row
        delete_sprint(sprint)
        delete_sprint(sprint)  # already deleted: no row
        rows = _rows(entity_kind="sprint")
        self.assertEqual(
            [(r.action, r.fields) for r in rows],
            [("create", []), ("update", ["name", "status"]), ("delete", [])],
        )
        self.assertTrue(all(r.project_id == self.project.project_id for r in rows))

    def test_project_rename_and_code_through_the_service(self):
        set_project_name(self.project, "Other", self.user.id)
        set_project_code(self.project, "OTH", self.user.id)
        self.assertEqual(
            [(r.action, r.fields) for r in _rows(entity_kind="project")],
            [("update", ["project_name"]), ("update", ["code"])],
        )

    def test_catalog_tag_and_label_create(self):
        add_project_tag(self.project, "bug")
        create_label(team_id=self.team.team_id, user=self.user, raw_name="Internal")
        self.assertEqual(
            sorted((r.entity_kind, r.action) for r in _rows()),
            [("label", "create"), ("tag", "create")],
        )

    def test_the_in_app_agent_is_recorded_as_agent(self):
        set_current_user(self.user, via={"kind": "agent"})
        try:
            create_sprint(
                project=self.project, name="Agent", start=date(2026, 3, 1), end=date(2026, 3, 7)
            )
        finally:
            clear_current_user()
        row = self.only_row()
        self.assertEqual((row.source_kind, row.source_app), ("agent", ""))
        self.assertEqual(row.actor_id, self.user.id)

    def test_a_connected_app_keeps_its_family_only(self):
        set_current_user(
            self.user,
            via={
                "kind": "oauth",
                "app": "claude",
                "connection_id": "c-123",
                "client_name": "My Laptop",
            },
        )
        try:
            set_project_name(self.project, "Via app", self.user.id)
        finally:
            clear_current_user()
        row = self.only_row()
        self.assertEqual((row.source_kind, row.source_app), ("oauth", "claude"))
        blob = json.dumps({"fields": row.fields, "kind": row.source_kind, "app": row.source_app})
        self.assertNotIn("c-123", blob)
        self.assertNotIn("My Laptop", blob)

    def test_an_unknown_app_or_kind_is_stored_as_nothing(self):
        set_current_user(self.user, via={"kind": "oauth", "app": "someone-else"})
        try:
            set_project_name(self.project, "Unknown app", self.user.id)
        finally:
            clear_current_user()
        row = self.only_row()
        self.assertEqual((row.source_kind, row.source_app), ("oauth", ""))

    def test_no_request_means_no_actor_and_no_source(self):
        set_project_name(self.project, "Command", self.user.id)
        row = self.only_row()
        self.assertIsNone(row.actor_id)
        self.assertEqual((row.source_kind, row.source_app), ("", ""))

    def test_field_names_are_capped(self):
        row = record(
            team_id=self.team.team_id,
            entity_kind="tag",
            entity_id=1,
            action="update",
            project_id=self.project.project_id,
            fields=["x" * 200] + [f"f{i}" for i in range(20)],
        )
        self.assertEqual(len(row.fields), 8)
        self.assertEqual(len(row.fields[0]), 40)

    def test_an_unknown_entity_or_action_is_refused(self):
        with self.assertRaises(ValueError):
            record(team_id=self.team.team_id, entity_kind="calendar", entity_id=1, action="create")
        with self.assertRaises(ValueError):
            record(team_id=self.team.team_id, entity_kind="sprint", entity_id=1, action="rename")
        self.assertEqual(_rows(), [])


class FailureRuleTests(ChangeBase):
    def test_a_failing_row_fails_the_change_and_leaves_no_half_change(self):
        with mock.patch(
            "origin.services.workspace_changes.WorkspaceChange.objects.create",
            side_effect=DatabaseError("boom"),
        ):
            with self.assertRaises(DatabaseError):
                create_sprint(
                    project=self.project, name="Lost", start=date(2026, 5, 1), end=date(2026, 5, 7)
                )
        self.assertFalse(Sprint.objects.filter(name="Lost").exists())
        self.assertEqual(_rows(), [])


class TagChangeLogTests(ChangeBase):
    """MIP-1889: the tag paths and their change-log rows."""

    URL = "/api/v2/project/tag/"

    def _post(self, name, **extra):
        body = {
            "team_id": self.team.team_id,
            "project_id": self.project.project_id,
            "tag_name": name,
            "tag_color": "#ff0000",
            "tag_text_color": "#ffffff",
            **extra,
        }
        return self.client.post(self.URL, body, format="json")

    def test_an_audit_failure_is_not_reported_as_a_duplicate_tag(self):
        with mock.patch(
            "origin.services.project_catalog.record_workspace_change",
            side_effect=IntegrityError("audit row"),
        ):
            with self.assertRaises(IntegrityError):
                add_project_tag(self.project, "bug")
        self.assertFalse(ProjectTags.objects.filter(project=self.project).exists())

    def test_a_real_duplicate_is_still_a_409(self):
        add_project_tag(self.project, "bug")
        with self.assertRaises(CatalogRefused):
            add_project_tag(self.project, "BUG")

    def test_a_failing_audit_row_leaves_no_tag_over_rest(self):
        self.authenticate()
        with mock.patch(
            "origin.views.project.prj_views.record_workspace_change",
            side_effect=DatabaseError("boom"),
        ):
            with self.assertRaises(DatabaseError):
                self._post("lost")
        self.assertFalse(ProjectTags.objects.filter(tag_name="lost").exists())

    def test_a_tag_added_after_a_delete_gets_a_new_entity_id(self):
        self.authenticate()
        self.assertEqual(self._post("a").status_code, 201)
        self.assertEqual(self._post("b").status_code, 201)
        self.client.generic(
            "DELETE",
            self.URL,
            json.dumps({"project_id": self.project.project_id, "tag_name": "a"}),
            content_type="application/json",
        )
        self.assertEqual(self._post("c").status_code, 201)
        ids = [r.entity_id for r in _rows(entity_kind="tag", action="create")]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_a_tag_and_its_row_are_filed_under_the_projects_team(self):
        self.authenticate()
        self.assertEqual(self._post("mine").status_code, 201)
        tag = ProjectTags.objects.get(tag_name="mine")
        self.assertEqual(tag.team_id, self.project.team_id)
        self.assertEqual(self.only_row().team_id, self.project.team_id)

    def test_a_legacy_tag_without_a_team_can_still_be_renamed_and_deleted(self):
        self.authenticate()
        ProjectTags.objects.create(
            team=None, project=self.project, tag_id=1, tag_name="old", tag_color="#fff"
        )
        res = self.client.put(
            self.URL,
            {"project_id": self.project.project_id, "old_tag_name": "old", "tag_name": "new"},
            format="json",
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", ""))
        self.assertEqual(_rows(entity_kind="tag", action="update")[0].team_id, self.team.team_id)


class DeletedActorTests(ChangeBase):
    def test_a_hard_deleted_actor_leaves_the_row_with_no_actor(self):
        set_current_user(self.user)
        try:
            set_project_name(self.project, "Before delete", self.user.id)
        finally:
            clear_current_user()
        row_id = self.only_row().change_id
        self.assertEqual(WorkspaceChange.objects.get(pk=row_id).actor_id, self.user.id)
        self.user.delete()
        row = WorkspaceChange.objects.get(pk=row_id)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.fields, ["project_name"])

    def test_account_deletion_keeps_the_rows(self):
        from origin.services.account_deletion import delete_account

        # user2 owns no team, so the soft delete goes through; the row must stay, actor kept.
        set_current_user(self.user2)
        try:
            set_project_name(self.project, "Kept", self.user.id)
        finally:
            clear_current_user()
        row_id = self.only_row().change_id
        delete_account(self.user2)
        row = WorkspaceChange.objects.get(pk=row_id)
        self.assertEqual(row.actor_id, self.user2.id)
        self.assertEqual(row.fields, ["project_name"])


class ExportTests(ChangeBase):
    def test_export_lists_only_the_rows_the_person_made(self):
        set_current_user(self.user)
        try:
            set_project_name(self.project, "Mine", self.user.id)
        finally:
            clear_current_user()
        set_current_user(self.user2)
        try:
            set_project_code(self.project, "THEM", self.user.id)
        finally:
            clear_current_user()
        export = build_export(self.user)
        listed = export["workspace_changes"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["fields"], ["project_name"])
        self.assertNotIn("actor", listed[0])
        self.assertIn("workspace_changes", export["more_counts"])


class RetentionTests(TestCase):
    def test_the_policy_has_a_365_day_approval_and_no_default(self):
        policy = retention.BY_NAME["workspace_changes"]
        self.assertEqual(policy.approved_days, 365)
        self.assertEqual(policy.action, retention.Action.DELETE)
        self.assertEqual(policy.age_field, "created_at")
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop(policy.env_var, None)
            self.assertIsNone(policy.days())
            self.assertIsNone(policy.candidates())


class ReadEndpointTests(ChangeBase):
    def test_a_member_reads_changes_and_a_stranger_gets_404(self):
        set_current_user(self.user)
        try:
            set_project_name(self.project, "Seen", self.user.id)
        finally:
            clear_current_user()
        self.authenticate()
        res = self.client.get(f"/api/v2/project/changes/?project_id={self.project.project_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["changes"][0]["fields"], ["project_name"])
        self.assertEqual(res.data["changes"][0]["actor_id"], str(self.user.id))
        self.assertNotIn("Seen", json.dumps(res.data))

        stranger = CustomUser.objects.create_user(
            username="stranger", email="stranger@example.com", password="x-pass-123"
        )
        TeamMembers.objects.create(team=self.team, attendee=stranger)
        self.authenticate(stranger)
        res = self.client.get(f"/api/v2/project/changes/?project_id={self.project.project_id}")
        self.assertEqual(res.status_code, 404)

    def test_bad_before_is_a_400(self):
        self.authenticate()
        res = self.client.get(
            f"/api/v2/project/changes/?project_id={self.project.project_id}&before=abc"
        )
        self.assertEqual(res.status_code, 400)


class McpPathTests(McpBase):
    def test_a_key_write_is_recorded_with_kind_api_key_and_never_its_name(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=True)
        result = self._result(
            self._call(
                "manage_sprint",
                {
                    "action": "create",
                    "project_id": self.project.project_id,
                    "name": "Polish Week",
                    "start_date": "2026-07-01",
                    "end_date": "2026-07-07",
                },
            )
        )
        self.assertIn("content", result)
        rows = _rows(entity_kind="sprint")
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].source_kind, rows[0].source_app), ("api_key", ""))
        self.assertEqual(rows[0].actor_id, self.user.id)
        self.assertNotIn("mcp", json.dumps([rows[0].fields, rows[0].source_kind]))


class MoreChangePathTests(ChangeBase):
    def test_a_failing_row_leaves_the_rename_unchanged(self):
        with mock.patch(
            "origin.services.workspace_changes.WorkspaceChange.objects.create",
            side_effect=DatabaseError("boom"),
        ):
            with self.assertRaises(DatabaseError):
                set_project_name(self.project, "Never", self.user.id)
        self.project.refresh_from_db()
        self.assertEqual(self.project.project_name, "Website")

    def test_owner_transfer_is_one_row(self):
        self.authenticate()
        res = self.client.put(
            "/api/v2/project/",
            {"project_id": self.project.project_id, "owner_id": str(self.user2.id)},
            format="json",
        )
        self.assertEqual(res.status_code, 200, getattr(res, "data", ""))
        row = self.only_row()
        self.assertEqual(
            (row.entity_kind, row.action, row.fields), ("project", "update", ["owner"])
        )

    def test_deleting_a_project_writes_one_row_that_outlives_it(self):
        from origin.services.project_delete import delete_project

        project_id = self.project.project_id
        set_current_user(self.user)
        try:
            delete_project(self.project)
        finally:
            clear_current_user()
        self.assertFalse(ProjectMaster.objects.filter(pk=self.project.pk).exists())
        row = self.only_row()
        self.assertEqual(
            (row.entity_kind, row.action, row.project_id),
            ("project", "delete", project_id),
        )
        self.assertEqual(row.actor_id, self.user.id)
