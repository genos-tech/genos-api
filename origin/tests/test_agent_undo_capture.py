"""Undo, the capture half: what an approved write keeps so that it can be undone.

An approved write returns its PRE-IMAGE under `__undo__`; the controller pops it and
stores it on `AgentStep.undo_json`; the browser is told (`undo` on the result event,
`undoable` on the card). Doing the undo is `test_agent_undo_endpoint`.

What these pin, and why each is a trap:

  * the MODEL sees exactly what it saw before: `result_json` and the function
    response it is replayed are the tool's own return minus `__summary__` and
    `__undo__`, and the pre-image is nowhere in them (it is replayed to the model, and
    blanked by retention: neither is right for it);
  * a write is asked for its pre-image only on the approve path (`capture_undo`), so a
    direct call, MCP and the evals get the result they always got;
  * each v1 tool stores what its undo needs, and a call that touches what undo cannot
    put back (collaborators, custom fields, a move, a folder move, a milestone's own task)
    stores nothing, so the result event carries no `undo` and the card's promise is
    kept in both directions;
  * an EDITED approval captures the state before the EDITED write: the pre-image is
    taken inside the tool, with the arguments it runs with;
  * a write that changed nothing stores nothing.

`TransactionTestCase` via `DecideEditBase`, as `test_agent_decide_edits`: the resume runs
on a real worker thread whose writes must really commit.
"""

from __future__ import annotations

import copy
import dataclasses
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from origin.middleware.current_user import clear_current_user, set_current_user
from origin.models.common.team_models import TeamMembers
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import agent_views
from origin.search_engine.agent import controller, undo
from origin.search_engine.agent.model_fence import fence_for_model
from origin.search_engine.agent.tools import REGISTRY, ToolContext
from origin.search_engine.agent.tools.base import Tool
from origin.search_engine.llm.types import AgentMessage, FunctionCall
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.views.utils.note_role import ROLE_OWNER
from origin.views.utils.note_version import snapshot_note_version

from .test_agent_decide_edits import DecideEditBase

BODY = [{"type": "paragraph", "content": [{"type": "text", "text": "The original spec"}]}]
REWRITE = [{"type": "paragraph", "content": [{"type": "text", "text": "A bad rewrite"}]}]
PERSONAL = 1
POPPED = {"__summary__", "__undo__"}


def blocks(text):
    return [{"type": "paragraph", "content": [{"type": "text", "text": text, "styles": {}}]}]


