"""The refusal-burst alert sees refusals that never became a 403 (MIP-744).

`ForbiddenBurstMiddleware` counts HTTP 403s. The probing it exists to catch
doesn't produce them: a refused MCP tool call is HTTP 200 `isError: true`, and
a tenant guard answers 404 on purpose. These tests pin, per source, that the
refusal reaches the same per-caller counter, that a MISTAKE (a bad argument, an
id that never existed) does not, and that the caller is told exactly what they
were told before.
"""

import pathlib
import re

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from origin.models.common.api_key_models import SCOPE_READ, SCOPE_WRITE
from origin.models.common.security_event_models import SecurityAction, SecurityEvent
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.models.task.task_models import TaskMaster
from origin.services import throttles
from origin.tests.test_mcp_endpoint import McpBase

ALERT = SecurityAction.ALERT_FORBIDDEN_BURST


@override_settings(SECURITY_FORBIDDEN_BURST=3)
class McpRefusalBurstTests(McpBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        # A second team the caller has nothing to do with.
        self.stranger = self.user2
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.stranger
        )
        other_project = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Theirs",
            code="THR",
            owner=self.stranger,
            project_system_user=self.stranger,
        )
        ProjectMembers.objects.create(
            team=self.other_team, project=other_project, attendee=self.stranger
        )
        self.foreign_task = TaskMaster.objects.create(
            team=self.other_team,
            project=other_project,
            reporter=self.stranger,
            assignee=self.stranger,
            title="Not yours",
            status="Open",
            project_task_number=1,
        )

    def alerts(self):
        return list(SecurityEvent.objects.filter(action=ALERT))

    def _walk_foreign_task(self, n):
        for _ in range(n):
            self._call("get_task", {"task_id": self.foreign_task.task_id})

    # -- a refusal counts ----------------------------------------------------

    def test_walking_another_teams_tasks_over_mcp_raises_the_alert(self):
        self._key(scope=SCOPE_READ)

        self._walk_foreign_task(5)

        self.assertEqual(len(self.alerts()), 1, "one alert for the whole burst, not one per call")

    def test_a_read_only_key_asking_to_write_counts(self):
        self._key(scope=SCOPE_READ)

        for _ in range(3):
            res = self._call("create_task", {"title": "x", "project_id": self.project.project_id})
            self.assertTrue(self._result(res)["isError"])

        self.assertEqual(len(self.alerts()), 1)

    def test_a_destructive_tool_without_the_opt_in_counts(self):
        self._key(scope=SCOPE_WRITE, allow_destructive=False)

        for _ in range(3):
            res = self._call("delete_calendar_event", {"event_id": "abc"})
            self.assertTrue(self._result(res)["isError"])

        self.assertEqual(len(self.alerts()), 1)

    # -- the caller is told what they were told before ------------------------

    def test_the_caller_still_sees_an_ordinary_200_isError(self):
        self._key(scope=SCOPE_READ)

        res = self._call("get_task", {"task_id": self.foreign_task.task_id})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(self._result(res)["isError"])
        self.assertNotIn("alert", str(res.data).lower())

    # -- a mistake does not --------------------------------------------------

    def test_a_task_that_never_existed_does_not_count(self):
        self._key(scope=SCOPE_READ)

        for _ in range(10):
            self._call("get_task", {"task_id": 999999999})

        self.assertEqual(self.alerts(), [])

    def test_a_bad_argument_does_not_count(self):
        self._key(scope=SCOPE_WRITE)

        for _ in range(10):
            self._call("create_task", {"project_id": self.project.project_id})  # no title

        self.assertEqual(self.alerts(), [])

    def test_a_deleted_task_does_not_count(self):
        """A stale link to a row that was deleted is not probing."""
        self._key(scope=SCOPE_READ)
        TaskMaster.objects.filter(pk=self.task.pk).update(is_deleted=True)

        for _ in range(10):
            self._call("get_task", {"task_id": self.task.task_id})

        self.assertEqual(self.alerts(), [])

    # -- who it counts against ------------------------------------------------

    def test_two_keys_are_counted_separately(self):
        self._key(scope=SCOPE_READ)
        self._walk_foreign_task(2)
        self._key(scope=SCOPE_READ)  # a different key, same user

        self._walk_foreign_task(2)

        self.assertEqual(self.alerts(), [])

    def test_the_alert_names_the_key_that_probed(self):
        self._key(scope=SCOPE_READ)

        self._walk_foreign_task(3)

        row = self.alerts()[0]
        self.assertIsNotNone(row.api_key_id)
        self.assertEqual(row.metadata["refused"], 3)

    def test_with_the_cache_down_the_call_is_unaffected(self):
        from unittest import mock

        class Dead:
            def __getattr__(self, name):
                def fail(*a, **k):
                    from django_redis.exceptions import ConnectionInterrupted

                    raise ConnectionInterrupted(connection=None)

                return fail

        self._key(scope=SCOPE_READ)
        with mock.patch.object(throttles, "throttle_cache", return_value=Dead()):
            res = self._call("get_task", {"task_id": self.foreign_task.task_id})

        self.assertEqual(res.status_code, 200)
        self.assertTrue(self._result(res)["isError"])
        self.assertEqual(self.alerts(), [])


