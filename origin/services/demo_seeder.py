"""Demo environment seeding and cleanup.

The `/api/v2/user/demo/` endpoint calls `create_demo_environment` to
populate a freshly-created `is_demo=True` user with a full, isolated
team (4 bot peers, projects, tasks + subtasks, sprints, milestones,
threaded chats, notes). Cleanup runs from two places: `LogoutView` for
users who explicitly sign out, and the `cleanup_demo_users` management
command for stale demos.

Cleanup cannot rely on FK CASCADE — most FKs in this codebase use
SET_NULL, and several chat/note tables reference rows via bare
UUIDField (no FK at all). `delete_demo_team_data` therefore enumerates
every team-scoped table explicitly. v1 intentionally skips
MentionFact, ReactionFact, ToDoFact, ReadStatus, ActivityFact, and
TaskActivity in the seeder; cleanup still sweeps them so demos created
via future interactive use are removed cleanly.

The seeded content is written so the Spotlight Cmd-K AI search has
real material to answer against: tasks describe specific decisions and
acceptance criteria, chats discuss actual implementation choices, and
notes contain methodology + retrospectives. Demo users feel the AI is
"genuinely good" because there is enough indexable detail for it to
quote and cite back.
"""

from __future__ import annotations

import logging
import threading
import uuid
from contextlib import contextmanager
from datetime import date, timedelta
from importlib import import_module
from typing import List

from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from origin.middleware.current_user import (
    clear_current_user,
    get_current_user,
    set_current_user,
)
from origin.models.chat.activity_models import ActivityFact
from origin.models.chat.mention_models import MentionFact
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.chat.unified_models import (
    Channel,
    ChannelDirectPair,
    ChannelKind,
    ChannelMember,
    Message,
)
from origin.models.common.notification_models import NotificationPreference
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import TIER_SET_BY_OPERATOR, CustomUser
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.milestone_models import MilestoneAssignees, MilestoneMaster
from origin.models.task.sprint_models import Sprint, SprintConfig

# Not written directly by the seeder — `task_signals` fans out a row per
# watched field change, so seeding a task produces activity rows stamped
# "now". The backdating pass realigns them with their task so the
# TaskActivityFeed doesn't contradict the task's own creation date.
from origin.models.task.task_activity_models import TaskActivity, TaskActivityActionType
from origin.models.task.task_models import TaskComments, TaskDependency, TaskMaster
from origin.search_engine.models import AgentRun, AgentSession, RagChunk
from origin.services.activity_suppression import suppress_task_activity

# BlockNote body builders live in `demo_blocks` so the per-language
# content modules can share them without importing this module (which
# pulls in the whole model layer). Re-exported below for the callers
# that have always imported `_body` / `_text_body` from here.
from origin.services.demo_blocks import (  # noqa: F401  (re-exported)
    _PARA_PROPS,
    _blank_para,
    _block_id,
    _body,
    _bullet,
    _bullet_placeholder,
    _heading,
    _para,
    _placeholder,
    _section,
    _text_body,
)
from origin.services.task_blocking import CLOSED_STATUSES

# Bot peers. `first`/`last` stay ASCII in EVERY language: the eval
# suites look these up by exact `attendee__username` match (~90 refs to
# Alice / Bob / Carol / Dan across cases.yaml), and `_create_bot_users`
# builds each bot's email from `first.lower()` — a Japanese given name
# would produce a non-ASCII email local part. Only `role` is localised,
# via `BOT_ROLES` in the per-language content modules.
BOT_PROFILES = [
    {"first": "Alice", "last": "Chen", "role": "Product Lead"},
    {"first": "Bob", "last": "Martinez", "role": "Engineer"},
    {"first": "Carol", "last": "Park", "role": "Designer"},
    {"first": "Dan", "last": "OConnor", "role": "QA"},
]

# Bot ROLES are localised; bot NAMES are not, in any language.
#
# Two hard reasons, both mechanical:
#   1. `cases.yaml` resolves mentions with `attendee__username=<literal>`
#      — an EXACT match, not a substring — and "Alice" / "Bob Martinez" /
#      "Carol" / "Dan" appear ~90x across the eval suites.
#   2. `_create_bot_users` builds each email as
#      f"demo-bot-{short}-{profile['first'].lower()}@genos.app", so a
#      Japanese given name would produce a non-ASCII email local part.
#
# Latin names in a Japanese workspace are unremarkable for a tech-company
# demo, so this costs nothing in realism. `role` has zero eval references
# and is visible in the member list, so it is worth translating.
BOT_ROLES = {
    "en": ["Product Lead", "Engineer", "Designer", "QA"],
    "ja": ["プロダクトリード", "エンジニア", "デザイナー", "QA"],
}

# The languages with a full content table. `create_demo_environment`
# falls back to "en" for anything else, which is also why the default is
# "en": `evals/fixture.py` calls it with no `lang` and the eval suites
# assert on English titles.
DEMO_LANGUAGES = ("en", "ja")


def _demo_lang(value: object) -> str:
    """Collapse a caller-supplied language to one we have content for.

    Mirrors `normalize_locale`'s contract (takes `object` because the
    value arrives off request JSON — a client can send `7` or `["ja"]`)
    but narrows to the tables that exist rather than to all seven UI
    locales. An unknown code degrades to English; it never raises, since
    failing a demo signin over a locale string would be absurd.
    """
    code = value.strip().lower() if isinstance(value, str) else ""
    return code if code in DEMO_LANGUAGES else "en"


# ---------------------------------------------------------------------------
# PM task-card header body — port of `taskMessageTemplate` in
# `genos-frontend/src/features/tasks/utils/TaskMessageTemplate.ts`.
#
# The v3 PM pane renders ONE top-level "task card" Message per task (the
# composer is disabled for PM — only task lifecycle writes there). The
# card bubble is keyed off the linked `task` FK; the body below mirrors
# the live shape byte-for-byte (no block ids, heading without
# `isToggleable`) so seeded cards are indistinguishable from real ones.
# ---------------------------------------------------------------------------

# BlockNote ships a fixed `textColor` palette; map taskMeta hex codes to
# the closest named slot, same table as the frontend template.
_TASK_STATUS_COLOR = {
    "Open": "blue",
    "WIP": "orange",
    "Pending": "purple",
    "Closed": "green",
    "Deleted": "red",
}
_TASK_PRIORITY_COLOR = {
    "Minimal": "gray",
    "Low": "green",
    "Normal": "blue",
    "High": "orange",
    "Critical": "red",
}
# Card blocks match the frontend template exactly: a level-3 heading
# WITHOUT `isToggleable` (the seeded note headings carry it; the card
# headings do not) and no per-block `id`.
_CARD_HEADING_PROPS = {
    "level": 3,
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}


def _card_label(text: str) -> dict:
    return {"text": text, "type": "text", "styles": {"bold": True}}


def _card_chip(text: str, color: str | None) -> dict:
    return {"text": text, "type": "text", "styles": {"bold": True, "textColor": color or "gray"}}


def _neutral_chip() -> dict:
    return {"text": "—", "type": "text", "styles": {"bold": True, "textColor": "gray"}}


def _card_mention(user: CustomUser, team_id) -> dict:
    """Inline `mention` node matching the frontend `mentionNode` shape so
    the card's assignee / reporter rows render as click-through mentions."""
    return {
        "type": "mention",
        "props": {
            "avatarImgPath": [""],
            "customStatus": "N/A",
            "teamId": str(team_id),
            "teamName": "N/A",
            "userEmail": user.email,
            "userId": str(user.id),
            "userName": user.username,
        },
    }


def _task_card_body(
    *,
    title: str,
    status: str,
    priority: str,
    assignee: CustomUser | None,
    reporter: CustomUser,
    due_date,
    team_id,
    is_milestone: bool = False,
    labels: dict[str, str] | None = None,
) -> list:
    """Build the PM task-card header body. Effort isn't seeded, so the
    Effort row shows the neutral em-dash chip (matching an unset value).

    `labels` supplies the field names (`CARD_LABELS` from a content
    table). It is optional, and defaults to English, because
    `starter_seeder` reaches these builders too — real customer
    workspaces are not part of the bilingual demo, and their behaviour
    must not change. The *values* beside the labels stay as they are:
    `status` and `priority` are DB enum strings, so a chip reading
    `Open` is correct in either language.
    """
    labels = labels if labels is not None else _content("en").CARD_LABELS
    title_prefix = "🚩" if is_milestone else "🧾"
    due_str = due_date.isoformat() if due_date else "—"
    assignee_node = (
        _card_mention(assignee, team_id)
        if assignee is not None
        else {
            "text": labels["unassigned"],
            "type": "text",
            "styles": {"italic": True, "textColor": "gray"},
        }
    )
    return [
        {
            "type": "heading",
            "props": _CARD_HEADING_PROPS,
            "content": [{"text": f"{title_prefix} {title}", "type": "text", "styles": {}}],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [
                _card_label(labels["status"]),
                _card_chip(status, _TASK_STATUS_COLOR.get(status)),
            ],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [
                _card_label(labels["priority"]),
                _card_chip(priority, _TASK_PRIORITY_COLOR.get(priority)),
            ],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [_card_label(labels["effort"]), _neutral_chip()],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [_card_label(labels["assignee"]), assignee_node],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [_card_label(labels["reporter"]), _card_mention(reporter, team_id)],
            "children": [],
        },
        {
            "type": "paragraph",
            "props": _PARA_PROPS,
            "content": [
                _card_label(labels["due"]),
                {"text": f"📅 {due_str}", "type": "text", "styles": {}},
            ],
            "children": [],
        },
        {"type": "paragraph", "props": _PARA_PROPS, "content": [], "children": []},
    ]


# ---------------------------------------------------------------------------
# Task body templates — port of `TASK_TEMPLATES` in
# `genos-frontend/src/features/tasks/components/contents/CreateTaskForm.tsx`.
# Empty (placeholder-text) variants the picker reads from. The demo
# seeds project-specific *filled* variants below.
# ---------------------------------------------------------------------------

TASK_TEMPLATE_DEFAULT: list = [
    *_section("🧾 Summary", _placeholder("One or two lines on what this task delivers.")),
    *_section(
        "🪜 Motivation",
        _placeholder("Why does this matter? What problem are we solving?"),
    ),
    *_section(
        "✅ Acceptance criteria",
        _bullet_placeholder("First condition that must be true when this is done."),
        _bullet_placeholder("Second condition…"),
        _bullet_placeholder("Third condition…"),
    ),
    *_section("🎯 Notes & links", _placeholder("Anything else worth pinning here.")),
]


# ---------------------------------------------------------------------------
# Per-language content resolution
# ---------------------------------------------------------------------------
#
# The blueprint tables live in `demo_content_en` / `demo_content_ja` — two
# modules of pure data, no logic — and this module holds the seeding logic
# that reads whichever one the visitor's language selects. Splitting them
# out kept `demo_seeder.py` reviewable (the content is ~2000 lines per
# language; inlining both would make the logic a rounding error in its
# own file) and, more usefully, made the English half's move verifiable:
# the prose was lifted verbatim, so "did the eval-pinned titles survive?"
# is a diff, not a re-reading of 40 strings.
#
# Both tables are structurally identical — same keys, same list lengths,
# same enum values, same task ORDER (`parent_idx` indexes into it
# positionally). Only human-readable strings differ. `_CONTENT_KEYS`
# below is that contract written down, and `assert_content_parity`
# enforces it; `test_demo_bilingual` calls it so a structural drift
# between the two tables fails a test instead of a visitor's signin.
_CONTENT_MODULES: dict = {}

# Every name a content module must define. Listed explicitly rather than
# discovered by `dir()`, so that adding a constant to one table and
# forgetting the other is caught by `assert_content_parity` (i.e. by CI)
# instead of by whichever visitor's signin first reaches the new key.
_CONTENT_KEYS = (
    "WEBSITE_MILESTONE_BODY",
    "ROADMAP_MILESTONE_BODY",
    "PROJECT_BLUEPRINTS",
    "DM_BLUEPRINTS",
    "GM_BLUEPRINT",
    "NOTE_WELCOME_BODY",
    "NOTE_SPOTLIGHT_TIPS_BODY",
    "NOTE_WEEKLY_PRIORITIES_BODY",
    "NOTE_DESIGN_SYSTEM_INVENTORY_BODY",
    "TASK_NOTE_SYNTHESIS_METHODOLOGY_BODY",
    "CHAT_NOTE_GM_KICKOFF_RECAP_BODY",
    "NOTE_TITLES",
    "TODO_CONTENT",
    "CHANNEL_TITLES",
    # Both added when the hardcoded English in this module was found:
    # visible prose that lived in the seeder, so neither table declared it
    # and `assert_content_parity` could not see it.
    "SELF_DM_MESSAGES",
    "SPRINT_NAME",
    "CARD_LABELS",
)


def _content(lang: str):
    """The content module for `lang`, imported on first use.

    Imported lazily, and cached, for two reasons: only one language is
    ever seeded per visitor (so eagerly importing both doubles the
    import cost of a module that Django loads at startup for every
    request path that touches a seeder), and the content modules import
    `demo_blocks` rather than this module — keeping the import one-way
    means neither table can ever create a cycle.
    """
    lang = _demo_lang(lang)
    module = _CONTENT_MODULES.get(lang)
    if module is None:
        module = import_module(f"origin.services.demo_content_{lang}")
        _CONTENT_MODULES[lang] = module
    return module


def _same_task_lists(same, where: str, en_tasks, ja_tasks) -> None:
    """Assert one blueprint task list matches its translation structurally.

    Split out of `assert_content_parity` so extra-milestone task lists get
    the identical treatment — they were previously unchecked, and they hold
    5 of the 10 dependency edges.
    """
    same(f"{where} task count", len(en_tasks), len(ja_tasks))
    for ti, (et, jt) in enumerate(zip(en_tasks, ja_tasks)):
        at = f"{where}.task[{ti}]"
        same(f"{at} keys", sorted(et), sorted(jt))
        # Positional wiring, DB-facing values, and the dependency key must
        # match exactly. `key` is the identifier `_DEPENDENCY_EDGES` resolves
        # against, so a mismatch silently drops an edge in ONE language —
        # which is the bug that made this check necessary.
        for field in ("key", "parent_idx", "is_milestone_child", "status", "priority", "assignee"):
            if field in et or field in jt:
                same(f"{at}.{field}", et.get(field), jt.get(field))
        same(f"{at} comments", len(et.get("comments", [])), len(jt.get("comments", [])))
        assert et["status"] in _TASK_STATUS_COLOR, f"{at}: bad status {et['status']!r}"
        assert jt["status"] in _TASK_STATUS_COLOR, f"{at}: bad JA status {jt['status']!r}"
        assert et["priority"] in _TASK_PRIORITY_COLOR, f"{at}: bad priority"
        assert jt["priority"] in _TASK_PRIORITY_COLOR, f"{at}: bad JA priority"


