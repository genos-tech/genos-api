"""`create_reviewer_account` (MIP-1113): the sign-in OpenAI's app reviewers use.

What is pinned, and why each matters:

  * **Dry-run writes nothing**, and it is the default: the command is run in
    production by hand, so the no-flag run must be safe.
  * **Apply builds a usable account**: a non-demo user, email verified, owning
    a Pro team (operator-set) seeded by the demo seeder, signing in with the
    printed password and no code step.
  * **It is not a demo account**: sign-out and the `cleanup_demo_users` sweep
    delete demo workspaces. A reviewer whose data vanished after the first
    sign-out is the worst way this can fail.
  * **The MCP gate opens** for it, through the same check the MCP endpoint
    runs, against the production tier table, with a Free control that stays shut.
  * **Idempotent**: a re-run reports; `--reset-password` changes only the hash.
  * **It refuses** any address that is not a reviewer's (a real user, a demo
    visitor), and changes nothing when it does.
  * **The password** is printed exactly once, and nowhere else: not in logs,
    stderr or the audit row.
"""

from __future__ import annotations

import logging
import re
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from origin.models.chat.unified_models import Channel, Message
from origin.models.common.api_key_models import SCOPE_READ, ApiKey, generate_key, hash_key
from origin.models.common.security_event_models import SecurityEvent, SecuritySource
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import TIER_SET_BY_OPERATOR
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.milestone_models import MilestoneMaster
from origin.models.task.task_models import TaskComments, TaskMaster
from origin.search_engine import quota

User = get_user_model()

EMAIL = "reviewer@example.com"
SIGNIN = "/api/v2/user/signin/"
SIGNOUT = "/api/v2/user/signout/"
MCP = "/api/public/v1/mcp"
PASSWORD_LINE = re.compile(r"^Password \(shown once.*\): (\S+)$", re.MULTILINE)


def _production_quotas():
    """The live tier table: settings_test only changes `schedules_enabled`, but the
    gate must be proven against what production serves."""
    return {**settings.SEARCH_ENGINE, "TIER_QUOTAS": settings.PRODUCTION_TIER_QUOTAS}


def _run(*args, **kwargs):
    out, err = StringIO(), StringIO()
    call_command("create_reviewer_account", *args, stdout=out, stderr=err, **kwargs)
    return out.getvalue(), err.getvalue()


def _counts():
    return {
        "users": User.objects.count(),
        "teams": TeamMaster.objects.count(),
        "members": TeamMembers.objects.count(),
        "projects": ProjectMaster.objects.count(),
        "tasks": TaskMaster.objects.count(),
        "comments": TaskComments.objects.count(),
        "channels": Channel.objects.count(),
        "messages": Message.objects.count(),
        "events": SecurityEvent.objects.count(),
    }


class _CaptureLogs:
    """Every record any logger handles while active (the `origin` logger does not
    propagate to the root, so a root handler alone would miss it)."""

    def __init__(self):
        self.lines: list[str] = []

    def __enter__(self):
        original = logging.Logger.handle
        lines = self.lines

        def handle(logger_self, record):
            lines.append(record.getMessage())
            return original(logger_self, record)

        self._patch = mock.patch.object(logging.Logger, "handle", handle)
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


def _normal_user(email="someone@example.com", **extra):
    return User.objects.create_user(
        email=email, username="Someone", password="a-real-password-1", **extra
    )


class DryRunTests(TestCase):
    def test_no_flag_is_a_dry_run_and_writes_nothing(self):
        before = _counts()
        out, _ = _run("--email", EMAIL)
        self.assertEqual(_counts(), before)
        self.assertIn("[dry-run]", out)
        self.assertIn("Nothing written", out)
        self.assertIsNone(PASSWORD_LINE.search(out))
        self.assertFalse(User.objects.with_email(EMAIL).exists())

    def test_explicit_dry_run_writes_nothing(self):
        before = _counts()
        out, _ = _run("--email", EMAIL, "--dry-run")
        self.assertEqual(_counts(), before)
        self.assertIsNone(PASSWORD_LINE.search(out))

    def test_yes_without_apply_is_still_a_dry_run(self):
        before = _counts()
        _run("--email", EMAIL, "--yes")
        self.assertEqual(_counts(), before)

    def test_apply_needs_yes(self):
        before = _counts()
        with self.assertRaisesMessage(CommandError, "--yes"):
            _run("--email", EMAIL, "--apply")
        self.assertEqual(_counts(), before)

    def test_dry_run_and_apply_are_exclusive(self):
        with self.assertRaises(CommandError):
            _run("--email", EMAIL, "--apply", "--dry-run", "--yes")

    def test_free_is_not_an_offered_plan(self):
        with self.assertRaises(CommandError):
            _run("--email", EMAIL, "--plan", "free")

    def test_reset_password_for_nobody_is_refused(self):
        with self.assertRaisesMessage(CommandError, "existing reviewer"):
            _run("--email", EMAIL, "--reset-password", "--apply", "--yes")


