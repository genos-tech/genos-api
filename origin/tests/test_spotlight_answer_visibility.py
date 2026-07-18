"""Collection-gate tests for the `spotlight_answer` lane
(`agent/source_visibility.py`).

Covers the 2026-07-18 gate changes:
  * milestone sources resolve (project members; backing-task fallback) —
    previously unclassifiable, silently dropping every milestone-citing answer;
  * todo sources resolve to exactly the asker (personal by design), so a
    todo-citing answer is collected but visible only to its asker;
  * an audience of ONE (the asker) is collectible (`_MIN_SHARE_AUDIENCE` 2→1)
    — self-reuse in solo/single-member-project workspaces;
and the fail-closed invariants that must NOT regress: unknown/conversation
sources, missing rows, empty source lists, and disjoint audiences all still
drop the run.
"""

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.source_visibility import (
    shareable_acl_for_sources,
    source_acl_user_ids,
)
from origin.tests.test_base import BaseAPITestCase


def _milestone_source(project_id=None, task_id=None):
    return {
        "entity_type": "milestone",
        "entity_id": "milestone:1",
        "project_id": str(project_id) if project_id is not None else None,
        "task_id": str(task_id) if task_id is not None else None,
    }


def _todo_source():
    return {"entity_type": "todo", "entity_id": "todo:2026-07-18:item:1"}


def _task_source(task_id):
    return {"entity_type": "task", "entity_id": f"task:{task_id}", "task_id": str(task_id)}


class SpotlightAnswerVisibilityTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Visibility Project",
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.get_or_create(project=self.project, attendee=self.user)
        ProjectMembers.objects.get_or_create(project=self.project, attendee=self.user2)
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="T", status="Open"
        )
        self.uid = str(self.user.id)
        self.uid2 = str(self.user2.id)

    # ---- milestone sources (previously always unclassifiable) ----

    def test_milestone_resolves_via_project_members(self):
        acl = source_acl_user_ids(_milestone_source(project_id=self.project.project_id))
        self.assertEqual(acl, {self.uid, self.uid2})

    def test_milestone_falls_back_to_backing_task(self):
        acl = source_acl_user_ids(_milestone_source(task_id=self.task.task_id))
        self.assertEqual(acl, {self.uid, self.uid2})

    def test_milestone_without_ids_or_row_fails_closed(self):
        self.assertIsNone(source_acl_user_ids(_milestone_source()))
        self.assertIsNone(source_acl_user_ids(_milestone_source(task_id=999999)))

    def test_milestone_citing_answer_is_collected(self):
        # The original prod bug: a milestone chip dropped the whole run.
        acl = shareable_acl_for_sources(
            [_milestone_source(project_id=self.project.project_id)], asker_id=self.uid
        )
        self.assertEqual(acl, sorted({self.uid, self.uid2}))

    # ---- todo sources (asker-only audience) ----

    def test_todo_resolves_to_asker(self):
        self.assertEqual(source_acl_user_ids(_todo_source(), asker_id=self.uid), {self.uid})

    def test_todo_without_asker_fails_closed(self):
        self.assertIsNone(source_acl_user_ids(_todo_source()))

    def test_todo_collapses_shared_answer_to_asker(self):
        # task is visible to u1+u2; adding a todo narrows the audience to
        # the asker alone — the answer is collected but never team-shared.
        acl = shareable_acl_for_sources(
            [_task_source(self.task.task_id), _todo_source()], asker_id=self.uid
        )
        self.assertEqual(acl, [self.uid])

    # ---- audience-of-one collection (was: dropped) ----

    def test_audience_of_one_is_collected(self):
        acl = shareable_acl_for_sources([_todo_source()], asker_id=self.uid)
        self.assertEqual(acl, [self.uid])

    # ---- fail-closed invariants that must not regress ----

    def test_empty_sources_fail_closed(self):
        self.assertIsNone(shareable_acl_for_sources([], asker_id=self.uid))

    def test_unknown_and_conversation_sources_fail_closed(self):
        for etype in ("conversation", "web", "future_lane"):
            src = {"entity_type": etype, "entity_id": f"{etype}:1"}
            self.assertIsNone(shareable_acl_for_sources([src], asker_id=self.uid), msg=etype)

    def test_missing_task_row_fails_closed(self):
        self.assertIsNone(shareable_acl_for_sources([_task_source(999999)], asker_id=self.uid))

    def test_disjoint_audiences_fail_closed(self):
        # Task audience {u1, u2} ∩ todo audience {outsider} = ∅ — nobody
        # can see all the evidence, so the run must not be collected.
        outsider = "00000000-0000-4000-8000-00000000dead"
        acl = shareable_acl_for_sources(
            [_task_source(self.task.task_id), _todo_source()], asker_id=outsider
        )
        self.assertIsNone(acl)
