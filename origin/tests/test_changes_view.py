"""`GET /api/v2/changes/` (MIP-970 step 4).

Writes are asserted under `captureOnCommitCallbacks(execute=True)`: a token only
moves once the writing transaction has committed, and Django's `TestCase` never
commits (see `test_change_feed`).
"""

import json
from pathlib import Path
from unittest import mock

from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from origin.models.common.team_models import ExternalGrant, ShareStatus
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.project.prj_models import ProjectMaster, ProjectMembers
from origin.services import change_feed as cf
from origin.services import throttles
from origin.services.throttles import ChangeFeedThrottle
from origin.tests.security_clock import MidMinute
from origin.tests.test_change_feed import ChangeFeedCase

GOLDEN = Path(__file__).parent / "golden" / "changes_response.json"
URL = "/api/v2/changes/"

# Constant, not "small": see `test_the_cost_does_not_grow_with_the_workspace`.
MAX_QUERIES = 8


class ChangesViewCase(ChangeFeedCase):
    def setUp(self):
        super().setUp()
        self.authenticate(self.a_owner)

    def get(self, team=None, project=None, user=None, **extra):
        if user is not None:
            self.authenticate(user)
        params = {"team_id": str((team or self.team_a).team_id), **extra}
        if project is not None:
            params["project_id"] = project.project_id
        return self.client.get(URL, params)

    def tokens(self, **kwargs):
        res = self.get(**kwargs)
        self.assertEqual(res.status_code, 200, res.data)
        return res.data["tokens"]

    def diff(self, before, after):
        return {name for name in after if before[name] != after[name]}

    def write(self, fn):
        with self.captureOnCommitCallbacks(execute=True):
            fn()


class ChangesContractTests(ChangesViewCase):
    def test_the_response_has_exactly_the_shared_contracts_keys(self):
        contract = json.loads(GOLDEN.read_text())
        res = self.get(project=self.project)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(set(res.data), set(contract))
        self.assertEqual(set(res.data["tokens"]), set(contract["tokens"]))
        # Every token is an opaque string (or null for the project one).
        for name, token in res.data["tokens"].items():
            self.assertIsInstance(token, str, name)

    def test_the_collections_are_the_six_the_client_drives(self):
        self.assertEqual(
            list(self.tokens(project=self.project)),
            [
                "tasks_project",
                "tasks_team",
                "projects",
                "notes_personal",
                "notes_task",
                "notes_chat",
            ],
        )
        self.assertEqual(list(cf.COLLECTIONS), list(self.tokens(project=self.project)))

    def test_an_untouched_workspace_reads_zero_everywhere(self):
        cf.reset_pending()
        ChangeVersionCleaner.clear()
        self.assertEqual(
            self.tokens(project=self.project),
            {
                "tasks_project": "0",
                "tasks_team": "0",
                "projects": "0",
                "notes_personal": "0",
                "notes_task": "0",
                "notes_chat": "0",
            },
        )

    def test_the_answer_is_never_cacheable(self):
        self.assertEqual(self.get().headers["Cache-Control"], "no-store")


class ChangeVersionCleaner:
    @staticmethod
    def clear():
        from origin.models.common.change_models import ChangeVersion

        ChangeVersion.objects.all().delete()


