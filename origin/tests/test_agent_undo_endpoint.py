"""Undo, the doing half: `POST /api/v2/agent/runs/<run_id>/steps/<step_id>/undo/`.

The capture half (what an approved write keeps) is `test_agent_undo_capture`. Here a real
write is approved through the real endpoint, and then undone through this one, so what is
undone is what a person would have been shown. What these pin, and why each is a trap:

  * ONE 404 for a run that is not there, one that is someone else's, a step that is not
    there and a step of another run: a guessed id confirms nothing;
  * the refusals have machine codes and statuses (`expired` 410, `changed_since`,
    `already_undone`, `not_undoable` 409, `permission_lost` 403), and a refusal changes nothing;
  * "nothing has changed since" is STATE, not time and not a version number: a note's newest
    version is rewritten IN PLACE by its editor's own save within five minutes, so the guard
    compares what the note says now with what the write left;
  * `update_task` puts back field by field: a field somebody else changed since is skipped and
    reported, and only when nothing at all can be put back is it refused;
  * once, under concurrency: two presses run it once;
  * the permission is checked again at undo time, with scenarios that REALLY revoke it (the
    reporter and assignee of a task keep access by that rule, a note's owner always may edit);
  * no tool runs, no model is called, and nothing is sent: no mention, no notification, no
    push, no email, and the stored mention set follows the body quietly;
  * the activity is stamped `{"kind": "app", "undo_of": {...}}`, distinct from the agent's write
    and from a plain edit by the same person.

`TransactionTestCase`, as the capture tests: the approve runs on a real worker thread.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import threading
import time
from datetime import timedelta
from unittest import mock

from django.db import connections
from django.urls import resolve
from django.utils import timezone
from rest_framework.test import APIClient

from origin.middleware import current_user
from origin.models.chat.unified_models import Activity
from origin.models.common.notification_models import EmailNotificationEvent
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_delivery_models import (
    TaskDelivery,
    TaskDeliverySource,
    TaskDeliveryState,
)
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.search_engine.agent import controller, undo, undo_apply
from origin.search_engine.agent.tools import REGISTRY
from origin.search_engine.agent_views import AgentStepUndoView
from origin.search_engine.models import AgentRun, AgentSession, AgentStep
from origin.services.collab_push import PushOutcome
from origin.services.throttles import AgentUndoThrottle
from origin.views.utils.note_role import ROLE_EDITOR, ROLE_VIEWER
from origin.views.utils.note_version import snapshot_note_version

from .test_agent_undo_capture import BODY, PERSONAL, REWRITE, CaptureBase, blocks

STATUS = TaskActivityActionType.STATUS
DESCRIPTION = TaskActivityActionType.DESCRIPTION
TITLE = TaskActivityActionType.TITLE


def text_of(body):
    """The words of a BlockNote body, whatever fields the writer put on its blocks."""
    out = []

    def walk(blocks_):
        for block in blocks_ or []:
            out.extend(part.get("text", "") for part in block.get("content") or [])
            walk(block.get("children"))

    walk(body)
    return "".join(out)


def mention(user):
    return {
        "type": "paragraph",
        "content": [
            {"type": "text", "text": "ping ", "styles": {}},
            {"type": "mention", "props": {"userId": str(user.id)}},
        ],
    }


class UndoBase(CaptureBase):
    def go(self, tool, arguments, edited_args=None, **run_overrides):
        """Approve a real write through the real endpoint; its run and its step."""
        run, token, step = self.pause(tool, arguments, **run_overrides)
        self.events(self.post(run, token, edited_args=edited_args))
        step.refresh_from_db()
        run.refresh_from_db()
        return run, step

    @staticmethod
    def url(run, step):
        step_id = step if isinstance(step, (int, str)) else step.step_id
        return f"/api/v2/agent/runs/{run.run_id}/steps/{step_id}/undo/"

    def undo(self, run, step, client=None):
        return (client or self.client).post(self.url(run, step), {}, format="json")

    def undone(self, run, step):
        """Press Undo and expect it to work."""
        resp = self.undo(run, step)
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def refused(self, run, step, code, status):
        resp = self.undo(run, step)
        self.assertEqual((resp.status_code, resp.json()["code"]), (status, code), resp.content)
        step.refresh_from_db()
        self.assertIsNone(step.undone_at, "a refusal must not claim the step")
        return resp.json()

    def stamp(self, run, step):
        return {"kind": "app", "undo_of": {"run_id": str(run.run_id), "step": step.step_index}}

    def newest(self, task, action):
        return (
            TaskActivity.objects.filter(task=task, action_type=action)
            .order_by("-activity_id")
            .first()
        )

    def someone_else_edits(self, task, **fields):
        """A colleague's edit in the app, after the agent's write."""
        current_user.set_current_user(self.other, via={"kind": "app"})
        try:
            task.refresh_from_db()
            for name, value in fields.items():
                setattr(task, name, value)
            task.save(update_fields=[*fields, "ts_updated_at"])
        finally:
            current_user.clear_current_user()

    def backdate(self, step, **delta):
        body = copy.deepcopy(step.undo_json)
        body["captured_at"] = (timezone.now() - timedelta(**delta)).isoformat()
        AgentStep.objects.filter(pk=step.pk).update(undo_json=body)


class TheRoute(UndoBase):
    def test_it_resolves(self):
        match = resolve("/api/v2/agent/runs/6f0c5e0e-0000-4000-8000-000000000000/steps/12/undo/")
        self.assertIs(match.func.cls, AgentStepUndoView)
        self.assertEqual(
            match.kwargs, {"run_id": "6f0c5e0e-0000-4000-8000-000000000000", "step_id": "12"}
        )

    def test_only_post(self):
        run, step = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "hi"})
        self.assertEqual(self.client.get(self.url(run, step)).status_code, 405)

    def test_signing_in_is_required(self):
        run, step = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "hi"})
        resp = APIClient().post(self.url(run, step), {}, format="json")
        self.assertEqual(resp.status_code, 401)

    def test_it_has_its_own_throttle_and_not_the_llm_one(self):
        from origin.services.throttles import AgentThrottle

        scopes = [t.scope for t in AgentStepUndoView.throttle_classes]
        self.assertIn("agent_undo", scopes)
        self.assertNotIn(AgentThrottle.scope, scopes)
        run, step = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "hi"})
        with mock.patch.object(AgentUndoThrottle, "rate", "2/min"):
            codes = [self.undo(run, 999_999).status_code for _ in range(3)]
        self.assertEqual(codes, [404, 404, 429])

    def test_the_llm_budget_is_not_spent_by_it(self):
        from origin.services.throttles import AgentThrottle

        run, step = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "hi"})
        with mock.patch.object(AgentThrottle, "rate", "1/min"):
            self.assertEqual(self.undo(run, step).status_code, 200)


class NotFoundIsOneAnswer(UndoBase):
    def test_every_way_of_not_finding_it_is_the_same_answer(self):
        run, step = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "one"})
        run2, step2 = self.go("add_comment", {"task_id": self.task.task_id, "body_text": "two"})
        # Someone else's run, with a real step of its own.
        theirs = AgentRun.objects.create(
            team_id=str(self.team.pk),
            user_id=str(self.other.id),
            query="theirs",
            status="done",
        )
        their_step = AgentStep.objects.create(
            run=theirs,
            step_index=0,
            tool_name="add_comment",
            summary="ok",
            undo_json=copy.deepcopy(step.undo_json),
        )
        stranger = self._client(self.other)
        probes = {
            "unknown run": (self.client, "6f0c5e0e-0000-4000-8000-000000000000", step.step_id),
            "not even a uuid": (self.client, "nope", step.step_id),
            "unknown step": (self.client, run.run_id, 99_999_999),
            "not even a number": (self.client, run.run_id, "abc"),
            "a real step of another of my runs": (self.client, run.run_id, step2.step_id),
            "someone else's run, as me": (self.client, theirs.run_id, their_step.step_id),
            "my run, as someone else": (stranger, run.run_id, step.step_id),
            "my step, in their run": (stranger, theirs.run_id, step.step_id),
        }
        answers = {}
        for name, (client, run_id, step_id) in probes.items():
            resp = client.post(
                f"/api/v2/agent/runs/{run_id}/steps/{step_id}/undo/", {}, format="json"
            )
            answers[name] = (resp.status_code, resp.json())
        self.assertEqual({a[0] for a in answers.values()}, {404}, answers)
        self.assertEqual(len({json.dumps(a[1], sort_keys=True) for a in answers.values()}), 1)
        self.assertEqual(next(iter(answers.values()))[1]["code"], "not_found")
        # And none of them did anything.
        for tracked in (step, step2, their_step):
            tracked.refresh_from_db()
            self.assertIsNone(tracked.undone_at)
        self.assertEqual(TaskComments.objects.filter(is_deleted=True).count(), 0)


class RefusalsEveryStepShares(UndoBase):
    def one(self):
        return self.go("add_comment", {"task_id": self.task.task_id, "body_text": "hi"})

    def test_a_step_with_no_undo_is_not_undoable(self):
        run, _step = self.one()
        read = AgentStep.objects.create(
            run=run, step_index=9, tool_name="list_tasks", summary="ok", result_json={}
        )
        body = self.refused(run, read, "not_undoable", 409)
        self.assertTrue(body["error"])

    def test_a_write_that_kept_no_undo_is_not_undoable(self):
        run, step = self.go(
            "update_task", {"task_id": self.task.task_id, "collaborators": [str(self.other.id)]}
        )
        self.assertIsNone(step.undo_json)
        self.refused(run, step, "not_undoable", 409)

    def test_a_tool_with_no_handler_is_not_undoable_whatever_is_stored(self):
        run, _step = self.one()
        step = AgentStep.objects.create(
            run=run,
            step_index=8,
            tool_name="delete_project",
            summary="ok",
            undo_json=undo.stored("delete_project", {"image": {"project_id": 1}}),
        )
        self.refused(run, step, "not_undoable", 409)

    def test_an_undo_stored_for_another_tool_is_not_undoable(self):
        run, step = self.one()
        body = copy.deepcopy(step.undo_json)
        body["tool"] = "create_task"
        AgentStep.objects.filter(pk=step.pk).update(undo_json=body)
        self.refused(run, step, "not_undoable", 409)

    def test_a_shape_this_code_does_not_know_is_not_undoable(self):
        run, step = self.one()
        body = copy.deepcopy(step.undo_json)
        body["v"] = 99
        AgentStep.objects.filter(pk=step.pk).update(undo_json=body)
        self.refused(run, step, "not_undoable", 409)
        body["v"] = undo.SCHEMA_VERSION
        body["image"] = {"unexpected": True}
        AgentStep.objects.filter(pk=step.pk).update(undo_json=body)
        self.refused(run, step, "not_undoable", 409)
        self.assertFalse(TaskComments.objects.get(task=self.task).is_deleted)

    def test_a_second_press_is_already_undone_and_does_nothing(self):
        run, step = self.one()
        self.undone(run, step)
        rows = TaskActivity.objects.filter(task=self.task).count()
        resp = self.undo(run, step)
        self.assertEqual((resp.status_code, resp.json()["code"]), (409, "already_undone"))
        self.assertEqual(TaskActivity.objects.filter(task=self.task).count(), rows)
        step.refresh_from_db()
        self.assertIsNotNone(step.undone_at)  # the first one's claim stands

    def test_after_seven_days_it_is_expired(self):
        run, step = self.one()
        self.backdate(step, days=7, seconds=1)
        body = self.refused(run, step, "expired", 410)
        self.assertIn("7 days", body["error"])
        self.assertFalse(TaskComments.objects.get(task=self.task).is_deleted)

    def test_just_inside_the_window_it_still_works(self):
        run, step = self.one()
        self.backdate(step, days=7, seconds=-30)
        self.undone(run, step)
        self.assertTrue(TaskComments.objects.get(task=self.task).is_deleted)

    def test_the_window_runs_from_the_write_not_from_the_proposal(self):
        """The step row is made when the call is PROPOSED, which can be long before it is
        approved: an old pause must not eat the window of a fresh write."""
        run, token, step = self.pause(
            "add_comment", {"task_id": self.task.task_id, "body_text": "late"}
        )
        AgentStep.objects.filter(pk=step.pk).update(created_at=timezone.now() - timedelta(days=30))
        self.events(self.post(run, token))
        step.refresh_from_db()
        self.assertGreater(step.undo_json and undo.available_until(step.undo_json), timezone.now())
        self.undone(run, step)

    def test_an_undone_step_that_has_also_expired_says_it_was_undone(self):
        run, step = self.one()
        self.undone(run, step)
        self.backdate(step, days=30)
        resp = self.undo(run, step)
        self.assertEqual(resp.json()["code"], "already_undone")

    def test_two_presses_at_once_run_it_once(self):
        run, step = self.one()
        calls = []
        real = undo_apply.HANDLERS["add_comment"]

        def slow(undoing, image):
            calls.append(1)
            time.sleep(0.4)  # long enough for the second request to be waiting on the lock
            return real(undoing, image)

        results = []

        def press():
            try:
                resp = self._client(self.user).post(self.url(run, step), {}, format="json")
                results.append((resp.status_code, resp.json().get("code")))
            finally:
                connections.close_all()

        with mock.patch.dict(undo_apply.HANDLERS, {"add_comment": slow}):
            threads = [threading.Thread(target=press) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
        self.assertEqual(len(calls), 1, "the undo ran twice")
        self.assertEqual(sorted(results, key=str), [(200, None), (409, "already_undone")])
        self.assertTrue(TaskComments.objects.get(task=self.task).is_deleted)
        deletions = TaskActivity.objects.filter(
            task=self.task, action_type=TaskActivityActionType.COMMENT_DELETED
        )
        self.assertEqual(deletions.count(), 1, "the undo's effects happened twice")

    def test_a_refusal_rolls_the_claim_back(self):
        run, step = self.one()
        written = TaskComments.objects.get(task=self.task).comment_body
        TaskComments.objects.filter(task=self.task).update(comment_body=blocks("edited"))
        self.refused(run, step, "changed_since", 409)  # and the step is not claimed
        # Put it back as it was written: the same click now works.
        TaskComments.objects.filter(task=self.task).update(comment_body=written)
        self.undone(run, step)


class UndoingCreateTask(UndoBase):
    def make(self, **extra):
        return self.go(
            "create_task",
            {"title": "Fresh", "project_id": self.project.project_id, "priority": "High", **extra},
        )

    def test_it_deletes_the_task_the_way_the_app_does(self):
        run, step = self.make()
        task = TaskMaster.objects.get(title="Fresh")
        body = self.undone(run, step)

        task.refresh_from_db()
        self.assertEqual(task.status, "Deleted")
        self.assertFalse(task.is_deleted)  # the app's delete is the status; the row stays
        self.assertEqual(body["action"], "deleted")
        self.assertEqual(body["restored"], [])
        self.assertEqual(body["skipped"], [])
        self.assertEqual(body["run_id"], str(run.run_id))
        self.assertEqual(body["step_id"], step.step_id)
        self.assertEqual(body["tool_name"], "create_task")
        self.assertEqual(
            body["entity"],
            {
                "kind": "task",
                "task_id": task.task_id,
                "project_id": self.project.project_id,
                "display_id": task.display_id,
            },
        )
        step.refresh_from_db()
        self.assertEqual(step.undone_at.isoformat(), body["undone_at"])

    def test_the_history_tells_it_from_the_agents_write_and_from_an_edit(self):
        run, step = self.make()
        task = TaskMaster.objects.get(title="Fresh")
        self.undone(run, step)
        created = self.newest(task, TaskActivityActionType.CREATED)
        self.assertEqual(created.metadata["via"], {"kind": "agent"})
        deleted = self.newest(task, STATUS)
        self.assertEqual(deleted.metadata["via"], self.stamp(run, step))
        self.assertEqual((deleted.old_value, deleted.new_value), ("Open", "Deleted"))
        self.assertEqual(str(deleted.actor_id), str(self.user.id))

    def test_it_is_refused_once_anything_about_the_task_has_changed(self):
        changes = {
            "its title": lambda t: self.someone_else_edits(t, title="Renamed by a colleague"),
            "its status": lambda t: self.someone_else_edits(t, status="WIP"),
            "its priority": lambda t: self.someone_else_edits(t, priority="Low"),
            "its assignee": lambda t: self.someone_else_edits(t, assignee=self.other),
            "its body": lambda t: self.someone_else_edits(t, content=copy.deepcopy(REWRITE)),
            "its collaborators": lambda t: t.collaborators.add(self.other),
            "a comment on it": lambda t: TaskComments.objects.create(
                task=t, sender=self.other, comment_id=1, comment_body=blocks("hey")
            ),
            "a task under it": lambda t: TaskMaster.objects.create(
                team=self.team,
                project=self.project,
                reporter=self.user,
                title="Child",
                status="Open",
                parent_task_id=t.task_id,
            ),
            "a task that depends on it": lambda t: TaskDependency.objects.create(
                team=self.team,
                blocker_task=t,
                blocked_task=self.task,
                created_by=self.other,
            ),
            "a task it depends on": lambda t: TaskDependency.objects.create(
                team=self.team,
                blocker_task=self.task,
                blocked_task=t,
                created_by=self.other,
            ),
            "a pull request linked to it": lambda t: TaskDelivery.objects.create(
                team=self.team,
                task=t,
                repo_owner="genos-tech",
                repo_name="genos-api",
                branch="fix",
                state=TaskDeliveryState.BRANCH,
                source=TaskDeliverySource.MANUAL,
                created_by=self.other,
            ),
        }
        for what, change in changes.items():
            with self.subTest(change=what):
                run, step = self.go(
                    "create_task",
                    {"title": f"Fresh {what}", "project_id": self.project.project_id},
                )
                task = TaskMaster.objects.get(title=f"Fresh {what}")
                change(task)
                body = self.refused(run, step, "changed_since", 409)
                self.assertIn("Nothing was undone", body["error"])
                self.assertNotEqual(TaskMaster.objects.get(pk=task.pk).status, "Deleted")

    def test_writes_on_the_task_taken_back_first_do_not_block_undoing_its_creation(self):
        """Newest first: a comment was added to the new task, and then undone. The task's
        history now has the comment's rows above where it stood when it was made, and a
        guard that looked at the history would refuse the creation's undo for good."""
        run, step = self.make()
        task = TaskMaster.objects.get(title="Fresh")
        comment_run, comment_step = self.go(
            "add_comment", {"task_id": task.task_id, "body_text": "On the new task"}
        )
        self.refused(run, step, "changed_since", 409)  # while the comment stands
        self.undone(comment_run, comment_step)
        self.undone(run, step)
        self.assertEqual(TaskMaster.objects.get(pk=task.pk).status, "Deleted")

    def test_a_task_deleted_since_is_changed_not_deleted_twice(self):
        run, step = self.make()
        task = TaskMaster.objects.get(title="Fresh")
        self.someone_else_edits(task, status="Deleted")
        self.refused(run, step, "changed_since", 409)

    def test_a_task_that_is_gone_is_changed(self):
        run, step = self.make()
        TaskMaster.objects.filter(title="Fresh").delete()
        self.refused(run, step, "changed_since", 409)

    def test_the_sessions_cached_reads_are_dropped(self):
        session = AgentSession.objects.create(team_id=str(self.team.pk), user_id=str(self.user.id))
        run, step = self.go(
            "create_task",
            {"title": "Fresh", "project_id": self.project.project_id},
            session=session,
        )
        with mock.patch.object(undo_apply.tool_cache, "invalidate_session") as invalidate:
            self.undone(run, step)
        invalidate.assert_called_once_with(str(session.session_id))


