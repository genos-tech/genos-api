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
from datetime import date, timedelta
from typing import List

from django.core.management import call_command
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from origin.models.common.user_models import CustomUser
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.notification_models import NotificationPreference
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_models import TaskMaster, TaskComments
from origin.models.task.sprint_models import Sprint, SprintConfig
from origin.models.task.milestone_models import MilestoneMaster, MilestoneAssignees
from origin.models.chat.dm_models import (
    DMMaster,
    DMMessages,
    DMThreadMessages,
    UserDMMapping,
)
from origin.models.chat.gm_models import GMMaster, GMMembers, GMMessages, GMThreadMessages
from origin.models.chat.mdm_models import (
    MDMMaster,
    MDMMembers,
    MDMMessages,
    MDMThreadMessages,
)
from origin.models.chat.pm_models import PMMessages, PMThreadMessages
from origin.models.chat.chat_master_models import UserChatMaster
from origin.models.chat.mention_models import MentionFact
from origin.models.chat.reaction_models import ReactionFact
from origin.models.chat.chat_attachment_models import ChatAttachmentFact
from origin.models.chat.read_status_models import ReadStatus, ActivityReadStatus
from origin.models.chat.activity_models import ActivityFact
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.note.chat_note_models import ChatNoteMaster
from origin.models.note.common_note_models import NotePermissionMaster
from origin.search_engine.models import RagChunk

BOT_PROFILES = [
    {"first": "Alice", "last": "Chen", "role": "Product Lead"},
    {"first": "Bob", "last": "Martinez", "role": "Engineer"},
    {"first": "Carol", "last": "Park", "role": "Designer"},
    {"first": "Dan", "last": "OConnor", "role": "QA"},
]


# ---------------------------------------------------------------------------
# BlockNote body builders
# ---------------------------------------------------------------------------

# `isToggleable` ships on every heading the frontend writes; include it
# so seeded blocks match real ones byte-for-byte under diff tools.
_HEADING_PROPS = {
    "level": 3,
    "textColor": "default",
    "isToggleable": False,
    "textAlignment": "left",
    "backgroundColor": "default",
}
_PARA_PROPS = {
    "textColor": "default",
    "textAlignment": "left",
    "backgroundColor": "default",
}
_PLACEHOLDER_STYLES = {"italic": True, "textColor": "gray"}


def _block_id() -> str:
    """Fresh per-block UUID. BlockNote assigns one to every block in
    real user-typed documents; the seeded shape was missing this field,
    making the rows easy to spot as "not user-typed."""
    return str(uuid.uuid4())


