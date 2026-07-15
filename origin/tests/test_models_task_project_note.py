"""Model-layer tests for origin/models/task/, origin/models/project/,
and origin/models/note/.

These tests exercise the ORM directly (no API/view layer): custom
methods/properties (``TaskMaster.display_id``), post-save signals
(``set_root_task_id`` / ``assign_project_task_number``), custom
``save()`` overrides (``TaskCommentReactionFact`` / ``...MentionFact``
uid), DB-level constraints (unique + check), FK on_delete semantics
(CASCADE vs SET_NULL divergence between sibling tables), defaults, and
the integer-keyed note "tree" via ``parent_note_id``.

Where the task framing implies stronger semantics than the code has
(e.g. "parent FK" for notes — there is no FK), the tests assert the
ACTUAL current behavior and document the divergence.
"""

from datetime import date
from unittest import mock

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from origin.models.chat.unified_models import Channel
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.favorite_note_models import NoteFavoriteMaster
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.recent_note_models import NoteRecentMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import (
    ProjectMaster,
    ProjectMembers,
    ProjectTags,
)
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.models.task.task_activity_models import (
    TaskActivity,
    TaskActivityActionType,
)
from origin.models.task.task_models import (
    TaskCommentMentionFact,
    TaskCommentReactionFact,
    TaskDependency,
    TaskMaster,
)
from origin.tests.test_base import BaseAPITestCase


def _detach_project_channels(project):
    """A ``post_save`` signal (pm_channel_signals) auto-creates a
    ``Channel(kind=PM)`` for every ProjectMaster, and Channel.project /
    Channel.team are ``on_delete=PROTECT``. To exercise the project's own
    cascade behavior we must first remove that orthogonal PM channel so
    the project/team delete isn't blocked. (Members CASCADE off the
    channel.)"""
    Channel.objects.filter(project=project).delete()


