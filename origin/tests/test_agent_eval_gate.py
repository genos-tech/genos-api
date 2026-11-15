"""The `agent-evals` gate (MIP-1101, simplified in MIP-1537): which PRs start the billed Vertex run.

`agent-evals` used to start a billed Vertex run for any PR under `origin/search_engine/`, so an
MCP-only tool, a refactor or a log line cost a run somebody had to cancel by hand. Now the workflow's
`paths:` is the list of files the retrieval suite really exercises, and its cheap `gate` job lists the
changed files and skips the Vertex steps when none of them is a retrieval file (an edit of the
workflow alone is the case: it matches `paths:` but is tested for free). `scripts/agent_eval_gate.py`
decides.

MIP-1101 also hashed what the in-app model is shown and ran on "the hash moved". That rule is gone:
the retrieval suite cannot see prompts or tool declarations, and
`origin.tests.test_agent_fingerprint_golden` already fails when what the model reads changes.

Two things are pinned here, because a gate that is wrong in either direction costs money or
silently skips a real change:
  * the decision: fail-open on every missing or unreadable input, the labels, the retrieval paths;
  * the workflow text: the cheap job has no secrets and no OIDC token, never fails the run, and the
    billed job reads only an explicit `run=false` as "skip".
"""

import importlib.util
import json
import os
import tempfile
from pathlib import Path
from unittest import TestCase as PlainTestCase
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "agent-evals.yml"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gate = _load("agent_eval_gate")


class DecideTests(PlainTestCase):
    def decide(self, **kw):
        args = {
            "event": "pull_request",
            "labels": set(),
            "changed_files": ["origin/search_engine/agent/tools/list_tasks.py"],
        }
        args.update(kw)
        return gate.decide(**args)

    def test_nothing_retrieval_changed_skips(self):
        run, why = self.decide()
        self.assertFalse(run)
        self.assertIn("no retrieval file changed", why)

    def test_a_workflow_only_edit_skips(self):
        # how `agent-evals.yml` is tested for free: it matches `paths:` but is no retrieval file
        self.assertFalse(self.decide(changed_files=[".github/workflows/agent-evals.yml"])[0])

    def test_an_unreadable_file_list_fails_open(self):
        self.assertTrue(self.decide(changed_files=None)[0])

    def test_labels(self):
        self.assertTrue(self.decide(labels={"run-evals"})[0], "run-evals forces the run")
        self.assertFalse(
            self.decide(labels={"skip-evals"}, changed_files=["origin/search_engine/search.py"])[0],
            "skip-evals forces the skip",
        )
        self.assertTrue(self.decide(labels={"skip-evals", "run-evals"})[0], "both: the run wins")
        self.assertTrue(
            self.decide(labels={"bug"}, changed_files=["origin/search_engine/search.py"])[0],
            "any other label does nothing",
        )
        self.assertFalse(self.decide(labels={"bug"})[0])

    def test_skip_label_skips_even_with_no_file_list(self):
        self.assertFalse(self.decide(labels={"skip-evals"}, changed_files=None)[0])

    def test_dispatch_runs_and_dry_run_only_reports(self):
        self.assertTrue(self.decide(event="workflow_dispatch")[0])
        run, why = self.decide(event="workflow_dispatch", dry_run=True)
        self.assertFalse(run)
        self.assertIn("run=true", why)
        self.assertIn("dry run", why)

    def test_the_weekly_schedule_always_runs(self):
        # on `main` no file "changed": without its own rule the weekly run would skip itself
        run, why = self.decide(event="schedule", changed_files=["README.md"])
        self.assertTrue(run)
        self.assertIn("weekly", why)
        self.assertTrue(self.decide(event="schedule", changed_files=None)[0])
        self.assertTrue(self.decide(event="schedule", labels={"skip-evals"})[0])

    def test_an_empty_file_list_is_a_readable_list(self):
        self.assertFalse(self.decide(changed_files=[])[0])