@override_settings(SECURITY_FORBIDDEN_BURST=3)
class TenantGuardBurstTests(McpBase):
    """A tenant guard answers 404 on purpose. A caller walking ids through it
    is probing, so a refusal on a row that EXISTS counts; an id that doesn't
    exist is a typo and does not. The answer is the same 404 either way."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(setattr, throttles, "_down_until", 0.0)
        self.outsider = self.user2
        self.other_team = TeamMaster.objects.create(
            team_name="Elsewhere", team_email="else@example.com", owner=self.outsider
        )
        self.other_project = ProjectMaster.objects.create(
            team=self.other_team,
            project_name="Theirs",
            code="THR",
            owner=self.outsider,
            project_system_user=self.outsider,
        )
        self.authenticate(self.user)

    def alerts(self):
        return list(SecurityEvent.objects.filter(action=ALERT))

    def _team(self, team_id):
        return self.client.get("/api/v2/task/getTeamTasks/", {"team_id": str(team_id)})

    def _project(self, project_id):
        return self.client.get(
            "/api/v2/project/",
            {"team_id": str(self.team.team_id), "project_id": str(project_id)},
        )

    def test_walking_other_teams_counts(self):
        for _ in range(5):
            self.assertEqual(self._team(self.other_team.team_id).status_code, 404)

        self.assertEqual(len(self.alerts()), 1)

    def test_walking_other_teams_projects_counts(self):
        for _ in range(5):
            self.assertEqual(self._project(self.other_project.project_id).status_code, 404)

        self.assertEqual(len(self.alerts()), 1)

    def test_ids_that_never_existed_do_not_count(self):
        import uuid

        for _ in range(10):
            self.assertEqual(self._team(uuid.uuid4()).status_code, 404)
            self.assertEqual(self._project(987654321).status_code, 404)

        self.assertEqual(self.alerts(), [])

    def test_a_malformed_id_does_not_count_or_crash(self):
        for _ in range(10):
            self.assertIn(self._team("not-a-uuid").status_code, (400, 404))

        self.assertEqual(self.alerts(), [])

    def test_a_deleted_team_does_not_count(self):
        TeamMaster.objects.filter(pk=self.other_team.pk).update(is_deleted=True)

        for _ in range(10):
            self._team(self.other_team.team_id)

        self.assertEqual(self.alerts(), [])

    def test_the_answer_is_identical_for_a_real_team_and_a_made_up_one(self):
        """Counting must not become a new oracle: a real team the caller is
        outside and an id that doesn't exist look exactly alike."""
        import uuid

        real = self._team(self.other_team.team_id)
        made_up = self._team(uuid.uuid4())

        self.assertEqual(real.status_code, made_up.status_code)
        self.assertEqual(real.data, made_up.data)
        self.assertEqual(real.get("Content-Type"), made_up.get("Content-Type"))

    def test_my_own_team_is_never_counted(self):
        for _ in range(10):
            self.assertEqual(self._team(self.team.team_id).status_code, 200)

        self.assertEqual(self.alerts(), [])

    def test_one_refusal_is_counted_once(self):
        """A 404 from a guard must not ALSO be seen by the 403 middleware, or a
        probe would trip the alert at half the intended rate."""
        for _ in range(2):
            self._team(self.other_team.team_id)

        self.assertEqual(self.alerts(), [], "2 refusals are under a threshold of 3")

        self._team(self.other_team.team_id)

        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.alerts()[0].metadata["refused"], 3)

    def test_another_callers_refusals_are_not_mine(self):
        """Counted per caller: one user's 403 must not push another over."""
        from rest_framework.test import APIClient

        self._team(self.other_team.team_id)
        self._team(self.other_team.team_id)
        other = APIClient()
        other.force_authenticate(self.user2)  # a viewer of self.team
        for _ in range(5):
            other.put(
                "/api/v2/team/profile/",
                {"team_id": str(self.team.team_id), "team_name": "x"},
                format="json",
            )

        # self.user is at 2; user2's own burst is a different caller's.
        self.assertEqual([a.actor_id for a in self.alerts()], [self.user2.id])

    def test_a_real_403_and_guard_404s_share_one_window(self):
        """One prober uses whatever surface works: a 403 from a viewer write and
        404s from the tenant guards add up to the one threshold."""
        viewer = self.user2
        TeamMembers.objects.get_or_create(team=self.other_team, attendee=viewer)
        other_team_task_list = "/api/v2/task/getTeamTasks/"
        self.authenticate(viewer)

        # viewer: one real 403 (a rename it may not do) ...
        res = self.client.put(
            "/api/v2/team/profile/",
            {"team_id": str(self.team.team_id), "team_name": "x"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        # ... and two guard 404s for a team it has nothing to do with.
        third_party = TeamMaster.objects.create(
            team_name="Third", team_email="third@example.com", owner=self.user
        )
        for _ in range(2):
            self.assertEqual(
                self.client.get(
                    other_team_task_list, {"team_id": str(third_party.team_id)}
                ).status_code,
                404,
            )

        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.alerts()[0].metadata["refused"], 3)

    def test_with_the_cache_down_the_guard_still_answers(self):
        from unittest import mock

        class Dead:
            def __getattr__(self, name):
                def fail(*a, **k):
                    from django_redis.exceptions import ConnectionInterrupted

                    raise ConnectionInterrupted(connection=None)

                return fail

        with mock.patch.object(throttles, "throttle_cache", return_value=Dead()):
            res = self._team(self.other_team.team_id)

        self.assertEqual(res.status_code, 404)
        self.assertEqual(self.alerts(), [])


class ToolRefusalsAreForbiddenTests(SimpleTestCase):
    """A refusal raised as a plain `ToolError` is invisible to the burst alert
    (MIP-744), and a tool added later would silently join them. Every
    authorization-shaped raise in a tool must be a `ToolForbidden`.

    Source-level on purpose: there are ~280 raises and no registry of them, so
    this is the one check that sees a new one. A message that merely mentions
    permission is fine if it is listed here WITH a reason."""

    TOOLS = pathlib.Path(__file__).resolve().parent.parent / "search_engine" / "agent" / "tools"

    # Phrases that mean "the thing exists and you may not have it".
    REFUSAL = re.compile(
        r"not authori[sz]ed|belongs to a different team|is in a different team"
        r"|you are not a member|not a member of that",
        re.I,
    )

    # `raise ToolError(` messages that match REFUSAL but are not a refusal.
    # Keyed "file:fragment" so an exemption can't outlive the line it excuses.
    EXEMPT = {}

    def _plain_raises(self):
        pat = re.compile(r"raise ToolError\(((?:[^()]|\([^()]*\))*)\)", re.S)
        for path in sorted(self.TOOLS.glob("*.py")):
            src = path.read_text()
            for m in pat.finditer(src):
                text = " ".join(
                    re.findall(r'"([^"]*)"', m.group(1)) + re.findall(r"'([^']*)'", m.group(1))
                )
                yield path.name, src[: m.start()].count("\n") + 1, text

    def test_no_authorization_failure_is_a_plain_tool_error(self):
        leaks = [
            f"{name}:{line}  {text[:70]}"
            for name, line, text in self._plain_raises()
            if self.REFUSAL.search(text)
            and not any(
                key.startswith(f"{name}:") and key.split(":", 1)[1] in text for key in self.EXEMPT
            )
        ]

        self.assertEqual(
            leaks,
            [],
            "these refuse a caller but raise ToolError, so the refusal-burst alert "
            "cannot see them. Raise ToolForbidden (origin.search_engine.agent.tools."
            "base) at the guard, or add the line to EXEMPT with a reason.",
        )

    def test_the_guard_test_itself_can_see_a_refusal(self):
        """A scan that finds nothing proves nothing: it must flag a planted one."""
        planted = 'raise ToolError("Not authorized: task is in a different team.")'
        hits = [m for m in re.finditer(r"raise ToolError\(((?:[^()]|\([^()]*\))*)\)", planted)]

        self.assertEqual(len(hits), 1)
        self.assertTrue(self.REFUSAL.search(hits[0].group(1)))

    def test_the_converted_refusals_are_all_forbidden(self):
        forbidden = sum(
            len(re.findall(r"raise ToolForbidden\(", p.read_text()))
            for p in self.TOOLS.glob("*.py")
        )

        self.assertGreaterEqual(forbidden, 50, "the guards lost their ToolForbidden kind")
