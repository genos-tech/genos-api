"""Incident-response tooling (MIP-865): `operator_cutoff` and the API off switches.

Two things are pinned, and each guard has a test that fails without it:

1. **A cut-off made from a shell leaves a trail, and really cuts off.**
   Every subcommand writes one `SecurityEvent` (source `operator`, the
   reason, the operator's OS user, ids, never an address), a `--dry-run`
   changes and writes nothing, a second run adds nothing, and the target
   really stops working: a JWT, a refresh token, a key on REST v1 and on MCP.
2. **The public API and MCP can be switched off by env**, answering 503
   before any key is looked up, without touching `TIER_QUOTAS`.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken
from rest_framework_simplejwt.tokens import RefreshToken

from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.security_event_models import (
    SecurityAction,
    SecurityEvent,
    SecuritySource,
)
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.search_engine import quota
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_endpoint import _mcp_enabled_everywhere

User = get_user_model()

ME = "/api/public/v1/me/"
MCP = "/api/public/v1/mcp"
APP_PROBE = "/api/v2/api-keys/"  # JWT-only, any signed-in user may call it
OPERATOR = "alice-oncall"


def _mint(user, team=None, name="ci"):
    raw = generate_key()
    key = ApiKey.objects.create(
        user=user, team=team, name=name, key_hash=hash_key(raw), prefix=raw[:11]
    )
    return key, raw


class CutoffBase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        patcher = mock.patch(
            "origin.management.commands.operator_cutoff.getpass.getuser", return_value=OPERATOR
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self, *args):
        out = StringIO()
        call_command("operator_cutoff", *args, stdout=out, stderr=StringIO())
        return out.getvalue()

    def events(self, action=None):
        qs = SecurityEvent.objects.filter(source=SecuritySource.OPERATOR)
        return qs.filter(action=action) if action else qs

    def all_event_text(self):
        """Every stored column of every event, as one string."""
        rows = SecurityEvent.objects.all().values()
        return json.dumps(list(rows), default=str)


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class UserCutoffTests(CutoffBase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([str(self.user.id), str(self.user2.id)])

    def test_event_is_recorded_as_an_operator_action_with_the_reason(self):
        key, _ = _mint(self.user)
        out = self.run_cmd("user", str(self.user.id), "--reason", "token leaked in a gist")
        row = self.events(SecurityAction.OPERATOR_USER_DEACTIVATED).get()
        self.assertEqual(row.source, SecuritySource.OPERATOR)
        self.assertIsNone(row.actor_id)
        self.assertEqual(row.actor_label, f"operator:{OPERATOR}")
        self.assertEqual((row.target_type, row.target_id), ("user", str(self.user.id)))
        self.assertEqual(row.metadata["reason"], "token leaked in a gist")
        self.assertEqual(row.metadata["operator"], OPERATOR)
        self.assertEqual(row.metadata["user_id"], str(self.user.id))
        self.assertEqual(row.metadata["api_key_ids"], [str(key.id)])
        self.assertIn(str(row.event_id), out)

    def test_no_plaintext_email_reaches_the_event_or_the_output(self):
        _, raw = _mint(self.user)
        out = self.run_cmd(
            "user", str(self.user.id), "--reason", "reported by kenji@example.com, see ticket"
        )
        text = self.all_event_text()
        for email in (self.user.email, "kenji@example.com"):
            self.assertNotIn(email, text)
            self.assertNotIn(email, out)
        self.assertIn("k***@example.com", text)  # the reason is masked, not dropped
        self.assertNotIn(raw, text)
        self.assertNotIn(raw, out)

    def test_deactivates_blacklists_sessions_and_revokes_keys(self):
        personal, _ = _mint(self.user, name="mine")
        team_key, _ = _mint(self.user, team=self.team, name="bot")
        other_key, _ = _mint(self.user2, name="not theirs")
        refresh = RefreshToken.for_user(self.user)
        self.run_cmd("user", str(self.user.id), "--reason", "compromised")
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        for k in (personal, team_key):
            k.refresh_from_db()
            self.assertIsNotNone(k.revoked_at)
        other_key.refresh_from_db()
        self.assertIsNone(other_key.revoked_at)
        # The refresh token can no longer be used to mint an access token.
        with self.assertRaises(TokenError):
            RefreshToken(str(refresh))
        self.assertTrue(BlacklistedToken.objects.filter(token__user=self.user).exists())

    def test_an_access_token_stops_working(self):
        self.authenticate(self.user)
        self.assertEqual(self.client.get(APP_PROBE).status_code, 200)
        self.run_cmd("user", str(self.user.id), "--reason", "compromised")
        self.assertEqual(self.client.get(APP_PROBE).status_code, 401)
        # Someone else's token is unaffected.
        self.authenticate(self.user2)
        self.assertEqual(self.client.get(APP_PROBE).status_code, 200)

    def test_the_users_keys_stop_working_on_rest_and_mcp(self):
        _, raw = _mint(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertEqual(self.client.get(ME).status_code, 200)
        self.run_cmd("user", str(self.user.id), "--reason", "compromised")
        self.assertEqual(self.client.get(ME).status_code, 401)
        self.assertEqual(self.client.post(MCP, {}, format="json").status_code, 401)

    def test_dry_run_changes_and_writes_nothing(self):
        key, _ = _mint(self.user)
        RefreshToken.for_user(self.user)
        before = SecurityEvent.objects.count()
        out = self.run_cmd("user", str(self.user.id), "--reason", "r", "--dry-run")
        self.assertTrue(out.startswith("[dry-run]"))
        self.user.refresh_from_db()
        key.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(key.revoked_at)
        self.assertFalse(BlacklistedToken.objects.exists())
        self.assertEqual(SecurityEvent.objects.count(), before)

    def test_second_run_is_a_no_op_with_no_second_event(self):
        _mint(self.user)
        self.run_cmd("user", str(self.user.id), "--reason", "first")
        out = self.run_cmd("user", str(self.user.id), "--reason", "second")
        self.assertIn("already cut off", out)
        self.assertEqual(self.events(SecurityAction.OPERATOR_USER_DEACTIVATED).count(), 1)

    def test_an_inactive_user_with_a_live_key_is_not_already_cut_off(self):
        key, _ = _mint(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        self.run_cmd("user", str(self.user.id), "--reason", "finish it")
        key.refresh_from_db()
        self.assertIsNotNone(key.revoked_at)
        row = self.events().get()
        self.assertFalse(row.metadata["was_active"])

    def test_no_audit_row_means_no_cutoff(self):
        key, _ = _mint(self.user)
        with mock.patch(
            "origin.management.commands.operator_cutoff.record_security_event", return_value=None
        ):
            with self.assertRaises(CommandError):
                self.run_cmd("user", str(self.user.id), "--reason", "r")
        self.user.refresh_from_db()
        key.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(key.revoked_at)


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class KeyCutoffTests(CutoffBase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([str(self.user.id)])
        self.key, self.raw = _mint(self.user, team=self.team)
        self.sibling, self.sibling_raw = _mint(self.user, name="sibling")

    def _status(self, raw, path=ME):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        if path == MCP:
            return self.client.post(
                MCP, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, format="json"
            )
        return self.client.get(path)

    def test_revoke_by_id_blocks_that_key_on_rest_and_mcp_only(self):
        self.assertEqual(self._status(self.raw).status_code, 200)
        self.assertEqual(self._status(self.raw, MCP).status_code, 200)
        self.run_cmd("api-key", str(self.key.id), "--reason", "pasted in a ticket")
        self.assertEqual(self._status(self.raw).status_code, 401)
        self.assertEqual(self._status(self.raw, MCP).status_code, 401)
        self.assertEqual(self._status(self.sibling_raw).status_code, 200)
        self.assertEqual(self._status(self.sibling_raw, MCP).status_code, 200)

    def test_revoke_by_prefix(self):
        self.run_cmd("api-key", self.key.prefix, "--reason", "r")
        self.key.refresh_from_db()
        self.assertIsNotNone(self.key.revoked_at)
        self.sibling.refresh_from_db()
        self.assertIsNone(self.sibling.revoked_at)

    def test_event_fields(self):
        self.run_cmd("api-key", str(self.key.id), "--reason", "leak")
        row = self.events(SecurityAction.OPERATOR_API_KEY_REVOKED).get()
        self.assertEqual(row.source, SecuritySource.OPERATOR)
        self.assertEqual(row.actor_label, f"operator:{OPERATOR}")
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual((row.target_type, row.target_id), ("api_key", str(self.key.id)))
        self.assertEqual(row.metadata["reason"], "leak")
        self.assertEqual(row.metadata["operator"], OPERATOR)
        self.assertEqual(row.metadata["owner_id"], str(self.user.id))
        self.assertNotIn(self.user.email, self.all_event_text())
        self.assertNotIn(self.raw, self.all_event_text())

    def test_dry_run_changes_and_writes_nothing(self):
        before = SecurityEvent.objects.count()
        out = self.run_cmd("api-key", str(self.key.id), "--reason", "r", "--dry-run")
        self.assertTrue(out.startswith("[dry-run]"))
        self.key.refresh_from_db()
        self.assertIsNone(self.key.revoked_at)
        self.assertEqual(SecurityEvent.objects.count(), before)
        self.assertEqual(self._status(self.raw).status_code, 200)

    def test_second_run_records_nothing_more(self):
        self.run_cmd("api-key", str(self.key.id), "--reason", "first")
        out = self.run_cmd("api-key", str(self.key.id), "--reason", "second")
        self.assertIn("already revoked", out)
        self.assertEqual(self.events().count(), 1)

    def test_a_key_pasted_as_the_argument_is_not_found_and_not_echoed(self):
        with self.assertRaises(CommandError) as ctx:
            self.run_cmd("api-key", self.raw, "--reason", "oops")
        self.assertNotIn(self.raw, str(ctx.exception))
        self.key.refresh_from_db()
        self.assertIsNone(self.key.revoked_at)


class TeamKeysCutoffTests(CutoffBase):
    def setUp(self):
        super().setUp()
        self.other_team = TeamMaster.objects.create(
            team_name="Other", team_email="o@example.com", owner=self.user
        )
        TeamMembers.objects.create(team=self.other_team, attendee=self.user)
        self.k1, _ = _mint(self.user, team=self.team, name="a")
        self.k2, _ = _mint(self.user2, team=self.team, name="b")
        self.elsewhere, _ = _mint(self.user, team=self.other_team, name="c")
        self.personal, _ = _mint(self.user, name="p")

    def _revoked(self, key):
        key.refresh_from_db()
        return key.revoked_at is not None

    def test_revokes_only_that_teams_keys(self):
        self.run_cmd("team-keys", str(self.team.team_id), "--reason", "tenant breach")
        self.assertTrue(self._revoked(self.k1))
        self.assertTrue(self._revoked(self.k2))
        self.assertFalse(self._revoked(self.elsewhere))
        self.assertFalse(self._revoked(self.personal))

    def test_one_event_for_the_team(self):
        self.run_cmd("team-keys", str(self.team.team_id), "--reason", "tenant breach")
        row = self.events(SecurityAction.OPERATOR_TEAM_KEYS_REVOKED).get()
        self.assertEqual(row.source, SecuritySource.OPERATOR)
        self.assertEqual(row.team_id, self.team.team_id)
        self.assertEqual(row.metadata["reason"], "tenant breach")
        self.assertEqual(row.metadata["operator"], OPERATOR)
        self.assertEqual(row.metadata["api_keys_revoked"], 2)
        self.assertEqual(set(row.metadata["api_key_ids"]), {str(self.k1.id), str(self.k2.id)})
        text = self.all_event_text()
        self.assertNotIn(self.user.email, text)
        self.assertNotIn(self.user2.email, text)

    def test_dry_run_changes_and_writes_nothing(self):
        before = SecurityEvent.objects.count()
        out = self.run_cmd("team-keys", str(self.team.team_id), "--reason", "r", "--dry-run")
        self.assertTrue(out.startswith("[dry-run]"))
        self.assertFalse(self._revoked(self.k1))
        self.assertFalse(self._revoked(self.k2))
        self.assertEqual(SecurityEvent.objects.count(), before)

    def test_second_run_records_nothing_more(self):
        self.run_cmd("team-keys", str(self.team.team_id), "--reason", "first")
        out = self.run_cmd("team-keys", str(self.team.team_id), "--reason", "second")
        self.assertIn("already clear", out)
        self.assertEqual(self.events().count(), 1)


class UsageAndExitCodeTests(CutoffBase):
    def test_reason_is_required_on_every_subcommand(self):
        key, _ = _mint(self.user)
        for args in (
            ("user", str(self.user.id)),
            ("api-key", str(key.id)),
            ("team-keys", str(self.team.team_id)),
        ):
            with self.assertRaises(CommandError, msg=args):
                self.run_cmd(*args)
        self.user.refresh_from_db()
        key.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertIsNone(key.revoked_at)
        self.assertEqual(SecurityEvent.objects.filter(source=SecuritySource.OPERATOR).count(), 0)

    def test_a_blank_reason_is_refused(self):
        with self.assertRaises(CommandError):
            self.run_cmd("user", str(self.user.id), "--reason", "   ")
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_unknown_targets_raise_and_write_nothing(self):
        missing = str(uuid.uuid4())
        for args in (
            ("user", missing),
            ("user", "not-a-uuid"),
            ("api-key", missing),
            ("api-key", "gnos_zzzzzz"),
            ("team-keys", missing),
        ):
            with self.assertRaises(CommandError, msg=args):
                self.run_cmd(*args, "--reason", "r")
        self.assertEqual(self.events().count(), 0)

    def _run_like_manage_py(self, *args):
        """`manage.py operator_cutoff ...` minus the interpreter: the same
        `run_from_argv` that turns a CommandError into the exit code. It
        closes every DB connection afterwards, which would break the test
        transaction, so that one call is stubbed."""
        from origin.management.commands.operator_cutoff import Command  # noqa: PLC0415

        with (
            mock.patch("django.core.management.base.connections.close_all"),
            mock.patch.object(sys, "stderr", StringIO()),
            mock.patch.object(sys, "stdout", StringIO()),
        ):
            Command().run_from_argv(["manage.py", "operator_cutoff", *args])

    def test_the_process_exits_non_zero_for_an_unknown_target(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_like_manage_py("user", str(uuid.uuid4()), "--reason", "r")
        self.assertEqual(ctx.exception.code, 1)

    def test_the_process_exits_non_zero_without_a_reason(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_like_manage_py("user", str(self.user.id))
        self.assertNotEqual(ctx.exception.code, 0)

    def test_the_process_exits_zero_on_success(self):
        self._run_like_manage_py("team-keys", str(self.team.team_id), "--reason", "r")  # no exit


# -- Part 3: the off switches -------------------------------------------------


@override_settings(SEARCH_ENGINE=_mcp_enabled_everywhere())
class OffSwitchTests(CutoffBase):
    def setUp(self):
        super().setUp()
        quota.invalidate_effective_tier([str(self.user.id)])
        self.key, self.raw = _mint(self.user)

    def _rest(self, raw=None):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw or self.raw}")
        return self.client.get(ME)

    def _mcp(self, raw=None, rid=7):
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw or self.raw}")
        return self.client.post(MCP, {"jsonrpc": "2.0", "id": rid, "method": "ping"}, format="json")

    def test_default_is_off_and_nothing_changes(self):
        self.assertFalse(settings.PUBLIC_API_DISABLED)
        self.assertFalse(settings.MCP_DISABLED)
        self.assertEqual(self._rest().status_code, 200)
        self.assertEqual(self._mcp().status_code, 200)

    @override_settings(PUBLIC_API_DISABLED=True)
    def test_public_api_disabled_turns_off_rest_and_mcp(self):
        r = self._rest()
        self.assertEqual(r.status_code, 503)
        self.assertIn("error", r.json())
        m = self._mcp()
        self.assertEqual(m.status_code, 503)
        body = m.json()
        self.assertEqual(body["jsonrpc"], "2.0")
        self.assertEqual(body["id"], 7)
        self.assertEqual(body["error"]["code"], -32001)

    @override_settings(PUBLIC_API_DISABLED=True)
    def test_the_refusal_is_the_same_for_good_bad_and_missing_keys(self):
        # Answered before any key is looked up: nothing about validity leaks,
        # nothing is recorded as a rejected key, and last_used is untouched.
        replies = []
        for raw in (self.raw, generate_key()):
            replies.append((self._rest(raw).status_code, self._rest(raw).json()))
        self.client.credentials()
        replies.append((self.client.get(ME).status_code, self.client.get(ME).json()))
        self.assertEqual(len({json.dumps(r, sort_keys=True) for r in replies}), 1)
        self.assertEqual(replies[0][0], 503)
        self.key.refresh_from_db()
        self.assertIsNone(self.key.last_used_at)
        self.assertFalse(
            SecurityEvent.objects.filter(
                action__in=[SecurityAction.API_KEY_USED, SecurityAction.API_KEY_REJECTED]
            ).exists()
        )

    @override_settings(MCP_DISABLED=True)
    def test_mcp_disabled_turns_off_mcp_only(self):
        m = self._mcp()
        self.assertEqual(m.status_code, 503)
        self.assertEqual(m.json()["error"]["code"], -32001)
        self.assertEqual(self._rest().status_code, 200)

    def test_switches_are_read_per_request_not_at_import(self):
        self.assertEqual(self._rest().status_code, 200)
        with override_settings(PUBLIC_API_DISABLED=True):
            self.assertEqual(self._rest().status_code, 503)
        self.assertEqual(self._rest().status_code, 200)

    @override_settings(PUBLIC_API_DISABLED=True, MCP_DISABLED=True)
    def test_tier_quotas_are_untouched(self):
        self.assertEqual(settings.SEARCH_ENGINE, _mcp_enabled_everywhere())

    def test_the_session_app_is_not_switched_off(self):
        with override_settings(PUBLIC_API_DISABLED=True, MCP_DISABLED=True):
            self.authenticate(self.user)
            self.assertEqual(self.client.get(APP_PROBE).status_code, 200)


class EnvSwitchParsingTests(BaseAPITestCase):
    """An off switch that fails open is not one: a typo turns it ON."""

    def test_parsing(self):
        from apis.settings import _env_switch  # noqa: PLC0415

        cases = {
            None: False,
            "": False,
            "0": False,
            "false": False,
            "False": False,
            "no": False,
            "off": False,
            "1": True,
            "true": True,
            "TRUE": True,
            "yes": True,
            "on": True,
            "yse": True,  # a typo shuts it, not leaves it open
        }
        for raw, expected in cases.items():
            env = {} if raw is None else {"X_SWITCH": raw}
            with mock.patch.dict(os.environ, env, clear=False):
                if raw is None:
                    os.environ.pop("X_SWITCH", None)
                self.assertIs(_env_switch("X_SWITCH"), expected, msg=repr(raw))