def assert_content_parity() -> None:
    """Fail loudly if the two content tables have drifted apart.

    Structural parity is what lets one set of seeding functions consume
    either table. The failure it guards is quiet: a JA blueprint with one
    fewer task than EN does not raise at import, it raises (or worse,
    silently mis-parents a task) deep inside `_create_blueprint_tasks`
    when `parent_idx` indexes past the end — during a visitor's signin.

    Checks, in the order they'd bite:
      * every `_CONTENT_KEYS` name is defined in both modules;
      * `PROJECT_BLUEPRINTS` has the same project count, and each
        project the same task count, tag count and comment counts —
        including each extra milestone's own task list;
      * every task's `key` matches, so `_DEPENDENCY_EDGES` resolves
        identically in both languages;
      * every task spec declares a `key`, and keys are unique per project
        (a duplicate silently shadows an edge endpoint);
      * `status` / `priority` are DB enum values, never translated;
      * chat blueprints have the same message and reply counts, and the
        same `parent_index` / `bot_index` wiring.

    Called by the bilingual test suite rather than at import time: a
    structural bug should fail CI, not make the module unimportable.
    """
    en, ja = _content("en"), _content("ja")

    for key in _CONTENT_KEYS:
        for mod in (en, ja):
            assert hasattr(mod, key), f"{mod.__name__} is missing {key}"

    def same(label, a, b):
        assert a == b, f"{label}: en={a!r} ja={b!r}"

    same("PROJECT_BLUEPRINTS length", len(en.PROJECT_BLUEPRINTS), len(ja.PROJECT_BLUEPRINTS))
    for pi, (ep, jp) in enumerate(zip(en.PROJECT_BLUEPRINTS, ja.PROJECT_BLUEPRINTS)):
        same(f"project[{pi}] keys", sorted(ep), sorted(jp))
        same(f"project[{pi}] tag count", len(ep["tags"]), len(jp["tags"]))
        same(f"project[{pi}] task count", len(ep["tasks"]), len(jp["tasks"]))
        same(
            f"project[{pi}] pm_messages",
            len(ep.get("pm_messages", [])),
            len(jp.get("pm_messages", [])),
        )
        _same_task_lists(same, f"project[{pi}]", ep["tasks"], jp["tasks"])
        # Extra milestones carry their own task lists, and the messiest of
        # them (Billing & Plans Revamp) owns 5 of the 10 dependency edges —
        # so its keys need checking just as much as the main list's.
        en_extra = ep.get("extra_milestones") or []
        ja_extra = jp.get("extra_milestones") or []
        same(f"project[{pi}] extra_milestone count", len(en_extra), len(ja_extra))
        for mi, (em, jm) in enumerate(zip(en_extra, ja_extra)):
            _same_task_lists(same, f"project[{pi}].extra[{mi}]", em["tasks"], jm["tasks"])

        # Every task must DECLARE a key, and keys must be unique within the
        # project — `_seed_task_dependencies` builds a dict, so a duplicate
        # silently shadows whichever task came first, and a missing key makes
        # any edge naming it a no-op. Both failures are invisible at seed
        # time; they show up as an absent Blocked column.
        for mod_label, blueprint in (("en", ep), ("ja", jp)):
            specs = list(blueprint["tasks"])
            for extra in blueprint.get("extra_milestones") or []:
                specs += extra["tasks"]
            keys = [s.get("key") for s in specs]
            missing = [s["title"] for s in specs if not s.get("key")]
            assert not missing, f"project[{pi}] ({mod_label}): tasks missing a key: {missing}"
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            assert not dupes, f"project[{pi}] ({mod_label}): duplicate task keys: {dupes}"

    # Every edge endpoint must name a task that exists, or the edge is a
    # silent no-op. Checked against the EN table; keys are asserted equal
    # above, so that covers both.
    all_keys = set()
    for bp in en.PROJECT_BLUEPRINTS:
        all_keys.update(t["key"] for t in bp["tasks"])
        for extra in bp.get("extra_milestones") or []:
            all_keys.update(t["key"] for t in extra["tasks"])
    unknown = sorted(
        {k for edge in _DEPENDENCY_EDGES for k in edge if k not in all_keys}
    )
    assert not unknown, f"_DEPENDENCY_EDGES names unknown task keys: {unknown}"

    same("DM_BLUEPRINTS length", len(en.DM_BLUEPRINTS), len(ja.DM_BLUEPRINTS))
    for di, (ed, jd) in enumerate(zip(en.DM_BLUEPRINTS, ja.DM_BLUEPRINTS)):
        same(f"dm[{di}].bot_index", ed["bot_index"], jd["bot_index"])
        same(f"dm[{di}] messages", len(ed["messages"]), len(jd["messages"]))
        same(f"dm[{di}].thread parent", ed["thread"]["parent_index"], jd["thread"]["parent_index"])
        same(
            f"dm[{di}].thread messages",
            len(ed["thread"]["messages"]),
            len(jd["thread"]["messages"]),
        )

    # Self-DM messages are positional: `seq` is the list index + 1, so a
    # length mismatch would shift the scratch chat's ordering between
    # languages (and `seq` is the axis four consumers order by).
    same("SELF_DM_MESSAGES length", len(en.SELF_DM_MESSAGES), len(ja.SELF_DM_MESSAGES))
    for mod in (en, ja):
        assert "{n}" in mod.SPRINT_NAME, f"{mod.__name__}.SPRINT_NAME needs an {{n}} field"

    # Card labels are looked up by key while building 25 task cards, so a
    # missing key is a `KeyError` mid-signin rather than a cosmetic gap.
    same("CARD_LABELS keys", sorted(en.CARD_LABELS), sorted(ja.CARD_LABELS))

    same("GM messages", len(en.GM_BLUEPRINT["messages"]), len(ja.GM_BLUEPRINT["messages"]))
    en_threads = [en.GM_BLUEPRINT["thread"], *en.GM_BLUEPRINT.get("extra_threads", [])]
    ja_threads = [ja.GM_BLUEPRINT["thread"], *ja.GM_BLUEPRINT.get("extra_threads", [])]
    same("GM thread count", len(en_threads), len(ja_threads))
    for ti, (et, jt) in enumerate(zip(en_threads, ja_threads)):
        same(f"GM thread[{ti}].parent_index", et["parent_index"], jt["parent_index"])
        same(f"GM thread[{ti}] messages", len(et["messages"]), len(jt["messages"]))


# ---------------------------------------------------------------------------
# Backdating — make the workspace look months old without hiding it from
# the search index.
# ---------------------------------------------------------------------------
#
# The demo's whole job is to show what Genos looks like after a team has
# used it for a while: a visitor who sees every row stamped "8 seconds
# ago" is looking at an empty product with sample text in it. So display
# timestamps get pushed into the past.
#
# The one rule that makes this safe: DISPLAY timestamps move, INDEX
# timestamps do not.
#
#   ts_sent_at / ts_created_at  → read by the UI and the agent's
#       fetch_chat_thread. Backdate freely; this is what makes the
#       history look real.
#   ts_updated_at               → read by the search pipeline. Every one
#       of the 11 chunkers filters its incremental window on it
#       (`ts_updated_at__gte=since`) and `_apply_freshness` decays
#       relevance by `exp(-age_days/90)` against it.
#
# Backdating `ts_updated_at` would break the demo twice: the reindex
# kicked off right after signin (`--since-minutes 5`) would skip the
# rows, and anything that did get indexed would be score-decayed into
# irrelevance. A visitor's first question would be answered with "I
# couldn't find anything" — the worst possible first impression.
#
# So every field below is a display field, and `ts_updated_at` is never
# named. Because `auto_now_add` / `auto_now` ignore values passed to
# `create()`, backdating has to be a post-create `.update()` sweep:
# `.update()` bypasses `pre_save` (so `auto_now` does not re-fire) and
# writes only the columns named.
#
# Note ONE deliberate omission: notes. `PersonalNoteMaster` and friends
# display `ts_updated_at` (personal_note_views.py orders and serialises
# it) — the same field the note chunker windows on. There is no
# display/index split to exploit, so backdating a note would trade
# searchability for a cosmetic "edited 3 months ago" label. Notes read as
# recently-edited instead, which is honest for a living document anyway.


def _backdate(model, pks, **fields) -> None:
    """One batched `.update()` of DISPLAY timestamps on `pks`.

    Never pass `ts_updated_at` — see the module comment above. Silently
    no-ops on an empty pk list so callers don't need to guard.
    """
    if not pks:
        return
    assert "ts_updated_at" not in fields, (
        "ts_updated_at is the search index's incremental-window and "
        "freshness-decay field — backdating it hides demo content from "
        "Spotlight. Backdate a display field instead."
    )
    model.objects.filter(pk__in=list(pks)).update(**fields)


def _backdate_each(model, pk_to_value: dict, field: str) -> None:
    """Per-row backdate: one `.update()` per distinct value.

    `_backdate` sets one timestamp on many rows; this sets a different
    timestamp on each row, which is what a conversation needs (messages
    must march forward, not share an instant). Groups by value so N rows
    with M distinct timestamps cost M queries, not N.
    """
    by_value: dict = {}
    for pk, value in pk_to_value.items():
        by_value.setdefault(value, []).append(pk)
    for value, pks in by_value.items():
        model.objects.filter(pk__in=pks).update(**{field: value})


# How far back the workspace's history reaches. 12 weeks reads as "this
# team has been at it for a quarter" while staying inside every retention
# window that could hide it (the tightest relevant one was free tier's
# 180-day chat cutoff, which the pro plan lifts entirely).
_HISTORY_DAYS = 84


def _timeline(count: int, *, oldest_days_ago: int, newest_days_ago: int, now):
    """`count` ascending datetimes spread from oldest → newest.

    Used to lay a conversation out along a plausible calendar. Strictly
    increasing, so thread replies always follow their parent and the
    chat's visible order matches its `seq` order.

    Deterministic — no `random`. The eval fixture reseeds the same
    `short` slug expecting reproducible content, and a random timeline
    would make every reseed a different dataset. The jitter below is a
    fixed function of the row index: enough to break up the "every
    message exactly 6.2 hours apart" machine-regularity, while landing
    the same way every run.
    """
    if count <= 0:
        return []
    span_days = max(oldest_days_ago - newest_days_ago, 1)
    out = []
    for i in range(count):
        # Even spread across the span, then nudged by a per-index offset.
        frac = i / max(count - 1, 1)
        days_ago = oldest_days_ago - frac * span_days
        ts = now - timedelta(days=days_ago)
        # Deterministic jitter: minutes from a cheap hash of the index.
        # Also pulls timestamps toward working hours so the history reads
        # like a team's week rather than a cron job's.
        jitter_minutes = ((i * 37) % 47) * 7
        ts = ts.replace(hour=9 + (i * 3) % 9, minute=0, second=0, microsecond=0)
        ts = ts + timedelta(minutes=jitter_minutes)
        out.append(ts)
    # Guarantee monotonicity even after the hour-of-day rewrite above
    # (which can invert two adjacent same-day rows).
    for i in range(1, len(out)):
        if out[i] <= out[i - 1]:
            out[i] = out[i - 1] + timedelta(minutes=17)
    # Never let jitter push a timestamp into the future.
    return [min(ts, now - timedelta(minutes=1)) for ts in out]


# How recently each OPEN task was last touched, cycled by task order.
#
# A single "stop 2 days short of now" rule is right per task and wrong
# across the board: it stamps every open task's newest change on the same
# day, so a visitor scanning the task list sees fourteen feeds that all end
# "yesterday". Per-task spread looked correct and the aggregate still read
# as a batch job — the tell this whole pass exists to remove.
#
# Cycling a few depths instead gives the board a front (touched this week)
# and a back (quiet for a fortnight), which is what a real team's board
# looks like. Deterministic, like every other stamp here: indexed by task
# order, not randomised, so a reseed reproduces the same dataset. Capped
# below the task's own age at the call site, so a 2-day-old task cannot be
# handed a 16-day-old change.
_RECENCY_LADDER = (1, 4, 2, 9, 3, 16, 6, 11)

# Per-channel history spans, as (oldest_days_ago, newest_days_ago).
#
# These are what turn "a pile of messages" into a chronology. The DM with
# Bob is the oldest because it holds the framer-motion decision and its
# rationale — the thing the visitor is meant to ask about and the agent is
# meant to explain from weeks-old context. The GM and PM channels trail
# forward to the present so the workspace also looks CURRENTLY active;
# a history that stops three weeks ago reads as abandoned, which is its
# own kind of bad first impression.
_DM_SPANS = {
    0: (74, 2),  # Alice (PM) — spans the whole quarter, still live
    1: (_HISTORY_DAYS, 4),  # Bob (Eng) — oldest; carries the decision
    2: (58, 1),  # Carol (Design) — joined the redesign later
}
_GM_SPAN = (78, 1)
_SELF_DM_SPAN = (21, 0)
# ⚠️ `_PM_SPAN[0]` also dates the TASKS (`_backdate_project_tasks`), so it is
# coupled to the sprint cadence: `prior_sprints` in
# `_create_project_from_blueprint` must reach at least this far back, or the
# oldest tasks predate every sprint window and get clamped into sprint 1.
# At 14-day sprints the current 5 prior sprints cover 75 days against this 70
# — only 5 days of slack, so widening this span means raising that count.
# Pinned by `test_the_sprint_cadence_reaches_the_oldest_task`.
_PM_SPAN = (70, 3)


