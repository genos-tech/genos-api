"""`python manage.py agent_eval_group`: one paid session, many questions (MIP-1271).

Runs the eval-group cases (`agent/evals/group/*.yaml`, plus the legacy suites on request)
across ARMS: arm0 (every flag off), one arm per `group/arms/<tag>.yaml`, and `all-on` (the
union of every arm). Each item arm runs only the cases carrying its tag, and the baseline
is run once and reused, so the bill is the selection twice (arm0, all-on) plus each item's
own cases once. Prints a markdown matrix, per-arm totals with the delta against arm0, and
a bisect hint when all-on regresses a case arm0 passes.

    # what would it cost? makes NO model call and needs no credentials
    python manage.py agent_eval_group --dry-run

    # one real case on arm0: measured tokens and cost, and the projection for the plan
    python manage.py agent_eval_group --measure-one --cost-cap-usd 1

    # the session, with a hard stop
    python manage.py agent_eval_group --tag t977 --tag t991 --cost-cap-usd 40 --json run.jsonl

`RUNBOOK.md` next to this file's package (`agent/evals/group/`) has the exact sequence.
Ken's cap for the whole paid session is US$65; the command will not make a real run without
`--cost-cap-usd`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from origin.search_engine.agent.evals.group import harness
from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.agent.evals.group.case_runner import DefaultCaseRunner


class Command(BaseCommand):
    help = "Run eval-group cases across flag arms; matrix, cost per arm, delta vs arm0."

    #: injectable for tests: `(judge: bool) -> CaseRunner`
    runner_factory = staticmethod(lambda judge: DefaultCaseRunner(judge=judge))
    #: injectable for tests: `() -> list[str]` problems with the OpenSearch alias (MIP-1504)
    preflight = staticmethod(lambda: harness.opensearch_preflight())

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            "--arms",
            help="Comma list: arm names from group/arms/ and/or all-on. Default: all of them.",
        )
        parser.add_argument(
            "--tag",
            action="append",
            default=[],
            help="Select the cases carrying this tag. Repeatable.",
        )
        parser.add_argument(
            "--case",
            action="append",
            default=[],
            dest="case_ids",
            help="Select a case id. Repeatable.",
        )
        parser.add_argument("--lang", choices=("en", "ja"))
        parser.add_argument(
            "--reps", type=int, default=1, help="Repetitions per case (a case's own `reps:` wins)."
        )
        parser.add_argument(
            "--cost-cap-usd",
            type=float,
            default=None,
            help="Hard stop, checked before every model call.",
        )
        parser.add_argument(
            "--max-cases", type=int, default=None, help="Keep only the first N selected cases."
        )
        parser.add_argument(
            "--measure-one",
            action="store_true",
            help="Run exactly one case on arm0; print measured cost and the plan projection.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print the plan and projected cost. Makes no model call, needs no credentials.",
        )
        parser.add_argument(
            "--judge",
            action="store_true",
            help="Also score behaviour answers with the LLM judge (extra calls).",
        )
        parser.add_argument(
            "--concurrency",
            type=int,
            default=1,
            help="Cases run in parallel WITHIN an arm (arms never overlap).",
        )
        parser.add_argument(
            "--json",
            dest="json_out",
            help="Write every CaseResult, one per line, to this file (also .report.md and .summary.json).",
        )
        parser.add_argument(
            "--include-legacy",
            action="store_true",
            help="With no --tag/--case, also select the legacy cases.yaml and routing suites.",
        )
        parser.add_argument(
            "--skip-unavailable",
            action="store_true",
            help="Drop arms whose flags this stack lacks instead of refusing.",
        )
        parser.add_argument(
            "--model",
            help="Model to PRICE the projection on (default: the provider default). Does not change what runs.",
        )
        parser.add_argument(
            "--provider", default=None, help="Bind an LlmChoice (with --effort) around every case."
        )
        parser.add_argument("--effort", default=None)
        parser.add_argument(
            "--no-locale",
            action="store_true",
            help=(
                "Do NOT pass a case's `lang` as the member's locale. Production does, so use this "
                "only to reproduce an older run."
            ),
        )
        parser.add_argument(
            "--preflight-only",
            action="store_true",
            help=(
                "Check the run environment (the OpenSearch alias: an alias, with a knn_vector "
                "`embedding`) and exit; status 1 when it is not usable. No model call."
            ),
        )
        parser.add_argument(
            "--stop-file",
            default="",
            help="If this file exists, stop cleanly before the next case.",
        )

    # ------------------------------------------------------------------ #

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            plan, notes = self._plan(options)
        except harness.HarnessError as exc:
            raise CommandError(str(exc)) from exc

        model = harness.planning_model(options.get("model"))
        judge = bool(options.get("judge"))
        for note in notes + plan.notes:
            self.stdout.write(f"note: {note}")

        if options["dry_run"]:
            self._print_plan(plan, model, judge, options)
            return
        if options["preflight_only"]:
            problems = self.preflight()
            for problem in problems:
                self.stderr.write(self.style.ERROR(f"PREFLIGHT: {problem}"))
            if problems:
                sys.exit(1)
            self.stdout.write("preflight ok: the OpenSearch alias is an alias with a knn_vector.")
            return

        if options.get("cost_cap_usd") is None:
            raise CommandError(
                "A real run needs --cost-cap-usd (use --dry-run to only see the plan)."
            )
        if options["cost_cap_usd"] <= 0:
            raise CommandError("--cost-cap-usd must be positive.")
        choice = self._choice(options)

        projection = harness.project_cost(plan, model, judge=judge)
        runnable = projection["items"]
        avg_estimate = projection["cost_usd"] / runnable if runnable else 0.0
        guard = harness.SessionSpend(
            options["cost_cap_usd"], since=timezone.now(), estimate_case=avg_estimate
        )
        runner = self.runner_factory(judge)
        if isinstance(runner, DefaultCaseRunner):
            runner.apply_locale = not options["no_locale"]
            # Refuse BEFORE the first paid call (MIP-1504). Only for the real runner: a test
            # that injects a fake one has no index to check.
            if harness.plan_needs_search(plan):
                problems = self.preflight()
                if problems:
                    raise CommandError(
                        "PREFLIGHT failed, nothing was run (no override: fix the environment, "
                        "or use --tag/--case to select only routing cases):\n  - "
                        + "\n  - ".join(problems)
                    )

        if options["measure_one"]:
            self._measure_one(plan, runner, guard, model, judge, choice, options)
            return

        if projection["cost_usd"] > options["cost_cap_usd"]:
            self.stdout.write(
                self.style.WARNING(
                    f"projection {harness.usd(projection['cost_usd'])} is above the cap "
                    f"{harness.usd(options['cost_cap_usd'])}: the run will stop early with a partial report"
                )
            )
        report = self._execute(plan, runner, guard, choice, options)
        text = harness.render_markdown(
            report.results, plan, report, model=model, cap_usd=options["cost_cap_usd"]
        )
        self.stdout.write(text)
        self._write_outputs(options, report, plan, text)
        if report.aborted:
            self.stderr.write(self.style.ERROR(f"ABORTED: {report.aborted}"))
            sys.exit(3)

    # ------------------------------------------------------------------ #

    def _plan(self, options: dict[str, Any]) -> tuple[harness.Plan, list[str]]:
        notes: list[str] = []
        cases = harness.load_cases(
            group_dir=harness.GROUP_DIR, evals_dir=harness.EVALS_DIR, include_legacy=True
        )
        selected = harness.select_cases(
            cases,
            tags=options["tag"],
            case_ids=options["case_ids"],
            lang=options.get("lang"),
            include_legacy=options["include_legacy"],
            max_cases=options.get("max_cases"),
        )
        if not selected:
            raise harness.HarnessError(
                "no case selected (is there any file in agent/evals/group/?)"
            )
        arms = harness.load_arms(harness.ARMS_DIR)
        requested = [a.strip() for a in (options.get("arms") or "").split(",") if a.strip()] or None

        in_play = (
            list(arms)
            if requested is None or harness.ALL_ON in requested
            else [a for a in requested if a in arms]
        )
        for name in list(in_play):
            problems = harness.validate_overrides(arms[name])
            if problems:
                if options["skip_unavailable"]:
                    notes.append("DROPPED " + "; ".join(problems))
                    del arms[name]
                    if requested:
                        requested = [r for r in requested if r != name]
                else:
                    raise harness.HarnessError(
                        "\n".join(problems)
                        + "\n(--skip-unavailable drops such arms; a typo'd flag would otherwise compare "
                        "the baseline with itself)"
                    )
        plan = harness.build_plan(selected, arms, arm_names=requested, reps=max(options["reps"], 1))
        if harness.ALL_ON in [a.name for a in plan.arms]:
            all_on = harness.all_on_arm(arms)
            problems = harness.validate_overrides(all_on)
            if problems:
                raise harness.HarnessError("\n".join(problems))
        return plan, notes

    def _choice(self, options: dict[str, Any]) -> Any:
        provider = (options.get("provider") or "").strip() or None
        effort = (options.get("effort") or "").strip() or None
        if bool(provider) != bool(effort):
            raise CommandError("--provider and --effort go together.")
        if not provider:
            return None
        from django.conf import settings  # noqa: PLC0415

        from origin.search_engine.llm.choice import LlmChoice  # noqa: PLC0415

        try:
            model = settings.LLM_CATALOG.model_for_effort(provider, effort)
        except Exception as exc:  # noqa: BLE001 — bad CLI input
            raise CommandError(f"Cannot resolve {provider}/{effort}: {exc}") from exc
        return LlmChoice(provider=provider, model=model, effort=effort)

    # ------------------------------------------------------------------ #

    def _print_plan(
        self, plan: harness.Plan, model: str, judge: bool, options: dict[str, Any]
    ) -> None:
        w = self.stdout.write
        projection = harness.project_cost(plan, model, judge=judge)
        w("=== agent_eval_group plan (dry run: no model call, no fixture touched) ===")
        w(
            f"cases selected: {len(plan.cases)}   arms: {', '.join(a.name for a in plan.arms)}   reps default: {options['reps']}"
        )
        w(
            f"projection priced on model: {model or '(none)'} (routing cases on {harness.ROUTING_MODEL})"
            + (
                "   WARNING: no catalog price for it, cost is unknown"
                if projection["unpriced"]
                else ""
            )
        )
        warning = self._locale_warning(plan, options)
        if warning:
            w(warning)
        w("")
        w("| arm | overrides | runs | model calls | projected cost |")
        w("|---|---|---|---|---|")
        for arm in plan.arms:
            row = projection["per_arm"].get(arm.name, {"items": 0, "calls": 0, "cost_usd": 0.0})
            skipped = sum(1 for i in plan.for_arm(arm.name) if i.skip)
            w(
                f"| {arm.name} | {arm.effective or '-'} | {int(row['items'])}"
                + (f" (+{skipped} skipped)" if skipped else "")
                + f" | {int(row['calls'])} | ${row['cost_usd']:.2f} |"
            )
        w("")
        w(
            f"TOTAL: {projection['items']} runs, ~{int(projection['calls'])} model calls, projected ${projection['cost_usd']:.2f}"
            + (
                f" (cap {harness.usd(options['cost_cap_usd'])})"
                if options.get("cost_cap_usd")
                else ""
            )
        )
        w(
            "The projection uses round per-case token guesses; run --measure-one to replace them with a measurement."
        )
        if harness.plan_needs_search(plan):
            w(
                "A real run first checks the OpenSearch alias (an alias, with a knn_vector "
                "`embedding`) and refuses otherwise; `--preflight-only` runs that check alone."
            )
        w("")
        w("| case | lang | kind | tags | fixture | reps | runs on |")
        w("|---|---|---|---|---|---|---|")
        for case in plan.cases:
            arms_for = [
                a.name
                for a in plan.arms
                if any(i.case is case and not i.skip for i in plan.for_arm(a.name))
            ]
            skipped_on = [
                a.name
                for a in plan.arms
                if any(i.case is case and i.skip for i in plan.for_arm(a.name))
            ]
            w(
                f"| {case.id} | {case.lang} | {case.kind} | {','.join(sorted(case.tags - {'eval-group'}))} | "
                f"{case.fixture or '-'} | {case.reps or options['reps']} | {','.join(arms_for) or '-'}"
                + (f" (skipped on {','.join(skipped_on)})" if skipped_on else "")
                + " |"
            )

    def _locale_warning(self, plan: harness.Plan, options: dict[str, Any]) -> str:
        """One line when a selected `lang: ja` case would run without the language directive."""
        ja = [c for c in plan.cases if c.lang == "ja"]
        if not ja:
            return ""
        if options.get("no_locale"):
            return (
                f"WARNING: {len(ja)} selected case(s) have lang: ja but --no-locale is set: they run "
                "without the Japanese language directive production gives a ja member."
            )
        unaware = [c.id for c in ja if c.kind == "resume" and not _resume_runner_reads_locale()]
        if unaware:
            return (
                f"WARNING: {len(unaware)} selected resume case(s) have lang: ja but resume_runner does not "
                "read `locale`: they run without the Japanese language directive."
            )
        return ""

    def _measure_one(self, plan, runner, guard, model, judge, choice, options) -> None:
        candidates = [i for i in plan.for_arm(harness.ARM0) if not i.skip]
        pick = next(
            (i for i in candidates if i.case.kind != "resume"),
            candidates[0] if candidates else None,
        )
        if pick is None:
            raise CommandError("nothing runnable on arm0 to measure")
        one = harness.Plan(
            cases=[pick.case],
            arms=[plan.arms[0]],
            items=[harness.PlanItem(case=pick.case, arm=plan.arms[0], rep=1)],
        )
        self.stdout.write(f"measuring one case on arm0: {pick.case.id} ({pick.case.kind})")
        report = self._execute(one, runner, guard, choice, {**options, "concurrency": 1})
        result = report.results[0]
        u = result["usage"]
        est = harness.estimate_item(pick.case, model, judge=judge)
        self.stdout.write(
            f"measured: calls={u['calls']} input_tokens={u['input_tokens']} (cached {u['cached_tokens']}) "
            f"output_tokens={u['output_tokens']} cost=${u['cost_usd']:.4f}"
            + (
                f" (unpriced calls: {result.get('unpriced_calls')}, a lower bound)"
                if result.get("unpriced_calls")
                else ""
            )
        )
        self.stdout.write(
            f"verdict: {'PASS' if result['passed'] else 'FAIL'}  tools: {result['tools']}"
            + (f"  skipped: {result['skipped']}" if result.get("skipped") else "")
            + (f"  error: {result['error']}" if result.get("error") else "")
        )
        if not est["cost_usd"] or not u["cost_usd"]:
            self.stdout.write(
                "cannot scale the projection: the estimate or the measurement has no price."
            )
            return
        scale = u["cost_usd"] / est["cost_usd"]
        projection = harness.project_cost(plan, model, judge=judge, scale=scale)
        self.stdout.write(
            f"estimated for this case: ${est['cost_usd']:.4f} -> measured/estimate = {scale:.2f}\n"
            f"PROJECTION for the whole plan, scaled by that ratio: {projection['items']} runs, "
            f"~{int(projection['calls'])} calls, ${projection['cost_usd']:.2f} on {model}"
        )
        for name, row in projection["per_arm"].items():
            self.stdout.write(f"  {name}: {int(row['items'])} runs, ${row['cost_usd']:.2f}")
        self.stdout.write(
            "One case is a rough sample: re-run --measure-one on a different case before trusting it."
        )
        self._write_outputs(
            options, report, one, harness.render_markdown(report.results, one, report, model=model)
        )

    def _execute(self, plan, runner, guard, choice, options) -> harness.RunReport:
        sink = None
        json_out = options.get("json_out")
        if json_out:
            Path(json_out).parent.mkdir(parents=True, exist_ok=True)
            sink = open(json_out, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally

        def emit(result: Any) -> None:
            if sink is not None:
                sink.write(harness.result_json(result) + "\n")
                sink.flush()

        def bind(call):
            if choice is None:
                return call()
            from origin.search_engine.llm.choice import reset_llm_choice, set_llm_choice  # noqa: PLC0415

            token = set_llm_choice(choice)
            try:
                return call()
            finally:
                reset_llm_choice(token)

        opts = harness.RunOptions(
            concurrency=max(int(options.get("concurrency") or 1), 1),
            cap_usd=options.get("cost_cap_usd"),
            stop_file=options.get("stop_file") or "",
            emit=emit,
            progress=lambda m: self.stdout.write(m),
            bind=bind,
        )
        try:
            # `kind: resume` cases drive the real views through the test client, host `testserver`
            with (
                group_usage.listen(guard.on_record),
                harness.guarded_llm(guard),
                harness.allow_test_host(),
            ):
                return harness.run_plan(plan, runner, guard, opts)
        finally:
            if sink is not None:
                sink.close()

    def _write_outputs(self, options, report, plan, text) -> None:
        json_out = options.get("json_out")
        if not json_out:
            return
        base = Path(json_out)
        base.with_suffix(".report.md").write_text(text, encoding="utf-8")
        summary = harness.summarize(report.results, plan.arms)
        summary["aborted"] = report.aborted
        summary["spent_usd"] = report.spent_usd
        base.with_suffix(".summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )


def _resume_runner_reads_locale() -> bool:
    """Whether `resume_runner` (another agent's module) mentions `locale` at all."""
    import importlib.util  # noqa: PLC0415

    try:
        spec = importlib.util.find_spec("origin.search_engine.agent.evals.group.resume_runner")
    except (ImportError, ValueError):
        return False
    if spec is None or not spec.origin:
        return False
    try:
        return "locale" in Path(spec.origin).read_text(encoding="utf-8")
    except OSError:
        return False