class UndoingUpdateTask(UndoBase):
    def write(self, **fields):
        return self.go("update_task", {"task_id": self.task.task_id, **fields})

    def fresh(self):
        return TaskMaster.objects.get(pk=self.task.pk)

    def test_it_puts_back_what_the_call_changed_and_only_that(self):
        run, step = self.write(title="Renamed", priority="High", due_date="2026-12-01")
        self.someone_else_edits(self.task, effort_level="Extensive")  # not this call's field
        body = self.undone(run, step)

        task = self.fresh()
        self.assertEqual((task.title, task.priority, task.due_date), ("Seed", "Low", None))
        self.assertEqual(task.effort_level, "Extensive")  # untouched
        self.assertEqual(body["action"], "restored")
        self.assertEqual(body["restored"], ["title", "priority", "due_date"])
        self.assertEqual(body["skipped"], [])
        self.assertEqual(body["entity"]["task_id"], self.task.task_id)

    def test_a_field_somebody_changed_since_is_skipped_and_reported(self):
        run, step = self.write(title="Renamed", priority="High", status="WIP")
        self.someone_else_edits(self.task, priority="Critical")
        body = self.undone(run, step)

        task = self.fresh()
        self.assertEqual((task.title, task.status), ("Seed", "Open"))
        self.assertEqual(task.priority, "Critical")  # theirs, left alone
        self.assertEqual(body["restored"], ["title", "status"])
        self.assertEqual(body["skipped"], [{"field": "priority", "reason": "changed_since"}])
        step.refresh_from_db()
        self.assertIsNotNone(step.undone_at)

    def test_when_nothing_can_be_put_back_it_is_changed_since(self):
        run, step = self.write(title="Renamed", priority="High")
        self.someone_else_edits(self.task, title="Theirs", priority="Critical")
        body = self.refused(run, step, "changed_since", 409)
        self.assertEqual(sorted(s["field"] for s in body["skipped"]), ["priority", "title"])
        task = self.fresh()
        self.assertEqual((task.title, task.priority), ("Theirs", "Critical"))

    def test_the_users_own_edit_in_the_app_counts_as_a_change_too(self):
        run, step = self.write(title="Renamed")
        current_user.set_current_user(self.user, via={"kind": "app"})
        try:
            task = self.fresh()
            task.title = "Renamed again, by me"
            task.save(update_fields=["title", "ts_updated_at"])
        finally:
            current_user.clear_current_user()
        self.refused(run, step, "changed_since", 409)
        self.assertEqual(self.fresh().title, "Renamed again, by me")

    def test_status_dates_and_a_cleared_start_date(self):
        self.someone_else_edits(self.task, start_date=timezone.now().date())
        before = self.fresh().start_date
        run, step = self.write(status="Closed", start_date="", due_date="2026-12-31")
        self.assertIsNone(self.fresh().start_date)
        self.undone(run, step)
        task = self.fresh()
        self.assertEqual((task.status, task.start_date, task.due_date), ("Open", before, None))

    def test_tags_are_compared_as_a_set_and_put_back(self):
        for index, name in enumerate(("bug", "ui")):
            ProjectTags.objects.create(
                team=self.team,
                project=self.project,
                tag_id=index + 1,
                tag_name=name,
                tag_color="red",
                tag_text_color="white",
            )
        run, step = self.write(tags=["bug"])
        self.assertEqual([t["tagName"] for t in self.fresh().tags], ["bug"])
        self.undone(run, step)
        self.assertFalse(self.fresh().tags)

        run, step = self.write(tags=["bug", "ui"])
        self.someone_else_edits(self.task, tags=list(reversed(self.fresh().tags)))
        self.undone(run, step)  # same tags, another order: not a change
        self.assertFalse(self.fresh().tags)

        run, step = self.write(tags=["bug"])
        self.someone_else_edits(self.task, tags=[self.fresh().tags[0], {"tagName": "ui"}])
        self.refused(run, step, "changed_since", 409)

    def test_the_body_comes_back_from_the_history_row_that_kept_it(self):
        run, step = self.write(content_text="Entirely new text")
        before = self.fresh().ts_updated_at
        with mock.patch("origin.services.collab_push.push_task_body") as push:
            body = self.undone(run, step)
        task = self.fresh()
        self.assertEqual(task.content, BODY)
        self.assertEqual(body["restored"], ["body"])
        push.assert_called_once_with(self.task.task_id, BODY)  # the open editor follows
        self.assertGreater(task.ts_updated_at, before)  # and the sync watermark moved
        row = self.newest(self.task, DESCRIPTION)
        self.assertEqual(row.metadata["via"], self.stamp(run, step))
        self.assertTrue(row.metadata["previousBodyKept"])  # an undo is itself restorable

    def test_a_task_with_no_body_gets_its_empty_body_back(self):
        bare = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Bare", status="Open"
        )
        run, step = self.go("update_task", {"task_id": bare.task_id, "body_append": "first words"})
        self.assertTrue(TaskMaster.objects.get(pk=bare.pk).content)
        self.undone(run, step)
        self.assertIsNone(TaskMaster.objects.get(pk=bare.pk).content)

    def test_a_body_edited_since_is_skipped_and_the_rest_is_put_back(self):
        run, step = self.write(title="Renamed", content_text="Entirely new text")
        self.someone_else_edits(self.task, content=copy.deepcopy(REWRITE))
        body = self.undone(run, step)
        task = self.fresh()
        self.assertEqual(task.title, "Seed")
        self.assertEqual(task.content, REWRITE)
        self.assertEqual(body["restored"], ["title"])
        self.assertEqual(body["skipped"], [{"field": "body", "reason": "changed_since"}])

    def test_a_body_whose_history_row_is_gone_cannot_be_put_back(self):
        run, step = self.write(content_text="Entirely new text")
        TaskActivity.objects.filter(
            activity_id=step.undo_json["image"]["body"]["activity_id"]
        ).delete()
        body = self.refused(run, step, "changed_since", 409)
        self.assertEqual(body["skipped"], [{"field": "body", "reason": "changed_since"}])

    def test_a_task_deleted_since_cannot_be_undone(self):
        run, step = self.write(title="Renamed")
        TaskMaster.objects.filter(pk=self.task.pk).update(is_deleted=True)
        self.refused(run, step, "changed_since", 409)
        TaskMaster.objects.filter(pk=self.task.pk).delete()
        self.refused(run, step, "changed_since", 409)

    def test_the_history_rows_of_the_undo_carry_its_stamp(self):
        run, step = self.write(title="Renamed", priority="High")
        self.undone(run, step)
        for action in (TITLE, TaskActivityActionType.PRIORITY):
            with self.subTest(action=action):
                row = self.newest(self.task, action)
                self.assertEqual(row.metadata["via"], self.stamp(run, step))
                self.assertEqual(str(row.actor_id), str(self.user.id))
        written = TaskActivity.objects.filter(task=self.task, action_type=TITLE).order_by(
            "activity_id"
        )
        self.assertEqual(written[0].metadata["via"], {"kind": "agent"})

    def test_an_edited_approval_is_undone_to_the_state_before_the_edited_write(self):
        run, step = self.go(
            "update_task",
            {"task_id": self.task.task_id, "title": "Proposed", "priority": "Critical"},
            edited_args={"title": "Edited title", "priority": "High"},
        )
        self.assertEqual(self.fresh().title, "Edited title")
        self.undone(run, step)
        task = self.fresh()
        self.assertEqual((task.title, task.priority), ("Seed", "Low"))


