"""MIP-1469: the sequence scenario's builder is production code, a fixture base class cannot
clear a real database, and the seeder leaves nothing behind but its teams.

  * `origin/services/dev_fixtures/sequence_scenario.py` imports without `origin/tests`, and the
    dev seeder (`agent/evals/group/sequence.py`) builds the story with the tests tree unimportable;
  * `SequenceScenario.setUp` (which clears tasks) refuses to run outside the test database, writes
    nothing before it refuses, and clears only the tasks of its own team;
  * seeding writes no `api_key.used` security events (they are append-only: nothing could clean
    them up), and the guard is what stops them: without it the events DO appear.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import sys
from pathlib import Path
from unittest import mock

from django.db import connection
from django.test import SimpleTestCase, TestCase

from origin.models.common.security_event_models import SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.evals.group import sequence
from origin.services.dev_fixtures import guards
from origin.services.dev_fixtures.sequence_scenario import ScenarioError, SequenceDriver
from origin.tests.fixtures.sequence_scenario import SequenceScenario

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_FILES = [
    "origin/services/dev_fixtures/__init__.py",
    "origin/services/dev_fixtures/guards.py",
    "origin/services/dev_fixtures/sequence_scenario.py",
    "origin/search_engine/agent/evals/group/sequence.py",
]


class ProductionImportTests(SimpleTestCase):
    def test_no_production_fixture_file_names_the_tests_tree(self):
        for rel in PRODUCTION_FILES:
            text = (ROOT / rel).read_text()
            for node in ast.walk(ast.parse(text, filename=rel)):
                if isinstance(node, ast.Import):
                    modules = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    modules = [node.module or ""]
                else:
                    continue
                self.assertFalse(
                    any(m == "origin.tests" or m.startswith("origin.tests.") for m in modules),
                    f"{rel}:{node.lineno}",
                )
            # the old loader hid the import behind a concatenated name
            self.assertNotIn('"origin" + ".tests"', text, rel)
            self.assertNotIn("_load_test_module", text, rel)

    def test_the_builder_imports_with_the_tests_tree_unimportable(self):
        blocked = {name: None for name in list(sys.modules) if name.startswith("origin.tests")}
        blocked["origin.tests"] = None
        names = [n for n in sys.modules if n.startswith("origin.services.dev_fixtures")]
        with mock.patch.dict(sys.modules):
            for name in names:
                sys.modules.pop(name)
            sys.modules.update(blocked)
            module = importlib.import_module("origin.services.dev_fixtures.sequence_scenario")
            self.assertTrue(hasattr(module, "SequenceDriver"))

    def test_the_driver_reports_an_unexpected_step_as_a_scenario_error(self):
        driver = SequenceDriver(team_id="t", team_name="n", team_email="e", people={}, host="h")
        with self.assertRaises(ScenarioError):
            driver._expect(False, "POST answered 400")
        driver._expect(True)


class SeederWithoutTestsTreeTests(TestCase):
    def test_the_seeder_builds_the_story_with_the_tests_tree_unimportable(self):
        blocked = {name: None for name in list(sys.modules) if name.startswith("origin.tests")}
        blocked["origin.tests"] = None
        with mock.patch.dict(sys.modules, blocked):
            built = sequence.ensure_sequence()
        self.assertEqual(built["built"], ["v1", "v2", "ja"])
        self.assertEqual(
            TaskMaster.objects.filter(team_id=sequence.team_id("v1")).count(),
            1,
        )


class SeederLeavesNoSecurityEventsTests(TestCase):
    def test_seeding_writes_no_security_event_rows(self):
        before = SecurityEvent.objects.count()
        sequence.ensure_sequence()
        self.assertEqual(SecurityEvent.objects.count(), before)

    def test_without_the_guard_the_story_does_write_them(self):
        # sensitivity: if this stops being true, the test above no longer proves anything
        before = SecurityEvent.objects.filter(action="api_key.used").count()
        with mock.patch.object(sequence, "_no_security_events", contextlib.nullcontext):
            sequence.ensure_sequence()
        self.assertGreater(SecurityEvent.objects.filter(action="api_key.used").count(), before)

    def test_reseeding_leaves_none_either(self):
        sequence.ensure_sequence()
        before = SecurityEvent.objects.count()
        sequence.ensure_sequence(reseed=True)
        self.assertEqual(SecurityEvent.objects.count(), before)


class GuardTests(SimpleTestCase):
    def test_the_guard_passes_on_the_test_database(self):
        self.assertTrue(connection.settings_dict["NAME"].startswith("test_"))
        guards.require_test_database("x")

    def test_it_refuses_any_other_database_and_says_what_it_protects(self):
        for name in ("origin", "eval_paid", "prod", ""):
            with mock.patch.dict(connection.settings_dict, {"NAME": name}):
                with self.assertRaises(guards.NotATestDatabase) as caught:
                    guards.require_test_database("the clear")
            self.assertIn("the clear", str(caught.exception))


class DecoyBase(SequenceScenario):
    """A decoy team and task exist BEFORE the base `setUp` runs: it must not touch them."""

    def setUp(self):
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.create_user(
            email="decoy@example.com", username="decoy", password="x"
        )
        decoy_team = TeamMaster.objects.create(
            team_name="Decoy", team_email="decoy-team@example.com", owner=owner
        )
        TeamMembers.objects.create(team=decoy_team, attendee=owner)
        project = ProjectMaster.objects.create(
            team=decoy_team,
            project_name="Decoy",
            code="DCY",
            owner=owner,
            project_system_user=owner,
        )
        self.decoy_task = TaskMaster.objects.create(
            team=decoy_team, project=project, reporter=owner, assignee=owner, title="keep me"
        )
        super().setUp()


class SetUpClearsOnlyItsOwnTeamTests(DecoyBase):
    def test_another_teams_task_survives_and_this_teams_noise_is_gone(self):
        self.assertTrue(TaskMaster.objects.filter(pk=self.decoy_task.pk).exists())
        self.assertFalse(TaskMaster.objects.filter(team=self.team).exists())


class SetUpRefusesOutsideTheTestDatabaseTests(TestCase):
    def test_it_raises_before_writing_or_deleting_anything(self):
        project_rows = ProjectMaster.objects.count()
        task = None
        from django.contrib.auth import get_user_model

        owner = get_user_model().objects.create_user(
            email="o@example.com", username="o", password="x"
        )
        team = TeamMaster.objects.create(team_name="T", team_email="t@example.com", owner=owner)
        project = ProjectMaster.objects.create(
            team=team, project_name="P", code="PPP", owner=owner, project_system_user=owner
        )
        task = TaskMaster.objects.create(
            team=team, project=project, reporter=owner, assignee=owner, title="precious"
        )
        users = get_user_model().objects.count()
        base = SequenceScenario("runTest")
        with mock.patch.dict(connection.settings_dict, {"NAME": "origin"}):
            with self.assertRaises(guards.NotATestDatabase):
                base.setUp()
        self.assertTrue(TaskMaster.objects.filter(pk=task.pk).exists())
        self.assertEqual(get_user_model().objects.count(), users)  # nothing written first
        self.assertEqual(ProjectMaster.objects.count(), project_rows + 1)
