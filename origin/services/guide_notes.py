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