class UndoingRestoreTaskBody(UndoBase):
    def restore(self):
        rewritten = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            reporter=self.user,
            title="Rewritten",
            status="Open",
            content=copy.deepcopy(BODY),
        )
        kept = self.agent_rewrite(rewritten, copy.deepcopy(REWRITE))
        run, step = self.go(
            "restore_task_body", {"task_id": rewritten.task_id, "activity_id": kept.activity_id}
        )
        return rewritten, run, step

    def test_it_restores_again(self):
        task, run, step = self.restore()
        self.assertEqual(TaskMaster.objects.get(pk=task.pk).content, BODY)
        body = self.undone(run, step)
        self.assertEqual(TaskMaster.objects.get(pk=task.pk).content, REWRITE)
        self.assertEqual(body["restored"], ["body"])
        self.assertEqual(body["tool_name"], "restore_task_body")

    def test_it_is_refused_once_the_body_has_been_edited(self):
        task, run, step = self.restore()
        self.someone_else_edits(task, content=blocks("typed since"))
        self.refused(run, step, "changed_since", 409)
        self.assertEqual(TaskMaster.objects.get(pk=task.pk).content, blocks("typed since"))


class UndoingAddComment(UndoBase):
    def test_it_deletes_the_comment_softly_through_the_service(self):
        TaskComments.objects.create(
            task=self.task, sender=self.other, comment_id=1, comment_body=blocks("first")
        )
        run, step = self.go(
            "add_comment", {"task_id": self.task.task_id, "body_text": "Looks good"}
        )
        comment = TaskComments.objects.get(task=self.task, comment_id=2)
        with (
            mock.patch(
                "origin.services.task_comment_manage.unified_writer."
                "soft_delete_task_comment_as_thread_reply"
            ) as mirror,
            mock.patch("origin.services.task_comment_manage.purge_task_comment") as purge,
        ):
            body = self.undone(run, step)
        comment.refresh_from_db()
        self.assertTrue(comment.is_deleted)  # soft: its number is not reused
        mirror.assert_called_once_with(task_id=self.task.task_id, comment_id=2)
        purge.assert_called_once_with(self.task.task_id, 2)
        self.assertEqual(body["action"], "deleted")
        self.assertEqual(body["comment_count"], 1)  # the page sets its chip from it
        self.assertEqual(
            body["entity"],
            {
                "kind": "comment",
                "task_id": self.task.task_id,
                "project_id": self.project.project_id,
                "display_id": self.task.display_id,
                "comment_id": 2,
            },
        )
        row = self.newest(self.task, TaskActivityActionType.COMMENT_DELETED)
        self.assertEqual(row.metadata["via"], self.stamp(run, step))

    def test_it_is_refused_once_the_comment_has_been_edited_or_deleted(self):
        for what, change in {
            "edited": lambda c: TaskComments.objects.filter(pk=c.pk).update(
                comment_body=blocks("edited by them")
            ),
            "deleted": lambda c: TaskComments.objects.filter(pk=c.pk).update(is_deleted=True),
        }.items():
            with self.subTest(change=what):
                run, step = self.go(
                    "add_comment", {"task_id": self.task.task_id, "body_text": f"c {what}"}
                )
                comment = (
                    TaskComments.objects.filter(task=self.task).order_by("-comment_id").first()
                )
                change(comment)
                self.refused(run, step, "changed_since", 409)
                self.assertNotEqual(TaskComments.objects.get(pk=comment.pk).comment_body, None)


