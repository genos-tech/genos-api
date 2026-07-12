"""Note write tools — BAU Wave 2 hardening.

Contract under test:

  * `update_note` ACL matches the note UI (`require_write_role`): owner,
    explicit editor, and task-note project members may edit; an explicit
    Viewer row beats the implicit project-member Editor; personal notes
    still grant no implicit access.
  * Every title/body change through `update_note` writes a version
    snapshot with the REST PUT's coalescing; folder-only moves don't.
  * `create_note` writes the same trio the REST create paths do — note,
    ROLE_OWNER permission row, v1 version snapshot — for both families.
  * Result payloads carry `title` / `parent_context` so the controller
    emits a clickable note chip and the frontend can deep-link + apply.
"""

from datetime import date

from origin.models.chat.todo_models import ToDoGroup, ToDoItem
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.controller import (
    _friendly_arguments,
    _ui_sources_from_tool_result,
)
from origin.search_engine.agent.tools.base import ToolContext, ToolError
from origin.search_engine.agent.tools.create_note import _run as create_note_run
from origin.search_engine.agent.tools.entity_links import resolve_note_entity_link
from origin.search_engine.agent.tools.update_note import _run as update_note_run
from origin.views.utils.note_role import (
    NOTE_TYPE_PERSONAL,
    NOTE_TYPE_TASK,
    ROLE_EDITOR,
    ROLE_OWNER,
    ROLE_VIEWER,
)

from .test_base import BaseAPITestCase


class NoteWriteToolTestBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website Redesign",
            code="WRD",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.ctx2 = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user2.id))

    def _personal_note(self, title="mine", owner=None):
        return PersonalNoteMaster.objects.create(
            team=self.team, owner=owner or self.user, title=title, body=[]
        )

    def _task_note(self, title="shared", owner=None, project=None):
        return TaskNoteMaster.objects.create(
            team=self.team,
            project=project or self.project,
            owner=owner or self.user,
            title=title,
            body=[],
        )

    def _versions(self, note_type, note_id):
        return list(
            NoteVersionMaster.objects.filter(note_type=note_type, note_id=note_id).order_by(
                "version_no"
            )
        )


class UpdateNoteAclTests(NoteWriteToolTestBase):
    def test_owner_can_edit_personal_note(self):
        note = self._personal_note()
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "title": "renamed"}, self.ctx
        )
        self.assertEqual(res["changed_fields"], ["title"])

    def test_personal_note_has_no_implicit_access(self):
        note = self._personal_note()
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "personal", "note_id": note.note_id, "title": "x"}, self.ctx2
            )

    def test_explicit_editor_can_edit_personal_note(self):
        note = self._personal_note()
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_PERSONAL,
            role_id=ROLE_EDITOR,
        )
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "title": "x"}, self.ctx2
        )
        self.assertEqual(res["changed_fields"], ["title"])

    def test_project_member_can_edit_task_note(self):
        # UI parity: task notes are a shared surface — project membership
        # grants implicit Editor (this was previously rejected).
        note = self._task_note()
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        res = update_note_run(
            {"note_type": "task", "note_id": note.note_id, "content_text": "hello"}, self.ctx2
        )
        self.assertEqual(res["changed_fields"], ["body"])

    def test_explicit_viewer_beats_implicit_member_editor(self):
        note = self._task_note()
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user2,
            note_id=note.note_id,
            note_type=NOTE_TYPE_TASK,
            role_id=ROLE_VIEWER,
        )
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "task", "note_id": note.note_id, "content_text": "x"}, self.ctx2
            )

    def test_non_member_cannot_edit_task_note(self):
        note = self._task_note()
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "task", "note_id": note.note_id, "content_text": "x"}, self.ctx2
            )

    def test_cross_team_note_rejected(self):
        note = self._task_note()
        foreign = ToolContext(team_id="other-team", user_id=str(self.user.id))
        with self.assertRaises(ToolError):
            update_note_run(
                {"note_type": "task", "note_id": note.note_id, "title": "x"}, foreign
            )


