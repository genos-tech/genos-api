"""The Genos Guide — seeded user-manual notes, one copy per user per team.

Why NOTES, and why MY-notes: Spotlight's retrieval is ACL-scoped
OpenSearch — whatever a user can see, Genos can find and cite. Seeding
the manual as notes therefore makes "how do I …?" a question Spotlight
can answer with citations. Placement follows from the ACL:

  * TEAM notes would give one shared copy — but anyone can edit it and
    an editor can delete it, taking the manual away from everyone.
  * The OWNER's my-notes would be safe — but invisible to every
    teammate's Spotlight (my-notes are private), so only the owner
    could ask about Genos.
  * PER-USER my-notes give every member their own indestructible copy.
    My-notes are team-scoped and Spotlight runs in a team context, so
    per-(user, team) is exactly the retrieval-correct grain; and since
    search ACL means each user only ever sees their own copy, the
    duplication can never pollute anyone's ranking.

Seeded from `TeamMembersView.post` (the client calls /team/join/ both
after creating a team and on every team switch, so existing users pick
the guide up on their next switch) and from the starter seeder.
Idempotency is the `TeamMembers.guide_seeded_at` stamp — durable on
purpose: personal-folder deletion is a HARD recursive delete, so a
folder-existence check would re-ambush a user who deleted their guide
on every team switch. Once stamped, re-seeding is a deliberate choice
(`seed_guide_notes(user, team, force=True)` from a shell), never
automatic. The folder check stays as a secondary guard against
double-creation when no membership row exists to stamp.

Content rule: everything below describes SHIPPED behavior in plain
instructional prose. When the product changes, change the guide — new
users seed the new text (existing copies are the user's own notes and
are deliberately never rewritten).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.services.demo_seeder import _body

log = logging.getLogger(__name__)

GUIDE_FOLDER_NAME = "Genos Guide"

_B = "bullet"

GUIDE_NOTES: list[tuple[str, list]] = [
    (
        "Genos basics: layout and navigation",
        _body(
            (
                "What Genos is",
                [
                    "Genos is chat, tasks, and notes in one workspace, with an AI "
                    "assistant (Spotlight) that can read all three. Instead of "
                    "switching between a chat app, a task tracker, and a wiki, "
                    "everything lives in one place and stays connected: a chat "
                    "thread can become a task, a task carries its own notes and "
                    "discussion, and Spotlight can answer questions across all "
                    "of it.",
                    "This guide describes Genos as of August 2026. It lives in "
                    "your My Notes, so it is private to you — and because "
                    "Spotlight can read your notes, you can also just ask Genos "
                    'questions like "how do I create a milestone?" and it will '
                    "answer from these pages.",
                ],
            ),
            (
                "The four main surfaces",
                [
                    (
                        _B,
                        "Inbox — where things that need you arrive: activity "
                        "notices (mentions, replies, reactions) on the "
                        "Activities tab, and join/access requests awaiting a "
                        "decision on the Requests tab.",
                    ),
                    (
                        _B,
                        "Chat — direct messages, group chats, and one "
                        "automatically-created channel per project.",
                    ),
                    (_B, "Tasks — projects, tasks, milestones, sprints, and boards."),
                    (
                        _B,
                        "Notes — collaborative documents: private My Notes, "
                        "shared team folders, and notes attached to tasks and "
                        "chats.",
                    ),
                    "Switch between surfaces with the icons in the left "
                    "sidebar. Your team name at the top of the sidebar switches "
                    "between teams if you belong to more than one.",
                ],
            ),
            (
                "Works everywhere",
                [
                    (
                        _B,
                        "Press Cmd-K (Ctrl-K on Windows) anywhere to open "
                        "Spotlight, the AI assistant.",
                    ),
                    (
                        _B,
                        "On a phone, Genos is an installable app: use your "
                        'browser\'s "Add to Home Screen". On iPhone, '
                        "installing is also what enables push notifications.",
                    ),
                    (
                        _B,
                        "Genos ships in seven languages (English, 日本語, 中文, "
                        "العربية, हिन्दी, Français, Español) with full "
                        "right-to-left layout for Arabic; pick yours in "
                        "Settings. Light and dark themes follow your choice "
                        "too.",
                    ),
                    (
                        _B,
                        "Paste any Genos link (a task, a note, a message) into "
                        "chat and it becomes a clickable preview that opens "
                        "in place, without losing where you were.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Chat: messages, threads, and mentions",
        _body(
            (
                "Kinds of conversations",
                [
                    (
                        _B,
                        "Direct messages (DM) — one-to-one, plus small "
                        "multi-person DMs for quick side conversations.",
                    ),
                    (
                        _B,
                        "Group messages (GM) — named group chats you create "
                        "and invite people into; joining can also be "
                        "requested and approved through the Inbox.",
                    ),
                    (
                        _B,
                        "Project channels — every project gets one "
                        "automatically. Task activity appears there as task "
                        "cards, and each card carries its own reply thread, "
                        "so discussion stays attached to the work.",
                    ),
                ],
            ),
            (
                "Everyday messaging",
                [
                    (
                        _B,
                        "Reply in a thread to keep side-discussions out of "
                        "the main flow — threads have their own pane.",
                    ),
                    (
                        _B,
                        "@-mention someone and they are notified in the app, "
                        "by push, and by email if they're away. Mention "
                        "groups let one @ reach a defined set of people.",
                    ),
                    (
                        _B,
                        "React with emoji — including your team's own custom "
                        "emoji and GIFs. Admins can upload custom emoji.",
                    ),
                    (_B, "Pin important messages to a channel so they're easy to find again."),
                    (
                        _B,
                        "Flag a message to turn it into a personal follow-up; "
                        "mark the flag done when handled, and review past "
                        "flags any time.",
                    ),
                    (
                        _B,
                        "The todo pane (next to chat) keeps a lightweight "
                        "personal checklist per day, separate from real "
                        "project tasks.",
                    ),
                ],
            ),
            (
                "Finding things again",
                [
                    (
                        _B,
                        "Search covers messages, tasks, and notes — both "
                        "keyword search and asking Spotlight in plain "
                        "language.",
                    ),
                    (
                        _B,
                        "Clicking a message link jumps you to that exact "
                        "message in its conversation.",
                    ),
                    (
                        _B,
                        "Unread filters and per-group personal tags help "
                        "keep a busy sidebar manageable.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Tasks and projects",
        _body(
            (
                "Projects",
                [
                    "A project is the container for tasks, milestones, "
                    "sprints, and its own chat channel. Create one from the "
                    "Tasks sidebar; make it public (visible to the team) or "
                    "private (members only). Each project gets a short code, "
                    "so tasks get readable IDs like WRD-12.",
                    (
                        _B,
                        "Project owners can define per-project custom fields "
                        "for tasks, mark certain fields as required, and set "
                        "reusable task body templates.",
                    ),
                    (
                        _B,
                        "Project labels group projects in the sidebar, and "
                        "the sidebar can filter by label.",
                    ),
                ],
            ),
            (
                "Tasks",
                [
                    (
                        _B,
                        "Tasks have a status (Open, WIP, Pending, Blocked, "
                        "Closed), a priority, an assignee, optional "
                        "collaborators, a due date, tags, and a rich "
                        "description with comments underneath.",
                    ),
                    (_B, "Break work down with sub-tasks; a task's page shows its whole subtree."),
                    (
                        _B,
                        "Dependencies: mark that task B is blocked by task A. "
                        "Genos sets the blocked task's status to Blocked "
                        "automatically and clears it when the blocker "
                        "closes. The task graph view draws the whole "
                        "dependency picture.",
                    ),
                    (
                        _B,
                        "Task Weight (1–25) combines priority and time "
                        'pressure so "what should I do next" has an '
                        "answer; team capacity and velocity views build on "
                        "it.",
                    ),
                    (
                        _B,
                        "Comments support @-mentions and reactions, and every "
                        "change to a task is recorded in its Activities "
                        "feed — a per-task audit trail.",
                    ),
                ],
            ),
            (
                "Milestones and sprints",
                [
                    (
                        _B,
                        "A milestone bundles tasks toward one dated goal and "
                        "shows progress; assign tasks to it and it tracks "
                        "completion.",
                    ),
                    (
                        _B,
                        "Sprints give each project a repeating cadence; the "
                        "sprint board shows the current sprint's tasks by "
                        "status, and the task table gives a filterable, "
                        "sortable spreadsheet view.",
                    ),
                    (
                        _B,
                        "Filters (assignee, status, tag, and more) can be "
                        "saved as named filter sets shared with the "
                        "project.",
                    ),
                ],
            ),
            (
                "GitHub",
                [
                    (
                        _B,
                        "Connect GitHub in Settings → Integrations to link "
                        "pull requests to tasks; a linked task can close "
                        "automatically when its PR merges.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Notes: personal, team, and shared",
        _body(
            (
                "Where notes live",
                [
                    (
                        _B,
                        "My Notes — private to you (like this guide). "
                        "Organize with folders and nested folders.",
                    ),
                    (
                        _B,
                        "Team folders — a folder shared with the team; the "
                        "folder carries the access rules, so everything "
                        "inside follows them.",
                    ),
                    (
                        _B,
                        "Task notes — attached to a task; people in the "
                        "project can read and edit them, so plans live next "
                        "to the work.",
                    ),
                    (
                        _B,
                        "Chat notes — attached to a conversation, for shared "
                        "minutes and decisions.",
                    ),
                ],
            ),
            (
                "Editing",
                [
                    (
                        _B,
                        "The editor is a block editor: headings, bullet and "
                        "numbered lists, checkboxes, code blocks, tables, "
                        "images, and @-mentions that notify the person.",
                    ),
                    (
                        _B,
                        "Shared notes are real-time collaborative — several "
                        "people can edit at once and see each other's "
                        "changes live.",
                    ),
                    (
                        _B,
                        "Version history records revisions automatically; "
                        "open it to compare and restore an earlier "
                        "version. Deleting content is therefore rarely "
                        "fatal.",
                    ),
                    (
                        _B,
                        "Import Markdown files as notes and export any note "
                        "back to Markdown from its menu.",
                    ),
                ],
            ),
            (
                "Sharing and access",
                [
                    (
                        _B,
                        "Share a note with specific people as editors or "
                        "viewers. If you hit a note you can't open, you can "
                        "request access — the owner approves or declines "
                        "from their Inbox.",
                    ),
                    (_B, "Everything you can read is searchable — by keyword and by Spotlight."),
                ],
            ),
        ),
    ),
    (
        "Genos AI (Spotlight): ask, act, and stay ahead",
        _body(
            (
                "Asking questions",
                [
                    "Press Cmd-K (Ctrl-K) and ask in plain language: "
                    '"what\'s due this week?", "summarize the discussion '
                    'about the pricing page", "who is working on the login '
                    'bug?". Spotlight reads your chats, tasks, notes, and '
                    "todos — only ever the things YOU can see — and answers "
                    "with citations you can click to open the source.",
                    (
                        _B,
                        "Mention things precisely with @ (people) and # "
                        "(projects, tasks) inside your question to point "
                        "Spotlight at exactly the right context.",
                    ),
                    (
                        _B,
                        "You can also ask about a specific thread or note "
                        "from its own Ask button, and turn a good answer "
                        "into a note.",
                    ),
                ],
            ),
            (
                "Letting it act",
                [
                    (
                        _B,
                        "Spotlight can also DO things: create tasks and task "
                        "plans, update tasks in bulk, write notes, and "
                        "more — depending on your plan.",
                    ),
                    (
                        _B,
                        "Every write shows an approval preview first: you "
                        "see exactly what will be created or changed and "
                        "approve or reject it. Nothing is written without "
                        "your click.",
                    ),
                    (
                        _B,
                        "Effort levels trade speed for depth (quick answers "
                        "vs. thorough multi-step research); which levels "
                        "are available depends on your plan.",
                    ),
                    (
                        _B,
                        "Long-running asks keep working if you close the "
                        "window — you'll get a notification when the answer "
                        "is ready.",
                    ),
                    (
                        _B,
                        "AI usage is measured in monthly credits that come "
                        "with your plan; the settings page shows what "
                        "you've used.",
                    ),
                ],
            ),
            (
                "It comes to you",
                [
                    (
                        _B,
                        "On higher plans, Genos sends a proactive digest to "
                        "your Inbox — overdue work, blockers, and stale "
                        "items, at 8am your local time.",
                    ),
                    (
                        _B,
                        "A daily email digest (all plans) summarizes what "
                        "you haven't seen; both digests can be switched off "
                        "in Settings → Notifications.",
                    ),
                    (_B, "Rate answers with 👍/👎 — feedback improves the product."),
                ],
            ),
        ),
    ),
    (
        "Searching and asking: how to get better answers",
        _body(
            (
                "One box, two different tools",
                [
                    "Cmd-K opens a box that does two jobs, and which one you "
                    "get depends on what you type. A few words gives you "
                    "instant keyword results — messages, tasks, notes and "
                    "todos whose text matches. A whole question, sent with "
                    "Enter, makes Genos read the matching material and answer "
                    "it. Short and specific searches; a sentence asks.",
                ],
            ),
            (
                "Narrowing a search",
                [
                    (
                        _B,
                        "The chips under the box limit results by kind — "
                        "chat, task, note, todo, or answers Genos gave you "
                        "before. There is also a project filter and a date "
                        "range.",
                    ),
                    (
                        _B,
                        "There is no query syntax. Quotation marks, AND, OR, "
                        "a leading minus and field:value are all read as "
                        "ordinary words, so reach for the chips instead of "
                        "typing a filter. Search matches meaning as well as "
                        "spelling, which is why a plain phrase usually beats "
                        "keyword-guessing.",
                    ),
                    (
                        _B,
                        "Both tools are limited to what your account can "
                        "already open. A teammate's private notes, or a "
                        "channel you're not in, cannot show up — for you or "
                        "for Genos.",
                    ),
                ],
            ),
            (
                "Asking well",
                [
                    (
                        _B,
                        "Name things with @ (people) and # (projects, tasks) "
                        "inside the question itself. \"What's left on #WRD "
                        'before Friday?" beats "what\'s left" because it '
                        "removes the guess about which work you mean.",
                    ),
                    (
                        _B,
                        "Ask where the material already is: a thread and a "
                        "note each have their own Ask button, which scopes "
                        "the question to that one conversation or document "
                        "instead of the whole workspace.",
                    ),
                    (
                        _B,
                        "Answers carry citations. Click one to open the "
                        "source — an answer is only as good as what it cites, "
                        "so the citations are the part worth checking.",
                    ),
                    (
                        _B,
                        "Clicking a search result opens a preview on top of "
                        "your place in the list, so you don't lose it. "
                        "Cmd-click (Ctrl-click) opens the real page instead.",
                    ),
                    (
                        _B,
                        "Save a good answer as a note and it becomes "
                        "searchable itself. Rate answers with 👍/👎.",
                    ),
                ],
            ),
            (
                "When it can't find something",
                [
                    (
                        _B,
                        "Brand-new content takes a moment to become "
                        "searchable. If a task you just made doesn't come up, "
                        "wait a beat and retry before concluding it's lost.",
                    ),
                    (
                        _B,
                        "Web search is a separate thing from workspace "
                        "search: it's a toggle in Settings → Spotlight, and "
                        "it's included from the Core plan up. Without it, "
                        "Genos answers only from your workspace.",
                    ),
                    (
                        _B,
                        "Earlier questions are kept in the history list on "
                        "the Genos page, so you can reopen a past "
                        "conversation instead of asking again from scratch.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Notifications: in-app, push, and email",
        _body(
            (
                "The three channels",
                [
                    (_B, "In-app — toasts and the Inbox Activities feed while you're using Genos."),
                    (
                        _B,
                        "Push — system notifications when the tab is in the "
                        "background or the app is closed. Allow them in "
                        "Settings → Notifications. Genos is smart per "
                        "device: the screen you're actively looking at "
                        "shows a toast instead of a push, while your other "
                        "devices still get pushed.",
                    ),
                    (
                        _B,
                        "Email — for when you're away. Genos waits until "
                        "you've been gone a while, then sends ONE batched "
                        "email covering what you missed — never one email "
                        "per event. Anything you already read in the app is "
                        "dropped from the email.",
                    ),
                ],
            ),
            (
                "Tuning it",
                [
                    (
                        _B,
                        "Settings → Notifications has a master switch, "
                        "per-category toggles (mentions, thread replies, "
                        "task comments, requests, every-message), and a "
                        "separate set of email-only toggles.",
                    ),
                    (
                        _B,
                        "Mute a specific chat from its header bell, or mute "
                        "one thread, task, or note from its ⋮ menu — "
                        "manage all mutes in Settings.",
                    ),
                    (
                        _B,
                        "Every notification email has an unsubscribe link; "
                        "the daily email digest has its own separate "
                        "opt-out.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Teams, members, and roles",
        _body(
            (
                "Membership",
                [
                    (
                        _B,
                        "Invite teammates by email from the team menu; they "
                        "get a link that lands them in your team. People "
                        "can also request to join by team ID, which you "
                        "approve from the Inbox.",
                    ),
                    (
                        _B,
                        "Genos picks up each member's timezone and language "
                        "automatically, so due dates, digests, and emails "
                        "follow each person's own clock and language.",
                    ),
                ],
            ),
            (
                "Roles",
                [
                    (
                        _B,
                        "Owner — one per team (and per project, and per "
                        "group chat). Only the owner can delete the thing "
                        "or hand it to someone else.",
                    ),
                    (
                        _B,
                        "Editor — the day-to-day admin: invite and manage "
                        "members, rename, change avatars, set other "
                        "members' roles. Give a second person Editor early "
                        "so the team never depends on one account.",
                    ),
                    (
                        _B,
                        "Viewer — the default role: full read access, "
                        "participates in chat and tasks, no member "
                        "management.",
                    ),
                    (
                        _B,
                        "If an owner goes absent, an Editor can file an "
                        "ownership claim; if the owner doesn't respond "
                        "within 30 days, ownership transfers. This is the "
                        "safety valve against an abandoned team.",
                    ),
                ],
            ),
            (
                "Integrations and billing",
                [
                    (
                        _B,
                        "Google Calendar (Settings → Integrations) shows "
                        "your events beside your tasks, supports several "
                        "Google accounts at once, and can sync tasks to "
                        "your calendar.",
                    ),
                    (_B, "GitHub connects pull requests to tasks."),
                    (
                        _B,
                        "Plans and billing live in Settings → Plans; "
                        "payment is handled by Stripe, and the plan applies "
                        "to the whole team.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Settings: every option, and where to find it",
        _body(
            (
                "Opening Settings",
                [
                    "The gear at the bottom of the left sidebar opens "
                    "Settings. On a phone, it's the Account tab, then "
                    "Settings. It opens over whatever you were doing rather "
                    "than navigating away, so you don't lose your place. The "
                    "sections below are its tabs, in order.",
                ],
            ),
            (
                "General",
                [
                    (
                        _B,
                        "Appearance — Light, Dark, or System (System follows "
                        "your OS and changes with it). Separately, an accent "
                        "colour: purple, blue, teal, emerald, amber, rose or "
                        "slate. The phone's Account tab has a quick "
                        "light/dark switch, but only the full setting here "
                        "can choose System.",
                    ),
                    (
                        _B,
                        "Language — English, 日本語, 中文, العربية, हिन्दी, "
                        "Français, Español. Arabic switches the whole layout "
                        "right-to-left. This also sets the language your "
                        "notification emails arrive in. Anything not yet "
                        "translated falls back to English rather than showing "
                        "blank.",
                    ),
                    (
                        _B,
                        "Privacy — whether to share anonymous usage "
                        "analytics.",
                    ),
                ],
            ),
            (
                "Plan & Usage",
                [
                    "Your team's plan, meters for what you've used, and the "
                    "upgrade and billing links. This page is the authority on "
                    "your own numbers — see the plans page of this guide for "
                    "what each tier includes. Only a team's owner sees its "
                    "billing section.",
                ],
            ),
            (
                "Spotlight",
                [
                    (
                        _B,
                        "Which AI model (or, on plans with it, which effort "
                        "level) Genos uses, plus your usage so far. Some "
                        "models are listed but not yet selectable — they show "
                        "as coming soon rather than being hidden.",
                    ),
                    (
                        _B,
                        "Switches for AI answers in Spotlight and for web "
                        "search. The same panel is reachable from the gear on "
                        "the Genos page.",
                    ),
                ],
            ),
            (
                "Chat",
                [
                    (
                        _B,
                        "Message layout: roomy bubbles, or compact rows that "
                        "fit more on screen.",
                    ),
                    (
                        _B,
                        "Your three one-click reaction emoji.",
                    ),
                    (
                        _B,
                        "Whether double-clicking a message turns it into a "
                        "to-do.",
                    ),
                ],
            ),
            (
                "Tasks",
                [
                    (
                        _B,
                        "Whether the quick-add row insists on the fields your "
                        "project marks required, or lets you fill them in "
                        "later.",
                    ),
                    (
                        _B,
                        "Whether a task closes itself when its linked pull "
                        "request merges (needs GitHub connected).",
                    ),
                    (
                        _B,
                        "Whether task due dates sync to Google Calendar, "
                        "including a one-off backfill of existing tasks "
                        "(needs Google connected, with calendar access "
                        "granted).",
                    ),
                ],
            ),
            (
                "Notifications, Mention groups, Custom emoji",
                [
                    (
                        _B,
                        "Notifications — the master switch, the browser "
                        "permission prompt, per-category toggles for push and "
                        "for email separately, your muted chats and muted "
                        "threads/tasks/notes, and the two digests. Covered in "
                        "full on the notifications page of this guide.",
                    ),
                    (
                        _B,
                        "Mention groups — define a name that @-mentions a set "
                        "of people at once. Any member can create one.",
                    ),
                    (
                        _B,
                        "Custom emoji — upload your team's own. Names are "
                        "lowercase letters, numbers, underscore, plus and "
                        "hyphen, up to 50 characters; files up to 512 KB as "
                        "png, jpg, gif, webp or svg. Any member can upload; "
                        "only whoever uploaded one can delete it.",
                    ),
                ],
            ),
            (
                "Shortcuts, Account, Integrations, Developer",
                [
                    (
                        _B,
                        "Shortcuts — a read-only list of the keyboard "
                        "shortcuts. They aren't re-bindable.",
                    ),
                    (
                        _B,
                        "Account — export your data as a JSON file, or delete "
                        "your account. Deleting asks you to type DELETE and "
                        "confirm with your password, and is refused while you "
                        "still own a team that has other members in it: hand "
                        "that team over first.",
                    ),
                    (
                        _B,
                        "Integrations — connect Google and GitHub, and choose "
                        "which repositories Genos may see.",
                    ),
                    (
                        _B,
                        "Developer — personal API keys and team webhooks. "
                        "Anyone can make a key; webhooks need to be a team "
                        "owner or editor. See the API page of this guide.",
                    ),
                ],
            ),
            (
                "Your profile (a separate window)",
                [
                    "Your avatar in the sidebar opens your profile, which is "
                    "not part of Settings: display name (up to 50 "
                    "characters), avatar (jpg or png, size capped by your "
                    "plan), a custom status of up to 50 characters, an "
                    "Appear offline switch that hides your presence from "
                    "everyone, your job title and your base country. Clicking "
                    "anyone else's avatar opens the same window read-only, "
                    "with a button to DM them.",
                ],
            ),
            (
                "Things people look for that aren't there",
                [
                    (
                        _B,
                        "Changing your password while signed in — there's no "
                        "screen for it. Use the forgotten-password link on "
                        "the sign-in page, which emails you a reset.",
                    ),
                    (
                        _B,
                        "Changing your email address — not available in the "
                        "app; your address is shown read-only on your "
                        "profile.",
                    ),
                    (
                        _B,
                        "A timezone picker — deliberately absent. Genos reads "
                        "your timezone from your browser and follows it, so "
                        "due dates and digests land on your clock wherever "
                        "you are.",
                    ),
                    (
                        _B,
                        "Task table columns and sorting — these live on the "
                        "task table itself, in its column settings, not in "
                        "Settings.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Keyboard shortcuts, in full",
        _body(
            (
                "Two conventions, and why keys differ",
                [
                    "Where a shortcut uses a modifier, Mac uses Cmd and "
                    "Windows/Linux generally uses Ctrl or Alt — each shortcut "
                    "below says which. Settings → Shortcuts shows the same "
                    "list inside the app.",
                ],
            ),
            (
                "Anywhere in Genos",
                [
                    (
                        _B,
                        "Cmd-K (Ctrl-K) — open Spotlight. On the Genos page "
                        "it puts the cursor in that page's own box instead of "
                        "opening a second one on top.",
                    ),
                    (
                        _B,
                        "Escape — close Spotlight. Escape generally closes "
                        "whatever is topmost: a menu, a window, or an "
                        "@-mention list before the window holding it.",
                    ),
                    (
                        _B,
                        "Switch surface: hold Cmd (hold Alt on "
                        "Windows/Linux) and tap Ctrl. Each tap moves through "
                        "Inbox, Chat, Tasks, Notes and Genos in the order you "
                        "last used them, so one tap is \"back to where I "
                        "was\". Add Shift to go backwards, release the held "
                        "key to commit, or press Escape to cancel and stay "
                        "put.",
                    ),
                ],
            ),
            (
                "Jump straight to an action",
                [
                    "Hold Ctrl and Cmd together (Ctrl and Alt on "
                    "Windows/Linux), then a letter:",
                    (_B, "T — go to Tasks and start a new task."),
                    (_B, "N — go to Notes and start a new personal note."),
                    (_B, "C — the compact calendar."),
                    (_B, "M — create a Meet link and copy it to your clipboard."),
                    (_B, "H — the History window."),
                    (_B, "G — the dependency graph for the task you're previewing."),
                ],
            ),
            (
                "In the Spotlight / Genos box",
                [
                    (_B, "Up and Down — move through the results."),
                    (
                        _B,
                        "Enter — open the highlighted result as a preview. "
                        "With nothing highlighted, Enter sends your question "
                        "instead.",
                    ),
                    (
                        _B,
                        "Shift-Enter — a new line inside a long question, "
                        "without sending it.",
                    ),
                    (
                        _B,
                        "Cmd-click or Ctrl-click a result — go to the real "
                        "page rather than previewing it.",
                    ),
                ],
            ),
            (
                "In chat",
                [
                    (
                        _B,
                        "Cmd-Enter or Ctrl-Enter — send. Either modifier "
                        "works on every platform. Plain Enter makes a new "
                        "line, on purpose: it stops half-finished messages "
                        "being sent.",
                    ),
                    (
                        _B,
                        "The same combination sends a thread reply, saves an "
                        "edit, and posts a task comment.",
                    ),
                    (
                        _B,
                        "Cmd-click (or Alt-click) a message — open its thread "
                        "without leaving the conversation.",
                    ),
                    (
                        _B,
                        "Cmd-Shift-Left/Right (Alt-Shift on Windows/Linux) — "
                        "move between chat tabs; with Up/Down instead, move "
                        "through the conversation list. Both are ignored "
                        "while you're typing in a field.",
                    ),
                ],
            ),
            (
                "In tasks and notes",
                [
                    (
                        _B,
                        "Quick-add row: Enter creates the task, Escape "
                        "discards the row.",
                    ),
                    (
                        _B,
                        "The full create-task form: Cmd-Enter on Mac, "
                        "Ctrl-Enter on Windows/Linux. This one is strict "
                        "about the platform, unlike the chat composer which "
                        "takes either.",
                    ),
                    (
                        _B,
                        "In any editor, three backticks then Enter starts a "
                        "code block.",
                    ),
                    (
                        _B,
                        "In a title field, Enter finishes editing and saves.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Plans, limits, and what happens when you hit one",
        _body(
            (
                "Where to look",
                [
                    "Settings → Plan & Usage shows your plan and your live "
                    "usage; the plans page compares tiers and handles "
                    "upgrades through Stripe. A plan applies to the whole "
                    "team, and only the team's owner sees the billing "
                    "section. The figures below are the shape of the tiers — "
                    "trust the Usage page for your own current numbers.",
                ],
            ),
            (
                "What each plan includes",
                [
                    (
                        _B,
                        "Free — 20 AI asks and 10 web searches a day, 50 "
                        "tasks and 50 notes a month, 180 days of message "
                        "history, uploads up to 5 MB, 30 days of Genos "
                        "conversation history, no integrations.",
                    ),
                    (
                        _B,
                        "Core — 100 asks and 25 web searches a day, 200 tasks "
                        "and notes a month, a year of message history, 25 MB "
                        "uploads, 180 days of Genos history, web search and "
                        "Google Calendar.",
                    ),
                    (
                        _B,
                        "Pro — 250 asks and 60 web searches a day, 500 tasks "
                        "and notes a month, unlimited message history, 50 MB "
                        "uploads, a year of Genos history, GitHub as well, "
                        "the highest effort levels, team-wide agent memory, a "
                        "weekly proactive digest, and MCP.",
                    ),
                    (
                        _B,
                        "Max — 500 asks and 150 web searches a day, unlimited "
                        "tasks and notes, 100 MB uploads, unlimited Genos "
                        "history, and a daily digest.",
                    ),
                    (
                        _B,
                        "Enterprise — unlimited asks and searches, 200 MB "
                        "uploads, arranged by contacting us rather than "
                        "bought in-app.",
                    ),
                ],
            ),
            (
                "Credits",
                [
                    (
                        _B,
                        "AI work also draws on a monthly credit allowance "
                        "that comes with the plan: 5 on Free, 30 on Core, 70 "
                        "on Pro, 150 on Max. Asking, summarizing a thread and "
                        "summarizing a note are the things that cost.",
                    ),
                    (
                        _B,
                        "You can buy extra credits in packs of 10, 50 or 100. "
                        "Bought credits don't expire at the end of the month "
                        "the way the monthly allowance does.",
                    ),
                    (
                        _B,
                        "A single request is capped at 5 credits, so no one "
                        "question can quietly eat the month.",
                    ),
                ],
            ),
            (
                "Hitting a limit",
                [
                    (
                        _B,
                        "Daily limits reset the next day; nothing is lost, "
                        "and the work you already did stays.",
                    ),
                    (
                        _B,
                        "History limits hide rather than delete. Messages and "
                        "past Genos conversations older than your window stop "
                        "appearing, and upgrading brings them back — they were "
                        "never thrown away.",
                    ),
                    (
                        _B,
                        "An upload refused as too large is your plan's "
                        "per-file cap, not a broken file. The cap is per file, "
                        "not per day.",
                    ),
                    (
                        _B,
                        "What Genos may do for you also varies by plan: "
                        "reading on Free, creating and updating from Core, "
                        "and larger reorganizing work above that. It always "
                        "asks before writing anything, on every plan.",
                    ),
                ],
            ),
        ),
    ),
    (
        "Tips, shortcuts, and getting unstuck",
        _body(
            (
                "Fast moves",
                [
                    (
                        _B,
                        "Cmd-K (Ctrl-K) — Spotlight, from anywhere. The "
                        "single most useful habit in Genos.",
                    ),
                    (
                        _B,
                        "Paste a Genos URL into any message and it renders "
                        "as a live preview card; clicking opens it in a "
                        "modal so you never lose your place.",
                    ),
                    (
                        _B,
                        'A thread can be linked to a task, so "we should '
                        'do this" conversations become tracked work in '
                        "two clicks.",
                    ),
                    (
                        _B,
                        "The sprint board, task table, and task graph are "
                        "three views of the same tasks — use the one that "
                        "fits the moment.",
                    ),
                ],
            ),
            (
                "When something seems off",
                [
                    (
                        _B,
                        "Not getting notifications? Check the browser "
                        "permission chip in Settings → Notifications, and "
                        "on iPhone make sure Genos is installed to the "
                        "home screen.",
                    ),
                    (
                        _B,
                        "Can't see a project or note? You may not be a "
                        "member — ask the owner to add you, or use the "
                        "request-access flow where offered.",
                    ),
                    (
                        _B,
                        "Something looks stale? Reload the tab first — "
                        "long-lived tabs keep running old code after an "
                        "update.",
                    ),
                ],
            ),
            (
                "Remember",
                [
                    "These guide notes are searchable by Spotlight, so the "
                    "fastest way to use them is to not read them: just ask "
                    'Genos — "how do I mute a chat?", "what do Editors '
                    'have permission to do?", "how do sprints work?" — '
                    "and it will answer with the relevant page cited.",
                    "Questions Genos can't answer: genos.support@genosai.dev",
                ],
            ),
        ),
    ),
    (
        "Fixes for specific problems",
        _body(
            (
                "Notifications",
                [
                    (
                        _B,
                        "Nothing arriving at all — check the browser "
                        "permission chip in Settings → Notifications first. "
                        "If the browser itself is blocking Genos, no setting "
                        "in the app can override that.",
                    ),
                    (
                        _B,
                        "Nothing on an iPhone — Safari only delivers push to "
                        "an installed app. Add Genos to your home screen, "
                        "open it from there, and allow notifications.",
                    ),
                    (
                        _B,
                        "Quiet for one conversation only — it's probably "
                        "muted. Settings → Notifications lists every mute you "
                        "hold, chats and threads/tasks/notes alike, and "
                        "unmutes from there.",
                    ),
                    (
                        _B,
                        "Too much email — push and email have separate "
                        "category toggles, so you can keep the in-app pings "
                        "and turn the mail off. Every notification email also "
                        "has an unsubscribe link, and the digest has its own "
                        "opt-out.",
                    ),
                    (
                        _B,
                        "A push you expected showed up as a quiet toast "
                        "instead — that's deliberate. The screen you're "
                        "actively looking at gets a toast; your other devices "
                        "still get the push.",
                    ),
                ],
            ),
            (
                "Access",
                [
                    (
                        _B,
                        "A project or note won't open — you're probably not a "
                        "member. Where it's offered, use request access: the "
                        "owner approves or declines from their Inbox.",
                    ),
                    (
                        _B,
                        "You can't invite anyone — inviting needs owner or "
                        "editor. Ask an owner to make you an editor.",
                    ),
                    (
                        _B,
                        "The owner has gone quiet — an editor can file an "
                        "ownership claim, and if the owner doesn't respond "
                        "within 30 days ownership transfers. A team can't be "
                        "stranded by one absent account.",
                    ),
                    (
                        _B,
                        "Account deletion is refused — you still own a team "
                        "with other people in it. Transfer that team, then "
                        "delete.",
                    ),
                ],
            ),
            (
                "Genos and search",
                [
                    (
                        _B,
                        "It can't find something you just made — indexing "
                        "takes a moment. Retry shortly.",
                    ),
                    (
                        _B,
                        "It says it can't see something you can see — check "
                        "you're in the team you think you're in; the sidebar's "
                        "team name at the top switches teams, and each team's "
                        "content is separate.",
                    ),
                    (
                        _B,
                        "It answered from something out of date — the guide "
                        "and your notes are ordinary notes that you can edit. "
                        "Fix the note and the answer follows.",
                    ),
                    (
                        _B,
                        "You've run out of asks for today — the count resets "
                        "tomorrow, and Settings → Plan & Usage shows where you "
                        "stand.",
                    ),
                    (
                        _B,
                        "A model you want is greyed out — some are listed as "
                        "coming soon, and the deeper effort levels need a "
                        "higher plan.",
                    ),
                ],
            ),
            (
                "Everything else",
                [
                    (
                        _B,
                        "An upload was refused — it's over your plan's "
                        "per-file size cap.",
                    ),
                    (
                        _B,
                        "The page looks stale or a button does nothing — "
                        "reload the tab. A tab left open for days keeps "
                        "running the code it started with.",
                    ),
                    (
                        _B,
                        "You deleted a note's content by accident — open its "
                        "version history and restore an earlier revision. "
                        "Revisions are recorded automatically.",
                    ),
                    (
                        _B,
                        "You deleted this guide and want it back — it's "
                        "deliberately not re-created on its own, so ask "
                        "support to restore it.",
                    ),
                    (
                        _B,
                        "Still stuck: genos.support@genosai.dev",
                    ),
                ],
            ),
        ),
    ),
    (
        "Playbooks: how different kinds of work fit Genos",
        _body(
            (
                "How to read this page",
                [
                    "Genos doesn't assume you write software. Below is one "
                    "starting shape per kind of work — pick the closest, "
                    "ignore the rest. They all use the same three pieces "
                    "(chat, tasks, notes) arranged differently, so nothing "
                    "here needs a setting you don't already have.",
                ],
            ),
            (
                "Engineers",
                [
                    (
                        _B,
                        "One project per service or repo. Connect GitHub so "
                        "pull requests link to tasks, and turn on closing a "
                        "task when its PR merges — then the board keeps "
                        "itself current.",
                    ),
                    (
                        _B,
                        "Use dependencies for real blockers: mark B blocked by "
                        "A and Genos sets and clears B's Blocked status "
                        "itself. The graph view shows what's actually holding "
                        "the release.",
                    ),
                    (
                        _B,
                        "Keep design docs as task notes so the plan sits on "
                        "the work, and ask Genos things like \"what changed "
                        'on #API this week?" instead of reading the whole '
                        "channel.",
                    ),
                ],
            ),
            (
                "Product and project managers",
                [
                    (
                        _B,
                        "Milestones for dated outcomes, sprints for cadence, "
                        "and the task table when you need a spreadsheet view. "
                        "Save the filters you keep rebuilding as named filter "
                        "sets and share them with the project.",
                    ),
                    (
                        _B,
                        "Define the custom fields your process needs and mark "
                        "them required, so tasks can't arrive half-specified.",
                    ),
                    (
                        _B,
                        "Task Weight, capacity and velocity views answer \"is "
                        'this plan real?"; a proactive digest on the higher '
                        "plans surfaces overdue and stale work before a "
                        "standup does.",
                    ),
                    (
                        _B,
                        'Ask for status rather than assembling it: "what\'s at '
                        'risk before Friday?", "what did we close last '
                        'sprint?"',
                    ),
                ],
            ),
            (
                "Marketing",
                [
                    (
                        _B,
                        "A project per campaign or channel, with a milestone "
                        "per launch date. Tags for medium — blog, email, "
                        "social, paid — then filter to one at a time.",
                    ),
                    (
                        _B,
                        "Draft copy in notes so several people can write in "
                        "the same document at once, and keep the brief as the "
                        "task note above the drafts.",
                    ),
                    (
                        _B,
                        "The campaign's channel keeps approvals attached to "
                        "the work; pin the final approved asset to the channel "
                        "so nobody ships last week's version.",
                    ),
                ],
            ),
            (
                "Research and analysis",
                [
                    (
                        _B,
                        "One note per source or interview, in a folder per "
                        "study, and one task per open question. Notes are the "
                        "material; tasks are what you still owe.",
                    ),
                    (
                        _B,
                        "Because Genos cites its sources, asking across a "
                        "folder of interviews gives you an answer you can "
                        "check — click through to the exact note before you "
                        "quote it.",
                    ),
                    (
                        _B,
                        "Ask a single long document its own questions from the "
                        "note's Ask button, and save the answer as a new note "
                        "to build a summary layer over your raw material.",
                    ),
                ],
            ),
            (
                "Students",
                [
                    (
                        _B,
                        "A project per course, a milestone per exam or "
                        "deadline, and lecture notes in a folder per subject. "
                        "Due dates put every course on one calendar.",
                    ),
                    (
                        _B,
                        "The daily to-do pane beside chat suits study "
                        "sessions: it's a per-day checklist, separate from "
                        "the real coursework tasks.",
                    ),
                    (
                        _B,
                        "Ask your own notes questions before an exam — "
                        '"explain what I wrote about elasticity" — and use '
                        "group chats for study groups.",
                    ),
                ],
            ),
            (
                "Operations, back office, and support",
                [
                    (
                        _B,
                        "Recurring procedures belong in notes as checklists, "
                        "with a task per run. Reusable task body templates "
                        "mean each run starts from the same steps instead of "
                        "someone's memory.",
                    ),
                    (
                        _B,
                        "One project per process — onboarding, invoicing, "
                        "vendor reviews — with required fields for the "
                        "details that must never be missing.",
                    ),
                    (
                        _B,
                        "Flag a message to turn a request that arrived in chat "
                        "into a personal follow-up, and turn the ones that "
                        "need tracking into tasks so they stop living in "
                        "someone's head.",
                    ),
                    (
                        _B,
                        "Every task keeps an activity trail of who changed "
                        "what and when, which is usually what an audit is "
                        "asking for.",
                    ),
                ],
            ),
            (
                "Whatever the work is",
                [
                    "The pattern underneath all of these: discussion in chat, "
                    "commitments as tasks, durable knowledge in notes — and "
                    "Genos reading all three so you can ask instead of "
                    "hunting. Start with one real project rather than a "
                    "perfect structure; the structure is easy to change "
                    "later, and an empty workspace teaches you nothing.",
                ],
            ),
        ),
    ),
    (
        "Connecting Genos to other tools: API, MCP, and realtime",
        _body(
            (
                "Who this page is for",
                [
                    "This page is for anyone connecting Genos to something "
                    "else — a script, another app, or an AI coding agent. "
                    "One part needs no programming at all: you paste one "
                    "address into an AI coding agent's settings, and it can "
                    "then work your tasks directly. If none of that is you, "
                    "skip this page; nothing else in Genos depends on it.",
                    "An API key acts as the person who created it, so it "
                    "only ever sees what that person can see, and the "
                    "realtime stream acts as whoever is signed in. Webhooks "
                    "are the exception: a team owner or editor sets one up "
                    "for the whole team, and it carries that team's events "
                    "whoever created it.",
                ],
            ),
            (
                "Getting a key",
                [
                    (
                        _B,
                        "Create keys in Settings → Developer. A key is shown "
                        "once, when you make it — copy it then, because "
                        "there is no readable copy afterwards. Revoke one "
                        "any time and it stops working on the next request.",
                    ),
                    (
                        _B,
                        "Pick read or write. A read key sees what you can "
                        "see and changes nothing; a write key can also "
                        "create and update.",
                    ),
                    (
                        _B,
                        "Keys made on that screen are personal: they act as "
                        "you across every team you belong to, so each "
                        "request has to name which team it means. "
                        "Team-scoped keys, which carry their own team, "
                        "exist in the API but cannot yet be created here.",
                    ),
                ],
            ),
            (
                "Which one to reach for",
                [
                    (
                        _B,
                        "The REST API — for a script or another app that "
                        "reads or creates Genos data on demand: your "
                        "projects, their tasks, and single tasks to create "
                        "or update. Use it for reports, imports, and "
                        "anything you run on a schedule.",
                    ),
                    (
                        _B,
                        "Webhooks — when you would rather Genos told you. A "
                        "team owner or editor gives it a public https "
                        "address, and it posts there as tasks change, or as "
                        "messages arrive in the channels you name, so you "
                        "hear about changes as they happen instead of "
                        "checking on a timer.",
                    ),
                    (
                        _B,
                        "MCP (Model Context Protocol) — when you want an AI "
                        "coding agent to work your tasks directly instead of "
                        "you pasting descriptions into it. Add Genos to an "
                        "MCP client as an HTTP server at "
                        "https://api.genosai.dev/api/public/v1/mcp"
                        "?team_id=<your team's id> — no trailing slash "
                        "before the question mark — sending the header "
                        "Authorization: ApiKey <your key>. The scheme is the "
                        "literal word ApiKey, not Bearer. The agent can then "
                        "read tasks itself and, with a write key, update "
                        "them and comment back.",
                    ),
                    (
                        _B,
                        "The realtime stream — for something live: a "
                        "dashboard, or a bot that reacts the moment a "
                        "message arrives. It carries the same live events "
                        "the Genos app itself uses — messages, reactions, "
                        "who has read what, who is online — and signs in "
                        "with your Genos login rather than an API key. That "
                        "is the dividing line: realtime where a person is "
                        "signed in, webhooks for one server talking to "
                        "another.",
                    ),
                ],
            ),
            (
                "Checking it works",
                [
                    "Start with GET /api/public/v1/me/. It reports the "
                    "person, the team, and the scope your key acts with, so "
                    "you learn the key is live before something subtler "
                    "fails. The team comes back empty for a personal key — "
                    "that is your cue that you have to name a team on each "
                    "request yourself.",
                    (
                        _B,
                        "Settings → Developer also links to the Genos "
                        "Developers page — the full reference for the REST "
                        "API, MCP, webhooks, and every realtime event, with "
                        "working examples.",
                    ),
                ],
            ),
        ),
    ),
    (
        "API keys and webhooks, step by step",
        _body(
            (
                "Making a key",
                [
                    "Settings → Developer, then create a key. You give it a "
                    "name, choose whether it may only read or also write, and "
                    "optionally an expiry anywhere from 1 day to 10 years. "
                    "Keys begin with gnos_ so they're recognisable in a log.",
                    (
                        _B,
                        "The key is shown once, at that moment. Copy it "
                        "straight into wherever it's going to live; there is "
                        "no way to read it again afterwards, only to delete it "
                        "and make another.",
                    ),
                    (
                        _B,
                        "It acts as you — anything you can see, it can see. "
                        "Treat it like your password: never in a public "
                        "repository, never in code that runs in a browser.",
                    ),
                    (
                        _B,
                        "Deleting a key stops it working on its very next "
                        "request, so it's the right response to a key you "
                        "think has leaked.",
                    ),
                ],
            ),
            (
                "Using it",
                [
                    "Send the header Authorization: ApiKey <your key>. The "
                    "scheme is the literal word ApiKey — Bearer is for the "
                    "app's own sign-in and is refused here, and equally a key "
                    "cannot be used to create or delete keys. That separation "
                    "is deliberate: a leaked key can't mint more of itself.",
                    (
                        _B,
                        "GET /api/public/v1/me/ — who the key acts as, and "
                        "with what scope. Start here to prove the key works "
                        "before debugging anything subtler.",
                    ),
                    (
                        _B,
                        "GET /api/public/v1/projects/ — your projects.",
                    ),
                    (
                        _B,
                        "GET and POST /api/public/v1/tasks/ — list tasks, or "
                        "create one.",
                    ),
                    (
                        _B,
                        "GET and PATCH /api/public/v1/tasks/<id>/ — read or "
                        "update a single task.",
                    ),
                    (
                        _B,
                        "GET /api/public/v1/openapi.json — the machine-readable "
                        "description of all of the above, and the one endpoint "
                        "that needs no key at all.",
                    ),
                    (
                        _B,
                        "Each key is limited to 120 requests a minute. A "
                        "personal key belongs to you rather than to one team, "
                        "so requests have to say which team they mean — that's "
                        "why me/ comes back with no team attached.",
                    ),
                ],
            ),
            (
                "Webhooks",
                [
                    "A webhook is Genos telling your server, instead of your "
                    "script asking on a timer. A team owner or editor adds an "
                    "https address in Settings → Developer and picks the "
                    "events it should receive:",
                    (_B, "task.created, task.updated, task.completed"),
                    (_B, "task.comment_created"),
                    (
                        _B,
                        "message.created — and this one only ever covers "
                        "channels you name explicitly. An empty list means no "
                        "chat events, not all of them, and direct messages can "
                        "never be sent to a webhook at all.",
                    ),
                    (
                        _B,
                        "Deliveries are recorded, so you can see what was "
                        "actually sent. If your endpoint fails 10 times in a "
                        "row Genos disables it rather than hammering a dead "
                        "server — fix the endpoint and turn it back on.",
                    ),
                ],
            ),
            (
                "MCP, for AI coding agents",
                [
                    "On Pro and above, point an AI coding agent at Genos and "
                    "it can work your tasks directly. Add it as an HTTP MCP "
                    "server at https://api.genosai.dev/api/public/v1/mcp"
                    "?team_id=<your team's id> — no trailing slash before the "
                    "question mark — with the same ApiKey header as above. The "
                    "agent gets tools for reading tasks and "
                    "notes and searching the workspace; with a write key it "
                    "can also create tasks, update them and comment back. "
                    "Natural-language queries beat keywords in its search "
                    "tool, the same as they do for you.",
                ],
            ),
            (
                "Things that don't exist yet",
                [
                    (
                        _B,
                        "Public links. Nothing in Genos can be shared with "
                        "someone who isn't signed in and permitted — a link "
                        "you paste anywhere still checks the reader's access.",
                    ),
                    (
                        _B,
                        "Spreadsheet export. Settings → Account exports your "
                        "own data as JSON, and a note exports as Markdown from "
                        "its menu, but there's no CSV of tasks or chat.",
                    ),
                    (
                        _B,
                        "Slack, Notion, Jira and Asana connections. Google and "
                        "GitHub are the two integrations that exist; links to "
                        "other tools pasted in a task are just links.",
                    ),
                ],
            ),
            (
                "The full reference",
                [
                    "Settings → Developer links to the Genos Developers page, "
                    "which is the complete specification — every field, every "
                    "event payload, and working examples — for all of the "
                    "above.",
                ],
            ),
        ),
    ),
]


def seed_guide_notes(user, team, *, force: bool = False) -> bool:
    """Create the Genos Guide folder + notes for (user, team).

    Returns True when it seeded, False when the membership stamp (or the
    folder itself) says it already happened — so a user who deleted the
    guide keeps it deleted. `force=True` is the deliberate re-seed lever
    for support/shell use. Raises on failure; callers treat seeding as
    best-effort.
    """
    membership = TeamMembers.objects.filter(team=team, attendee=user).first()
    if not force and membership is not None and membership.guide_seeded_at is not None:
        return False
    if PersonalNoteFolder.objects.filter(team=team, owner=user, name=GUIDE_FOLDER_NAME).exists():
        # Folder already there (e.g. seeded before the stamp existed) —
        # backfill the stamp so future calls short-circuit cheaply.
        if membership is not None and membership.guide_seeded_at is None:
            membership.guide_seeded_at = timezone.now()
            membership.save(update_fields=["guide_seeded_at", "ts_updated_at"])
        return False

    with transaction.atomic():
        folder = PersonalNoteFolder.objects.create(team=team, owner=user, name=GUIDE_FOLDER_NAME)
        for title, body in GUIDE_NOTES:
            note = PersonalNoteMaster.objects.create(
                team=team,
                owner=user,
                title=title,
                body=body,
                folder_id=folder.folder_id,
            )
            # The note APIs 403 without an explicit owner-role row, even
            # for the owner (note_type 1 = personal, role 1 = owner).
            NotePermissionMaster.objects.create(
                team=team, user=user, note_id=note.note_id, note_type=1, role_id=1
            )
        if membership is not None:
            membership.guide_seeded_at = timezone.now()
            membership.save(update_fields=["guide_seeded_at", "ts_updated_at"])
    log.info("[guide] seeded %d guide notes user=%s team=%s", len(GUIDE_NOTES), user.pk, team.pk)
    return True