def _backdate_channel_messages(channel, span, now) -> None:
    """Lay one channel's messages out along `span`, in `seq` order.

    Ordering by `seq` is not a stylistic choice — it is the invariant
    that keeps four separate consumers honest, because the seeder
    assigns `seq` in narrative (blueprint) order:

      * the sidebar resolves each channel's preview message by MAX(seq)
        but the client then sorts channels by that message's `tsSent`
        (channel_views.py:364-379), so a non-monotonic assignment would
        put a channel in a sidebar slot its preview text contradicts;
      * the agent's `fetch_chat_thread` and `thread_summary` read threads
        by `seq` while the REST/UI paths read by `ts_sent_at` — diverge
        and the AI narrates the conversation in a different order than
        the visitor sees;
      * `chat_chunker` builds each chunk's context window from the
        PRECEDING messages by `seq`, so divergence would caption a chunk
        with conversation that chronologically follows it;
      * read cursors advance forward-only on `seq` precisely because
        "ts could regress with clock skew or backdated edits"
        (read_cursor_views.py:180) — seq is the load-bearing axis and
        unread badges are unaffected by anything here.

    TOP-LEVEL messages take the whole span; thread replies are then hung
    just after their own parent. The naive version walked `seq` across
    every row including replies, which looked correct and was subtly
    wrong: replies take seq AFTER all top-level messages, so they
    absorbed the recent end of the span and the channel's LAST top-level
    message — the one the sidebar shows as the preview, since it filters
    `is_thread_reply=False` — ended up over a month old. Every channel
    read as abandoned in the chat list while the history itself looked
    fine. Replies clustering near their parent is also just what real
    threads do: an afternoon's back-and-forth, not one reply per week.

    This is safe precisely because every consumer above groups before it
    orders (main timeline vs per-thread entity), so a reply is only ever
    compared against other replies in the same thread — which is exactly
    the scope `_assert_seq_monotonic` enforces.
    """
    top_pks = list(
        Message.objects.filter(channel=channel, is_thread_reply=False)
        .order_by("seq")
        .values_list("pk", flat=True)
    )
    oldest, newest = span
    stamps = _timeline(len(top_pks), oldest_days_ago=oldest, newest_days_ago=newest, now=now)
    parent_ts = dict(zip(top_pks, stamps))
    _backdate_each(Message, parent_ts, "ts_sent_at")

    # Thread replies: a conversation in the hours after the parent.
    reply_ts: dict = {}
    for root_pk, root_sent in parent_ts.items():
        reply_pks = list(
            Message.objects.filter(channel=channel, thread_root_id=root_pk, is_thread_reply=True)
            .order_by("seq")
            .values_list("pk", flat=True)
        )
        # Accumulate strictly positive gaps so each reply is later than
        # the one before it by construction. Computing an absolute offset
        # per index instead (and clamping it to `now`) is what an earlier
        # version did, and the modulo term made it non-monotonic —
        # `_assert_seq_monotonic` caught the inversion.
        ts = root_sent
        for i, pk in enumerate(reply_pks):
            ts = ts + timedelta(minutes=41 + (i * 29) % 79)
            reply_ts[pk] = ts
        # If the walk overshot `now` (a thread hanging off a parent dated
        # today), compress it back into the window before the clamp can
        # flatten two rows onto the same instant.
        overshoot = reply_ts.get(reply_pks[-1]) if reply_pks else None
        if overshoot is not None and overshoot >= now:
            span = now - timedelta(minutes=1) - root_sent
            step = span / (len(reply_pks) + 1)
            for i, pk in enumerate(reply_pks, start=1):
                reply_ts[pk] = root_sent + step * i
    _backdate_each(Message, reply_ts, "ts_sent_at")


def _backdate_history(team, demo_user, bots, seeded_projects, now) -> None:
    """Push the workspace's DISPLAY timestamps into the past.

    Called at the end of `create_demo_environment`, inside the same
    transaction, once every row exists — it reads back what was created
    rather than threading offsets through a dozen creation functions, so
    the blueprint tables stay pure content with no timeline bookkeeping
    mixed in.

    Never touches `ts_updated_at` on anything (see the module comment
    above); `_backdate`'s assertion enforces that mechanically.
    """
    # ---- Chat ----------------------------------------------------------
    # DM channels carry no title, so they are located by their membership
    # rather than by name: the bot the DM is with (peer DMs), or the demo
    # user paired with themselves (the self-DM scratch chat).
    for bot_index, span in _DM_SPANS.items():
        if bot_index >= len(bots):
            continue
        channel = (
            Channel.objects.filter(team=team, kind=ChannelKind.DM, members__user=bots[bot_index])
            .distinct()
            .first()
        )
        if channel:
            _backdate_channel_messages(channel, span, now)

    self_id = str(demo_user.id)
    self_dm = Channel.objects.filter(
        team=team,
        kind=ChannelKind.DM,
        direct_pair__user_lo=self_id,
        direct_pair__user_hi=self_id,
    ).first()
    if self_dm:
        _backdate_channel_messages(self_dm, _SELF_DM_SPAN, now)

    for channel in Channel.objects.filter(team=team, kind=ChannelKind.GM):
        _backdate_channel_messages(channel, _GM_SPAN, now)

    # ---- Projects, milestones, tasks, comments -------------------------
    project_pks = [s["project"].pk for s in seeded_projects]
    # Projects were created first in the story, so they predate their
    # content — one day before the oldest PM message.
    _backdate(
        ProjectMaster,
        project_pks,
        ts_created_at=now - timedelta(days=_PM_SPAN[0] + 1),
    )

    for seeded in seeded_projects:
        project = seeded["project"]
        _backdate_project_tasks(project, now)
        pm_channel = Channel.objects.filter(project=project, kind=ChannelKind.PM).first()
        if pm_channel:
            # The PM channel's own creation predates its first card.
            _backdate(
                Channel,
                [pm_channel.pk],
                ts_created_at=now - timedelta(days=_PM_SPAN[0] + 1),
            )
            _backdate_pm_channel(pm_channel, now)


def _assign_tasks_to_sprints(project) -> None:
    """Re-home each task into the sprint whose window contains its date.

    Runs after the task backdating in the same pass, because a task's
    sprint is only decidable once it has a date. `_create_sprint` happens
    at project-build time and can only hand out the active sprint, so
    without this every task lands in the last sprint: the earlier ones
    render as empty shells, and a task created nine weeks ago is filed
    under a sprint that starts next week.

    Tasks older than the whole cadence clamp to the first sprint rather
    than going sprintless — a demo board with unassigned tasks reads as
    incomplete seeding, and "this predates our sprint process" is the
    honest reading of the oldest work anyway. With today's spans nothing
    takes that branch (the cadence out-reaches the oldest task by 5 days);
    it exists so shortening the cadence degrades instead of orphaning.

    Uses `.update()`, not `.save()`, and that is the load-bearing detail:
    `sprint_id` is in `_TRACKED_TASK_FIELDS`, so a save would emit a
    `sprint_changed` audit row per task stamped `now`, in a workspace
    whose entire premise is that its history is months old. The sprint a
    task was *born* in is the only assignment the story wants; this
    reshuffle is a seeding artifact and must leave no trace in the feed.
    """
    sprints = list(
        Sprint.objects.filter(project=project)
        .order_by("sequence_number")
        .values_list("pk", "start_date", "end_date")
    )
    if not sprints:
        return

    first_pk, cadence_start, _ = sprints[0]
    by_sprint: dict[object, list[object]] = {}
    for task_pk, created in TaskMaster.objects.filter(project=project).values_list(
        "pk", "ts_created_at"
    ):
        # `localdate`, not `.date()`. The window bounds are plain
        # DateFields built from `date.today()`, which follows TIME_ZONE
        # because Django's `Settings.__init__` does `os.environ["TZ"] =
        # TIME_ZONE; time.tzset()` — so the bounds are LOCAL dates while
        # `ts_created_at` is an aware UTC datetime whose `.date()` is its
        # UTC date. Under `TIME_ZONE="Asia/Tokyo"` those disagree for every
        # instant from 15:00 UTC on (measured), landing a task one sprint
        # off. Today's UTC setting hides it; the same off-by-one-day class
        # as the `since` midnight bug.
        created_date = timezone.localdate(created)
        target = next(
            (pk for pk, start, end in sprints if start <= created_date <= end),
            None,
        )
        if target is None:
            # Older than the cadence → first sprint. Newer than the last
            # window can't happen (tasks are backdated, never post-dated),
            # but fall to the active sprint if it ever does.
            target = first_pk if created_date < cadence_start else sprints[-1][0]
        by_sprint.setdefault(target, []).append(task_pk)

    # One UPDATE per sprint, not per task.
    for sprint_pk, task_pks in by_sprint.items():
        TaskMaster.objects.filter(pk__in=task_pks).update(sprint_id=sprint_pk)


