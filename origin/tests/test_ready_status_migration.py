"""MIP-2602: migration 0308 renames only the built-in rows that still have the old default name."""

from importlib import import_module

from django.apps import apps

from origin.models.project.prj_models import ProjectStatus
from origin.services.project_statuses import ensure_project_statuses
from origin.tests.test_project_statuses import StatusTestBase

migration = import_module("origin.migrations.0308_rename_in_review_status_ready")


class RenameInReviewTests(StatusTestBase):
    def old_project(self, name, code):
        """A project set up before the rename."""
        project = self.make_project(name, code)
        ensure_project_statuses(project)
        ProjectStatus.objects.filter(project=project, builtin_key="In Review").update(
            name="In Review"
        )
        return project

    def review_name(self, project):
        return ProjectStatus.objects.get(project=project, builtin_key="In Review").name

    def test_only_unchanged_names_without_a_ready_are_renamed(self):
        unchanged = self.old_project("Unchanged", "UN")
        off = self.old_project("Turned off", "OF")
        ProjectStatus.objects.filter(project=off, builtin_key="In Review").update(
            archived_at="2026-01-01T00:00:00Z"
        )
        renamed = self.old_project("Renamed", "RE")
        ProjectStatus.objects.filter(project=renamed, builtin_key="In Review").update(name="QA")
        recased = self.old_project("Recased", "RC")
        ProjectStatus.objects.filter(project=recased, builtin_key="In Review").update(
            name="in review"
        )
        own = self.old_project("Own ready", "OR")
        ProjectStatus.objects.create(
            team=self.team, project=own, name="READY", category="WIP", sort_order=20
        )

        migration.rename(apps, None)

        self.assertEqual(
            [self.review_name(p) for p in (unchanged, off, renamed, recased, own)],
            ["Ready", "Ready", "QA", "in review", "In Review"],
        )
        self.assertTrue(ProjectStatus.objects.filter(project=own, name="READY").exists())

        migration.rename(apps, None)  # safe to run again
        self.assertEqual(self.review_name(own), "In Review")
