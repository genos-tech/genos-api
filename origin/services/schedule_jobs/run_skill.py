"""Job `run_skill` (MIP-2045): a schedule runs a skill the schedule's owner may run.

"08:30 every weekday: run my /weekly-review". A skill is a note-backed runbook (`origin.models.skills`,
MIP-2039); this job gives it a clock. Everything else about a schedule stays what it was: the
recurrence, the pause rules, the plan gate, the Inbox delivery, the "Run now" button.

WHAT RUNS. The same skill text and the same fenced block as `/name` in Spotlight
(`services/skills/run.py`), resolved for the schedule's OWNER at every run, so a skill that was
deleted, hidden, taken down or un-shared since fails the run in words instead of running on the
authority it once had. The version is the PUBLISHED one and it is pinned when the schedule is saved
(`params.version_no`): an unattended run never reads the owner's live draft, and a later publish
(or a stranger's update of an installed skill) does not change what runs until the owner edits the
schedule. A private skill that was never published has no version, so it cannot be scheduled.

WHAT IT MAY DO. Read, as the owner. Every tool that writes is undeclared (nobody is there to
approve a card), web search is out, and the caller's own plan gates still apply, unioned with the
skill's ceiling and the outside-author rules: the run never has a tool the owner would not have in
Spotlight. If the loop still asks to write, the run fails (`paused`).

WHAT IT COSTS. The owner's AI credits, as `surface="schedule"` (the surface the AI sentences of a
report already use, `billable_surfaces` in `credit_policy.yaml`), under one metered request whose id
goes on the Inbox item's cost block (`Report.ai_request_id`). At zero credits nothing is called: the
report is one line saying so (`Report.ai_skipped`), the run counts as a success and the schedule
keeps its time, so it runs again when credits are back. The loop also stops itself mid-run when the
balance is spent (`credit_budget_usd_micro`), and the partial answer is kept.

WHERE IT GOES. The owner's Inbox (the cost is on the Inbox item), or a chat the owner is ALONE in
(MIP-2196): one system-authored message, the cost on a cost-only Inbox item (MIP-2351). Never a chat
other people read (`schedule_audience.AI_NOT_ALONE`), checked when the schedule is saved and again before every run, so a
person added to that chat since stops the run BEFORE the model is called. One who joins while the
model runs gets nothing: the report is filed in the Inbox with its cost (`schedule_delivery.deliver`).

SWITCHED OFF. `GENOS_SKILLS` off: the job is not offered (`Job.available`), a schedule cannot be
saved with it, and an existing row's run fails with a sentence. Nothing is called.

The skill's text is the owner's (or a teammate's, or a stranger's installed one): it reaches the
model only inside the fence of `skills/run.build_block`, and the answer comes back as DATA: it is
cut into short plain lines and passed through the delivery's own checks before it is stored.
"""

from __future__ import annotations

import logging
import re
import textwrap
from dataclasses import dataclass, replace
from typing import Any

from django.conf import settings

from origin.search_engine.agent.tools.base import ToolContext
from origin.services.schedule_jobs import AI_EFFORT_KEY, Job, Report, ReportSection, register_job
from origin.services.schedule_jobs.report_kit import clip

log = logging.getLogger("origin.schedules")

JOB_KEY = "run_skill"
EFFORT_DEFAULT = "medium"
MAX_INPUT_CHARS = 1000

# How many schedules of this job a member may keep, by their EFFECTIVE tier (design K9, the
# maintainer confirmed 2026-11-23). None = unlimited. Kept here, like `skills.service.SKILLS_MAX_BY_TIER`,
# until the plan table grows a key for it. A schedule's plan gate (Core and up) applies on top.
SKILL_SCHEDULES_MAX_BY_TIER: dict[str, int | None] = {
    "free": 0,
    "core": 3,
    "pro": 10,
    "max": 30,
    "enterprise": None,
}