def _backdate_project_tasks(project, now) -> None:
    """Backdate one project's tasks, milestones and comments.

    Task creation order IS story order — the blueprints open each
    milestone with its backing task, then list the work under it, and
    they already put the finished work (the closed audit, the closed
    interview scheduling) near the front. So `task_id` ascending is the
    chronology, exactly as `seq` is for chat, and no content needs a
    hand-written date.

    An earlier version sorted by status instead ("closed work is old,
    open work is recent"), which reads better in the abstract but
    contradicts the PM channel: a task's card takes the task's own
    timestamp and cards are seq-ordered by task creation, so grouping by
    status made display order disagree with `seq`. `_assert_seq_monotonic`
    caught it. Creation order satisfies both, and the blueprint ordering
    means it costs nothing in realism.
    """
    task_pks = list(
        TaskMaster.objects.filter(project=project).order_by("task_id").values_list("pk", flat=True)
    )
    if not task_pks:
        return
    stamps = _timeline(
        len(task_pks), oldest_days_ago=_PM_SPAN[0], newest_days_ago=_PM_SPAN[1], now=now
    )
    _backdate_each(TaskMaster, dict(zip(task_pks, stamps)), "ts_created_at")

    # Now that the tasks have real dates, put each one in the sprint whose
    # window actually contains it. `_create_sprint` runs before this pass,
    # so at creation time every task was handed the ACTIVE sprint — which
    # left the prior sprints as empty shells and, worse, dated a task into
    # a sprint that had not started yet (a task filed nine weeks ago sitting
    # in a sprint whose window opens next week is a self-contradiction on
    # the sprint board, and `get_sprint_summary` reports it verbatim).
    _assign_tasks_to_sprints(project)

    # Milestones frame the work, so they predate the tasks under them.
    ms_pks = list(
        MilestoneMaster.objects.filter(project=project)
        .order_by("milestone_id")
        .values_list("pk", flat=True)
    )
    if ms_pks:
        stamps = _timeline(
            len(ms_pks), oldest_days_ago=_PM_SPAN[0] + 1, newest_days_ago=50, now=now
        )
        _backdate_each(MilestoneMaster, dict(zip(ms_pks, stamps)), "ts_created_at")

    # The audit feed the frontend renders (TaskActivityFeed) — created by
    # the post_save signals during seeding, so every row says "now".
    # Not indexed by any chunker, so this is pure display.
    #
    # The FIRST row of a task is its `created` event and belongs exactly on
    # the task's creation date; the rest are the decisions replayed by
    # `_replay_task_history` and have to spread FORWARD from there, or a
    # task that was re-prioritised three times shows three changes at the
    # same instant three months ago — which reads as a seeding artifact,
    # the precise impression this pass exists to remove. Ordering by
    # `activity_id` preserves replay order, so old→new values still chain
    # (Low → Normal → High reads in that order, never shuffled).
    for index, (pk, created, status) in enumerate(
        TaskMaster.objects.filter(project=project)
        .order_by("task_id")
        .values_list("pk", "ts_created_at", "status")
    ):
        act_pks = list(
            TaskActivity.objects.filter(task_id=pk)
            .order_by("activity_id")
            .values_list("pk", flat=True)
        )
        if not act_pks:
            continue
        created_age = max((now - created).days, 0)
        # Changes land between the task's creation and now, stopping short
        # of the present so the newest change isn't stamped "just now" on
        # a workspace the visitor has never touched.
        #
        # CLOSED tasks stop earlier, for the same reason their comments do
        # (see the comment pass below): nobody re-prioritises a task that
        # shipped two months ago, so a `priority` change dated last week on
        # a task closed in July is a seeding tell. Same first-third window
        # as the comments, so a closed task's changes and its discussion
        # wrap up together instead of drifting apart.
        if (status or "") in CLOSED_STATUSES:
            newest_days_ago = max(created_age - max(created_age // 3, 1), 7)
        else:
            newest_days_ago = _RECENCY_LADDER[index % len(_RECENCY_LADDER)]
        stamps = [created] + _timeline(
            len(act_pks) - 1,
            oldest_days_ago=max(created_age - 1, 1),
            newest_days_ago=min(newest_days_ago, max(created_age - 1, 1)),
            now=now,
        )
        _backdate_each(TaskActivity, dict(zip(act_pks, stamps)), "ts_created_at")

    # Comments: spread between their task's creation and now, so a
    # comment can never predate the task it is on.
    #
    # CLOSED tasks are the exception: their discussion has to finish while
    # the work was still live. Spreading a closed task's comments toward
    # `now` produces a task that was finished two months ago and is still
    # being argued about last week — which a visitor reading the
    # framer-motion spike notices immediately, because its comments ARE
    # the decision the rest of the workspace cites as settled. There is no
    # closed_at column to anchor to, so the window ends partway through
    # the task's life instead.
    for task_pk, created, status in TaskMaster.objects.filter(project=project).values_list(
        "pk", "ts_created_at", "status"
    ):
        comment_pks = list(
            TaskComments.objects.filter(task_id=task_pk)
            .order_by("comment_id")
            .values_list("pk", flat=True)
        )
        if not comment_pks:
            continue
        age = max((now - created).days, 1)
        if (status or "") in CLOSED_STATUSES:
            # Wrap up in the first third of the task's life, and never
            # closer than a week to the present.
            newest = max(age - max(age // 3, 1), 7)
        else:
            newest = max(age // 4, 1)
        stamps = _timeline(
            len(comment_pks),
            oldest_days_ago=max(age - 1, 1),
            newest_days_ago=min(newest, max(age - 1, 1)),
            now=now,
        )
        _backdate_each(TaskComments, dict(zip(comment_pks, stamps)), "ts_sent_at")


def _backdate_pm_channel(pm_channel, now) -> None:
    """Align the PM channel's messages with the rows they mirror.

    A PM channel is not free-form chat: it is one task-card header per
    task, plus every task comment mirrored as a thread reply under its
    card. Both halves already have a true timestamp elsewhere, so this
    copies rather than invents — a card takes its task's `ts_created_at`,
    a mirrored reply takes its comment's `ts_sent_at`. Inventing a
    separate timeline here would let a card and its own task disagree
    about when the task was filed, which is exactly the kind of internal
    contradiction a curious visitor notices.

    The mirrors are addressable because their ids are deterministic:
    `task_comment_message_uuid(task_id, comment_id)` (unified_writer.py:65).

    ONE SUBTLETY, and it is the reason this function can't just copy
    blindly. `seq` allocation in a PM channel is "all N cards first, then
    every mirrored comment" — so seq order is NOT chronological here even
    before backdating, and copying source timestamps makes the divergence
    explicit (a reply on card 1 gets a lower ts than card 9, but a higher
    seq). `chat_chunker` splits this channel into one entity per thread
    plus a main timeline of non-rooting cards, and it orders WITHIN each
    of those groups, so the comparison that matters is card-vs-card and
    reply-vs-reply-in-the-same-thread — both of which stay monotonic
    here, because cards inherit task creation order and replies inherit
    `comment_id` order. The cross-group inversion is invisible to every
    consumer, and `_assert_seq_monotonic` pins exactly that scope.
    """
    from origin.services.unified_writer import task_comment_message_uuid

    # Task-card headers: same instant as the task they represent.
    card_stamps = {}
    for msg_pk, task_created in Message.objects.filter(
        channel=pm_channel, is_thread_reply=False, task__isnull=False
    ).values_list("pk", "task__ts_created_at"):
        if task_created:
            card_stamps[msg_pk] = task_created
    _backdate_each(Message, card_stamps, "ts_sent_at")

    # Mirrored comments: same instant as the comment.
    mirror_stamps = {}
    for task_id, comment_id, sent in TaskComments.objects.filter(
        task__project=pm_channel.project
    ).values_list("task_id", "comment_id", "ts_sent_at"):
        mirror_stamps[task_comment_message_uuid(task_id, comment_id)] = sent
    # Only rows that actually exist in this channel (the mirror write is
    # best-effort and returns None on failure).
    live = set(
        Message.objects.filter(channel=pm_channel, pk__in=list(mirror_stamps)).values_list(
            "pk", flat=True
        )
    )
    _backdate_each(Message, {k: v for k, v in mirror_stamps.items() if k in live}, "ts_sent_at")

    # A mirrored reply must never predate the card it hangs under: the
    # card is the task, and a comment cannot precede its own task. The
    # comment timeline is derived from the task's creation date so this
    # holds by construction — assert it rather than trusting it, since a
    # future edit to either span would break it silently.
    for root_id, root_sent in Message.objects.filter(
        channel=pm_channel, is_thread_reply=False
    ).values_list("pk", "ts_sent_at"):
        earliest = (
            Message.objects.filter(channel=pm_channel, thread_root_id=root_id)
            .order_by("ts_sent_at")
            .values_list("ts_sent_at", flat=True)
            .first()
        )
        assert earliest is None or root_sent is None or earliest >= root_sent, (
            f"PM thread reply predates its task card ({earliest} < {root_sent}) — "
            "a comment cannot be older than the task it is on."
        )


def _assert_seq_monotonic(team) -> None:
    """Fail loudly if any channel's `ts_sent_at` disagrees with its `seq`.

    `seq` is the load-bearing ordering axis for four consumers that do
    NOT read `ts_sent_at` (the sidebar's preview resolution, the agent's
    `fetch_chat_thread` / `thread_summary`, `chat_chunker`'s context
    window, and read-cursor advance). Backdating only touches
    `ts_sent_at`, so a mistake here does not raise — it quietly makes the
    AI narrate a conversation in a different order than the visitor reads
    it. That is the kind of bug that ships.

    Scope matches how the consumers actually group messages: the
    non-thread timeline, and each thread's replies. PM channels are
    exempt from the cross-group comparison for the reason documented in
    `_backdate_pm_channel`.
    """
    for channel in Channel.objects.filter(team=team):
        rows = list(
            Message.objects.filter(channel=channel)
            .order_by("seq")
            .values_list("seq", "ts_sent_at", "is_thread_reply", "thread_root_id")
        )
        groups: dict = {}
        for seq, sent, is_reply, root in rows:
            key = ("thread", root) if is_reply else ("main", None)
            groups.setdefault(key, []).append((seq, sent))
        for key, entries in groups.items():
            prev_seq, prev_sent = None, None
            for seq, sent in entries:
                if prev_sent is not None and sent is not None:
                    assert sent >= prev_sent, (
                        f"channel {channel.id} group {key}: ts_sent_at regresses "
                        f"at seq {prev_seq}→{seq} ({prev_sent} → {sent}). seq order "
                        "and display order must agree; see _backdate_channel_messages."
                    )
                prev_seq, prev_sent = seq, sent


# ---------------------------------------------------------------------------
# Seeder entry point
# ---------------------------------------------------------------------------


def create_demo_environment(
    demo_user: CustomUser, *, short: str | None = None, lang: str = "en"
) -> dict:
    """Provision a fresh demo team + bot peers + sample data for
    `demo_user`. Returns `{"team_id": str, "team_name": str}` so the
    sign-in endpoint can pre-fill the frontend localStorage and skip
    `/jointeam`.

    Wrapped in a single transaction so partial failures roll back —
    the caller's `create_user` call must also be inside the same
    `transaction.atomic()` for the user row to roll back too.

    `short`: optional override for the per-tenant slug suffix. The demo
    sign-in path leaves this None so each demo user gets a fresh random
    slug. The eval fixture passes a fixed slug so the same content
    re-seeds the same names / emails reproducibly.

    `lang`: which content table to seed ("en" or "ja"; see
    `DEMO_LANGUAGES`). The LP that feeds this endpoint ships a 日本語/EN
    toggle, so both are first-class. The default is "en" and must stay
    that way: `evals/fixture.py` calls this with no `lang` and the eval
    suites — which gate deploys — assert on English title substrings.
    Anything unrecognised degrades to English rather than raising.

    Only ONE language is seeded per visitor, so the second content table
    costs source lines, not rows or latency.
    """
    if short is None:
        short = uuid.uuid4().hex[:8]
    lang = _demo_lang(lang)

    with transaction.atomic():
        # Set on the user so the agent's locale fallback resolves even
        # when the client sends no explicit locale: `agent_views` reads
        # `normalize_locale(data.get("locale")) or
        # normalize_locale(request.user.language)`. With it, Spotlight
        # answers in the visitor's language — and `language_directive`
        # is written to hold that line even over mixed-language
        # retrieved content, so an EN query against the JA workspace
        # still answers in English rather than breaking.
        if demo_user.language != lang:
            demo_user.language = lang
            demo_user.save(update_fields=["language"])

        # 1. Bot peer users
        bots = _create_bot_users(short, lang)

        # 2. Team
        # `plan="pro"` is load-bearing for the demo in three separate
        # ways, not just a generosity setting:
        #   1. `agent_tool_level` becomes "organize", which is what
        #      DECLARES the approval-gated write tools to the model. At
        #      "read" (free) `disabled_tools_for_level` hides every
        #      write tool, so the visitor cannot even see that Genos can
        #      act — the demo's whole payoff is unreachable.
        #   2. `message_retention_days` becomes None (unlimited). On
        #      free it is 180 days, and BOTH the search filter and the
        #      REST read path drop chat older than that — which would
        #      silently swallow the backdated history below.
        #   3. `agent_memory` becomes "team", which un-hides the
        #      conversation / spotlight_answer recall lanes.
        # `resolve_effective_tier` takes the max over the user's own
        # tier and their teams' plans, so the team plan alone suffices.
        # `plan_set_by=operator` because a hand-set plan is not Stripe's
        # to take away (see the field comment in team_models.py) — and
        # this team self-destructs on logout anyway.
        team = TeamMaster.objects.create(
            team_name=f"Demo Team {short}",
            team_email=f"demo-team-{short}@genos.app",
            owner=demo_user,
            is_demo=True,
            plan="pro",
            plan_set_by=TIER_SET_BY_OPERATOR,
        )

        # 3. Team members (demo user + bots)
        all_members = [demo_user] + bots
        TeamMembers.objects.bulk_create([TeamMembers(team=team, attendee=u) for u in all_members])

        # 4. Notification preferences (one per user)
        NotificationPreference.objects.bulk_create(
            [NotificationPreference(user=u) for u in all_members]
        )

        # 5-10. Projects + sprints + milestones + tasks (per blueprint)
        content = _content(lang)
        seeded_projects = []
        for blueprint in content.PROJECT_BLUEPRINTS:
            seeded = _create_project_from_blueprint(
                team, demo_user, all_members, bots, short, blueprint, content
            )
            seeded_projects.append(seeded)

        # `_TASK_HISTORY` names tasks by blueprint key, and a key matching
        # no task in THIS project is skipped rather than raising — that skip
        # is what lets one shared list serve every project (same contract as
        # `_DEPENDENCY_EDGES`). The cost is that a mistyped key degrades
        # silently to a quieter activity feed, which no row count would
        # flag. So check the thing that actually goes wrong: a key that
        # resolved in *no* project at all.
        #
        # Checked here rather than by counting applied steps, because the
        # step count is a property of the data (a step whose value already
        # holds is a no-op, and the rewind below makes each column's first
        # step silent) — a count floor would need re-tuning on every
        # mechanism change and would say nothing about which key broke.
        seeded_keys: set = set()
        for seeded in seeded_projects:
            seeded_keys |= seeded["history_keys"]
        orphans = sorted({step[0] for step in _TASK_HISTORY} - seeded_keys)
        if orphans:
            raise RuntimeError(
                f"_TASK_HISTORY names task keys that exist in no blueprint: {orphans} "
                "— they were silently skipped, leaving those tasks with an empty "
                "activity feed. Fix the key or drop the steps."
            )

        # 11. DMs + thread messages
        _create_dms(team, demo_user, bots, content)

        # 12. Group chat + thread messages
        _create_group_chat(team, demo_user, all_members, content)

        # 13. Project (PM) channel: one task-card header per task + each
        #     task's comment thread mirrored as v3 thread replies.
        for seeded in seeded_projects:
            _create_pm_messages(
                seeded["project"],
                all_members,
                seeded["blueprint"],
                seeded["tasks"],
                seeded["backing_task"],
                seeded.get("extra_milestones"),
                card_labels=content.CARD_LABELS,
            )

        # 14. (legacy UserChatMaster seeding removed — the legacy chat
        #     tables are dropped; chat is seeded on the v3 Channel/Message
        #     schema above.)

        # 15. Notes (personal + task + chat note with permissions)
        _create_notes(team, demo_user, bots, seeded_projects, content)

        # 16. Todos (demo user only — todos are personal-scoped)
        _create_todos(team, demo_user, content)

        # 17. Push DISPLAY timestamps into the past so the workspace reads
        #     as three months of real use instead of "created 8 seconds
        #     ago". Runs last, inside the same transaction, because it
        #     reads back every row it rewrites — that keeps the blueprint
        #     tables pure content with no timeline bookkeeping in them.
        #     NEVER touches `ts_updated_at`; see the module comment above
        #     `_backdate`.
        now = timezone.now()
        _backdate_history(team, demo_user, bots, seeded_projects, now)
        # Cheap (one query per channel) and catches the one class of bug
        # backdating can introduce silently — display order disagreeing
        # with `seq`, which four consumers depend on.
        _assert_seq_monotonic(team)

    return {
        "team_id": str(team.team_id),
        "team_name": team.team_name,
    }


def _delete_demo_team_search_chunks(team_id: uuid.UUID) -> None:
    """Remove this team's chunks from OpenSearch + RagChunk tracking.

    Called from `delete_demo_team_data`. Failures are logged but
    swallowed: cleanup must keep going even when OpenSearch is
    unreachable. The next regular reindex will leave orphan chunks
    pointing at deleted entities; the RagChunk filter on team_id is
    what makes targeted cleanup possible.
    """
    team_id_str = str(team_id)

    try:
        chunk_ids = list(
            RagChunk.objects.filter(team_id=team_id).values_list("chunk_id", flat=True)
        )
        if not chunk_ids:
            return

        try:
            # Local import so import-time failures (missing client
            # config in dev) don't prevent the demo cleanup from
            # running at all — only the OpenSearch side will skip.
            from opensearchpy import helpers as os_helpers

            from origin.search_engine.opensearch_client import (
                get_client,
                get_index_alias,
            )

            actions = [
                {"_op_type": "delete", "_index": get_index_alias(), "_id": cid}
                for cid in chunk_ids
            ]
            client = get_client()
            os_helpers.bulk(client, actions, raise_on_error=False, raise_on_exception=False)
        except Exception as exc:
            logger.warning(
                "Demo OpenSearch chunk delete failed for team %s: %s",
                team_id_str,
                exc,
                exc_info=True,
            )

        # Drop the tracking rows regardless of whether the OpenSearch
        # bulk delete succeeded — they're per-team and unreachable
        # without the team's Postgres rows anyway.
        RagChunk.objects.filter(team_id=team_id).delete()
    except Exception as exc:
        logger.warning(
            "Demo RagChunk cleanup failed for team %s: %s",
            team_id_str,
            exc,
            exc_info=True,
        )


def kick_off_demo_reindex(since_minutes: int = 5) -> None:
    """Fire `manage.py opensearch_reindex --since-minutes N` on a daemon
    thread so Spotlight (Cmd-K) can search the demo user's seeded
    content right after signin — without waiting for the next 10-min
    cron tick.

    Runs in a background thread so the demo signin response returns
    immediately. Failures (OpenSearch down, no creds, etc.) are logged
    but never raised: the demo flow must work even when the search
    index is unavailable; the next cron tick will reconcile.

    Must be called AFTER the seeding transaction commits — the
    reindexer filters by ts_updated_at, so it needs the rows to be
    visible in the DB.
    """

    def _run() -> None:
        try:
            call_command("opensearch_reindex", since_minutes=since_minutes)
        except Exception as exc:
            logger.warning("Demo opensearch reindex failed: %s", exc, exc_info=True)
        finally:
            # Each spawned thread gets its own ORM connection; release
            # it so the pool doesn't accumulate idle handles.
            connection.close()

    threading.Thread(target=_run, daemon=True, name="demo-reindex").start()


def _create_bot_users(short: str, lang: str = "en") -> List[CustomUser]:
    """The four bot peers. Names stay ASCII in every language and only
    `role` is localised — see the comment on `BOT_ROLES` for the two
    mechanical reasons (exact-match eval lookups, and emails built from
    the given name)."""
    roles = BOT_ROLES.get(lang, BOT_ROLES["en"])
    bots: List[CustomUser] = []
    for idx, profile in enumerate(BOT_PROFILES):
        email = f"demo-bot-{short}-{profile['first'].lower()}@genos.app"
        bot = CustomUser(
            email=email,
            username=f"{profile['first']} {profile['last']}",
            role=roles[idx] if idx < len(roles) else profile["role"],
            base_country="USA",
            is_demo=True,
            language=lang,
        )
        bot.set_unusable_password()
        bot.save()
        bots.append(bot)
    return bots


def _resolve_user(key: str, demo_user: CustomUser, bots: List[CustomUser]) -> CustomUser:
    """Map a blueprint key ("demo", "bot0".."bot3") to a user instance."""
    if key == "demo":
        return demo_user
    if key.startswith("bot"):
        return bots[int(key[3:])]
    raise ValueError(f"Unknown user key: {key}")


def _create_project_from_blueprint(
    team, demo_user, all_members, bots, short, blueprint, content
):
    """Create one project, its tags, sprint, milestone (with backing
    task), and all tasks + subtasks + comments. Returns
    `{"project": project, "blueprint": blueprint, "tasks": [TaskMaster, ...]}`.

    `content` is the per-language content module (see `_content`) — the
    blueprint alone is not enough, because the sprint names come from the
    table rather than from any one project's blueprint."""
    from origin.services.project_code import derive_project_code

    project_name = f"{blueprint['name']} · demo-{short}"
    # Derive a 2–6 letter code so each task gets a real PRJ-123 display
    # id instead of the "#42" fallback. Scope uniqueness to the team
    # (different demo teams can both have a "WR" code).
    taken_codes = set(
        ProjectMaster.objects.filter(team=team, code__isnull=False).values_list("code", flat=True)
    )
    project = ProjectMaster.objects.create(
        team=team,
        project_name=project_name,
        code=derive_project_code(project_name, taken_codes),
        owner=demo_user,
        project_system_user=demo_user,
        is_private=False,
    )

    # Per-row `.save()` so the `_sync_pm_channel_member` post_save
    # signal fires and creates the matching `ChannelMember` rows for
    # the PM Channel (the signal is skipped by `bulk_create`). Without
    # this the demo user wouldn't see the PM channels in their v3
    # chat list, since `_user_channels_qs` filters by membership.
    for u in all_members:
        ProjectMembers.objects.create(team=team, project=project, attendee=u)

    ProjectTags.objects.bulk_create(
        [
            ProjectTags(
                team=team,
                project=project,
                tag_id=tag_idx + 1,
                tag_name=name,
                tag_color=color,
                tag_text_color=text_color,
            )
            for tag_idx, (name, color, text_color) in enumerate(blueprint["tags"])
        ]
    )

    # Five closed sprints behind the active one. The count is not a taste
    # call: at 14 days each, 5 prior sprints reach back 75 days, and the
    # oldest task is backdated to `_PM_SPAN[0]` (70) days ago. Fewer
    # sprints and the earliest tasks predate the whole cadence — measured
    # at `prior_sprints=2` (today's value), 14 of 25 tasks fell outside
    # every sprint window, including all three CLOSED ones, so the work
    # that visibly shipped had no sprint it could have shipped in.
    # `_assign_tasks_to_sprints` then re-homes each task by date.
    sprint = _create_sprint(
        team, project, prior_sprints=5, name_template=content.SPRINT_NAME
    )
    # Everything below writes `TaskActivity` rows through the post_save
    # signals, and the audit row's actor comes from the thread-local, NOT
    # from the `reporter` column each task is given. Without this every
    # `created` row renders "Someone filed this" while the replayed changes
    # right beneath it name real people — and the anonymous half reads as a
    # bug rather than as system behaviour.
    #
    # Set here, at the demo call site, rather than inside the helpers:
    # `_create_blueprint_tasks` and `_create_milestone_with_backing_task`
    # are cross-imported by `starter_seeder`, which seeds real customer
    # workspaces and must keep whatever actor its own request supplies.
    with _acting_as(demo_user):
        milestone, backing_task = _create_milestone_with_backing_task(
            team, project, sprint, demo_user, bots, blueprint["milestone"]
        )
        tasks = _create_blueprint_tasks(
            team,
            project,
            sprint,
            milestone,
            backing_task,
            demo_user,
            bots,
            blueprint["tasks"],
        )

    # Optional additional milestones (same spec shape as the main one,
    # each with its own task list). Used for the deliberately messy
    # "Billing & Plans Revamp" milestone that the agent's organize flow
    # (update_tasks_bulk) is demoed against.
    extra_milestones = []
    all_project_tasks: List[TaskMaster] = list(tasks)
    for extra in blueprint.get("extra_milestones", []):
        with _acting_as(demo_user):
            extra_ms, extra_backing = _create_milestone_with_backing_task(
                team, project, sprint, demo_user, bots, extra["milestone"]
            )
            extra_tasks = _create_blueprint_tasks(
                team,
                project,
                sprint,
                extra_ms,
                extra_backing,
                demo_user,
                bots,
                extra["tasks"],
            )
        extra_milestones.append({"backing_task": extra_backing, "tasks": extra_tasks})
        all_project_tasks += [extra_backing, *extra_tasks]

    # Seed a few realistic blocker→blocked edges so the relationship graph
    # has data: powers `get_task_blockers` / `list_task_dependencies` and is
    # the substrate the GraphRAG eval cases (Q2.4) measure against.
    # Keyed by the blueprint's language-neutral `"key"` — see
    # `_DEPENDENCY_EDGES` for why titles cannot be used.
    tasks_by_key = {
        t._blueprint_key: t for t in all_project_tasks if getattr(t, "_blueprint_key", None)
    }
    # Attributed, because adding a blocker edge makes `sync_blocked_status`
    # flip the blocked task to `Blocked`, which the signals record as a real
    # status change. Unattributed it renders "Someone marked this Blocked"
    # on seven tasks — and those rows sit right next to named ones, so the
    # anonymous half reads as a bug rather than as system behaviour.
    with _acting_as(demo_user):
        _seed_task_dependencies(team, demo_user, tasks_by_key)

    # Replay the decisions made ABOUT these tasks, so each one's activity
    # feed shows a history instead of a single "created this". Must run
    # AFTER the dependency edges above: `sync_blocked_status` derives
    # `Blocked` from open blocker edges, and a status replay crossing the
    # blocking boundary re-runs that derivation — with the edges already
    # in place it settles on the same answer the edges imply.
    history_keys = _replay_task_history(tasks_by_key, demo_user, bots, _TASK_HISTORY)

    return {
        "project": project,
        "blueprint": blueprint,
        "tasks": tasks,
        "backing_task": backing_task,
        "extra_milestones": extra_milestones,
        "history_keys": history_keys,
    }


# Realistic within-project dependencies. Each pair is
# (blocker_key, blocked_key) meaning "blocker blocks blocked", naming tasks by
# their blueprint `"key"` — the language-neutral identifier every task spec
# carries. Pairs whose endpoints aren't both in the given project's task list
# are skipped, so the same list is safe to apply to every project.
#
# Keys rather than title substrings, because titles are TRANSLATED. Matching
# "Migrate marketing pages" against a task titled
# "マーケティングページを design system v3 へ移行" finds nothing, and the miss is
# silent — `get_or_create` is simply never reached. That cost the Japanese
# workspace 9 of its 10 edges, and with them every one of its 7 Blocked tasks
# (`Blocked` is DERIVED from open blocker edges by `sync_blocked_status`, so
# no edges means no Blocked column at all), the 2-hop chain the GraphRAG evals
# measure, and the premise of the organize demo — whose whole point is that
# Billing's priorities contradict its dependency order. With no dependencies
# there is no order to contradict, so the demo's climax had nothing to fix.
#
# Nothing detected it: the edges live here rather than in either content
# table, so `assert_content_parity` cannot see them, and a "mostly Japanese"
# ratio assertion says nothing about a dependency graph that simply isn't
# there. Keys make the match language-independent by construction.
_DEPENDENCY_EDGES = [
    # Website Redesign: the spike decision and the design-system migration both
    # gate the Hero build.
    ("spike_framer_motion", "build_hero_featuregrid"),
    ("migrate_marketing_pages", "build_hero_featuregrid"),
    # …and the Hero build gates the accessibility audit, giving the graph a
    # 2-hop chain (framer-motion → Hero → Accessibility audit) — the A1
    # multi-hop eval target (graph_native_2hop_* in retrieval_cases.yaml).
    ("build_hero_featuregrid", "accessibility_audit"),
    # Q2 Roadmap: interview synthesis and competitor analysis feed the proposal.
    ("synthesize_interviews", "roadmap_proposal"),
    ("competitor_analysis", "roadmap_proposal"),
    # Billing & Plans Revamp (the messy organize-demo milestone): the
    # entitlements matrix gates integration + metering, which gate the
    # customer-facing flow and the support dashboard. The blueprint's
    # priorities/due dates deliberately CONTRADICT this order — that
    # contradiction is what the update_tasks_bulk organize flow fixes.
    ("billing_entitlements_matrix", "payment_provider_sandbox"),
    ("billing_entitlements_matrix", "usage_metering"),
    ("payment_provider_sandbox", "upgrade_downgrade_flow"),
    ("usage_metering", "billing_admin_dashboard"),
    ("upgrade_downgrade_flow", "billing_admin_dashboard"),
]


def _seed_task_dependencies(team, demo_user, tasks_by_key: dict) -> None:
    """Seed the blocker→blocked edges whose both endpoints are in this
    project, keyed by blueprint `"key"` rather than by title.

    `tasks_by_key` maps blueprint key → TaskMaster for one project's tasks.
    A key that isn't present belongs to another project and is skipped —
    which is how one shared edge list serves every project.
    """
    for blocker_key, blocked_key in _DEPENDENCY_EDGES:
        blocker = tasks_by_key.get(blocker_key)
        blocked = tasks_by_key.get(blocked_key)
        if not blocker or not blocked or blocker.task_id == blocked.task_id:
            continue
        TaskDependency.objects.get_or_create(
            blocker_task=blocker,
            blocked_task=blocked,
            defaults={"team": team, "created_by": demo_user},
        )


# Task history: the decisions a team makes ABOUT a task, not just its
# final state.
#
# Without this every seeded task is born in the state it ends in, so its
# TaskActivityFeed reads "created this" and nothing else — 25 `created`
# rows and 7 incidental `status_changed` across a whole workspace. A real
# product team re-prioritises, parks work, pulls in dates and reassigns,
# and that record is most of what makes a three-month workspace look
# three months old. A task that was always High and always Open has no
# story; one that went Normal → High when a customer escalated, then
# Pending when the dependency slipped, does.
#
# Why REPLAY saves instead of writing TaskActivity rows directly: the
# rows are emitted by `task_signals` from a pre/post-save diff, so
# replaying the change produces exactly the shape the real app produces —
# correct `field_name`, correct old→new pair, correct actor — and cannot
# drift from the frontend's render contract (`TaskActivityFeed` keys its
# chip colours and relation labels off `field_name`). Hand-written rows
# would be a second, silently-diverging implementation of that contract.
#
# Each step is (task_key, actor_key, field, value) and describes the state
# the task PASSED THROUGH, in order. The final state is the blueprint's,
# not the list's: `_replay_task_history` walks these values and then
# restores whatever the blueprint declared, so the search index — which
# stores only the final `task_status` / `task_priority` (task_chunker.py)
# — is unchanged by construction. No chunker indexes TaskActivity at all,
# so this whole surface is display-only and carries no retrieval risk.
#
# Three authoring rules, each learned from a bad first draft:
#
#  1. **Never name the blueprint's own final value in a step.** The
#     restore step supplies it. Writing it explicitly produces a visible
#     `High → Low → High` round-trip in the feed, which reads as a seeder
#     looping rather than a team deciding.
#  2. **Never set `status` on a task whose final status is `Blocked`.**
#     `Blocked` is DERIVED from open blocker edges by `sync_blocked_status`
#     (see `_DEPENDENCY_EDGES`), so a status replay on one of those seven
#     tasks fights the derivation and lands wherever the last writer won.
#     Change their priority or assignee instead.
#  3. **Keep step counts under the task's age in days.** `_backdate_history`
#     spreads a task's activity between its creation and now; six changes
#     on a two-day-old task compress into one afternoon.
_TASK_HISTORY = [
    # --- Website Redesign -------------------------------------------------
    # The framer-motion spike is the workspace's SPINE: it is the decision
    # the DM, the GM and the decision note all cite, and the one a visitor
    # is most likely to open. So it gets the fullest history — filed as a
    # side investigation, escalated when the bundle number landed, then
    # closed. Ends Closed/Low: low because once it was decided the spike
    # itself stopped mattering, which is exactly what the feed should show.
    ("spike_framer_motion", "bot2", "priority", "Normal"),
    ("spike_framer_motion", "bot1", "status", "WIP"),
    ("spike_framer_motion", "bot2", "priority", "High"),
    # The audit that fed the migration. Closed, so its changes wrap up in
    # the first third of its life alongside its comments.
    ("audit_marketing_pages", "demo", "priority", "Low"),
    ("audit_marketing_pages", "demo", "status", "WIP"),
    # Hero + FeatureGrid is Blocked-derived (rule 2) — priority only. It was
    # the first thing scheduled after the spike settled, then had to wait.
    ("build_hero_featuregrid", "bot2", "priority", "Normal"),
    # The accessibility pass is also Blocked-derived. Dan picked it up from
    # Carol once it became a remediation job rather than a review.
    ("accessibility_audit", "bot2", "assignee", "bot2"),
    # The launch blocker that started life as routine cleanup. Filed Low by
    # Bob; escalated once the trace showed it was the design-system cutover.
    # Only 2 days old, so it gets ONE change (rule 3).
    ("slow_homepage_load", "bot1", "priority", "Low"),
    # Plausible analytics: scope grew when marketing asked for named events,
    # then settled back once it was split out. Ends Normal per blueprint.
    ("plausible_analytics", "bot1", "priority", "Low"),
    ("plausible_analytics", "bot0", "priority", "High"),
    # The nav work was Carol's while it was a drawer prototype, then became
    # engineering's. Ends WIP/bot1 per blueprint.
    ("responsive_navigation", "bot2", "assignee", "bot2"),
    ("responsive_navigation", "bot2", "status", "Open"),
    # Marketing-page migration: pulled its date in when the launch firmed up.
    ("migrate_marketing_pages", "demo", "due_offset", 21),
    ("migrate_marketing_pages", "bot1", "priority", "Normal"),
    # --- Q2 Roadmap -------------------------------------------------------
    # Competitor analysis was started, then de-prioritised when the
    # interviews took over — the "let's park this" case. Ends Open/Normal.
    ("competitor_analysis", "demo", "priority", "High"),
    ("competitor_analysis", "demo", "status", "WIP"),
    ("competitor_analysis", "bot0", "priority", "Low"),
    ("competitor_analysis", "demo", "status", "Open"),
    # Q3 OKRs: Alice drafted it and pushed the date out as the quarter
    # slipped; it came back to the visitor escalated, with the date pulled in.
    ("q3_okrs", "demo", "due_offset", 30),
    ("q3_okrs", "bot0", "priority", "Normal"),
    ("q3_okrs", "bot0", "assignee", "bot0"),
    ("q3_okrs", "bot0", "due_offset", 40),
    ("q3_okrs", "demo", "priority", "High"),
    # Onboarding funnel: escalated, then parked pending the synthesis —
    # which is exactly what its `Pending` status means. Ends Pending/Normal.
    ("onboarding_funnel", "demo", "priority", "High"),
    ("onboarding_funnel", "bot3", "status", "WIP"),
    ("onboarding_funnel", "demo", "priority", "Critical"),
    ("onboarding_funnel", "bot3", "status", "Pending"),
    # Stakeholder map quietly dropped down the list. Nobody's fault.
    ("stakeholder_map", "demo", "priority", "Normal"),
    ("stakeholder_map", "bot0", "status", "WIP"),
    # Interview synthesis: the date moved twice as recruiting slipped.
    ("synthesize_interviews", "bot0", "due_offset", 3),
    ("synthesize_interviews", "demo", "priority", "Normal"),
    ("synthesize_interviews", "bot0", "due_offset", 12),
    # The two milestone children under Q2 Discovery Sprint. Neither is
    # blocked-derived, so both can take status. Recruiting ran long and the
    # scheduling task was escalated before it closed; the coding job grew
    # once it became synthesis rather than transcription.
    #
    # Note the actor ORDER matters here, not just the set. A history with
    # one step per column supplies every column's birth value and so emits
    # no replay rows at all — only the closing restore, which is attributed
    # to the LAST step's actor. Put the person who should own the visible
    # change last, or the whole feed reads as the demo user's own doing.
    ("schedule_interviews", "demo", "priority", "Low"),
    ("schedule_interviews", "bot0", "status", "WIP"),
    ("code_recordings", "demo", "status", "Open"),
    ("code_recordings", "bot0", "priority", "Normal"),
    # --- Billing & Plans Revamp (the messy organize-demo milestone) -------
    # This milestone is DELIBERATELY inconsistent — the organize demo's
    # premise is that priorities contradict dependency order. The history
    # shows that mess ACCUMULATING (each change locally reasonable) rather
    # than arriving fully formed, which is what makes the AI's later
    # "these priorities disagree with your dependencies" land.
    #
    # Four of the six are Blocked-derived, so priority/assignee only
    # (rule 2): upgrade_downgrade_flow, payment_provider_sandbox,
    # usage_metering, billing_admin_dashboard.
    ("upgrade_downgrade_flow", "bot1", "priority", "Low"),
    ("upgrade_downgrade_flow", "bot0", "priority", "Critical"),
    ("payment_provider_sandbox", "demo", "priority", "Normal"),
    ("payment_provider_sandbox", "demo", "assignee", "bot0"),
    ("payment_provider_sandbox", "bot1", "priority", "High"),
    ("usage_metering", "bot1", "priority", "High"),
    ("billing_admin_dashboard", "demo", "priority", "Low"),
    # These two are NOT blocked, so status is fair game.
    ("billing_entitlements_matrix", "bot0", "priority", "Normal"),
    ("billing_entitlements_matrix", "bot1", "status", "WIP"),
    ("invoice_pdf_templates", "bot0", "priority", "Normal"),
]

# Fields a history step may touch, mapped to the TaskMaster column. A
# step naming anything else is a blueprint bug and raises (see below) —
# silently ignoring it would mean the feed just quietly lacks the row.
_HISTORY_FIELDS = {
    "status": "status",
    "priority": "priority",
    "assignee": "assignee_id",
    "due_offset": "due_date",
}

# Tasks whose final status is derived, not declared — see rule 2 above.
_DERIVED_STATUS_KEYS = frozenset(blocked for _, blocked in _DEPENDENCY_EDGES)



def _replay_task_history(tasks_by_key: dict, demo_user, bots, history) -> set:
    """Replay per-task decisions so the audit feed has real history.

    Each task is first rewound (silently) to the earliest state its history
    names, then each subsequent step re-saves one field with a specific
    actor, which makes `task_signals` emit the matching `TaskActivity` row.
    The task is finally landed back on its blueprint state, so **only the
    audit trail changes** — every task still ends in the
    status/priority/assignee the blueprint declares, which is what the
    search index and every eval case actually read.

    Returns the set of blueprint keys whose history was applied, so the
    caller can tell a key that matched no project at all (a typo) from one
    that matched a different project (normal — the same list is applied
    per-project).

    Raises on a blueprint that violates the authoring rules above rather
    than seeding a feed that reads wrong. Each of these was a real defect
    in the first draft, and every one of them is invisible in aggregate
    counts — only reading the rendered feed finds them, so they are
    asserted here instead.
    """
    applied = 0
    # Group by task so each task is restored exactly once, after all of
    # its steps — restoring between steps would double every row.
    steps_by_task: dict = {}
    for task_key, actor_key, field, value in history:
        if task_key not in tasks_by_key:
            # Belongs to another project's blueprint; the same list is
            # applied per-project, same contract as `_DEPENDENCY_EDGES`.
            continue
        if field not in _HISTORY_FIELDS:
            raise ValueError(
                f"_TASK_HISTORY step for {task_key!r} names unknown field {field!r}; "
                f"expected one of {sorted(_HISTORY_FIELDS)}"
            )
        # Rule 2: a status replay on a task whose status is DERIVED from
        # dependency edges fights `sync_blocked_status` and lands
        # nondeterministically — the loser is whichever wrote last.
        if field == "status" and task_key in _DERIVED_STATUS_KEYS:
            raise ValueError(
                f"_TASK_HISTORY sets status on {task_key!r}, whose status is derived "
                "from its blocker edges by sync_blocked_status. Change priority or "
                "assignee instead, or remove the dependency edge."
            )
        steps_by_task.setdefault(task_key, []).append((actor_key, field, value))

    for task_key, steps in steps_by_task.items():
        task = tasks_by_key[task_key]
        # Reload the whole row before touching it. `sync_blocked_status`
        # wrote `status` via a queryset `.update()` during dependency
        # seeding, which leaves the in-memory instance stale — and a stale
        # instance does not merely restore a wrong value, it makes the
        # signal INVENT one: `pre_save` snapshots the DB row while
        # `post_save` diffs the instance, so any tracked field that drifted
        # yields a phantom `Blocked → Open` row on the next save of an
        # unrelated field. That row claims a change the DB never made.
        #
        # Refreshing only the columns this task's history touches is not
        # enough — the diff covers every field in `_TRACKED_TASK_FIELDS`,
        # so the reload has to be as wide as the diff.
        task.refresh_from_db()
        touched = {field for _, field, _ in steps}
        final = {_HISTORY_FIELDS[f]: getattr(task, _HISTORY_FIELDS[f]) for f in touched}

        # Resolve every step to a concrete column value up front, so the
        # birth state below can be read off the same list the replay walks.
        resolved_steps = []
        for actor_key, field, value in steps:
            column = _HISTORY_FIELDS[field]
            if field == "assignee":
                resolved = _resolve_user(value, demo_user, bots)
                new_value = resolved.id if resolved else None
            elif field == "due_offset":
                new_value = date.today() + timedelta(days=int(value))
            else:
                new_value = value
            resolved_steps.append((actor_key, column, new_value))

        # The BIRTH state: the first value each touched column passes
        # through. Applied with a queryset `.update()` so `task_signals`
        # emits nothing — the task has to *look* born here, and its
        # `created` row is already written.
        #
        # This rewind is what keeps the feed from round-tripping. The task
        # is inserted in its FINAL state, so a replay that starts there and
        # restores there cannot help ending where it began: every chain
        # rendered `High → Low → High`, which reads as a seeder looping
        # rather than a team deciding. Rewinding first makes the visible
        # chain strictly forward — birth → … → blueprint.
        #
        # Safe against `sync_blocked_status` because rule 2 above forbids
        # `status` steps on derived-status tasks, so `status` is never in
        # `birth` for one of those seven. `.update()` also names only these
        # columns, so `ts_updated_at` is untouched and the row stays
        # indexable (the invariant `test_index_timestamps_stay_fresh` pins).
        birth: dict = {}
        for _, column, new_value in resolved_steps:
            birth.setdefault(column, new_value)

        # Rule 1, generalised: a chain that starts where it ends IS the
        # round-trip, whatever its length. Checked per column against the
        # birth value rather than against every step, because an
        # intermediate value equal to the final one is fine — a task that
        # goes High → Low → High → Normal reads as a real argument.
        for column, value in birth.items():
            if final.get(column) == value:
                raise ValueError(
                    f"_TASK_HISTORY starts {task_key!r}'s {column!r} history at "
                    f"{value!r}, which is also the blueprint's final value — the "
                    "chain would return to where it began and render as a "
                    "round-trip. Make the first step a state the task has since left."
                )

        TaskMaster.objects.filter(pk=task.pk).update(**birth)
        for column, value in birth.items():
            setattr(task, column, value)
        if "status" in birth:
            _patch_created_status(task, birth["status"])

        # Visible replay: every step except the one that supplied its
        # column's birth value, which is already in place and would emit
        # nothing (the signal diffs).
        born: set = set()
        actors_seen: set = set()
        for actor_key, column, new_value in resolved_steps:
            if column not in born:
                born.add(column)
                continue
            if getattr(task, column) == new_value:
                # A no-op save emits no activity row, so skip it rather
                # than counting a step that did nothing.
                continue
            actor = _resolve_user(actor_key, demo_user, bots)
            setattr(task, column, new_value)
            _save_as(task, actor, update_fields=[column])
            applied += 1
            actors_seen.add(actor.id if actor else None)

        # Land on the blueprint end state. Attributed to the same actor
        # who made the last change, which reads naturally in the feed.
        last_actor = _resolve_user(steps[-1][0], demo_user, bots)
        restore = {c: v for c, v in final.items() if getattr(task, c) != v}
        if restore:
            # A restore that CLOSES the task is written in its own save,
            # after the others. One save emits one row per changed field in
            # `_TRACKED_TASK_FIELDS` order, which puts `status` before
            # `priority` — so a closing restore rendered "closed this" and
            # then "changed priority", i.e. a task still being triaged
            # after it shipped. Closing is the last thing that happens to a
            # task, and the feed has to say so.
            closing = (
                {"status": restore.pop("status")}
                if restore.get("status") in CLOSED_STATUSES
                else {}
            )
            for group in (restore, closing):
                if not group:
                    continue
                for column, value in group.items():
                    setattr(task, column, value)
                _save_as(task, last_actor, update_fields=list(group))
                applied += len(group)
                actors_seen.add(last_actor.id if last_actor else None)

        # Rule 4: the feed must show somebody other than the visitor.
        #
        # Every step's actor is wishful until the replay runs, because the
        # step that supplies a column's BIRTH value emits no row — it is
        # applied by the silent rewind. So a task whose history names one
        # step per column emits only the closing restore, attributed to the
        # LAST step's actor, and the named bots in the earlier steps never
        # appear at all. That reads as the demo visitor having quietly done
        # everything themselves, which is the opposite of the collaboration
        # the workspace exists to show — and it is invisible to every count,
        # since the rows are all present and correctly attributed to
        # *someone*. Checked against rows actually written, not intent.
        if actors_seen and actors_seen == {demo_user.id}:
            named = {actor for actor, _f, _v in steps}
            if named - {"demo"}:
                raise ValueError(
                    f"_TASK_HISTORY names {sorted(named - {'demo'})} on {task_key!r} but "
                    "every row it emitted is the demo user's: the steps naming those "
                    "actors supplied birth values, which the rewind applies silently. "
                    "Reorder so a bot's step is last, or add a second step on one of "
                    "its columns."
                )

    return set(steps_by_task)


def _patch_created_status(task, status: str) -> None:
    """Keep the `created` row's payload consistent with the rewind.

    `task_record_changes` stamps `{"title", "status", "is_milestone"}` into
    the CREATED row at insert time, so it names the task's FINAL status —
    which contradicts a feed whose first visible change starts from the
    birth state. `status` is the only key of the three that any history
    step touches, so correcting it is complete.
    """
    row = (
        TaskActivity.objects.filter(task=task, action_type=TaskActivityActionType.CREATED)
        .order_by("activity_id")
        .first()
    )
    if row is None or not isinstance(row.new_value, dict):
        return
    payload = dict(row.new_value)
    if payload.get("status") == status:
        return
    payload["status"] = status
    TaskActivity.objects.filter(pk=row.pk).update(new_value=payload)


@contextmanager
def _acting_as(actor):
    """Attribute audit rows written in this block to `actor`.

    `task_signals._record` reads the actor from the thread-local set by
    `CurrentUserMiddleware`, which the seeder never runs through — so
    without this every seeded activity row has `actor=None` and the feed
    renders "Someone" against each change. Restores the previous value
    rather than clearing, so nesting is safe and a caller that WAS inside
    a request keeps its actor.
    """
    previous = get_current_user()
    set_current_user(actor)
    try:
        yield
    finally:
        if previous is not None:
            set_current_user(previous)
        else:
            clear_current_user()


def _save_as(task, actor, *, update_fields):
    """`task.save()` attributed to `actor` in the audit trail."""
    with _acting_as(actor):
        task.save(update_fields=update_fields)


def _create_sprint(
    team,
    project,
    *,
    prior_sprints: int = 0,
    name_template: str = "Sprint {n}",
) -> Sprint:
    """Create the project's sprint cadence and return the ACTIVE sprint.

    `prior_sprints` (demo only) prepends N already-completed sprints
    before the active one, so the project shows visible cadence instead
    of looking like it started today. The default 0 reproduces the
    original single-sprint behaviour byte-for-byte — `starter_seeder`
    calls this for real customer workspaces, where a brand-new project
    genuinely has no history to show.

    `name_template` is the sprint's display name, formatted with `n`.
    It exists because sprint names are *visible* — the sprint picker and
    `get_sprint_summary` both render them — and the demo seeds Japanese
    workspaces, where a row reading "Sprint 1" is one of the few
    untranslated strings a visitor would actually see. It is a keyword
    with an English default so `starter_seeder`'s positional call (real
    customer workspaces) keeps its current behaviour untouched.

    Sprint dates are plain `DateField`s (no `auto_now`), so unlike every
    other timestamp in the demo these can just be passed to `create()`
    and need no backdating pass.
    """
    today = date.today()
    duration = 14
    # With history, the active sprint is mid-flight (started 5 days ago)
    # rather than starting today — a sprint that begins the instant you
    # sign in is the same "created 8 seconds ago" tell the backdating
    # exists to remove. Without history, keep the original anchor.
    active_start = today - timedelta(days=5) if prior_sprints else today
    SprintConfig.objects.create(
        team=team,
        project=project,
        duration_days=duration,
        anchor_date=active_start,
        auto_roll=False,
    )
    # Completed sprints, oldest first, each ending the day before the
    # next one starts. `sequence_number` is unique per project and must
    # stay monotonic with `start_date`.
    for i in range(prior_sprints):
        back = prior_sprints - i
        start = active_start - timedelta(days=duration * back)
        Sprint.objects.create(
            team=team,
            project=project,
            name=name_template.format(n=i + 1),
            sequence_number=i + 1,
            start_date=start,
            end_date=start + timedelta(days=duration - 1),
            status="completed",
            is_auto_generated=False,
        )
    return Sprint.objects.create(
        team=team,
        project=project,
        name=name_template.format(n=prior_sprints + 1),
        sequence_number=prior_sprints + 1,
        start_date=active_start,
        end_date=active_start + timedelta(days=duration - 1),
        status="active",
        is_auto_generated=False,
    )


def _create_milestone_with_backing_task(
    team,
    project,
    sprint,
    demo_user,
    bots,
    milestone_spec,
):
    """Create the milestone's backing TaskMaster (is_milestone=True)
    plus the MilestoneMaster row pointing to it. Children of the
    milestone (created elsewhere) reference the backing task via
    `parent_task_id` so they render as sub-tasks in the table — that's
    the convention documented in milestone_models.py.
    """
    today = date.today()
    backing_task = TaskMaster.objects.create(
        team=team,
        project=project,
        sprint=sprint,
        reporter=demo_user,
        assignee=demo_user,
        title=milestone_spec["title"],
        status=milestone_spec["status"],
        priority=milestone_spec["priority"],
        content=milestone_spec["body"],
        due_date=today + timedelta(days=milestone_spec.get("due_offset_days", 28)),
        tags=[],
        mentioned_user_ids=[],
        is_milestone=True,
    )

    milestone = MilestoneMaster.objects.create(
        team=team,
        project=project,
        sprint=sprint,
        reporter=demo_user,
        task=backing_task,
        title=milestone_spec["title"],
        description=milestone_spec["body"],
        status=milestone_spec["status"],
        priority=milestone_spec["priority"],
        due_date=today + timedelta(days=milestone_spec.get("due_offset_days", 28)),
    )

    MilestoneAssignees.objects.bulk_create(
        [
            MilestoneAssignees(team=team, milestone=milestone, user=demo_user),
        ]
    )

    return milestone, backing_task


def _create_blueprint_tasks(
    team,
    project,
    sprint,
    milestone,
    backing_task,
    demo_user,
    bots,
    task_specs,
) -> List[TaskMaster]:
    """Create all tasks for one project from the blueprint task specs,
    including subtasks (parent_task_id pointing to a sibling task) and
    milestone children (parent_task_id pointing to the backing task).

    Tasks are created via `.save()` rather than `bulk_create` so the
    post_save signal that sets `root_task_id` fires, and so the audit
    `TaskActivity` rows get populated for the demo's activity feed.

    Returns the created tasks in blueprint order. A spec's optional
    language-neutral `"key"` is copied onto the returned instance as
    `_blueprint_key` so the caller can wire dependencies by key without a
    second pass over the blueprint; specs without one (`starter_seeder`'s
    blueprints) simply carry None.
    """
    today = date.today()
    created: List[TaskMaster] = []

    for idx, spec in enumerate(task_specs):
        assignee = _resolve_user(spec["assignee"], demo_user, bots)

        if spec["parent_idx"] is not None:
            parent_task = created[spec["parent_idx"]]
            parent_task_id = parent_task.task_id
            attached_milestone = parent_task.milestone
        elif spec["is_milestone_child"]:
            parent_task_id = backing_task.task_id
            attached_milestone = milestone
        else:
            parent_task_id = None
            attached_milestone = None

        task = TaskMaster.objects.create(
            team=team,
            project=project,
            sprint=sprint,
            milestone=attached_milestone,
            assignee=assignee,
            reporter=demo_user,
            title=spec["title"],
            status=spec["status"],
            priority=spec["priority"],
            content=spec["body"],
            due_date=today + timedelta(days=spec.get("due_offset_days", 7)),
            tags=[],
            mentioned_user_ids=[],
            parent_task_id=parent_task_id,
        )
        # Transient, never persisted: `TaskMaster` has no key column and
        # should not — the key identifies a blueprint ENTRY, not a task.
        task._blueprint_key = spec.get("key")
        created.append(task)

        comment_rows = []
        for cidx, (sender_key, text) in enumerate(spec.get("comments", [])):
            comment_rows.append(
                TaskComments(
                    task=task,
                    sender=_resolve_user(sender_key, demo_user, bots),
                    comment_id=cidx + 1,
                    comment_body=_text_body(text),
                )
            )
        if comment_rows:
            TaskComments.objects.bulk_create(comment_rows)

    return created


# ---------------------------------------------------------------------------
# Chats (DM, GM, PM) — with thread messages
# ---------------------------------------------------------------------------


def _canonical_dm_pair(user_a_id, user_b_id):
    """Return (user_lo, user_hi) in canonical order for ChannelDirectPair.
    Mirrors `channel_views._canonical_dm_pair` — UUIDs are sorted as
    strings so the canonicalization is deterministic regardless of which
    user "creates" the DM.
    """
    a, b = str(user_a_id), str(user_b_id)
    return (a, b) if a < b else (b, a)


def _create_dms(team, demo_user, bots, content):
    """Three multi-turn DMs (demo↔Alice, ↔Bob, ↔Carol), each with one
    thread on a representative message. Plus a self-DM that matches
    what /jointeam's `moveToTeam` would have created.

    V3 schema only: writes to `Channel(kind=DM)` + `ChannelDirectPair`
    + `ChannelMember` + `Message`. Legacy `DMMessages` / `DMMaster` are
    no longer written — the frontend reads from `/api/v3/channels/`
    exclusively.

    `content` is the per-language content module (see `_content`).
    """
    for spec in content.DM_BLUEPRINTS:
        bot = bots[spec["bot_index"]]
        channel = Channel.objects.create(team=team, kind=ChannelKind.DM, title="")
        user_lo, user_hi = _canonical_dm_pair(demo_user.id, bot.id)
        ChannelDirectPair.objects.create(channel=channel, user_lo=user_lo, user_hi=user_hi)
        ChannelMember.objects.create(channel=channel, user=demo_user, role="member")
        ChannelMember.objects.create(channel=channel, user=bot, role="member")

        thread_spec = spec.get("thread")
        thread_parent_idx = thread_spec["parent_index"] if thread_spec else None

        created_msgs = []
        for midx, (who, text) in enumerate(spec["messages"]):
            sender = bot if who == "bot" else demo_user
            msg = Message.objects.create(
                channel=channel,
                sender=sender,
                seq=midx + 1,
                body=_text_body(text),
                body_text=text,
                metadata={},
            )
            created_msgs.append(msg)

        if thread_spec:
            parent_msg = created_msgs[thread_parent_idx]
            for tidx, (who, text) in enumerate(thread_spec["messages"]):
                sender = bot if who == "bot" else demo_user
                Message.objects.create(
                    channel=channel,
                    sender=sender,
                    seq=len(created_msgs) + tidx + 1,
                    body=_text_body(text),
                    body_text=text,
                    is_thread_reply=True,
                    parent=parent_msg,
                    thread_root=parent_msg,
                    metadata={},
                )
            # Reply count is denormalized on the parent (the v3 message
            # serializer reads it without a count query). Bump here
            # since we bypass `_allocate_seq_and_create_message`.
            parent_msg.reply_count = len(thread_spec["messages"])
            parent_msg.save(update_fields=["reply_count", "ts_updated_at"])

    # Self-DM (personal scratch chat). Mirrors what `moveToTeam` creates
    # on first team entry — without it the workspace lands missing the
    # user's expected default chat. Seeded with several "notes to self"
    # messages so the chat actually loads when Spotlight routes a todo
    # source-chip click to it (an empty chat short-circuits the loader
    # and the user ends up on the inbox fallback).
    #
    # The messages come from the content table rather than living here,
    # because they are six sentences of *visible* prose — the ones a
    # visitor reads in their own scratch chat, and two of them carry story
    # content the workspace cites elsewhere. Hardcoded here they were
    # invisible to `assert_content_parity` (which compares the two content
    # modules) and would have rendered English in a Japanese workspace.
    # Worth noting why no test caught that: with a 213-string corpus, six
    # English rows cap the achievable CJK ratio at 0.972 — far above the
    # 0.6 floor `test_ja_seeds_japanese` asserts. A "mostly Japanese"
    # assertion is structurally blind to a small fixed set of English
    # strings; only reading the seeded rows finds them.
    self_channel = Channel.objects.create(team=team, kind=ChannelKind.DM, title="")
    self_id = str(demo_user.id)
    ChannelDirectPair.objects.create(channel=self_channel, user_lo=self_id, user_hi=self_id)
    ChannelMember.objects.create(channel=self_channel, user=demo_user, role="member")
    for midx, text in enumerate(content.SELF_DM_MESSAGES):
        Message.objects.create(
            channel=self_channel,
            sender=demo_user,
            seq=midx + 1,
            body=_text_body(text),
            body_text=text,
            metadata={},
        )


def _create_group_chat(team, demo_user, members, content) -> Channel:
    """Single GM with all team members, one threaded message.

    V3 schema only: writes to `Channel(kind=GM)` + `ChannelMember` +
    `Message`. Legacy `GMMaster` / `GMMembers` / `GMMessages` are no
    longer written.

    `content` is the per-language content module (see `_content`).
    """
    channel = Channel.objects.create(
        team=team,
        kind=ChannelKind.GM,
        title=content.CHANNEL_TITLES["gm"].format(team_name=team.team_name),
        owner=demo_user,
    )
    ChannelMember.objects.bulk_create(
        [
            ChannelMember(
                channel=channel,
                user=u,
                role="owner" if u.id == demo_user.id else "member",
            )
            for u in members
        ]
    )

    gm_blueprint = content.GM_BLUEPRINT
    created_msgs = []
    for midx, (sender_idx, text) in enumerate(gm_blueprint["messages"]):
        msg = Message.objects.create(
            channel=channel,
            sender=members[sender_idx],
            seq=midx + 1,
            body=_text_body(text),
            body_text=text,
            metadata={},
        )
        created_msgs.append(msg)

    # One or more threads, each anchored to a top-level message by
    # index. `extra_threads` carries e.g. the mobile-performance
    # planning discussion (the create_task_plan demo surface).
    next_seq = len(created_msgs) + 1
    for thread_spec in [gm_blueprint["thread"], *gm_blueprint.get("extra_threads", [])]:
        parent_msg = created_msgs[thread_spec["parent_index"]]
        for sender_idx, text in thread_spec["messages"]:
            Message.objects.create(
                channel=channel,
                sender=members[sender_idx],
                seq=next_seq,
                body=_text_body(text),
                body_text=text,
                is_thread_reply=True,
                parent=parent_msg,
                thread_root=parent_msg,
                metadata={},
            )
            next_seq += 1
        parent_msg.reply_count = len(thread_spec["messages"])
        parent_msg.save(update_fields=["reply_count", "ts_updated_at"])

    return channel


def _create_pm_messages(
    project,
    members,
    blueprint,
    tasks,
    backing_task,
    extra_milestones=None,
    *,
    card_labels: dict[str, str] | None = None,
):
    """Seed the PM channel the way the v3 model actually works.

    The v3 PM pane renders ONE top-level "task-card" `Message` per task
    (FK-linked via `Message.task`); anything without a task FK is hidden
    by the frontend filter (`v3ToLegacy.v3MessagesToLegacy`). The PM
    composer is disabled, so free-form project chatter has no home in
    the v3 model. Each task's comments are mirrored under its card as v3
    thread replies — exactly what the live task-create + task-comment
    paths produce.

    Mapping from the blueprint:
      * Every task (milestone backing task + blueprint tasks/subtasks)
        gets a task-card header Message.
      * The curated `pm_messages` / `pm_thread` discussion — which used
        to be seeded as standalone PM messages that the v3 filter now
        drops — is folded into comments on the milestone backing task so
        the content stays searchable and surfaces under that card.

    The PM `Channel` + `ChannelMember` rows were already created by the
    `pm_channel_signals.post_save(ProjectMaster)` receiver; we only
    locate the channel and write `Message` / `TaskComments` rows.
    """
    # Local import avoids any import-time cycle (unified_writer imports
    # the chat models this module also pulls in).
    from origin.services.unified_writer import write_task_comment_as_thread_reply

    channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
    team = project.team
    all_tasks = [backing_task] + list(tasks)
    # Extra milestones (e.g. Billing & Plans Revamp) get their cards after
    # the main blueprint's — backing task first, then its tasks, matching
    # the order the live create paths would have produced.
    for extra in extra_milestones or []:
        all_tasks += [extra["backing_task"], *extra["tasks"]]

    # 1. One task-card header per task. `seq` is per-channel; the channel
    #    starts empty, so headers take seq 1..N and the comment mirror
    #    (below) allocates seq N+1.. for the thread replies.
    for seq, task in enumerate(all_tasks, start=1):
        Message.objects.create(
            channel=channel,
            sender=task.reporter,
            seq=seq,
            body=_task_card_body(
                title=task.title,
                status=task.status,
                priority=task.priority,
                assignee=task.assignee,
                reporter=task.reporter,
                due_date=task.due_date,
                team_id=team.team_id,
                is_milestone=task.is_milestone,
                labels=card_labels,
            ),
            body_text=f"{'🚩' if task.is_milestone else '🧾'} {task.title}",
            task=task,
            metadata={},
        )

    # 2. Fold the curated project discussion into comments on the
    #    milestone backing task (mirrored under its card in step 3).
    def resolve_member(key: str) -> CustomUser:
        return members[0] if key == "demo" else members[int(key[3:]) + 1]

    discussion = list(blueprint["pm_messages"]) + list(blueprint["pm_thread"]["messages"])
    discussion_comments = [
        TaskComments(
            task=backing_task,
            sender=resolve_member(sender_key),
            comment_id=cidx + 1,
            comment_body=_text_body(text),
        )
        for cidx, (sender_key, text) in enumerate(discussion)
    ]
    if discussion_comments:
        TaskComments.objects.bulk_create(discussion_comments)

    # 3. Mirror every task's comments (each blueprint task's seeded
    #    comments + the discussion now on the backing task) as v3 thread
    #    replies under the matching header, via the same writer the live
    #    dual-write path uses. `bypass_flag=True` runs it regardless of
    #    the runtime UNIFIED_MESSAGING_DUAL_WRITE setting.
    for task in all_tasks:
        for c in TaskComments.objects.filter(task=task).order_by("comment_id"):
            write_task_comment_as_thread_reply(
                task_id=task.task_id,
                comment_id=c.comment_id,
                sender_id=str(c.sender_id),
                body=c.comment_body,
                project_id=project.project_id,
                bypass_flag=True,
            )


def _create_notes(team, demo_user, bots, seeded_projects, content):
    """Rich personal + task + chat notes. Each note gets an explicit
    NotePermissionMaster ROLE_OWNER row (the note APIs 403 without
    one, even for the owner). Note types: 1=Personal, 2=Task, 3=Chat.

    `content` is the per-language content module (see `_content`).
    """
    permissions: list[NotePermissionMaster] = []
    titles = content.NOTE_TITLES

    # Sidebar folders for the personal notes — shows off the folder
    # feature out of the box: two root folders, one nested subfolder,
    # and one note left unfiled at the My Notes root so the demo
    # covers both filed and unfiled states.
    folder_getting_started = PersonalNoteFolder.objects.create(
        team=team, owner=demo_user, name=titles["folder_getting_started"]
    )
    folder_product = PersonalNoteFolder.objects.create(
        team=team, owner=demo_user, name=titles["folder_product"]
    )
    folder_design = PersonalNoteFolder.objects.create(
        team=team,
        owner=demo_user,
        name=titles["folder_design"],
        parent_folder_id=folder_product.folder_id,
    )

    # (title, body, folder or None=root)
    personal_specs = [
        (titles["welcome"], content.NOTE_WELCOME_BODY, folder_getting_started),
        (
            titles["spotlight_tips"],
            content.NOTE_SPOTLIGHT_TIPS_BODY,
            folder_getting_started,
        ),
        (titles["weekly_priorities"], content.NOTE_WEEKLY_PRIORITIES_BODY, None),
        (
            titles["design_system_inventory"],
            content.NOTE_DESIGN_SYSTEM_INVENTORY_BODY,
            folder_design,
        ),
    ]
    for title, body, folder in personal_specs:
        note = PersonalNoteMaster.objects.create(
            team=team,
            owner=demo_user,
            title=title,
            body=body,
            folder_id=folder.folder_id if folder else None,
        )
        permissions.append(
            NotePermissionMaster(
                team=team,
                user=demo_user,
                note_id=note.note_id,
                note_type=1,
                role_id=1,
            )
        )

    # Task note: attach to the "Synthesize customer interviews" task
    # (the first task in the Q2 Roadmap project, which is index 1).
    roadmap = seeded_projects[1]
    synthesis_task = roadmap["tasks"][0]
    task_note = TaskNoteMaster.objects.create(
        team=team,
        project=roadmap["project"],
        owner=demo_user,
        task=synthesis_task,
        title=titles["task_synthesis"],
        body=content.TASK_NOTE_SYNTHESIS_METHODOLOGY_BODY,
    )
    permissions.append(
        NotePermissionMaster(
            team=team,
            user=demo_user,
            note_id=task_note.note_id,
            note_type=2,
            role_id=1,
        )
    )

    # Chat note on the GM channel — owner + editors so the bots can see
    # it too. Role IDs: 1=owner, 2=editor, 3=viewer. The chat note is
    # keyed on the v3 `Channel` UUID and attached to the GM standup
    # thread: chat notes are thread-scoped (every chat-note URL is
    # `…/thread/{threadId}/note/{noteId}`, and "Open related chat" opens
    # `currentChatNote.threadId`), so a `thread_root_id=None` note has no
    # thread to open. `thread_root_id` is the standup thread's root
    # `Message.id` — the single top-level GM row that has replies
    # (`is_thread_reply=False, reply_count > 0`), seeded just above by
    # `_create_group_chat`. Falls back to a channel-level note only if the
    # thread root can't be located (defensive; the GM thread is always
    # seeded).
    gm_channel = Channel.objects.filter(team=team, kind=ChannelKind.GM, is_deleted=False).first()
    gm_thread_root = (
        Message.objects.filter(
            channel=gm_channel,
            is_thread_reply=False,
            reply_count__gt=0,
        )
        .order_by("seq")
        .first()
        if gm_channel
        else None
    )
    chat_note = ChatNoteMaster.objects.create(
        team=team,
        owner=demo_user,
        chat_type=2,  # GM
        channel=gm_channel,
        is_thread=gm_thread_root is not None,
        thread_root_id=gm_thread_root.id if gm_thread_root else None,
        title=titles["chat_kickoff_recap"],
        body=content.CHAT_NOTE_GM_KICKOFF_RECAP_BODY,
    )
    permissions.append(
        NotePermissionMaster(
            team=team,
            user=demo_user,
            note_id=chat_note.note_id,
            note_type=3,
            role_id=1,
        )
    )
    permissions.extend(
        NotePermissionMaster(
            team=team,
            user=bot,
            note_id=chat_note.note_id,
            note_type=3,
            role_id=2,
        )
        for bot in bots
    )

    NotePermissionMaster.objects.bulk_create(permissions)


def _create_todos(team, demo_user, content):
    """Seed a few days of todos for the demo user so Spotlight can demo
    the `list_today_todos` / `list_uncompleted_todos` agent tools and
    so cross-entity search ("framer-motion", "Plausible", …) lights up
    todos alongside chats / tasks / notes.

    Topics intentionally overlap with the seeded DMs / tasks / notes so
    Spotlight queries return mixed-entity results:
      * "framer-motion" / "perf budget" → Bob's DM + responsive-nav
        task + a Website Redesign todo
      * "Plausible" / "customer interview synthesis" → matching todos
      * "Q3 bets" → Alice's DM + Q2 Roadmap todo

    Calendar layout (all relative to the server-local "today"):
      * Today        — 7 items, two with subitems, mixed completion
      * Yesterday    — 4 items, three done + one carryover
      * 3 days ago   — 3 items, all done (proves the "fully completed
                       group" UX path)

    `content` is the per-language content module (see `_content`); `t`
    below is its `TODO_CONTENT` table.
    """
    today = timezone.localdate()
    t = content.TODO_CONTENT

    # Tags. Two project-aligned + a generic "Personal" lane. Sort order
    # mirrors the project order in PROJECT_BLUEPRINTS so the pane reads
    # left-to-right like the Spotlight sidebar.
    cat_web = ToDoCategory.objects.create(
        team=team, user=demo_user, name=t["cat_web"], sort_order=0
    )
    cat_q2 = ToDoCategory.objects.create(
        team=team, user=demo_user, name=t["cat_q2"], sort_order=1
    )
    cat_personal = ToDoCategory.objects.create(
        team=team, user=demo_user, name=t["cat_personal"], sort_order=2
    )

    # --- Today ---
    grp_today = ToDoGroup.objects.create(
        team=team, user=demo_user, local_date=today, is_completed=False
    )

    # Parent with two subitems — exercises the nesting UI + agent
    # `list_today_todos` rendering.
    parent_hero = ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        title=t["hero_parent"],
        notes=_text_body(t["hero_parent_notes"]),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        parent_item=parent_hero,
        title=t["hero_sub1"],
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        parent_item=parent_hero,
        title=t["hero_sub2"],
        sort_order=1,
    )

    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        title=t["spike_review"],
        notes=_text_body(t["spike_review_notes"]),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_q2,
        title=t["synthesize"],
        is_completed=True,
        ts_completed_at=timezone.now(),
        sort_order=2,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_q2,
        title=t["reply_alice"],
        notes=_text_body(t["reply_alice_notes"]),
        sort_order=3,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_personal,
        title=t["offer_letter"],
        sort_order=4,
    )
    ToDoItem.objects.create(
        group=grp_today,
        title=t["water"],
        is_completed=True,
        ts_completed_at=timezone.now(),
        sort_order=5,
    )
    _recompute_group_completion_for_demo(grp_today)

    # --- Yesterday ---
    grp_yesterday = ToDoGroup.objects.create(
        team=team,
        user=demo_user,
        local_date=today - timedelta(days=1),
        is_completed=False,
    )
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_web,
        title=t["storybook"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=18),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_web,
        title=t["perf_budget"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=22),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_q2,
        title=t["synthesis_outline"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=24),
        sort_order=2,
    )
    # Carryover: shows up in `list_uncompleted_todos` for the past week.
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_q2,
        title=t["spike_ticket"],
        notes=_text_body(t["spike_ticket_notes"]),
        sort_order=3,
    )
    _recompute_group_completion_for_demo(grp_yesterday)

    # --- 3 days ago (fully completed) ---
    grp_old = ToDoGroup.objects.create(
        team=team,
        user=demo_user,
        local_date=today - timedelta(days=3),
        is_completed=False,
    )
    ToDoItem.objects.create(
        group=grp_old,
        category=cat_web,
        title=t["plausible"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(days=3, hours=2),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_old,
        category=cat_q2,
        title=t["sprint_kickoff"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(days=3, hours=5),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_old,
        category=cat_q2,
        title=t["review_milestones"],
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(days=3, hours=8),
        sort_order=2,
    )
    _recompute_group_completion_for_demo(grp_old)


def _recompute_group_completion_for_demo(group):
    """Mirror of `_recompute_group_completion` in todo_views.py, scoped
    to one group. Inlined here so the seeder doesn't import the view
    layer.
    """
    has_open = ToDoItem.objects.filter(group=group, is_completed=False).exists()
    if group.is_completed == has_open:  # truthy when state needs to flip
        group.is_completed = not has_open
        group.save(update_fields=["is_completed"])


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def delete_demo_team_data(team_id: uuid.UUID) -> None:
    """Delete every row scoped to `team_id` across the team-scoped
    tables. Required because most FKs in this codebase are SET_NULL
    and several chat/note tables reference rows via bare UUIDField.
    Idempotent: re-running on a cleaned-up team is a no-op.

    Also sweeps the OpenSearch index and `RagChunk` tracking table for
    this team. The reindexer only deletes chunks for entities it still
    sees in Postgres; once we delete the source rows, its stale-cleanup
    pass would never find them and the chunks would linger forever.
    """
    # Strip OpenSearch chunks first so a partial failure here doesn't
    # leave the DB cleaned but the index polluted. Best-effort: never
    # raise — cleanup must complete even when the search index is down.
    _delete_demo_team_search_chunks(team_id)

    # suppress_task_activity: deleting TaskComments/TaskAttachments here
    # fires post_delete receivers that INSERT a TaskActivity row for the
    # task being torn down; mid-cascade those orphan against origin_taskmaster
    # and trip the deferred FK at COMMIT. Suppress auditing for the teardown
    # (we're deleting everything anyway). See activity_suppression.
    with transaction.atomic(), suppress_task_activity():
        # Pre-collect parent IDs once so children can be filtered by
        # them without triggering N+1 lookups.
        # Legacy chat ids (still the key on the kept Mention/Reaction fact
        # tables) are derived from the v3 channels via `legacy_chat_id` —
        # the legacy DM/GM master tables are gone.
        dm_ids = list(
            Channel.objects.filter(team=team_id, kind=ChannelKind.DM)
            .exclude(legacy_chat_id=None)
            .values_list("legacy_chat_id", flat=True)
        )
        gm_ids = list(
            Channel.objects.filter(team=team_id, kind=ChannelKind.GM)
            .exclude(legacy_chat_id=None)
            .values_list("legacy_chat_id", flat=True)
        )
        project_ids = list(
            ProjectMaster.objects.filter(team=team_id).values_list("project_id", flat=True)
        )
        task_ids = list(TaskMaster.objects.filter(team=team_id).values_list("task_id", flat=True))
        member_ids = list(
            TeamMembers.objects.filter(team=team_id).values_list("attendee_id", flat=True)
        )

        chat_note_ids = list(
            ChatNoteMaster.objects.filter(team=team_id).values_list("note_id", flat=True)
        )
        task_note_ids = list(
            TaskNoteMaster.objects.filter(team=team_id).values_list("note_id", flat=True)
        )
        personal_note_ids = list(
            PersonalNoteMaster.objects.filter(team=team_id).values_list("note_id", flat=True)
        )

        # Fact tables that key off (chat_type, chat_id) with no FK — must
        # filter explicitly to avoid leaking rows. (MentionFact / ReactionFact
        # are kept analytics tables; the legacy ChatAttachmentFact / ReadStatus
        # tables are dropped, so they're no longer cleaned here.)
        for chat_type, chat_ids in [(1, dm_ids), (2, gm_ids), (3, project_ids)]:
            if not chat_ids:
                continue
            MentionFact.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()
            ReactionFact.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()

        # ActivityFact is team-scoped via a proper FK.
        ActivityFact.objects.filter(team=team_id).delete()

        # Legacy DM/GM/MDM/PM chat trees + UserChatMaster are gone (their
        # tables were dropped in the v3 migration); chat lives entirely on
        # the v3 Channel/Message schema cleaned up just below.

        # V3 unified messaging schema. Order matters: Message.channel is
        # PROTECT, so messages must go before channels; Channel.project
        # and Channel.team are both PROTECT, so channels must go before
        # ProjectMaster / TeamMaster below. Cascades on these deletes
        # take care of MessageReaction, MessageMention, MessageAttachment,
        # Flag, ChannelMember, ChannelDirectPair, Pin, and ReadCursor.
        Message.objects.filter(channel__team=team_id).delete()
        Channel.objects.filter(team=team_id).delete()

        # Per-user todo. ToDoGroup CASCADE removes its items; ToDoCategory
        # rows are user-scoped (not tied to a group) so they need their own
        # pass. (Legacy UserChatMaster is gone — its table was dropped.)
        ToDoGroup.objects.filter(team=team_id).delete()
        ToDoCategory.objects.filter(team=team_id).delete()

        # Note version history. `NoteVersionMaster.team` and `.editor` are
        # both SET_NULL and the row keys its note by a bare `note_id`, so
        # nothing cascades from the team, the note or the user: without this
        # a demo user who edits a note leaves a row behind with both FKs
        # nulled, holding the note body, forever. Before the notes below, so
        # a version written by an in-flight save can't outlive its note.
        NoteVersionMaster.objects.filter(team=team_id).delete()

        # Notes (permissions first, then masters)
        all_note_pairs = (
            [(1, nid) for nid in personal_note_ids]
            + [(2, nid) for nid in task_note_ids]
            + [(3, nid) for nid in chat_note_ids]
        )
        if all_note_pairs:
            # NotePermissionMaster is bare (note_type, note_id) — filter
            # by team is sufficient since we control creation.
            NotePermissionMaster.objects.filter(team=team_id).delete()
        ChatNoteMaster.objects.filter(team=team_id).delete()
        TaskNoteMaster.objects.filter(team=team_id).delete()
        PersonalNoteMaster.objects.filter(team=team_id).delete()
        PersonalNoteFolder.objects.filter(team=team_id).delete()

        # Tasks. TaskActivity / TaskComments / TaskAttachments all CASCADE
        # on task delete, so TaskMaster.delete() sweeps them. The explicit
        # TaskComments delete stays (cheaper first pass), but the real
        # orphan-safety comes from suppress_task_activity() above: without
        # it, the comment/attachment post_delete receivers would re-insert
        # a TaskActivity mid-cascade and trip the deferred FK at COMMIT.
        TaskComments.objects.filter(task_id__in=task_ids).delete()
        TaskMaster.objects.filter(team=team_id).delete()

        # Projects (Sprint/SprintConfig/MilestoneMaster cascade on
        # project delete; MilestoneAssignees cascades on milestone).
        ProjectTags.objects.filter(team=team_id).delete()
        ProjectMembers.objects.filter(team=team_id).delete()
        ProjectMaster.objects.filter(team=team_id).delete()

        # Agent history (Spotlight asks). `AgentSession.team_id` /
        # `AgentRun.team_id` are bare CharFields with no FK, so nothing
        # here cascades from the team or the user — without this pass
        # every demo visitor who asks a single question leaks rows that
        # outlive their team.
        #
        # ORDER MATTERS: runs first, then sessions. `AgentRun.session`
        # is on_delete=SET_NULL (search_engine/models.py), so deleting
        # the sessions first would NULL the link and ORPHAN the runs —
        # along with their steps and LLM-call telemetry — instead of
        # removing them. AgentStep / AgentLlmCall / AgentRunJudgement /
        # AgentRunFeedback all CASCADE from AgentRun, so deleting runs
        # sweeps them.
        AgentRun.objects.filter(team_id=str(team_id)).delete()
        AgentSession.objects.filter(team_id=str(team_id)).delete()

        # Common
        TeamMembers.objects.filter(team=team_id).delete()
        TeamMaster.objects.filter(team_id=team_id).delete()
        # NotificationPreference + UserFeatureAccess cascade on user
        # delete, so they get handled when the bot/demo users are
        # removed in delete_demo_environment.


def delete_demo_environment(demo_user: CustomUser) -> None:
    """Delete every team this demo user owns (with all its data) plus
    the bot peer users that were created as members of those teams,
    then the demo user themself.

    Safe to call on a non-demo user (no-op if the user owns no demo
    teams) but the caller should still gate on `user.is_demo` to avoid
    accidental deletion of real users.
    """
    if not demo_user.is_demo:
        return

    with transaction.atomic():
        owned_demo_teams = list(TeamMaster.objects.filter(owner=demo_user, is_demo=True))
        bot_ids: List[uuid.UUID] = []
        for team in owned_demo_teams:
            bot_ids.extend(
                TeamMembers.objects.filter(team=team)
                .exclude(attendee=demo_user)
                .values_list("attendee_id", flat=True)
            )
            delete_demo_team_data(team.team_id)

        # Bots only ever belong to this one demo team, so it's safe to
        # delete them now. Filtering on is_demo prevents nuking a real
        # user if a bug ever cross-linked them.
        if bot_ids:
            CustomUser.objects.filter(id__in=bot_ids, is_demo=True).delete()

        # Finally the demo user. CASCADE on NotificationPreference /
        # UserFeatureAccess handles those automatically.
        demo_user.delete()