class ProjectModelTests(BaseAPITestCase):
    """ProjectMaster + sibling project tables."""

    def _project(self, name="Genos Core", code=None, team=None):
        return ProjectMaster.objects.create(
            team=team or self.team,
            project_name=name,
            owner=self.user,
            code=code,
        )

    def test_project_name_is_globally_unique(self):
        self._project(name="Unique Name")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # project_name has unique=True (NOT scoped to team).
                self._project(name="Unique Name")

    def test_code_unique_per_team_when_set(self):
        self._project(name="P One", code="GEN")
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._project(name="P Two", code="GEN")

    def test_code_partial_constraint_allows_multiple_nulls(self):
        # The unique constraint is conditional (code IS NOT NULL), so
        # many projects in the same team may have a NULL code.
        self._project(name="No Code A", code=None)
        self._project(name="No Code B", code=None)
        self.assertEqual(
            ProjectMaster.objects.filter(team=self.team, code__isnull=True).count(),
            2,
        )

    def test_same_code_allowed_across_different_teams(self):
        from origin.models.common.team_models import TeamMaster

        team2 = TeamMaster.objects.create(
            team_name="Second Team",
            team_email="t2@example.com",
            owner=self.user,
        )
        self._project(name="Team1 GEN", code="GEN", team=self.team)
        # Same code "GEN" but a different team — constraint is per-team.
        p2 = self._project(name="Team2 GEN", code="GEN", team=team2)
        self.assertEqual(p2.code, "GEN")

    def test_code_not_auto_populated_on_orm_create(self):
        # Code derivation lives in services/project_code.py, invoked by
        # the create view — NOT a model signal. A bare ORM create leaves
        # code NULL.
        p = self._project(name="Raw Create", code=None)
        p.refresh_from_db()
        self.assertIsNone(p.code)

    def test_defaults(self):
        p = self._project(name="Defaults Proj")
        self.assertFalse(p.is_private)
        self.assertFalse(p.is_deleted)
        self.assertIsNotNone(p.project_id)

    def test_project_member_unique_per_project_attendee(self):
        p = self._project(name="Member Proj")
        ProjectMembers.objects.create(team=self.team, project=p, attendee=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectMembers.objects.create(
                    team=self.team, project=p, attendee=self.user
                )

    def test_project_tag_unique_per_project_name(self):
        p = self._project(name="Tag Proj")
        ProjectTags.objects.create(
            team=self.team,
            project=p,
            tag_id=1,
            tag_name="bug",
            tag_color="#fff",
            tag_text_color="#000",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ProjectTags.objects.create(
                    team=self.team,
                    project=p,
                    tag_id=2,
                    tag_name="bug",
                    tag_color="#aaa",
                    tag_text_color="#111",
                )


class TaskDisplayIdTests(BaseAPITestCase):
    """TaskMaster.display_id property — three branches."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Display Proj",
            owner=self.user,
            code="GEN",
        )

    def test_display_id_uses_code_and_number_when_present(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        task.refresh_from_db()  # signal assigns project_task_number
        self.assertEqual(task.project_task_number, 1)
        self.assertEqual(task.display_id, "GEN-1")

    def test_display_id_falls_back_when_project_has_no_code(self):
        proj_no_code = ProjectMaster.objects.create(
            team=self.team, project_name="NoCode Proj", owner=self.user, code=None
        )
        task = TaskMaster.objects.create(
            team=self.team, project=proj_no_code, title="t", status="Open"
        )
        task.refresh_from_db()
        # project_task_number IS assigned, but code is None -> fallback.
        self.assertIsNotNone(task.project_task_number)
        self.assertEqual(task.display_id, f"#{task.task_id}")

    def test_display_id_falls_back_for_orphan_task(self):
        # No project -> project_task_number stays None -> fallback.
        task = TaskMaster.objects.create(
            team=self.team, project=None, title="orphan", status="Open"
        )
        task.refresh_from_db()
        self.assertIsNone(task.project_task_number)
        self.assertEqual(task.display_id, f"#{task.task_id}")


class TaskNumberingSignalTests(BaseAPITestCase):
    """assign_project_task_number post-save signal."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Num Proj", owner=self.user, code="NUM"
        )

    def test_sequential_numbers_within_project(self):
        nums = []
        for i in range(3):
            t = TaskMaster.objects.create(
                team=self.team, project=self.project, title=f"t{i}", status="Open"
            )
            t.refresh_from_db()
            nums.append(t.project_task_number)
        self.assertEqual(nums, [1, 2, 3])

    def test_numbering_is_per_project_independent(self):
        proj2 = ProjectMaster.objects.create(
            team=self.team, project_name="Num Proj 2", owner=self.user, code="NUM2"
        )
        t1 = TaskMaster.objects.create(
            team=self.team, project=self.project, title="a", status="Open"
        )
        t2 = TaskMaster.objects.create(
            team=self.team, project=proj2, title="b", status="Open"
        )
        t1.refresh_from_db()
        t2.refresh_from_db()
        # Each project restarts numbering at 1.
        self.assertEqual(t1.project_task_number, 1)
        self.assertEqual(t2.project_task_number, 1)

    def test_orphan_task_gets_no_number(self):
        t = TaskMaster.objects.create(
            team=self.team, project=None, title="orphan", status="Open"
        )
        t.refresh_from_db()
        self.assertIsNone(t.project_task_number)

    def test_explicit_number_is_respected(self):
        # Signal skips assignment when a number is already set.
        t = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="explicit",
            status="Open",
            project_task_number=99,
        )
        t.refresh_from_db()
        self.assertEqual(t.project_task_number, 99)
        # The next auto-assigned task should be MAX+1 == 100.
        t2 = TaskMaster.objects.create(
            team=self.team, project=self.project, title="next", status="Open"
        )
        t2.refresh_from_db()
        self.assertEqual(t2.project_task_number, 100)

    def test_unique_constraint_on_project_number(self):
        existing = TaskMaster.objects.create(
            team=self.team, project=self.project, title="first", status="Open"
        )
        existing.refresh_from_db()
        dup_num = existing.project_task_number
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                # Explicit duplicate number bypasses the auto-assign
                # (signal only fills when None) and hits the unique
                # constraint directly.
                TaskMaster.objects.create(
                    team=self.team,
                    project=self.project,
                    title="dup",
                    status="Open",
                    project_task_number=dup_num,
                )

    def test_number_race_collision_is_retried(self):
        # Regression for the "create task -> 500" bug: two concurrent
        # creates in the same project can compute the same MAX+1 and the
        # loser's UPDATE hit the unique constraint, which used to bubble
        # out as an unhandled 500. The signal must now retry and land on
        # the next free number instead of raising.
        t1 = TaskMaster.objects.create(
            team=self.team, project=self.project, title="first", status="Open"
        )
        t1.refresh_from_db()
        self.assertEqual(t1.project_task_number, 1)

        # Force the first claim to reuse t1's number (simulating a racing
        # create that already committed number 1), then let the recompute
        # return the real next value on retry.
        with mock.patch(
            "origin.models.task.task_models._next_project_task_number",
            side_effect=[1, 2],
        ) as m:
            t2 = TaskMaster.objects.create(
                team=self.team, project=self.project, title="second", status="Open"
            )
        t2.refresh_from_db()
        # Collided on 1, retried, and claimed 2 — no exception escaped.
        self.assertEqual(t2.project_task_number, 2)
        self.assertEqual(m.call_count, 2)