class CaptureBase(DecideEditBase):
    """A project the user is on, a task with a body, a note with two versions, and a
    way to approve a real tool through the real endpoint while seeing what it returned."""

    def setUp(self):
        super().setUp()
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        for member in (self.user, self.other):
            ProjectMembers.objects.create(team=self.team, project=self.project, attendee=member)
        self.task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            assignee=self.user,
            title="Seed",
            status="Open",
            priority="Low",
            content=copy.deepcopy(BODY),
        )
        self.note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="Plan v2", body=blocks("second draft")
        )
        NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=self.note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_OWNER,
        )
        self.v1 = self.version("Plan v1", "first draft")
        self.v2 = self.version("Plan v2", "second draft")
        # What each tool returned, as the controller received it (before it popped anything).
        self.raw: dict[str, list[dict]] = {}

    def version(self, title, text):
        return snapshot_note_version(
            team=self.team,
            editor=self.user,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title=title,
            body=blocks(text),
            force_new=True,
        )

    def spy(self, name):
        """Wrap the real tool so what it returned is recorded before the controller pops."""
        real = REGISTRY[name]

        def run(args, ctx):
            result = real.run(args, ctx)
            self.raw.setdefault(name, []).append(copy.deepcopy(result))
            return result

        patcher = mock.patch.dict(REGISTRY, {name: dataclasses.replace(real, run=run)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def approve(self, tool, arguments, edited_args=None):
        self.spy(tool)
        run, token, step = self.pause(tool, arguments)
        events = self.events(self.post(run, token, edited_args=edited_args))
        step.refresh_from_db()
        return events, step

    def result_event(self, events):
        (event,) = self.of_type(events, "tool_call_result")
        return event

    def agent_rewrite(self, task, content):
        """A rewrite of `task`'s body the way the agent makes one: the history keeps the old."""
        set_current_user(self.user, via={"kind": "agent"})
        try:
            task.content = content
            task.save(update_fields=["content"])
        finally:
            clear_current_user()
        return (
            TaskActivity.objects.filter(task=task, action_type=TaskActivityActionType.DESCRIPTION)
            .order_by("-activity_id")
            .first()
        )

    def scenarios(self):
        """One approvable call per v1 tool, each on its own entities."""
        rewritten = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Rewritten",
            status="Open",
            content=copy.deepcopy(BODY),
        )
        kept = self.agent_rewrite(rewritten, copy.deepcopy(REWRITE))
        return {
            "create_task": {"title": "Fresh", "project_id": self.project.project_id},
            "update_task": {"task_id": self.task.task_id, "title": "Renamed", "priority": "High"},
            "restore_task_body": {"task_id": rewritten.task_id, "activity_id": kept.activity_id},
            "add_comment": {"task_id": self.task.task_id, "body_text": "Looks good"},
            "create_note": {"note_type": "personal", "title": "Made", "content_text": "hello"},
            "update_note": {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": "Plan v3",
                "content_text": "third draft",
            },
            "restore_note_version": {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "version_no": 1,
            },
        }


class TheModelSeesWhatItSawBefore(CaptureBase):
    def test_result_and_replay_are_the_tools_own_return_minus_the_popped_keys(self):
        for name, arguments in self.scenarios().items():
            with self.subTest(tool=name):
                self.loop_messages.clear()
                events, step = self.approve(name, arguments)

                (raw,) = self.raw[name]
                self.assertIn("__undo__", raw, "the tool was not asked for its pre-image")
                expected = {k: v for k, v in raw.items() if k not in POPPED}

                self.assertEqual(step.result_json, expected)
                self.assertEqual(step.summary, raw["__summary__"])
                # The turn the resumed loop is handed: what the model reads next.
                replay = self.loop_messages[-1][-1]
                self.assertEqual(replay.role, "tool_response")
                self.assertEqual(replay.function_response, fence_for_model(expected, name))
                # And the tool result event the browser gets carries no pre-image either.
                event = self.result_event(events)
                self.assertNotIn("__undo__", json.dumps(event))

    def test_the_pre_image_is_nowhere_in_result_json(self):
        for name, arguments in self.scenarios().items():
            with self.subTest(tool=name):
                _events, step = self.approve(name, arguments)
                self.assertIsNotNone(step.undo_json)
                self.assertNotIn("__undo__", step.result_json)
                text = json.dumps(step.result_json)
                for needle in (
                    "fingerprint",
                    "body_hash",
                    "after_hash",
                    "head_version_no",
                    "captured_at",
                    '"image"',
                    '"entity"',
                    '"pre"',
                    '"post"',
                ):
                    self.assertNotIn(needle, text)

    def test_a_later_pause_replays_the_same_function_response(self):
        """`_rebuild_messages` replays `result_json` of every step before a later pause."""
        events, step = self.approve(
            "update_task", {"task_id": self.task.task_id, "title": "Renamed"}
        )
        step.run.status = "awaiting_approval"
        step.run.save(update_fields=["status"])
        AgentStep.objects.create(
            run=step.run,
            step_index=1,
            tool_name="create_task",
            arguments_json={"title": "x", "project_id": self.project.project_id},
            summary=controller.PENDING_APPROVAL_MARKER,
        )
        messages, _pending = controller._rebuild_messages(step.run)
        replayed = [m for m in messages if m.role == "tool_response"]
        (raw,) = self.raw["update_task"]
        expected = {k: v for k, v in raw.items() if k not in POPPED}
        self.assertEqual(replayed[0].function_response, fence_for_model(expected, "update_task"))


class OnlyTheApprovePathAsksForAPreImage(CaptureBase):
    def test_a_direct_call_returns_what_it_always_did(self):
        from origin.search_engine.agent.tools.add_comment import ADD_COMMENT
        from origin.search_engine.agent.tools.create_task import CREATE_TASK
        from origin.search_engine.agent.tools.update_task import UPDATE_TASK

        for tool, args in (
            (CREATE_TASK, {"title": "Direct", "project_id": self.project.project_id}),
            (UPDATE_TASK, {"task_id": self.task.task_id, "title": "Direct title"}),
            (ADD_COMMENT, {"task_id": self.task.task_id, "body_text": "hi"}),
        ):
            with self.subTest(tool=tool.name):
                self.assertNotIn("__undo__", tool.run(args, self.ctx))

    def test_the_context_flag_is_off_unless_the_approve_path_sets_it(self):
        self.assertFalse(ToolContext(team_id="t", user_id="u").capture_undo)
        seen = []

        def run(args, ctx):
            seen.append(ctx.capture_undo)
            return {"__summary__": "done"}

        fake = Tool(
            name="create_task",
            description="",
            parameters_schema=REGISTRY["create_task"].parameters_schema,
            run=run,
            requires_approval=True,
        )
        with mock.patch.dict(REGISTRY, {"create_task": fake}):
            run_, token, _ = self.pause("create_task", {"title": "x", "project_id": 1})
            self.events(self.post(run_, token))
        self.assertEqual(seen, [True])


class EachV1ToolStoresWhatItsUndoNeeds(CaptureBase):
    def stored(self, tool, arguments, **kw):
        events, step = self.approve(tool, arguments, **kw)
        event = self.result_event(events)
        return events, step, event

    def assertStamp(self, step, event, tool):
        body = step.undo_json
        self.assertEqual(body["v"], undo.SCHEMA_VERSION)
        self.assertEqual(body["tool"], tool)
        written = undo.captured_at(body)
        self.assertLess(abs((timezone.now() - written).total_seconds()), 60)
        self.assertEqual(
            event["undo"],
            {"step_id": step.step_id, "available_until": (written + timedelta(days=7)).isoformat()},
        )
        self.assertIsNone(step.undone_at)

    def test_create_task_stamps_the_task_as_made(self):
        _events, step, event = self.stored(
            "create_task",
            {
                "title": "Fresh",
                "project_id": self.project.project_id,
                "priority": "High",
                "content_text": "draft",
            },
        )
        task = TaskMaster.objects.get(title="Fresh")
        self.assertStamp(step, event, "create_task")
        image = step.undo_json["image"]
        self.assertEqual(image["task_id"], task.task_id)
        self.assertEqual(image["fingerprint"], undo.task_fingerprint(task.task_id))
        self.assertEqual(image["fingerprint"]["title"], "Fresh")
        self.assertEqual(image["fingerprint"]["priority"], "High")
        self.assertEqual(
            step.undo_json["entity"],
            {
                "kind": "task",
                "task_id": task.task_id,
                "project_id": self.project.project_id,
                "display_id": task.display_id,
            },
        )

    def test_update_task_keeps_the_old_and_the_new_of_each_field_it_changed(self):
        _events, step, event = self.stored(
            "update_task",
            {
                "task_id": self.task.task_id,
                "title": "Renamed",
                "priority": "High",
                "status": "Open",  # already Open: not a change, not in the pre-image
                "due_date": "2026-12-01",
                "tags": [],
            },
        )
        self.assertStamp(step, event, "update_task")
        image = step.undo_json["image"]
        self.assertEqual(image["task_id"], self.task.task_id)
        self.assertEqual(
            image["fields"],
            {
                "title": {"before": "Seed", "after": "Renamed"},
                "priority": {"before": "Low", "after": "High"},
                "due_date": {"before": None, "after": "2026-12-01"},
            },
        )
        self.assertIsNone(image["body"])

    def test_update_task_points_at_the_history_row_that_kept_the_old_body(self):
        _events, step, event = self.stored(
            "update_task", {"task_id": self.task.task_id, "content_text": "a new body"}
        )
        self.assertStamp(step, event, "update_task")
        body = step.undo_json["image"]["body"]
        self.assertEqual(body["mode"], "row")
        row = TaskActivity.objects.get(activity_id=body["activity_id"])
        self.assertEqual(row.task_id, self.task.task_id)
        self.assertEqual(row.old_value, BODY)
        self.assertTrue(row.metadata["previousBodyKept"])
        self.assertEqual(
            body["after_hash"], undo.state_hash(TaskMaster.objects.get(pk=self.task.pk).content)
        )
        # The text itself is not copied into the undo.
        self.assertNotIn("original spec", json.dumps(step.undo_json))

    def test_update_task_keeps_the_old_body_even_when_the_surface_would_not(self):
        """The history keeps a body only for the agent and API keys, by `via`. The write asks
        for it itself, so the undo does not depend on who the signal thinks is writing."""
        from origin.search_engine.agent.tools.update_task import UPDATE_TASK

        ctx = dataclasses.replace(self.ctx, capture_undo=True)
        set_current_user(self.user, via={"kind": "app"})
        try:
            out = UPDATE_TASK.run({"task_id": self.task.task_id, "content_text": "newer"}, ctx)
        finally:
            clear_current_user()
        self.assertEqual(out["__undo__"]["image"]["body"]["mode"], "row")

    def test_update_task_on_a_task_with_no_body_needs_no_row(self):
        bare = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Bare", status="Open"
        )
        self.assertIsNone(bare.content)
        _events, step, _event = self.stored(
            "update_task", {"task_id": bare.task_id, "body_append": "first words"}
        )
        body = step.undo_json["image"]["body"]
        self.assertEqual(body["mode"], "none")
        self.assertIsNone(body["activity_id"])

    def test_update_task_is_not_undoable_when_it_touches_what_undo_cannot_put_back(self):
        calls = {
            "collaborators": {"collaborators": [str(self.other.id)]},
            "custom_fields": None,  # no field defined: covered by the predicate test; skipped here
            "parent": {"parent_task_id": ""},
        }
        for name, extra in calls.items():
            if extra is None:
                continue
            with self.subTest(argument=name):
                events, step, event = self.stored(
                    "update_task", {"task_id": self.task.task_id, "title": f"x {name}", **extra}
                )
                self.assertIsNone(step.undo_json)
                self.assertNotIn("undo", event)

    def test_a_milestones_backing_task_is_not_undoable(self):
        TaskMaster.objects.filter(pk=self.task.pk).update(is_milestone=True)
        _events, step, event = self.stored(
            "update_task", {"task_id": self.task.task_id, "title": "Renamed"}
        )
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).title, "Renamed")
        self.assertIsNone(step.undo_json)
        self.assertNotIn("undo", event)

    def test_a_write_that_changed_nothing_stores_nothing(self):
        _events, step, event = self.stored(
            "update_task", {"task_id": self.task.task_id, "title": "Seed", "status": "Open"}
        )
        self.assertIsNone(step.undo_json)
        self.assertNotIn("undo", event)
        _events, step, event = self.stored(
            "update_note",
            {"note_id": self.note.note_id, "note_type": "personal", "title": "Plan v2"},
        )
        self.assertIsNone(step.undo_json)
        self.assertNotIn("undo", event)

    def test_restore_task_body_keeps_the_body_it_replaced(self):
        rewritten = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Rewritten",
            status="Open",
            content=copy.deepcopy(BODY),
        )
        kept = self.agent_rewrite(rewritten, copy.deepcopy(REWRITE))
        _events, step, event = self.stored(
            "restore_task_body", {"task_id": rewritten.task_id, "activity_id": kept.activity_id}
        )
        self.assertStamp(step, event, "restore_task_body")
        body = step.undo_json["image"]["body"]
        self.assertEqual(body["mode"], "row")
        self.assertNotEqual(body["activity_id"], kept.activity_id)
        row = TaskActivity.objects.get(activity_id=body["activity_id"])
        self.assertEqual(row.old_value, REWRITE)  # the body the restore replaced
        self.assertEqual(
            TaskMaster.objects.get(pk=rewritten.pk).content, BODY
        )  # and the restore did its job

    def test_add_comment_stamps_the_comment_as_written(self):
        _events, step, event = self.stored(
            "add_comment", {"task_id": self.task.task_id, "body_text": "Looks good"}
        )
        self.assertStamp(step, event, "add_comment")
        comment = TaskComments.objects.get(task=self.task)
        image = step.undo_json["image"]
        self.assertEqual(image["comment_id"], comment.comment_id)
        self.assertEqual(image["body_hash"], undo.state_hash(comment.comment_body))
        self.assertEqual(step.undo_json["entity"]["kind"], "comment")

    def test_create_note_stamps_the_note_as_made_and_who_holds_a_role_on_it(self):
        _events, step, event = self.stored(
            "create_note", {"note_type": "personal", "title": "Made", "content_text": "hello"}
        )
        note = PersonalNoteMaster.objects.get(title="Made")
        self.assertStamp(step, event, "create_note")
        image = step.undo_json["image"]
        self.assertEqual((image["family"], image["note_id"]), ("personal", note.note_id))
        self.assertEqual(image["state"], undo.live_note_state("personal", note.note_id))
        self.assertEqual(image["head_version_no"], 1)
        self.assertEqual(image["access"], undo.note_access_hash("personal", note.note_id))

    def test_update_note_names_the_version_it_replaced_and_the_one_it_wrote(self):
        _events, step, event = self.stored(
            "update_note",
            {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": "Plan v3",
                "content_text": "third draft",
            },
        )
        self.assertStamp(step, event, "update_note")
        image = step.undo_json["image"]
        self.assertEqual(image["pre"]["version_no"], self.v2.version_no)
        self.assertEqual(image["post"]["version_no"], self.v2.version_no + 1)
        self.assertEqual(image["pre"]["state"], undo.version_state(self.v2))
        self.assertEqual(
            image["post"]["state"], undo.live_note_state("personal", self.note.note_id)
        )
        self.assertNotEqual(image["pre"]["state"], image["post"]["state"])
        self.assertNotIn("second draft", json.dumps(step.undo_json))

    def test_update_note_keeps_the_state_it_replaces_when_no_version_holds_it(self):
        """A note nobody saved through the app has no version row, or only an older one: the
        edit keeps the current state first (`keep_current_state`), and the undo names it."""
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(
            body=blocks("typed in the editor, never versioned")
        )
        _events, step, _event = self.stored(
            "update_note",
            {"note_id": self.note.note_id, "note_type": "personal", "content_text": "agent text"},
        )
        pre = NoteVersionMaster.objects.get(
            note_type=PERSONAL,
            note_id=self.note.note_id,
            version_no=step.undo_json["image"]["pre"]["version_no"],
        )
        self.assertEqual(pre.body, blocks("typed in the editor, never versioned"))

    def test_update_note_with_a_folder_move_is_not_undoable(self):
        from origin.models.note.personal_note_models import PersonalNoteFolder

        folder = PersonalNoteFolder.objects.create(
            team=self.team, owner=self.user, name="Archive", scope=PersonalNoteFolder.SCOPE_PERSONAL
        )
        _events, step, event = self.stored(
            "update_note",
            {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": "Plan v3",
                "folder_id": folder.folder_id,
            },
        )
        self.assertEqual(PersonalNoteMaster.objects.get(pk=self.note.pk).title, "Plan v3")
        self.assertIsNone(step.undo_json)
        self.assertNotIn("undo", event)

    def test_restore_note_version_names_the_state_it_replaced(self):
        _events, step, event = self.stored(
            "restore_note_version",
            {"note_id": self.note.note_id, "note_type": "personal", "version_no": 1},
        )
        self.assertStamp(step, event, "restore_note_version")
        image = step.undo_json["image"]
        self.assertEqual(image["pre"]["version_no"], self.v2.version_no)
        self.assertEqual(image["pre"]["state"], undo.version_state(self.v2))
        self.assertEqual(image["post"]["version_no"], self.v2.version_no + 1)
        self.assertEqual(image["post"]["state"], undo.version_state(self.v1))

    def test_restore_note_version_replaces_a_state_no_version_held(self):
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(body=blocks("unsaved typing"))
        _events, step, _event = self.stored(
            "restore_note_version",
            {"note_id": self.note.note_id, "note_type": "personal", "version_no": 1},
        )
        image = step.undo_json["image"]
        pre = NoteVersionMaster.objects.get(
            note_type=PERSONAL, note_id=self.note.note_id, version_no=image["pre"]["version_no"]
        )
        self.assertEqual(pre.body, blocks("unsaved typing"))
        self.assertEqual(image["post"]["version_no"], image["pre"]["version_no"] + 1)

    def test_a_chat_note_and_a_task_note_are_covered_too(self):
        from origin.models.note.task_note_models import TaskNoteMaster

        _events, step, _event = self.stored(
            "create_note",
            {
                "note_type": "task",
                "title": "Plan doc",
                "content_text": "text",
                "project_id": self.project.project_id,
            },
        )
        note = TaskNoteMaster.objects.get(title="Plan doc")
        self.assertEqual(step.undo_json["image"]["family"], "task")
        self.assertEqual(step.undo_json["image"]["note_id"], note.note_id)
        self.assertEqual(ChatNoteMaster.objects.count(), 0)