class ChangesAccessTests(ChangesViewCase):
    def test_signed_out_is_refused(self):
        self.unauthenticate()
        self.assertEqual(
            self.client.get(URL, {"team_id": str(self.team_a.team_id)}).status_code, 401
        )

    def test_a_team_id_is_required(self):
        self.assertEqual(self.client.get(URL).status_code, 400)

    def test_a_stranger_gets_the_not_found_other_team_endpoints_give(self):
        res = self.get(team=self.team_a, user=self.c_owner)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data, {"error": "Team not found."})

    def test_a_refused_stranger_is_not_counted_toward_the_forbidden_burst_alert(self):
        # A tab left open by someone just removed from the team polls this every
        # 30-60 s; counting each as a refusal would trip the MIP-744 alert for a
        # stale client. The other endpoints the client polls never counted either.
        with mock.patch("origin.services.security_events.note_forbidden") as counted:
            for _ in range(25):
                self.assertEqual(self.get(team=self.team_a, user=self.c_owner).status_code, 404)
        counted.assert_not_called()

    def test_a_deleted_team_is_the_same_404(self):
        self.team_a.is_deleted = True
        self.team_a.save()
        res = self.get()
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.data, {"error": "Team not found."})

    def test_an_unknown_or_malformed_team_is_the_same_404(self):
        for bad in ("00000000-0000-0000-0000-000000000000", "abc", "1"):
            res = self.client.get(URL, {"team_id": bad})
            self.assertEqual(res.status_code, 404, bad)
            self.assertEqual(res.data, {"error": "Team not found."})

    def test_a_member_of_the_team_is_served(self):
        self.assertEqual(self.get(user=self.a_viewer).status_code, 200)

    def test_project_token_only_for_a_project_the_caller_is_on(self):
        # a_viewer is on the team but not on either project.
        self.assertIsNone(self.tokens(project=self.project, user=self.a_viewer)["tasks_project"])
        # The owner is on it.
        self.assertIsNotNone(self.tokens(project=self.project, user=self.a_owner)["tasks_project"])
        # A project of another team: not theirs.
        self.assertIsNone(
            self.tokens(project=self.foreign_project, user=self.a_owner)["tasks_project"]
        )

    def test_project_token_is_null_without_or_with_a_bad_project_id(self):
        self.assertIsNone(self.tokens()["tasks_project"])
        for bad in ("abc", "", "0", "999999999"):
            res = self.get(project_id=bad)
            self.assertEqual(res.status_code, 200, bad)
            self.assertIsNone(res.data["tokens"]["tasks_project"], bad)

    def test_a_project_the_caller_cannot_open_never_reveals_activity(self):
        before = self.tokens(project=self.project, user=self.a_viewer)
        self.write(lambda: self.make_task(self.project))
        after = self.tokens(project=self.project, user=self.a_viewer)
        # `tasks_project` stays null for them (no id, no leak) ...
        self.assertIsNone(before["tasks_project"])
        self.assertIsNone(after["tasks_project"])
        # ... while the owner, who is on it, does see it move.
        owner_after = self.tokens(project=self.project, user=self.a_owner)
        self.assertNotEqual(owner_after["tasks_project"], "0")

    def test_the_team_id_is_used_as_sent_not_rewritten_to_the_projects_team(self):
        # A guest of A looking at A's project from inside team B's workspace.
        # `AuthenticatedAPIView` would rewrite team_id to A and answer with A's
        # team tokens; this view answers for B (the collections the client holds).
        def share():
            self.active_project_grant()
            ProjectMembers.objects.get_or_create(
                team=self.team_a, project=self.project, attendee=self.b_owner
            )

        self.write(share)  # committed: the grant's own bumps are not what is under test
        before = self.tokens(team=self.team_b, project=self.project, user=self.b_owner)

        # A write in B's own team moves B's team tokens ...
        self.write(lambda: cf.bump_notes(self.team_b.team_id, personal=True))
        after = self.tokens(team=self.team_b, project=self.project, user=self.b_owner)
        self.assertEqual(self.diff(before, after), {"notes_personal"})

        # ... and a write in the stranger team moves nothing.
        self.write(lambda: cf.bump_notes(self.team_c.team_id, personal=True))
        self.assertEqual(
            self.tokens(team=self.team_b, project=self.project, user=self.b_owner), after
        )


