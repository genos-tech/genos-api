"""Regression tests for the sprint bootstrap on a brand-new project.

`GET /api/v2/sprint/list/` lazily materialises a project's `SprintConfig`
and its upcoming `Sprint` rows. That's a WRITE on a read path, and a fresh
project draws several of those GETs at once (dashboard, sidebar, milestone
picker…), so both writes have to tolerate concurrency:

  * `_ensure_default_config` used to filter-then-create, so every concurrent
    caller saw "no config" and INSERTed — all but one 500'd with
    `duplicate key ... origin_sprintconfig_project_id_key`.
  * `_ensure_upcoming_sprints` derives `sequence_number` from a prior read,
    so concurrent callers computed the same numbers and collided on
    `unique_project_sprint_sequence`.

The single-threaded tests below pin the idempotency contract each fix
rests on. True thread-level concurrency isn't exercised here: Django's
`TestCase` wraps each test in a transaction that its threads can't see,
and `TransactionTestCase` + real threads against the test DB is flaky by
nature. The race itself was reproduced and the fix verified against the
live dev database (6 concurrent callers: 3 IntegrityErrors before, none
after, exactly 1 config and 6 unique sequence numbers).
"""

from origin.models.project.prj_models import ProjectMaster
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.tests.test_base import BaseAPITestCase
from origin.views.task.sprint_views import (
    _ensure_default_config,
    _ensure_upcoming_sprints,
    _needs_upcoming_sprints,
)


class SprintBootstrapTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Bootstrap Proj",
            owner=self.user,
        )
        # A brand-new project: no config, no sprints.
        SprintConfig.objects.filter(project=self.project).delete()
        Sprint.objects.filter(project=self.project).delete()

    def test_ensure_default_config_creates_exactly_one(self):
        config = _ensure_default_config(self.project)
        self.assertIsNotNone(config)
        self.assertEqual(SprintConfig.objects.filter(project=self.project).count(), 1)

    def test_ensure_default_config_is_idempotent(self):
        # The get_or_create contract: a second call must RETURN the first
        # row, never attempt a second INSERT. That's what stops a
        # concurrent caller from 500ing on the OneToOne constraint.
        first = _ensure_default_config(self.project)
        second = _ensure_default_config(self.project)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(SprintConfig.objects.filter(project=self.project).count(), 1)

    def test_ensure_default_config_returns_the_existing_row_untouched(self):
        existing = _ensure_default_config(self.project)
        existing.duration_days = 7
        existing.save()

        again = _ensure_default_config(self.project)
        # `defaults` must not be re-applied over a user's edited cadence.
        self.assertEqual(again.duration_days, 7)

    def test_ensure_upcoming_sprints_materialises_the_horizon(self):
        config = _ensure_default_config(self.project)
        _ensure_upcoming_sprints(self.project, config)

        sprints = Sprint.objects.filter(project=self.project, is_deleted=False)
        self.assertEqual(sprints.count(), config.upcoming_horizon)
        seqs = sorted(sprints.values_list("sequence_number", flat=True))
        self.assertEqual(len(seqs), len(set(seqs)), "sequence numbers must be unique")

    def test_ensure_upcoming_sprints_is_idempotent(self):
        # The second call must create nothing — the guard that keeps a
        # repeat caller off `unique_project_sprint_sequence`.
        config = _ensure_default_config(self.project)
        _ensure_upcoming_sprints(self.project, config)
        count_after_first = Sprint.objects.filter(project=self.project).count()

        _ensure_upcoming_sprints(self.project, config)
        self.assertEqual(Sprint.objects.filter(project=self.project).count(), count_after_first)

    def test_needs_upcoming_sprints_gates_the_lock(self):
        # The fast path: once the horizon is materialised the probe must say
        # "no work", so the common case never takes the row lock.
        config = _ensure_default_config(self.project)
        self.assertTrue(_needs_upcoming_sprints(self.project, config))

        _ensure_upcoming_sprints(self.project, config)
        self.assertFalse(_needs_upcoming_sprints(self.project, config))

    def test_auto_roll_off_creates_nothing(self):
        config = _ensure_default_config(self.project)
        config.auto_roll = False
        config.save()

        _ensure_upcoming_sprints(self.project, config)
        self.assertEqual(Sprint.objects.filter(project=self.project).count(), 0)