class ThePredictionAndTheFactAgree(CaptureBase):
    def test_every_scenario_the_card_calls_undoable_gets_an_undo(self):
        """`undoable` is a prediction and `undo` the fact: for the covered tools they agree."""
        for name, arguments in self.scenarios().items():
            with self.subTest(tool=name):
                self.assertTrue(undo.proposal(name, arguments).undoable)
                events, _step = self.approve(name, arguments)
                self.assertIn("undo", self.result_event(events))

    def test_a_call_the_card_calls_not_undoable_gets_no_undo(self):
        calls = {
            "update_task": {"task_id": self.task.task_id, "collaborators": [str(self.other.id)]},
        }
        for name, arguments in calls.items():
            with self.subTest(tool=name):
                self.assertFalse(undo.proposal(name, arguments).undoable)
                events, step = self.approve(name, arguments)
                self.assertNotIn("undo", self.result_event(events))
                self.assertIsNone(step.undo_json)


class AnEditedApprovalCapturesTheStateBeforeTheEditedWrite(CaptureBase):
    def test_update_task(self):
        events, step = self.approve(
            "update_task",
            {"task_id": self.task.task_id, "title": "Proposed", "priority": "Critical"},
            edited_args={"title": "Edited title", "priority": "High"},
        )
        self.assertIs(self.result_event(events)["edited"], True)
        fields = step.undo_json["image"]["fields"]
        # What was there before: not the proposal, which never ran.
        self.assertEqual(fields["title"], {"before": "Seed", "after": "Edited title"})
        self.assertEqual(fields["priority"], {"before": "Low", "after": "High"})

    def test_update_note(self):
        events, step = self.approve(
            "update_note",
            {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": "Proposed",
                "content_text": "proposed text",
            },
            edited_args={"title": "Edited title", "content_text": "edited text"},
        )
        self.assertIs(self.result_event(events)["edited"], True)
        image = step.undo_json["image"]
        self.assertEqual(image["pre"]["state"], undo.version_state(self.v2))
        edited = PersonalNoteMaster.objects.get(pk=self.note.pk)
        self.assertEqual(edited.title, "Edited title")
        self.assertEqual(image["post"]["state"], undo.note_state_hash(edited.title, edited.body))

    def test_create_task(self):
        events, step = self.approve(
            "create_task",
            {"title": "Proposed", "project_id": self.project.project_id},
            edited_args={"title": "Edited title"},
        )
        task = TaskMaster.objects.get(title="Edited title")
        self.assertEqual(step.undo_json["image"]["task_id"], task.task_id)
        self.assertEqual(step.undo_json["image"]["fingerprint"]["title"], "Edited title")

    def test_a_rejected_write_stores_nothing(self):
        run, token, step = self.pause("create_task", {"title": "x", "project_id": 1})
        self.events(self.post(run, token, decision="reject"))
        step.refresh_from_db()
        self.assertIsNone(step.undo_json)