class UndoingCreateNote(UndoBase):
    def make(self, **extra):
        run, step = self.go(
            "create_note",
            {"note_type": "personal", "title": "Made", "content_text": "hello", **extra},
        )
        return PersonalNoteMaster.objects.get(title="Made"), run, step

    def test_it_deletes_the_note_it_made_for_good(self):
        note, run, step = self.make()
        with mock.patch("origin.services.note_delete.purge_note") as purge:
            body = self.undone(run, step)
        self.assertFalse(PersonalNoteMaster.objects.filter(pk=note.pk).exists())  # HARD
        self.assertFalse(
            NoteVersionMaster.objects.filter(note_type=PERSONAL, note_id=note.note_id).exists()
        )
        self.assertFalse(
            NotePermissionMaster.objects.filter(note_type=PERSONAL, note_id=note.note_id).exists()
        )
        purge.assert_called_once_with("personal", note.note_id)
        self.assertEqual(body["action"], "deleted")
        self.assertEqual(
            body["entity"],
            {"kind": "note", "note_type": "personal", "note_id": note.note_id, "title": "Made"},
        )

    def test_a_task_note_too(self):
        run, step = self.go(
            "create_note",
            {
                "note_type": "task",
                "title": "Plan doc",
                "content_text": "text",
                "project_id": self.project.project_id,
            },
        )
        note = TaskNoteMaster.objects.get(title="Plan doc")
        self.undone(run, step)
        self.assertFalse(TaskNoteMaster.objects.filter(pk=note.pk).exists())

    def test_it_is_refused_once_the_note_has_changed(self):
        def edited_in_the_app_within_five_minutes(note):
            # The editor's own autosave coalesces into the newest version row IN PLACE:
            # the version number does not move, and a guard on it alone would pass.
            PersonalNoteMaster.objects.filter(pk=note.pk).update(body=blocks("typed since"))
            snapshot_note_version(
                team=self.team,
                editor=self.user,
                note_type=PERSONAL,
                note_id=note.note_id,
                title=note.title,
                body=blocks("typed since"),
            )

        changes = {
            "a newer version": lambda n: snapshot_note_version(
                team=self.team,
                editor=self.other,
                note_type=PERSONAL,
                note_id=n.note_id,
                title="Made",
                body=blocks("someone else"),
                force_new=True,
            ),
            "an autosave in the app": edited_in_the_app_within_five_minutes,
            "its title": lambda n: PersonalNoteMaster.objects.filter(pk=n.pk).update(title="New"),
            "its access": lambda n: NotePermissionMaster.objects.create(
                team=self.team,
                user=self.other,
                note_id=n.note_id,
                note_type=PERSONAL,
                role_id=ROLE_VIEWER,
            ),
            "a sub-note": lambda n: PersonalNoteMaster.objects.create(
                team=self.team,
                owner=self.user,
                title="Sub",
                body=[],
                parent_note_id=n.note_id,
            ),
        }
        for what, change in changes.items():
            with self.subTest(change=what):
                note, run, step = self.make()
                change(note)
                if what == "an autosave in the app":
                    # The trap is real: the version number is still the one recorded.
                    head = undo.head_version("personal", note.note_id)
                    self.assertEqual(head.version_no, step.undo_json["image"]["head_version_no"])
                self.refused(run, step, "changed_since", 409)
                self.assertTrue(PersonalNoteMaster.objects.filter(pk=note.pk).exists())
                PersonalNoteMaster.objects.filter(parent_note_id=note.note_id).delete()
                PersonalNoteMaster.objects.filter(pk=note.pk).delete()

    def test_a_note_that_is_gone_is_changed(self):
        note, run, step = self.make()
        PersonalNoteMaster.objects.filter(pk=note.pk).delete()
        self.refused(run, step, "changed_since", 409)


