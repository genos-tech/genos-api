"""`restore_task_body` (MIP-880): walk back a rewrite of a task's description.

`update_task` with `content_text` replaces the whole body, and over MCP there is
no approval card and no undo. The previous body is kept on the history row of an
agent or API-key rewrite, `get_task_history` says it is `restorable` and (new)
gives the `activity_id` that names it, and this puts it back. It shares its
implementation with the page's restore, so the rules there are the rules here:
scoped to the task, pushed into the live editor document, and itself undoable.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import ToolError, ToolForbidden
from origin.search_engine.agent.tools.get_task_history import GET_TASK_HISTORY
from origin.search_engine.agent.tools.restore_task_body import RESTORE_TASK_BODY

from .test_base import BaseAPITestCase

User = get_user_model()
DESC = TaskActivityActionType.DESCRIPTION

ORIGINAL = [{"type": "paragraph", "content": [{"type": "text", "text": "The long, careful spec"}]}]
REWRITE = [{"type": "paragraph", "content": [{"type": "text", "text": "Oops, a fragment"}]}]
AGENT = {"kind": "agent"}


class RestoreTaskBodyTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Site",
            code="SIT",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Spec",
            status="Open",
            content=ORIGINAL,
        )
        TaskActivity.objects.filter(task=self.task).delete()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))

    def tearDown(self):
        clear_current_user()
        super().tearDown()

    def write(self, content, via=AGENT, task=None):
        task = task or self.task
        set_current_user(self.user, via=via)
        task.content = content
        task.save(update_fields=["content"])
        clear_current_user()

    def history(self, task=None):
        out = GET_TASK_HISTORY.run({"task_id": (task or self.task).task_id}, self.ctx)
        return [e for e in out["history"] if e["action"] == DESC]

    def restore(self, activity_id, task=None, ctx=None):
        return RESTORE_TASK_BODY.run(
            {"task_id": (task or self.task).task_id, "activity_id": activity_id}, ctx or self.ctx
        )

    def fresh(self):
        return TaskMaster.objects.get(pk=self.task.pk).content

    # ---- the id that makes a restore possible ----

    def test_history_names_the_edit_to_restore(self):
        self.write(REWRITE)
        (entry,) = self.history()
        self.assertTrue(entry["restorable"])
        self.assertIsInstance(entry["activity_id"], int)

    def test_an_edit_that_kept_nothing_has_no_id_to_restore_from(self):
        self.write(REWRITE, via={"kind": "app"})
        (entry,) = self.history()
        self.assertFalse(entry["restorable"])
        self.assertNotIn("activity_id", entry)

    # ---- the restore ----

    def test_it_puts_the_old_text_back(self):
        self.write(REWRITE)
        (entry,) = self.history()

        result = self.restore(entry["activity_id"])

        self.assertEqual(self.fresh(), ORIGINAL)
        self.assertEqual(result["restored_from"], entry["activity_id"])
        self.assertTrue(result["replaced_body_kept"])
        self.assertIn("Restored the description", result["__summary__"])

    def test_it_does_not_hand_either_body_to_the_model(self):
        self.write(REWRITE)
        (entry,) = self.history()
        result = self.restore(entry["activity_id"])
        self.assertNotIn("careful spec", str(result))
        self.assertNotIn("fragment", str(result))

    def test_a_restore_can_itself_be_undone(self):
        self.write(REWRITE)
        (first,) = self.history()
        self.restore(first["activity_id"])
        self.assertEqual(self.fresh(), ORIGINAL)

        restorable = [e for e in self.history() if e["restorable"]]
        newest = max(restorable, key=lambda e: e["activity_id"])
        self.restore(newest["activity_id"])

        self.assertEqual(self.fresh(), REWRITE)

    def test_it_pushes_the_body_into_the_live_editor(self):
        self.write(REWRITE)
        (entry,) = self.history()
        with mock.patch("origin.services.collab_push.push_task_body") as push:
            self.restore(entry["activity_id"])
        push.assert_called_once_with(self.task.task_id, ORIGINAL)

    # ---- what it refuses ----

    def test_an_id_from_another_task_is_not_found(self):
        other = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Other",
            status="Open",
            content=ORIGINAL,
        )
        self.write(REWRITE, task=other)
        (theirs,) = self.history(task=other)

        with self.assertRaises(ToolError) as caught:
            self.restore(theirs["activity_id"])

        self.assertIn("get_task_history", str(caught.exception))
        self.assertEqual(self.fresh(), ORIGINAL, "this task is untouched")

    def test_an_edit_that_kept_nothing_cannot_be_restored(self):
        self.write(REWRITE, via={"kind": "app"})
        row = TaskActivity.objects.get(task=self.task, action_type=DESC)
        with self.assertRaises(ToolError):
            self.restore(row.activity_id)
        self.assertEqual(self.fresh(), REWRITE)

    def test_the_ids_must_be_numbers(self):
        with self.assertRaises(ToolError):
            self.restore("abc")

    def test_a_user_who_cannot_see_the_task_is_refused(self):
        self.write(REWRITE)
        (entry,) = self.history()
        stranger = User.objects.create_user(
            username="stranger", email="stranger@example.com", password="pw"
        )
        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="e@team.example.com", owner=stranger
        )
        TeamMembers.objects.create(team=elsewhere, attendee=stranger)
        bystander = User.objects.create_user(
            username="bystander", email="bystander@example.com", password="pw"
        )
        TeamMembers.objects.create(team=self.team, attendee=bystander)

        for ctx in (
            ToolContext(team_id=str(elsewhere.team_id), user_id=str(stranger.id)),
            ToolContext(team_id=str(self.team.team_id), user_id=str(bystander.id)),
        ):
            with self.assertRaises(ToolForbidden):
                self.restore(entry["activity_id"], ctx=ctx)
        self.assertEqual(self.fresh(), REWRITE)

    def test_a_member_acting_for_another_team_is_refused(self):
        self.write(REWRITE)
        (entry,) = self.history()
        elsewhere = TeamMaster.objects.create(
            team_name="Elsewhere 2", team_email="e2@team.example.com", owner=self.user
        )
        TeamMembers.objects.create(team=elsewhere, attendee=self.user)
        ctx = ToolContext(team_id=str(elsewhere.team_id), user_id=str(self.user.id))
        with self.assertRaises(ToolForbidden):
            self.restore(entry["activity_id"], ctx=ctx)
        self.assertEqual(self.fresh(), REWRITE)

    def test_a_deleted_task_is_refused(self):
        self.write(REWRITE)
        (entry,) = self.history()
        TaskMaster.objects.filter(pk=self.task.pk).update(is_deleted=True)
        with self.assertRaises(ToolError):
            self.restore(entry["activity_id"])

    def test_it_is_an_approval_gated_write(self):
        self.assertTrue(RESTORE_TASK_BODY.requires_approval)
        self.assertIs(REGISTRY["restore_task_body"], RESTORE_TASK_BODY)