class ForcedPathsTests(PlainTestCase):
    def forced(self, path):
        return bool(gate.forced_by([path]))

    def test_the_retrieval_files_run_the_evals(self):
        for path in (
            "origin/search_engine/search.py",
            "origin/search_engine/reranker.py",
            "origin/search_engine/query_rewriter.py",
            "origin/search_engine/chunkers/task_chunker.py",
            "origin/search_engine/embeddings/vertex_embedder.py",
            "origin/search_engine/llm/gemini_client.py",
            "origin/search_engine/llm/choice.py",
            "origin/search_engine/ingestion.py",
            "origin/search_engine/agent/acl.py",  # stamped on every indexed chunk
            "origin/search_engine/agent/evals/retrieval_cases.yaml",
            "origin/search_engine/agent/evals/fixture.py",
            "origin/search_engine/agent/evals/runner.py",
            "origin/search_engine/management/commands/agent_eval.py",
            "origin/services/demo_seeder.py",
            "Pipfile",
            "Pipfile.lock",
        ):
            with self.subTest(path=path):
                self.assertTrue(self.forced(path))

    def test_what_the_retrieval_suite_cannot_see_does_not(self):
        # MIP-1527: the run exercises search + the index, never the agent's prompts, tool
        # declarations, tool results or loop, so none of these may start it
        for path in (
            "origin/search_engine/agent/tools/list_tasks.py",  # a tool's implementation
            "origin/search_engine/agent/tools/search_kb.py",  # the tool, not search() itself
            "origin/search_engine/agent/tools/__init__.py",  # a registration
            "origin/search_engine/agent/tools/base.py",  # where MCP-only tools are filed
            "origin/search_engine/agent/prompts.py",  # covered by the fingerprint golden
            "origin/search_engine/agent/controller.py",  # the loop (MIP-1528: not forced)
            "origin/search_engine/agent/model_fence.py",
            "origin/search_engine/agent/fence.py",  # only the reranker uses it, off in CI
            "origin/search_engine/agent/content_length.py",
            "origin/search_engine/agent/tools/task_access.py",
            "origin/search_engine/agent/label_acl.py",
            "origin/search_engine/agent/source_visibility.py",
            "origin/search_engine/agent_views.py",
            "origin/search_engine/models.py",
            "origin/search_engine/credits.py",
            "origin/search_engine/llm/claude_client.py",  # not the rewrite path
            "origin/search_engine/llm/openai_client.py",
            "origin/search_engine/llm/gemini_cache.py",
            "origin/search_engine/management/commands/agent_eval_group.py",
            "origin/search_engine/agent/evals/cases.yaml",
            "origin/search_engine/agent/evals/routing_cases.yaml",
            "origin/views/public/mcp/tools.py",
            "origin/models/task/task_models.py",
            "apis/settings.py",  # agent flags / billing every day: the weekly run covers it
            "README.md",
            ".github/workflows/agent-evals.yml",
            "scripts/retrieval_gate.py",
        ):
            with self.subTest(path=path):
                self.assertFalse(self.forced(path))

    def test_a_retrieval_file_runs_among_other_changes(self):
        run, why = gate.decide(
            event="pull_request",
            labels=set(),
            changed_files=["README.md", "origin/search_engine/search.py"],
        )
        self.assertTrue(run)
        self.assertIn("search.py", why)


class ParseTests(PlainTestCase):
    def test_parse_labels(self):
        self.assertEqual(gate.parse_labels('["run-evals", "bug"]'), {"run-evals", "bug"})
        self.assertEqual(gate.parse_labels("run-evals, bug"), {"run-evals", "bug"})
        self.assertEqual(gate.parse_labels("null"), set())
        self.assertEqual(gate.parse_labels(""), set())
        self.assertEqual(gate.parse_labels(None), set())


class MainTests(PlainTestCase):
    """The entry point: reads the environment, writes `run=` to $GITHUB_OUTPUT, never raises."""

    def run_main(self, *, files=None, env=None):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            out = tmp / "out"
            if files is not None:
                (tmp / "files").write_text("\n".join(files))
            environ = {
                "EVENT": "pull_request",
                "LABELS": "[]",
                "CHANGED_FILES_FILE": str(tmp / "files"),
                "GITHUB_OUTPUT": str(out),
                **(env or {}),
            }
            with mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual(gate.main(), 0)
            return dict(line.split("=", 1) for line in out.read_text().splitlines())

    def test_no_retrieval_file_writes_run_false(self):
        self.assertEqual(self.run_main(files=["README.md"])["run"], "false")

    def test_a_retrieval_file_writes_run_true(self):
        self.assertEqual(self.run_main(files=["origin/search_engine/search.py"])["run"], "true")

    def test_a_missing_file_list_fails_open(self):
        self.assertEqual(self.run_main(files=None)["run"], "true")

    def test_a_crash_in_the_gate_fails_open(self):
        with mock.patch.object(gate, "decide", side_effect=RuntimeError("boom")):
            self.assertEqual(self.run_main(files=["README.md"])["run"], "true")

    def test_labels_come_from_the_environment(self):
        self.assertEqual(
            self.run_main(
                files=["origin/search_engine/search.py"], env={"LABELS": '["skip-evals"]'}
            )["run"],
            "false",
        )
        self.assertEqual(
            self.run_main(files=["README.md"], env={"LABELS": '["run-evals"]'})["run"], "true"
        )

    def test_a_file_name_cannot_write_an_output_of_its_own(self):
        # a PR chooses its file names; a newline in one must not be able to add `run=false`
        out = self.run_main(files=["origin/search_engine/search.py\nrun=false"])
        self.assertEqual(out["run"], "true")
        self.assertEqual(len(out), 2, out)