class RefusalTests(TestCase):
    """An address that is not a reviewer's is refused and nothing changes."""

    def _assert_refused(self, user, email=None):
        before = _counts()
        password_hash = user.password
        for args in ((), ("--apply", "--yes"), ("--reset-password", "--apply", "--yes")):
            with self.assertRaisesMessage(CommandError, "not a reviewer account"):
                _run("--email", email or user.email, *args)
        self.assertEqual(_counts(), before)
        user.refresh_from_db()
        self.assertEqual(user.password, password_hash)

    def test_existing_normal_user_is_refused(self):
        self._assert_refused(_normal_user())

    def test_address_match_ignores_case(self):
        user = _normal_user(email="Someone@Example.com")
        self._assert_refused(user, email="someone@example.com")

    def test_normal_user_who_owns_a_normal_team_is_refused(self):
        user = _normal_user()
        TeamMaster.objects.create(team_name="Real team", team_email="real@example.com", owner=user)
        self._assert_refused(user)

    def test_demo_visitor_is_refused(self):
        # A demo visitor owns a demo team too; only a NON-demo owner is a reviewer.
        user = _normal_user(email="demo+abc@genos.app", is_demo=True)
        TeamMaster.objects.create(
            team_name="Product & Design",
            team_email="demo-team-abc@genos.app",
            owner=user,
            is_demo=True,
        )
        self._assert_refused(user)

    def test_lookalike_team_email_does_not_make_a_reviewer(self):
        # `team_email` is user-supplied at team creation, so it is not the marker.
        user = _normal_user()
        TeamMaster.objects.create(
            team_name="Genos Review", team_email="demo-team-12345678@genos.app", owner=user
        )
        self._assert_refused(user)

    def test_team_name_in_use_is_refused(self):
        TeamMaster.objects.create(team_name="Genos Review", team_email="x@example.com")
        before = _counts()
        with self.assertRaisesMessage(CommandError, "already exists"):
            _run("--email", EMAIL, "--apply", "--yes")
        self.assertEqual(_counts(), before)