class TaskRootIdSignalTests(BaseAPITestCase):
    """set_root_task_id post-save signal — four documented cases."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Root Proj", owner=self.user, code="ROOT"
        )

    def _task(self, title="t", parent_task_id=None, root_task_id=None):
        return TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title=title,
            status="Open",
            parent_task_id=parent_task_id,
            root_task_id=root_task_id,
        )

    def test_top_level_root_is_self(self):
        t = self._task()
        t.refresh_from_db()
        self.assertEqual(t.root_task_id, t.task_id)

    def test_subtask_inherits_parents_root(self):
        parent = self._task(title="parent")
        parent.refresh_from_db()
        child = self._task(title="child", parent_task_id=parent.task_id)
        child.refresh_from_db()
        self.assertEqual(child.root_task_id, parent.task_id)
        self.assertEqual(child.root_task_id, parent.root_task_id)

    def test_deep_subtask_resolves_to_top_root(self):
        root = self._task(title="root")
        root.refresh_from_db()
        mid = self._task(title="mid", parent_task_id=root.task_id)
        mid.refresh_from_db()
        leaf = self._task(title="leaf", parent_task_id=mid.task_id)
        leaf.refresh_from_db()
        self.assertEqual(leaf.root_task_id, root.task_id)

    def test_subtask_with_missing_parent_falls_back_to_self(self):
        # parent_task_id points at a nonexistent task -> fallback self.
        orphan = self._task(title="orphan", parent_task_id=999999)
        orphan.refresh_from_db()
        self.assertEqual(orphan.root_task_id, orphan.task_id)

    def test_explicit_root_task_id_is_not_overwritten(self):
        # When the caller sets root_task_id, the signal's guard skips.
        t = self._task(title="preset", root_task_id=424242)
        t.refresh_from_db()
        self.assertEqual(t.root_task_id, 424242)


class TaskDependencyTests(BaseAPITestCase):
    """TaskDependency unique pair + no-self-block check constraint + CASCADE."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Dep Proj", owner=self.user, code="DEP"
        )
        self.blocker = TaskMaster.objects.create(
            team=self.team, project=self.project, title="blocker", status="Open"
        )
        self.blocked = TaskMaster.objects.create(
            team=self.team, project=self.project, title="blocked", status="Open"
        )

    def test_create_dependency(self):
        dep = TaskDependency.objects.create(
            blocker_task=self.blocker,
            blocked_task=self.blocked,
            team=self.team,
            created_by=self.user,
        )
        self.assertIsNotNone(dep.pk)

    def test_unique_pair_constraint(self):
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team=self.team
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskDependency.objects.create(
                    blocker_task=self.blocker,
                    blocked_task=self.blocked,
                    team=self.team,
                )

    def test_self_block_check_constraint(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskDependency.objects.create(
                    blocker_task=self.blocker,
                    blocked_task=self.blocker,
                    team=self.team,
                )

    def test_reverse_direction_is_allowed(self):
        # (A blocks B) and (B blocks A) are distinct rows.
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team=self.team
        )
        rev = TaskDependency.objects.create(
            blocker_task=self.blocked, blocked_task=self.blocker, team=self.team
        )
        self.assertIsNotNone(rev.pk)

    def test_cascade_on_task_delete(self):
        TaskDependency.objects.create(
            blocker_task=self.blocker, blocked_task=self.blocked, team=self.team
        )
        self.blocker.delete()
        self.assertEqual(TaskDependency.objects.count(), 0)