class UndoingUpdateNote(UndoBase):
    def edit(self):
        return self.go(
            "update_note",
            {
                "note_id": self.note.note_id,
                "note_type": "personal",
                "title": "Plan v3",
                "content_text": "third draft",
            },
        )

    def live(self):
        note = PersonalNoteMaster.objects.get(pk=self.note.pk)
        return note.title, note.body

    def test_it_puts_the_replaced_version_back(self):
        run, step = self.edit()
        self.assertEqual(self.live()[0], "Plan v3")
        with mock.patch.object(
            undo_apply, "push_note_body_now", return_value=PushOutcome.SENT
        ) as push:
            body = self.undone(run, step)
        self.assertEqual(self.live(), ("Plan v2", blocks("second draft")))
        self.assertEqual(body["action"], "restored")
        self.assertEqual(body["restored"], ["title", "body"])
        self.assertEqual(body["live_editor"], "sent")
        push.assert_called_once_with("personal", self.note.note_id, blocks("second draft"))
        self.assertEqual(
            body["entity"],
            {
                "kind": "note",
                "note_type": "personal",
                "note_id": self.note.note_id,
                "title": "Plan v2",
            },
        )
        versions = list(
            NoteVersionMaster.objects.filter(
                note_type=PERSONAL, note_id=self.note.note_id
            ).order_by("version_no")
        )
        # v1, v2, the agent's v3, and the undo's own v4: what it replaced is a version.
        self.assertEqual([v.version_no for v in versions], [1, 2, 3, 4])
        self.assertEqual(versions[-1].restored_from_version_no, self.v2.version_no)

    def test_a_live_editor_that_could_not_be_told_is_reported_not_hidden(self):
        run, step = self.edit()
        with mock.patch.object(undo_apply, "push_note_body_now", return_value=PushOutcome.FAILED):
            body = self.undone(run, step)
        self.assertEqual(body["live_editor"], "failed")
        self.assertEqual(self.live()[0], "Plan v2")  # the undo is done all the same

    def test_with_no_collab_server_there_is_no_editor_to_tell(self):
        run, step = self.edit()
        self.assertEqual(self.undone(run, step)["live_editor"], "skipped")

    def test_a_push_that_blows_up_does_not_undo_the_undo(self):
        run, step = self.edit()
        with mock.patch.object(undo_apply, "push_note_body_now", side_effect=RuntimeError("boom")):
            body = self.undone(run, step)
        self.assertNotIn("live_editor", body)
        self.assertEqual(self.live()[0], "Plan v2")
        step.refresh_from_db()
        self.assertIsNotNone(step.undone_at)

    def test_the_state_no_version_held_comes_back_too(self):
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(
            body=blocks("typed in the editor, never versioned")
        )
        run, step = self.go(
            "update_note",
            {"note_id": self.note.note_id, "note_type": "personal", "content_text": "agent text"},
        )
        self.undone(run, step)
        self.assertEqual(self.live()[1], blocks("typed in the editor, never versioned"))

    def test_it_is_refused_once_the_note_has_changed(self):
        def autosave(note):
            PersonalNoteMaster.objects.filter(pk=note.pk).update(body=blocks("typed since"))
            snapshot_note_version(
                team=self.team,
                editor=self.user,  # the agent's snapshot was this editor's, seconds ago
                note_type=PERSONAL,
                note_id=note.note_id,
                title="Plan v3",
                body=blocks("typed since"),
            )

        changes = {
            "a newer version": lambda n: snapshot_note_version(
                team=self.team,
                editor=self.other,
                note_type=PERSONAL,
                note_id=n.note_id,
                title="Plan v3",
                body=blocks("a colleague"),
                force_new=True,
            ),
            "an autosave in the app": autosave,
            "its title": lambda n: PersonalNoteMaster.objects.filter(pk=n.pk).update(title="Other"),
            "the version it replaced, gone": lambda n: NoteVersionMaster.objects.filter(
                note_type=PERSONAL, note_id=n.note_id, version_no=1
            ).delete(),
            "the version it replaced, rewritten": lambda n: NoteVersionMaster.objects.filter(
                note_type=PERSONAL, note_id=n.note_id, version_no=1
            ).update(body=blocks("rewritten history")),
        }
        for what, change in changes.items():
            with self.subTest(change=what):
                # A note of its own for each case: v1 holds what the agent replaces.
                note = PersonalNoteMaster.objects.create(
                    team=self.team, owner=self.user, title="Plan v2", body=blocks("second draft")
                )
                snapshot_note_version(
                    team=self.team,
                    editor=self.user,
                    note_type=PERSONAL,
                    note_id=note.note_id,
                    title="Plan v2",
                    body=blocks("second draft"),
                )
                run, step = self.go(
                    "update_note",
                    {
                        "note_id": note.note_id,
                        "note_type": "personal",
                        "title": "Plan v3",
                        "content_text": "third draft",
                    },
                )
                image = step.undo_json["image"]
                self.assertEqual((image["pre"]["version_no"], image["post"]["version_no"]), (1, 2))
                change(note)
                if what == "an autosave in the app":
                    head = undo.head_version("personal", note.note_id)
                    self.assertEqual(head.version_no, 2)  # the trap: still the recorded number
                self.refused(run, step, "changed_since", 409)
                self.assertEqual(
                    PersonalNoteMaster.objects.get(pk=note.pk).title != "Plan v2", True
                )

    def test_restore_note_version_is_undone_to_what_it_replaced(self):
        run, step = self.go(
            "restore_note_version",
            {"note_id": self.note.note_id, "note_type": "personal", "version_no": 1},
        )
        self.assertEqual(self.live(), ("Plan v1", blocks("first draft")))
        body = self.undone(run, step)
        self.assertEqual(self.live(), ("Plan v2", blocks("second draft")))
        self.assertEqual(body["tool_name"], "restore_note_version")
        self.assertEqual(body["restored"], ["title", "body"])

    def test_the_stored_mention_set_follows_the_restored_body_without_telling_anyone(self):
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(
            body=[mention(self.other)], mentioned_user_ids=[str(self.other.id)]
        )
        snapshot_note_version(
            team=self.team,
            editor=self.user,
            note_type=PERSONAL,
            note_id=self.note.note_id,
            title="Plan v2",
            body=[mention(self.other)],
            force_new=True,
        )
        run, step = self.edit()
        PersonalNoteMaster.objects.filter(pk=self.note.pk).update(mentioned_user_ids=[])
        before = Activity.objects.count()
        with mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities") as push:
            self.undone(run, step)
        self.assertEqual(Activity.objects.count(), before)
        push.assert_not_called()
        self.assertEqual(
            PersonalNoteMaster.objects.get(pk=self.note.pk).mentioned_user_ids,
            [str(self.other.id)],
        )


