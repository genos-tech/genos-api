"""Multi-arm eval harness: cases, arms, plan, cost cap, execution, report.

One paid session answers many questions. The questions are the ITEMS (tags such as
`t977`); each item is a flag (an ARM) in `SEARCH_ENGINE`. The harness runs

  arm0     every flag off. The baseline, run ONCE per case and reused by every arm.
  <tag>    one item's overrides (plus the overrides of the arms it `requires`),
           on ONLY the cases carrying that tag (or `requires_arm: <tag>`).
  all-on   the union of every item's overrides, on the whole selection: the
           interaction check, and the answer to "do they hold up together".

so the cost is (selection) + (sum of each item's own cases) + (selection), not
selection x arms. See RUNBOOK.md for the commands.

This module is pure logic around two seams, so it is testable without a model:

  * `CaseRunner`: `(case, arm_name, overrides) -> CaseResult` (usage.CaseResult). The
    default (`case_runner.DefaultCaseRunner`) drives the real runners; a test passes a
    fake.
  * the model client: every call the process makes goes through `llm._build_adapter`,
    which the harness wraps with a gate that enforces the cost cap before EACH call.

Nothing here makes a call by itself: `build_plan` and `project_cost` (what `--dry-run`
uses) read YAML, settings and catalog prices only.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from django.conf import settings

from origin.search_engine.agent.evals.group import usage as group_usage
from origin.search_engine.agent.evals.group.usage import CaseResult

GROUP_DIR = Path(__file__).parent
ARMS_DIR = GROUP_DIR / "arms"
EVALS_DIR = GROUP_DIR.parent

ARM0 = "arm0"
ALL_ON = "all-on"
GROUP_TAG = "eval-group"
KINDS = ("behaviour", "routing", "resume")
#: The model a routing case is sent to (as `agent_eval_routing`): one cheap call per case.
ROUTING_MODEL = "gemini-3.5-flash-lite"
FIXTURES = ("v1", "sequence")

# Keys of a case file entry that the harness owns. They are removed before the entry
# reaches `runner.run_behavior_case`, which would not know them.
GROUP_ONLY_KEYS = frozenset(
    {
        "kind",
        "lang",
        "tags",
        "fixture",
        "viewer",
        "reps",
        "requires_arm",
        "est",
        "note",
        # MIP-1288 (T1): named seeds a case needs applied before it runs (group/seeds.py).
        "seed",
        # MIP-905 (see group/content_checks.py): the member's saved level for the case, the
        # case's own model choice, and environment the case needs (named in the plan notes).
        "member_prefs",
        "llm",
        "needs_env",
        # MIP-1504: the result the case is EXPECTED to give on an arm (see GroupCase.expected).
        "expect_baseline",
    }
)
# `expect:` keys evaluated by the harness (runner.py does not know them).
GROUP_EXPECT_KEYS = frozenset(
    {
        "facts_in_order",
        "tools_in_order",
        "max_calls",
        "no_tool_results_contain",
        # MIP-905 (group/content_checks.py): what the model proposed, and the answer's length
        "pending_write",
        "answer_chars_at_most",
        "answer_chars_at_least",
    }
)

_CJK = re.compile(r"[぀-ヿ㐀-鿿]")


class HarnessError(Exception):
    """A usage error: bad case file, bad arm, unknown flag. Nothing was run."""


class CostCapReached(Exception):
    """The session cost cap would be crossed by the next model call."""


# --------------------------------------------------------------------------- #
# Cases                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class GroupCase:
    id: str
    kind: str
    lang: str
    tags: frozenset[str]
    fixture: str
    raw: dict[str, Any]
    source: str
    reps: int | None = None
    requires_arm: str | None = None
    #: `{arm name: "pass"|"fail"}` from the case's `expect_baseline` (MIP-1504). A string value
    #: is the BASELINE (arm0); a mapping names arms. Only shown in the report and the JSONL row:
    #: it never changes what runs, what passes or the regression arithmetic. A positive case
    #: fails on arm0 BY DESIGN, and a FAIL there must not read as breakage.
    expected: dict[str, str] = field(default_factory=dict)

    @property
    def base_id(self) -> str:
        return self.id.rsplit(".", 1)[0] if self.id.endswith((".en", ".ja")) else self.id

    @property
    def turns(self) -> int:
        return max(len(self.raw.get("turns") or []), 1)


EXPECTED_RESULTS = ("pass", "fail")


def _parse_expected(entry: dict[str, Any], *, source: str, cid: str) -> dict[str, str]:
    """`expect_baseline: fail` (arm0) or `expect_baseline: {arm0: fail, t977: pass}`."""
    raw = entry.get("expect_baseline")
    if raw in (None, ""):
        return {}
    pairs = raw if isinstance(raw, dict) else {ARM0: raw}
    out: dict[str, str] = {}
    for arm_name, value in pairs.items():
        word = str(value).strip().lower()
        if word not in EXPECTED_RESULTS:
            raise HarnessError(
                f"{source}: case {cid}: expect_baseline for {arm_name!r} must be one of "
                f"{EXPECTED_RESULTS}, got {value!r}"
            )
        out[str(arm_name)] = word
    return out


def _detect_lang(text: str) -> str:
    return "ja" if _CJK.search(text or "") else "en"


def _case_from_entry(entry: dict[str, Any], *, source: str, group_file: bool) -> GroupCase:
    if not isinstance(entry, dict) or not entry.get("id"):
        raise HarnessError(f"{source}: every case needs an `id` (got {entry!r:.80})")
    cid = str(entry["id"])
    kind = entry.get("kind") or "behaviour"
    if kind not in KINDS:
        raise HarnessError(f"{source}: case {cid}: kind {kind!r} is not one of {KINDS}")
    fixture = entry.get("fixture", "v1" if group_file else None)
    if fixture is True:
        fixture = "v1"
    if fixture in (None, False):
        fixture = ""
    elif fixture not in FIXTURES:
        raise HarnessError(f"{source}: case {cid}: fixture {fixture!r} is not one of {FIXTURES}")
    tags = set(entry.get("tags") or ())
    tags.add("legacy" if not group_file else GROUP_TAG)
    if group_file:
        tags.add(GROUP_TAG)
    query = entry.get("query") or (entry.get("turns") or [{}])[0].get("query") or ""
    lang = entry.get("lang") or _detect_lang(query)
    reps = entry.get("reps")
    return GroupCase(
        id=cid,
        kind=kind,
        lang=lang,
        tags=frozenset(tags),
        fixture=fixture,
        raw=dict(entry),
        source=source,
        reps=int(reps) if reps else None,
        requires_arm=entry.get("requires_arm") or None,
        expected=_parse_expected(entry, source=source, cid=cid),
    )


def _read_yaml_list(path: Path) -> list[Any]:
    with path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return []
    if not isinstance(data, list):
        raise HarnessError(f"{path}: must be a YAML list of cases, got {type(data).__name__}")
    return data


def load_cases(
    *,
    group_dir: Path = GROUP_DIR,
    evals_dir: Path = EVALS_DIR,
    include_legacy: bool = True,
) -> list[GroupCase]:
    """Every case the harness knows: group files, then (optionally) the legacy suites.

    Legacy behaviour cases have no `tags`/`lang`; they get the tag `legacy` and a language
    guessed from the query. Legacy routing intents become one case per language, id
    `<intent>.<lang>`. Duplicate ids are an error: a `--case` must mean one thing.
    """
    cases: list[GroupCase] = []
    for path in sorted(group_dir.glob("*.yaml")):
        for entry in _read_yaml_list(path):
            cases.append(_case_from_entry(entry, source=path.name, group_file=True))
    if include_legacy:
        legacy_path = evals_dir / "cases.yaml"
        if legacy_path.exists():
            for entry in _read_yaml_list(legacy_path):
                cases.append(_case_from_entry(entry, source="cases.yaml", group_file=False))
        routing_path = evals_dir / "routing_cases.yaml"
        if routing_path.exists():
            for item in _read_yaml_list(routing_path):
                for lang in ("en", "ja"):
                    if not item.get(lang):
                        continue
                    entry = {
                        "id": f"{item['id']}.{lang}",
                        "kind": "routing",
                        "lang": lang,
                        "query": item[lang],
                        "ok": list(item.get("ok") or ()),
                        "no_tool": bool(item.get("no_tool")),
                        "tags": ["legacy-routing"],
                    }
                    cases.append(
                        _case_from_entry(entry, source="routing_cases.yaml", group_file=False)
                    )
    seen: dict[str, str] = {}
    for case in cases:
        if case.id in seen:
            raise HarnessError(
                f"duplicate case id {case.id!r} in {seen[case.id]} and {case.source}"
            )
        seen[case.id] = case.source
    return cases


def select_cases(
    cases: list[GroupCase],
    *,
    tags: list[str] | None = None,
    case_ids: list[str] | None = None,
    lang: str | None = None,
    include_legacy: bool = False,
    max_cases: int | None = None,
) -> list[GroupCase]:
    """The cases a run covers.

    `--case` and `--tag` add up. With neither, the selection is every case tagged
    `eval-group` (plus the legacy suites when `include_legacy`). An id or tag that matches
    nothing is an error, never a silent empty run.
    """
    chosen: dict[str, GroupCase] = {}
    for wanted in case_ids or ():
        hits = [c for c in cases if c.id == wanted] or [c for c in cases if c.base_id == wanted]
        if not hits:
            raise HarnessError(f"no case with id {wanted!r}")
        for case in hits:
            chosen[case.id] = case
    for tag in tags or ():
        hits = [c for c in cases if tag in c.tags]
        if not hits:
            raise HarnessError(f"no case carries the tag {tag!r}")
        for case in hits:
            chosen[case.id] = case
    if not case_ids and not tags:
        for case in cases:
            legacy = case.source in ("cases.yaml", "routing_cases.yaml")
            if GROUP_TAG in case.tags or (include_legacy and legacy):
                chosen[case.id] = case
    selected = [c for c in cases if c.id in chosen]  # file order, stable
    if lang:
        selected = [c for c in selected if c.lang == lang]
    if max_cases is not None:
        selected = selected[: max(max_cases, 0)]
    return selected


# --------------------------------------------------------------------------- #
# Arms                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class Arm:
    name: str
    own: dict[str, Any] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    effective: dict[str, Any] = field(default_factory=dict)
    #: the arm names whose behaviour this arm switches on (itself and what it requires)
    satisfies: frozenset[str] = frozenset()
    source: str = ""
    #: environment the arm cannot measure without (`needs:` in its file): `{SEARCH_ENGINE key:
    #: value}`. Not applied (arm overrides do that); only checked, and named in the plan notes.
    needs: dict[str, Any] = field(default_factory=dict)

    @property
    def is_baseline(self) -> bool:
        return self.name == ARM0


def load_arms(arms_dir: Path = ARMS_DIR) -> dict[str, Arm]:
    """`group/arms/<tag>.yaml` -> `{tag: Arm}` with `requires` resolved into `effective`."""
    raw: dict[str, Arm] = {}
    for path in sorted(arms_dir.glob("*.yaml")) if arms_dir.exists() else []:
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise HarnessError(f"{path.name}: must be a mapping")
        name = str(data.get("arm") or "")
        if name != path.stem:
            raise HarnessError(
                f"{path.name}: `arm: {name!r}` must equal the file name ({path.stem!r})"
            )
        if name in (ARM0, ALL_ON):
            raise HarnessError(f"{path.name}: {name!r} is reserved")
        overrides = data.get("overrides") or {}
        if not isinstance(overrides, dict):
            raise HarnessError(f"{path.name}: `overrides` must be a mapping")
        requires = list(data.get("requires") or [])
        needs = data.get("needs") or {}
        if not isinstance(needs, dict):
            raise HarnessError(f"{path.name}: `needs` must be a mapping")
        raw[name] = Arm(
            name=name, own=dict(overrides), requires=requires, source=path.name, needs=dict(needs)
        )

    def resolve(name: str, trail: tuple[str, ...]) -> tuple[dict[str, Any], set[str]]:
        if name in trail:
            raise HarnessError(f"arms require each other in a cycle: {' -> '.join((*trail, name))}")
        arm = raw.get(name)
        if arm is None:
            raise HarnessError(f"{trail[-1]}.yaml: requires unknown arm {name!r}")
        merged: dict[str, Any] = {}
        names = {name}
        for dep in arm.requires:
            dep_over, dep_names = resolve(dep, (*trail, name))
            _merge(merged, dep_over, f"{name} requires {dep}")
            names |= dep_names
        _merge(merged, arm.own, name)
        return merged, names

    for name, arm in raw.items():
        arm.effective, names = resolve(name, ())
        arm.satisfies = frozenset(names)
    return raw


def _merge(into: dict[str, Any], more: dict[str, Any], who: str) -> None:
    for key, value in more.items():
        if key in into and into[key] != value:
            raise HarnessError(
                f"{who}: {key} is set to {into[key]!r} and to {value!r} by two arms; "
                "an arm union cannot hold both"
            )
        into[key] = value


def all_on_arm(arms: dict[str, Arm]) -> Arm:
    effective: dict[str, Any] = {}
    for arm in arms.values():
        _merge(effective, arm.effective, f"all-on ({arm.name})")
    return Arm(
        name=ALL_ON,
        effective=effective,
        satisfies=frozenset(arms),
        source="(union of every arm)",
    )


def baseline_arm() -> Arm:
    return Arm(name=ARM0, source="(nothing overridden)")


def validate_overrides(arm: Arm, current: dict[str, Any] | None = None) -> list[str]:
    """Problems with an arm's effective overrides against the live `SEARCH_ENGINE`.

    * a key that is not in SEARCH_ENGINE: a typo, or a flag the stack under test does not
      have yet. Applying it would change nothing and compare the baseline to itself.
    * a value equal to the current one: the arm would change nothing (and when the current
      value comes from the environment, arm0 is not the baseline either).
    """
    cfg = settings.SEARCH_ENGINE if current is None else current
    problems = []
    for key, value in arm.effective.items():
        if key not in cfg:
            problems.append(f"arm {arm.name} ({arm.source}): unknown SEARCH_ENGINE key {key!r}")
        elif cfg[key] == value:
            problems.append(
                f"arm {arm.name} ({arm.source}): {key} is already {value!r}, so the arm changes "
                "nothing (check the environment: is the flag already on?)"
            )
    return problems


@contextmanager
def apply_overrides(overrides: dict[str, Any]) -> Iterator[None]:
    """Apply overrides to `settings.SEARCH_ENGINE`; restore on exit, errors included.

    Refuses an unknown key BEFORE touching anything. It mutates the process-global dict,
    exactly as `agent_eval_compare` does, so arms must run one after another.
    """
    cfg = settings.SEARCH_ENGINE
    unknown = sorted(k for k in overrides if k not in cfg)
    if unknown:
        raise HarnessError(f"unknown SEARCH_ENGINE override key(s): {', '.join(unknown)}")
    saved = {k: cfg[k] for k in overrides}
    try:
        for key, value in overrides.items():
            cfg[key] = value
        yield
    finally:
        for key, value in saved.items():
            cfg[key] = value


# --------------------------------------------------------------------------- #
# Plan                                                                        #
# --------------------------------------------------------------------------- #


@dataclass
class PlanItem:
    case: GroupCase
    arm: Arm
    rep: int
    #: set when the item will not run (the reason shown as `skipped`)
    skip: str = ""


@dataclass
class Plan:
    cases: list[GroupCase]
    arms: list[Arm]
    items: list[PlanItem]
    notes: list[str] = field(default_factory=list)

    def runnable(self) -> list[PlanItem]:
        return [i for i in self.items if not i.skip]

    def for_arm(self, arm_name: str) -> list[PlanItem]:
        return [i for i in self.items if i.arm.name == arm_name]


def build_plan(
    cases: list[GroupCase],
    arms: dict[str, Arm],
    *,
    arm_names: list[str] | None = None,
    reps: int = 1,
) -> Plan:
    """What runs where. The saving rule, in one place:

    arm0     the whole selection, except cases that `requires_arm` (they cannot run
             without the flag; they are listed as skipped, at no cost)
    <tag>    only cases carrying the tag, or `requires_arm` it or an arm it requires
    all-on   the whole selection
    """
    wanted = [n for n in (arm_names or [*arms, ALL_ON]) if n not in (ARM0, "0")]
    for name in wanted:
        if name != ALL_ON and name not in arms:
            raise HarnessError(
                f"unknown arm {name!r}; known: {', '.join([*arms, ALL_ON]) or '(none)'}"
            )
    plan = Plan(cases=cases, arms=[baseline_arm()], items=[])
    chosen: list[Arm] = []
    for name in dict.fromkeys(wanted):
        arm = all_on_arm(arms) if name == ALL_ON else arms[name]
        if name == ALL_ON:
            # all-on only means something over the arms that are being compared.
            if len(arms) < 1:
                plan.notes.append("all-on dropped: there are no arm files")
                continue
        chosen.append(arm)
    plan.arms += chosen
    for arm in chosen:
        for key, value in _arm_needs(arm, arms).items():
            current = settings.SEARCH_ENGINE.get(key)
            if current != value:
                plan.notes.append(
                    f"WARNING arm {arm.name}: needs {key}={value!r} in the environment (it is "
                    f"{current!r}); without it the arm runs the same code as arm0 and measures "
                    "nothing"
                )
    for key, value in _cases_need(cases).items():
        current = settings.SEARCH_ENGINE.get(key)
        if current != value:
            plan.notes.append(
                f"WARNING cases need {key}={value!r} in the environment (it is {current!r}): "
                "they would pass or fail for the wrong reason"
            )

    for arm in plan.arms:
        for case in cases:
            applies, skip = _applies(case, arm, arms)
            if not applies and not skip:
                continue
            for rep in range(1, (case.reps or reps) + 1):
                plan.items.append(PlanItem(case=case, arm=arm, rep=rep, skip=skip))
    for arm in plan.arms:
        if arm.name not in (ARM0,) and not any(i.arm is arm for i in plan.items):
            plan.notes.append(
                f"arm {arm.name}: none of the selected cases carries its tag, nothing to run"
            )
    plan.arms = [a for a in plan.arms if a.is_baseline or any(i.arm is a for i in plan.items)]
    return plan


def _arm_needs(arm: Arm, arms: dict[str, Arm]) -> dict[str, Any]:
    """The environment an arm needs (`needs:` in its file; its required arms' too)."""
    out: dict[str, Any] = {}
    for name in arm.satisfies:
        out.update(arms[name].needs if name in arms else {})
    return out


def _cases_need(cases: list[GroupCase]) -> dict[str, Any]:
    """The environment the selected cases name (`needs_env:` in a case)."""
    out: dict[str, Any] = {}
    for case in cases:
        out.update(case.raw.get("needs_env") or {})
    return out


def _applies(case: GroupCase, arm: Arm, arms: dict[str, Arm]) -> tuple[bool, str]:
    need = case.requires_arm
    if arm.is_baseline:
        if need:
            return False, f"requires_arm {need}"
        return True, ""
    if arm.name == ALL_ON:
        if need and need not in arms:
            return False, f"requires_arm {need}: no such arm file on this stack"
        return True, ""
    if need:
        return (need in arm.satisfies), ""
    return (arm.name in case.tags), ""


# --------------------------------------------------------------------------- #
# Projection (no model call)                                                  #
# --------------------------------------------------------------------------- #

# Starting guesses per case, used by `--dry-run` until `--measure-one` replaces them with
# a measurement. Deliberately round and on the high side: a plan that projects low is the
# expensive mistake. A case may carry its own `est:`.
DEFAULT_ESTIMATE = {
    "behaviour": {"calls": 4, "input_tokens": 14000, "cached_tokens": 8000, "output_tokens": 900},
    "routing": {"calls": 1, "input_tokens": 7000, "cached_tokens": 5000, "output_tokens": 80},
    "resume": {"calls": 3, "input_tokens": 12000, "cached_tokens": 6000, "output_tokens": 700},
}
JUDGE_ESTIMATE = {"calls": 1, "input_tokens": 4000, "cached_tokens": 0, "output_tokens": 300}


def planning_model(override: str | None = None) -> str:
    """The model the projection prices: `--model`, else the provider default."""
    if override:
        return override
    cfg = settings.SEARCH_ENGINE
    provider = (cfg.get("LLM_PROVIDER") or "gemini").lower()
    key = {"gemini": "GEMINI_MODEL", "claude": "CLAUDE_MODEL", "openai": "OPENAI_MODEL"}.get(
        provider
    )
    return str(cfg.get(key) or "") if key else ""


def estimate_item(case: GroupCase, model: str, *, judge: bool = False) -> dict[str, Any]:
    base = dict(DEFAULT_ESTIMATE[case.kind])
    base.update({k: int(v) for k, v in (case.raw.get("est") or {}).items() if k in base})
    if "est" not in case.raw:
        for key in base:
            base[key] *= case.turns
    est = dict(base)
    if judge and case.kind == "behaviour":
        for key, value in JUDGE_ESTIMATE.items():
            est[key] += value
    cost = group_usage.estimate_cost_usd(
        ROUTING_MODEL if case.kind == "routing" else model,
        input_tokens=est["input_tokens"],
        output_tokens=est["output_tokens"],
        cached_tokens=est["cached_tokens"],
    )
    est["cost_usd"] = cost
    return est


def project_cost(
    plan: Plan, model: str, *, judge: bool = False, scale: float = 1.0
) -> dict[str, Any]:
    """Calls and dollars the plan is expected to cost. `scale` is a measured/estimated ratio."""
    per_arm: dict[str, dict[str, float]] = defaultdict(
        lambda: {"items": 0, "calls": 0, "cost_usd": 0.0}
    )
    unpriced = False
    for item in plan.runnable():
        est = estimate_item(item.case, model, judge=judge)
        row = per_arm[item.arm.name]
        row["items"] += 1
        row["calls"] += est["calls"]
        if est["cost_usd"] is None:
            unpriced = True
        else:
            row["cost_usd"] += est["cost_usd"] * scale
    return {
        "model": model,
        "unpriced": unpriced,
        "per_arm": {name: dict(row) for name, row in per_arm.items()},
        "items": sum(r["items"] for r in per_arm.values()),
        "calls": sum(r["calls"] for r in per_arm.values()),
        "cost_usd": sum(r["cost_usd"] for r in per_arm.values()),
    }


# --------------------------------------------------------------------------- #
# Cost cap                                                                    #
# --------------------------------------------------------------------------- #


class SessionSpend:
    """Everything the session has spent, and the rule that stops it.

    `spent` is the in-process sum of EVERY priced record that reached the spend seam
    while the session was open (cases, judge, retries, records no case owns), which does
    not depend on AI_COST_METER. The ledger total, when the meter is on, is a second
    opinion: the larger of the two counts. The check runs before every model call (see
    `GuardedAdapter`) and before every case is dispatched:

        spent + reserved + average cost of one call (or case)  >  cap   ->  stop

    OVERSHOOT BOUND. A call already in flight is never cut, so the session can end above
    the cap by at most the cost of the calls in flight when it tripped: with
    `--concurrency N` that is N running calls, about N x (cost of one call), a few cents
    on the flash tier. The per-case reservation (the estimate of each dispatched case) is
    what stops N parallel cases from each passing the check on the same headroom.
    """

    def __init__(self, cap_usd: float | None, *, since: Any = None, estimate_case: float = 0.0):
        self.cap_usd = cap_usd
        self.since = since
        self._lock = threading.Lock()
        self._spent = 0.0
        self._calls = 0
        self._cases_done = 0
        self._cases_cost = 0.0
        self._reserved = 0.0
        self.estimate_case = estimate_case
        self.tripped = ""

    # -- feeding ------------------------------------------------------------ #
    def on_record(self, rec: Any, _attributed: bool) -> None:
        scratch = group_usage.UsageCapture()
        scratch.add(rec)
        with self._lock:
            self._spent += scratch.cost_usd
            self._calls += scratch.calls

    def case_done(self, cost_usd: float) -> None:
        with self._lock:
            self._cases_done += 1
            self._cases_cost += cost_usd

    # -- reading ------------------------------------------------------------ #
    @property
    def spent(self) -> float:
        with self._lock:
            local = self._spent
        ledger = group_usage.ledger_total_usd(self.since) if self.since is not None else 0.0
        return max(local, ledger)

    @property
    def avg_call(self) -> float:
        with self._lock:
            return self._spent / self._calls if self._calls else 0.0

    @property
    def avg_case(self) -> float:
        with self._lock:
            if self._cases_done:
                return self._cases_cost / self._cases_done
        return self.estimate_case

    # -- the rule ----------------------------------------------------------- #
    def check_call(self) -> None:
        if self.cap_usd is None:
            return
        spent = self.spent
        if spent + self.avg_call > self.cap_usd:
            self._trip(
                f"{usd(spent)} spent + {usd(self.avg_call)} average call > cap {usd(self.cap_usd)}"
            )

    def reserve_case(self) -> float:
        """Check the cap for one more case and reserve its estimated cost. Returns the amount."""
        est = self.avg_case
        if self.cap_usd is not None:
            spent = self.spent
            with self._lock:
                reserved = self._reserved
            if spent + reserved + est > self.cap_usd:
                self._trip(
                    f"{usd(spent)} spent + {usd(reserved)} reserved + {usd(est)} for the next case "
                    f"> cap {usd(self.cap_usd)}"
                )
        with self._lock:
            self._reserved += est
        return est

    def release_case(self, amount: float) -> None:
        with self._lock:
            self._reserved = max(self._reserved - amount, 0.0)

    def _trip(self, why: str) -> None:
        self.tripped = why
        raise CostCapReached(why)


class GuardedAdapter:
    """Wraps a provider adapter so the cap is checked before EVERY `generate_step`."""

    def __init__(self, inner: Any, guard: SessionSpend):
        self._inner = inner
        self._guard = guard

    def generate_step(self, *args: Any, **kwargs: Any) -> Any:
        self._guard.check_call()
        return self._inner.generate_step(*args, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


@contextmanager
def guarded_llm(guard: SessionSpend) -> Iterator[None]:
    """Route every adapter the process builds through the cost-cap gate.

    `get_model_client()` resolves `_build_adapter` as a module global at call time, so one
    patch covers the controller, the rewriter, the reranker, the judge and the routing
    runner, none of which can be reached by patching the name they imported.
    """
    from origin.search_engine import llm  # noqa: PLC0415

    original = llm._build_adapter

    def build(provider: str) -> Any:
        return GuardedAdapter(original(provider), guard)

    llm._build_adapter = build
    try:
        yield
    finally:
        llm._build_adapter = original


# --------------------------------------------------------------------------- #
# The run environment (MIP-1504)                                              #
# --------------------------------------------------------------------------- #

#: The host name the DRF/Django test client sends. Production settings do not list it, so a
#: `kind: resume` case (which drives the real views through `APIClient`) died with
#: `DisallowedHost: testserver` outside the test runner, which adds it by itself.
TEST_HOST = "testserver"


@contextmanager
def allow_test_host() -> Iterator[None]:
    """Make `testserver` an allowed host for the block; the list is restored afterwards."""
    before = list(settings.ALLOWED_HOSTS)
    if TEST_HOST not in before:
        settings.ALLOWED_HOSTS = [*before, TEST_HOST]
    try:
        yield
    finally:
        settings.ALLOWED_HOSTS = before


def plan_needs_search(plan: Plan) -> bool:
    """Whether any run of the plan reads the OpenSearch index (behaviour and resume cases run the
    agent, whose search tools do; routing cases make one model call and touch nothing)."""
    return any(i.case.kind != "routing" for i in plan.runnable())


def opensearch_preflight(client: Any = None, *, alias: str | None = None) -> list[str]:
    """Problems with the OpenSearch alias the run would search; `[]` = fine.

    The dev compose alias `knowledge_chunks_current` can be a PLAIN auto-created index with no
    vector mapping: every vector search then 400s, the agent finds nothing, and a paid run
    measures an agent with no retrieval (the first paid session lost about $0.2 to it). So a real
    run needs an ALIAS whose index maps `embedding` as `knn_vector`: set `OPENSEARCH_INDEX` and
    `OPENSEARCH_ALIAS` to names of the run's own, then `opensearch_setup` and a reindex.

    `client` is injectable (a test passes a fake); by default the production client is used.
    """
    cfg = settings.SEARCH_ENGINE
    alias = alias or str(cfg.get("OPENSEARCH_ALIAS") or "")
    fix = (
        "set OPENSEARCH_INDEX and OPENSEARCH_ALIAS to names of the run's own, then run "
        "`python manage.py opensearch_setup` and reindex"
    )
    try:
        if client is None:
            from origin.search_engine.opensearch_client import get_client  # noqa: PLC0415

            client = get_client()
        is_alias = bool(client.indices.exists_alias(name=alias))
        exists = is_alias or bool(client.indices.exists(index=alias))
        mapping = client.indices.get_mapping(index=alias) if exists else {}
    except Exception as exc:  # noqa: BLE001 - any failure here means the run cannot search
        return [f"cannot read OpenSearch for {alias!r} ({type(exc).__name__}: {str(exc)[:200]})"]
    problems: list[str] = []
    if not exists:
        return [f"OpenSearch has no alias or index named {alias!r}: {fix}"]
    if not is_alias:
        problems.append(
            f"{alias!r} is a plain index, not an alias (the dev compose default is created by the "
            f"first write, with no vector mapping): {fix}"
        )
    for index, body in sorted((mapping or {}).items()):
        props = ((body or {}).get("mappings") or {}).get("properties") or {}
        kind = (props.get("embedding") or {}).get("type")
        if kind != "knn_vector":
            problems.append(
                f"index {index!r} (behind {alias!r}) maps `embedding` as {kind!r}, not "
                f"'knn_vector': vector search returns nothing. {fix}"
            )
    if not mapping:
        problems.append(f"{alias!r} resolved to no index mapping: {fix}")
    return problems


# --------------------------------------------------------------------------- #
# Group-level checks on an answer (the part runner.py cannot express)         #
# --------------------------------------------------------------------------- #


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower())


def group_checks(
    expect: dict[str, Any],
    *,
    answer: str,
    tools: list[str],
    tool_text: str,
    calls: int,
    pending: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Checks for the group-only `expect:` keys. Returns `[{"name","ok","detail"}]`.

    facts_in_order        [fact, ...]; a fact is a phrase or a list of alternatives (any one
                          counts). Every fact must appear, and the FIRST appearances must be
                          in this order. Match on distinctive phrases, not on tokens.
    tools_in_order        tool names that must be called, in this order (other calls may sit
                          between them)
    max_calls             model calls <= N
    no_tool_results_contain  strings that must NOT appear in any tool result (a viewer who may
                          not see something must not even be handed it)
    pending_write         (MIP-905) checks on the write the model PROPOSED, from the
                          `tool_call_pending_approval` events: tool, task counts, body lengths
                          in characters, headings, not cut off. See `content_checks`.
    answer_chars_at_most / answer_chars_at_least   the answer's length in characters
    """
    checks: list[dict[str, Any]] = []
    if (
        "pending_write" in expect
        or "answer_chars_at_most" in expect
        or "answer_chars_at_least" in expect
    ):
        from origin.search_engine.agent.evals.group import content_checks  # noqa: PLC0415

        if "pending_write" in expect:
            checks += content_checks.check_pending(
                expect["pending_write"] or {}, pending or [], answer=answer
            )
        checks += content_checks.check_answer_length(expect, answer)
    if "facts_in_order" in expect:
        text = _norm(answer)
        last = -1
        missing: list[str] = []
        misordered: list[str] = []
        for fact in expect["facts_in_order"]:
            alts = [fact] if isinstance(fact, str) else list(fact)
            hits = [text.find(_norm(a)) for a in alts if _norm(a) in text]
            if not hits:
                missing.append(alts[0])
                continue
            pos = min(hits)
            if pos < last:
                misordered.append(alts[0])
            last = max(last, pos)
        checks.append(
            {
                "name": "facts_in_order",
                "ok": not missing and not misordered,
                "detail": "; ".join(
                    ([f"missing: {missing}"] if missing else [])
                    + ([f"out of order: {misordered}"] if misordered else [])
                )
                or "all facts present, in order",
            }
        )
    if "tools_in_order" in expect:
        want = list(expect["tools_in_order"])
        it = iter(tools)
        ok = all(any(name == got for got in it) for name in want)
        checks.append({"name": "tools_in_order", "ok": ok, "detail": f"want {want}, got {tools}"})
    if "max_calls" in expect:
        limit = int(expect["max_calls"])
        checks.append(
            {
                "name": "max_calls",
                "ok": calls <= limit,
                "detail": f"{calls} model calls, limit {limit}",
            }
        )
    if "no_tool_results_contain" in expect:
        haystack = _norm(tool_text)
        leaked = [s for s in expect["no_tool_results_contain"] if _norm(s) in haystack]
        checks.append(
            {
                "name": "no_tool_results_contain",
                "ok": not leaked,
                "detail": f"leaked into a tool result: {leaked}" if leaked else "none leaked",
            }
        )
    return checks


# --------------------------------------------------------------------------- #
# Execution                                                                   #
# --------------------------------------------------------------------------- #

CaseRunner = Callable[[GroupCase, str, dict[str, Any]], CaseResult]


@dataclass
class RunOptions:
    concurrency: int = 1
    cap_usd: float | None = None
    stop_file: str = ""
    emit: Callable[[CaseResult], None] | None = None
    progress: Callable[[str], None] | None = None
    #: wrap each worker: `(callable) -> callable` (the command binds the LlmChoice here;
    #: contextvars do not cross into pool threads)
    bind: Callable[[Callable[[], Any]], Any] | None = None


@dataclass
class RunReport:
    results: list[CaseResult]
    aborted: str = ""
    spent_usd: float = 0.0
    unattributed_usd: float = 0.0
    unpriced_calls: int = 0


def run_plan(plan: Plan, runner: CaseRunner, guard: SessionSpend, options: RunOptions) -> RunReport:
    """Run every item, arm by arm. Overrides are applied per arm and always restored."""
    report = RunReport(results=[])
    emit = options.emit or (lambda _r: None)
    say = options.progress or (lambda _m: None)

    def record(result: CaseResult) -> None:
        report.results.append(result)
        emit(result)

    for arm in plan.arms:
        items = plan.for_arm(arm.name)
        for item in items:
            if item.skip:
                record(_skipped(item, item.skip))
        todo = [i for i in items if not i.skip]
        if not todo:
            continue
        if report.aborted:
            for item in todo:
                record(_skipped(item, f"not run: {report.aborted}"))
            continue
        say(f"--- arm {arm.name}: {len(todo)} runs, overrides {arm.effective or '(none)'}")
        try:
            with apply_overrides(arm.effective):
                _run_arm(arm, todo, runner, guard, options, report, record, say)
        except KeyboardInterrupt:
            # Ctrl-C ends the session cleanly: what ran is reported, the rest is marked not run.
            report.aborted = "interrupted (Ctrl-C)"
            done = {(r["case_id"], r["arm"], r.get("rep")) for r in report.results}
            for item in todo:
                if (item.case.id, arm.name, item.rep) not in done:
                    record(_skipped(item, f"not run: {report.aborted}"))
    report.spent_usd = guard.spent
    report.unattributed_usd = group_usage.UNATTRIBUTED.cost_usd
    return report


def _skipped(item: PlanItem, reason: str) -> CaseResult:
    result = group_usage.new_result(item.case.id, item.arm.name, item.case.lang, rep=item.rep)
    result["skipped"] = reason
    return result


def _run_arm(arm, todo, runner, guard, options, report, record, say) -> None:
    def work(item: PlanItem) -> CaseResult | None:
        if report.aborted or (options.stop_file and Path(options.stop_file).exists()):
            report.aborted = report.aborted or f"stop file {options.stop_file} exists"
            return _skipped(item, f"not run: {report.aborted}")
        try:
            reserved = guard.reserve_case()
        except CostCapReached as exc:
            report.aborted = f"cost cap: {exc}"
            return _skipped(item, f"not run: {report.aborted}")
        started = time.monotonic()
        try:
            with group_usage.capture_usage() as cap:
                try:
                    result = runner(item.case, arm.name, dict(arm.effective))
                except CostCapReached as exc:
                    report.aborted = f"cost cap: {exc}"
                    result = group_usage.new_result(item.case.id, arm.name, item.case.lang)
                    result["skipped"] = f"cost cap reached mid-case: {exc}"
                except Exception as exc:  # noqa: BLE001 — one crashed case must not end the session
                    result = group_usage.new_result(item.case.id, arm.name, item.case.lang)
                    result["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            measured = cap.as_dict()
            if measured["calls"] or not result.get("usage", {}).get("calls"):
                result["usage"] = measured
            result["unpriced_calls"] = cap.unpriced_calls
            guard.case_done(float(result["usage"]["cost_usd"]))
        finally:
            guard.release_case(reserved)
        result["arm"] = arm.name
        if arm.name in item.case.expected:
            result["expected"] = item.case.expected[arm.name]
        result["case_id"] = item.case.id
        result["lang"] = item.case.lang
        result["rep"] = item.rep
        result.setdefault("duration_ms", int((time.monotonic() - started) * 1000))
        if guard.tripped and not report.aborted:
            report.aborted = f"cost cap: {guard.tripped}"
        return result

    def wrapped(item: PlanItem) -> CaseResult | None:
        return options.bind(lambda: work(item)) if options.bind else work(item)

    if options.concurrency <= 1:
        for item in todo:
            result = wrapped(item)
            if result is not None:
                record(result)
                say(_progress_line(result))
        return
    with ThreadPoolExecutor(max_workers=options.concurrency) as pool:
        try:
            for result in pool.map(wrapped, todo):
                if result is not None:
                    record(result)
                    say(_progress_line(result))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            raise


def _progress_line(result: CaseResult) -> str:
    verdict = (
        "SKIP"
        if result.get("skipped")
        else "ERR "
        if result.get("error")
        else "PASS"
        if result["passed"]
        else "FAIL"
    )
    u = result["usage"]
    return f"  {verdict}  {result['arm']:<10} {result['case_id']:<44} calls={u['calls']} ${u['cost_usd']:.4f}"


# --------------------------------------------------------------------------- #
# Report                                                                      #
# --------------------------------------------------------------------------- #


def _verdict(rows: list[CaseResult]) -> tuple[str, float | None]:
    """`("PASS"|"FAIL"|"2/3"|"skip"|"ERR"|"", pass_rate)` over one case's reps on one arm."""
    if not rows:
        return "", None
    ran = [r for r in rows if not r.get("skipped")]
    if not ran:
        return "skip", None
    passed = sum(1 for r in ran if r["passed"])
    if all(r.get("error") for r in ran):
        return "ERR", 0.0
    rate = passed / len(ran)
    if len(ran) == 1:
        return ("PASS" if passed else "FAIL"), rate
    return f"{passed}/{len(ran)}", rate


def _cell(rows: list[CaseResult], expected: str | None) -> str:
    """The matrix cell: the verdict, plus what the case expected on this arm when it said so.

    `FAIL (expected)` is a baseline that fails by design; `PASS (expected FAIL)` is the
    interesting one. A crashed run (`ERR`) is never called expected."""
    verdict, rate = _verdict(rows)
    if not expected or rate is None or verdict == "ERR":
        return verdict
    if rate == (1.0 if expected == "pass" else 0.0):
        return f"{verdict} (expected)"
    return f"{verdict} (expected {expected.upper()})"


def _by_case_arm(results: list[CaseResult]) -> dict[tuple[str, str], list[CaseResult]]:
    grouped: dict[tuple[str, str], list[CaseResult]] = defaultdict(list)
    for r in results:
        grouped[(r["case_id"], r["arm"])].append(r)
    return grouped


def _sum_usage(rows: list[CaseResult]) -> dict[str, float]:
    total = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cost_usd": 0.0, "calls": 0}
    for r in rows:
        for key in total:
            total[key] += r["usage"][key]
    return total


def summarize(results: list[CaseResult], arms: list[Arm]) -> dict[str, Any]:
    grouped = _by_case_arm(results)
    base_rows = {cid: rows for (cid, arm), rows in grouped.items() if arm == ARM0}
    out: dict[str, Any] = {"arms": {}, "regressions": {}, "improvements": {}}
    for arm in arms:
        rows = [r for r in results if r["arm"] == arm.name]
        ran = [r for r in rows if not r.get("skipped")]
        totals = _sum_usage(ran)
        entry: dict[str, Any] = {
            "runs": len(ran),
            "passed": sum(1 for r in ran if r["passed"]),
            "failed": sum(1 for r in ran if not r["passed"]),
            "skipped": len(rows) - len(ran),
            "unpriced_calls": sum(r.get("unpriced_calls", 0) for r in ran),
            **totals,
        }
        if not arm.is_baseline:
            both = [
                cid
                for (cid, a), rs in grouped.items()
                if a == arm.name
                and any(not r.get("skipped") for r in rs)
                and any(not r.get("skipped") for r in base_rows.get(cid, []))
            ]
            base = [r for cid in both for r in base_rows[cid] if not r.get("skipped")]
            mine = [r for cid in both for r in grouped[(cid, arm.name)] if not r.get("skipped")]
            b, m = _sum_usage(base), _sum_usage(mine)
            entry["vs_arm0"] = {
                "cases": len(both),
                "pass_delta": sum(1 for r in mine if r["passed"])
                - sum(1 for r in base if r["passed"]),
                "calls_delta": m["calls"] - b["calls"],
                "tokens_delta": (m["input_tokens"] + m["output_tokens"])
                - (b["input_tokens"] + b["output_tokens"]),
                "cost_delta_usd": round(m["cost_usd"] - b["cost_usd"], 6),
                "cost_delta_pct": round((m["cost_usd"] - b["cost_usd"]) / b["cost_usd"] * 100, 1)
                if b["cost_usd"]
                else None,
            }
            regress, improve = [], []
            for cid in both:
                _, rate_a = _verdict(base_rows[cid])
                _, rate_b = _verdict(grouped[(cid, arm.name)])
                if rate_a is None or rate_b is None:
                    continue
                if rate_b < rate_a:
                    regress.append(cid)
                elif rate_b > rate_a:
                    improve.append(cid)
            out["regressions"][arm.name] = regress
            out["improvements"][arm.name] = improve
        out["arms"][arm.name] = entry
    out["bisect"] = bisect_hints(results, arms)
    return out


def bisect_hints(results: list[CaseResult], arms: list[Arm]) -> list[dict[str, Any]]:
    """When all-on regresses a case arm0 passes, which arm is to blame?

    Candidates are the item arms that ran the same case and also do worse than arm0. None
    means no single item arm reproduces it (it never ran there, or it is an interaction).
    """
    grouped = _by_case_arm(results)
    hints = []
    item_arms = [a.name for a in arms if a.name not in (ARM0, ALL_ON)]
    for (cid, arm), rows in sorted(grouped.items()):
        if arm != ALL_ON:
            continue
        _, rate_on = _verdict(rows)
        _, rate_base = _verdict(grouped.get((cid, ARM0), []))
        if rate_on is None or rate_base is None or rate_on >= rate_base:
            continue
        culprits, tested, untested = [], [], []
        for name in item_arms:
            arm_rows = grouped.get((cid, name))
            rate = _verdict(arm_rows)[1] if arm_rows else None
            if rate is None:
                untested.append(name)
                continue
            tested.append(name)
            if rate < rate_base:
                culprits.append(name)
        bisect = f"`--case {cid} --arms {','.join(item_arms)}`"
        if culprits:
            action = f"regressed by arm(s): {', '.join(culprits)}"
        elif tested:
            action = (
                f"{', '.join(tested)} alone pass it; never ran it: {', '.join(untested) or 'none'}. "
                f"Either an untested arm or an interaction: bisect with {bisect}, dropping one arm at a time"
            )
        else:
            action = (
                f"no item arm ran this case (item arms only run cases carrying their own tag); "
                f"bisect with {bisect}, dropping one arm at a time"
            )
        hints.append(
            {
                "case_id": cid,
                "arm0_rate": rate_base,
                "all_on_rate": rate_on,
                "culprits": culprits,
                "hint": action,
            }
        )
    return hints


def _fmt_tools(tools: list[str], limit: int = 6) -> str:
    if not tools:
        return "-"
    shown = tools[:limit]
    return ">".join(shown) + (f">(+{len(tools) - limit})" if len(tools) > limit else "")


def _money(value: float) -> str:
    return f"${value:.4f}"


def usd(value: float) -> str:
    """Dollars for a message: cents above $1, four places below (caps can be tiny)."""
    return f"${value:.2f}" if abs(value) >= 1 else f"${value:.4f}"


def _proposed_note(ran: list[CaseResult]) -> str:
    """For a run that failed or said nothing: what the model proposed (MIP-1504), so the row
    explains itself. Passing rows with an answer stay as they were."""
    for r in ran:
        proposed = r.get("proposed")
        if proposed and (not r["passed"] or not r.get("answer")):
            return "; proposed " + " | ".join(
                f"{p['tool']}({p['args']})".replace("|", "/").replace("\n", " ")
                for p in proposed[:2]
            )
    return ""


def _expectation_lines(
    grouped: dict[tuple[str, str], list[CaseResult]],
    expected_of: dict[str, dict[str, str]],
    names: list[str],
) -> list[str]:
    """One line on `expect_baseline` (MIP-1504), only when some case declares one: how many ran
    as expected, and which did not. A mismatch is the finding: a case that fails by design on
    arm0 and passes, or a control that fails."""
    total, differs = 0, []
    for cid, wanted in expected_of.items():
        for arm, expected in wanted.items():
            if arm not in names:
                continue
            verdict, rate = _verdict(grouped.get((cid, arm), []))
            if rate is None or verdict == "ERR":
                continue
            total += 1
            if rate != (1.0 if expected == "pass" else 0.0):
                differs.append(f"{cid} on {arm}: expected {expected.upper()}, got {verdict}")
    if not total:
        return []
    head = f"Expected results (`expect_baseline`): {total - len(differs)} of {total} as expected"
    if not differs:
        return [head + ".", ""]
    return [head + "; DIFFERENT:", *[f"- {d}" for d in differs], ""]


def render_markdown(
    results: list[CaseResult],
    plan: Plan,
    report: RunReport | None = None,
    *,
    model: str = "",
    cap_usd: float | None = None,
) -> str:
    """The matrix: verdicts, then per-run detail, per-arm totals, the delta vs arm0, bisect hints."""
    arms = plan.arms
    grouped = _by_case_arm(results)
    summary = summarize(results, arms)
    names = [a.name for a in arms]
    case_ids = [c.id for c in plan.cases if any((c.id, n) in grouped for n in names)]
    lines: list[str] = []
    lines.append("# Eval group report\n")
    if report and report.aborted:
        lines.append(f"**ABORTED (partial report): {report.aborted}**\n")
    spent = report.spent_usd if report else sum(r["usage"]["cost_usd"] for r in results)
    lines.append(
        f"Spent ${spent:.4f}"
        + (f" of cap {usd(cap_usd)}" if cap_usd is not None else "")
        + (f"; priced on {model}" if model else "")
    )
    if report and report.unattributed_usd:
        lines.append(
            f"Spend not attributed to any case: ${report.unattributed_usd:.4f} (counted in the total above, not in the matrix)."
        )
    unpriced = sum(a["unpriced_calls"] for a in summary["arms"].values())
    if unpriced:
        lines.append(
            f"WARNING: {unpriced} call(s) on a model with no catalog price; every cost below is a lower bound."
        )
    lines.append("")
    lines.append(
        "## Matrix (PASS/FAIL per case and arm; `n/m` = passes over repetitions; blank = not run there)\n"
    )
    lines.append("| case | lang | " + " | ".join(names) + " |")
    lines.append("|---|---|" + "|".join("---" for _ in names) + "|")
    lang_of = {c.id: c.lang for c in plan.cases}
    expected_of = {c.id: c.expected for c in plan.cases}
    for cid in case_ids:
        cells = [_cell(grouped.get((cid, n), []), expected_of.get(cid, {}).get(n)) for n in names]
        lines.append(f"| {cid} | {lang_of.get(cid, '')} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.extend(_expectation_lines(grouped, expected_of, names))
    lines.append("## Detail (per case and arm, summed over repetitions)\n")
    lines.append(
        "| case | arm | verdict | tools in order (first rep) | calls | in / out tokens (cached) | cost | note |"
    )
    lines.append("|---|---|---|---|---|---|---|---|")
    for cid in case_ids:
        for n in names:
            rows = grouped.get((cid, n))
            if not rows:
                continue
            verdict = _verdict(rows)[0]
            ran = [r for r in rows if not r.get("skipped")]
            u = _sum_usage(ran)
            note = (
                rows[0].get("skipped")
                or rows[0].get("error")
                or "; ".join(
                    f"{c['name']}: {c['detail']}" for r in ran for c in r["checks"] if not c["ok"]
                )[:160]
            )
            note += _proposed_note(ran)
            lines.append(
                f"| {cid} | {n} | {verdict} | {_fmt_tools((ran[0]['tools'] if ran else []))} | {int(u['calls'])} | "
                f"{int(u['input_tokens'])} / {int(u['output_tokens'])} ({int(u['cached_tokens'])}) | {_money(u['cost_usd'])} | {note} |"
            )
    lines.append("")
    lines.append("## Per-arm totals and delta vs arm0 (over the cases both ran)\n")
    lines.append(
        "| arm | runs | pass | fail | skip | calls | in tokens | out tokens | cost | d pass | d tokens | d cost | d cost % |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for n in names:
        a = summary["arms"][n]
        d = a.get("vs_arm0")
        delta = (
            f"{d['pass_delta']:+d} | {d['tokens_delta']:+d} | {d['cost_delta_usd']:+.4f} | "
            f"{('%+.1f%%' % d['cost_delta_pct']) if d['cost_delta_pct'] is not None else 'n/a'}"
            if d
            else "- | - | - | -"
        )
        lines.append(
            f"| {n} | {a['runs']} | {a['passed']} | {a['failed']} | {a['skipped']} | {int(a['calls'])} | "
            f"{int(a['input_tokens'])} | {int(a['output_tokens'])} | {_money(a['cost_usd'])} | {delta} |"
        )
    lines.append("")
    for n in names:
        if n == ARM0:
            continue
        reg, imp = summary["regressions"].get(n, []), summary["improvements"].get(n, [])
        if reg or imp:
            lines.append(f"- {n}: regressed {reg or 'none'}; improved {imp or 'none'}")
    lines.append("")
    lines.append("## Bisect hints (all-on worse than arm0)\n")
    if summary["bisect"]:
        for h in summary["bisect"]:
            lines.append(
                f"- {h['case_id']}: arm0 {h['arm0_rate']:.0%} -> all-on {h['all_on_rate']:.0%}; {h['hint']}"
            )
    else:
        lines.append(
            "- none (all-on did not regress any case arm0 passes)"
            if ALL_ON in names
            else "- all-on was not run"
        )
    for note in plan.notes:
        lines.append(f"\nNote: {note}")
    return "\n".join(lines) + "\n"


def result_json(result: CaseResult) -> str:
    return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