# The words of a run that did not call the model, by the code `schedule_delivery` files.
_SKIPPED = {
    "no_credits": (
        "Not run: you have no AI credits left this month. It runs again at its next time once "
        "your credits are back."
    ),
}
_SKIPPED["demo_limit"] = (
    "Not run: the demo's AI is at its limit for today. Create a free account to keep using Genos AI."
)
_SKIPPED_OTHER = "Not run: AI is not available right now. It runs again at its next time."
# A run that called the model and then could not give an answer: one line, filed with its cost
# (a failed request is not billed).
FAILED_RUN = "The skill run did not finish, so there is nothing to report. It is not charged."
FAILED_WRITE = (
    "The skill asked to change something, which a scheduled run may not do (it only reads). "
    "It is not charged."
)
FAILED_EMPTY = "The skill run gave no answer. It is not charged."

# What the model is told on top of the skill block (`skills/run.build_block`): nobody is here.
UNATTENDED = (
    "SCHEDULED RUN: this skill is running on a schedule and nobody is here to answer. Do not ask "
    "questions: use the defaults the steps state, or your best judgement, and say in one line what "
    "you assumed. You can only read in this run, so where the steps ask for a change, list it as a "
    "suggestion for the person to make later. Write the answer as a short report: a first line "
    "that sums it up, then short lines, one fact each, at most 20 lines. No greeting and no "
    "closing question."
)
# The same words without the last two sentences (the shape of the answer), for a run whose answer
# is not a report: a dashboard's is data (MIP-2399). `UNATTENDED` above is untouched.
UNATTENDED_RULES = UNATTENDED[: UNATTENDED.index(" Write the answer as a short report")]


def _enabled() -> bool:
    return bool(getattr(settings, "GENOS_SKILLS", False))


def skill_schedules_limit(user_id: Any) -> int | None:
    """How many `run_skill` schedules this member may keep (None = unlimited). A tier that cannot
    be read is unlimited, like the other plan gates: our failure never blocks a member."""
    from origin.search_engine import quota  # noqa: PLC0415 - search_engine is heavy

    try:
        tier = quota.get_effective_tier(str(user_id))
    except Exception:  # noqa: BLE001
        log.exception("skill_schedules_limit: tier lookup failed for user=%s", user_id)
        return None
    return SKILL_SCHEDULES_MAX_BY_TIER.get(tier)


# -- saving: what the owner may schedule ---------------------------------------------------------


def clean_input(value: Any) -> str:
    """The words the skill is given, as stored: text only, hidden characters removed, at most
    `MAX_INPUT_CHARS`. Empty is allowed (the skill's own defaults apply)."""
    from origin.services.schedules import ScheduleError  # noqa: PLC0415 (import cycle)
    from origin.services.skills import run as skill_run  # noqa: PLC0415

    if value is None:
        return ""
    if not isinstance(value, str):
        raise ScheduleError("`input` must be text.")
    cleaned = skill_run.clean_text(value).strip()
    if len(cleaned) > MAX_INPUT_CHARS:
        raise ScheduleError(f"`input` is too long: at most {MAX_INPUT_CHARS} characters.")
    return cleaned


def pin_skill(ctx: ToolContext, params: dict[str, Any]) -> dict[str, Any]:
    """`{skill_id, version_no, input?}` to store, or a refusal in words. The owner must be allowed
    to run the skill NOW (the same rule as `/name`: theirs, a teammate's published team skill, an
    installed or official one), the skill must have a published version, and the version is pinned
    in the result. Shared by the "Run a skill" job and a dashboard made by a skill (MIP-2399)."""
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.services.schedules import ScheduleError  # noqa: PLC0415 (import cycle)
    from origin.services.skills import run as skill_run  # noqa: PLC0415
    from origin.services.skills import service  # noqa: PLC0415

    if not _enabled():
        raise ScheduleError("Skills are not available right now.")
    user = CustomUser.objects.filter(pk=ctx.user_id).first()
    if user is None:
        raise ScheduleError("Skills are not available right now.")
    pinned = params.get("version_no")
    try:
        resolved = skill_run.resolve(
            user,
            ctx.team_id,
            {"id": params.get("skill_id")},
            pinned_version=pinned if isinstance(pinned, int) and pinned > 0 else None,
            published_only=True,
        )
    except service.SkillNotFound:
        raise ScheduleError("That skill was not found, or you may not run it.") from None
    except service.SkillNeedsConfirmation:
        raise ScheduleError(
            "Run this skill once in Genos and confirm it before you schedule it: nobody is there "
            "to confirm it when the schedule runs."
        ) from None
    except service.SkillError as exc:
        raise ScheduleError(str(exc)[:200]) from None
    out: dict[str, Any] = {"skill_id": resolved.skill_id, "version_no": resolved.version_no}
    if text := clean_input(params.get("input")):
        out["input"] = text
    return out


