"""English demo content — the `"en"` half of the bilingual demo dataset.

Pure data. Every constant here is a BlockNote body or a nested blueprint
dict; there are no model imports and no logic, which is what lets
`demo_content_ja` be a straight structural mirror (see that module's
docstring for the structural contract both tables obey).

Three things about this table specifically:

  * **It is the eval table.** `evals/fixture.py` calls
    `create_demo_environment` with no `lang`, so it seeds English, and
    the three suites (76 behaviour + 75 retrieval + 6 summary cases)
    assert on ~40 English title substrings via a case-insensitive
    substring match. Those suites gate deploys. Rewording a title here
    can turn a green deploy red; adding words around a pinned substring
    is safe, removing or translating one is not.
  * **The prose moved here unchanged.** This content was lifted out of
    `demo_seeder.py` verbatim when the demo went bilingual — byte-for-
    byte, verified by hashing every constant before and after the move.
    That was deliberate: it makes "did the eval-pinned literals survive?"
    a question answered by a diff rather than by re-reading 40 strings.
  * **Numerals and proper nouns are load-bearing.** `Lighthouse`, `95`,
    `120KB`, `40KB`, `Dovetail`, `12 interviews`, `8 themes` are asserted
    by `answer_contains` cases, and more importantly they are the
    concrete details that make the AI's answer sound like it read the
    team's actual history rather than paraphrasing a summary.

`BOT_ROLES` and the note/todo LABELS live in `demo_seeder` alongside the
logic that consumes them, because they are single strings rather than
bodies. Everything with paragraphs in it lives here.
"""

from __future__ import annotations