class PermissionIsRecheckedAtUndoTime(UndoBase):
    """Each scenario REALLY revokes access: the rules the writes were authorized by say that
    a project's owner, and a task's reporter and assignee, keep access whatever the
    membership rows say, and a note's owner always may edit it. So the user here holds
    their access only as a member of a project somebody else owns, or by a grant."""

    def setUp(self):
        super().setUp()
        self.their_project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Theirs",
            code="THR",
            owner=self.other,
            project_system_user=self.other,
        )
        for member in (self.user, self.other):
            ProjectMembers.objects.create(
                team=self.team, project=self.their_project, attendee=member
            )

    def make_theirs(self):
        """A task somebody else made and is the assignee and reporter of, in their project."""
        return TaskMaster.objects.create(
            team=self.team,
            project=self.their_project,
            reporter=self.other,
            assignee=self.other,
            title="Theirs",
            status="Open",
            priority="Low",
            content=copy.deepcopy(BODY),
        )

    def leave_project(self):
        ProjectMembers.objects.filter(project=self.their_project, attendee=self.user).delete()

    def test_a_task_the_user_can_no_longer_edit(self):
        theirs = self.make_theirs()
        run, step = self.go("update_task", {"task_id": theirs.task_id, "title": "Renamed"})
        self.leave_project()
        body = self.refused(run, step, "permission_lost", 403)
        self.assertIn("can no longer change", body["error"])
        self.assertEqual(TaskMaster.objects.get(pk=theirs.pk).title, "Renamed")

    def test_a_comment_on_a_task_the_user_can_no_longer_edit(self):
        theirs = self.make_theirs()
        run, step = self.go("add_comment", {"task_id": theirs.task_id, "body_text": "hi"})
        self.leave_project()
        self.refused(run, step, "permission_lost", 403)
        self.assertFalse(TaskComments.objects.get(task=theirs).is_deleted)

    def test_a_task_note_the_user_can_no_longer_edit(self):
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.their_project,
            owner=self.other,
            title="Their plan",
            body=blocks("their text"),
        )
        snapshot_note_version(
            team=self.team,
            editor=self.other,
            note_type=2,
            note_id=note.note_id,
            title="Their plan",
            body=blocks("their text"),
        )
        run, step = self.go(
            "update_note",
            {"note_id": note.note_id, "note_type": "task", "content_text": "agent text"},
        )
        self.assertEqual(text_of(TaskNoteMaster.objects.get(pk=note.pk).body), "agent text")
        self.leave_project()
        self.refused(run, step, "permission_lost", 403)
        self.assertEqual(text_of(TaskNoteMaster.objects.get(pk=note.pk).body), "agent text")

    def test_a_note_whose_editor_grant_was_revoked(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.other, title="Shared", body=blocks("theirs")
        )
        grant = NotePermissionMaster.objects.create(
            team=self.team,
            user=self.user,
            note_id=note.note_id,
            note_type=PERSONAL,
            role_id=ROLE_EDITOR,
        )
        run, step = self.go(
            "update_note",
            {"note_id": note.note_id, "note_type": "personal", "content_text": "agent text"},
        )
        NotePermissionMaster.objects.filter(pk=grant.pk).update(role_id=ROLE_VIEWER)
        self.refused(run, step, "permission_lost", 403)
        self.assertEqual(text_of(PersonalNoteMaster.objects.get(pk=note.pk).body), "agent text")

    def test_an_author_made_a_reader_of_their_own_note_may_not_delete_it(self):
        run, step = self.go(
            "create_note", {"note_type": "personal", "title": "Made", "content_text": "hello"}
        )
        note = PersonalNoteMaster.objects.get(title="Made")
        NotePermissionMaster.objects.filter(
            note_type=PERSONAL, note_id=note.note_id, user=self.user
        ).update(role_id=ROLE_VIEWER)
        self.refused(run, step, "permission_lost", 403)
        self.assertTrue(PersonalNoteMaster.objects.filter(pk=note.pk).exists())

    def test_a_person_who_has_left_the_team(self):
        run, step = self.go(
            "create_task", {"title": "Fresh", "project_id": self.project.project_id}
        )
        # They no longer belong to the team in any capacity (it has a new owner, they are
        # off the roster and off every project), though they still report the task.
        self.team.owner = self.other
        self.team.save(update_fields=["owner"])
        self.user.team_attendees.update(is_deleted=True)
        ProjectMembers.objects.filter(attendee=self.user).delete()  # a guest is a participant
        self.refused(run, step, "permission_lost", 403)
        self.assertEqual(TaskMaster.objects.get(title="Fresh").status, "Open")

    def test_losing_access_is_not_a_changed_since(self):
        """Two different things to tell a person, with two different codes."""
        theirs = self.make_theirs()
        run, step = self.go("update_task", {"task_id": theirs.task_id, "title": "Renamed"})
        self.leave_project()
        TaskMaster.objects.filter(pk=theirs.pk).update(title="Changed too")
        self.assertEqual(self.undo(run, step).json()["code"], "permission_lost")