class TaskCommentFactSaveTests(BaseAPITestCase):
    """Custom save() builds the composite `uid` primary key."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Fact Proj", owner=self.user, code="FACT"
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )

    def test_reaction_uid_composed_from_task_comment_reaction(self):
        r = TaskCommentReactionFact.objects.create(
            team=self.team,
            task=self.task,
            comment_id=5,
            reaction_id=7,
            reaction_emoji="👍",
            sender=self.user,
        )
        self.assertEqual(r.uid, f"{self.task.task_id}-5-7")
        self.assertEqual(r.pk, r.uid)

    def test_reaction_duplicate_uid_raises_on_create(self):
        TaskCommentReactionFact.objects.create(
            team=self.team,
            task=self.task,
            comment_id=1,
            reaction_id=2,
            reaction_emoji="x",
            sender=self.user,
        )
        # Same (task, comment_id, reaction_id) => same composed uid (the
        # PK). `objects.create` force-inserts, so a second create with a
        # colliding uid raises rather than upserting. (The unique
        # constraint on the same tuple is redundant with the PK here.)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskCommentReactionFact.objects.create(
                    team=self.team,
                    task=self.task,
                    comment_id=1,
                    reaction_id=2,
                    reaction_emoji="y",
                    sender=self.user2,
                )
        self.assertEqual(TaskCommentReactionFact.objects.count(), 1)

    def test_mention_uid_composed_from_task_comment_user(self):
        m = TaskCommentMentionFact.objects.create(
            team=self.team,
            task=self.task,
            comment_id=3,
            mentioned_user=self.user2,
        )
        self.assertEqual(m.uid, f"{self.task.task_id}-3-{self.user2.id}")


class SprintModelTests(BaseAPITestCase):
    """Sprint + SprintConfig."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Sprint Proj", owner=self.user, code="SPR"
        )

    def _sprint(self, seq, name="S", project=None):
        return Sprint.objects.create(
            team=self.team,
            project=project or self.project,
            name=name,
            sequence_number=seq,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
        )

    def test_sprint_defaults(self):
        s = self._sprint(1)
        self.assertEqual(s.status, "upcoming")
        self.assertTrue(s.is_auto_generated)
        self.assertFalse(s.is_deleted)

    def test_unique_sequence_per_project(self):
        self._sprint(1)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._sprint(1, name="dup")

    def test_same_sequence_across_projects_allowed(self):
        proj2 = ProjectMaster.objects.create(
            team=self.team, project_name="Sprint Proj 2", owner=self.user, code="SPR2"
        )
        self._sprint(1)
        s2 = self._sprint(1, project=proj2)
        self.assertIsNotNone(s2.pk)

    def test_ordering_by_project_then_start_date(self):
        # ordering = ["project_id", "start_date"]
        late = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="late",
            sequence_number=2,
            start_date=date(2026, 3, 1),
            end_date=date(2026, 3, 14),
        )
        early = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="early",
            sequence_number=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
        )
        ordered = list(Sprint.objects.filter(project=self.project))
        self.assertEqual(ordered, [early, late])

    def test_sprint_config_defaults(self):
        cfg = SprintConfig.objects.create(
            team=self.team, project=self.project, anchor_date=date(2026, 1, 1)
        )
        self.assertEqual(cfg.duration_days, 14)
        self.assertTrue(cfg.auto_roll)
        self.assertEqual(cfg.upcoming_horizon, 6)

    def test_sprint_config_one_to_one(self):
        SprintConfig.objects.create(
            team=self.team, project=self.project, anchor_date=date(2026, 1, 1)
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SprintConfig.objects.create(
                    team=self.team, project=self.project, anchor_date=date(2026, 2, 1)
                )

    def test_sprint_config_cascade_on_project_delete(self):
        SprintConfig.objects.create(
            team=self.team, project=self.project, anchor_date=date(2026, 1, 1)
        )
        _detach_project_channels(self.project)
        self.project.delete()
        self.assertEqual(SprintConfig.objects.count(), 0)

    def test_sprint_cascade_on_project_delete(self):
        self._sprint(1)
        _detach_project_channels(self.project)
        self.project.delete()
        self.assertEqual(Sprint.objects.count(), 0)


class MilestoneModelTests(BaseAPITestCase):
    """MilestoneMaster + MilestoneAssignees."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="MS Proj", owner=self.user, code="MS"
        )

    def _milestone(self, title="M", project=None, sprint=None, task=None):
        return MilestoneMaster.objects.create(
            team=self.team,
            project=project or self.project,
            sprint=sprint,
            reporter=self.user,
            title=title,
            task=task,
        )

    def test_milestone_status_default(self):
        m = self._milestone()
        self.assertEqual(m.status, "Open")
        self.assertFalse(m.is_deleted)

    def test_milestone_cascade_on_project_delete(self):
        self._milestone()
        _detach_project_channels(self.project)
        self.project.delete()
        self.assertEqual(MilestoneMaster.objects.count(), 0)

    def test_milestone_sprint_set_null_on_sprint_delete(self):
        sprint = Sprint.objects.create(
            team=self.team,
            project=self.project,
            name="S",
            sequence_number=1,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 14),
        )
        m = self._milestone(sprint=sprint)
        sprint.delete()
        m.refresh_from_db()
        self.assertIsNone(m.sprint_id)
        # Milestone itself survives.
        self.assertEqual(MilestoneMaster.objects.count(), 1)

    def test_milestone_backing_task_set_null_on_task_delete(self):
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            title="backing",
            status="Open",
            is_milestone=True,
        )
        m = self._milestone(task=task)
        task.delete()
        m.refresh_from_db()
        self.assertIsNone(m.task_id)

    def test_milestone_assignee_unique(self):
        m = self._milestone()
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                MilestoneAssignees.objects.create(
                    team=self.team, milestone=m, user=self.user
                )

    def test_milestone_assignee_cascade_on_milestone_delete(self):
        m = self._milestone()
        MilestoneAssignees.objects.create(team=self.team, milestone=m, user=self.user)
        m.delete()
        self.assertEqual(MilestoneAssignees.objects.count(), 0)


class TaskFkOnDeleteTests(BaseAPITestCase):
    """The non-obvious divergence: TaskMaster's project/milestone/sprint
    FKs are SET_NULL (task survives a project delete), whereas
    MilestoneMaster/Sprint/SprintConfig CASCADE off the same project."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Div Proj", owner=self.user, code="DIV"
        )

    def test_task_is_deleted_with_its_project(self):
        """`TaskMaster.project` is CASCADE.

        It used to be SET_NULL, and this test pinned that: the task survived
        with `project=None`. That wasn't a requirement — it left tasks (and
        their comments and notes) in the DB forever, invisible and unreachable.
        The delete-project modal now states this data is destroyed, so it is.
        """
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        _detach_project_channels(self.project)
        self.project.delete()
        self.assertFalse(TaskMaster.objects.filter(pk=task.pk).exists())

    def test_task_milestone_fk_set_null_on_milestone_delete(self):
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="M"
        )
        task = TaskMaster.objects.create(
            team=self.team,
            project=self.project,
            milestone=milestone,
            title="t",
            status="Open",
        )
        milestone.delete()
        task.refresh_from_db()
        self.assertIsNone(task.milestone_id)

    def test_task_survives_team_delete(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        # The team is referenced by the project's auto-created PM Channel
        # (PROTECT) — detach it first so the team delete isn't blocked.
        _detach_project_channels(self.project)
        self.team.delete()
        task.refresh_from_db()
        # TaskMaster.team is SET_NULL — the task survives.
        self.assertIsNone(task.team_id)


class TaskActivityTests(BaseAPITestCase):
    """TaskActivity action-type choices, metadata default, CASCADE."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Act Proj", owner=self.user, code="ACT"
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )

    def test_metadata_defaults_to_empty_dict(self):
        act = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=self.user,
            action_type=TaskActivityActionType.CREATED,
        )
        self.assertEqual(act.metadata, {})

    def test_choice_values_are_stored_verbatim(self):
        act = TaskActivity.objects.create(
            team=self.team,
            task=self.task,
            action_type=TaskActivityActionType.STATUS,
            field_name="status",
            old_value="Open",
            new_value="WIP",
        )
        act.refresh_from_db()
        self.assertEqual(act.action_type, "status_changed")
        self.assertEqual(act.old_value, "Open")
        self.assertEqual(act.new_value, "WIP")

    def test_action_type_choices_not_db_enforced_but_fail_full_clean(self):
        # The DB column accepts arbitrary strings (choices are a
        # form/serializer concern, not a DB constraint).
        act = TaskActivity.objects.create(
            team=self.team, task=self.task, action_type="totally_bogus"
        )
        self.assertEqual(act.action_type, "totally_bogus")
        # But model validation rejects it.
        with self.assertRaises(ValidationError):
            act.full_clean()

    def test_cascade_on_task_delete(self):
        TaskActivity.objects.create(
            team=self.team, task=self.task, action_type=TaskActivityActionType.CREATED
        )
        self.task.delete()
        self.assertEqual(TaskActivity.objects.count(), 0)


class NoteTreeTests(BaseAPITestCase):
    """The note 'tree' uses a plain BigIntegerField parent_note_id, NOT
    a FK. Assert the actual (non-cascading, non-validated) behavior."""

    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team, project_name="Note Proj", owner=self.user, code="NOTE"
        )

    def test_personal_note_parent_is_plain_int_no_fk(self):
        parent = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="parent"
        )
        child = PersonalNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            title="child",
            parent_note_id=parent.note_id,
        )
        self.assertEqual(child.parent_note_id, parent.note_id)
        # No .parent accessor exists (it's not a ForeignKey field).
        self.assertFalse(hasattr(child, "parent"))

    def test_parent_note_id_dangling_after_parent_delete(self):
        # Because parent_note_id is NOT an FK, deleting the parent leaves
        # the child's pointer dangling (no cascade, no SET_NULL).
        parent = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="parent"
        )
        parent_id = parent.note_id
        child = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="child", parent_note_id=parent_id
        )
        parent.delete()
        child.refresh_from_db()
        # Still points at the now-gone parent id.
        self.assertEqual(child.parent_note_id, parent_id)
        self.assertFalse(
            PersonalNoteMaster.objects.filter(note_id=parent_id).exists()
        )

    def test_parent_note_id_accepts_arbitrary_int(self):
        # No referential integrity — any int is accepted.
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n", parent_note_id=123456789
        )
        note.refresh_from_db()
        self.assertEqual(note.parent_note_id, 123456789)

    def test_mentioned_user_ids_defaults_to_empty_list(self):
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="n"
        )
        note.refresh_from_db()
        self.assertEqual(note.mentioned_user_ids, [])

    def test_task_note_tree(self):
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        root = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=task,
            title="root",
        )
        child = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=task,
            title="child",
            parent_note_id=root.note_id,
        )
        self.assertIsNone(root.parent_note_id)
        self.assertEqual(child.parent_note_id, root.note_id)

    def test_task_note_is_deleted_with_its_task(self):
        """`TaskNoteMaster.task` is CASCADE.

        It used to be SET_NULL, leaving a note attached to nothing once its
        task was gone. A note about a deleted task is unreachable, so it goes
        with it.
        """
        task = TaskMaster.objects.create(
            team=self.team, project=self.project, title="t", status="Open"
        )
        note = TaskNoteMaster.objects.create(
            team=self.team,
            project=self.project,
            owner=self.user,
            task=task,
            title="n",
        )
        task.delete()
        self.assertFalse(TaskNoteMaster.objects.filter(pk=note.pk).exists())


class NoteAuxModelTests(BaseAPITestCase):
    """Favorite / Recent / Version / Permission note side-tables."""

    def test_favorite_str_and_unique(self):
        fav = NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=10, note_type=1
        )
        self.assertEqual(
            str(fav), f"User {self.user.id} - Note 10 (Type: 1)"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NoteFavoriteMaster.objects.create(
                    team=self.team, user=self.user, note_id=10, note_type=1
                )

    def test_favorite_same_note_id_different_type_allowed(self):
        NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=10, note_type=1
        )
        # Same note_id but a different note_type is a distinct favorite.
        f2 = NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=10, note_type=2
        )
        self.assertIsNotNone(f2.pk)

    def test_favorite_ordering_newest_first(self):
        f1 = NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=1, note_type=1
        )
        f2 = NoteFavoriteMaster.objects.create(
            team=self.team, user=self.user, note_id=2, note_type=1
        )
        ordered = list(NoteFavoriteMaster.objects.filter(user=self.user))
        # ordering = ["-ts_created_at"] -> most recent first.
        self.assertEqual(ordered[0], f2)
        self.assertEqual(ordered[1], f1)

    def test_recent_str_and_unique(self):
        rec = NoteRecentMaster.objects.create(
            team=self.team, user=self.user, note_id=42, note_type=3
        )
        self.assertEqual(
            str(rec), f"User {self.user.id} - Note 42 (Type: 3)"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NoteRecentMaster.objects.create(
                    team=self.team, user=self.user, note_id=42, note_type=3
                )

    def test_note_version_unique_per_note(self):
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=5,
            version_no=1,
            editor=self.user,
            title="v1",
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NoteVersionMaster.objects.create(
                    team=self.team,
                    note_type=1,
                    note_id=5,
                    version_no=1,
                    editor=self.user,
                    title="dup",
                )

    def test_note_version_monotonic_distinct_versions(self):
        v1 = NoteVersionMaster.objects.create(
            team=self.team, note_type=1, note_id=5, version_no=1, editor=self.user
        )
        v2 = NoteVersionMaster.objects.create(
            team=self.team, note_type=1, note_id=5, version_no=2, editor=self.user
        )
        self.assertIsNone(v1.restored_from_version_no)
        self.assertEqual(
            NoteVersionMaster.objects.filter(note_type=1, note_id=5).count(), 2
        )
        # Same version_no allowed when (note_type, note_id) differs.
        other = NoteVersionMaster.objects.create(
            team=self.team, note_type=2, note_id=5, version_no=1, editor=self.user
        )
        self.assertIsNotNone(other.pk)

    def test_note_permission_unique(self):
        NotePermissionMaster.objects.create(
            team=self.team, user=self.user, note_id=7, note_type=1, role_id=1
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                NotePermissionMaster.objects.create(
                    team=self.team, user=self.user, note_id=7, note_type=1, role_id=3
                )

    def test_chat_note_thread_root_uuid_nullable(self):
        # ChatNoteMaster keys on channel UUID + thread_root UUID; both
        # nullable. A non-thread chat note has no root.
        note = ChatNoteMaster.objects.create(
            team=self.team,
            owner=self.user,
            chat_type=1,
            is_thread=False,
            title="chat note",
        )
        note.refresh_from_db()
        self.assertIsNone(note.thread_root_id)
        self.assertIsNone(note.channel_id)
        self.assertEqual(note.mentioned_user_ids, [])