class UpdateNoteSnapshotTests(NoteWriteToolTestBase):
    def test_body_change_writes_version_snapshot(self):
        note = self._personal_note()
        update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "content_text": "## New body"},
            self.ctx,
        )
        versions = self._versions(NOTE_TYPE_PERSONAL, note.note_id)
        self.assertEqual(len(versions), 1)
        note.refresh_from_db()
        self.assertEqual(versions[0].body, note.body)
        self.assertEqual(versions[0].editor_id, self.user.id)

    def test_same_editor_burst_coalesces_to_one_row(self):
        note = self._personal_note()
        for body in ("draft one", "draft two"):
            update_note_run(
                {"note_type": "personal", "note_id": note.note_id, "content_text": body},
                self.ctx,
            )
        versions = self._versions(NOTE_TYPE_PERSONAL, note.note_id)
        self.assertEqual(len(versions), 1)

    def test_different_editor_gets_own_version_row(self):
        note = self._task_note()
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user2)
        update_note_run(
            {"note_type": "task", "note_id": note.note_id, "content_text": "by owner"}, self.ctx
        )
        update_note_run(
            {"note_type": "task", "note_id": note.note_id, "content_text": "by member"},
            self.ctx2,
        )
        versions = self._versions(NOTE_TYPE_TASK, note.note_id)
        self.assertEqual([v.version_no for v in versions], [1, 2])
        self.assertEqual([v.editor_id for v in versions], [self.user.id, self.user2.id])

    def test_folder_only_move_does_not_snapshot(self):
        folder = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="F")
        note = self._personal_note()
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "folder_id": folder.folder_id},
            self.ctx,
        )
        self.assertEqual(res["changed_fields"], ["folder"])
        self.assertEqual(self._versions(NOTE_TYPE_PERSONAL, note.note_id), [])

    def test_noop_update_does_not_snapshot(self):
        note = self._personal_note(title="same")
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "title": "same"}, self.ctx
        )
        self.assertEqual(res["changed_fields"], [])
        self.assertEqual(self._versions(NOTE_TYPE_PERSONAL, note.note_id), [])


class UpdateNotePayloadTests(NoteWriteToolTestBase):
    def test_task_note_result_carries_title_and_parent_context(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=task,
            title="shared",
            body=[],
        )
        res = update_note_run(
            {"note_type": "task", "note_id": note.note_id, "title": "renamed"}, self.ctx
        )
        self.assertEqual(res["title"], "renamed")
        self.assertEqual(
            res["parent_context"],
            {"project_id": str(self.project.project_id), "task_id": str(task.task_id)},
        )


class CreateNoteParityTests(NoteWriteToolTestBase):
    def test_personal_create_writes_v1_snapshot(self):
        res = create_note_run(
            {"note_type": "personal", "title": "n", "content_text": "## Body"}, self.ctx
        )
        versions = self._versions(NOTE_TYPE_PERSONAL, res["note_id"])
        self.assertEqual([v.version_no for v in versions], [1])
        note = PersonalNoteMaster.objects.get(note_id=res["note_id"])
        self.assertEqual(versions[0].body, note.body)

    def test_task_create_writes_owner_row_and_v1_snapshot(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        res = create_note_run(
            {
                "note_type": "task",
                "title": "plan",
                "content_text": "## Plan",
                "project_id": self.project.project_id,
                "task_id": task.task_id,
            },
            self.ctx,
        )
        self.assertEqual([v.version_no for v in self._versions(NOTE_TYPE_TASK, res["note_id"])], [1])
        self.assertTrue(
            NotePermissionMaster.objects.filter(
                note_id=res["note_id"],
                note_type=NOTE_TYPE_TASK,
                user=self.user,
                role_id=ROLE_OWNER,
            ).exists()
        )
        self.assertEqual(
            res["parent_context"],
            {"project_id": str(self.project.project_id), "task_id": str(task.task_id)},
        )


class ControllerNoteChipTests(NoteWriteToolTestBase):
    def test_create_note_result_yields_note_chip(self):
        res = create_note_run(
            {
                "note_type": "task",
                "title": "plan",
                "project_id": self.project.project_id,
            },
            self.ctx,
        )
        chips = _ui_sources_from_tool_result("create_note", res)
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["entity_type"], "note")
        self.assertEqual(chips[0]["entity_id"], f"note:task:{res['note_id']}")
        self.assertEqual(chips[0]["project_id"], str(self.project.project_id))

    def test_update_note_result_yields_note_chip(self):
        note = self._personal_note(title="mine")
        res = update_note_run(
            {"note_type": "personal", "note_id": note.note_id, "title": "renamed"}, self.ctx
        )
        chips = _ui_sources_from_tool_result("update_note", res)
        self.assertEqual(chips[0]["entity_id"], f"note:personal:{note.note_id}")
        self.assertEqual(chips[0]["title"], "renamed")


