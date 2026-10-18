"""The Genos Guide — English and Japanese user-manual notes per user and team.

Why NOTES, and why MY-notes: Spotlight's retrieval is ACL-scoped
OpenSearch — whatever a user can see, Genos can find and cite. Seeding
the manual as notes therefore makes "how do I …?" a question Spotlight
can answer with citations. Placement follows from the ACL:

  * TEAM notes would give one shared copy — but anyone can edit it and
    an editor can delete it, taking the manual away from everyone.
  * The OWNER's my-notes would be safe — but invisible to every
    teammate's Spotlight (my-notes are private), so only the owner
    could ask about Genos.
  * PER-USER my-notes give every member their own private copies.
    My-notes are team-scoped and Spotlight runs in a team context, so
    per-(user, team) is exactly the retrieval-correct grain; and since
    search ACL means each user only ever sees their own copy, the
    duplication can never pollute anyone's ranking.

Seeded from `TeamMembersView.post` (the client calls /team/join/ both
after creating a team and on every team switch, so existing users pick
the guide up on their next switch) and from the starter seeder.
Idempotency is the `TeamMembers.guide_seeded_at` stamp — durable on
purpose: personal-folder deletion is a HARD recursive delete, so a user
who deleted both editions is not re-ambushed on every team switch.
If one edition exists and the other is missing, the missing language is
repaired; this also adds Japanese for users who received the English-only
guide before the Japanese edition shipped. Re-seeding both deleted
editions is a deliberate `force=True` support action.

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
                ],
            ),
            (
                "Finding an answer you already had",
                [
                    "The Genos page keeps a history list beside the "
                    "conversation, so a question you asked last week is "
                    "something to reopen rather than ask again. It shows your "
                    "twenty most recent conversations, and two things reach "
                    "past that:",
                    (
                        _B,
                        "The search box above the list matches the text of "
                        "your past questions — every question in a "
                        "conversation, not only the first one shown on the "
                        "row. So a match can surface a conversation whose "
                        "visible label doesn't contain your words, which is "
                        "usually right: the question you remember asking was "
                        "often several turns in. It searches your whole "
                        "history, not just the twenty on screen.",
                    ),
                    (
                        _B,
                        "Pin a conversation with the pin on its row and it "
                        "stays at the top under Pinned, above Recent, however "
                        "old it gets. Pinning is the fix for an ask you'll "
                        "want repeatedly — a decision, a summary you keep "
                        "re-reading — because pinned conversations are exempt "
                        "from that twenty-row limit. Unpin from the same pin.",
                    ),
                    (
                        _B,
                        "Pinning does not extend how long history is kept. "
                        "Your plan's Genos history window still applies, so a "
                        "conversation old enough to fall outside it stops "
                        "appearing even when pinned — and comes back, still "
                        "pinned, if you upgrade. Nothing was deleted.",
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
                    (
                        _B,
                        "In-app — toasts and the Inbox Activities feed while you're using Genos.",
                    ),
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
        "Working with another company: cross-team sharing",
        _body(
            (
                "What this is for",
                [
                    "Sometimes the people you need in a chat, a project, or a "
                    "folder of notes work for someone else — a client, an "
                    "agency, a contractor. Genos lets you share one specific "
                    "thing with another team without letting them into the "
                    "rest of your workspace, and without anyone creating a "
                    "second account.",
                    "It works in two steps: connect the two teams once, then "
                    "share individual items. Connecting on its own gives the "
                    "other team access to nothing at all.",
                ],
            ),
            (
                "Step 1: connect the two teams",
                [
                    (
                        _B,
                        "Ask the other company for their team ID. It is on "
                        "their team profile — the team menu at the top of the "
                        'left sidebar, then "Show Team Profile" — under Team '
                        "ID, and it is safe to send in an email: on its own it "
                        "grants nothing.",
                    ),
                    (
                        _B,
                        "Open your own team profile the same way, find "
                        '"Connected teams", paste their ID and choose Request '
                        "connection. It appears in your list straight away, "
                        'marked "Waiting for them to approve".',
                    ),
                    (
                        _B,
                        "The request goes to their team OWNER, who finds it in "
                        'Inbox → Requests as a "Team Connection" card with '
                        "Approve and Reject on it. Their owner or an editor "
                        'can also answer it from "Connected teams" in their '
                        "own team profile — the same request, either place.",
                    ),
                    (
                        _B,
                        "Once approved, both teams see the other in Connected "
                        'teams, labelled "You invited them" or "They invited '
                        'you" so it is always clear who asked whom. You only '
                        "do this once per company, no matter how much you go "
                        "on to share.",
                    ),
                    (
                        _B,
                        "Only an owner or editor can request or approve a "
                        "connection. Anyone on the team can see who you are "
                        "connected to.",
                    ),
                    (
                        _B,
                        "Nothing arrived? Check they gave you the team ID and "
                        "not their own user or project id, and that it is the "
                        "team OWNER looking — the card goes to them.",
                    ),
                ],
            ),
            (
                "Step 2: share one thing",
                [
                    (
                        _B,
                        "A chat — create a new group chat and turn on the "
                        "external option, then pick the connected teams to "
                        "invite. An external chat is always private, and "
                        "stays private; it shows an External badge in the "
                        "chat list.",
                    ),
                    (
                        _B,
                        "A project — open the project profile and use the "
                        '"Teams with access" section to share it with a '
                        "connected team. Everything in the project comes "
                        "along: its tasks, their comments, files, task "
                        "notes, milestones, and the project's own chat "
                        "channel.",
                    ),
                    (
                        _B,
                        "A folder of notes — open a Team Notes folder's "
                        "members dialog and share it there. The folder must "
                        "be set to private first: a public folder means "
                        '"everyone in my team", which is not a boundary you '
                        "can share across.",
                    ),
                    (
                        _B,
                        "When you share, you choose a ceiling: up to viewer, "
                        "or up to editor. That is the most the other team's "
                        "people can be given.",
                    ),
                    (
                        _B,
                        "The other team's owner gets it in Inbox → Requests as "
                        'a "Shared With Your Team" card, and accepts it there. '
                        "The card names the item and your team, and the person "
                        "who accepts is let in straight away — so they can "
                        "open it, look at it, and decide who else on their side "
                        "needs it.",
                    ),
                ],
            ),
            (
                "Where the shared item turns up",
                [
                    "This is the question everybody asks next, and the answer "
                    "is: in your own workspace, in the ordinary place for that "
                    "kind of thing. You do not go looking in the other "
                    "company's team for it.",
                    (
                        _B,
                        "A shared project appears in your own project list, "
                        "with a share icon next to its name; hover it to see "
                        "which team it belongs to.",
                    ),
                    (
                        _B,
                        "An external chat appears in your own chat list, "
                        "badged with the name of the team whose room it is.",
                    ),
                    (
                        _B,
                        "A shared note folder appears at the top level of your "
                        'Team Notes, marked "Shared" with the owning team\'s '
                        "name. Everything already inside it comes with it.",
                    ),
                    (
                        _B,
                        "Only people who were admitted see it. A colleague on "
                        "your team who has not been added sees nothing at all "
                        "— accepting the share does not put it in everyone's "
                        "sidebar.",
                    ),
                    (
                        _B,
                        "Nothing there? Somebody has to admit you. Ask whoever "
                        "on your team approved the share — the next section is "
                        "how they do it.",
                    ),
                ],
            ),
            (
                "Each side manages its own people",
                [
                    "This is the part that saves everyone time. Once a share "
                    "is accepted, the other team's owner or editor adds and "
                    "removes their own colleagues on that item whenever they "
                    "like — no request back to you, no approval, however long "
                    "afterwards. You approved the team and the item; they "
                    "staff their side of it.",
                    (
                        _B,
                        "How: open the item — the shared project's profile, "
                        "the external chat's profile, or the shared folder's "
                        'members dialog — and find "Teams with access". Your '
                        "own team's row has a person-plus button; it lists your "
                        "colleagues, and clicking one admits them.",
                    ),
                    (
                        _B,
                        "The ordinary member picker on a shared item is not the "
                        "way in. That one adds people to the OWNING team's "
                        'item, so it is not shown to you — "Teams with access" '
                        "replaces it.",
                    ),
                    (
                        _B,
                        "They can only add people who are actually members of "
                        "their team, and never above the ceiling you set.",
                    ),
                    (
                        _B,
                        "You keep a veto: you can remove any individual "
                        "participant from your item at any time. What you "
                        "cannot do is add their people for them.",
                    ),
                    (
                        _B,
                        "If someone leaves the other team, their access to "
                        "your shared items disappears with their membership. "
                        "You do not have to notice or do anything.",
                    ),
                ],
            ),
            (
                "Working inside a shared item",
                [
                    "Inside the thing that was shared, the two teams work as "
                    "one team. That is the whole point, and it is worth being "
                    "specific about, because the boundary is around the item "
                    "rather than inside it.",
                    (
                        _B,
                        "In a shared project, either side can create and edit "
                        "tasks, move them, comment, attach files, write task "
                        "notes, and set up milestones and sprints. Its project "
                        "chat works the same for everyone in it.",
                    ),
                    (
                        _B,
                        "In an external chat, everyone in the room sends "
                        "messages, replies in threads, reacts and mentions "
                        "each other normally.",
                    ),
                    (
                        _B,
                        "In a shared note folder, either side writes notes, "
                        "edits them together in real time, and creates "
                        "subfolders to organise the work. A subfolder you "
                        "create in someone else's shared folder is yours to "
                        "rename and delete.",
                    ),
                    (
                        _B,
                        "A viewer-ceiling share is the exception: those "
                        "participants read, and do not write.",
                    ),
                ],
            ),
            (
                "What stays with the owning team",
                [
                    "The team that owns the item keeps the decisions that are "
                    "about the item itself rather than the work in it:",
                    (
                        _B,
                        "Renaming or deleting the shared project, chat or "
                        "folder; moving the folder, or changing who in the "
                        "owning team can reach it.",
                    ),
                    (
                        _B,
                        "Sharing it onward. A team it was shared with cannot "
                        "pass it to a third company, and cannot invite the "
                        "owning team's own people into it.",
                    ),
                    (
                        _B,
                        "Ending the share. If they do, it disappears from your "
                        "workspace immediately — including anything your side "
                        "wrote inside it, which stays with them.",
                    ),
                ],
            ),
            (
                "What an outside participant does not see",
                [
                    (
                        _B,
                        "Only what was shared: the shared project, not the "
                        "owning team's other projects; the external chat, not "
                        "their other chats; the shared folder, not their other "
                        "notes. Never their member list.",
                    ),
                    (
                        _B,
                        "The owning team's other work does not become visible "
                        "by being nearby. A public folder in their Team Notes "
                        'means "everyone in their team", which is not you.',
                    ),
                    (
                        _B,
                        "Search and Genos follow the same boundary. They can "
                        "search and ask questions, and get answers only from "
                        "the items they were admitted to.",
                    ),
                    (
                        _B,
                        "They keep their own plan and their own AI limits — "
                        "their usage is never billed to you, and yours is "
                        "never billed to them.",
                    ),
                    (
                        _B,
                        "The owning team can also appear in their team menu "
                        'with a "Guest" label, which opens the same shared '
                        "items from the owner's side of the fence. It is an "
                        "extra way in, not the main one.",
                    ),
                ],
            ),
            (
                "Ending it",
                [
                    (
                        _B,
                        'Stop sharing one item from its "Teams with access" '
                        "section. Everyone that team had on it loses access "
                        "immediately.",
                    ),
                    (
                        _B,
                        "Disconnect the whole team from Connected teams. That "
                        "ends every chat, project, and folder shared between "
                        "you, in both directions, at once — so it asks you to "
                        "confirm, and tells you how many people were removed.",
                    ),
                    (
                        _B,
                        "Either side can end a share or a connection without "
                        "the other's agreement. You can reconnect later; the "
                        "individual shares do not come back on their own.",
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
                        "Privacy — whether to share anonymous usage analytics.",
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
                        "Message layout: roomy bubbles, or compact rows that fit more on screen.",
                    ),
                    (
                        _B,
                        "Your three one-click reaction emoji.",
                    ),
                    (
                        _B,
                        "Whether double-clicking a message turns it into a to-do.",
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
                        'last used them, so one tap is "back to where I '
                        'was". Add Shift to go backwards, release the held '
                        "key to commit, or press Escape to cancel and stay "
                        "put.",
                    ),
                ],
            ),
            (
                "Jump straight to an action",
                [
                    "Hold Ctrl and Cmd together (Ctrl and Alt on Windows/Linux), then a letter:",
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
                        "Shift-Enter — a new line inside a long question, without sending it.",
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
                        "Quick-add row: Enter creates the task, Escape discards the row.",
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
                        "In any editor, three backticks then Enter starts a code block.",
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
                        "An upload was refused — it's over your plan's per-file size cap.",
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
                        'the work, and ask Genos things like "what changed '
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
                        'Task Weight, capacity and velocity views answer "is '
                        'this plan real?"; a proactive digest on the higher '
                        "plans surfaces overdue and stale work before a "
                        "standup does.",
                    ),
                    (
                        _B,
                        "Ask for status rather than assembling it: \"what's at "
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
                    "name and choose whether it may only read or also write. "
                    "Every key expires — after 90 days unless you pick another "
                    "length, up to one year — and then you make a new one. "
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
                        "GET and POST /api/public/v1/tasks/ — list tasks, or create one.",
                    ),
                    (
                        _B,
                        "GET and PATCH /api/public/v1/tasks/<id>/ — read or update a single task.",
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
JAPANESE_GUIDE_FOLDER_NAME = "Genos ガイド"

JAPANESE_GUIDE_NOTES: list[tuple[str, list]] = [
    (
        "Genosの基本：画面構成とナビゲーション",
        _body(
            (
                "Genosとは",
                [
                    "Genosは、チャット、タスク、ノートを1つのワークスペースに統合したサービスで、これら3つすべてを読み取ることができるAIアシスタント「Spotlight」を搭載しています。チャットアプリ、タスク管理ツール、Wikiの間を行き来する必要はなく、すべてが1か所に集約され、相互に連携しています。チャットのスレッドをタスクに変えることができ、タスクには独自のノートやディスカッションが紐付けられ、Spotlightはこれらすべてを横断して質問に答えることができます。",
                    "このガイドは、2026年8月時点のGenosについて解説しています。このガイドは「マイノート」内に保存されるため、あなただけが閲覧できます。また、Spotlightがノートを読み取ることができるため、「マイルストーンの作成方法は？」といった質問をGenosに投げかけるだけで、これらのページから回答が得られます。",
                ],
            ),
            (
                "4つの主要画面",
                [
                    (
                        _B,
                        "「受信トレイ」：対応が必要な事項が届く場所です。「アクティビティ」タブにはアクティビティに関する通知（メンション、返信、リアクション）が、「リクエスト」タブには承認待ちの参加リクエストやアクセスリクエストが表示されます。",
                    ),
                    (
                        _B,
                        "チャット：ダイレクトメッセージ、グループチャット、およびプロジェクトごとに1つずつ自動的に作成されるチャンネル。",
                    ),
                    (_B, "タスク：プロジェクト、タスク、マイルストーン、スプリント、ボード。"),
                    (
                        _B,
                        "ノート：共同編集できるドキュメントです。非公開の「マイノート」、共有の「チームノート」フォルダー、タスクやチャットに添付されたノートがあります。",
                    ),
                    "左サイドバーのアイコンを使って、主要画面を切り替えます。複数のチームに所属している場合は、サイドバー上部のチーム名からチームを切り替えられます。",
                ],
            ),
            (
                "どこからでも使える機能",
                [
                    (
                        _B,
                        "画面のどこからでもCmd-K（WindowsではCtrl-K）を押すと、AIアシスタント「Spotlight」が開きます。",
                    ),
                    (
                        _B,
                        "スマートフォンでは、Genosはインストール可能なアプリです。ブラウザの「ホーム画面に追加」機能をご利用ください。iPhoneの場合、インストールを行うことでプッシュ通知も有効になります。",
                    ),
                    (
                        _B,
                        "Genosは7言語（English、日本語、中文、العربية、हिन्दी、Français、Español）に対応し、アラビア語では画面全体が右から左のレイアウトになります。言語は「設定」で選択できます。ライト／ダークテーマも「設定」で選べます。",
                    ),
                    (
                        _B,
                        "Genosのリンク（タスク、ノート、メッセージなど）をチャットに貼り付けると、クリック可能なプレビューとしてその場で表示され、現在の画面の位置を離れることなく確認できます。",
                    ),
                ],
            ),
        ),
    ),
    (
        "チャット：メッセージ、スレッド、メンション",
        _body(
            (
                "会話の種類",
                [
                    (
                        _B,
                        "ダイレクトメッセージ（DM）――1対1のやり取りに加え、ちょっとした雑談のための少人数でのDMも可能です。",
                    ),
                    (
                        _B,
                        "グループメッセージ（GM）：名前を付けて作成し、メンバーを招待するグループチャットです。参加希望者は参加をリクエストでき、リクエストは受信トレイから承認できます。",
                    ),
                    (
                        _B,
                        "プロジェクトチャンネル：すべてのプロジェクトに自動的に1つ作成されます。タスクのアクティビティはタスクカードとしてここに表示され、各カードには独自の返信スレッドが付いているため、議論が作業と密接に結びついた状態を維持できます。",
                    ),
                ],
            ),
            (
                "日常的なメッセージ操作",
                [
                    (
                        _B,
                        "サイドディスカッションがメインのスレッドの流れに混ざらないよう、スレッド内で返信してください。スレッドには専用のペインがあります。",
                    ),
                    (
                        _B,
                        "誰かを@メンションすると、その相手にはアプリ内、プッシュ通知、およびオフラインの場合はメールで通知が届きます。グループメンションを使えば、1つの@で指定したメンバー全員に通知を送ることができます。",
                    ),
                    (
                        _B,
                        "絵文字でリアクション：チーム独自のカスタム絵文字やGIFも使用できます。管理者はカスタム絵文字をアップロードできます。",
                    ),
                    (
                        _B,
                        "重要なメッセージをチャンネルに固定して、後で簡単に見つけられるようにしましょう。",
                    ),
                    (
                        _B,
                        "メッセージにフラグを立てて、個人的なフォローアップ事項として管理しましょう。処理が完了したらフラグを「完了」に設定し、過去のフラグはいつでも確認できます。",
                    ),
                    (
                        _B,
                        "（チャットの隣にある）ToDoペインには、実際のプロジェクトのタスクとは別に、1日ごとのシンプルな個人用チェックリストが表示されます。",
                    ),
                ],
            ),
            (
                "あとから見つける",
                [
                    (
                        _B,
                        "検索の対象はメッセージ、タスク、ノートです。キーワード検索だけでなく、Spotlightに自然な言葉で質問することも可能です。",
                    ),
                    (
                        _B,
                        "メッセージのリンクをクリックすると、その会話内のそのメッセージの箇所に直接移動します。",
                    ),
                    (
                        _B,
                        "「未読」フィルターやグループごとの個人用タグを活用すれば、情報量の多いサイドバーも整理しやすくなります。",
                    ),
                ],
            ),
        ),
    ),
    (
        "タスクとプロジェクト",
        _body(
            (
                "プロジェクト",
                [
                    "プロジェクトとは、タスク、マイルストーン、スプリント、および専用のチャットチャンネルをまとめる容器のようなものです。「タスク」サイドバーからプロジェクトを作成し、公開（チーム全員に表示）または非公開（メンバーのみ表示）に設定できます。各プロジェクトには短縮コードが割り当てられるため、タスクには「WRD-12」のような読みやすいIDが付与されます。",
                    (
                        _B,
                        "プロジェクトオーナーは、タスクごとにプロジェクト固有のカスタムフィールドを定義したり、特定のフィールドを必須項目に指定したり、再利用可能なタスク本文テンプレートを設定したりすることができます。",
                    ),
                    (
                        _B,
                        "プロジェクトラベルは、サイドバー内のプロジェクトをグループ分けし、サイドバーではラベルごとにフィルタリングすることができます。",
                    ),
                ],
            ),
            (
                "タスク",
                [
                    (
                        _B,
                        "タスクには、ステータス（「未処理」、「進行中」、「保留中」、「ブロック中」、「完了」）、優先度、担当者、任意の共同作業者、期限、タグ、およびその下にコメントが記載された詳細な説明があります。",
                    ),
                    (
                        _B,
                        "作業をサブタスクに分割します。タスクのページには、そのタスクのサブツリー全体が表示されます。",
                    ),
                    (
                        _B,
                        "依存関係：タスクBがタスクAによってブロックされていることを示します。Genosは、ブロックされているタスクのステータスを自動的に「ブロック中」に設定し、ブロック元が完了するとそのステータスを解除します。タスクグラフビューでは、依存関係の全体像が表示されます。",
                    ),
                    (
                        _B,
                        "「タスクの重み（1～25）」は、優先度と時間的プレッシャーを組み合わせたもので、「次に何をすべきか」という問いに対する答えとなるものです。チームのキャパシティやベロシティの把握は、これを基盤としています。",
                    ),
                    (
                        _B,
                        "コメントでは@メンションやリアクションが利用でき、タスクへの変更はすべて「アクティビティ」フィードに記録されます。これは、タスクごとの変更履歴となります。",
                    ),
                ],
            ),
            (
                "マイルストーンとスプリント",
                [
                    (
                        _B,
                        "マイルストーンは、特定の日付を目標とするタスクをまとめ、進捗状況を表示します。タスクを割り当てると、その完了状況を追跡します。",
                    ),
                    (
                        _B,
                        "スプリントは各プロジェクトに定期的なリズムをもたらします。スプリントボードには現在のスプリントのタスクがステータス別に表示され、タスクテーブルではフィルタリングや並べ替えが可能なスプレッドシート形式で表示されます。",
                    ),
                    (
                        _B,
                        "フィルター（担当者、ステータス、タグなど）は、名前を付けたフィルターセットとして保存し、プロジェクト内で共有することができます。",
                    ),
                ],
            ),
            (
                "GitHub",
                [
                    (
                        _B,
                        "「設定」→「連携」でGitHubを連携させると、プルリクエストをタスクにリンクできます。リンクされたタスクは、そのプルリクエストがマージされると自動的にクローズされます。",
                    ),
                ],
            ),
        ),
    ),
    (
        "ノート：個人、チーム、共有",
        _body(
            (
                "ノートの保存場所",
                [
                    (
                        _B,
                        "「マイノート」：あなただけのプライベートなノート（このガイドのように）。フォルダーやネストされたフォルダーを使って整理できます。",
                    ),
                    (
                        _B,
                        "チームフォルダー：チームと共有されるフォルダー。このフォルダーにはアクセスルールが設定されており、フォルダー内のすべてのファイルやフォルダーはそのルールに従います。",
                    ),
                    (
                        _B,
                        "タスクのノート：タスクに添付されます。プロジェクトのメンバーはこれを閲覧・編集できるため、計画と作業が密接に連携します。",
                    ),
                    (
                        _B,
                        "チャットノート：会話に添付され、議事録や決定事項を共有するためのものです。",
                    ),
                ],
            ),
            (
                "編集",
                [
                    (
                        _B,
                        "このエディタはブロックエディタです。見出し、箇条書きや番号付きリスト、チェックボックス、コードブロック、表、画像、および相手に通知を送る@メンションなどが利用できます。",
                    ),
                    (
                        _B,
                        "共有ノートはリアルタイムで共同編集が可能です。複数のユーザーが同時に編集でき、お互いの変更内容をその場で確認できます。",
                    ),
                    (
                        _B,
                        "バージョン履歴には変更内容が自動的に記録されます。これを開けば、各バージョンを比較したり、以前のバージョンを復元したりできます。したがって、コンテンツを削除しても、致命的な事態になることはほとんどありません。",
                    ),
                    (
                        _B,
                        "Markdownファイルをノートとしてインポートし、各ノートのメニューからMarkdown形式でエクスポートできます。",
                    ),
                ],
            ),
            (
                "共有とアクセス",
                [
                    (
                        _B,
                        "特定のユーザーをエディターやビューアーとして指定し、ノートを共有できます。開くことができないノートにアクセスした場合は、アクセス権をリクエストできます。オーナーは受信トレイから承認または拒否を行います。",
                    ),
                    (
                        _B,
                        "表示されているすべてのコンテンツは、キーワードやSpotlightを使って検索できます。",
                    ),
                ],
            ),
        ),
    ),
    (
        "Genos AI（Spotlight）：質問、操作、先回り",
        _body(
            (
                "質問する",
                [
                    "Cmd-K（Ctrl-K）を押して、平易な言葉で「今週の締め切りは？」「価格設定ページに関する議論をまとめて」「ログインのバグは誰が対応している？」などと尋ねてみてください。Spotlightは、チャット、タスク、ノート、ToDoリストなど――あくまであなた自身が見ることができるもののみ――を読み取り、クリックすれば出典元を開ける引用情報を返してくれます。",
                    (
                        _B,
                        "質問文の中で、@（人）や#（プロジェクト、タスク）を使って具体的に言及することで、Spotlightに正確な文脈を認識させることができます。",
                    ),
                    (
                        _B,
                        "また、個々のスレッドやノートにある「質問」ボタンから質問を送信したり、良い回答をノートとして保存したりすることもできます。",
                    ),
                ],
            ),
            (
                "操作を任せる",
                [
                    (
                        _B,
                        "Spotlightには、タスクやタスク計画の作成、タスクの一括更新、ノートの作成など、さまざまな機能もあります（ご利用のプランによって異なります）。",
                    ),
                    (
                        _B,
                        "書き込みを行うたびに、まず承認用のプレビューが表示されます。作成または変更される内容が正確に確認できるため、承認または却下を行うことができます。クリックしない限り、何も書き込まれません。",
                    ),
                    (
                        _B,
                        "エフォートレベルでは、速さと深さのバランス（すばやい回答から、複数段階の徹底した調査まで）を選べます。利用できるレベルはプランによって異なります。",
                    ),
                    (
                        _B,
                        "実行中のクエリは、ウィンドウを閉じても処理が継続されます。回答の準備が整うと通知が届きます。",
                    ),
                    (
                        _B,
                        "AIの利用量は、ご契約のプランに含まれる月間クレジットで計測されます。設定ページでは、これまでの利用状況を確認できます。",
                    ),
                ],
            ),
            (
                "Genosから届く情報",
                [
                    (
                        _B,
                        "上位プランでは、Genosが現地時間の午前8時に、期限切れのタスク、障害要因、未処理の項目などをまとめたダイジェストを、受信トレイに自動的に送信します。",
                    ),
                    (
                        _B,
                        "毎日配信されるメールダイジェスト（全プラン対象）には、まだご覧になっていないコンテンツの要約がまとめられています。どちらのダイジェストも、「設定」→「通知」からオフにすることができます。",
                    ),
                    (
                        _B,
                        "回答を👍/👎で評価してください。フィードバックは製品の改善につながります。",
                    ),
                ],
            ),
        ),
    ),
    (
        "検索と質問：より良い答えを得る方法",
        _body(
            (
                "1つのボックス、2つの使い方",
                [
                    "Cmd-Kを押すと、2つの機能を持つダイアログボックスが開きます。どちらの機能が表示されるかは、入力内容によって異なります。単語をいくつか入力すると、テキストが一致するメッセージ、タスク、ノート、ToDoのキーワード検索結果が即座に表示されます。質問文をそのままEnterキーで送信すると、Genosが一致する内容を読み取り、それに答えてくれます。検索は短く具体的に、質問は一文で入力してください。",
                ],
            ),
            (
                "検索を絞り込む",
                [
                    (
                        _B,
                        "ボックス下のチップでは、結果の種類（チャット、タスク、ノート、ToDo、またはGenosが以前に提供した回答）ごとに絞り込むことができます。また、プロジェクトフィルターや日付範囲の絞り込み機能もあります。",
                    ),
                    (
                        _B,
                        "クエリ構文はありません。引用符、AND、OR、先頭のマイナス記号、および「フィールド:値」はすべて通常の単語として認識されるため、フィルターを入力するよりもチップを使うほうがよいでしょう。検索はスペルだけでなく意味も一致させるため、たいていの場合、キーワードを推測するよりも、そのままのフレーズで検索したほうが良い結果が得られます。",
                    ),
                    (
                        _B,
                        "どちらのツールも、あなたのアカウントで既に開くことができる範囲に限定されています。チームメイトのプライベートノートや、あなたが参加していないチャンネルは、あなたにとってもGenosにとっても表示されることはありません。",
                    ),
                ],
            ),
            (
                "効果的な質問のしかた",
                [
                    (
                        _B,
                        "質問文の中で、@（人）や#（プロジェクト、タスク）を使って対象を明示しましょう。「金曜日までに#WRDで残っている作業は？」という書き方は、「残っている作業は？」という書き方よりも優れています。なぜなら、どの作業を指しているのかという曖昧さがなくなるからです。",
                    ),
                    (
                        _B,
                        "その資料がすでにどこにあるかを確認する：スレッドやノートにはそれぞれ専用の「質問」ボタンがあり、ワークスペース全体ではなく、その特定の会話やドキュメントに限定して質問を行うことができます。",
                    ),
                    (
                        _B,
                        "回答には出典が記載されています。いずれかをクリックすると出典元が開きます。回答の価値は引用元の内容によって決まるため、出典を確認することが重要です。",
                    ),
                    (
                        _B,
                        "検索結果をクリックすると、リスト上の現在の位置の上にプレビューが表示されるため、その位置を失うことはありません。Cmdキーを押しながらクリック（Ctrlキーを押しながらクリック）すると、代わりに実際のページが開きます。",
                    ),
                    (
                        _B,
                        "良い回答をノートとして保存すると、それ自体が検索対象になります。回答には👍/👎で評価を付けましょう。",
                    ),
                ],
            ),
            (
                "何かが見つからないとき",
                [
                    (
                        _B,
                        "新しいコンテンツは、検索対象になるまでに少し時間がかかります。作成したばかりのタスクが表示されない場合は、失われたと決めつける前に、少し待ってからもう一度試してみてください。",
                    ),
                    (
                        _B,
                        "Web検索はワークスペース検索とは別の機能です。これは「設定」→「Spotlight」にある切り替えスイッチで、Coreプラン以上で利用可能です。この機能が有効になっていない場合、Genosはワークスペース内の情報のみを回答します。",
                    ),
                ],
            ),
            (
                "過去の回答を見つける",
                [
                    "Genosページでは会話の横に履歴一覧が表示されるため、先週の質問も、もう一度尋ねるのではなく会話を開き直せます。直近20件の会話が表示され、それより前の会話には次の2つの方法でアクセスできます。",
                    (
                        _B,
                        "リストの上にある検索ボックスは、過去の質問のテキストと一致するものを検索します。これは、その行に最初に表示される質問だけでなく、会話内のすべての質問を対象としています。そのため、表示されているラベルに検索語が含まれていない会話でも、一致する結果として表示されることがあります。これは通常、正しい結果です。なぜなら、自分が尋ねたことを覚えている質問は、多くの場合、会話の何ターンも先にあるものだからです。検索対象は画面上に表示されている20件だけでなく、履歴全体です。",
                    ),
                    (
                        _B,
                        "会話の行にあるピンアイコンをクリックしてその会話を「ピン留め」すると、どれだけ時間が経っても「ピン留め済み」セクションの下、「最近の会話」の上部に表示され続けます。ピン留めは、繰り返し参照したい内容（決定事項や何度も読み返す要約など）に対する解決策となります。ピン留めされた会話は、20行という制限の対象外となるためです。ピン留めを解除するには、同じピンアイコンをクリックしてください。",
                    ),
                    (
                        _B,
                        "ピン留めをしても、履歴の保存期間は延長されません。ご利用のプランのGenos履歴保存期間の制限は引き続き適用されるため、その期間を超えて古い会話は、ピン留めしていても表示されなくなります。ただし、プランをアップグレードすると、その会話はピン留めされたまま再び表示されるようになります。データが削除されたわけではありません。",
                    ),
                ],
            ),
        ),
    ),
    (
        "通知：アプリ内、プッシュ、メール",
        _body(
            (
                "3つの通知方法",
                [
                    (
                        _B,
                        "アプリ内：Genosの利用中に表示されるトースト通知や「Inbox」アクティビティフィード。",
                    ),
                    (
                        _B,
                        "プッシュ通知：タブがバックグラウンドにある場合やアプリが閉じられている場合に、システム通知が届きます。「設定」→「通知」で許可してください。Genosはデバイスごとに賢く動作します。現在表示中の画面にはプッシュ通知ではなくトースト通知が表示され、他のデバイスには引き続きプッシュ通知が届きます。",
                    ),
                    (
                        _B,
                        "メール：外出中のために。Genosは、あなたがしばらく不在になったのを確認してから、見逃した内容をまとめて1通のメールとして送信します。イベントごとに1通ずつ送信されることはありません。アプリですでに読んだ内容は、メールから除外されます。",
                    ),
                ],
            ),
            (
                "通知を調整する",
                [
                    (
                        _B,
                        "「設定」→「通知」には、マスタースイッチ、カテゴリごとのトグル（メンション、スレッドへの返信、タスクへのコメント、リクエスト、すべてのメッセージ）、およびメール専用のトグルが別途用意されています。",
                    ),
                    (
                        _B,
                        "特定のチャットのヘッダーにあるベルアイコンをタップしてミュートしたり、スレッド、タスク、ノートの⋮メニューからミュートしたりできます。すべてのミュート設定は「設定」で管理できます。",
                    ),
                    (
                        _B,
                        "すべての通知メールには配信停止リンクが記載されています。また、毎日のメールダイジェストには、それとは別の配信停止リンクがあります。",
                    ),
                ],
            ),
        ),
    ),
    (
        "チーム、メンバー、役割",
        _body(
            (
                "メンバーシップ",
                [
                    (
                        _B,
                        "チームメニューからメールでチームメンバーを招待すると、招待されたメンバーにはチームページへアクセスできるリンクが送信されます。また、チームIDを使って参加リクエストを送ることも可能で、その場合は受信トレイから承認を行うことができます。",
                    ),
                    (
                        _B,
                        "Genosは各メンバーのタイムゾーンと言語を自動的に検知するため、期限、ダイジェスト、メールは各人の現地時間と言語に合わせて配信されます。",
                    ),
                ],
            ),
            (
                "役割",
                [
                    (
                        _B,
                        "オーナー：チームごとに1名（プロジェクトごと、グループチャットごとに1名）。そのアイテムを削除したり、他の誰かに引き継いだりできるのはオーナーのみです。",
                    ),
                    (
                        _B,
                        "エディター：日常的な管理業務：メンバーの招待や管理、名前の変更、アバターの変更、他のメンバーの役割の設定などを行います。チームが特定の1つのアカウントに依存しないように、早い段階で別のメンバーにもエディター権限を付与しましょう。",
                    ),
                    (
                        _B,
                        "「Viewer」：デフォルトのロール：完全な読み取り権限を持ち、チャットやタスクに参加できますが、メンバーの管理権限はありません。",
                    ),
                    (
                        _B,
                        "オーナーが不在となった場合、エディターは所有権の主張を行うことができます。オーナーが30日以内に応答しない場合、所有権は移転します。これは、チームが放置されるのを防ぐための安全策です。",
                    ),
                ],
            ),
            (
                "連携と請求",
                [
                    (
                        _B,
                        "Googleカレンダー（「設定」→「連携」）では、タスクの横に予定が表示されます。複数のGoogleアカウントを同時に利用でき、タスクをカレンダーに同期することも可能です。",
                    ),
                    (_B, "GitHubでは、プルリクエストとタスクを関連付けます。"),
                    (
                        _B,
                        "プランと請求情報は「設定」→「プラン」で確認できます。支払いはStripeを通じて行われ、プランはチーム全体に適用されます。",
                    ),
                ],
            ),
        ),
    ),
    (
        "他の会社との連携：チーム間共有",
        _body(
            (
                "チーム間共有の用途",
                [
                    "チャットやプロジェクト、ノートのフォルダーなどで必要な相手が、クライアントや代理店、外部業者など、他の組織に所属している場合があります。Genosを使えば、ワークスペースの他の部分へのアクセス権を付与することなく、また誰かが別のアカウントを作成する必要もなく、特定の1つのアイテムだけを別のチームと共有することができます。",
                    "この機能は2つのステップで動作します。まず2つのチームを1回接続し、その後、個々のアイテムを共有します。接続だけを行っても、相手チームには一切アクセス権が与えられません。",
                ],
            ),
            (
                "ステップ1：2つのチームを接続する",
                [
                    (
                        _B,
                        "相手チームにチームIDを尋ねてください。チームIDは、相手チームのプロフィール（左サイドバー上部の「チーム」メニューから「チームプロフィールを表示」を選択）の「チームID」欄に記載されています。このIDをメールで送信しても問題ありません。このIDだけでは、何も許可されることはありません。",
                    ),
                    (
                        _B,
                        "同じ手順で自分のチームプロフィールを開き、「接続済みのチーム」を見つけて、相手のIDを貼り付け、「接続をリクエスト」を選択します。すると、そのチームがすぐにリストに表示され、「承認待ち」と表示されます。",
                    ),
                    (
                        _B,
                        "リクエストはチームのオーナーに送信され、オーナーは「受信トレイ」→「リクエスト」内で、「チーム接続」カードとして表示されます。このカードには「承認」と「却下」のボタンが付いています。また、オーナーやエディターは、自身のチームプロフィールの「接続済みのチーム」からもこのリクエストに回答できます。どちらの場所からでも、同じリクエストに対応できます。",
                    ),
                    (
                        _B,
                        "承認されると、両チームは「接続済みのチーム」で相手チームを確認できるようになり、「あなたが招待しました」または「相手が招待しました」と表示されるため、誰が誰を招待したかが常に明確になります。この操作は、その後どれだけ情報を共有することになっても、1社につき1回だけ行えば済みます。",
                    ),
                    (
                        _B,
                        "接続の依頼や承認ができるのは、オーナーまたはエディターのみです。チームメンバーなら誰でも、あなたが誰と接続しているかを確認できます。",
                    ),
                    (
                        _B,
                        "届きませんでしたか？ 相手が自社のユーザーIDやプロジェクトIDではなく、チームIDを指定してくれたか、また、確認を行っているのがチームの「オーナー」であるかを確認してください。カードはオーナー宛てに送られます。",
                    ),
                ],
            ),
            (
                "ステップ2：アイテムを共有する",
                [
                    (
                        _B,
                        "チャット：新しいグループチャットを作成し、「外部」オプションをオンにして、招待したい連携チームを選択します。外部チャットは常に非公開であり、その状態が維持されます。チャット一覧には「外部」バッジが表示されます。",
                    ),
                    (
                        _B,
                        "プロジェクト：プロジェクトのプロファイルを開き、「アクセス権を持つチーム」セクションを使用して、連携済みのチームと共有します。プロジェクト内のすべての要素（タスク、コメント、ファイル、タスクのノート、マイルストーン、およびプロジェクト専用のチャットチャンネル）がそのまま引き継がれます。",
                    ),
                    (
                        _B,
                        "ノートのフォルダー：「チームノート」フォルダーのメンバーダイアログを開き、そこから共有してください。そのフォルダーは、まず「非公開」に設定しておく必要があります。「公開」フォルダーは「チーム内の全員」を意味しますが、この範囲では共有を行うことができません。",
                    ),
                    (
                        _B,
                        "共有する際、「ビューアーまで」か「エディターまで」のいずれかを選択します。これが、相手チームのメンバーに付与できる権限の上限となります。",
                    ),
                    (
                        _B,
                        "相手チームのオーナーには、受信トレイ → リクエストに「チームと共有されました」というカードとして表示され、そこで承認を行います。カードにはアイテム名とあなたのチーム名が記載されており、承認した人はすぐにアクセスできるようになります。そのため、その人はアイテムを開いて内容を確認し、自チーム内で他に誰に必要か判断することができます。",
                    ),
                ],
            ),
            (
                "共有アイテムの表示場所",
                [
                    "これは誰もが次に尋ねる質問ですが、答えはこうです。自分のワークスペース、つまりそういうものを置くいつもの場所です。他社のチームを探し回るようなことはしません。",
                    (
                        _B,
                        "共有プロジェクトは、自分のプロジェクト一覧に表示され、名前の横に共有アイコンが表示されます。その上にカーソルを合わせると、どのチームに属しているかを確認できます。",
                    ),
                    (
                        _B,
                        "外部チャットは、自分のチャット一覧に表示され、そのチャットルームを所有するチーム名がバッジとして付いています。",
                    ),
                    (
                        _B,
                        "「共有」と表示された共有ノートフォルダーが、チームノートの最上位に表示されます。このフォルダーには、所有チームの名前が記載されています。フォルダー内にすでに存在するすべてのコンテンツは、このフォルダーとともに引き継がれます。",
                    ),
                    (
                        _B,
                        "承認された人だけが閲覧できます。チーム内の同僚で、共有先に追加されていない人は何も表示されません。共有を受け入れたからといって、全員のサイドバーに表示されるわけではありません。",
                    ),
                    (
                        _B,
                        "何も表示されませんか？ 誰かが承認する必要があります。チーム内で共有を承認した人に確認してください。次のセクションでは、その手順について説明しています。",
                    ),
                ],
            ),
            (
                "各チームが自分のメンバーを管理する",
                [
                    "これが、全員の時間を節約できるポイントです。共有が承認されると、相手チームのオーナーやエディターは、そのアイテムに関していつでも自由に自チームのメンバーを追加・削除できます。あなたへの再依頼も承認も不要で、どれだけ時間が経っていても構いません。あなたがチームとアイテムを承認すれば、あとは相手側が自チームのメンバーを配置するだけです。",
                    (
                        _B,
                        "方法：対象項目（共有プロジェクトのプロファイル、外部チャットのプロファイル、または共有フォルダーのメンバーダイアログ）を開き、「アクセス権を持つチーム」を探します。自分のチームの行には「人＋」ボタンがあり、そこには同僚の一覧が表示されます。追加する同僚をクリックすると、その共有アイテムへのアクセスが付与されます。",
                    ),
                    (
                        _B,
                        "共有アイテムにある通常のメンバー選択機能は、外部メンバーがアクセスするための手段ではありません。この機能は所有チームのメンバーを追加するためのものなので、共有先のチームには表示されず、代わりに「アクセス権を持つチーム」が表示されます。",
                    ),
                    (
                        _B,
                        "追加できるのは、実際に自分のチームのメンバーである人だけであり、設定した上限を超えることは決してありません。",
                    ),
                    (
                        _B,
                        "所有チームには拒否権があり、いつでも共有アイテムから個々の参加者を削除できます。ただし、相手チームのメンバーを相手に代わって追加することはできません。",
                    ),
                    (
                        _B,
                        "相手チームのメンバーが退会すると、そのメンバーのメンバーシップ終了に伴い、共有アイテムへのアクセス権も失われます。特に確認や操作を行う必要はありません。",
                    ),
                ],
            ),
            (
                "共有アイテム内での作業",
                [
                    "共有された対象の内部では、2つのチームが1つのチームとして活動します。そこがまさに重要な点であり、境界は対象の「内部」ではなく「周囲」にあるため、この点を明確にしておく価値があります。",
                    (
                        _B,
                        "共有プロジェクトでは、どちらの側もタスクの作成・編集、移動、コメントの追加、ファイルの添付、タスクノートの記入、マイルストーンやスプリントの設定を行うことができます。プロジェクトチャットは、参加している全員に対して同じように機能します。",
                    ),
                    (
                        _B,
                        "外部チャットでは、ルーム内の全員が通常通りメッセージを送信したり、スレッドに返信したり、リアクションを付けたり、お互いをメンションしたりできます。",
                    ),
                    (
                        _B,
                        "共有ノートフォルダーでは、双方がノートを書き込んだり、リアルタイムで共同編集を行ったり、サブフォルダーを作成して作業を整理したりできます。他のユーザーの共有フォルダー内に作成したサブフォルダーは、名前を変更したり削除したりすることができます。",
                    ),
                    (
                        _B,
                        "権限の上限をビューアーに設定した共有は例外で、参加者は閲覧のみ可能です。",
                    ),
                ],
            ),
            (
                "所有チームだけが行える操作",
                [
                    "そのアイテムを所有するチームは、そのアイテムに含まれる作業そのものではなく、アイテム自体に関する決定権を保持します：",
                    (
                        _B,
                        "共有プロジェクト、チャット、またはフォルダーの名称変更や削除、フォルダーの移動、あるいは所有チームのメンバーのうち誰がアクセスできるかの変更。",
                    ),
                    (
                        _B,
                        "他チームへの再共有。共有先のチームは、アイテムを第三の会社へ再共有したり、所有チームのメンバーをアイテムへ招待したりできません。",
                    ),
                    (
                        _B,
                        "共有の終了。共有を終了すると、そのコンテンツはワークスペースから即座に削除されます。これには、あなたの側がその中に書き込んだ内容も含まれますが、それらの内容は相手側に残ります。",
                    ),
                ],
            ),
            (
                "外部参加者には見えないもの",
                [
                    (
                        _B,
                        "共有されたもののみ：共有されたプロジェクト（所有チームの他のプロジェクトは含まない）、外部チャット（チームの他のチャットは含まない）、共有フォルダー（チームの他のノートは含まない）。メンバーリストは決して含まれません。",
                    ),
                    (
                        _B,
                        "そのチームが管理する他の作業は、近くにあるからといって表示されるわけではありません。そのチームの「チームノート」内のパブリックフォルダーは「そのチームの全員」を対象としており、あなたはその対象外です。",
                    ),
                    (
                        _B,
                        "検索とGenosにも同じアクセス境界が適用されます。外部参加者が検索や質問をしたとき、回答に使われるのはアクセスを許可されたアイテムだけです。",
                    ),
                    (
                        _B,
                        "彼らは独自のプランとAIの利用制限を設けており、彼らの利用分はあなたに請求されることはなく、あなたの利用分も彼らに請求されることはありません。",
                    ),
                    (
                        _B,
                        "所有チームも、チームメニューに「ゲスト」というラベル付きで表示されることがあり、そこからオーナー側の共有アイテムと同じものが開くことができます。これは追加のアクセス方法であり、主なアクセス方法ではありません。",
                    ),
                ],
            ),
            (
                "共有を終了する",
                [
                    (
                        _B,
                        "「アクセス権を持つチーム」セクションにあるアイテムの共有を停止します。そのチームから参加していた全員が、直ちにアクセスできなくなります。",
                    ),
                    (
                        _B,
                        "「接続済みのチーム」からチーム全体を解除します。これにより、チーム間で共有されていたすべてのチャット、プロジェクト、フォルダーが、双方向で即座に終了します。そのため、確認のメッセージが表示され、削除された人数も通知されます。",
                    ),
                    (
                        _B,
                        "どちらの側も、相手の同意なしに共有や接続を終了できます。後で再接続することは可能ですが、個々の共有は自動的に復活することはありません。",
                    ),
                ],
            ),
        ),
    ),
    (
        "設定：すべての項目と場所",
        _body(
            (
                "設定を開く",
                [
                    "左サイドバーの下部にある歯車アイコンをタップすると、「設定」が開きます。スマートフォンでは、「アカウント」タブをタップしてから「設定」を選択します。別の画面に移動するのではなく、現在表示中の画面の上に設定画面が重ねて表示されるため、閲覧中の場所を失うことはありません。以下のセクションは、設定画面のタブを順に並べたものです。",
                ],
            ),
            (
                "一般",
                [
                    (
                        _B,
                        "外観：ライト、ダーク、またはシステム（「システム」はOSの設定に従い、OSの変更に合わせて変化します）。これとは別に、アクセントカラーとして、パープル、ブルー、ティール、エメラルド、アンバー、ローズ、スレートから選択できます。スマートフォンの「アカウント」タブにはライト／ダークを切り替えるクイックスイッチがありますが、「システム」を選択できるのは、ここでの詳細設定のみです。",
                    ),
                    (
                        _B,
                        "言語：英語、日本語、中国語、アラビア語、ヒンディー語、フランス語、スペイン語。アラビア語を選択すると、レイアウト全体が右から左へ切り替わります。これにより、通知メールの表示言語も設定されます。まだ翻訳されていない部分は、空白になるのではなく、英語が表示されます。",
                    ),
                    (_B, "プライバシー：匿名化された利用状況の分析データを共有するかどうか。"),
                ],
            ),
            (
                "プランと使用状況",
                [
                    "チームのプラン、利用状況のメーター、アップグレードおよび請求に関するリンク。このページは、ご自身の利用状況に関する正確な情報源です。各ティアに含まれる内容については、本ガイドの「プラン」ページをご覧ください。請求セクションを表示できるのは、チームのオーナーのみです。",
                ],
            ),
            (
                "Spotlight",
                [
                    (
                        _B,
                        "Genosで使用するAIモデル（対応プランではエフォートレベル）と、これまでの利用状況を確認できます。一部のモデルは一覧に表示されていてもまだ選択できず、「近日公開予定」と表示されます。",
                    ),
                    (
                        _B,
                        "SpotlightでのAI回答とWeb検索用の切り替えスイッチです。Genosページの歯車アイコンからも、同じパネルにアクセスできます。",
                    ),
                ],
            ),
            (
                "チャット",
                [
                    (
                        _B,
                        "メッセージのレイアウト：ゆったりとしたバブル表示、または画面により多く表示できるコンパクトな行表示。",
                    ),
                    (_B, "ワンクリックで反応できる3つの絵文字。"),
                    (_B, "メッセージをダブルクリックすると、それがToDo項目になるかどうか。"),
                ],
            ),
            (
                "タスク",
                [
                    (
                        _B,
                        "クイック追加行では、プロジェクトで必須と指定されたフィールドの入力が必須となるか、それとも後で入力できるか。",
                    ),
                    (
                        _B,
                        "関連付けられたプルリクエストがマージされた際に、そのタスクが自動的にクローズされるかどうか（GitHubとの連携が必要です）。",
                    ),
                    (
                        _B,
                        "タスクの期限がGoogleカレンダーと同期されるかどうか。これには、既存のタスクの一括遡及反映も含まれます（Googleとの連携が必要で、カレンダーへのアクセス権限が許可されている必要があります）。",
                    ),
                ],
            ),
            (
                "通知、メンショングループ、カスタム絵文字",
                [
                    (
                        _B,
                        "通知：マスタースイッチ、ブラウザの権限確認画面、カテゴリごとのプッシュ通知とメール通知の個別切り替え、ミュートしたチャットやスレッド／タスク／ノート、そして2種類のダイジェスト。詳細については、本ガイドの「通知」ページで詳しく解説しています。",
                    ),
                    (
                        _B,
                        "メンショングループ：複数のユーザーを一度に@メンションできる名前を定義します。メンバーなら誰でも作成できます。",
                    ),
                    (
                        _B,
                        "カスタム絵文字：チーム独自の絵文字をアップロードしましょう。名前は小文字、数字、アンダースコア、プラス記号、ハイフンのみ使用可能で、最大50文字までです。ファイルはPNG、JPG、GIF、WebP、またはSVG形式で、最大512 KBまでです。メンバーなら誰でもアップロードできますが、削除できるのはアップロードした本人だけです。",
                    ),
                ],
            ),
            (
                "ショートカット、アカウント、連携、開発者",
                [
                    (
                        _B,
                        "ショートカット：キーボードショートカットの一覧（読み取り専用）。これらのショートカットは再割り当てできません。",
                    ),
                    (
                        _B,
                        "アカウント：データをJSONファイルとしてエクスポートするか、アカウントを削除します。削除を行うには「DELETE」と入力し、パスワードで確認する必要があります。ただし、他のメンバーがいるチームを所有している間は削除が拒否されます。その場合は、まずチームの所有権を他のメンバーへ譲渡してください。",
                    ),
                    (
                        _B,
                        "連携：GoogleとGitHubを連携させ、Genosがアクセスできるリポジトリを選択します。",
                    ),
                    (
                        _B,
                        "開発者：個人用APIキーとチーム用Webhook。APIキーの作成は誰でも行えますが、Webhookの設定にはチームのオーナーまたはエディターである必要があります。本ガイドのAPIページをご覧ください。",
                    ),
                ],
            ),
            (
                "プロフィール（別ウィンドウ）",
                [
                    "サイドバーのアバターをクリックすると、プロフィール画面が開きます。このプロフィールは「設定」の一部ではありません。プロフィールには、表示名（最大50文字）、アバター（jpgまたはpng、サイズは利用プランによって制限されます）、最大50文字のカスタムステータス、全員に対してオンライン状態を非表示にする「オフライン表示」スイッチ、役職、および居住国が表示されます。他のユーザーのアバターをクリックすると、同じウィンドウが読み取り専用で開き、そのユーザーにDMを送るためのボタンが表示されます。",
                ],
            ),
            (
                "設定にはない項目",
                [
                    (
                        _B,
                        "ログイン中にパスワードを変更する場合――そのための画面はありません。ログインページの「パスワードをお忘れの方」リンクから手続きを行ってください。リセット用のリンクがメールで送信されます。",
                    ),
                    (
                        _B,
                        "メールアドレスの変更：アプリでは利用できません。プロフィールには、メールアドレスが読み取り専用で表示されます。",
                    ),
                    (
                        _B,
                        "タイムゾーン選択機能：意図的に搭載していません。Genosはブラウザからタイムゾーンを読み取り、それに準拠するため、期限やダイジェストは、どこにいてもあなたの現地時間に合わせて配信されます。",
                    ),
                    (
                        _B,
                        "タスクテーブルの列と並べ替え：これらは「設定」ではなく、タスクテーブル自体の列設定にあります。",
                    ),
                ],
            ),
        ),
    ),
    (
        "キーボードショートカット一覧",
        _body(
            (
                "基本ルールとOSによるキーの違い",
                [
                    "ショートカットで修飾キーを使用する場合、MacではCmdキー、WindowsやLinuxでは通常CtrlキーまたはAltキーが使用されます。以下の各ショートカットには、使用するキーが明記されています。「設定」→「ショートカット」では、アプリ内で同じリストを確認できます。",
                ],
            ),
            (
                "Genosのどこでも",
                [
                    (
                        _B,
                        "Cmd-K（Ctrl-K）：Spotlightを開きます。Genosのページでは、上に別のウィンドウを開くのではなく、そのページ自体の入力ボックスにカーソルを移動させます。",
                    ),
                    (
                        _B,
                        "Escキー：Spotlightを閉じます。Escキーを押すと、通常、最前面にあるもの（メニュー、ウィンドウ、または@メンション一覧など）が、それを表示しているウィンドウよりも先に閉じられます。",
                    ),
                    (
                        _B,
                        "画面の切り替え：Cmdキー（Windows/Linuxの場合はAltキー）を押したまま、Ctrlキーを1回タップします。タップするたびに、「受信トレイ」「チャット」「タスク」「ノート」「Genos」の順に、最後に使用した順に移動します。つまり、1回タップするだけで「前の画面に戻る」ことができます。Shiftキーを追加するとさらに前の画面に移動し、押していたキーを離すと操作が確定し、Escキーを押すとキャンセルして現在の画面にとどまります。",
                    ),
                ],
            ),
            (
                "操作画面を直接開く",
                [
                    "CtrlキーとCmdキーを同時に押したまま（Windows/LinuxではCtrlキーとAltキー）、文字を入力してください：",
                    (_B, "T：「タスク」に移動し、新しいタスクを作成します。"),
                    (_B, "N：「ノート」に移動し、新しい個人用ノートを作成します。"),
                    (_B, "C：コンパクトなカレンダー。"),
                    (_B, "M：Meetのリンクを作成し、クリップボードにコピーします。"),
                    (_B, "H：「履歴」ウィンドウ。"),
                    (_B, "G：プレビュー中のタスクの依存関係グラフ。"),
                ],
            ),
            (
                "Spotlight／Genosの入力欄",
                [
                    (_B, "「上・下」：検索結果をスクロールします。"),
                    (
                        _B,
                        "Enterキーを押すと、ハイライトされた検索結果がプレビューとして開きます。何もハイライトされていない場合は、Enterキーを押すと質問が送信されます。",
                    ),
                    (_B, "Shift+Enter：長い質問の途中で改行を入れる（送信は行わない）。"),
                    (
                        _B,
                        "検索結果をCmdキーを押しながらクリックするか、Ctrlキーを押しながらクリックすると、プレビューではなく実際のページに移動します。",
                    ),
                ],
            ),
            (
                "チャット内",
                [
                    (
                        _B,
                        "Cmd-EnterまたはCtrl-Enter：送信。どちらの修飾キーも、すべてのプラットフォームで機能します。Enterキーのみを押すと、意図的に改行が入ります。これは、書きかけのメッセージが誤って送信されるのを防ぐためです。",
                    ),
                    (
                        _B,
                        "この同じ操作で、スレッドへの返信、編集内容の保存、タスクへのコメントの投稿が行われます。",
                    ),
                    (
                        _B,
                        "メッセージをCmdキーを押しながらクリック（またはAltキーを押しながらクリック）すると、会話画面を離れることなくそのスレッドを開くことができます。",
                    ),
                    (
                        _B,
                        "Cmd-Shift-左/右（Windows/LinuxではAlt-Shift）：チャットタブ間を移動します。代わりに上/下キーを押すと、会話リストを順にスクロールします。いずれの操作も、入力欄で文字を入力している間は無視されます。",
                    ),
                ],
            ),
            (
                "タスクとノート内",
                [
                    (
                        _B,
                        "行のクイック追加：Enterキーを押すとタスクが作成され、Escキーを押すとその行が破棄されます。",
                    ),
                    (
                        _B,
                        "タスク作成フォームの完全な形式：MacではCmd+Enter、Windows/LinuxではCtrl+Enter。チャット作成フォームはどちらのキーでも入力可能ですが、こちらはプラットフォームごとに指定されたキー入力が必須です。",
                    ),
                    (
                        _B,
                        "どのエディタでも、バッククォート3つを入力してEnterキーを押すと、コードブロックが始まります。",
                    ),
                    (_B, "タイトル欄では、Enterキーを押すと編集が終了し、内容が保存されます。"),
                ],
            ),
        ),
    ),
    (
        "プラン、利用上限、上限に達したときの動作",
        _body(
            (
                "確認場所",
                [
                    "「設定」→「プランと使用状況」では、ご利用中のプランとリアルタイムの利用状況が表示されます。プランページでは、各プランの比較や、Stripeを通じたプランのアップグレードを行うことができます。プランはチーム全体に適用され、請求に関するセクションを表示できるのはチームのオーナーのみです。以下は各プランの概要です。ご自身の現在の利用状況については、「使用状況」ページをご確認ください。",
                ],
            ),
            (
                "各プランの内容",
                [
                    (
                        _B,
                        "Free：1日あたりAIへの質問20件とWeb検索10件、1か月あたりタスク50件とノート50件、180日分のメッセージ履歴、最大5 MBのアップロード、30日分のGenos会話履歴、連携機能なし。",
                    ),
                    (
                        _B,
                        "Core：1日あたり100件の質問と25回のWeb検索、1か月あたり200件のタスクとノート、1年分のメッセージ履歴、25 MBのアップロード、180日分のGenos履歴、Web検索、Googleカレンダー。",
                    ),
                    (
                        _B,
                        "Pro：1日あたり250件の質問と60回のWeb検索、1か月あたり500件のタスクとノート、無制限のメッセージ履歴、50 MBのアップロード、1年分のGenos履歴、GitHub、最高のエフォートレベル、チーム全体のエージェントメモリ、週次のプロアクティブダイジェスト、MCP。",
                    ),
                    (
                        _B,
                        "Max：1日あたり500件の質問と150回のWeb検索、無制限のタスクとノート、100 MBのアップロード、無制限のGenos履歴、およびデイリーダイジェスト。",
                    ),
                    (
                        _B,
                        "Enterprise：質問と検索は無制限で、最大200 MBのファイルをアップロードできます。アプリ内では購入できず、利用するにはお問い合わせが必要です。",
                    ),
                ],
            ),
            (
                "クレジット",
                [
                    (
                        _B,
                        "AI機能の利用には、プランごとに付与される月間クレジット枠も消費されます。Freeプランでは5クレジット、Coreプランでは30クレジット、Proプランでは70クレジット、Maxプランでは150クレジットです。質問、スレッドの要約、ノートの要約を行うと、クレジットが消費されます。",
                    ),
                    (
                        _B,
                        "クレジットは、10、50、100単位のパックで購入できます。購入したクレジットは、毎月の割り当て分とは異なり、月末に有効期限が切れることはありません。",
                    ),
                    (
                        _B,
                        "1回のリクエストの上限は5クレジットですので、1つの質問だけでその月のクレジットをこっそり使い果たしてしまうことはありません。",
                    ),
                ],
            ),
            (
                "上限に達したとき",
                [
                    (
                        _B,
                        "1日の上限は翌日にリセットされます。データが失われることはなく、これまでに進めた作業はそのまま残ります。",
                    ),
                    (
                        _B,
                        "履歴は削除されるのではなく、非表示になります。表示範囲より古いメッセージや過去のGenosでの会話は表示されなくなりますが、アップグレードを行うと再び表示されるようになります。これらは決して削除されたわけではありません。",
                    ),
                    (
                        _B,
                        "「ファイルが大きすぎる」という理由でアップロードが拒否されたのは、ご利用のプランにおける1ファイルあたりの容量制限に達しているためであり、ファイルに不具合があるわけではありません。この制限は1日ごとではなく、1ファイルごとに適用されます。",
                    ),
                    (
                        _B,
                        "Genosが提供できる機能もプランによって異なります。「Free」プランでは閲覧、「Core」プランでは作成・更新が可能で、それ以上のプランでは大規模な再編成作業も行えます。どのプランでも、何かを書き込む前には必ず確認のメッセージが表示されます。",
                    ),
                ],
            ),
        ),
    ),
    (
        "ヒント、ショートカット、困ったときの対処",
        _body(
            (
                "すばやく操作する",
                [
                    (
                        _B,
                        "Cmd-K（Ctrl-K）：どこからでもSpotlightを利用できます。Genosで最も役立つ習慣です。",
                    ),
                    (
                        _B,
                        "GenosのURLをメッセージに貼り付けると、ライブプレビューカードとして表示されます。クリックするとモーダルウィンドウで開くため、現在の位置がずれることはありません。",
                    ),
                    (
                        _B,
                        "スレッドをタスクにリンクさせることができるため、「これをやるべきだ」という会話も、2回のクリックで追跡可能な作業として登録できます。",
                    ),
                    (
                        _B,
                        "スプリントボード、タスクテーブル、タスクグラフは、同じタスクを表す3つのビューです。その場の状況に合わせて、適切なものを使用してください。",
                    ),
                ],
            ),
            (
                "動作がおかしいとき",
                [
                    (
                        _B,
                        "通知が届かない場合は、「設定」→「通知」にあるブラウザの権限設定を確認してください。また、iPhoneの場合は、Genosがホーム画面に追加されていることを確認してください。",
                    ),
                    (
                        _B,
                        "プロジェクトやノートが表示されない場合は、メンバー登録がされていない可能性があります。オーナーに追加を依頼するか、利用可能な場合はアクセスリクエストの手順に従ってください。",
                    ),
                    (
                        _B,
                        "表示が古くなっているように見えますか？　まずはタブを再読み込みしてみてください。長時間開いているタブでは、更新後も古いコードが実行され続けていることがあります。",
                    ),
                ],
            ),
            (
                "覚えておきたいこと",
                [
                    "このガイドノートはSpotlightで検索可能です。そのため、最も手っ取り早く活用するには、わざわざ読む必要はありません。「チャットのミュート方法は？」「エディターにはどのような権限がある？」「スプリントはどのように機能する？」といった質問をGenosに投げかけるだけで、関連するページが引用された回答が返ってきます。",
                    "Genosが答えられない質問：genos.support@genosai.dev",
                ],
            ),
        ),
    ),
    (
        "よくある問題の解決方法",
        _body(
            (
                "通知",
                [
                    (
                        _B,
                        "まったく通知が届かない場合は、まず「設定」→「通知」にあるブラウザの権限設定を確認してください。ブラウザ自体がGenosをブロックしている場合、アプリ側の設定ではそれを上書きすることはできません。",
                    ),
                    (
                        _B,
                        "iPhoneで通知がまったく届かない場合：Safariはインストール済みのアプリにのみプッシュ通知を送信します。Genosをホーム画面に追加し、そこからアプリを開いて、通知を許可してください。",
                    ),
                    (
                        _B,
                        "特定の会話のみミュートされている場合――おそらくミュート設定が有効になっているのでしょう。「設定」→「通知」には、チャットやスレッド、タスク、ノートなど、現在ミュートされているすべての項目が一覧表示されており、そこからミュートを解除できます。",
                    ),
                    (
                        _B,
                        "メールが多すぎる場合：プッシュ通知とメールにはそれぞれ個別の設定スイッチがあるため、アプリ内の通知はそのままに、メール通知のみをオフにすることができます。また、すべての通知メールには配信停止リンクが記載されており、ダイジェストメールにも個別の配信停止設定があります。",
                    ),
                    (
                        _B,
                        "期待していたプッシュ通知が、代わりに目立たないトースト通知として表示された――これは意図的な仕様です。現在アクティブに閲覧している画面にはトースト通知が表示され、その他のデバイスには引き続きプッシュ通知が届きます。",
                    ),
                ],
            ),
            (
                "アクセス",
                [
                    (
                        _B,
                        "プロジェクトやノートが開けない場合は、おそらくメンバー登録がされていないためです。アクセス権が提供されている場合は、「アクセスをリクエスト」をご利用ください。オーナーは受信トレイから承認または拒否を行います。",
                    ),
                    (
                        _B,
                        "誰かを招待することはできません。招待するには、オーナーまたはエディターの権限が必要です。オーナーに依頼して、エディターに任命してもらってください。",
                    ),
                    (
                        _B,
                        "オーナーからの連絡が途絶えています。エディターは所有権の主張を行うことができ、オーナーが30日以内に応答しない場合、所有権は移転します。1つのアカウントが不在であるだけで、チームが放置されることはありません。",
                    ),
                    (
                        _B,
                        "アカウントの削除は拒否されました。他のメンバーがいるチームをまだ所有しています。そのチームを譲渡してから、削除してください。",
                    ),
                ],
            ),
            (
                "Genosと検索",
                [
                    (
                        _B,
                        "作成したばかりの内容が見つからない場合、インデックス作成には少し時間がかかります。しばらくしてからもう一度お試しください。",
                    ),
                    (
                        _B,
                        "Genosが「あなたには見えているものを参照できない」と回答した場合は、自分が意図したチームに所属しているか確認してください。サイドバー上部のチーム名をクリックするとチームを切り替えることができ、各チームのコンテンツは個別に管理されています。",
                    ),
                    (
                        _B,
                        "回答は古い情報に基づいていました。ガイドとあなたのノートは、編集可能な通常のノートです。ノートを修正すれば、回答も自動的に更新されます。",
                    ),
                    (
                        _B,
                        "本日のリクエスト枠を使い切りました。リクエスト枠は明日リセットされます。現在の残数は「設定」→「プランと使用状況」で確認できます。",
                    ),
                    (
                        _B,
                        "希望するモデルがグレー表示になっています。一部は「近日公開予定」と記載されており、より深いエフォートレベルを利用するには上位プランが必要です。",
                    ),
                ],
            ),
            (
                "その他",
                [
                    (
                        _B,
                        "アップロードが拒否されました。プランのファイルごとのサイズ制限を超えています。",
                    ),
                    (
                        _B,
                        "ページが古くなっているように見える場合や、ボタンが反応しない場合は、タブを再読み込みしてください。何日も開いたままにしているタブでは、最初に実行されたコードがそのまま実行され続けています。",
                    ),
                    (
                        _B,
                        "誤ってノートの内容を削除してしまった場合は、そのノートのバージョン履歴を開き、以前のバージョンに復元してください。バージョンは自動的に記録されています。",
                    ),
                    (
                        _B,
                        "このガイドを削除してしまい、元に戻したい場合――このガイドは意図的に自動再作成されないようになっているため、サポートに復元を依頼してください。",
                    ),
                    (_B, "まだ解決しない場合：genos.support@genosai.dev"),
                ],
            ),
        ),
    ),
    (
        "プレイブック：仕事別のGenos活用方法",
        _body(
            (
                "このページの読み方",
                [
                    "Genosは、ユーザーがソフトウェア開発者であることを前提としていません。以下に、業務の種類ごとに1つの基本レイアウトを示します。最も近いものを選び、残りは無視してください。これらはすべて、同じ3つの要素（チャット、タスク、ノート）を異なる配置で組み合わせたものなので、ここに示されているものには、すでに持っていない設定は必要ありません。",
                ],
            ),
            (
                "エンジニア",
                [
                    (
                        _B,
                        "サービスまたはリポジトリごとに1つのプロジェクトを作成します。GitHubを連携させてプルリクエストをタスクに紐付け、プルリクエストがマージされた際にタスクを完了するように設定すれば、ボードは自動的に最新の状態に保たれます。",
                    ),
                    (
                        _B,
                        "真のブロック要因には依存関係を活用しましょう。BがAによってブロックされているとマークすると、Genosが自動的にBの「ブロック中」ステータスを設定・解除します。グラフビューでは、実際にリリースを妨げている要因が確認できます。",
                    ),
                    (
                        _B,
                        "設計ドキュメントをタスクノートとして保存し、計画が作業に反映されるようにするとともに、チャンネル全体を読む代わりに、Genosに「今週、#APIでは何が変更されましたか？」といった質問を投げかけましょう。",
                    ),
                ],
            ),
            (
                "プロダクト／プロジェクトマネージャー",
                [
                    (
                        _B,
                        "期限付きの成果物には「マイルストーン」、ペース管理には「スプリント」、スプレッドシート形式での表示が必要な場合は「タスクテーブル」を活用しましょう。繰り返し設定しているフィルターは、名前付きフィルターセットとして保存し、プロジェクト内で共有しましょう。",
                    ),
                    (
                        _B,
                        "プロセスに必要なカスタムフィールドを定義し、それらを必須項目として設定してください。そうすることで、タスクが不完全な状態で登録されるのを防ぐことができます。",
                    ),
                    (
                        _B,
                        "「タスクの重み」「キャパシティ」「ベロシティ」の各ビューは、「この計画は現実的か？」という疑問に答えます。また、上位プランのプロアクティブダイジェストにより、スタンドアップミーティングが行われる前に、期限切れや滞っている作業が明らかになります。",
                    ),
                    (
                        _B,
                        "自分で情報を集めて進捗をまとめる代わりに、Genosへ状況を質問しましょう。「金曜日までにリスクとなる要素は何か？」「前回のスプリントで何を完了させたか？」",
                    ),
                ],
            ),
            (
                "マーケティング",
                [
                    (
                        _B,
                        "キャンペーンまたはチャネルごとに1つのプロジェクトを作成し、公開日ごとにマイルストーンを設定します。媒体（ブログ、メール、SNS、有料広告）ごとにタグを付け、それらを1つずつ絞り込んでいきます。",
                    ),
                    (
                        _B,
                        "ノートに下書きを作成し、複数の人が同じドキュメントに同時に書き込めるようにし、下書きの上にはタスクノートとして概要を記載しておいてください。",
                    ),
                    (
                        _B,
                        "キャンペーンのチャンネルでは、作業に対する承認情報を常に紐付けておきます。最終承認済みのアセットをチャンネルにピン留めしておけば、先週のバージョンが誤って出荷されるのを防げます。",
                    ),
                ],
            ),
            (
                "調査と分析",
                [
                    (
                        _B,
                        "情報源やインタビューごとに1つのノートを作成し、研究ごとに1つのフォルダーにまとめ、未解決の質問ごとに1つのタスクを設定します。ノートは収集した資料であり、タスクは未解決の課題です。",
                    ),
                    (
                        _B,
                        "Genosは出典を明記しているため、インタビューの資料一式を調べてみれば、確認可能な答えが得られます。引用する前に、出典となったノートをクリックして内容を確認してください。",
                    ),
                    (
                        _B,
                        "1つの長い文書に対して、そのノートの「質問」ボタンから質問を投げかけ、その回答を新しいノートとして保存することで、生の資料の上に要約の層を構築することができます。",
                    ),
                ],
            ),
            (
                "学生",
                [
                    (
                        _B,
                        "科目ごとに1つのプロジェクト、試験や締め切りごとに1つのマイルストーン、そして科目ごとのフォルダーに講義ノート。提出期限を設定することで、すべての科目を1つのカレンダーにまとめられます。",
                    ),
                    (
                        _B,
                        "チャットの横にある「毎日のToDo」パネルは、勉強の時間にぴったりです。これは、実際の課題とは別の、1日ごとのチェックリストです。",
                    ),
                    (
                        _B,
                        "試験の前に、自分のノートに質問を投げかけてみましょう――「弾力性について書いた内容を説明してください」といった具合に――、また、グループチャットを活用して勉強会を開きましょう。",
                    ),
                ],
            ),
            (
                "運用、バックオフィス、サポート",
                [
                    (
                        _B,
                        "繰り返し行う手順は、チェックリストとしてノートにまとめ、実行ごとに1つのタスクとして記載します。タスク本文のテンプレートを再利用することで、各実行は誰かの記憶に頼るのではなく、同じ手順から開始できるようになります。",
                    ),
                    (
                        _B,
                        "プロセスごとに1つのプロジェクト（オンボーディング、請求書発行、ベンダー評価など）を設定し、絶対に欠けてはならない詳細情報については必須項目として指定します。",
                    ),
                    (
                        _B,
                        "メッセージにフラグを立てて、チャットで届いたリクエストを個人的なフォローアップ事項に変え、追跡が必要なものはタスクとして登録し、誰かの頭の中だけで終わらないようにしましょう。",
                    ),
                    (
                        _B,
                        "すべてのタスクには、誰が、何を、いつ変更したかというアクティビティ履歴が記録されており、これは通常、監査で求められる情報そのものです。",
                    ),
                ],
            ),
            (
                "どんな仕事でも",
                [
                    "これらすべてに共通する仕組みとは、チャットでの議論、タスクとしてのコミットメント、ノートにまとめられた永続的な知識――そして、これら3つすべてをGenosが読み取ることで、自分で探し回る必要がなく、質問するだけで済むようにすることです。完璧な構造を目指すよりも、まずは実際のプロジェクトを1つ始めてみましょう。構造は後で簡単に変更できますし、空っぽのワークスペースからは何も学べないからです。",
                ],
            ),
        ),
    ),
    (
        "Genosと他のツールを接続する：API、MCP、リアルタイム",
        _body(
            (
                "このページの対象者",
                [
                    "このページは、Genosをスクリプトや他のアプリ、AIコーディングエージェントなど、何か他のものと連携させる方に向けたものです。そのうちの1つの方法については、プログラミングは一切必要ありません。AIコーディングエージェントの設定画面に1つのアドレスを貼り付けるだけで、そのエージェントが直接タスクを実行できるようになります。もしこれらのいずれにも該当しない場合は、このページをスキップしてください。Genosの他の機能はこの方法に依存していません。",
                    "APIキーは、それを作成したユーザーとして機能するため、そのユーザーが閲覧できる範囲の情報のみを閲覧できます。一方、リアルタイムストリームは、ログインしているユーザーとして機能します。ただし、Webhookは例外です。チームオーナーまたはエディターがチーム全体向けに設定すると、作成者が誰であっても、そのチームのイベントを配信します。",
                ],
            ),
            (
                "APIキーを取得する",
                [
                    (
                        _B,
                        "「設定」→「開発者」でキーを作成します。キーは作成時に一度だけ表示されます。その後、読み取れる形で保存されることはありませんので、その時点でコピーしておいてください。キーはいつでも無効化でき、無効化すると次のリクエストから使用できなくなります。",
                    ),
                    (
                        _B,
                        "「読み取り」か「書き込み」を選択してください。「読み取り」キーでは、表示されている内容を確認するだけで、何も変更しません。「書き込み」キーでは、作成や更新も行うことができます。",
                    ),
                    (
                        _B,
                        "その画面で作成されたキーは個人用です。これらのキーは、ユーザーが所属するすべてのチームでユーザー自身として機能するため、各リクエストでは対象となるチームを指定する必要があります。APIには、独自のチームを保持する「チームスコープのキー」が存在しますが、現時点ではこの画面から作成することはできません。",
                    ),
                ],
            ),
            (
                "用途に合う方法を選ぶ",
                [
                    (
                        _B,
                        "REST API：スクリプトや他のアプリが、必要に応じてGenosのデータ（プロジェクト、そのタスク、および作成・更新対象の個々のタスク）を読み込んだり作成したりするためのものです。レポートの作成、データのインポート、およびスケジュールに基づいて実行するあらゆる処理にご利用ください。",
                    ),
                    (
                        _B,
                        "Webhook：Genosから変更を通知してほしい場合に使います。チームのオーナーまたはエディターが公開HTTPSアドレスを指定すると、タスクが変更されたときや、指定したチャンネルにメッセージが届いたときに、そのアドレスへPOSTされます。定期的に確認しなくても、変更が発生した時点で通知を受け取れます。",
                    ),
                    (
                        _B,
                        "MCP（Model Context Protocol）：説明文を貼り付けるのではなく、AIコーディングエージェントに直接タスクを実行させたい場合。https://api.genosai.dev/api/public/v1/mcp?team_id=<your team's id>（疑問符の前にスラッシュは付けない）で、GenosをHTTPサーバーとしてMCPクライアントに追加し、ヘッダー「Authorization: ApiKey <your key>」を送信します。スキームは「Bearer」ではなく、文字通りの「ApiKey」です。これにより、エージェントはタスクを自ら読み取り、書き込みキーを使用してタスクを更新し、コメントを返すことができます。",
                    ),
                    (
                        _B,
                        "リアルタイムストリーム：ライブコンテンツ向け：ダッシュボードや、メッセージが届いた瞬間に反応するボットなど。これは、Genosアプリ自体が使用するのと同じライブイベント（メッセージ、リアクション、誰が何を閲覧したか、誰がオンラインかなど）を伝送し、APIキーではなくGenosのログイン情報でサインインします。これが分かれ目となります。ユーザーがサインインしている場合はリアルタイムストリーム、サーバー間で通信する場合はWebhookとなります。",
                    ),
                ],
            ),
            (
                "動作を確認する",
                [
                    "まずは `GET /api/public/v1/me/` から試してみてください。このリクエストでは、そのキーが関連付けられているユーザー、チーム、およびスコープが返されるため、より微妙なエラーが発生する前に、キーが有効であることを確認できます。個人用キーの場合、チーム情報は空で返されます。これは、各リクエストでチーム名を自分で指定する必要があるという合図です。",
                    (
                        _B,
                        "「設定」→「開発者」からは、Genos Developersページにもリンクされています。このページには、REST API、MCP、Webhook、およびすべてのリアルタイムイベントに関する完全なリファレンスと、動作確認済みのサンプルが掲載されています。",
                    ),
                ],
            ),
        ),
    ),
    (
        "APIキーとWebhook：手順ガイド",
        _body(
            (
                "APIキーを作成する",
                [
                    "「設定」→「開発者」の順に選択し、キーを作成します。キーに名前を付け、読み取り専用にするか書き込みも可能にするかを選択し、有効期限を設定します（既定は90日、最長1年。期限が切れたら新しいキーを作成します）。キー名は「gnos_」で始まるため、ログ上で識別しやすくなっています。",
                    (
                        _B,
                        "キーはその瞬間に一度だけ表示されます。それを保存する場所にそのままコピーしてください。一度表示されたキーを再度読み取ることはできず、削除して新しいキーを作成するしかありません。",
                    ),
                    (
                        _B,
                        "それはあなたと同じように振る舞います――あなたが見えるものはすべて、それも見えます。パスワードと同じように扱ってください。公開リポジトリには絶対に置かないでください。また、ブラウザで実行されるコードの中にも絶対に含めないでください。",
                    ),
                    (
                        _B,
                        "キーを削除すると、その直後のリクエストからそのキーは機能しなくなるため、漏洩したと思われるキーに対しては、これが適切な対応となります。",
                    ),
                ],
            ),
            (
                "APIキーを使用する",
                [
                    "ヘッダー「Authorization: ApiKey <your key>」を送信してください。スキームは文字通り「ApiKey」です。「Bearer」はアプリ独自のサインイン用であり、ここでは拒否されます。同様に、キーを使用してキーを作成または削除することもできません。この分離は意図的なものです。つまり、漏洩したキーがそれ自体をさらに生成することはできないからです。",
                    (
                        _B,
                        "GET /api/public/v1/me/：キーがどのユーザーとして機能し、どのようなスコープを持つかを確認します。より複雑なデバッグを行う前に、まずここから始めて、キーが正常に動作することを確認してください。",
                    ),
                    (_B, "GET /api/public/v1/projects/：あなたのプロジェクト。"),
                    (
                        _B,
                        "GETおよびPOST /api/public/v1/tasks/：タスクの一覧表示、または新規作成。",
                    ),
                    (
                        _B,
                        "GETおよびPATCH /api/public/v1/tasks/<id>/：単一のタスクを読み取る、または更新する。",
                    ),
                    (
                        _B,
                        "GET /api/public/v1/openapi.json：上記のすべてに関する機械可読な記述であり、キーを一切必要としない唯一のエンドポイントです。",
                    ),
                    (
                        _B,
                        "各キーのリクエスト数は1分あたり120件に制限されています。個人用キーは特定のチームに属するものではなく、ユーザー個人に属するものです。そのため、リクエストでは対象となるチームを明示する必要があります。これが、`me/` に対してチームが指定されていない状態で返される理由です。",
                    ),
                ],
            ),
            (
                "Webhook",
                [
                    "Webhookとは、スクリプトがタイマーで定期的に問い合わせるのではなく、Genosがサーバーに通知を行う仕組みです。チームオーナーまたはエディターは、「設定」→「開発者」でHTTPSアドレスを追加し、受信するイベントを選択します：",
                    (_B, "task.created、task.updated、task.completed"),
                    (_B, "task.comment_created"),
                    (
                        _B,
                        "message.created：ただし、これは明示的に指定したチャンネルにのみ適用されます。リストが空のリストはチャットイベントを一切送らないという意味であり、すべてのチャンネルを対象にするという意味ではありません。また、ダイレクトメッセージはWebhookに送信されることはありません。",
                    ),
                    (
                        _B,
                        "配信状況は記録されるため、実際に何が送信されたかを確認できます。エンドポイントが10回連続で失敗した場合、Genosは応答のないサーバーに繰り返しリクエストを送り続けるのではなく、そのエンドポイントを無効化します。エンドポイントを修正してから、再度有効にしてください。",
                    ),
                ],
            ),
            (
                "AIコーディングエージェント向けMCP",
                [
                    "Proプラン以上では、AIコーディングエージェントをGenosに割り当てると、タスクを直接処理できるようになります。https://api.genosai.dev/api/public/v1/mcp?team_id=<your team's id>（疑問符の前にスラッシュは付けないでください）に、上記と同じApiKeyヘッダーを指定して、HTTP MCPサーバーとして追加してください。このエージェントは、タスクやノートを読み取り、ワークスペースを検索するためのツールを備えています。また、書き込みキーがあれば、タスクの作成、更新、コメントの返信も可能です。検索ツールでは、キーワード検索よりも自然言語によるクエリの方が精度が高く、これはユーザーが直接検索する場合と同様です。",
                ],
            ),
            (
                "まだ提供されていない機能",
                [
                    (
                        _B,
                        "公開リンク。Genosでは、ログインしておらず、アクセス許可を得ていないユーザーとは、いかなるコンテンツも共有できません。どこかに貼り付けたリンクであっても、ビューアーのアクセス権限が確認されます。",
                    ),
                    (
                        _B,
                        "スプレッドシートへのエクスポート。「設定」→「アカウント」から自身のデータをJSON形式でエクスポートでき、ノートはメニューからMarkdown形式でエクスポートできますが、タスクやチャットのCSV形式でのエクスポート機能はありません。",
                    ),
                    (
                        _B,
                        "Slack、Notion、Jira、Asanaとの連携機能。現在利用できる連携はGoogleとGitHubの2つです。その他のツールへのリンクをタスクへ貼り付けても、通常のリンクとして扱われます。",
                    ),
                ],
            ),
            (
                "完全なリファレンス",
                [
                    "「設定」→「開発者」から「Genos Developers」ページにリンクしています。このページには、上記のすべてに関する完全な仕様（すべてのフィールド、すべてのイベントペイロード、および動作例）が掲載されています。",
                ],
            ),
        ),
    ),
]


def seed_guide_notes(user, team, *, force: bool = False) -> bool:
    """Create both language editions of the Genos Guide for (user, team).

    Returns True when at least one language folder was created. The membership
    stamp is the primary idempotency guard, so a user who deletes the guide
    keeps it deleted. Without a stamp, existing language folders are retained
    and only missing editions are created. `force=True` deliberately performs
    that repair even for stamped memberships. Raises on failure; callers treat
    seeding as best-effort.
    """

    notes_created = 0
    with transaction.atomic():
        membership = (
            TeamMembers.objects.select_for_update().filter(team=team, attendee=user).first()
        )
        existing_folder_names = set(
            PersonalNoteFolder.objects.filter(
                team=team,
                owner=user,
                name__in=(GUIDE_FOLDER_NAME, JAPANESE_GUIDE_FOLDER_NAME),
            ).values_list("name", flat=True)
        )
        if (
            not force
            and membership is not None
            and membership.guide_seeded_at is not None
            and (not existing_folder_names or len(existing_folder_names) == 2)
        ):
            return False

        guide_editions = (
            (GUIDE_FOLDER_NAME, GUIDE_NOTES),
            (JAPANESE_GUIDE_FOLDER_NAME, JAPANESE_GUIDE_NOTES),
        )
        for folder_name, guide_notes in guide_editions:
            if folder_name in existing_folder_names:
                continue

            folder = PersonalNoteFolder.objects.create(team=team, owner=user, name=folder_name)
            for title, body in guide_notes:
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
                    team=team,
                    user=user,
                    note_id=note.note_id,
                    note_type=1,
                    role_id=1,
                )
                notes_created += 1

        if membership is not None:
            membership.guide_seeded_at = timezone.now()
            membership.save(update_fields=["guide_seeded_at", "ts_updated_at"])

    log.info(
        "[guide] seeded %d guide notes user=%s team=%s",
        notes_created,
        user.pk,
        team.pk,
    )
    return notes_created > 0
