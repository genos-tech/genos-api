"""The opt-in starter workspace (readiness plan §3.3, PR 2).

The properties that matter, in order: it must NEVER inherit the demo
environment's self-destruct semantics (`is_demo`), it must be fully
owned by the real user with no bot residue, and a seeding failure must
never lose the team the user just created.
"""

from unittest import mock

from django.utils import timezone
from rest_framework import status

from origin.models.chat.todo_models import ToDoItem
from origin.models.chat.unified_models import Channel
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import CustomUser
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectTags
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskDependency, TaskMaster
from origin.services.demo_seeder import delete_demo_environment
from origin.services.starter_seeder import (
    EXAMPLES_BLUEPRINT,
    JAPANESE_EXAMPLES_BLUEPRINT,
    JAPANESE_STARTER_BLUEPRINT,
    STARTER_BLUEPRINT,
)
from origin.tests.test_base import BaseAPITestCase


class StarterWorkspaceTests(BaseAPITestCase):
    URL = "/api/v2/team/create/"

    def _create_team(self, with_starter=True):
        self.authenticate()
        payload = {
            "team_name": "Acme",
            "team_email": "acme@example.com",
            "owner_id": str(self.user.id),
        }
        if with_starter is not None:
            payload["with_starter"] = with_starter
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            resp = self.client.post(self.URL, payload, format="json")
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        return TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])

    def test_opt_in_seeds_the_full_starter_content(self):
        team = self._create_team(with_starter=True)

        project_blueprints = (
            (STARTER_BLUEPRINT, EXAMPLES_BLUEPRINT),
            (JAPANESE_STARTER_BLUEPRINT, JAPANESE_EXAMPLES_BLUEPRINT),
        )
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 2)
        for starter_blueprint, examples_blueprint in project_blueprints:
            project = ProjectMaster.objects.get(team=team, project_name=starter_blueprint["name"])
            self.assertEqual(str(project.owner_id), str(self.user.id))

            tasks = TaskMaster.objects.filter(team=team, project=project)
            # Both blueprints' tasks, plus one backing task per milestone.
            self.assertEqual(
                tasks.count(),
                len(starter_blueprint["tasks"]) + len(examples_blueprint["tasks"]) + 2,
            )
            # Every task belongs to the real user — no bot residue possible.
            for task in tasks:
                self.assertEqual(str(task.assignee_id), str(self.user.id))
                self.assertEqual(str(task.reporter_id), str(self.user.id))

            self.assertEqual(
                ProjectTags.objects.filter(project=project).count(),
                len(starter_blueprint["tags"]),
            )

        self.assertEqual(TaskDependency.objects.filter(team=team).count(), 2)
        from origin.services.guide_notes import (
            GUIDE_FOLDER_NAME,
            GUIDE_NOTES,
            JAPANESE_GUIDE_FOLDER_NAME,
            JAPANESE_GUIDE_NOTES,
        )

        self.assertEqual(
            PersonalNoteMaster.objects.filter(team=team, owner=self.user).count(),
            len(GUIDE_NOTES) + len(JAPANESE_GUIDE_NOTES),
        )
        from origin.models.note.personal_note_models import PersonalNoteFolder

        for folder_name in (GUIDE_FOLDER_NAME, JAPANESE_GUIDE_FOLDER_NAME):
            self.assertTrue(
                PersonalNoteFolder.objects.filter(
                    team=team, owner=self.user, name=folder_name
                ).exists()
            )
        self.assertEqual(
            ToDoItem.objects.filter(group__team=team, group__user=self.user).count(), 6
        )
        # Membership was seeded, so the client's follow-up join is a no-op.
        self.assertTrue(TeamMembers.objects.filter(team=team, attendee=self.user).exists())
        # The PM channel signal fired (per-row ProjectMembers save).
        self.assertTrue(Channel.objects.filter(project=project).exists())

    def test_examples_milestone_gives_every_trade_a_worked_example(self):
        # Signup never says what kind of work the person does, so the
        # second milestone carries one example per trade. Asserted by
        # name: silently losing one leaves that whole audience with an
        # onboarding tour that reads as "not for me", which is the exact
        # failure this milestone exists to prevent.
        team = self._create_team(with_starter=True)
        project = ProjectMaster.objects.get(team=team, project_name=STARTER_BLUEPRINT["name"])

        milestones = MilestoneMaster.objects.filter(team=team, project=project)
        self.assertEqual(milestones.count(), 2)
        examples = milestones.get(title=EXAMPLES_BLUEPRINT["milestone"]["title"])

        # The backing task carries no milestone FK, so this is exactly
        # the blueprint's tasks — the sub-task included, since it
        # inherits its parent's milestone.
        example_tasks = TaskMaster.objects.filter(milestone=examples)
        self.assertEqual(example_tasks.count(), len(EXAMPLES_BLUEPRINT["tasks"]))
        titles = " ".join(example_tasks.values_list("title", flat=True)).lower()
        for trade in (
            "engineering",
            "planning",
            "marketing",
            "research",
            "studying",
            "operations",
        ):
            self.assertIn(trade, titles)

        # One example is a sub-task of another, so the starter shows what
        # a task subtree looks like and not just a flat list.
        self.assertTrue(example_tasks.filter(parent_task_id__isnull=False).exists())

    def test_starter_never_touches_is_demo_and_no_bots_are_created(self):
        users_before = CustomUser.objects.count()
        team = self._create_team(with_starter=True)
        team.refresh_from_db()
        self.user.refresh_from_db()
        self.assertFalse(team.is_demo)
        self.assertFalse(self.user.is_demo)
        self.assertEqual(CustomUser.objects.count(), users_before)

    def test_starter_gets_one_active_sprint_and_no_demo_cadence(self):
        """`_create_sprint` is shared with the demo seeder, which grew a
        `prior_sprints` cadence (5 completed sprints + 1 active) so its
        backdated history has somewhere to live. That keyword defaults to
        0 for exactly this caller: a brand-new customer project has no
        history, and inventing completed sprints it never ran would be
        fabricated data in a real workspace.

        Nothing else pins this. The starter path calls `_create_sprint`
        positionally with no keywords, so a change to the *default* — or
        to the demo's call site made by editing the signature instead —
        would silently ship phantom sprints to paying customers, and
        every demo test would still pass.
        """
        from origin.models.task.sprint_models import Sprint, SprintConfig

        team = self._create_team(with_starter=True)
        for project in ProjectMaster.objects.filter(team=team):
            # Only the COUNT and STATUS are asserted. The name is not this
            # test's business: the starter path renames the sprint from its
            # blueprint's `sprint_name` right after creating it, so the
            # Japanese starter project legitimately reads "スプリント1" —
            # pinning "Sprint 1" here made the test fail on correct code.
            sprints = list(
                Sprint.objects.filter(project=project)
                .order_by("sequence_number")
                .values_list("sequence_number", "status")
            )
            self.assertEqual(
                sprints,
                [(1, "active")],
                f"{project.project_name!r} got {sprints} — a real customer workspace "
                "must start with exactly one active sprint, not the demo's cadence",
            )
            # The active sprint starts TODAY here (there is no history to
            # sit behind it), unlike the demo's mid-flight anchor.
            sprint = Sprint.objects.get(project=project)
            self.assertEqual(sprint.start_date, timezone.localdate())
            self.assertEqual(
                SprintConfig.objects.get(project=project).anchor_date,
                timezone.localdate(),
            )

    def test_demo_deletion_paths_cannot_reach_a_starter_workspace(self):
        # The demo environment self-destructs on logout; the starter
        # must be unreachable by that path because the USER is real.
        team = self._create_team(with_starter=True)
        delete_demo_environment(self.user)  # returns early: is_demo False
        self.assertTrue(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertEqual(ProjectMaster.objects.filter(team=team).count(), 2)

    def test_without_opt_in_nothing_is_seeded(self):
        team = self._create_team(with_starter=None)
        self.assertFalse(ProjectMaster.objects.filter(team=team).exists())
        self.assertFalse(PersonalNoteMaster.objects.filter(team=team).exists())

    def test_false_flag_is_not_truthy_matched(self):
        # Only the boolean true opts in — "true"-ish strings from odd
        # clients must not seed.
        self.authenticate()
        with mock.patch("origin.services.starter_seeder.kick_off_demo_reindex"):
            resp = self.client.post(
                self.URL,
                {
                    "team_name": "Acme2",
                    "team_email": "acme2@example.com",
                    "owner_id": str(self.user.id),
                    "with_starter": "true",
                },
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        team = TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])
        self.assertFalse(ProjectMaster.objects.filter(team=team).exists())

    def test_seeding_failure_does_not_lose_the_team(self):
        self.authenticate()
        with mock.patch(
            "origin.services.starter_seeder.create_starter_workspace",
            side_effect=RuntimeError("boom"),
        ):
            resp = self.client.post(
                self.URL,
                {
                    "team_name": "Acme3",
                    "team_email": "acme3@example.com",
                    "owner_id": str(self.user.id),
                    "with_starter": True,
                },
                format="json",
            )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, resp.data)
        team = TeamMaster.objects.get(team_id=resp.data["teamDetails"]["teamId"])
        self.assertFalse(ProjectMaster.objects.filter(team=team).exists())
