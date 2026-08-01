"""Opt-in starter workspace for a brand-new REAL team.

The readiness doc's §3.3 finding: a new team lands in a blank app with
no first action. The demo environment already proves seeded content
works — but it cannot be reused directly, because (a) `is_demo=True` is
a self-destruct flag (LogoutView deletes the environment; a daily
sweeper reaps it), (b) its content is written around four bot teammates
who don't exist in a real team (both as assignees and inside ~30 prose
references), and (c) it *creates* its team rather than accepting one.

So this module reuses the demo seeder's MACHINERY — sprint, milestone
+ backing task, blueprint tasks — with a small, self-contained blueprint
whose voice is onboarding ("learn Genos by doing") rather than fiction:
every task is assigned to the real owner, nothing sets `is_demo`, and
the team is injected, never created. Deleting the seeded project is
itself one of the tasks, so opting in is fully reversible.

Called (best-effort) from `TeamMasterView.post` when the client sends
`with_starter: true`. A seeding failure must never lose the team —
the caller wraps this in its own try/except and returns the team
regardless.

The eval fixture (`agent/evals/fixture.py`) depends on the DEMO
seeder's byte-identical output; this module deliberately shares only
helpers whose output is fully blueprint-driven, so it cannot drift the
fixture.
"""

from __future__ import annotations

import logging

from django.db import transaction

from origin.models.chat.todo_models import ToDoCategory, ToDoGroup, ToDoItem
from origin.models.common.team_models import TeamMembers
from origin.models.note.common_note_models import NotePermissionMaster
from origin.models.note.personal_note_models import PersonalNoteFolder, PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers, ProjectTags
from origin.models.task.task_models import TaskDependency
from origin.services.demo_seeder import (
    _body,
    _create_blueprint_tasks,
    _create_milestone_with_backing_task,
    _create_sprint,
    _text_body,
    kick_off_demo_reindex,
)
from origin.services.user_time import user_today

log = logging.getLogger(__name__)

# One project that teaches the product by being used. Schema mirrors
# PROJECT_BLUEPRINTS (demo_seeder) — same keys, consumed by the same
# `_create_blueprint_tasks`; every assignee is "demo" (= the real
# owner), which is what makes it safe with `bots=[]`.
STARTER_BLUEPRINT = {
    "name": "Getting started with Genos",
    "tags": [
        ("Setup", "#7c3aed", "#ffffff"),
        ("Team", "#0ea5e9", "#ffffff"),
        ("Try it", "#f59e0b", "#ffffff"),
    ],
    "milestone": {
        "title": "Your first week on Genos",
        "body": _body(
            (
                "🎯 Goal",
                [
                    "By the end of this milestone your team is set up, your first "
                    "real project exists, and everyone knows where work happens.",
                ],
            ),
            (
                "🪜 How to use this",
                [
                    "Each task below is one small step. Open a task to see the "
                    "details, tick it off when done — and when the whole list is "
                    "done, delete this project. It's yours to break.",
                ],
            ),
        ),
        "status": "Open",
        "priority": "High",
        "due_offset_days": 7,
    },
    "tasks": [
        {
            "title": "Invite your teammates",
            "status": "Open",
            "priority": "High",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 1,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Open the team menu (your avatar, top left) and choose "
                        "Invite. Genos is chat + tasks + notes in one place, so it "
                        "gets useful the moment a second person is here.",
                    ],
                ),
            ),
        },
        {
            "title": "Create your first real project",
            "status": "Open",
            "priority": "High",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 2,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "In Tasks, create a project for something you're actually "
                        "working on. Every project gets its own chat channel "
                        "automatically, so discussion and work stay together.",
                    ],
                ),
            ),
        },
        {
            "title": "Ask Spotlight something about your workspace",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 3,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Press Cmd-K (Ctrl-K on Windows) and ask something like "
                        '"what\'s due this week?". Spotlight reads your chats, '
                        "tasks and notes — the more you put in, the better the "
                        "answers get.",
                    ],
                ),
            ),
        },
        {
            "title": "Turn on notifications",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 3,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Settings → Notifications. Allow browser notifications for "
                        "real-time pings; email fills in whenever you're away, in "
                        "one batched message rather than one per event.",
                    ],
                ),
            ),
        },
        {
            "title": "Connect Google Calendar or GitHub",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 5,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "Settings → Integrations. Calendar events show up beside "
                        "your tasks; GitHub PRs link to the tasks they close.",
                    ],
                ),
            ),
        },
        {
            "title": "Done exploring? Delete this project",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 7,
            "body": _body(
                (
                    "🧾 What to do",
                    [
                        "This starter project is scaffolding, not furniture. Once "
                        "your real work lives here, delete it from the project "
                        "menu — everything it created goes with it.",
                    ],
                ),
            ),
        },
    ],
}