class WorkflowTextTests(PlainTestCase):
    """The workflow cannot run here: its shape is pinned so a later edit cannot turn the gate off or
    give the cheap job power it must not have."""

    @classmethod
    def setUpClass(cls):
        cls.text = WORKFLOW.read_text()
        cls.wf = yaml.safe_load(cls.text)
        cls.on = cls.wf.get("on", cls.wf.get(True))

    def test_the_gate_job_has_no_secrets_and_no_oidc_token(self):
        job = self.wf["jobs"]["gate"]
        self.assertEqual(job["permissions"], {"contents": "read"})
        self.assertNotIn("services", job)
        self.assertNotIn("secrets.", json.dumps(job))
        self.assertNotIn("id-token", json.dumps(job))
        # the whole workflow still asks for the token (the billed job needs it): the cheap job opts out
        self.assertEqual(self.wf["permissions"].get("id-token"), "write")

    def test_the_surface_hash_is_gone(self):
        self.assertNotIn("surface", self.wf["jobs"])
        self.assertNotIn("agent_surface_hash", self.text)
        self.assertNotIn("pipenv", json.dumps(self.wf["jobs"]["gate"]))
        self.assertFalse((ROOT / "scripts" / "agent_surface_hash.py").exists())

    def test_every_gate_step_but_the_checkout_cannot_fail_the_job(self):
        steps = self.wf["jobs"]["gate"]["steps"]
        self.assertTrue(steps[0]["uses"].startswith("actions/checkout@"))
        for step in steps[1:]:
            with self.subTest(step=step.get("name") or step.get("uses") or step.get("run")):
                self.assertTrue(step.get("continue-on-error"), "a failing gate step must fail OPEN")

    def test_the_gate_step_always_runs_and_reads_only_the_environment(self):
        step = next(s for s in self.wf["jobs"]["gate"]["steps"] if s.get("id") == "gate")
        self.assertIn("!cancelled()", step["if"])
        self.assertEqual(step["run"].strip(), "python3 scripts/agent_eval_gate.py")
        for name in ("EVENT", "LABELS", "CHANGED_FILES_FILE"):
            self.assertIn(name, step["env"])

    def test_a_label_is_never_interpolated_into_a_shell_command(self):
        for job in self.wf["jobs"].values():
            for step in job["steps"]:
                run = step.get("run", "")
                self.assertNotIn("github.event.pull_request.labels", run)
                self.assertNotIn("github.event.label", run)
                self.assertNotIn("github.head_ref", run)

    def test_the_billed_job_waits_for_the_gate_but_still_runs_when_it_broke(self):
        job = self.wf["jobs"]["agent-evals"]
        self.assertEqual(job["needs"], "gate")
        condition = " ".join(job["if"].split())
        self.assertIn("!cancelled()", condition)
        self.assertIn("needs.gate.result != 'skipped'", condition)
        # a failed gate job must not be able to skip the evals
        self.assertNotIn("needs.gate.result == 'success'", condition)

    def test_only_an_explicit_false_skips_the_billed_steps(self):
        step = next(s for s in self.wf["jobs"]["agent-evals"]["steps"] if s.get("id") == "changes")
        self.assertEqual(step["env"]["GATE_RUN"], "${{ needs.gate.outputs.run }}")
        self.assertIn('[ "$GATE_RUN" = "false" ]', step["run"])
        self.assertEqual(step["run"].count("relevant=false"), 2)  # no GCP, and the explicit skip
        self.assertTrue(step["run"].rstrip().endswith('echo "relevant=true" >> "$GITHUB_OUTPUT"'))

    def test_every_billed_step_is_still_behind_the_decision(self):
        steps = self.wf["jobs"]["agent-evals"]["steps"]
        billed = [
            s
            for s in steps
            if "google-github-actions/auth" in s.get("uses", "") or "agent_eval" in s.get("run", "")
        ]
        self.assertGreaterEqual(len(billed), 3)
        for step in billed:
            self.assertIn("steps.changes.outputs.relevant == 'true'", step["if"])

    def test_the_escapes_exist(self):
        self.assertIn("workflow_dispatch", self.on)
        self.assertIn("labeled", self.on["pull_request"]["types"])
        self.assertIn("synchronize", self.on["pull_request"]["types"])
        self.assertIn("dry_run", self.on["workflow_dispatch"]["inputs"])
        self.assertIn("run-evals", self.text)
        self.assertIn("skip-evals", self.text)
        self.assertEqual(gate.RUN_LABEL, "run-evals")
        self.assertEqual(gate.SKIP_LABEL, "skip-evals")
        # an unrelated label must neither start a run nor cancel one
        self.assertIn("github.event.label.name == 'run-evals'", self.wf["jobs"]["gate"]["if"])
        self.assertIn("label-{0}", self.wf["concurrency"]["group"])

    def test_the_workflow_edit_itself_exercises_the_gate_but_a_script_edit_does_not_start_one(self):
        paths = self.on["pull_request"]["paths"]
        # listed so the PR that changes the gate runs it once: no retrieval file, so no billed step
        self.assertIn(".github/workflows/agent-evals.yml", paths)
        for path in paths:
            self.assertNotIn("scripts", path)

    def test_the_pull_request_paths_are_exactly_the_retrieval_list_plus_the_workflow(self):
        # one list (RETRIEVAL_PATHS in the gate script), mirrored here: a drift would either start
        # a billed run for nothing or skip a retrieval change
        self.assertEqual(
            self.on["pull_request"]["paths"],
            [*gate.RETRIEVAL_PATHS, ".github/workflows/agent-evals.yml"],
        )
        # no negation left over from the old `origin/search_engine/**` filter, no broad directory
        for path in self.on["pull_request"]["paths"]:
            self.assertFalse(path.startswith("!"), path)
            self.assertNotIn(path, ("origin/search_engine/**", "origin/search_engine/agent/**"))
            self.assertNotEqual(path, "apis/settings.py")

    def test_no_push_trigger_and_a_weekly_schedule(self):
        # MIP-1527: a merge only repeated the PR's run (and a successor's push cancelled it)
        self.assertNotIn("push", self.on)
        self.assertIn("workflow_dispatch", self.on)
        (entry,) = self.on["schedule"]
        minute, hour, dom, month, dow = entry["cron"].split()
        self.assertEqual((dom, month), ("*", "*"))
        self.assertTrue(dow.isdigit(), "weekly, not nightly: one weekday")
        # the same repo-variable switch the other scheduled evals use
        condition = " ".join(self.wf["jobs"]["agent-evals"]["if"].split())
        self.assertIn("vars.RUN_AGENT_EVALS == 'true'", condition)
        self.assertIn("github.event_name == 'pull_request'", condition)
        self.assertNotIn("push", self.wf["jobs"]["gate"]["if"])

    def test_the_floors_live_only_in_the_gate_script(self):
        self.assertNotIn("EVAL_MIN", self.text)
        self.assertNotIn("EVAL_MIN", json.dumps(self.wf.get("env", {})))
        step = next(
            s
            for s in self.wf["jobs"]["agent-evals"]["steps"]
            if "retrieval_gate" in s.get("run", "")
        )
        self.assertIn("scripts/retrieval_gate.py eval.out", step["run"])
        self.assertIn("steps.changes.outputs.relevant == 'true'", step["if"])

    def test_the_behaviour_smoke_is_gone(self):
        self.assertNotIn("simple_rag_wip_tasks", self.text)
        self.assertNotIn("--judge", self.text)
        for step in self.wf["jobs"]["agent-evals"]["steps"]:
            run = step.get("run", "")
            if "agent_eval" in run:
                # the only suite this workflow runs is the retrieval one
                for line in run.splitlines():
                    if "manage.py agent_eval " in line and "agent_eval_setup" not in line:
                        self.assertIn("--retrieval", line)

    def test_the_motivating_prs_are_not_forced(self):
        # #619 (MCP-only tools) touched these; none can move what the model reads
        self.assertEqual(
            gate.forced_by(
                [
                    "origin/search_engine/agent/controller.py",
                    "origin/search_engine/agent/tools/__init__.py",
                    "origin/search_engine/agent/tools/base.py",
                    "origin/tests/test_mcp_only_tools.py",
                    "origin/views/public/mcp/tools.py",
                    # #626
                    "origin/search_engine/agent/tools/get_task_history.py",
                ]
            ),
            [],
        )

    def test_there_is_no_deploy_job(self):
        # MIP-1537: it needed a push event the workflow no longer has, and the GCP env is gone
        self.assertEqual(set(self.wf["jobs"]), {"gate", "agent-evals"})