def check_params(ctx: ToolContext, params: dict[str, Any], schedule_id: Any) -> dict[str, Any]:
    """The params to store, or a refusal in words: the skill is pinned (`pin_skill`), and the
    member's plan allows this many `run_skill` schedules."""
    from origin.models.common.schedule_models import MemberSchedule  # noqa: PLC0415
    from origin.services.schedules import ScheduleLimitReached  # noqa: PLC0415 (import cycle)

    out = pin_skill(ctx, params)
    limit = skill_schedules_limit(ctx.user_id)
    # An edit of a schedule that ALREADY runs a skill adds none: after a plan downgrade it stays
    # editable (its input, its skill), as every existing schedule keeps running.
    already_one = (
        schedule_id is not None
        and MemberSchedule.objects.filter(pk=schedule_id, user_id=ctx.user_id, job=JOB_KEY).exists()
    )
    if limit is not None and not already_one:
        others = MemberSchedule.objects.filter(user_id=ctx.user_id, job=JOB_KEY)
        used = others.count()
        if used >= limit:
            raise ScheduleLimitReached(
                f"Your plan keeps up to {limit} schedules that run a skill, and you have {used}. "
                "Delete one or upgrade your plan.",
                limit=limit,
                used=used,
            )
    return out


# -- running -------------------------------------------------------------------------------------


def _fail(message: str):
    from origin.services.schedules import ScheduleRunError  # noqa: PLC0415 (import cycle)

    return ScheduleRunError(message)


def _skip_code(user_id: str, run_settings: Any) -> str:
    """Why this run must not call a model, as the code a delivery files ("" = it may). In the order
    a person can act on: the operator's switches, then the member's own credits."""
    from origin.services.report_ai import llm  # noqa: PLC0415

    if settings.SEARCH_ENGINE.get("AI_AGENT_KILL_SWITCH"):
        return llm.SKIP_OFF
    if run_settings is None:
        return llm.SKIP_NO_MODEL
    # A call that would not be recorded is silent spend, which is never allowed.
    if not settings.SEARCH_ENGINE.get("AI_COST_METER"):
        return llm.SKIP_METER_OFF
    if not llm.configured(run_settings.provider):
        return llm.SKIP_NOT_CONFIGURED
    return llm.credit_block(user_id) or llm.demo_block(user_id)


def _credit_budget_usd_micro(user_id: str) -> int:
    """What the run may spend before its loop stops itself, from the member's balance (0 = no
    enforcement: credits not authoritative, an unlimited plan, or any doubt)."""
    try:
        from origin.search_engine import credit_ledger, credits, quota  # noqa: PLC0415

        if not credit_ledger.credits_authoritative():
            return 0
        balance = credit_ledger.balance_milli(str(user_id), quota.get_effective_tier(str(user_id)))
        return credits.request_budget_usd_micro(balance, settings.CREDIT_POLICY)
    except Exception:  # noqa: BLE001 - a budget we cannot compute is no budget
        log.debug("run_skill: could not compute the credit budget", exc_info=True)
        return 0