class StoringTheUndoNeverFailsTheWrite(CaptureBase):
    def test_a_failure_to_store_it_loses_the_undo_and_nothing_else(self):
        real_save = AgentStep.save

        def failing_save(step, *args, update_fields=None, **kwargs):
            if update_fields == ["undo_json"]:
                raise RuntimeError("disk")
            return real_save(step, *args, update_fields=update_fields, **kwargs)

        with mock.patch.object(AgentStep, "save", failing_save):
            events, step = self.approve(
                "update_task", {"task_id": self.task.task_id, "title": "Renamed"}
            )
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).title, "Renamed")
        event = self.result_event(events)
        self.assertNotIn("undo", event)  # a promise only for an undo that is there
        self.assertIsNone(step.undo_json)
        self.assertEqual(step.result_json["task_id"], self.task.task_id)


class TheCardIsToldBeforeApproving(TestCase):
    """The pause event, driven through the real loop with a stand-in for the model.
    (A plain `TestCase`: `DecideEditBase` replaces the loop.)"""

    def setUp(self):
        from django.contrib.auth import get_user_model

        from origin.models.common.team_models import TeamMaster

        self.user = get_user_model().objects.create_user(
            username="carder", email="carder@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.ctx = ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id))
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Website",
            code="WEB",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=self.project, attendee=self.user)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Seed", status="Open"
        )

    @staticmethod
    def of_type(events, kind):
        return [e for e in events if e["type"] == kind]

    def test_a_read_that_returns_a_pre_image_anyway_does_not_leak_it(self):
        leaky = Tool(
            name="peek",
            description="",
            parameters_schema={"type": "OBJECT", "properties": {}},
            run=lambda args, ctx: {"__summary__": "s", "ok": True, "__undo__": {"image": {"a": 1}}},
        )

        class Client:
            def __init__(self):
                self.seen = []

            def generate_step(self, messages, tools, system_instruction, **kwargs):
                self.seen.append(list(messages))
                if len(self.seen) == 1:
                    yield (None, FunctionCall(name="peek", args={}))
                else:
                    yield ("done", None)

        client = Client()
        events = []
        with (
            mock.patch.object(controller, "REGISTRY", {"peek": leaky}),
            mock.patch.object(controller, "get_model_client", return_value=client),
        ):
            controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=self.ctx,
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        replay = [m for m in client.seen[1] if m.role == "tool_response"][0]
        self.assertEqual(replay.function_response, fence_for_model({"ok": True}, "peek"))
        self.assertNotIn("__undo__", json.dumps(events))

    def pending(self, name, args):
        fake = Tool(
            name=name,
            description="",
            parameters_schema=REGISTRY[name].parameters_schema,
            run=lambda a, c: {},
            requires_approval=True,
        )

        class Client:
            def generate_step(self, messages, tools, system_instruction, **kwargs):
                yield (None, FunctionCall(name=name, args=args))

        events = []
        with (
            mock.patch.object(controller, "REGISTRY", {name: fake}),
            mock.patch.object(controller, "get_model_client", return_value=Client()),
        ):
            controller._drive_loop(
                messages=[AgentMessage(role="user", text="q")],
                ctx=self.ctx,
                emit=events.append,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        (event,) = self.of_type(events, "tool_call_pending_approval")
        json.dumps(event)
        return event

    def test_an_undoable_call_says_so_and_gives_no_reason(self):
        event = self.pending("create_task", {"title": "x", "project_id": self.project.project_id})
        self.assertIs(event["undoable"], True)
        self.assertNotIn("undo_reason", event)
        event = self.pending("update_task", {"task_id": self.task.task_id, "title": "x"})
        self.assertIs(event["undoable"], True)

    def test_a_call_that_cannot_be_undone_says_why(self):
        cases = (
            ("delete_project", {"project_id": 5}, "irreversible_delete"),
            ("delete_note", {"note_type": "personal", "note_id": 5}, "irreversible_delete"),
            ("approve_join_request", {"inbox_item_id": 1}, "notified_people"),
            ("manage_project_members", {"project_id": 1}, "not_yet"),
            ("manage_note_sharing", {"note_type": "personal", "note_id": 1}, "not_yet"),
            ("update_comment", {"task_id": 1, "comment_id": 1, "body_text": "x"}, "no_snapshot"),
            ("update_tasks_bulk", {"updates": []}, "not_yet"),
            (
                "update_task",
                {"task_id": self.task.task_id, "collaborators": []},
                "unsupported_fields",
            ),
            (
                "update_note",
                {"note_id": 1, "note_type": "personal", "folder_id": None},
                "unsupported_fields",
            ),
        )
        for name, args, reason in cases:
            with self.subTest(tool=name):
                event = self.pending(name, args)
                self.assertIs(event["undoable"], False)
                self.assertEqual(event["undo_reason"], reason)


class ARestoredTurnShowsItsUndos(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model

        from origin.models.common.team_models import TeamMaster

        self.user = get_user_model().objects.create_user(
            username="restorer", email="restorer@example.com", password="x"
        )
        self.team = TeamMaster.objects.create(
            team_name="T", team_email="t@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.team, attendee=self.user)
        self.session = AgentSession.objects.create(
            team_id=str(self.team.pk), user_id=str(self.user.id)
        )

    def run_with(self, *steps):
        run = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.user.id),
            query="do it",
            status="done",
            final_answer_text="Done.",
            session=self.session,
        )
        made = []
        for index, (tool, body, undone_at) in enumerate(steps):
            made.append(
                AgentStep.objects.create(
                    run=run,
                    step_index=index,
                    tool_name=tool,
                    summary="ok",
                    undo_json=body,
                    undone_at=undone_at,
                )
            )
        return run, made

    def test_each_step_with_an_undo_is_listed_with_its_window_and_state(self):
        now = timezone.now()
        body = undo.stored("create_task", {"image": {"task_id": 1}}, now=now)
        gone = undo.stored("add_comment", {"image": {"task_id": 1}}, now=now)
        run, (first, read, second) = self.run_with(
            ("create_task", body, None), ("list_tasks", None, None), ("add_comment", gone, now)
        )
        (turn,) = agent_views._build_turns_payload(self.session)
        self.assertEqual(turn["run_id"], str(run.run_id))
        self.assertEqual(
            turn["undo"],
            [
                {
                    "step_id": first.step_id,
                    "step": 0,
                    "tool_name": "create_task",
                    "available_until": (now + timedelta(days=7)).isoformat(),
                    "undone_at": None,
                },
                {
                    "step_id": second.step_id,
                    "step": 2,
                    "tool_name": "add_comment",
                    "available_until": (now + timedelta(days=7)).isoformat(),
                    "undone_at": now.isoformat(),
                },
            ],
        )

    def test_a_turn_with_no_undo_has_an_empty_list(self):
        self.run_with(("list_tasks", None, None))
        (turn,) = agent_views._build_turns_payload(self.session)
        self.assertEqual(turn["undo"], [])

    def test_availability_is_not_judged_here(self):
        """Whether it can still be done is checked when it is tried: a stale one is listed."""
        long_ago = timezone.now() - timedelta(days=30)
        self.run_with(
            ("create_task", undo.stored("create_task", {"image": {"a": 1}}, now=long_ago), None)
        )
        (turn,) = agent_views._build_turns_payload(self.session)
        (entry,) = turn["undo"]
        self.assertEqual(entry["available_until"], (long_ago + timedelta(days=7)).isoformat())

    def test_deleting_the_conversation_deletes_the_undo_with_it(self):
        body = undo.stored("create_task", {"image": {"task_id": 1}})
        run, (step,) = self.run_with(("create_task", body, None))
        agent_views._delete_agent_conversations(
            user_id=str(self.user.id), session_id=self.session.session_id
        )
        self.assertFalse(AgentRun.objects.filter(run_id=run.run_id).exists())
        self.assertFalse(AgentStep.objects.filter(step_id=step.step_id).exists())
