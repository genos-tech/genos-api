"""The driver for the dashboard-by-a-skill cases (MIP-2378; the cases are MIP-2409's
`group/skills/t2409-dashboard-page.yaml`).

These are not MCP cases: a dashboard made by a skill is a SCHEDULED run of the in-app agent. So the driver
calls the same function the schedule calls (`run_skill.run_for_answer`) with the same arguments as
`ai_dashboard._run_from_skill`: the published official skill's block, `UNATTENDED_RULES` and the dashboard
`DIRECTIVE`, the same disabled tools (every write), the same effort default. Pass = `parse_dashboard`
accepts the answer (what the server does before it draws the page), then the case's `expect`; Claude then
judges the transcript against the fixture's real counts.

The official skill is synced inside the case's rolled-back transaction, so the eval database needs no
`sync_official_skills` run and keeps nothing of it.

The model is the production adapter of the schedule's rung: this module makes a BILLED call when it is run
for real. `agent_eval_dashboards` is its only caller and refuses without `--ken-approved` and
`--cost-cap-usd`; tests give `run_agent` a stub model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from origin.search_engine.agent.evals.group import mcp_dashboards

CASES_FILE = Path(__file__).parent / "skills" / "t2409-dashboard-page.yaml"

CASE_KEYS = frozenset({"id", "lang", "tags", "fixture", "skill", "query", "note", "expect"})
EXPECT_KEYS = frozenset({"no_writes", "tools_used_contains_any", "answer_does_not_match"})


class SkillDashboardError(Exception):
    """A case file or a seed that cannot be trusted to measure anything."""


@dataclass(frozen=True)
class SkillCase:
    id: str
    lang: str
    skill: str
    query: str
    expect: dict[str, Any]
    note: str = ""


@dataclass
class SkillRun:
    """What happened in one case: kept whole, because the judge reads it."""

    case_id: str
    answer: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)  # {tool_name, arguments, result}
    skipped: str = ""  # why no model was called
    failed: str = ""  # the run's own one-line failure
    page_error: str = ""  # why `parse_dashboard` refused the answer
    model: str = ""
    effort: str = ""


def load_cases(path: Path = CASES_FILE) -> list[SkillCase]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, list) or not data:
        raise SkillDashboardError(f"{path.name}: expected a non-empty list of cases")
    cases: list[SkillCase] = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("id"):
            raise SkillDashboardError(f"{path.name}: a case with no id: {entry!r}")
        where = f"{path.name}:{entry['id']}"
        if unknown := set(entry) - CASE_KEYS:
            raise SkillDashboardError(f"{where}: unknown keys {sorted(unknown)}")
        if entry.get("fixture") != "v1":
            raise SkillDashboardError(f"{where}: only the v1 fixture is supported")
        expect = entry.get("expect")
        if not isinstance(expect, dict) or set(expect) - EXPECT_KEYS:
            raise SkillDashboardError(f"{where}: expect needs only the keys {sorted(EXPECT_KEYS)}")
        for pattern in expect.get("answer_does_not_match") or []:
            re.compile(pattern)
        skill, query = entry.get("skill"), entry.get("query")
        if not skill or not isinstance(query, str) or not query.strip():
            raise SkillDashboardError(f"{where}: needs `skill` and `query`")
        cases.append(
            SkillCase(
                id=str(entry["id"]),
                lang=str(entry.get("lang") or "en"),
                skill=str(skill),
                query=query,
                expect=expect,
                note=str(entry.get("note") or ""),
            )
        )
    return cases


def run_settings(user_id: str = "") -> Any:
    """The model a dashboard schedule runs on (its job's default effort; clamped to the member's plan
    when a member is given)."""
    from origin.services.report_ai import effort  # noqa: PLC0415
    from origin.services.schedule_jobs.report_kit import SUMMARY_EFFORT_DEFAULT  # noqa: PLC0415

    return effort.resolve(None, SUMMARY_EFFORT_DEFAULT, user_id, clamp=bool(user_id))


def answer_rules() -> str:
    """What the schedule tells the model after the skill's text (`ai_dashboard._run_from_skill`; a test
    pins that the two stay the same)."""
    from origin.services.schedule_jobs import run_skill, skill_dashboard  # noqa: PLC0415

    return f"{run_skill.UNATTENDED_RULES} {skill_dashboard.DIRECTIVE}"


def _published_skill_id(slug: str) -> str:
    from origin.models.skills.skill_models import Skill  # noqa: PLC0415
    from origin.services.skills import official  # noqa: PLC0415

    official.sync(apply=True, only={slug})
    account, team = official.find_account()
    skill = Skill.objects.filter(owner=account, team=team, slug=slug).first()
    if skill is None:
        raise SkillDashboardError(f"the official skill {slug!r} could not be published")
    return str(skill.id)


def run_case(case: SkillCase, *, info: dict[str, Any]) -> SkillRun:
    """One case, one repetition, rolled back."""
    from django.conf import settings  # noqa: PLC0415
    from django.test import override_settings  # noqa: PLC0415

    from origin.search_engine.agent.tools.base import ToolContext  # noqa: PLC0415
    from origin.services.schedule_jobs import run_skill, skill_dashboard  # noqa: PLC0415
    from origin.services.schedule_jobs.report_kit import SUMMARY_EFFORT_DEFAULT  # noqa: PLC0415

    run = SkillRun(case_id=case.id)
    # The run refuses to call a model it cannot meter; the rows it writes are rolled back with it.
    engine = {**settings.SEARCH_ENGINE, "AI_COST_METER": True, "AI_CREDITS_AUTHORITATIVE": False}

    def on_tool_result(name: str, result: Any) -> None:
        run.tool_calls.append({"tool_name": name, "result": result})

    with (
        override_settings(SEARCH_ENGINE=engine, GENOS_SKILLS_PUBLIC=True),
        mcp_dashboards.case_sandbox(),
    ):
        chosen = run_settings(info["user_id"])
        if chosen is not None:
            run.model, run.effort = chosen.model, chosen.effort
        skill_id = _published_skill_id(case.skill)
        got = run_skill.run_for_answer(
            ToolContext(team_id=info["team_id"], user_id=info["user_id"]),
            {"source": "skill", "skill_id": skill_id, "input": case.query},
            answer_rules=answer_rules(),
            effort_default=SUMMARY_EFFORT_DEFAULT,
            on_tool_result=on_tool_result,
        )
        run.answer, run.failed, run.skipped = got.answer, got.failed, got.skipped
        if got.answer:
            try:
                skill_dashboard.parse_dashboard(got.answer, default_title=got.title)
            except skill_dashboard.DashboardAnswerError as exc:
                run.page_error = str(exc)
    return run


def check_run(case: SkillCase, run: SkillRun) -> list[dict[str, Any]]:
    """`[{name, ok, detail}]`. A run that never reached a verdict is `infra`, not a model failure."""
    from origin.services.schedule_jobs import run_skill  # noqa: PLC0415

    wrote = run.failed == run_skill.FAILED_WRITE
    if run.skipped:
        return [{"name": "infra", "ok": False, "detail": f"run skipped: {run.skipped}"}]
    if run.failed and not wrote and run.failed != run_skill.FAILED_EMPTY:
        return [{"name": "infra", "ok": False, "detail": run.failed}]
    checks: list[dict[str, Any]] = []

    def add(name: str, reasons: list[str]) -> None:
        checks.append({"name": name, "ok": not reasons, "detail": "; ".join(reasons) or "ok"})

    if run.failed:
        add("run_gave_an_answer", [run.failed])
    else:
        add("parse_dashboard", [run.page_error] if run.page_error else [])
    expect = case.expect
    if expect.get("no_writes"):
        add("no_writes", [run.failed] if wrote else [])
    if any_of := expect.get("tools_used_contains_any"):
        used = {c["tool_name"] for c in run.tool_calls}
        add(
            "tools_used_contains_any",
            [] if used & set(any_of) else [f"used {sorted(used)}, want one of {any_of}"],
        )
    if "answer_does_not_match" in expect:
        add(
            "answer_does_not_match",
            [
                f"answer matches {p!r}"
                for p in expect["answer_does_not_match"]
                if re.search(p, run.answer)
            ],
        )
    return checks


def passed(run: SkillRun, checks: list[dict[str, Any]]) -> bool:
    return not run.skipped and not run.failed and all(c["ok"] for c in checks)