def _disabled_tools(user_id: str, resolved: Any) -> set[str]:
    """Every tool this run does not declare: the skill's ceiling (read, and an outside author's
    extras), the owner's own plan gates, EVERY write, and web search. Only ever a union, so the run
    has at most what the owner has in Spotlight, and of that only the reads."""
    from origin.search_engine.agent.tool_tiers import (  # noqa: PLC0415 - search_engine is heavy
        INTEGRATION_TOOLS,
        disabled_tools_for_user,
    )
    from origin.search_engine.agent.tools import app_write_tools  # noqa: PLC0415
    from origin.services.skills import run as skill_run  # noqa: PLC0415

    return (
        skill_run.disabled_tools(resolved)
        | disabled_tools_for_user(user_id)
        | {t.name for t in app_write_tools()}
        | set(INTEGRATION_TOOLS["web"])
    )


@dataclass(frozen=True)
class SkillAnswer:
    """What one unattended run of a skill gave, before anything is made of it. `skipped` is the
    code of a run that did not call the model (a switch, no credits); `failed` is the one line of a
    run that did and could not give an answer (it is not charged)."""

    title: str
    answer: str = ""
    failed: str = ""
    credits_ran_out: bool = False
    skipped: str = ""
    request_id: str = ""


def run_for_answer(
    ctx: ToolContext,
    params: dict[str, Any],
    *,
    answer_rules: str = UNATTENDED,
    effort_default: str = EFFORT_DEFAULT,
) -> SkillAnswer:
    """Run the pinned skill read only, as its owner, and return what it said (MIP-2045). The one
    place a scheduled skill's model loop lives: "Run a skill" files the answer as a report, a
    dashboard made by a skill (MIP-2399) turns it into data. `answer_rules` is what the model is
    told about the run and the shape of its answer, after the skill's own text."""
    from origin.models.common.user_models import CustomUser  # noqa: PLC0415
    from origin.models.skills.skill_models import TOOL_LEVEL_READ  # noqa: PLC0415
    from origin.search_engine.agent import controller  # noqa: PLC0415 - search_engine is heavy
    from origin.search_engine.llm import spend  # noqa: PLC0415
    from origin.search_engine.llm.choice import (  # noqa: PLC0415
        LlmChoice,
        reset_llm_choice,
        set_llm_choice,
    )
    from origin.search_engine.metered import metered_request  # noqa: PLC0415
    from origin.search_engine.models import AiRequestCost  # noqa: PLC0415
    from origin.services.report_ai import effort as effort_mod  # noqa: PLC0415
    from origin.services.skills import run as skill_run  # noqa: PLC0415
    from origin.services.skills import service  # noqa: PLC0415

    if not _enabled():
        raise _fail("Skills are switched off right now, so this schedule did not run.")
    user = CustomUser.objects.filter(pk=ctx.user_id).first()
    pinned = params.get("version_no")
    try:
        resolved = skill_run.resolve(
            user,
            ctx.team_id,
            {"id": params.get("skill_id")},
            pinned_version=pinned if isinstance(pinned, int) and pinned > 0 else None,
            published_only=True,
        )
    except service.SkillNotFound:
        raise _fail(
            "That skill is no longer available to you, so this schedule did not run."
        ) from None
    except service.SkillNeedsConfirmation:
        raise _fail(
            "This skill needs your confirmation first (a new version by an outside author, or a "
            "copy of a skill that was removed as harmful), so this schedule did not run. Open the "
            "skill in Genos, read it and confirm it."
        ) from None
    except service.SkillError as exc:
        raise _fail(str(exc)[:200]) from None
    # Unattended: read only, whatever the skill declares (`effective_level` keeps an outside
    # author's skill at read too).
    resolved = replace(resolved, tool_level=TOOL_LEVEL_READ)
    title = clip(resolved.title or resolved.slug, 120)

    run_settings = effort_mod.resolve(params.get(AI_EFFORT_KEY), effort_default, ctx.user_id)
    skip = _skip_code(ctx.user_id, run_settings)
    if skip:
        log.info("run_skill_skipped user=%s reason=%s", ctx.user_id, skip)
        return SkillAnswer(title=title, skipped=skip)

    choice = LlmChoice(
        provider=run_settings.provider,
        model=run_settings.model,
        effort=run_settings.effort,
        subprocess_pins=False,
    )
    query = str(params.get("input") or "").strip() or "Run this skill now."
    events: list[dict[str, Any]] = []
    request_id = ""
    credits_ran_out = False
    failed = ""
    answer = ""
    token = set_llm_choice(choice)
    try:
        with metered_request(
            surface="schedule",
            user_id=ctx.user_id,
            team_id=ctx.team_id,
            chosen=choice,
            credit_budget_usd_micro=_credit_budget_usd_micro(ctx.user_id),
        ) as outcome:
            bound = spend.current_context()
            request_id = bound.request_id if bound is not None else ""
            # From here on a failure is a REPORT, never a raise: the job's savepoint rolls back with
            # an exception, and the spend this run already recorded (`AiSpendEvent`,
            # `AiRequestCost`) must survive it (`schedules._execute`, MIP-1210). A failed request is
            # marked so it is not billed (`billable_results`), and its one line says so.
            try:
                with spend.spend_purpose("skill_run"):
                    result = controller.run_agent(
                        query,
                        ctx,
                        events.append,
                        disabled_tools=_disabled_tools(ctx.user_id, resolved),
                        system_extra=f"{skill_run.build_block(resolved)}\n\n{answer_rules}",
                    )
            except Exception as exc:  # noqa: BLE001 - one skill's failure is that schedule's
                log.warning("run_skill_failed user=%s error=%s", ctx.user_id, type(exc).__name__)
                result, failed = None, FAILED_RUN
                outcome.mark(AiRequestCost.RESULT_APPLICATION_FAILURE)
            errors = [e for e in events if e.get("type") == "error"]
            credits_ran_out = bool(errors) and all(
                e.get("message") == controller.CREDITS_EXHAUSTED_MESSAGE for e in errors
            )
            if isinstance(result, dict) and result.get("paused"):
                # A write the run was not meant to be able to make: nobody can approve it.
                failed = FAILED_WRITE
                outcome.mark(AiRequestCost.RESULT_APPLICATION_FAILURE)
            elif errors and not credits_ran_out:
                failed = FAILED_RUN
                outcome.mark(AiRequestCost.RESULT_APPLICATION_FAILURE)
            elif credits_ran_out:
                # Not a failure: the work was done and the member keeps the partial answer.
                outcome.mark(AiRequestCost.RESULT_CREDITS_EXHAUSTED)
            answer = "".join(
                str(e.get("text") or "") for e in events if e.get("type") == "answer_delta"
            ).strip()
            if not failed and not answer and not credits_ran_out:
                failed = FAILED_EMPTY
                outcome.mark(AiRequestCost.RESULT_APPLICATION_FAILURE)
    finally:
        reset_llm_choice(token)
    return SkillAnswer(
        title=title,
        answer="" if failed else answer,
        failed=failed,
        credits_ran_out=credits_ran_out,
        request_id=request_id,
    )