from origin.services.demo_blocks import _body

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
                "title": "Spike: framer-motion vs native CSS animations for the redesign",
                "status": "Closed",
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
                        "✅ Decision",
                        [
                            "No framer-motion on the marketing pages. Drawer, modal fade and "
                            "hero stagger all ship as CSS animations, with the Web Animations "
                            "API for the drawer's interruptible slide.",
                        ],
                    ),
                    (
                        "📌 Findings",
                        [
                            (
                                "bullet",
                                "Bundle: framer-motion is 40KB gzip. The WAAPI drawer prototype came in at 800 bytes — a third of the entire 120KB JS budget, spent on one component.",
                            ),
                            (
                                "bullet",
                                "All three planned animations were reachable in pure CSS. The hero stagger needed a nth-child delay ladder rather than a spring, which is a cosmetic difference at this scale.",
                            ),
                            (
                                "bullet",
                                "CSS-only keeps SSR trivial — no hydration mismatch on the marketing pages.",
                            ),
                            (
                                "bullet",
                                "Revisit for in-app: the app has no marketing perf budget, so framer-motion's ergonomics may well win there. Tracked as a separate spike.",
                            ),
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
                "parent_idx": 1,
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
                "parent_idx": 1,
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
                    # ---- Sub-task-shaped follow-up. ----
                    # Demo surface for the agent's break-a-task-into-
                    # sub-tasks flow (create_task_plan with
                    # parent_task_id): the comments propose a concrete
                    # split of THIS task's work — the natural ask is
                    # "add these as sub-tasks of this task", never a new
                    # milestone (the milestone-from-a-thread demo lives
                    # in the group chat's mobile-speed thread instead).
                    (
                        "bot1",
                        "This is turning out to be more than one fix. Based on the trace I'd break it down: (1) rerun the profile on real devices after the design-system cutover, (2) strip the legacy Bootstrap CSS that still ships alongside v3, (3) add a regression check so this class of slowdown can't sneak back in.",
                    ),
                    (
                        "bot3",
                        "Agreed on the split — QA can take the device rerun and the regression check.",
                    ),
                    (
                        "demo",
                        "Sounds right. Let's add those as sub-tasks of this task so we can track them separately.",
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
        # ---- Deliberately DISORGANIZED milestone. ----
        # Demo surface for the agent's "Organize tasks with AI" flow
        # (list_tasks + list_task_dependencies → ONE update_tasks_bulk):
        # priorities and due dates contradict the dependency order on
        # purpose — the plans/entitlements matrix gates almost everything
        # yet sits at Low priority with a nearly-last due date, tasks
        # downstream of it are due BEFORE it, one dependent is already
        # WIP while its blocker is untouched, and an independent cosmetic
        # task carries an inflated Critical. Keep the contradictions if
        # you edit — a tidy milestone gives the agent nothing to fix.
        # (Topic chosen for keyword disjointness from the eval suites:
        # billing/payment/invoice appear nowhere in cases.yaml /
        # retrieval_cases.yaml, so the extra rows can't shift retrieval
        # rankings.)
        "extra_milestones": [
            {
                "milestone": {
                    "title": "Billing & Plans Revamp",
                    "status": "Open",
                    "priority": "High",
                    "due_offset_days": 25,
                    "body": _body(
                        (
                            "🎯 Goal",
                            [
                                "Move from the single flat plan to a tiered plans-and-limits "
                                "model: define the plans, meter usage against them, and let "
                                "customers change plans without a support ticket.",
                            ],
                        ),
                        (
                            "⚠️ State of this milestone",
                            [
                                "Tasks were brain-dumped in a hurry after the pricing "
                                "offsite — priorities and dates have not been groomed "
                                "against the dependency order yet.",
                            ],
                        ),
                    ),
                },
                "tasks": [
                    {
                        "title": "Define billing plans and entitlements matrix",
                        "status": "Open",
                        "priority": "Low",
                        "assignee": "bot0",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 24,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "Decide the plan tiers (Free / Pro / Max), which features "
                                    "and limits belong to each, and the upgrade/downgrade "
                                    "rules. Every other piece of the revamp consumes this "
                                    "matrix as its source of truth.",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot1",
                                "Shouldn't this land before we wire the payment sandbox? Hard to integrate against plan names we haven't defined.",
                            ),
                        ],
                    },
                    {
                        "title": "Integrate payment provider sandbox",
                        "status": "WIP",
                        "priority": "Critical",
                        "assignee": "bot1",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 6,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "Stand up the payment provider's sandbox: checkout "
                                    "session, webhooks for payment events, and the customer "
                                    "portal hand-off.",
                                ],
                            ),
                        ),
                        "comments": [
                            (
                                "bot1",
                                "Started scaffolding, but I'm coding against guessed plan names until the entitlements matrix lands.",
                            ),
                        ],
                    },
                    {
                        "title": "Build subscription upgrade and downgrade flow",
                        "status": "Open",
                        "priority": "Minimal",
                        "assignee": "bot2",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 10,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "In-app flow for switching plans: proration preview, "
                                    "confirmation, and the downgrade guard rails when usage "
                                    "exceeds the target plan's limits.",
                                ],
                            ),
                        ),
                        "comments": [],
                    },
                    {
                        "title": "Usage metering pipeline for plan limits",
                        "status": "Open",
                        "priority": "Normal",
                        "assignee": "bot1",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 4,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "Count the metered actions per workspace per day and "
                                    "expose them to the plan-limit checks. Needs the matrix "
                                    "to know WHICH actions are metered per tier.",
                                ],
                            ),
                        ),
                        "comments": [],
                    },
                    {
                        "title": "Billing admin dashboard for support",
                        "status": "Open",
                        "priority": "High",
                        "assignee": "bot2",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 5,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "Internal view for support: a customer's current plan, "
                                    "usage against limits, recent payment events, and manual "
                                    "plan-change override.",
                                ],
                            ),
                        ),
                        "comments": [],
                    },
                    {
                        "title": "Invoice PDF and receipt email templates",
                        "status": "Open",
                        "priority": "Critical",
                        "assignee": "bot3",
                        "is_milestone_child": True,
                        "parent_idx": None,
                        "due_offset_days": 26,
                        "body": _body(
                            (
                                "🧾 Summary",
                                [
                                    "Branded invoice PDF and the receipt email. Cosmetic and "
                                    "independent of the other tracks — can ship any time "
                                    "before launch.",
                                ],
                            ),
                        ),
                        "comments": [],
                    },
                ],
            },
        ],
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
        (
            0,
            "Separate topic before we wrap — mobile speed. We keep firefighting it one bug at a time; the latest iOS report is just the newest symptom. I'd like to scope a proper mobile performance hardening effort. Thread below — let's shape it.",
        ),
    ],
    "thread": {
        "parent_index": 13,  # Dan's keyboard-shortcuts question
        "messages": [
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
    # ---- Plan-shaped thread. ----
    # Demo surface for the agent's plan-from-a-chat-thread flow
    # (create_task_plan): a proper CHAT thread (not task comments)
    # scoping a new workstream with an implied breakdown, ordering
    # ("first / after the pipeline"), owners, a deadline, and the home
    # project named explicitly so the agent can resolve it. Nothing in
    # it exists as tasks yet, so the created milestone is clearly new.
    # Keyword note: deliberately avoids "Safari" / "Lighthouse" /
    # "marketing site" so it can't outrank those retrieval-eval targets.
    "extra_threads": [
        {
            "parent_index": 14,  # demo's "mobile speed" scoping message
            "messages": [
                (
                    2,
                    "Concretely I'd split it in two: (1) an image optimization pipeline — responsive sizes plus AVIF/WebP with lazy loading below the fold; (2) route-level code splitting so the landing pages stop shipping app code. Code splitting should come after the image pipeline, otherwise we're measuring against a moving baseline.",
                ),
                (
                    3,
                    "Add a third-party script audit before either of those — the analytics tags and the chat widget eat a scary slice of main-thread time in my last trace. Doing the audit first might delete work from the other two.",
                ),
                (
                    4,
                    "Whatever we ship, I want an automated speed check in CI on every PR — fail the build if the landing pages get slower. I can own that, but it needs the monitoring dashboards to exist first so we agree on the numbers we gate on.",
                ),
                (
                    2,
                    "Monitoring dashboards plus alerting can be mine. Needs the image pipeline done first, or the graphs are dominated by known-bad assets.",
                ),
                (
                    1,
                    "Owners then: Carol the script audit, Bob the image pipeline and monitoring, Dan the CI gate. High priority on the audit and the pipeline, normal for the rest — and let's aim for end of next month.",
                ),
                (
                    0,
                    "Perfect, that hangs together. This belongs in the Website Redesign project — someone turn this thread into a milestone with tasks when you get a minute.",
                ),
            ],
        },
    ],
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
# Single-string labels — note titles, folder names, todo text, channel name.
#
# These are not bodies, so they never went through `_body`; they lived as
# literals at their use sites until the demo went bilingual. Two of them
# are eval-pinned ("Welcome to your demo workspace" and the "Spotlight"
# in the Cmd-K note title), which is reason enough to keep the English
# spellings in one reviewable place.
# ---------------------------------------------------------------------------

CHANNEL_TITLES = {
    # Interpolated with the team name, which is generated, not translated.
    "gm": "general · {team_name}",
}

# Sprint display name, formatted with `n` (the sequence number). Visible
# in the sprint picker and in `get_sprint_summary`, which is why it lives
# in the content table rather than in `_create_sprint`.
SPRINT_NAME = "Sprint {n}"

# Field labels on the PM channel's task-card headers, one card per task
# (25 rows per workspace). `unassigned` is the italic grey placeholder the
# card shows in place of an assignee mention.
#
# These were literals inside `_task_card_body` until the demo went
# bilingual — the same class of miss as `SELF_DM_MESSAGES`, and invisible
# for the same reason: a card's *title* comes from the blueprint, so a
# Japanese workspace's cards read as Japanese while every label on them
# stayed English, and a "mostly CJK" assertion over the corpus never
# notices six short strings. The JA spellings match the shipped UI
# (`genos-frontend/src/i18n/locales/ja/index.ts`) rather than being
# translated afresh, so a card agrees with the task pane beside it.
CARD_LABELS = {
    "status": "Status: ",
    "priority": "Priority: ",
    "effort": "Effort: ",
    "assignee": "Assignee: ",
    "reporter": "Reporter: ",
    "due": "Due: ",
    "unassigned": "Unassigned",
}

# The demo user's own "notes to self" in their personal scratch chat,
# oldest first. POSITIONAL: the seeder derives each message's `seq` from
# the list index, and `assert_content_parity` compares the length across
# tables.
#
# The first two address the visitor directly (they explain the scratch
# chat and teach a Spotlight trick); the rest are the user's own working
# notes. Two carry story content the rest of the workspace cites — the
# framer-motion spike reminder with the perf-budget literals, and the
# Q3-bets note — so they are held to the same standard as the blueprints.
SELF_DM_MESSAGES = (
    "Welcome — this is your personal scratch chat. Drop quick thoughts, links, "
    "and reminders here. Press the ✓ icon in the header to toggle your todo "
    "list (the same data the Spotlight agent reads via `list_today_todos`).",
    'Spotlight tip: Cmd-K → "what\'s on my todo list today?" reads from the '
    'ToDoPane on the right. Try "add a todo: …" for an approval-gated write.',
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
)

NOTE_TITLES = {
    "folder_getting_started": "Getting started",
    "folder_product": "Product",
    "folder_design": "Design system",
    # ⚠ eval-pinned: `Welcome to your demo workspace` and `Spotlight`.
    "welcome": "Welcome to your demo workspace",
    "spotlight_tips": "How to get the most out of Spotlight (Cmd-K)",
    "weekly_priorities": "This week's priorities",
    "design_system_inventory": "Design system inventory (v3.2)",
    "task_synthesis": "Customer interview synthesis methodology",
    "chat_kickoff_recap": "Sprint 1 kickoff recap",
}

# Todo content, keyed so `_create_todos` reads as a calendar layout rather
# than a wall of strings. `cat_*` are the three tag lanes; each day is a
# list of (key, title) with bodies under `notes`.
TODO_CONTENT = {
    "cat_web": "Website Redesign",
    "cat_q2": "Q2 Roadmap",
    "cat_personal": "Personal",
    "hero_parent": "Ship homepage hero illustration handoff",
    "hero_parent_notes": (
        "Carol delivered v3 of the hero SVG yesterday. Need to pick a final variant, "
        "wire it into the BlockNote intro section, and confirm the alt text with Alice."
    ),
    "hero_sub1": "Pick final SVG variant from Carol's v3 batch",
    "hero_sub2": "Wire hero into the BlockNote intro section",
    "spike_review": "Review framer-motion vs CSS animations spike notes",
    "spike_review_notes": (
        "Bob filed his spike summary in the responsive nav thread. Read it, sign off "
        "on the recommendation, and link the decision into the Q2 roadmap doc."
    ),
    "synthesize": "Synthesize the remaining 3 customer interviews in Dovetail",
    "reply_alice": "Reply to Alice about the Q3 bets draft",
    "reply_alice_notes": "She wants a yes/no on the proactive surfacing vs search angle.",
    "offer_letter": "Sign offer letter for the new QA hire",
    "water": "Drink 2L water before EOD",
    "storybook": "Confirm 320px Storybook stories for responsive nav",
    "perf_budget": "Approve perf budget thresholds with Bob (Lighthouse 95 / JS 120KB)",
    "synthesis_outline": "Draft customer interview synthesis outline",
    "spike_ticket": "File spike ticket for in-app animation framework",
    "spike_ticket_notes": (
        "Bob and I agreed to defer the in-app animation decision until after launch. "
        "Spike ticket should track the post-launch evaluation."
    ),
    "plausible": "Set up Plausible events tracker on the marketing site",
    "sprint_kickoff": "Run sprint kickoff with Alice + Bob",
    "review_milestones": "Review Q2 milestones in the roadmap doc",
}
