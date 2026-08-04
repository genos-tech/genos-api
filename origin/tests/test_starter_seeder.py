"""The opt-in starter workspace (readiness plan §3.3, PR 2).

The properties that matter, in order: it must NEVER inherit the demo
environment's self-destruct semantics (`is_demo`), it must be fully
owned by the real user with no bot residue, and a seeding failure must
never lose the team the user just created.
"""

from unittest import mock

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
from origin.services.starter_seeder import EXAMPLES_BLUEPRINT, STARTER_BLUEPRINT
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

        project = ProjectMaster.objects.get(team=team)
        self.assertEqual(project.project_name, "Getting started with Genos")
        self.assertEqual(str(project.owner_id), str(self.user.id))

        tasks = TaskMaster.objects.filter(team=team, project=project)
        # Both blueprints' tasks, plus one backing task per milestone.
        self.assertEqual(
            tasks.count(),
            len(STARTER_BLUEPRINT["tasks"]) + len(EXAMPLES_BLUEPRINT["tasks"]) + 2,
        )
        # Every task belongs to the real user — no bot residue possible.
        for task in tasks:
            self.assertEqual(str(task.assignee_id), str(self.user.id))
            self.assertEqual(str(task.reporter_id), str(self.user.id))

        self.assertEqual(
            ProjectTags.objects.filter(project=project).count(),
            len(STARTER_BLUEPRINT["tags"]),
        )
        self.assertEqual(TaskDependency.objects.filter(team=team).count(), 1)
        from origin.services.guide_notes import GUIDE_FOLDER_NAME, GUIDE_NOTES

        self.assertEqual(
            PersonalNoteMaster.objects.filter(team=team, owner=self.user).count(),
            len(GUIDE_NOTES),
        )
        from origin.models.note.personal_note_models import PersonalNoteFolder

        self.assertTrue(
            PersonalNoteFolder.objects.filter(
                team=team, owner=self.user, name=GUIDE_FOLDER_NAME
            ).exists()
        )
        self.assertEqual(
            ToDoItem.objects.filter(group__team=team, group__user=self.user).count(), 3
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
        project = ProjectMaster.objects.get(team=team)

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

    def test_demo_deletion_paths_cannot_reach_a_starter_workspace(self):
        # The demo environment self-destructs on logout; the starter
        # must be unreachable by that path because the USER is real.
        team = self._create_team(with_starter=True)
        delete_demo_environment(self.user)  # returns early: is_demo False
        self.assertTrue(TeamMaster.objects.filter(team_id=team.team_id).exists())
        self.assertTrue(ProjectMaster.objects.filter(team=team).exists())

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
