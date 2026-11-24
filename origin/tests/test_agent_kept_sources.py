"""A run keeps the sources of its answer after the 30-day step-output cleanup (MIP-2288).

Ken decided on 2026-11-24 ("keep sources"): before `agent_step_results` blanks a step, the run keeps
its source ids and audience (no text), so History keeps its citations and a shared answer keeps its
audience for the run's own 180 days. No LLM, no OpenSearch.
"""

import json
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from origin.search_engine.agent.controller import reconstruct_sources_for_run
from origin.search_engine.agent.source_visibility import audit_run
from origin.search_engine.models import AgentRun, AgentStep
from origin.services import retention
from origin.tests.test_shared_answers import SharedAnswersTestCase

SECRET_TITLE = "SECRET-TITLE-Q3-reorg-shortlist"
SECRET_SUMMARY = "SECRET-SUMMARY signed in as someone@example.com"


def _purge():
    with mock.patch.dict("os.environ", {"RETENTION_AGENT_STEP_RESULTS_DAYS": "30"}):
        return retention.run_policy(retention.BY_NAME["agent_step_results"])


class KeptSourcesTests(SharedAnswersTestCase):
    def old_run(self, days=31):
        run = self.make_run(None, age=timedelta(days=days), index=False)
        # Text a purge must not leave behind, on top of the step make_run wrote.
        AgentStep.objects.filter(run=run).update(
            summary=SECRET_SUMMARY,
            result_json={
                "task_id": self.task.task_id,
                "title": SECRET_TITLE,
                "project_id": self.project.project_id,
                "description": SECRET_TITLE,
            },
        )
        AgentStep.objects.filter(run=run).update(created_at=timezone.now() - timedelta(days=days))
        run.refresh_from_db()
        return run

    def test_a_31_day_old_run_keeps_its_citations_and_audit(self):
        run = self.old_run()
        before_sources, before_audience = audit_run(run)
        self.assertEqual(before_audience, sorted([self.uid, self.uid2]))

        result = _purge()

        run = AgentRun.objects.get(pk=run.pk)
        self.assertEqual(result.changed, 1)
        step = run.steps.get()
        self.assertEqual((step.result_json, step.summary), (None, ""))  # the output is gone
        sources = reconstruct_sources_for_run(run)
        self.assertEqual(
            [s["entity_id"] for s in sources], [s["entity_id"] for s in before_sources]
        )
        self.assertEqual(sources[0]["task_id"], str(self.task.task_id))
        after_sources, after_audience = audit_run(run)
        self.assertEqual(after_audience, before_audience)
        self.assertEqual(
            [s["entity_id"] for s in after_sources], [s["entity_id"] for s in before_sources]
        )

    def test_no_content_text_survives_in_the_run(self):
        run = self.old_run()

        _purge()

        kept = json.dumps(AgentRun.objects.get(pk=run.pk).kept_sources)
        self.assertNotIn("SECRET", kept)
        self.assertNotIn("title", kept)
        self.assertNotIn("snippet", kept)
        self.assertIn("task_id", kept)

    def test_a_second_pass_changes_nothing_and_keeps_the_sources(self):
        run = self.old_run()
        _purge()
        first = AgentRun.objects.get(pk=run.pk).kept_sources

        second = _purge()

        self.assertEqual(second.changed, 0)
        self.assertEqual(AgentRun.objects.get(pk=run.pk).kept_sources, first)

    def test_a_late_step_of_the_run_is_merged_not_lost(self):
        run = self.old_run()
        _purge()
        other = TaskMasterFactory(self)
        late = AgentStep.objects.create(
            run=run,
            step_index=1,
            tool_name="fetch_task",
            summary=SECRET_SUMMARY,
            result_json={
                "task_id": other.task_id,
                "title": SECRET_TITLE,
                "project_id": self.project.project_id,
            },
        )
        AgentStep.objects.filter(pk=late.pk).update(created_at=timezone.now() - timedelta(days=40))

        _purge()

        ids = {s["task_id"] for s in AgentRun.objects.get(pk=run.pk).kept_sources["sources"]}
        self.assertEqual(ids, {str(self.task.task_id), str(other.task_id)})

    def test_a_run_with_no_source_keeps_nothing(self):
        run = self.make_run(None, age=timedelta(days=31), cites_task=False, index=False)
        AgentStep.objects.create(run=run, step_index=0, summary=SECRET_SUMMARY, result_json=None)
        AgentStep.objects.filter(run=run).update(created_at=timezone.now() - timedelta(days=40))

        _purge()

        self.assertIsNone(AgentRun.objects.get(pk=run.pk).kept_sources)

    def test_a_run_not_yet_old_keeps_its_step_and_has_no_kept_sources(self):
        run = self.old_run(days=5)

        result = _purge()

        run = AgentRun.objects.get(pk=run.pk)
        self.assertEqual(result.changed, 0)
        self.assertIsNone(run.kept_sources)
        self.assertIsNotNone(run.steps.get().result_json)

    def test_a_failed_audit_still_blanks_and_fails_closed(self):
        run = self.old_run()

        with mock.patch(
            "origin.search_engine.agent.source_visibility.audit_run", side_effect=RuntimeError
        ):
            _purge()

        run = AgentRun.objects.get(pk=run.pk)
        self.assertIsNone(run.steps.get().result_json)
        self.assertIsNone(run.kept_sources["audience"])
        self.assertEqual(run.kept_sources["sources"][0]["task_id"], str(self.task.task_id))


def TaskMasterFactory(case):
    from origin.models.task.task_models import TaskMaster

    return TaskMaster.objects.create(
        team=case.team, project=case.project, title="Other", status="Open"
    )