NOTE_WELCOME = _body(
    (
        "👋 Welcome to Genos",
        [
            "This note lives in My Notes — private to you. Notes support "
            "real-time co-editing when you share them, version history, and "
            "markdown import/export.",
            "The Getting started project in Tasks walks you through the rest.",
        ],
    ),
    (
        "⚡ Three things worth knowing",
        [
            "Cmd-K opens Spotlight anywhere — ask questions in plain language.",
            "Every project has its own chat channel; threads keep side discussions tidy.",
            "@-mention someone and they're notified — in the app, by push, "
            "and by email when they're away.",
        ],
    ),
)


def create_starter_workspace(user, team) -> None:
    """Seed the starter content into an EXISTING team for a REAL user.

    One atomic block; raises on failure (the caller decides that a
    failed seed must not fail team creation). Never touches `is_demo`.
    """
    with transaction.atomic():
        # Membership first — the view creates only the TeamMaster row and
        # the client normally joins via a separate call, which is
        # idempotent against this row (TeamMembersView short-circuits on
        # an existing membership).
        TeamMembers.objects.get_or_create(team=team, attendee=user)

        from origin.services.project_code import derive_project_code

        project_name = STARTER_BLUEPRINT["name"]
        taken_codes = set(
            ProjectMaster.objects.filter(team=team, code__isnull=False).values_list(
                "code", flat=True
            )
        )
        project = ProjectMaster.objects.create(
            team=team,
            project_name=project_name,
            code=derive_project_code(project_name, taken_codes),
            owner=user,
            project_system_user=user,
            is_private=False,
        )
        # Plain .save() so the PM-channel membership signal fires (same
        # reason as the demo seeder — bulk_create skips signals).
        ProjectMembers.objects.create(team=team, project=project, attendee=user)
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
                for tag_idx, (name, color, text_color) in enumerate(STARTER_BLUEPRINT["tags"])
            ]
        )

        sprint = _create_sprint(team, project)
        milestone, backing_task = _create_milestone_with_backing_task(
            team, project, sprint, user, [], STARTER_BLUEPRINT["milestone"]
        )
        tasks = _create_blueprint_tasks(
            team,
            project,
            sprint,
            milestone,
            backing_task,
            user,
            [],
            STARTER_BLUEPRINT["tasks"],
        )

        # One dependency so the graph/blocked UI has something to show:
        # inviting teammates blocks the "first real project" walkthrough.
        # Cosmetic, and deleted with the project.
        TaskDependency.objects.create(
            blocker_task=tasks[0],
            blocked_task=tasks[1],
            team=team,
            created_by=user,
        )

        folder = PersonalNoteFolder.objects.create(team=team, owner=user, name="Getting started")
        note = PersonalNoteMaster.objects.create(
            team=team,
            owner=user,
            title="Welcome to Genos",
            body=NOTE_WELCOME,
            folder_id=folder.folder_id,
        )
        NotePermissionMaster.objects.create(
            team=team, user=user, note_id=note.note_id, note_type=1, role_id=1
        )

        today = user_today(user)
        cat = ToDoCategory.objects.create(
            team=team, user=user, name="Getting started", sort_order=0
        )
        group = ToDoGroup.objects.create(team=team, user=user, local_date=today, is_completed=False)
        ToDoItem.objects.create(
            group=group,
            category=cat,
            title="Look around the Getting started project",
            notes=_text_body(
                "The tasks there double as a product tour — each one is a real action, not reading."
            ),
            sort_order=0,
        )
        ToDoItem.objects.create(
            group=group,
            category=cat,
            title="Press Cmd-K and ask Spotlight anything",
            sort_order=1,
        )

    # After commit, exactly like the demo path: content only becomes
    # searchable once OpenSearch has it, and the reindex must not run
    # inside the transaction.
    kick_off_demo_reindex()