class FriendlyArgumentTests(NoteWriteToolTestBase):
    def test_update_note_args_gain_note_title_and_keep_note_id(self):
        note = self._task_note(title="Perf research")
        out = _friendly_arguments(
            {"note_type": "task", "note_id": note.note_id, "content_text": "x"},
            tool_name="update_note",
        )
        self.assertEqual(out["note_title"], "Perf research")
        self.assertEqual(out["note_id"], note.note_id)

    def test_folder_id_resolves_to_folder_name(self):
        folder = PersonalNoteFolder.objects.create(team=self.team, owner=self.user, name="Research")
        out = _friendly_arguments(
            {"note_type": "personal", "title": "n", "folder_id": folder.folder_id},
            tool_name="create_note",
        )
        self.assertEqual(out["folder_id"], "Research")


def _links_in(body):
    """Flatten every link inline node in a stored BlockNote body."""
    out = []
    for block in body:
        for node in block.get("content") or []:
            if node.get("type") == "link":
                out.append((node["href"], node["content"][0]["text"]))
    return out


class NoteBodyEntityLinkTests(NoteWriteToolTestBase):
    """Citation tokens in note bodies become working in-app links
    (entity_links.resolve_note_entity_link via markdown_to_blocks)."""

    def _task(self, title="Investigate slow homepage load"):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title=title, status="Open"
        )
        task.refresh_from_db()  # signal assigns project_task_number/display_id
        return task

    def test_resolver_supported_types(self):
        task = self._task()
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, title="Perf hardening"
        )
        my_note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Research", body=[]
        )
        task_note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user,
            task=task, title="Plan", body=[],
        )
        team_id = str(self.team.team_id)
        pid = self.project.project_id

        self.assertEqual(
            resolve_note_entity_link(f"task:{task.task_id}", team_id=team_id),
            (f"/workspace/tasks/project/{pid}/task/{task.task_id}", task.display_id),
        )
        self.assertEqual(
            resolve_note_entity_link(f"project:{pid}", team_id=team_id),
            (f"/workspace/tasks/project/{pid}", "Website Redesign"),
        )
        self.assertEqual(
            resolve_note_entity_link(f"milestone:{milestone.milestone_id}", team_id=team_id),
            (
                f"/workspace/tasks/project/{pid}/milestone/{milestone.milestone_id}",
                "Perf hardening",
            ),
        )
        self.assertEqual(
            resolve_note_entity_link(f"note:personal:{my_note.note_id}", team_id=team_id),
            (f"/workspace/notes/my/{my_note.note_id}", "Research"),
        )
        self.assertEqual(
            resolve_note_entity_link(f"note:task:{task_note.note_id}", team_id=team_id),
            (
                f"/workspace/notes/task/project/{pid}/task/{task.task_id}"
                f"/note/{task_note.note_id}",
                "Plan",
            ),
        )
        # Chat tokens are a pure syntactic mapping.
        self.assertEqual(
            resolve_note_entity_link(
                "chat:pm:11111111-2222-3333-4444-555555555555:thread:"
                "66666666-7777-8888-9999-000000000000",
                team_id=team_id,
            ),
            (
                "/workspace/chat/pm/11111111-2222-3333-4444-555555555555"
                "/thread/66666666-7777-8888-9999-000000000000",
                "thread",
            ),
        )
        # Todo items link via the GROUP's date (a model-mangled token
        # date still resolves to the item's real day).
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 7, 12)
        )
        todo = ToDoItem.objects.create(group=group, title="ship the fix")
        self.assertEqual(
            resolve_note_entity_link(f"todo:2026-01-01:item:{todo.item_id}", team_id=team_id),
            (f"/workspace/todo/2026-07-12/item/{todo.item_id}", "ship the fix"),
        )

    def test_resolver_rejects_foreign_deleted_and_unroutable(self):
        task = self._task()
        team_id = str(self.team.team_id)
        # Foreign team.
        self.assertIsNone(resolve_note_entity_link(f"task:{task.task_id}", team_id="other"))
        # Deleted task.
        task.is_deleted = True
        task.save(update_fields=["is_deleted"])
        self.assertIsNone(resolve_note_entity_link(f"task:{task.task_id}", team_id=team_id))
        # Unknown id / malformed token / project-level task note (no route).
        self.assertIsNone(resolve_note_entity_link("task:999999", team_id=team_id))
        # Nonexistent todo item.
        self.assertIsNone(resolve_note_entity_link("todo:2026-07-10:item:5", team_id=team_id))
        # Foreign-team todo item.
        group = ToDoGroup.objects.create(
            team=self.team, user=self.user, local_date=date(2026, 7, 10)
        )
        todo = ToDoItem.objects.create(group=group, title="mine")
        self.assertIsNone(
            resolve_note_entity_link(f"todo:2026-07-10:item:{todo.item_id}", team_id="other")
        )
        # Malformed todo token (missing item segment).
        self.assertIsNone(resolve_note_entity_link("todo:2026-07-10", team_id=team_id))
        project_note = TaskNoteMaster.objects.create(
            team=self.team, project=self.project, owner=self.user, title="P", body=[]
        )
        self.assertIsNone(
            resolve_note_entity_link(f"note:task:{project_note.note_id}", team_id=team_id)
        )

    def test_create_note_body_carries_working_task_link(self):
        task = self._task()
        res = create_note_run(
            {
                "note_type": "task",
                "title": "Plan: mobile Safari perf",
                "content_text": (
                    "### 🎯 Goal\n"
                    f"Fix [the Safari task](task:{task.task_id}) — see [task:{task.task_id}]."
                ),
                "project_id": self.project.project_id,
                "task_id": task.task_id,
            },
            self.ctx,
        )
        note = TaskNoteMaster.objects.get(note_id=res["note_id"])
        href = f"/workspace/tasks/project/{self.project.project_id}/task/{task.task_id}"
        self.assertEqual(
            _links_in(note.body),
            [(href, "the Safari task"), (href, task.display_id)],
        )

    def test_update_note_body_resolves_links_too(self):
        task = self._task()
        note = self._personal_note()
        update_note_run(
            {
                "note_type": "personal",
                "note_id": note.note_id,
                "content_text": f"Progress on [the fix](task:{task.task_id}).",
            },
            self.ctx,
        )
        note.refresh_from_db()
        self.assertEqual(
            _links_in(note.body),
            [(f"/workspace/tasks/project/{self.project.project_id}/task/{task.task_id}", "the fix")],
        )

    def test_cross_team_token_degrades_to_prose(self):
        task = self._task()
        other_ctx = self.ctx  # same user, but pretend the task is foreign:
        TaskMaster.objects.filter(task_id=task.task_id).update(team=None)
        res = create_note_run(
            {
                "note_type": "personal",
                "title": "n",
                "content_text": f"see [the task](task:{task.task_id})",
            },
            other_ctx,
        )
        note = PersonalNoteMaster.objects.get(note_id=res["note_id"])
        self.assertEqual(_links_in(note.body), [])
        # The prose survives as plain text.
        flat = "".join(
            n.get("text", "")
            for b in note.body
            for n in (b.get("content") or [])
            if n.get("type") == "text"
        )
        self.assertIn("the task", flat)
