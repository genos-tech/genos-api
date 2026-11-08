"""Demo workspace v2: the visitor-facing presentation (MIP-1237).

The seeded story (a product team shipping a site redesign and doing roadmap
discovery) is rich, and `demo_content_en` / `demo_content_ja` ("v1") are also the
eval corpus: `evals/fixture.py` seeds them, ~40 titles and ~90 bot names are
pinned, and the retrieval suites are rank-sensitive. So v1 is frozen, and the
visitor's demo gets a presentation layer on top of the SAME story instead of a
second corpus:

  * the workspace reads as a real team, not a test fixture: a human team name and
    project names with no `demo-1ce530b4` slug (which also made every task id
    read `QDC-1`), and a visitor with a name instead of "Demo User 1ce530b4";
  * the calendar is today's: "Q2" in the story becomes the quarter that makes
    "now" the middle of that work, the way the due dates already are;
  * tasks are filed like a working team files them: tags and an effort estimate
    on every task (v1 left both empty, so the table's Tag and Effort columns
    were blank).

Nothing here touches v1: `version="v1"` (the default, and what the fixture uses)
never imports this module's adapters, and `test_demo_v1_golden` pins the
seeded text of both languages to a hash recorded before v2 existed.
"""

from __future__ import annotations

import copy
import re
from datetime import date
from types import SimpleNamespace
from typing import Any

DEMO_VERSIONS = ("v1", "v2")
DEFAULT_VERSION = "v1"

# What `DemoSignInView` seeds for a visitor. The eval fixture passes nothing.
VISITOR_VERSION = "v2"


def normalize_version(value: object) -> str:
    """Anything unrecognised is v1: the safe, eval-pinned content."""
    code = value.strip().lower() if isinstance(value, str) else ""
    return code if code in DEMO_VERSIONS else DEFAULT_VERSION


# A person the visitor is, rather than "Demo User <slug>". `username` is not
# unique (the bots already repeat across demo teams), and the visitor can rename
# themselves like anyone.
VISITOR_NAMES = {"en": "Sam Taylor", "ja": "佐藤 結衣"}

# The team the visitor works in. `team_name` is unique across the database and
# several demo workspaces exist at once, so a visitor gets the first name in
# the pool nobody is using; a visitor who finds the pool empty gets the first
# name with a number. (A visible "Product & Design 9" reads like a test fixture.)
TEAM_NAMES = {
    "en": [
        "Product & Design",
        "Product Studio",
        "Product Lab",
        "Launch Crew",
        "Design & Engineering",
        "Product Team",
    ],
    "ja": [
        "プロダクト＆デザイン",
        "プロダクトスタジオ",
        "プロダクトラボ",
        "ローンチチーム",
        "デザイン＆エンジニアリング",
        "プロダクトチーム",
    ],
}


def team_name_for(lang: str, taken: set[str]) -> str:
    pool = TEAM_NAMES.get(lang, TEAM_NAMES["en"])
    for name in pool:
        if name not in taken:
            return name
    n = 2
    while f"{pool[0]} {n}" in taken:
        n += 1
    return f"{pool[0]} {n}"


# Tags and effort by task `key` (language-neutral wiring, like `assignee` and
# `due_offset_days`). Tags are indexes into the blueprint's own `tags` list, so
# the same table serves both languages' tag names.
#   Website Redesign: 0 Frontend, 1 Design, 2 Performance, 3 Bug
#   Roadmap:          0 Planning, 1 Research, 2 Customer, 3 Spec
TASK_META: dict[str, dict[str, Any]] = {
    "spike_framer_motion": {"tags": (0, 2), "effort": "Low"},
    "migrate_marketing_pages": {"tags": (0, 1), "effort": "Extensive"},
    "audit_marketing_pages": {"tags": (1,), "effort": "Low"},
    "build_hero_featuregrid": {"tags": (0, 1), "effort": "High"},
    "responsive_navigation": {"tags": (0,), "effort": "Moderate"},
    "plausible_analytics": {"tags": (0,), "effort": "Low"},
    "accessibility_audit": {"tags": (1,), "effort": "Moderate"},
    "slow_homepage_load": {"tags": (2, 3), "effort": "High"},
    "synthesize_interviews": {"tags": (1, 2), "effort": "High"},
    "schedule_interviews": {"tags": (2,), "effort": "Minimal"},
    "code_recordings": {"tags": (1,), "effort": "Moderate"},
    "competitor_analysis": {"tags": (1,), "effort": "Moderate"},
    "roadmap_proposal": {"tags": (0, 3), "effort": "High"},
    "onboarding_funnel": {"tags": (0, 2), "effort": "Moderate"},
    "q3_okrs": {"tags": (0,), "effort": "Moderate"},
    "stakeholder_map": {"tags": (0, 2), "effort": "Low"},
}

EFFORT_CODES = {"Minimal": 1, "Low": 2, "Moderate": 3, "High": 4, "Extensive": 5}


def task_tags(blueprint_tags, indexes) -> list[dict]:
    """A task's `tags` JSON from the blueprint's `(name, color, text_color)`."""
    out = []
    for i in indexes:
        if 0 <= i < len(blueprint_tags):
            name, color, text_color = blueprint_tags[i]
            out.append({"tagName": name, "tagColor": color, "tagTextColor": text_color})
    return out


_QUARTER = re.compile(r"\bQ([1-4])\b")


def quarter_shift(today: date) -> int:
    """How far to move the story's quarters so its "Q2" is the one we are in.

    The story was written with "now" inside Q2 (a Q2 discovery sprint, Q3 bets).
    Today is wherever it is; Q2 maps to the current quarter and every other
    quarter keeps its distance from it (Q3 is next quarter, and so on).
    """
    current = (today.month - 1) // 3 + 1
    return current - 2


def requarter(text: str, shift: int) -> str:
    if shift % 4 == 0:
        return text
    return _QUARTER.sub(lambda m: f"Q{(int(m.group(1)) - 1 + shift) % 4 + 1}", text)


def _map_strings(value: Any, fn) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_map_strings(v, fn) for v in value]
    if isinstance(value, tuple):
        return tuple(_map_strings(v, fn) for v in value)
    if isinstance(value, dict):
        return {k: _map_strings(v, fn) for k, v in value.items()}
    return value


def adapt_content(content, today: date | None = None) -> SimpleNamespace:
    """A v2 view of a per-language content module.

    Same constants, same shapes (so every seeding function takes it as it takes
    the module); only text moves with the calendar, and each task gets its tags
    and effort. The module itself is never mutated: it is shared with v1 and with
    the eval fixture for the life of the process.
    """
    today = today or date.today()
    shift = quarter_shift(today)

    adapted: dict[str, Any] = {}
    for name in dir(content):
        if not name.isupper():
            continue
        value = copy.deepcopy(getattr(content, name))
        adapted[name] = _map_strings(value, lambda s: requarter(s, shift))

    for blueprint in adapted["PROJECT_BLUEPRINTS"]:
        tags = blueprint["tags"]
        for spec in blueprint["tasks"]:
            meta = TASK_META.get(spec.get("key"))
            if not meta:
                continue
            spec["tags_json"] = task_tags(tags, meta["tags"])
            spec["effort_level"] = meta["effort"]
    return SimpleNamespace(**adapted)