class ChangesTokenTests(ChangesViewCase):
    def test_a_task_write_moves_the_project_and_team_tokens_only(self):
        before = self.tokens(project=self.project)
        self.write(lambda: self.make_task(self.project))
        after = self.tokens(project=self.project)
        self.assertEqual(self.diff(before, after), {"tasks_project", "tasks_team"})

    def test_a_write_in_another_project_moves_the_team_token_not_this_projects(self):
        before = self.tokens(project=self.project)
        self.write(lambda: self.make_task(self.project2))
        after = self.tokens(project=self.project)
        self.assertEqual(self.diff(before, after), {"tasks_team"})

    def test_each_collection_moves_for_its_own_writes_and_no_others(self):
        cases = {
            "projects": lambda: ProjectMaster.objects.create(
                team=self.team_a,
                project_name="Another",
                owner=self.a_owner,
                project_system_user=self.a_owner,
            ),
            "notes_personal": lambda: PersonalNoteMaster.objects.create(
                team=self.team_a, owner=self.a_owner, title="n", body=[]
            ),
        }
        # A new project also carries its own tasks key and the names that task and
        # chat notes embed; a personal note moves nothing but itself.
        moved = {}
        for name, fn in cases.items():
            before = self.tokens(project=self.project)
            self.write(fn)
            moved[name] = self.diff(before, self.tokens(project=self.project))
        self.assertEqual(moved["notes_personal"], {"notes_personal"})
        self.assertEqual(moved["projects"], {"projects", "tasks_team", "notes_task", "notes_chat"})

    def test_nothing_moves_without_a_write(self):
        first = self.tokens(project=self.project)
        self.assertEqual(self.tokens(project=self.project), first)

    def test_a_write_in_another_team_moves_nothing_here(self):
        before = self.tokens(project=self.project)
        self.write(lambda: self.make_task(self.foreign_project))
        self.assertEqual(self.tokens(project=self.project), before)

    def test_tokens_do_not_move_before_the_commit(self):
        before = self.tokens(project=self.project)
        with self.captureOnCommitCallbacks(execute=False):
            self.make_task(self.project)
            self.assertEqual(self.tokens(project=self.project), before)
        cf.reset_pending()

    def test_a_guest_team_sees_the_hosts_writes(self):
        self.write(self.active_project_grant)
        before = self.tokens(team=self.team_b, user=self.b_owner)
        self.write(lambda: self.make_task(self.project))  # host team A's project
        after = self.tokens(team=self.team_b, user=self.b_owner)
        self.assertEqual(self.diff(before, after), {"tasks_team"})

    def test_a_team_that_is_nobodys_guest_ignores_other_teams(self):
        before = self.tokens(team=self.team_c, user=self.c_owner)
        self.write(lambda: self.make_task(self.project))
        self.write(lambda: cf.bump_team(self.team_b.team_id))
        self.assertEqual(self.tokens(team=self.team_c, user=self.c_owner), before)

    def test_a_revoked_grant_stops_folding_in_the_hosts_keys(self):
        grant = self.active_project_grant()
        grant.status = ShareStatus.REVOKED
        self.write(grant.save)
        settled = self.tokens(team=self.team_b, user=self.b_owner)
        self.write(lambda: self.make_task(self.project))
        self.assertEqual(self.tokens(team=self.team_b, user=self.b_owner), settled)

    def test_a_refetch_after_the_token_moves_returns_the_new_data(self):
        # End to end through the caches the project-tasks endpoint sits behind: a
        # client that sees the token move and refetches must get the new row.
        def project_tasks():
            res = self.client.get(
                "/api/v2/task/getProjectTasks/",
                {"team_id": str(self.team_a.team_id), "project_id": self.project.project_id},
            )
            self.assertEqual(res.status_code, 200)
            return res.data["data"]["tasks"]

        before = self.tokens(project=self.project)
        self.assertEqual(project_tasks(), [])
        self.write(lambda: self.make_task(self.project, title="fresh"))
        after = self.tokens(project=self.project)
        self.assertNotEqual(before["tasks_project"], after["tasks_project"])
        self.assertEqual([t["title"] for t in project_tasks()], ["fresh"])