class ApplyTests(TestCase):
    """One full seed shared by every test (each test rolls back to it)."""

    @classmethod
    def setUpTestData(cls):
        with _CaptureLogs() as logs:
            cls.out, cls.err = _run("--email", EMAIL, "--apply", "--yes")
        cls.log_lines = logs.lines
        match = PASSWORD_LINE.search(cls.out)
        assert match, cls.out
        cls.password = match.group(1)
        cls.user = User.objects.with_email(EMAIL).get()
        cls.team = TeamMaster.objects.get(owner=cls.user)

    def setUp(self):
        quota.invalidate_effective_tier([str(self.user.id)])

    def tearDown(self):
        quota.invalidate_effective_tier([str(self.user.id)])

    # -- the account --

    def test_user_is_a_verified_non_demo_account_with_the_printed_password(self):
        user = User.objects.get(pk=self.user.pk)
        self.assertFalse(user.is_demo)
        self.assertTrue(user.is_email_verified)
        self.assertTrue(user.is_active)
        self.assertEqual(user.primary_auth_provider, "email")
        self.assertTrue(user.check_password(self.password))
        self.assertGreaterEqual(len(self.password), 32)

    def test_team_is_the_named_pro_team_set_by_an_operator(self):
        team = TeamMaster.objects.get(pk=self.team.pk)
        self.assertEqual(team.team_name, "Genos Review")
        self.assertEqual(team.plan, "pro")
        self.assertEqual(team.plan_set_by, TIER_SET_BY_OPERATOR)
        self.assertEqual(team.owner_id, self.user.id)
        # The seeder's flag stays: with a non-demo owner it is the reviewer marker.
        self.assertTrue(team.is_demo)
        self.assertTrue(TeamMembers.objects.filter(team=team, attendee=self.user).exists())

    def test_workspace_holds_the_sample_data(self):
        tasks = TaskMaster.objects.filter(team=self.team, is_deleted=False, is_milestone=False)
        self.assertGreaterEqual(ProjectMaster.objects.filter(team=self.team).count(), 1)
        self.assertGreater(tasks.count(), 5)
        self.assertGreaterEqual(len(set(tasks.values_list("status", flat=True))), 3)
        self.assertGreaterEqual(MilestoneMaster.objects.filter(team=self.team).count(), 1)
        self.assertGreater(TaskComments.objects.filter(task__team=self.team).count(), 0)
        self.assertGreater(Channel.objects.filter(team=self.team).count(), 0)
        self.assertGreater(Message.objects.filter(channel__team=self.team).count(), 0)
        from origin.models.note.personal_note_models import PersonalNoteMaster

        self.assertGreater(PersonalNoteMaster.objects.filter(team=self.team).count(), 0)

    def test_summary_names_ids_and_counts(self):
        self.assertIn(str(self.team.team_id), self.out)
        self.assertIn(str(self.user.id), self.out)
        for project_id in ProjectMaster.objects.filter(team=self.team).values_list(
            "project_id", flat=True
        ):
            self.assertIn(str(project_id), self.out)
        self.assertIn("counts:", self.out)
        self.assertNotIn(EMAIL, self.out)  # the address is masked

    # -- the password goes nowhere but stdout, once --

    def test_password_is_printed_exactly_once_and_nowhere_else(self):
        self.assertEqual(self.out.count(self.password), 1)
        self.assertNotIn(self.password, self.err)
        self.assertTrue(self.log_lines)  # the capture really ran
        for line in self.log_lines:
            self.assertNotIn(self.password, line)
        for event in SecurityEvent.objects.all():
            self.assertNotIn(self.password, repr(event.metadata))

    def test_creation_is_audited_as_an_operator_action(self):
        event = SecurityEvent.objects.get(
            target_id=str(self.user.id), metadata__event="reviewer_account_created"
        )
        self.assertEqual(event.source, SecuritySource.OPERATOR)
        self.assertEqual(event.metadata["command"], "create_reviewer_account")

    # -- sign-in: no code step --

    def test_signs_in_with_email_and_password_and_no_code(self):
        res = APIClient().post(SIGNIN, {"email": EMAIL, "password": self.password}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertTrue(body.get("access"))
        self.assertNotIn("email_not_verified", res.content.decode())

    # -- not a demo account: it survives sign-out and the sweep --

    def test_survives_sign_out(self):
        client = APIClient()
        res = client.post(SIGNIN, {"email": EMAIL, "password": self.password}, format="json")
        self.assertEqual(res.status_code, 200)
        res = client.post(SIGNOUT)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(TeamMaster.objects.filter(pk=self.team.pk).exists())
        self.assertTrue(TaskMaster.objects.filter(team=self.team).exists())

    def test_survives_the_demo_sweep(self):
        tasks = TaskMaster.objects.filter(team=self.team).count()
        call_command("cleanup_demo_users", hours=0, stdout=StringIO())
        self.assertTrue(User.objects.filter(pk=self.user.pk).exists())
        self.assertTrue(TeamMaster.objects.filter(pk=self.team.pk).exists())
        self.assertEqual(TaskMaster.objects.filter(team=self.team).count(), tasks)

    # -- the MCP gate --

    def test_mcp_gate_opens_for_the_reviewer_and_stays_shut_for_free(self):
        control = _normal_user(email="free@example.com")
        with override_settings(SEARCH_ENGINE=_production_quotas()):
            quota.invalidate_effective_tier([str(self.user.id), str(control.id)])
            self.assertTrue(quota.get_mcp_enabled(str(self.user.id)))
            # Without the control, a fail-open gate would pass this test for anyone.
            self.assertFalse(quota.get_mcp_enabled(str(control.id)))
        quota.invalidate_effective_tier([str(control.id)])

    def test_mcp_tools_list_answers_for_the_reviewer(self):
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user,
            team=self.team,
            name="review",
            key_hash=hash_key(raw),
            prefix=raw[:11],
            scope=SCOPE_READ,
        )
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        with override_settings(SEARCH_ENGINE=_production_quotas()):
            quota.invalidate_effective_tier([str(self.user.id)])
            res = client.post(
                MCP, {"jsonrpc": "2.0", "method": "tools/list", "id": 1}, format="json"
            )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["result"]["tools"])

    # -- idempotent re-run --

    def test_rerun_reports_and_changes_nothing(self):
        before = _counts()
        password_hash = User.objects.get(pk=self.user.pk).password
        for args in ((), ("--apply", "--yes")):
            out, _ = _run("--email", EMAIL, *args)
            self.assertIn("already exists", out)
            self.assertIn(str(self.team.team_id), out)
            self.assertIsNone(PASSWORD_LINE.search(out))
        self.assertEqual(_counts(), before)
        self.assertEqual(User.objects.get(pk=self.user.pk).password, password_hash)

    def test_rerun_never_renames_the_team(self):
        _run("--email", EMAIL, "--team-name", "Something Else", "--apply", "--yes")
        self.assertEqual(TeamMaster.objects.get(pk=self.team.pk).team_name, "Genos Review")

    def test_rerun_restores_a_drifted_plan(self):
        TeamMaster.objects.filter(pk=self.team.pk).update(plan="free", plan_set_by="stripe")
        out, _ = _run("--email", EMAIL, "--apply", "--yes")
        team = TeamMaster.objects.get(pk=self.team.pk)
        self.assertEqual((team.plan, team.plan_set_by), ("pro", TIER_SET_BY_OPERATOR))
        self.assertIsNone(PASSWORD_LINE.search(out))

    def test_dry_run_reset_changes_nothing(self):
        before = _counts()
        password_hash = User.objects.get(pk=self.user.pk).password
        out, _ = _run("--email", EMAIL, "--reset-password")
        self.assertIn("[dry-run]", out)
        self.assertIsNone(PASSWORD_LINE.search(out))
        self.assertEqual(_counts(), before)
        self.assertEqual(User.objects.get(pk=self.user.pk).password, password_hash)

    def test_reset_password_changes_the_hash_and_only_that(self):
        before = _counts()
        old_hash = User.objects.get(pk=self.user.pk).password
        with _CaptureLogs() as logs:
            out, err = _run("--email", EMAIL, "--reset-password", "--apply", "--yes")
        new_password = PASSWORD_LINE.search(out).group(1)
        self.assertEqual(out.count(new_password), 1)
        self.assertNotIn(new_password, err)
        for line in logs.lines:
            self.assertNotIn(new_password, line)
        self.assertNotEqual(new_password, self.password)

        user = User.objects.get(pk=self.user.pk)
        self.assertNotEqual(user.password, old_hash)
        self.assertTrue(user.check_password(new_password))
        self.assertFalse(user.check_password(self.password))
        # One audit row more, and no other row anywhere.
        self.assertEqual(_counts(), {**before, "events": before["events"] + 1})
        self.assertTrue(
            SecurityEvent.objects.filter(
                target_id=str(user.id), metadata__event="reviewer_password_reset"
            ).exists()
        )

        client = APIClient()
        res = client.post(SIGNIN, {"email": EMAIL, "password": self.password}, format="json")
        self.assertEqual(res.status_code, 401)
        res = client.post(SIGNIN, {"email": EMAIL, "password": new_password}, format="json")
        self.assertEqual(res.status_code, 200)

    def test_other_teams_are_untouched(self):
        other_owner = _normal_user(email="other@example.com")
        other = TeamMaster.objects.create(
            team_name="Other", team_email="other-team@example.com", owner=other_owner
        )
        _run("--email", EMAIL, "--reset-password", "--plan", "max", "--apply", "--yes")
        other.refresh_from_db()
        self.assertEqual(
            (other.team_name, other.plan, other.owner_id), ("Other", "free", other_owner.id)
        )
        self.assertEqual(TeamMaster.objects.get(pk=self.team.pk).plan, "max")
