"""Opt-in starter workspace for a brand-new REAL team.

The readiness doc's §3.3 finding: a new team lands in a blank app with
no first action. The demo environment already proves seeded content
works — but it cannot be reused directly, because (a) `is_demo=True` is
a self-destruct flag (LogoutView deletes the environment; a daily
sweeper reaps it), (b) its content is written around four bot teammates
who don't exist in a real team (both as assignees and inside ~30 prose
references), and (c) it *creates* its team rather than accepting one.

So this module reuses the demo seeder's MACHINERY — sprint, milestone
+ backing task, blueprint tasks — with small, self-contained blueprints
whose voice is onboarding ("learn Genos by doing") rather than fiction:
every task is assigned to the real owner, nothing sets `is_demo`, and
the team is injected, never created. Deleting the seeded project is
itself one of the tasks, so opting in is fully reversible.

One project, two milestones: STARTER_BLUEPRINT is the setup week (get
the team in, make something real), EXAMPLES_BLUEPRINT is one worked
example per trade — engineering, planning, marketing, research,
studying, operations — because signup tells us nothing about what the
person does, and a tour written for engineers reads as "not for me" to
everyone else.

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
from origin.services.guide_notes import seed_guide_notes
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
        ("Example", "#10b981", "#ffffff"),
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

# A second milestone in the same project. The first one is setup ("get
# the team in, make a real project"); this one answers the question that
# follows it — "yes, but what do I actually DO with this?" — for someone
# who is not an engineer.
#
# Signing up does not tell us which kind of work a person does, and a
# tour written for one trade reads as "not for me" to the rest. So each
# task below is one trade's smallest real workflow, named by trade so it
# can be skimmed and mostly ignored: the point is that everyone finds
# their own row, not that anyone does all seven. Every one is a real
# action in the product, not reading, and the deeper version of each
# lives in the "Playbooks" page of the Genos Guide.
#
# Same schema and same helpers as STARTER_BLUEPRINT above; kept a
# separate constant rather than a second key so each dict stays exactly
# the shape `_create_milestone_with_backing_task` / `_create_blueprint_tasks`
# already consume.
EXAMPLES_BLUEPRINT = {
    "milestone": {
        "title": "Try Genos on real work",
        "body": _body(
            (
                "🎯 Goal",
                [
                    "Move from looking at Genos to using it, on something you "
                    "would have had to do anyway.",
                ],
            ),
            (
                "🪜 How to use this",
                [
                    "These are examples, not a checklist — find the one that "
                    "matches your job, do that one, and delete the rest. Each "
                    "takes a few minutes and leaves you with something real "
                    "rather than a tidy list.",
                ],
            ),
        ),
        "status": "Open",
        "priority": "Normal",
        "due_offset_days": 14,
    },
    "tasks": [
        {
            "title": "Engineering: link a pull request to a task",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 10,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Connect GitHub in Settings → Integrations, then paste a "
                        "pull request link onto a task. In Settings → Tasks you "
                        "can also have the task close itself when that PR "
                        "merges.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "The board stops being a second thing to update by "
                        "hand. Try dependencies too: mark one task blocked by "
                        "another and Genos sets and clears the Blocked status "
                        "for you.",
                    ],
                ),
            ),
        },
        {
            "title": "Planning: turn a date you care about into a milestone",
            "status": "WIP",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 10,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Make a milestone for your next real deadline and attach "
                        "two or three tasks to it. It then tracks its own "
                        "progress, so the question \"are we going to make it?\" "
                        "has somewhere to be answered.",
                    ],
                ),
            ),
        },
        {
            "title": "…and split one of those tasks into sub-tasks",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": False,
            "parent_idx": 1,
            "due_offset_days": 11,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Open a task and add sub-tasks for the steps inside it. "
                        "This task is itself a sub-task of the one above — that "
                        "indent in the table is what you're making.",
                    ],
                ),
            ),
        },
        {
            "title": "Marketing: draft something as a shared note",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 11,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Write your next post, email or brief as a note and share "
                        "it with a colleague as an editor. You can both type in "
                        "it at the same time and see each other's cursors.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Copy stops travelling as attachments. Keep the brief as "
                        "the task's own note so the instructions sit above the "
                        "drafts, and pin the approved version in the project's "
                        "channel so nobody ships last week's wording.",
                    ],
                ),
            ),
        },
        {
            "title": "Research: ask one long document its own questions",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 12,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Paste or import a long document as a note, then use the "
                        "Ask button on that note. The question is scoped to that "
                        "one document instead of the whole workspace, and the "
                        "answer cites the parts it used.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Save the answer as a new note and you have a summary "
                        "layer over your raw material — one that is itself "
                        "searchable next time.",
                    ],
                ),
            ),
        },
        {
            "title": "Studying: put a deadline and its reading in one place",
            "status": "Open",
            "priority": "Low",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 12,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Make a project for one course, a milestone for its next "
                        "exam or hand-in, and a notes folder for the lectures. "
                        "Then ask Genos about your own notes — \"explain what I "
                        "wrote about elasticity\".",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Every course's due dates end up on one calendar. The "
                        "to-do pane beside chat is a per-day list, which suits a "
                        "study session better than real coursework tasks do.",
                    ],
                ),
            ),
        },
        {
            "title": "Operations: make a procedure repeatable",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 13,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Write one recurring procedure — onboarding, invoicing, a "
                        "monthly close — as a note with checkboxes, and make one "
                        "task per run of it. A project owner can also set a "
                        "reusable task template and mark fields required.",
                    ],
                ),
                (
                    "💡 Why it helps",
                    [
                        "Each run starts from the same steps instead of somebody's "
                        "memory, and every task keeps a trail of who changed what "
                        "and when — usually the thing an audit is actually asking "
                        "for.",
                    ],
                ),
            ),
        },
        {
            "title": "Anyone: turn a conversation into tracked work",
            "status": "Open",
            "priority": "Normal",
            "assignee": "demo",
            "is_milestone_child": True,
            "parent_idx": None,
            "due_offset_days": 13,
            "body": _body(
                (
                    "🧾 Try this",
                    [
                        "Next time a chat message contains something you have to "
                        "do, flag it for yourself, or link the thread to a task. "
                        "\"We should do this\" becomes something with an owner and "
                        "a date, without retyping it.",
                    ],
                ),
            ),
        },
    ],
}


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

        # The by-trade examples, as a second milestone inside the same
        # project — not a second project, which would double what a user
        # has to delete to clean up, and would sit in the sidebar looking
        # like real work.
        examples_milestone, examples_backing_task = _create_milestone_with_backing_task(
            team, project, sprint, user, [], EXAMPLES_BLUEPRINT["milestone"]
        )
        _create_blueprint_tasks(
            team,
            project,
            sprint,
            examples_milestone,
            examples_backing_task,
            user,
            [],
            EXAMPLES_BLUEPRINT["tasks"],
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

        # The full user manual — the "Genos Guide" my-notes folder, the
        # same seed every member gets on joining a team (guide_notes.py).
        # Seeding here means the creator has it before their first
        # /team/join/ round-trip; the folder guard makes that a no-op.
        seed_guide_notes(user, team)

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
        ToDoItem.objects.create(
            group=group,
            category=cat,
            title="Do the one example that matches your job",
            notes=_text_body(
                "The 'Try Genos on real work' milestone has one for engineering, planning, "
                "marketing, research, studying and operations. Yours is in there; the rest "
                "are meant to be deleted."
            ),
            sort_order=2,
        )

    # After commit, exactly like the demo path: content only becomes
    # searchable once OpenSearch has it, and the reindex must not run
    # inside the transaction.
    kick_off_demo_reindex()
