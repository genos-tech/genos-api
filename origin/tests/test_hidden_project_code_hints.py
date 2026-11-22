"""Hidden project leftovers of MIP-1935 part 2 (MIP-1957): code race, display-id lookup, system-user email."""

from __future__ import annotations

import re
from unittest import mock

from django.contrib.auth import get_user_model

from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.search_engine.agent.tools.base import ToolError
from origin.services import hidden_scope
from origin.services.project_manage import create_project
from origin.tests.test_base import BaseAPITestCase
from origin.views.public.mcp.resolve import resolve_task_ref

PROJECT = "/api/v2/project/"


class _Base(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.hidden = ProjectMaster.objects.create(
            team=self.team,
            project_name="Codename Falcon",
            code="FAL",
            owner=self.user,
            project_system_user=self.user,
            is_private=True,
            is_hidden=True,
        )
        ProjectMembers.objects.create(team=self.team, project=self.hidden, attendee=self.user)
        TaskMaster.objects.create(
            team=self.team,
            project=self.hidden,
            reporter=self.user,
            title="Secret",
            status="Open",
            project_task_number=1,
        )


class CodeRaceTests(_Base):
    """Two creates that read the same free code: the loser retries with the next one."""

    def _stale_codes(self):
        # What a concurrent create sees: the code the winner is about to take is still free.
        return mock.patch.object(
            hidden_scope, "taken_project_codes", return_value={"FAL"}, create=False
        )

    def test_service_create_survives_a_code_taken_since_it_was_read(self):
        winner = ProjectMaster.objects.create(
            team=self.team, project_name="Alpha Beta", code="ALP", owner=self.user
        )
        calls = []

        def stale(team_id):
            # first read is stale (ALP looks free), later reads are fresh
            calls.append(1)
            if len(calls) == 1:
                return set()
            return set(ProjectMaster.objects.filter(team=team_id).values_list("code", flat=True))

        with mock.patch("origin.services.project_manage.taken_project_codes", side_effect=stale):
            mine = create_project(team=self.team, owner=self.user2, name="Alpha Gamma")
        self.assertNotEqual(mine.code, winner.code)
        self.assertEqual(ProjectMaster.objects.filter(team=self.team, code=mine.code).count(), 1)

    def test_api_create_survives_a_code_taken_since_it_was_read(self):
        from django.db import IntegrityError

        from origin.serializers.project.prj_serializers import ProjectMasterSerializer

        real_save = ProjectMasterSerializer.save
        calls = []

        def racing_save(serializer, **kw):
            calls.append(1)
            if len(calls) == 1:
                # the winner of the race commits the code between our read and our insert
                ProjectMaster.objects.create(
                    team=self.team,
                    project_name="Winner",
                    code=serializer.validated_data["code"],
                    owner=self.user,
                )
                raise IntegrityError("project_code_unique_per_team")
            return real_save(serializer, **kw)

        self.client.force_authenticate(self.user2)
        with mock.patch.object(ProjectMasterSerializer, "save", racing_save):
            res = self.client.post(
                PROJECT,
                {
                    "team": str(self.team.team_id),
                    "owner": self.user2.id,
                    "project_name": "Alpha Gamma",
                    "code": "ALP",
                },
                format="json",
            )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(calls), 2)  # the first try lost, the second won
        codes = list(ProjectMaster.objects.filter(team=self.team).values_list("code", flat=True))
        self.assertEqual(len(codes), len(set(codes)))


class DisplayIdLookupTests(_Base):
    def _message(self, ref, user_id):
        with self.assertRaises(ToolError) as cm:
            resolve_task_ref(ref, str(self.team.team_id), user_id=str(user_id))
        return str(cm.exception)

    def test_non_member_gets_the_missing_answer_for_a_hidden_code(self):
        hidden = self._message("FAL-1", self.user2.id)
        missing = self._message("ZZZ-1", self.user2.id)
        self.assertEqual(hidden.replace("FAL-1", "X"), missing.replace("ZZZ-1", "X"))
        self.assertNotRegex(hidden, r"\d{3,}")

    def test_a_hidden_task_number_that_does_not_exist_reads_the_same(self):
        self.assertEqual(
            self._message("FAL-99", self.user2.id).replace("FAL-99", "X"),
            self._message("FAL-1", self.user2.id).replace("FAL-1", "X"),
        )

    def test_member_still_resolves_it(self):
        task_id = resolve_task_ref("FAL-1", str(self.team.team_id), user_id=str(self.user.id))
        self.assertEqual(task_id, TaskMaster.objects.get(project=self.hidden).task_id)

    def test_a_member_of_a_visible_project_still_resolves_it(self):
        open_project = ProjectMaster.objects.create(
            team=self.team, project_name="Open", code="OPN", owner=self.user
        )
        ProjectMembers.objects.create(team=self.team, project=open_project, attendee=self.user2)
        task = TaskMaster.objects.create(
            team=self.team,
            project=open_project,
            reporter=self.user,
            title="Pub",
            status="Open",
            project_task_number=1,
        )
        self.assertEqual(
            resolve_task_ref("OPN-1", str(self.team.team_id), user_id=str(self.user2.id)),
            task.task_id,
        )


class SystemUserEmailTests(_Base):
    SUFFIXED = re.compile(r"^.+-[0-9a-f]{6}@genos\.tech$")

    def test_email_shape_does_not_tell_whether_a_name_was_used_before(self):
        fresh = create_project(team=self.team, owner=self.user2, name="Brand New")
        # the address an earlier (hidden or deleted) project of that name took
        User = get_user_model()
        User.objects.create_user(
            email=f"{self.team.team_id}-Reused@genos.tech", username="old", password=None
        )
        second = create_project(team=self.team, owner=self.user2, name="Reused")
        for project in (fresh, second):
            self.assertRegex(project.project_system_user.email, self.SUFFIXED)