def skipped_report(got: SkillAnswer) -> Report:
    """The one-line report of a run that did not call the model (`got.skipped` is its code)."""
    return {
        "title": got.title,
        "summary": _SKIPPED.get(got.skipped, _SKIPPED_OTHER),
        "sections": [],
        "ai_skipped": got.skipped,
    }


def run(ctx: ToolContext, params: dict[str, Any]) -> Report:
    got = run_for_answer(ctx, params)
    if got.skipped:
        return skipped_report(got)
    report = report_from_answer(got.title, got.answer)
    if got.failed:
        report["summary"] = got.failed
    elif got.credits_ran_out:
        report["summary"] = clip(
            "Stopped early: your AI credits ran out during the run. " + report["summary"], 280
        )
    report["ai_request_id"] = got.request_id
    return report


# -- the answer as a report ----------------------------------------------------------------------

_LINK = re.compile(r"\[([^\]\n]*)\]\(([^)\n]*)\)")  # [label](target)
_TOKEN = re.compile(r"\[(?:chat|task|note|project|todo|milestone):[^\]\n]*\]", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d{1,3}[.)])\s+")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_BOLD_LINE = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*:?\s*$")
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$")
LINE_WIDTH = 230  # the delivery keeps 240 characters of a line


_IMAGE = re.compile(r"!\[[^\]\n]*\]\([^)\n]*\)")  # ![alt](target): dropped whole


