"""Deleting a project drops its search chunks, whoever deletes it (MIP-1734).

A project delete cascades its tasks, milestones and notes and detaches its chat, and none of
those paths has a search hook: the chunks of a deleted project's task stayed searchable
(Spotlight, the agent, MCP) until the next `opensearch_reindex` pass swept the orphans.
`delete_project` now collects what it is about to take and purges it after the commit, so the
project menu's view, the agent's tool and the sample-data removal all get it from one place.
"""

from __future__ import annotations

from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.chat.unified_models import Channel, ChannelKind
from origin.models.note.task_note_models import TaskNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.agent.tools.delete_project import DELETE_PROJECT
from origin.search_engine.models import RagChunk
from origin.services.milestone_service import ensure_backing_task
from origin.services.project_delete import delete_project

from .test_base import BaseAPITestCase

User = get_user_model()

_BULK = "origin.search_engine.purge.os_helpers.bulk"
_CLIENT = "origin.search_engine.purge.get_client"


class _Seeded:
    """One project with a task, a milestone, a task note and a chat, each with chunks."""

    def make_project(self, name, code):
        project = ProjectMaster.objects.create(
            team=self.team,
            project_name=name,
            code=code,
            owner=self.user,
            project_system_user=self.user,
        )
        ProjectMembers.objects.create(team=self.team, project=project, attendee=self.user)
        return project

    def _chunk(self, chunk_id, etype, eid):
        return RagChunk.objects.create(
            chunk_id=chunk_id,
            entity_type=etype,
            entity_id=eid,
            chunk_type="x",
            team_id=self.team.team_id,
            text_hash="h",
            embedding_model="m",
        )

    def seed(self, project, tag):
        """Rows and chunks for one project. Returns the chunk ids that must go with it."""
        task = TaskMaster.objects.create(
            team=self.team,
            project=project,
            reporter=self.user,
            title=f"Task {tag}",
            status="Open",
            project_task_number=1,
        )
        milestone = MilestoneMaster.objects.create(
            team=self.team, project=project, reporter=self.user, title=f"Milestone {tag}"
        )
        ensure_backing_task(milestone)
        note = TaskNoteMaster.objects.create(
            team=self.team, project=project, task=task, owner=self.user, title=f"Note {tag}"
        )
        channel = Channel.objects.get(project=project, kind=ChannelKind.PM)
        chunks = [
            self._chunk(f"{tag}-task", "task", f"task:{task.task_id}"),
            # A comment is a chunk of the same entity.
            self._chunk(f"task:{task.task_id}:comment:7", "task", f"task:{task.task_id}"),
            self._chunk(f"{tag}-ms", "milestone", f"milestone:{milestone.milestone_id}"),
            self._chunk(f"{tag}-note", "note", f"note:task:{note.note_id}"),
            self._chunk(f"{tag}-notesum", "note_summary", f"note_summary:2:{note.note_id}"),
            self._chunk(f"{tag}-chat", "chat", f"pm:{channel.id}"),
            self._chunk(f"{tag}-thread", "chat", f"pm:{channel.id}:thread:abc"),
            self._chunk(
                f"{tag}-sum",
                "thread_summary",
                f"thread_summary:{int(ChannelKind.PM)}:{channel.id}:abc",
            ),
        ]
        return [c.chunk_id for c in chunks]

    def left(self, chunk_ids):
        return set(
            RagChunk.objects.filter(chunk_id__in=chunk_ids).values_list("chunk_id", flat=True)
        )


class _Contract(_Seeded):
    """The behaviour, run once per way of deleting. A subclass supplies `delete(project)`."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.doomed = self.make_project("Doomed", "DOO")
        self.doomed_id = self.doomed.project_id  # a deleted instance forgets its pk
        self.other = self.make_project("Other", "OTH")
        self.doomed_chunks = self.seed(self.doomed, "d")
        self.other_chunks = self.seed(self.other, "o")

    def run_delete(self, project):
        with (
            mock.patch(_BULK, return_value=(0, [])) as bulk,
            mock.patch(_CLIENT),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.delete(project)
        return bulk

    def test_every_chunk_of_the_project_goes(self):
        self.assertEqual(len(self.left(self.doomed_chunks)), 8)

        self.run_delete(self.doomed)

        self.assertFalse(ProjectMaster.objects.filter(project_id=self.doomed_id).exists())
        self.assertEqual(self.left(self.doomed_chunks), set())

    def test_another_projects_chunks_stay(self):
        self.run_delete(self.doomed)

        self.assertEqual(self.left(self.other_chunks), set(self.other_chunks))

    def test_an_opensearch_failure_does_not_undo_the_delete(self):
        with (
            mock.patch(_BULK, side_effect=ConnectionError("opensearch is down")),
            mock.patch(_CLIENT),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.delete(self.doomed)

        self.assertFalse(ProjectMaster.objects.filter(project_id=self.doomed_id).exists())
        # The tracking rows are kept so the next reindex's sweep retries.
        self.assertEqual(self.left(self.doomed_chunks), set(self.doomed_chunks))

    def test_a_bulk_that_reports_errors_does_not_undo_it_either(self):
        error = {"delete": {"_id": "d-task", "status": 500, "error": {"type": "boom"}}}
        with (
            mock.patch(_BULK, return_value=(0, [error])),
            mock.patch(_CLIENT),
            self.captureOnCommitCallbacks(execute=True),
        ):
            self.delete(self.doomed)

        self.assertFalse(ProjectMaster.objects.filter(project_id=self.doomed_id).exists())
        self.assertIn("d-task", self.left(self.doomed_chunks))

    def test_nothing_is_purged_before_the_commit(self):
        with mock.patch(_BULK, return_value=(0, [])) as bulk, mock.patch(_CLIENT):
            self.delete(self.doomed)  # no captureOnCommitCallbacks: the commit never comes

        bulk.assert_not_called()
        self.assertEqual(self.left(self.doomed_chunks), set(self.doomed_chunks))


class TheProjectMenuDeletePurgesSearch(_Contract, BaseAPITestCase):
    def delete(self, project):
        token = RefreshToken.for_user(self.user).access_token
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        response = self.client.delete(
            f"/api/v2/project/?team_id={self.team.team_id}&project_id={project.project_id}"
        )
        self.assertEqual(response.status_code, 204, response.content)


class TheAgentToolPurgesSearch(_Contract, BaseAPITestCase):
    def delete(self, project):
        DELETE_PROJECT.run(
            {"project_id": project.project_id, "project_name": project.project_name},
            ToolContext(team_id=str(self.team.team_id), user_id=str(self.user.id)),
        )


class TheServiceItself(_Contract, BaseAPITestCase):
    def delete(self, project):
        delete_project(project)

    def test_a_rolled_back_delete_purges_nothing(self):
        class Boom(Exception):
            pass

        with (
            mock.patch(_BULK, return_value=(0, [])) as bulk,
            mock.patch(_CLIENT),
            self.captureOnCommitCallbacks(execute=True),
        ):
            with self.assertRaises(Boom):
                with transaction.atomic():
                    delete_project(self.doomed)
                    raise Boom

        bulk.assert_not_called()
        self.assertTrue(ProjectMaster.objects.filter(project_id=self.doomed_id).exists())
        self.assertEqual(self.left(self.doomed_chunks), set(self.doomed_chunks))

    def test_a_project_with_nothing_in_it_deletes_quietly(self):
        empty = self.make_project("Empty", "EMP")
        empty_id = empty.project_id
        self.run_delete(empty)
        self.assertFalse(ProjectMaster.objects.filter(project_id=empty_id).exists())