class ChangesCostTests(ChangesViewCase):
    def queries(self, user=None):
        # One throwaway call first: the second is what a poll costs (caches warm).
        # Sign in OUTSIDE the measured window (it mints a token row).
        self.authenticate(user or self.a_owner)
        self.get(project=self.project)
        with CaptureQueriesContext(connection) as ctx:
            res = self.get(project=self.project)
        self.assertEqual(res.status_code, 200)
        return len(ctx), [q["sql"] for q in ctx.captured_queries]

    grown = 0

    def grow(self, n):
        """More of everything the answer could depend on: projects, notes,
        members and host teams."""
        for _ in range(n):
            ChangesCostTests.grown += 1
            i = ChangesCostTests.grown
            project = ProjectMaster.objects.create(
                team=self.team_a,
                project_name=f"grown {i}",
                owner=self.a_owner,
                project_system_user=self.a_owner,
            )
            ProjectMembers.objects.get_or_create(
                team=self.team_a, project=project, attendee=self.a_owner
            )
            PersonalNoteMaster.objects.create(
                team=self.team_a, owner=self.a_owner, title=f"n{i}", body=[]
            )
            member = self._member(self.team_a, f"grown_user_{i}", "viewer")
            ProjectMembers.objects.get_or_create(team=self.team_a, project=project, attendee=member)
            self._add_host(i)
        cf.reset_pending()

    def _add_host(self, i):
        """One more team that lends team A a project: one more host to read."""
        from origin.models.common.team_models import TeamMaster, TeamMembers
        from origin.services.external_grants import offer_grant, respond_to_grant
        from origin.services.member_roles import EDITOR
        from origin.services.team_connection import request_connection, respond_to_connection

        owner = self._user(f"host_owner_{i}")
        host = TeamMaster.objects.create(
            team_name=f"Host {i}", team_email=f"host{i}@example.com", owner=owner
        )
        TeamMembers.objects.create(team=host, attendee=owner)
        project = ProjectMaster.objects.create(
            team=host, project_name=f"Lent {i}", owner=owner, project_system_user=owner
        )
        connection_row = request_connection(host.team_id, self.team_a.team_id, owner)
        respond_to_connection(connection_row, self.a_owner, accept=True)
        grant = offer_grant(
            owner_team_id=host.team_id,
            guest_team_id=self.team_a.team_id,
            object_type=ExternalGrant.ObjectType.PROJECT,
            object_id=project.project_id,
            role_ceiling=EDITOR,
            actor=owner,
        )
        respond_to_grant(grant, self.a_owner, accept=True)

    def test_the_cost_is_a_small_constant(self):
        count, sql = self.queries()
        self.assertLessEqual(count, MAX_QUERIES, "\n".join(sql))

    def test_the_cost_for_a_plain_member_is_still_bounded(self):
        # Not the owner of the team or the project: the membership checks take
        # their longer path (owner lookup, then the membership row).
        ProjectMembers.objects.create(
            team=self.team_a, project=self.project, attendee=self.a_editor
        )
        cf.reset_pending()
        count, sql = self.queries(user=self.a_editor)
        self.assertLessEqual(count, MAX_QUERIES, "\n".join(sql))

    def test_the_cost_does_not_grow_with_the_workspace(self):
        small, _ = self.queries()
        self.grow(3)
        medium, _ = self.queries()
        self.grow(6)
        large, sql = self.queries()
        self.assertEqual(small, medium, "\n".join(sql))
        self.assertEqual(small, large, "\n".join(sql))

    def test_the_versions_are_read_in_one_query(self):
        self.write(lambda: self.make_task(self.project))
        _, sql = self.queries()
        reads = [q for q in sql if 'FROM "origin_changeversion"' in q]
        self.assertEqual(len(reads), 1, "\n".join(sql))


class ChangesThrottleTests(MidMinute, ChangesViewCase):
    # The `3/min` below counts in fixed minute windows, read through `throttles.time` (MIP-1007).
    clock_modules = (throttles,)

    def test_a_script_is_held_off_and_a_person_is_not(self):
        cache.clear()
        with mock.patch.object(ChangeFeedThrottle, "rate", "3/min"):
            codes = [self.get().status_code for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])

    def test_the_default_leaves_room_for_many_tabs_and_focus_bursts(self):
        from origin.services.throttles import parse_rate

        num, period = parse_rate(ChangeFeedThrottle.rate)
        per_minute = num * 60 / period
        # Five tabs polling every 30 s plus a handful of focus bursts is ~20/min.
        self.assertGreaterEqual(per_minute, 60)