def _heading(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "heading",
        "props": _HEADING_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _para(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _placeholder(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": _PLACEHOLDER_STYLES}],
        "children": [],
    }


def _blank_para() -> dict:
    return {
        "id": _block_id(),
        "type": "paragraph",
        "props": _PARA_PROPS,
        "content": [],
        "children": [],
    }


def _bullet(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "bulletListItem",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": {}}],
        "children": [],
    }


def _bullet_placeholder(text: str) -> dict:
    return {
        "id": _block_id(),
        "type": "bulletListItem",
        "props": _PARA_PROPS,
        "content": [{"text": text, "type": "text", "styles": _PLACEHOLDER_STYLES}],
        "children": [],
    }


def _section(title: str, *body: dict) -> list:
    return [_heading(title), *body, _blank_para()]


def _text_body(text: str) -> list:
    """Single-paragraph chat message body. The chat preview renderer
    calls `content.slice(0, -1)` and requires the remainder to be
    non-empty, so we always emit a trailing empty paragraph."""
    return [_para(text), _blank_para()]


def _body(*sections):
    """Build a multi-section rich body. Each section is a tuple
    `(heading, [items])`. Items are either strings (paragraphs) or
    `("bullet", text)` / `("placeholder", text)` tuples."""
    blocks: list = []
    for heading_text, items in sections:
        blocks.append(_heading(heading_text))
        for item in items:
            if isinstance(item, tuple):
                kind, text = item
                if kind == "bullet":
                    blocks.append(_bullet(text))
                elif kind == "bullet_placeholder":
                    blocks.append(_bullet_placeholder(text))
                elif kind == "placeholder":
                    blocks.append(_placeholder(text))
                else:
                    blocks.append(_para(text))
            else:
                blocks.append(_para(item))
        blocks.append(_blank_para())
    return blocks


# ---------------------------------------------------------------------------
# Task body templates — port of `TASK_TEMPLATES` in
# `frontend/weikiy/src/features/tasks/components/contents/CreateTaskForm.tsx`.
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
# Project blueprints — real content the Spotlight AI can answer against.
# Each task has a detailed body so semantic search has substance to
# retrieve and cite. Comments add multi-voice context.
#
# Status values intentionally span Open / WIP / Pending / Closed so the
# board / table views look populated. Tasks marked `is_milestone_child`
# are created as children of the milestone-backing task (parent_task_id
# = backing.task_id) and also reference the milestone FK directly.
# ---------------------------------------------------------------------------


WEBSITE_MILESTONE_BODY = _body(
    (
        "🎯 Goal",
        [
            "Ship the redesigned marketing site to genos.app/ for the v1.0 public "
            "launch. The new pages must pass our accessibility audit and stay "
            "under our Lighthouse mobile performance budget.",
        ],
    ),
    (
        "✅ Success criteria",
        [
            (
                "bullet",
                "All four marketing pages (home, pricing, about, contact) render with @genos/design-system v3 only — zero Bootstrap imports remaining.",
            ),
            ("bullet", "Lighthouse mobile score ≥ 95 on each page."),
            ("bullet", "axe-core reports zero Critical or Serious violations."),
            (
                "bullet",
                "Plausible event tracking live for hero CTA, pricing toggle, and contact form submit.",
            ),
        ],
    ),
    (
        "📦 In scope",
        [
            ("bullet", "Replace Bootstrap layout primitives with design-system Grid / Stack."),
            ("bullet", "Rebuild the hero, feature grid, and pricing table from scratch."),
            ("bullet", "Add Plausible analytics with named events for the CTAs above."),
        ],
    ),
    (
        "🚫 Out of scope",
        [
            ("bullet", "Blog page redesign (deferred to v1.1)."),
            ("bullet", "Internationalization — English only for the launch."),
            ("bullet", "Dark mode for the marketing site (in-app dark mode is separate)."),
        ],
    ),
    (
        "⚠️ Risks & dependencies",
        [
            "Hero illustration handoff from Carol is the long-pole — without it the "
            "homepage rebuild blocks. Backup plan: ship with a placeholder gradient "
            "behind the headline and swap the illustration in for v1.0.1.",
        ],
    ),
)

ROADMAP_MILESTONE_BODY = _body(
    (
        "🎯 Goal",
        [
            "Close Q2's discovery cycle with a roadmap proposal grounded in customer "
            "evidence: 12 interview transcripts coded into themes, a competitive "
            "teardown of Notion / Slack / Linear, and a top-5 bets recommendation for Q3.",
        ],
    ),
    (
        "✅ Success criteria",
        [
            ("bullet", "12 customer interview transcripts coded into ≤ 8 themes in Dovetail."),
            (
                "bullet",
                "Competitor analysis covers feature, pricing, and onboarding for each of Notion, Slack, Linear.",
            ),
            (
                "bullet",
                "Roadmap proposal document reviewed by leadership and lands within the Q2 deadline.",
            ),
            (
                "bullet",
                "Each bet in the Q3 proposal cites at least one interview quote or competitor data point.",
            ),
        ],
    ),
    (
        "📦 In scope",
        [
            ("bullet", "Customer interview synthesis (theme coding + cluster summaries)."),
            ("bullet", "Competitor product teardown (3 competitors, 3 surfaces each)."),
            ("bullet", "Top-5 Q3 bets document with effort estimates."),
        ],
    ),
    (
        "🚫 Out of scope",
        [
            ("bullet", "Pricing experimentation (separate workstream)."),
            ("bullet", "Engineering capacity planning (handled by EM team)."),
        ],
    ),
    (
        "⚠️ Risks & dependencies",
        [
            "Interview coverage is uneven — only 7 of 12 segments are enterprise users. "
            "If the synthesis reveals enterprise-heavy themes we should be cautious about "
            "extrapolating to SMB without a follow-up round.",
        ],
    ),
)


# Each entry is a project's full content blueprint. The seeder iterates
# in order and threads task IDs through subtasks / notes / pm threads.
PROJECT_BLUEPRINTS = [
    {
        "name": "Website Redesign",
        "tags": [
            ("Frontend", "#7c3aed", "#ffffff"),
            ("Design", "#ec4899", "#ffffff"),
            ("Performance", "#f59e0b", "#ffffff"),
            ("Bug", "#ef4444", "#ffffff"),
        ],
        "milestone": {
            "title": "v1.0 Public Launch",
            "body": WEBSITE_MILESTONE_BODY,
            "status": "Open",
            "priority": "High",
            "due_offset_days": 28,
        },
        # Tasks: index 0..N-1. `parent_idx` points to another task in
        # this list (its index) to indicate "subtask under that task";
        # otherwise None. `is_milestone_child` attaches the task to
        # the milestone (parent_task_id = backing.task_id, milestone
        # FK set). Subtasks override that — their parent_task_id is
        # the indicated task, not the backing.
        "tasks": [
            {
                "title": "Migrate marketing pages to design system v3",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 14,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Replace the legacy Bootstrap-based marketing pages with "
                            "primitives from @genos/design-system v3. Covers the home, "
                            "pricing, about, and contact pages.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Current pages stack 2019-era CSS overrides on Bootstrap 4. "
                            "They fail axe-core with two Critical issues (insufficient "
                            "contrast, missing landmark roles) and Lighthouse mobile "
                            "tops out at 71 because of unused CSS shipped from the "
                            "Bootstrap bundle. The redesign also blocks our launch — "
                            "marketing wants to send traffic to the new pages first.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "All four pages render with @genos/design-system v3 primitives only.",
                            ),
                            ("bullet", "Lighthouse mobile score ≥ 95 on each page."),
                            ("bullet", "axe-core reports zero Critical or Serious violations."),
                            (
                                "bullet",
                                "Visual regression suite (Chromatic) passes for desktop + mobile + iPad breakpoints.",
                            ),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Hero illustration handoff from Carol is in the Figma file "
                            "“Marketing v3 hero”. Subtasks below break this down into "
                            "audit + build phases. Coordinate Plausible analytics work "
                            "(separate task) to land before the public DNS cutover.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Kicking this off. I'll start by auditing what's still using Bootstrap so we know the surface area.",
                    ),
                    (
                        "bot1",
                        "Heads up — the pricing table has a custom Bootstrap grid override that's been brittle for months. I think we should rewrite the pricing component from scratch rather than carrying that pattern forward.",
                    ),
                    (
                        "bot2",
                        "Hero illustration is in Figma now: file “Marketing v3 hero”. Two variants attached — gradient + photo. Lean toward the gradient one for performance.",
                    ),
                ],
            },
            {
                "title": "Audit existing marketing pages",
                "status": "Closed",
                "priority": "Normal",
                "assignee": "demo",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 3,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Inventory every marketing page and list each component still "
                            "depending on Bootstrap. Output drives the migration order.",
                        ],
                    ),
                    (
                        "📌 Findings",
                        [
                            (
                                "bullet",
                                "Homepage: hero, feature grid, and testimonials — all on Bootstrap rows.",
                            ),
                            (
                                "bullet",
                                "Pricing: custom Bootstrap grid override (brittle, recommend full rewrite).",
                            ),
                            (
                                "bullet",
                                "About: mostly plain HTML; minimal Bootstrap dependency. Lowest effort to migrate.",
                            ),
                            (
                                "bullet",
                                "Contact: form uses Bootstrap form-group classes plus jQuery validation — needs design-system Form primitives.",
                            ),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Migration order recommendation: About → Contact → Pricing → "
                            "Homepage. About is lowest-risk; homepage is highest-impact "
                            "but should land after the smaller pages prove the pattern.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Audit finished. Pricing is the messiest piece — recommend rewriting that component wholesale rather than trying to port the existing markup.",
                    ),
                    (
                        "bot1",
                        "Agreed. The grid override there is the root cause of the cents-alignment bug we keep getting reports about. A clean rewrite kills two birds.",
                    ),
                ],
            },
            {
                "title": "Build Hero + FeatureGrid in design system v3",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot2",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 7,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Implement Hero and FeatureGrid components in @genos/design-system "
                            "v3 with Storybook coverage. These two compose the homepage rebuild "
                            "and are reused across the pricing and about pages.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Hero supports image, video, and illustration variants via a single `media` prop.",
                            ),
                            (
                                "bullet",
                                "FeatureGrid is responsive 1 / 2 / 3 columns without per-component media queries — uses CSS container queries via design-system tokens.",
                            ),
                            (
                                "bullet",
                                "Storybook stories cover every variant plus the loading and empty states.",
                            ),
                            ("bullet", "axe-core passes on every Storybook story."),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Hero is mostly done — Carol's illustration is wired in behind a "
                            "feature flag while the team reviews. FeatureGrid still needs the "
                            "3-column → 2-column container-query breakpoint sorted out for the "
                            "iPad landscape case.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot2",
                        "Hero is mostly done. Wiring the illustration in behind a feature flag while we sanity-check the gradient version against the metrics.",
                    ),
                    (
                        "bot1",
                        "Need a code review on the container-query approach — the iPad landscape breakpoint feels off. Pinging Bob.",
                    ),
                ],
            },
            {
                "title": "Implement responsive navigation with mobile drawer",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot1",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 10,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Replace the existing nav with a responsive component that uses a "
                            "hamburger drawer below 768px and a full horizontal layout above. "
                            "Keyboard focus order and screen-reader landmark must be correct.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Current nav is two separate components — one rendered on desktop, "
                            "one on mobile — that drift from each other constantly. Six bugs "
                            "this quarter have been mobile-only or desktop-only nav regressions. "
                            "Folding them into one responsive component eliminates that drift.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Single Nav component renders correctly at 320px, 768px, 1024px, and 1440px breakpoints.",
                            ),
                            (
                                "bullet",
                                "Drawer traps focus when open and restores focus to the toggle on close.",
                            ),
                            ("bullet", "Escape closes the drawer."),
                            ("bullet", "Skip-to-content link is the first focusable element."),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "Initial PR is up — drawer animation feels off below 320px (very small screens). Will tune the spring config.",
                    ),
                    (
                        "demo",
                        "Add a Storybook story for the 320px case so we don't regress it. Cypress can also pick it up if we run viewport tests.",
                    ),
                ],
            },
            {
                "title": "Set up Plausible analytics with named events",
                "status": "Open",
                "priority": "Normal",
                "assignee": "bot1",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 17,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Wire Plausible Analytics into the new marketing pages so we "
                            "have traffic measurement and page-view counts in place from "
                            "day one of the redesign launch. Named custom events for the "
                            "hero CTA, pricing-toggle, and contact-form-submit on top. "
                            "Plausible is privacy-friendly and GDPR-compliant — Google "
                            "Analytics is out for that reason.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Marketing needs to measure visitor traffic on the new pages "
                            "to know what's actually working: which pages get the most "
                            "page views, where traffic drops off, and which hero CTA "
                            "wording drives more signups (A/B test queued in v1.1). "
                            "Plausible is the team-wide standard for site analytics + "
                            "traffic measurement since the GDPR review last quarter.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Plausible script loaded on all new marketing pages via the "
                                "site-wide layout — page views and visitor sessions tracked "
                                "automatically.",
                            ),
                            (
                                "bullet",
                                "Custom events: `hero_cta_click`, `pricing_toggle_monthly_to_annual`, `pricing_toggle_annual_to_monthly`, `contact_form_submit`.",
                            ),
                            (
                                "bullet",
                                "Traffic numbers (page views, unique visitors, bounce rate) "
                                "and custom events both visible in the Plausible dashboard "
                                "on staging.",
                            ),
                            ("bullet", "No PII (email, name, etc.) in event properties."),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Plausible domain is already provisioned: genos-marketing.plausible.io. "
                            "Marketing has read access for traffic dashboards; engineering owns the script tag.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "Will land this after the nav merge so I don't conflict on the layout file.",
                    ),
                    (
                        "bot0",
                        "Marketing asked: can we add a `pricing_card_hover` event too? It'd help them tell which tier draws the most attention.",
                    ),
                    ("bot1", "Yes — easy add. I'll include it in the same PR."),
                ],
            },
            {
                "title": "Accessibility audit and remediation pass",
                "status": "Pending",
                "priority": "Normal",
                "assignee": "bot3",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 21,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Full WCAG 2.1 AA audit of the redesigned marketing pages using "
                            "axe-core + manual screen-reader testing. File and fix every "
                            "Critical or Serious finding before launch.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Our existing pages have two Critical axe-core violations: "
                            "insufficient color contrast on the secondary button (3.8:1 — "
                            "needs to be ≥ 4.5:1) and missing landmark roles in the nav. "
                            "These have been outstanding for over a year and are blocking "
                            "the enterprise procurement deal that requires VPAT documentation.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Zero Critical or Serious axe-core violations across all four marketing pages.",
                            ),
                            (
                                "bullet",
                                "All interactive elements reachable and operable via keyboard alone.",
                            ),
                            (
                                "bullet",
                                "Screen reader (VoiceOver on macOS Safari, NVDA on Windows Firefox) read-through gives a coherent linear narrative for each page.",
                            ),
                            (
                                "bullet",
                                "Color contrast ≥ 4.5:1 for all text and ≥ 3:1 for large text and UI components.",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot3",
                        "I'll start the audit once the FeatureGrid PR lands so I'm testing the real surface. Pre-audit will go quicker if Carol can confirm the secondary-button contrast in the latest tokens.",
                    ),
                    (
                        "bot2",
                        "Secondary button uses `--color-action-secondary` which is 4.6:1 against `--color-surface-default`. Should be safe but I'll re-verify with a contrast checker.",
                    ),
                ],
            },
            {
                "title": "Investigate slow homepage load on mobile Safari",
                "status": "WIP",
                "priority": "Normal",
                "assignee": "bot1",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 5,
                "body": _body(
                    (
                        "🐞 Summary",
                        [
                            "Reports of 4-6s homepage load times specifically on iOS Safari "
                            "16 and 17. Same network, Chrome on the same device loads in ~1s.",
                        ],
                    ),
                    (
                        "🔁 Steps to reproduce",
                        [
                            ("bullet", "Open Safari on iPhone running iOS 16.5 or later."),
                            ("bullet", "Navigate to https://genos.app/ on a 4G connection."),
                            (
                                "bullet",
                                "Observe the time-to-interactive in Safari's web inspector.",
                            ),
                        ],
                    ),
                    (
                        "🎯 Expected behavior",
                        [
                            "Homepage should be interactive in under 2.5s on mobile, matching "
                            "what Chrome on the same device shows.",
                        ],
                    ),
                    (
                        "💥 Actual behavior",
                        [
                            "Time to interactive lands at 4.2–6.1s. The waterfall shows a "
                            "long blocking task during parse — likely related to the legacy "
                            "Bootstrap CSS still being shipped. Memory usage also spikes well "
                            "above the budget.",
                        ],
                    ),
                    (
                        "🧪 Environment",
                        [
                            ("bullet", "Safari 16.5 and 17.0 on iPhone 12 and iPhone 14."),
                            ("bullet", "4G network throttled in Safari dev tools."),
                            ("bullet", "Confirmed reproducible on staging and production."),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot1",
                        "Initial profiling points at the Bootstrap CSS bundle. Once we land the design-system migration this likely fixes itself, but we should confirm with a perf trace after the cutover.",
                    ),
                    (
                        "bot3",
                        "QA can rerun the trace on the staging build once the migration is in.",
                    ),
                ],
            },
            {
                "title": "Spike: framer-motion vs native CSS animations for the redesign",
                "status": "Open",
                "priority": "Low",
                "assignee": "bot2",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 9,
                "body": _body(
                    (
                        "❓ Question",
                        [
                            "Should the redesign use framer-motion for the hero / drawer / "
                            "modal transitions, or rely on CSS animations and Web Animations API?",
                        ],
                    ),
                    (
                        "💡 Hypothesis",
                        [
                            "framer-motion ergonomics are better, but its 40KB gzip footprint "
                            "competes directly with our marketing-page perf budget. CSS-only "
                            "may be sufficient for the animations we actually have planned.",
                        ],
                    ),
                    (
                        "🧭 Approach",
                        [
                            (
                                "bullet",
                                "Audit the planned animations: drawer slide, modal fade, hero stagger. Inventory whether each is reachable in pure CSS.",
                            ),
                            (
                                "bullet",
                                "Prototype the hardest one (hero stagger) with both approaches and compare ergonomics + bundle impact.",
                            ),
                            (
                                "bullet",
                                "Check what other teams shipping with @genos/design-system are doing — keep us aligned.",
                            ),
                        ],
                    ),
                    (
                        "⏱ Timebox",
                        [
                            "Two days. Decision needed before the nav PR lands since the "
                            "drawer animation is the first concrete usage.",
                        ],
                    ),
                    (
                        "📌 Findings",
                        [
                            ("placeholder", "Will write up after the prototype lands."),
                        ],
                    ),
                    (
                        "🚧 Out of scope",
                        [
                            (
                                "bullet",
                                "In-app animations (sidebar transitions, etc.) — separate decision; this spike is marketing-page-scoped.",
                            ),
                            ("bullet", "GSAP — already ruled out for licensing reasons."),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot2",
                        "Leaning toward CSS-only for marketing. The drawer is the only animation that's actually tricky in pure CSS, and Bob already has a Web Animations API prototype that handles it.",
                    ),
                    (
                        "bot1",
                        "Confirmed — my prototype was 800 bytes vs framer-motion's 40KB. For marketing pages, no contest.",
                    ),
                ],
            },
        ],
        "pm_messages": [
            (
                "demo",
                "Kicking off Website Redesign! Sprint 1 covers the marketing page migration and the responsive nav. Detailed plan in the milestone.",
            ),
            (
                "bot1",
                "I'll take responsive nav and the Plausible setup. Drawer prototype is already in PR #1284 if anyone wants to review.",
            ),
            (
                "bot2",
                "Hero illustration is handed off in Figma — “Marketing v3 hero”. Two variants. Engineering team, ping me if anything's unclear.",
            ),
            (
                "bot0",
                "Marketing wants to know: when can we tell them the launch date? They're holding press outreach.",
            ),
            (
                "demo",
                "Targeting end of Sprint 2 for a soft launch with the four marketing pages migrated. Public launch one sprint after that, contingent on the a11y audit clearing.",
            ),
            (
                "bot3",
                "I'll start the audit once FeatureGrid is in. Will block on the secondary-button contrast issue if it doesn't get fixed in tokens.",
            ),
            ("bot2", "Secondary-button contrast is in the v3.2 token release — landing today."),
            (
                "demo",
                "Great. Adding a checkpoint to the Sprint 1 demo: we should walk through the migrated About page and confirm the pattern before scaling to the others.",
            ),
            (
                "bot1",
                "Sounds good. I'll keep the About migration small enough to ship Friday so we have something to review.",
            ),
        ],
        "pm_thread": {
            "parent_index": 4,  # demo's "Targeting end of Sprint 2..." message
            "messages": [
                (
                    "bot0",
                    "Soft launch end of Sprint 2 works for marketing. Can you also flag the press date as a constraint in the milestone so it doesn't get lost?",
                ),
                (
                    "demo",
                    "Done — added to the Risks section. They want at least 5 business days between soft launch and public so they can pitch.",
                ),
                ("bot0", "Perfect. I'll lock that into the marketing calendar."),
                (
                    "bot1",
                    "On my side — if any of the four pages need rework after the soft launch a11y audit, that's the buffer week.",
                ),
            ],
        },
    },
    {
        "name": "Q2 Roadmap",
        "tags": [
            ("Planning", "#3b82f6", "#ffffff"),
            ("Research", "#10b981", "#ffffff"),
            ("Customer", "#f97316", "#ffffff"),
            ("Spec", "#8b5cf6", "#ffffff"),
        ],
        "milestone": {
            "title": "Q2 Discovery Sprint",
            "body": ROADMAP_MILESTONE_BODY,
            "status": "Open",
            "priority": "High",
            "due_offset_days": 24,
        },
        "tasks": [
            {
                "title": "Synthesize 12 customer interviews into themes",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot0",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 12,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Code the 12 customer interview recordings into themes in Dovetail. "
                            "Output is a set of ≤ 8 cluster summaries with verbatim quotes that "
                            "feed the roadmap proposal.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Without a coded synthesis we end up with a roadmap built on the "
                            "loudest interview rather than the most representative pattern. "
                            "Coding forces us to count signal frequency.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            ("bullet", "All 12 transcripts uploaded and tagged in Dovetail."),
                            (
                                "bullet",
                                "Themes coded with at least one supporting quote per theme.",
                            ),
                            ("bullet", "≤ 8 cluster summaries written, each ≤ 200 words."),
                            (
                                "bullet",
                                "Coverage flagged: which segments contributed to which themes (enterprise vs SMB).",
                            ),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Interview segments: 7 enterprise, 4 SMB, 1 prosumer. Skew is "
                            "intentional but limits SMB-only generalization. The coverage "
                            "flag in the summary should make this clear to leadership.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "Started coding. Early signal: notifications and search keep coming up in the same breath — people want “the assistant to surface what I missed.”",
                    ),
                    (
                        "demo",
                        "That's interesting. Worth a separate theme on its own — “proactive surfacing” vs reactive search. Let's not collapse them prematurely.",
                    ),
                ],
            },
            {
                "title": "Schedule remaining 5 interviews",
                "status": "Closed",
                "priority": "Normal",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 2,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Book the last 5 customer interviews — 2 enterprise admins, "
                            "2 SMB owners, 1 prosumer power user.",
                        ],
                    ),
                    (
                        "📌 Findings",
                        [
                            (
                                "bullet",
                                "All 5 booked. Calendly link, recording consent, and pre-read sent.",
                            ),
                            (
                                "bullet",
                                "One enterprise admin asked for a $100 thank-you gift card — added to ops queue.",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "All booked. Last one is Thursday afternoon. Recordings consent collected upfront so synthesis can move quickly.",
                    ),
                ],
            },
            {
                "title": "Code recordings into themes (Dovetail)",
                "status": "WIP",
                "priority": "High",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": 0,
                "due_offset_days": 10,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Use Dovetail's auto-transcription, then human-code each "
                            "transcript with the agreed taxonomy: jobs, pains, current "
                            "workarounds, deal-breakers.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            ("bullet", "All 12 transcripts coded with the four-axis taxonomy."),
                            (
                                "bullet",
                                "Cross-coder reliability check on 2 transcripts with demo as second coder.",
                            ),
                            ("bullet", "Code book finalized and shared with the team."),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot0",
                        "Coding 4 of 12 so far. The auto-transcription is decent but I'm spot-checking every other paragraph — accents and overlapping speech still trip it up.",
                    ),
                ],
            },
            {
                "title": "Competitor analysis: Notion, Slack, Linear",
                "status": "Open",
                "priority": "Normal",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 17,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Teardown of three competitor products across feature, pricing, "
                            "and onboarding surfaces. Output is a comparison matrix plus a "
                            "narrative on where we're behind, on par, or ahead.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "We're losing enterprise deals 40% of the time to Linear and 25% "
                            "of the time to Notion. Sales asks for explicit talking points; "
                            "without a teardown they fall back to ad-hoc comparisons that "
                            "miss recent competitor changes.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Feature matrix across the three competitors with 25-30 capability rows.",
                            ),
                            (
                                "bullet",
                                "Pricing matrix including per-seat, billing cadence, and enterprise minimums.",
                            ),
                            ("bullet", "Onboarding walkthrough screenshots for each."),
                            (
                                "bullet",
                                "Where-we're-behind narrative with a recommendation per gap.",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Linear shipped a Cycles → Sprints rename last month. Worth checking whether their underlying model changed or it's a name swap.",
                    ),
                    ("bot1", "From the changelog, looks like just a rename — model is unchanged."),
                ],
            },
            {
                "title": "Roadmap proposal v1 — top 5 bets for Q3",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 22,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Write the v1 roadmap proposal: five concrete bets for Q3 with "
                            "an expected outcome, evidence link, and rough effort estimate. "
                            "Optimized for leadership review and prioritization debate.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            ("bullet", "Five bets, each ≤ 300 words."),
                            (
                                "bullet",
                                "Each bet cites at least one interview quote AND one competitor data point.",
                            ),
                            (
                                "bullet",
                                "Rough sizing (S / M / L / XL) and lead-team identified per bet.",
                            ),
                            ("bullet", "Reviewed with leadership in a 60-minute working session."),
                        ],
                    ),
                    (
                        "🎯 Notes & links",
                        [
                            "Top candidate bets so far: (1) proactive surfacing assistant, "
                            "(2) sprint-board polish based on Linear teardown, (3) onboarding "
                            "overhaul, (4) admin / SSO maturity for enterprise, (5) public API. "
                            "Order will change as synthesis lands.",
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Holding the proposal draft until the synthesis lands so the evidence column isn't empty.",
                    ),
                    (
                        "bot0",
                        "Synthesis target: Thursday. Should give you the long weekend to draft.",
                    ),
                ],
            },
            {
                "title": "Onboarding funnel teardown",
                "status": "Pending",
                "priority": "Normal",
                "assignee": "bot3",
                "is_milestone_child": True,
                "parent_idx": None,
                "due_offset_days": 19,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Walk through our current onboarding funnel from signup to first "
                            "task created, instrumenting drop-off at each step. Output is a "
                            "Sankey diagram + recommendations for the top 3 drop-offs.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "We know overall day-1 activation is 34%, but we don't know where "
                            "the other 66% leak out. Without that step-level visibility, any "
                            "onboarding redesign is guesswork.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            (
                                "bullet",
                                "Funnel instrumented in Plausible (signup → team-create → first-project → first-task).",
                            ),
                            ("bullet", "Sankey diagram of two weeks of traffic."),
                            (
                                "bullet",
                                "Top 3 drop-off steps identified with recommended hypotheses to test.",
                            ),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "bot3",
                        "Need eng help to wire the Plausible custom events. Can we dovetail this with the marketing-page Plausible task?",
                    ),
                    ("bot1", "Yes, easy. I'll add the in-app events in the same PR."),
                ],
            },
            {
                "title": "Define Q3 OKRs draft",
                "status": "Open",
                "priority": "High",
                "assignee": "demo",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 26,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Draft Q3 OKRs anchored on the five bets emerging from the "
                            "roadmap proposal. One objective per bet, two key results each.",
                        ],
                    ),
                    (
                        "✅ Acceptance criteria",
                        [
                            ("bullet", "5 objectives, 10 key results total."),
                            ("bullet", "Each KR is measurable (number / percentage / binary)."),
                            ("bullet", "Reviewed with each lead-team before submission."),
                        ],
                    ),
                ),
                "comments": [
                    (
                        "demo",
                        "Draft will follow the roadmap proposal — pointless to commit OKRs before the bets are stable.",
                    ),
                ],
            },
            {
                "title": "Update internal stakeholder map",
                "status": "Open",
                "priority": "Low",
                "assignee": "bot0",
                "is_milestone_child": False,
                "parent_idx": None,
                "due_offset_days": 30,
                "body": _body(
                    (
                        "🧾 Summary",
                        [
                            "Refresh the internal stakeholder map: who needs visibility on "
                            "which bet, in what cadence. Stale since Q4 last year.",
                        ],
                    ),
                    (
                        "🪜 Motivation",
                        [
                            "Marketing, support, sales, and finance each need to be looped "
                            "into the Q3 bets at the right cadence and depth. Without a clean "
                            "map we either over-communicate (meeting overload) or under-communicate "
                            "(surprised stakeholders).",
                        ],
                    ),
                ),
                "comments": [
                    ("bot0", "Will take a pass after the roadmap proposal lands."),
                ],
            },
        ],
        "pm_messages": [
            (
                "demo",
                "Q2 Discovery kicking off. Alice, you're leading the interview synthesis — coverage is uneven so we should call that out in the summary.",
            ),
            (
                "bot0",
                "On it. 5 of 12 interviews still to schedule, but I have all calendly links going out today.",
            ),
            (
                "bot0",
                "Quick early signal from the first 4 transcripts: notifications and search keep coming up together. Worth a theme on its own.",
            ),
            (
                "demo",
                "Interesting. Hold judgement on whether it's one theme or two until at least 8 are coded — early-coder bias is real.",
            ),
            (
                "bot3",
                "On my side: onboarding teardown is gated on Plausible events. Coordinating with Bob to land them as part of the marketing analytics work.",
            ),
            ("bot1", "Will fold the in-app events into the same PR. Should land end of Sprint 1."),
            (
                "bot0",
                "Synthesis target: end of Sprint 1. Roadmap draft starts Sprint 2 once we have themes + competitor teardown in hand.",
            ),
            (
                "demo",
                "Working backward from leadership review: proposal needs to land in 3 weeks. Tight but doable if synthesis stays on track.",
            ),
            ("bot0", "Yep. Will flag immediately if any interview slips and pushes the timeline."),
        ],
        "pm_thread": {
            "parent_index": 2,  # bot0's "notifications and search" message
            "messages": [
                (
                    "demo",
                    "What's the verbatim phrasing? If they're saying “the assistant should tell me what I missed,” that's proactive surfacing, which is different from search.",
                ),
                (
                    "bot0",
                    "Couple of quotes: “I don't want to search — I want it to find me first” and “the worst part of my Monday is figuring out what I missed over the weekend.”",
                ),
                (
                    "demo",
                    "Strongly proactive surfacing then. Don't collapse it into search-improvements.",
                ),
                (
                    "bot0",
                    "Got it. I'll keep them as separate themes and let the synthesis show whether they hold up.",
                ),
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# DM blueprints — three multi-turn conversations + one thread each.
# Sender keys: "demo" = the demo user, "bot" = the bot in this DM.
# ---------------------------------------------------------------------------

DM_BLUEPRINTS = [
    {
        "bot_index": 0,  # Alice (PM)
        "messages": [
            ("demo", "Haha, joined"),
            ("bot", "Hey — welcome to the demo workspace. Want a quick tour of what's set up?"),
            ("demo", "Sure, what should I look at first?"),
            (
                "bot",
                "Two projects on the sidebar: Website Redesign (active build) and Q2 Roadmap (planning). Sprint 1 is live in both.",
            ),
            ("demo", "Got it. What's the biggest open question right now?"),
            (
                "bot",
                "Whether the Q3 roadmap should bet on proactive surfacing (assistant tells you what you missed) or just better search. Interview signal is leaning proactive but we want more transcripts coded first.",
            ),
            ("demo", "And what's the call if synthesis lands ambiguous?"),
            (
                "bot",
                "Default to the smaller bet — search improvements — and queue proactive as a follow-on Q4 prototype. Don't want to over-commit on weak evidence.",
            ),
            ("demo", "Makes sense. What's blocking the proposal right now?"),
            (
                "bot",
                "Synthesis. Coding is at 4 of 12, target is end of Sprint 1. Once that's done I can draft inside a week.",
            ),
            ("demo", "Cool — ping me if anything slips, I can help unblock."),
        ],
        "thread": {
            "parent_index": 5,
            "messages": [
                (
                    "bot",
                    "Whether the Q3 roadmap should bet on proactive surfacing (assistant tells you what you missed) or just better search. Interview signal is leaning proactive but we want more transcripts coded first.",
                ),
                (
                    "demo",
                    "When you say “proactive surfacing,” are we talking about a daily digest, or something that interrupts in-stream?",
                ),
                (
                    "bot",
                    "Closer to a daily digest, I think. The interview phrasing is “Monday morning catchup,” not “interrupt me while I'm focused.”",
                ),
                (
                    "demo",
                    "OK that's much easier to scope. Notification fatigue concerns get a lot smaller.",
                ),
                (
                    "bot",
                    "Right — and it dovetails with the spotlight Cmd-K we're already shipping. Same underlying retrieval, different surface.",
                ),
            ],
        },
    },
    {
        "bot_index": 1,  # Bob (Engineer)
        "messages": [
            ("demo", "Haha, joined"),
            (
                "bot",
                "Heads up — I'm pushing the responsive nav PR today. Drawer animation is in pure CSS so we don't carry framer-motion.",
            ),
            ("demo", "Nice. What ruled out framer-motion in the end?"),
            (
                "bot",
                "Bundle size mainly. 40KB gzip for the marketing page is too steep for the perf budget we agreed on. Web Animations API handles the drawer fine.",
            ),
            ("demo", "Any other animations coming up that might force the issue?"),
            (
                "bot",
                "Modal fade and hero stagger. Both are doable in CSS — I'll have prototypes by end of week.",
            ),
            ("demo", "What about in-app animations? Different decision?"),
            (
                "bot",
                "Probably yes. The marketing pages have a perf budget; the app doesn't have the same constraint. We can revisit framer-motion for in-app if the ergonomics savings are real.",
            ),
            ("demo", "Track that as a separate spike so we don't conflate the two."),
            (
                "bot",
                "Will do. Filing it in the Website Redesign project for now, but it really belongs in the in-app project once that exists.",
            ),
            ("demo", "Good catch. Let's wait on that spike until after launch — no rush."),
        ],
        "thread": {
            "parent_index": 3,
            "messages": [
                (
                    "bot",
                    "Bundle size mainly. 40KB gzip for the marketing page is too steep for the perf budget we agreed on. Web Animations API handles the drawer fine.",
                ),
                (
                    "demo",
                    "What's the actual perf budget number we agreed on? Want to make sure we hold the line.",
                ),
                (
                    "bot",
                    "Lighthouse mobile ≥ 95 and bundle JS ≤ 120KB gzip on the marketing pages.",
                ),
                (
                    "demo",
                    "Cool. The 40KB for framer-motion would have eaten a third of that budget alone. Easy decision.",
                ),
                (
                    "bot",
                    "Yep. CSS-only also means SSR is trivial — no hydration mismatch worries.",
                ),
            ],
        },
    },
    {
        "bot_index": 2,  # Carol (Designer)
        "messages": [
            ("demo", "Haha, joined"),
            (
                "bot",
                "Hero illustration is up in Figma — file “Marketing v3 hero”. Two variants: gradient and photo.",
            ),
            ("demo", "Which one are you leaning toward?"),
            (
                "bot",
                "Gradient. Photo loads slower and we lose flexibility for the A/B test queued in v1.1.",
            ),
            (
                "demo",
                "Strong argument. Anything else I should know before the homepage rebuild lands?",
            ),
            (
                "bot",
                "Secondary-button contrast is at 4.6:1 in the v3.2 tokens — clears the audit. The old 3.8:1 was a layering bug, not a token issue.",
            ),
            ("demo", "Good to know. So the a11y audit shouldn't surface that anymore?"),
            (
                "bot",
                "Right. The other Critical violation — missing landmark roles — is a markup fix, not a design one. Bob is handling it as part of the nav rebuild.",
            ),
            ("demo", "Anything blocking on your side?"),
            (
                "bot",
                "Pricing-table component still doesn't have a spec. I need to know how many tiers + the toggle behavior before I can lock the layout.",
            ),
            (
                "demo",
                "Marketing said 3 tiers, monthly / annual toggle. I'll get you the copy by Thursday.",
            ),
        ],
        "thread": {
            "parent_index": 5,
            "messages": [
                (
                    "bot",
                    "Secondary-button contrast is at 4.6:1 in the v3.2 tokens — clears the audit. The old 3.8:1 was a layering bug, not a token issue.",
                ),
                ("demo", "Wait — how was 3.8 vs 4.6 a layering issue rather than a token one?"),
                (
                    "bot",
                    "We were stacking a 50% opacity overlay on top of the secondary button in hovers, which dropped the effective contrast. The token itself was always fine.",
                ),
                (
                    "demo",
                    "Got it. So as long as we don't reintroduce the opacity overlay, we're good. Worth a Storybook story to prevent regression.",
                ),
                ("bot", "Already added one — “SecondaryButton / Hover / Contrast”."),
            ],
        },
    },
]


# ---------------------------------------------------------------------------
# Group chat (GM) blueprint — team-wide standup chatter + decisions.
# Sender keys reference indices into all_members (0=demo, 1..4 = bots).
# ---------------------------------------------------------------------------

GM_BLUEPRINT = {
    "messages": [
        (0, "GM created."),
        (
            0,
            "Morning team — quick standup thread. Going to drop my updates here, please add yours below.",
        ),
        (
            0,
            "Working on the roadmap proposal outline. Blocked on customer interview synthesis (Alice).",
        ),
        (
            1,
            "synthesis is at 4 of 12 transcripts coded. Expect end of Sprint 1. Will flag if any interview slips.",
        ),
        (
            2,
            "responsive nav PR is up. Drawer animation in pure CSS, no framer-motion. Reviewers welcome.",
        ),
        (
            3,
            "hero illustration is in Figma — “Marketing v3 hero”. Recommend the gradient variant for perf and A/B flexibility.",
        ),
        (
            4,
            "blocked on FeatureGrid PR landing so I can start the a11y audit. Will tee up the test plan in the meantime.",
        ),
        (
            1,
            "also — interview signal so far suggests proactive surfacing is a stronger Q3 bet than I expected. Will write up after coding wraps.",
        ),
        (
            0,
            "Thanks all. Two decisions we should make this week: (1) which hero variant to ship, (2) whether to scope proactive surfacing for Q3 or queue it for Q4.",
        ),
        (
            3,
            "re hero — strong vote for gradient. Smaller bundle, more flexible for A/B testing copy.",
        ),
        (2, "agree on gradient — photo asset would push us off the perf budget."),
        (
            1,
            "on the proactive-surfacing question, can we hold the call until we have ≥ 8 transcripts coded? Don't want to commit on a 4-transcript signal.",
        ),
        (
            0,
            "Agreed — decision deferred to next Friday. By then synthesis should be at 10+ transcripts.",
        ),
        (
            4,
            "parking-lot question — are we doing keyboard-shortcut docs for the launch? Spotlight Cmd-K is reachable but nothing tells users it exists.",
        ),
        (
            0,
            "Good catch Dan. I'll add a “What's new” modal for the launch that highlights Cmd-K and the redesign. Carving a task out of this thread.",
        ),
    ],
    "thread": {
        "parent_index": 13,  # Dan's keyboard-shortcuts question
        "messages": [
            (
                4,
                "parking-lot question — are we doing keyboard-shortcut docs for the launch? Spotlight Cmd-K is reachable but nothing tells users it exists.",
            ),
            (
                1,
                "also worth mentioning that Cmd-K isn't just navigation anymore — the AI side of it answers questions across chats, tasks, and notes. People miss that.",
            ),
            (
                4,
                "yes! When I demoed it to my friend at TechRising they had no idea it could summarize a thread.",
            ),
            (
                0,
                "Good — the “What's new” modal should lead with the AI side then. Carol, can we get a tiny illustration showing the Cmd-K shortcut with an answer streaming back?",
            ),
            (3, "yes — I'll mock that up tomorrow."),
            (2, "I can wire the modal in with the launch banner. One PR, both surfaces."),
        ],
    },
}


# ---------------------------------------------------------------------------
# Note blueprints — rich content that gives Spotlight AI substance.
# ---------------------------------------------------------------------------

NOTE_WELCOME_BODY = _body(
    (
        "👋 Welcome to your demo workspace",
        [
            "This is a fully populated example of how the app looks in practice — "
            "two projects, a milestone each, real tasks with sub-tasks, threaded "
            "chats, and a few hand-written notes. Everything here is searchable.",
        ],
    ),
    (
        "🧭 Where to start",
        [
            (
                "bullet",
                "Hit Cmd-K (or Ctrl-K on Windows / Linux) to open Spotlight — the AI assistant searches every chat, task, and note in this workspace.",
            ),
            (
                "bullet",
                "Open the Tasks tab to see the sprint board, milestones, and the per-task notes feature.",
            ),
            (
                "bullet",
                "Chat sidebar shows DMs with three bot peers (Alice, Bob, Carol) plus the team's general channel.",
            ),
        ],
    ),
    (
        "🤖 Try Spotlight with these prompts",
        [
            ("bullet", "“What's the perf budget for the marketing site?”"),
            ("bullet", "“Why did we rule out framer-motion?”"),
            ("bullet", "“Summarize the customer interview signal so far.”"),
            ("bullet", "“What's blocking the roadmap proposal?”"),
        ],
    ),
    (
        "⏳ A note on persistence",
        [
            "Your demo data is wiped automatically when you sign out, and at most "
            "24 hours after your demo session was created. Nothing here connects "
            "to any other user or team.",
        ],
    ),
)

NOTE_SPOTLIGHT_TIPS_BODY = _body(
    (
        "🤖 How to get the most out of Spotlight (Cmd-K)",
        [
            "Spotlight is the AI search and Q&A overlay. It's not just a fuzzy file "
            "finder — it indexes every chat message, task description, comment, and "
            "note in this workspace, then routes natural-language questions through "
            "an LLM that cites the actual sources it pulled from.",
        ],
    ),
    (
        "✨ Patterns that work well",
        [
            (
                "bullet",
                "Ask about decisions: “Why did we choose CSS animations over framer-motion?” — the answer pulls Bob's DM and the spike task.",
            ),
            (
                "bullet",
                "Ask about state: “What's blocking the homepage rebuild?” — pulls task statuses + comment threads.",
            ),
            (
                "bullet",
                "Ask for summaries: “Summarize the customer interview signal” — pulls Alice's DMs, the PM channel thread, and the synthesis task.",
            ),
            (
                "bullet",
                "Follow up: each answer keeps conversation context, so you can ask “and what's the next step?” and it knows what “the next step” refers to.",
            ),
        ],
    ),
    (
        "🎯 Patterns that don't work well",
        [
            (
                "bullet",
                "Asking about things outside this workspace (other companies, the open web, current events) — Spotlight only knows what's in here.",
            ),
            (
                "bullet",
                "Math, code generation, or anything that doesn't reduce to “retrieve and summarize.”",
            ),
        ],
    ),
    (
        "⌨️ Shortcuts",
        [
            ("bullet", "Cmd-K / Ctrl-K — open Spotlight."),
            ("bullet", "Escape — close it (your conversation survives so you can resume)."),
            (
                "bullet",
                "Enter — submit the question for AI; arrow keys navigate the live search results above.",
            ),
        ],
    ),
)

NOTE_WEEKLY_PRIORITIES_BODY = _body(
    (
        "🎯 This week's focus",
        [
            "Three things, in order. Everything else is a distraction.",
        ],
    ),
    (
        "1. Land the responsive nav",
        [
            ("bullet", "PR #1284 review by Wednesday."),
            ("bullet", "320px breakpoint regression test in Storybook."),
            ("bullet", "Drawer focus trap tested on screen reader (Dan)."),
        ],
    ),
    (
        "2. Unblock the customer-interview synthesis",
        [
            ("bullet", "Pair with Alice on cross-coder reliability for 2 transcripts."),
            (
                "bullet",
                "Confirm the “proactive surfacing vs search” split before she codes the rest.",
            ),
        ],
    ),
    (
        "3. Draft the roadmap proposal scaffolding",
        [
            (
                "bullet",
                "Even with placeholder evidence — get the document skeleton in place so we're filling sections, not starting from scratch.",
            ),
            ("bullet", "Goal: leadership review in 3 weeks."),
        ],
    ),
    (
        "⏸ Explicitly deferred",
        [
            ("bullet", "Q3 OKR draft — waits on roadmap proposal."),
            ("bullet", "Onboarding funnel teardown — Plausible events ship first."),
            ("bullet", "In-app animation framework decision — post-launch."),
        ],
    ),
)

NOTE_DESIGN_SYSTEM_INVENTORY_BODY = _body(
    (
        "📚 Design system inventory (v3.2)",
        [
            "Snapshot of which @genos/design-system primitives are wired up and "
            "where they're used. Maintained manually for now — pulling this into "
            "Storybook docgen is a Q3 candidate.",
        ],
    ),
    (
        "✅ Stable primitives (safe to use everywhere)",
        [
            ("bullet", "Stack, Grid, Box — layout primitives, container-query-aware."),
            ("bullet", "Button, IconButton — secondary-button contrast fixed in v3.2."),
            (
                "bullet",
                "Input, Textarea, Select — form primitives with built-in error / hint slots.",
            ),
            ("bullet", "Heading, Text — typography with semantic levels."),
            ("bullet", "Modal, Drawer — focus trap + escape handling built in."),
        ],
    ),
    (
        "🚧 Experimental (use with eyes open)",
        [
            (
                "bullet",
                "Hero — exists but the “media” prop is still in flux. Lock to v3.2 if you depend on it.",
            ),
            (
                "bullet",
                "FeatureGrid — container-query breakpoints not finalized for iPad landscape.",
            ),
            (
                "bullet",
                "PricingTable — does not exist yet. Carol is designing; spec pending marketing's tier confirmation.",
            ),
        ],
    ),
    (
        "📦 Bundle impact",
        [
            (
                "bullet",
                "Full design-system import: ~78KB gzip (vs Bootstrap 4 at ~52KB but with no JS).",
            ),
            ("bullet", "Tree-shaking works — Hero alone is ~6KB."),
            (
                "bullet",
                "Container-query polyfill is ~3KB additional; only loaded on browsers that need it.",
            ),
        ],
    ),
    (
        "🔗 Where to find more",
        [
            ("bullet", "Storybook: storybook.genos.app/design-system"),
            ("bullet", "Token reference: design-tokens.genos.app"),
            ("bullet", "Slack channel: #design-system for questions"),
        ],
    ),
)

TASK_NOTE_SYNTHESIS_METHODOLOGY_BODY = _body(
    (
        "📚 Customer interview synthesis — methodology",
        [
            "Documenting the approach so future synthesis rounds reproduce it. Took "
            "two iterations last quarter to land on this; the cost is upfront but "
            "the output quality is meaningfully higher.",
        ],
    ),
    (
        "1. Transcribe + clean",
        [
            (
                "bullet",
                "Dovetail auto-transcription, then a human pass on every 5th paragraph (accents and overlapping speech are unreliable).",
            ),
            (
                "bullet",
                "Strip filler words (“um”, “like”, “you know”) — they get in the way of theme detection.",
            ),
            (
                "bullet",
                "Anonymize per the consent form. Replace company names with their industry vertical.",
            ),
        ],
    ),
    (
        "2. Code with the four-axis taxonomy",
        [
            ("bullet", "Jobs — what is the user trying to accomplish?"),
            ("bullet", "Pains — what hurts in their current workflow?"),
            ("bullet", "Workarounds — what have they hacked together to cope?"),
            ("bullet", "Deal-breakers — what would make them switch products?"),
        ],
    ),
    (
        "3. Cluster and write",
        [
            ("bullet", "Group codes into themes — aim for ≤ 8 to keep cognitive load manageable."),
            (
                "bullet",
                "Each theme gets a verbatim quote + a count of how many segments contributed.",
            ),
            (
                "bullet",
                "Flag coverage explicitly: enterprise vs SMB vs prosumer. Without this leadership over-indexes on the loudest segment.",
            ),
        ],
    ),
    (
        "4. Cross-coder reliability check",
        [
            (
                "bullet",
                "Two coders independently code 2 transcripts. If agreement < 80% on themes, re-examine the taxonomy.",
            ),
            (
                "bullet",
                "Document disagreements — the disagreement is itself a signal about ambiguous themes.",
            ),
        ],
    ),
    (
        "📝 Common pitfalls",
        [
            (
                "bullet",
                "Collapsing themes too early — “notifications + search” is two themes, not one, until the evidence proves otherwise.",
            ),
            (
                "bullet",
                "Cherry-picking quotes — every theme needs a count, not just a representative quote.",
            ),
            (
                "bullet",
                "Recency bias — coding the latest interviews first inflates their weight. Code in interview order.",
            ),
        ],
    ),
)

CHAT_NOTE_GM_KICKOFF_RECAP_BODY = _body(
    (
        "📝 Standup recap — Sprint 1 kickoff",
        [
            "Captured from the team standup chat. Posting here so the decisions and "
            "blockers are easy to find later without re-reading the whole thread.",
        ],
    ),
    (
        "✅ Decisions",
        [
            (
                "bullet",
                "Hero illustration: ship the gradient variant. Reasons: better perf, more A/B flexibility for the v1.1 copy test.",
            ),
            (
                "bullet",
                "Proactive surfacing vs search: decision deferred. Revisit after ≥ 8 customer interview transcripts are coded (target: next Friday).",
            ),
            (
                "bullet",
                "Animation framework for marketing pages: CSS-only. framer-motion ruled out on bundle-size grounds.",
            ),
            (
                "bullet",
                "“What's new” launch modal: yes. Will highlight Cmd-K and the redesign. Carol owns the illustration, Bob wires the modal.",
            ),
        ],
    ),
    (
        "🚧 Blockers and risks",
        [
            (
                "bullet",
                "Dan can't start the a11y audit until the FeatureGrid PR lands. Watch for delay.",
            ),
            (
                "bullet",
                "Synthesis is on the critical path for the roadmap proposal. If it slips past end of Sprint 1, the proposal slips too.",
            ),
            (
                "bullet",
                "Pricing component design is gated on marketing's tier-copy confirmation by Thursday.",
            ),
        ],
    ),
    (
        "📅 Next checkpoints",
        [
            ("bullet", "Friday — End-of-week demo. About-page migration walkthrough."),
            (
                "bullet",
                "Next Friday — Decision on proactive-surfacing vs search (with 8+ transcripts coded).",
            ),
            (
                "bullet",
                "End of Sprint 1 — Marketing PR train and the Plausible event PR both landed.",
            ),
        ],
    ),
)


# ---------------------------------------------------------------------------
# Seeder entry point
# ---------------------------------------------------------------------------


def create_demo_environment(demo_user: CustomUser, *, short: str | None = None) -> dict:
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
    """
    if short is None:
        short = uuid.uuid4().hex[:8]

    with transaction.atomic():
        # 1. Bot peer users
        bots = _create_bot_users(short)

        # 2. Team
        team = TeamMaster.objects.create(
            team_name=f"Demo Team {short}",
            team_email=f"demo-team-{short}@genos.app",
            owner=demo_user,
            is_demo=True,
        )

        # 3. Team members (demo user + bots)
        all_members = [demo_user] + bots
        TeamMembers.objects.bulk_create([TeamMembers(team=team, attendee=u) for u in all_members])

        # 4. Notification preferences (one per user)
        NotificationPreference.objects.bulk_create(
            [NotificationPreference(user=u) for u in all_members]
        )

        # 5-10. Projects + sprints + milestones + tasks (per blueprint)
        seeded_projects = []
        for blueprint in PROJECT_BLUEPRINTS:
            seeded = _create_project_from_blueprint(
                team, demo_user, all_members, bots, short, blueprint
            )
            seeded_projects.append(seeded)

        # 11. DMs + thread messages
        _create_dms(team, demo_user, bots)

        # 12. Group chat + thread messages
        gm = _create_group_chat(team, demo_user, all_members)

        # 13. Project channel messages + thread messages
        for seeded in seeded_projects:
            _create_pm_messages(seeded["project"], all_members, seeded["blueprint"])

        # 14. UserChatMaster per (user, team)
        UserChatMaster.objects.bulk_create(
            [
                UserChatMaster(team=team, user=u, flagged_messages=[], pinned_chats=[])
                for u in all_members
            ]
        )

        # 15. Notes (personal + task + chat note with permissions)
        _create_notes(team, demo_user, bots, seeded_projects, gm)

        # 16. Todos (demo user only — todos are personal-scoped)
        _create_todos(team, demo_user)

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


def _create_bot_users(short: str) -> List[CustomUser]:
    bots: List[CustomUser] = []
    for profile in BOT_PROFILES:
        email = f"demo-bot-{short}-{profile['first'].lower()}@genos.app"
        bot = CustomUser(
            email=email,
            username=f"{profile['first']} {profile['last']}",
            role=profile["role"],
            base_country="USA",
            is_demo=True,
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


def _create_project_from_blueprint(team, demo_user, all_members, bots, short, blueprint):
    """Create one project, its tags, sprint, milestone (with backing
    task), and all tasks + subtasks + comments. Returns
    `{"project": project, "blueprint": blueprint, "tasks": [TaskMaster, ...]}`."""
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

    ProjectMembers.objects.bulk_create(
        [ProjectMembers(team=team, project=project, attendee=u) for u in all_members]
    )

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

    sprint = _create_sprint(team, project)
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

    return {"project": project, "blueprint": blueprint, "tasks": tasks}


def _create_sprint(team, project) -> Sprint:
    today = date.today()
    SprintConfig.objects.create(
        team=team,
        project=project,
        duration_days=14,
        anchor_date=today,
        auto_roll=False,
    )
    return Sprint.objects.create(
        team=team,
        project=project,
        name="Sprint 1",
        sequence_number=1,
        start_date=today,
        end_date=today + timedelta(days=13),
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
            MilestoneAssignees(team=team, milestone=milestone, user=bots[0]),
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


def _create_dms(team, demo_user, bots):
    """Three multi-turn DMs (demo↔Alice, ↔Bob, ↔Carol), each with one
    thread on a representative message. Plus a self-DM that matches
    what /jointeam's `moveToTeam` would have created."""
    for spec in DM_BLUEPRINTS:
        bot = bots[spec["bot_index"]]
        dm = DMMaster(team=team, user_1_id=demo_user.id, user_2_id=bot.id)
        # DMMaster.save() auto-creates UserDMMapping rows.
        dm.save()

        thread_spec = spec.get("thread")
        thread_parent_idx = thread_spec["parent_index"] if thread_spec else None
        # The CHILD `DMThreadMessages.thread_id` carries the parent's
        # `message_id` (see pm_views.py:428 — the reply-count API joins
        # on this). The parent `DMMessages.thread_id` is NOT set by the
        # real frontend; reply counts come from a JOIN on the children
        # table (see dm_views.py:208-212), not from the parent's field.
        thread_id_value = thread_parent_idx + 1 if thread_parent_idx is not None else None

        created_msgs = []
        for midx, (who, text) in enumerate(spec["messages"]):
            sender = bot if who == "bot" else demo_user
            receiver = demo_user if who == "bot" else bot
            msg = DMMessages.objects.create(
                dm=dm,
                sender=sender,
                receiver=receiver,
                message_id=midx + 1,
                message_body=_text_body(text),
            )
            created_msgs.append(msg)

        if thread_spec:
            parent_msg = created_msgs[thread_parent_idx]
            for tidx, (who, text) in enumerate(thread_spec["messages"]):
                sender = bot if who == "bot" else demo_user
                receiver = demo_user if who == "bot" else bot
                DMThreadMessages.objects.create(
                    dm=dm,
                    thread_id=thread_id_value,
                    sender=sender,
                    receiver=receiver,
                    thread_message_id=tidx + 1,
                    thread_message_body=_text_body(text),
                    parent_message_uid=parent_msg,
                )

    # Self-DM (personal scratch chat). Mirrors what `moveToTeam` creates
    # on first team entry — without it the workspace lands missing the
    # user's expected default chat. Seeded with several "notes to self"
    # messages so the chat actually loads when Spotlight routes a todo
    # source-chip click to it (an empty chat short-circuits the loader
    # and the user ends up on the inbox fallback).
    self_dm = DMMaster(team=team, user_1_id=demo_user.id, user_2_id=demo_user.id)
    self_dm.save()
    self_dm_messages = [
        "Welcome — this is your personal scratch chat. Drop quick thoughts, links, "
        "and reminders here. Press the ✓ icon in the header to toggle your todo "
        "list (the same data the Spotlight agent reads via `list_today_todos`).",
        "Spotlight tip: Cmd-K → \"what's on my todo list today?\" reads from the "
        "ToDoPane on the right. Try \"add a todo: …\" for an approval-gated write.",
        "Reminder — review Bob's framer-motion vs CSS spike notes before approving "
        "the responsive nav merge. The perf budget literals (Lighthouse 95, JS "
        "120KB) live in the responsive-nav task body.",
        "Q3 bets draft is on Alice's desk. She wants a yes/no on the proactive "
        "surfacing vs search angle by EOW — the customer interview synthesis "
        "supports both, so the decision is about energy not evidence.",
        "Plausible dashboard URL: see the *Set up Plausible events tracker* todo "
        "from 3 days ago for the events spec.",
        "Carol's hero illustration v3 is in Figma. Need to pick a final variant, "
        "wire it into the BlockNote intro section, and confirm alt text with "
        "Alice. Subitems on today's todo track this.",
    ]
    for midx, text in enumerate(self_dm_messages):
        DMMessages.objects.create(
            dm=self_dm,
            sender=demo_user,
            receiver=demo_user,
            message_id=midx + 1,
            message_body=_text_body(text),
        )


def _create_group_chat(team, demo_user, members) -> GMMaster:
    gm = GMMaster.objects.create(
        group_name=f"general · {team.team_name}",
        owner_user=demo_user,
        owner_team=team,
    )
    GMMembers.objects.bulk_create([GMMembers(gm=gm, attendee=u) for u in members])

    thread_parent_idx = GM_BLUEPRINT["thread"]["parent_index"]
    # Thread id matches the parent message's message_id by convention.
    # Stored on the CHILD `GMThreadMessages.thread_id` only — the real
    # frontend leaves `GMMessages.thread_id` NULL on the parent.
    thread_id_value = thread_parent_idx + 1

    created_msgs = []
    for midx, (sender_idx, text) in enumerate(GM_BLUEPRINT["messages"]):
        msg = GMMessages.objects.create(
            gm=gm,
            sender=members[sender_idx],
            message_id=midx + 1,
            message_body=_text_body(text),
        )
        created_msgs.append(msg)

    parent_msg = created_msgs[thread_parent_idx]
    for tidx, (sender_idx, text) in enumerate(GM_BLUEPRINT["thread"]["messages"]):
        GMThreadMessages.objects.create(
            gm=gm,
            thread_id=thread_id_value,
            sender=members[sender_idx],
            thread_message_id=tidx + 1,
            thread_message_body=_text_body(text),
            parent_message_uid=parent_msg,
        )

    return gm


def _create_pm_messages(project, members, blueprint):
    """Per-project channel: messages + one thread, all driven by the
    project's blueprint. Sender keys here are `(member_index, text)`-
    style indices into `members` (0 = demo_user, 1-4 = bots)."""
    pm_messages = blueprint["pm_messages"]
    thread_spec = blueprint["pm_thread"]
    thread_parent_idx = thread_spec["parent_index"]
    # Thread id matches the parent message's message_id by convention.
    thread_id_value = thread_parent_idx + 1

    # Map blueprint sender keys to members[] indices: "demo" -> 0,
    # "bot0" -> 1, etc.
    def resolve_idx(key: str) -> int:
        if key == "demo":
            return 0
        return int(key[3:]) + 1

    created_msgs = []
    for midx, (sender_key, text) in enumerate(pm_messages):
        sender = members[resolve_idx(sender_key)]
        # Parent `PMMessages.thread_id` is NOT set — see DM helper for
        # why (the real frontend leaves it NULL; reply counts come from
        # a JOIN on PMThreadMessages, not from the parent column).
        msg = PMMessages.objects.create(
            project=project,
            sender=sender,
            message_id=midx + 1,
            message_body=_text_body(text),
        )
        created_msgs.append(msg)

    parent_msg = created_msgs[thread_parent_idx]
    for tidx, (sender_key, text) in enumerate(thread_spec["messages"]):
        sender = members[resolve_idx(sender_key)]
        PMThreadMessages.objects.create(
            project=project,
            thread_id=thread_id_value,
            sender=sender,
            thread_message_id=tidx + 1,
            thread_message_body=_text_body(text),
            parent_message_uid=parent_msg,
        )


def _create_notes(team, demo_user, bots, seeded_projects, gm):
    """Rich personal + task + chat notes. Each note gets an explicit
    NotePermissionMaster ROLE_OWNER row (the note APIs 403 without
    one, even for the owner). Note types: 1=Personal, 2=Task, 3=Chat.
    """
    permissions: list[NotePermissionMaster] = []

    personal_specs = [
        ("Welcome to your demo workspace", NOTE_WELCOME_BODY),
        ("How to get the most out of Spotlight (Cmd-K)", NOTE_SPOTLIGHT_TIPS_BODY),
        ("This week's priorities", NOTE_WEEKLY_PRIORITIES_BODY),
        ("Design system inventory (v3.2)", NOTE_DESIGN_SYSTEM_INVENTORY_BODY),
    ]
    for title, body in personal_specs:
        note = PersonalNoteMaster.objects.create(
            team=team,
            owner=demo_user,
            title=title,
            body=body,
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
        title="Customer interview synthesis methodology",
        body=TASK_NOTE_SYNTHESIS_METHODOLOGY_BODY,
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

    # Chat note on the GM kickoff thread — owner + editors so the bots
    # can see it too. Role IDs: 1=owner, 2=editor, 3=viewer.
    chat_note = ChatNoteMaster.objects.create(
        team=team,
        owner=demo_user,
        chat_type=2,  # GM
        chat_id=gm.gm_id,
        is_thread=False,
        thread_id=0,
        title="Sprint 1 kickoff recap",
        body=CHAT_NOTE_GM_KICKOFF_RECAP_BODY,
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


def _create_todos(team, demo_user):
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
    """
    today = timezone.localdate()

    # Tags. Two project-aligned + a generic "Personal" lane. Sort order
    # mirrors the project order in PROJECT_BLUEPRINTS so the pane reads
    # left-to-right like the Spotlight sidebar.
    cat_web = ToDoCategory.objects.create(
        team=team, user=demo_user, name="Website Redesign", sort_order=0
    )
    cat_q2 = ToDoCategory.objects.create(
        team=team, user=demo_user, name="Q2 Roadmap", sort_order=1
    )
    cat_personal = ToDoCategory.objects.create(
        team=team, user=demo_user, name="Personal", sort_order=2
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
        title="Ship homepage hero illustration handoff",
        notes=_text_body(
            "Carol delivered v3 of the hero SVG yesterday. Need to pick a final variant, "
            "wire it into the BlockNote intro section, and confirm the alt text with Alice."
        ),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        parent_item=parent_hero,
        title="Pick final SVG variant from Carol's v3 batch",
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        parent_item=parent_hero,
        title="Wire hero into the BlockNote intro section",
        sort_order=1,
    )

    ToDoItem.objects.create(
        group=grp_today,
        category=cat_web,
        title="Review framer-motion vs CSS animations spike notes",
        notes=_text_body(
            "Bob filed his spike summary in the responsive nav thread. Read it, sign off "
            "on the recommendation, and link the decision into the Q2 roadmap doc."
        ),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_q2,
        title="Synthesize the remaining 3 customer interviews in Dovetail",
        is_completed=True,
        ts_completed_at=timezone.now(),
        sort_order=2,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_q2,
        title="Reply to Alice about the Q3 bets draft",
        notes=_text_body("She wants a yes/no on the proactive surfacing vs search angle."),
        sort_order=3,
    )
    ToDoItem.objects.create(
        group=grp_today,
        category=cat_personal,
        title="Sign offer letter for the new QA hire",
        sort_order=4,
    )
    ToDoItem.objects.create(
        group=grp_today,
        title="Drink 2L water before EOD",
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
        title="Confirm 320px Storybook stories for responsive nav",
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=18),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_web,
        title="Approve perf budget thresholds with Bob (Lighthouse 95 / JS 120KB)",
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=22),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_q2,
        title="Draft customer interview synthesis outline",
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(hours=24),
        sort_order=2,
    )
    # Carryover: shows up in `list_uncompleted_todos` for the past week.
    ToDoItem.objects.create(
        group=grp_yesterday,
        category=cat_q2,
        title="File spike ticket for in-app animation framework",
        notes=_text_body(
            "Bob and I agreed to defer the in-app animation decision until after launch. "
            "Spike ticket should track the post-launch evaluation."
        ),
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
        title="Set up Plausible events tracker on the marketing site",
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(days=3, hours=2),
        sort_order=0,
    )
    ToDoItem.objects.create(
        group=grp_old,
        category=cat_q2,
        title="Run sprint kickoff with Alice + Bob",
        is_completed=True,
        ts_completed_at=timezone.now() - timedelta(days=3, hours=5),
        sort_order=1,
    )
    ToDoItem.objects.create(
        group=grp_old,
        category=cat_q2,
        title="Review Q2 milestones in the roadmap doc",
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

    with transaction.atomic():
        # Pre-collect parent IDs once so children can be filtered by
        # them without triggering N+1 lookups.
        dm_ids = list(DMMaster.objects.filter(team=team_id).values_list("dm_id", flat=True))
        gm_ids = list(GMMaster.objects.filter(owner_team=team_id).values_list("gm_id", flat=True))
        mdm_ids = list(
            MDMMaster.objects.filter(owner_team=team_id).values_list("mdm_id", flat=True)
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
        # filter explicitly to avoid leaking rows.
        for chat_type, chat_ids in [(1, dm_ids), (2, gm_ids), (3, project_ids)]:
            if not chat_ids:
                continue
            MentionFact.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()
            ReactionFact.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()
            ChatAttachmentFact.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()
            ReadStatus.objects.filter(chat_type=chat_type, chat_id__in=chat_ids).delete()

        # ActivityFact and its read-status are team-scoped via a proper FK.
        ActivityReadStatus.objects.filter(team=team_id).delete()
        ActivityFact.objects.filter(team=team_id).delete()

        # DM tree
        DMThreadMessages.objects.filter(dm_id__in=dm_ids).delete()
        DMMessages.objects.filter(dm_id__in=dm_ids).delete()
        UserDMMapping.objects.filter(team_id=team_id).delete()
        DMMaster.objects.filter(team=team_id).delete()

        # GM tree
        GMThreadMessages.objects.filter(gm_id__in=gm_ids).delete()
        GMMessages.objects.filter(gm_id__in=gm_ids).delete()
        GMMembers.objects.filter(gm_id__in=gm_ids).delete()
        GMMaster.objects.filter(owner_team=team_id).delete()

        # MDM tree
        MDMThreadMessages.objects.filter(mdm_id__in=mdm_ids).delete()
        MDMMessages.objects.filter(mdm_id__in=mdm_ids).delete()
        MDMMembers.objects.filter(mdm_id__in=mdm_ids).delete()
        MDMMaster.objects.filter(owner_team=team_id).delete()

        # Project channel
        PMThreadMessages.objects.filter(project_id__in=project_ids).delete()
        PMMessages.objects.filter(project_id__in=project_ids).delete()

        # User-level chat master + per-user todo. ToDoGroup CASCADE
        # removes its items; ToDoCategory rows are user-scoped (not
        # tied to a group) so they need their own pass.
        UserChatMaster.objects.filter(team=team_id).delete()
        ToDoGroup.objects.filter(team=team_id).delete()
        ToDoCategory.objects.filter(team=team_id).delete()

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

        # Tasks (TaskActivity cascades on task delete; comments/tags/
        # attachments are SET_NULL so delete explicitly).
        TaskComments.objects.filter(task_id__in=task_ids).delete()
        TaskMaster.objects.filter(team=team_id).delete()

        # Projects (Sprint/SprintConfig/MilestoneMaster cascade on
        # project delete; MilestoneAssignees cascades on milestone).
        ProjectTags.objects.filter(team=team_id).delete()
        ProjectMembers.objects.filter(team=team_id).delete()
        ProjectMaster.objects.filter(team=team_id).delete()

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