def _plain(text: str) -> str:
    """One line of plain text: an image goes whole (its address can carry data), a link keeps its
    label, a citation token goes, emphasis marks go."""
    text = _IMAGE.sub("", text)
    text = _LINK.sub(r"\1", text)
    text = _TOKEN.sub("", text)
    return " ".join(text.replace("*", "").replace("`", "").split())


def _first_link(raw: str) -> str:
    """The first link of the line that opens something IN THE APP. An outside address is never
    kept: a model's link can carry the workspace's words in its query string, and a stored report
    is something a person clicks without reading the address."""
    from origin.services.schedule_delivery import clean_link  # noqa: PLC0415

    for match in _LINK.finditer(_IMAGE.sub("", raw)):
        link = clean_link(match.group(2).strip())
        if link.startswith("/workspace/"):
            return link
    return ""


def report_from_answer(title: str, answer: str) -> Report:
    """The model's markdown answer as a `Report`: its first plain line is the summary, headings and
    bold lines open sections, and every other line is an item (a long one is split at spaces,
    never cut). Nothing here trusts the text: `schedule_delivery.clean_report` checks every field
    again, and a link is kept only when it is an app path or an https URL."""
    summary = ""
    sections: list[ReportSection] = []
    current: ReportSection | None = None
    for raw in answer.splitlines():
        if not raw.strip() or _RULE.match(raw):
            continue
        heading = _HEADING.match(raw) or _BOLD_LINE.match(raw)
        if heading and (label := _plain(heading.group(1)).rstrip(":").strip()):
            current = {"heading": clip(label, 120), "items": []}
            sections.append(current)
            continue
        bullet = bool(_BULLET.match(raw))
        body = _plain(_BULLET.sub("", raw, count=1))
        if not body:
            continue
        if not summary and not bullet and current is None:
            summary = clip(body, 280)
            continue
        if current is None:
            current = {"heading": "", "items": []}
            sections.append(current)
        link = _first_link(raw)
        for i, piece in enumerate(textwrap.wrap(body, LINE_WIDTH) or [body]):
            entry: dict[str, Any] = {"text": piece}
            if link and i == 0:
                entry["link"] = link
            current["items"].append(entry)  # type: ignore[arg-type]
    return {
        "title": title,
        "summary": summary or "The skill's answer is below.",
        "sections": sections,
    }


register_job(
    Job(
        key=JOB_KEY,
        title="Run a skill",
        description=(
            "Runs one of your skills on this schedule and files what it finds in your Inbox, or "
            "posts it in a chat only you are in. It only reads (it changes nothing), uses your AI credits like a question in Spotlight, "
            "and runs the published version you picked."
        ),
        run=run,
        params_schema={
            "properties": {
                "skill_id": {"type": "string"},
                "version_no": {"type": "integer", "minimum": 1},
                "input": {"type": "string"},
            },
            "required": ["skill_id"],
        },
        uses_ai=True,
        ai_in_private_chat=True,
        effort_default=EFFORT_DEFAULT,
        agent_note=(
            "`skill_id` is a skill's id from the skills list (a skill you own with a published "
            "version, a teammate's team skill, or one you installed). `version_no` is optional: "
            "it is pinned to the current published version when the schedule is saved. `input` is "
            "the words the skill is given (what you would type after /name)."
        ),
        available=_enabled,
        check_params=check_params,
        cost_estimated=False,
        unwrapped=True,
    )
)