class NoToolNoModelAndNothingSent(UndoBase):
    def test_no_tool_runs_and_no_model_is_called(self):
        runs = {name: self.go(name, args) for name, args in self.scenarios().items()}

        def boom(args, ctx):
            raise AssertionError("an undo ran a tool")

        # EVERY registered tool, reads and the other writes included: an undo that
        # reached for `delete_comment` to undo a comment would trip it too.
        tripwires = {name: dataclasses.replace(tool, run=boom) for name, tool in REGISTRY.items()}
        with (
            mock.patch.dict(REGISTRY, tripwires),
            mock.patch.object(controller, "get_model_client", side_effect=AssertionError("llm")),
            mock.patch.object(controller, "resume_agent", side_effect=AssertionError("resumed")),
            mock.patch.object(controller, "run_agent", side_effect=AssertionError("ran")),
        ):
            # Newest first: the note restore came after the note edit, and an undo of the
            # edit would (rightly) find the note changed since.
            for name, (run, step) in reversed(list(runs.items())):
                with self.subTest(tool=name):
                    self.assertEqual(self.undo(run, step).status_code, 200, name)

    def test_the_undo_module_never_reaches_for_the_registry(self):
        import inspect

        source = inspect.getsource(undo_apply)
        for needle in ("REGISTRY", "controller", ".run(", "get_model_client", "tool.run"):
            self.assertNotIn(needle, source.replace("`", ""), needle)

    def test_nothing_is_sent_and_the_mention_set_follows_the_body(self):
        mentioned = [mention(self.other)]
        TaskMaster.objects.filter(pk=self.task.pk).update(
            content=mentioned, mentioned_user_ids=[str(self.other.id)]
        )
        run, step = self.go("update_task", {"task_id": self.task.task_id, "content_text": "plain"})
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).mentioned_user_ids, [])

        activities = Activity.objects.count()
        emails = EmailNotificationEvent.objects.count()
        with (
            mock.patch("origin.services.webpush_dispatch.schedule_push_for_activities") as push,
            mock.patch("origin.services.collab_push.push_task_body"),
        ):
            self.undone(run, step)
        self.assertEqual(TaskMaster.objects.get(pk=self.task.pk).content, mentioned)
        self.assertEqual(Activity.objects.count(), activities, "an undo filed a notification")
        self.assertEqual(EmailNotificationEvent.objects.count(), emails)
        push.assert_not_called()
        # Quietly, the set the next save diffs against matches the body: that save will not
        # announce a mention that was there all along.
        self.assertEqual(
            TaskMaster.objects.get(pk=self.task.pk).mentioned_user_ids, [str(self.other.id)]
        )

    def test_nothing_is_recalled_either(self):
        """Undoing a comment does not take back the notification the comment sent."""
        self.task.assignee = self.other  # the comment will tell them
        self.task.save(update_fields=["assignee"])
        before = Activity.objects.count()
        run, step = self.go(
            "add_comment",
            {"task_id": self.task.task_id, "body_text": "hello"},
        )
        sent = Activity.objects.count()
        self.assertGreater(sent, before, "the comment told nobody: the test proves nothing")
        self.undone(run, step)
        self.assertEqual(Activity.objects.count(), sent)


class TheViaOverride(UndoBase):
    def tearDown(self):
        current_user.clear_current_user()
        super().tearDown()

    def test_it_reports_the_stamp_and_puts_back_what_was_there(self):
        current_user.set_current_user(self.user, via={"kind": "agent"})
        with current_user.via_override({"kind": "app", "undo_of": {"step": 1}}):
            self.assertEqual(current_user.get_current_source()["undo_of"], {"step": 1})
        self.assertEqual(current_user.get_current_source(), {"kind": "agent"})
        self.assertEqual(current_user.get_current_user(), self.user)

    def test_with_nothing_before_it_leaves_nothing_after(self):
        with current_user.via_override({"kind": "app", "undo_of": {}}):
            self.assertIsNotNone(current_user.get_current_source())
        self.assertIsNone(current_user.get_current_source())

    def test_it_puts_the_source_back_even_when_the_block_fails(self):
        current_user.set_current_user(self.user, via={"kind": "agent"})
        with self.assertRaises(RuntimeError):
            with current_user.via_override({"kind": "app"}):
                raise RuntimeError("x")
        self.assertEqual(current_user.get_current_source(), {"kind": "agent"})
