"""The owner's agent use counts (MIP-2011): sums per member per app per week, nothing finer.

Pinned: owner only (member, editor and outsider get 404, like the security log); the counts are
right; the key name is in the owner's answer; the answer never carries a target, a tool name, a run
id or an error code, whatever the rows hold; another team's rows and personal-key rows are left out.
"""

import json
import uuid
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.agent_call_models import (
    APP_CHATGPT,
    APP_CLAUDE,
    APP_OTHER,
    CREDENTIAL_API_KEY,
    CREDENTIAL_OAUTH,
    AgentCall,
)
from origin.models.common.api_key_models import ApiKey, generate_key, hash_key
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.services.member_roles import EDITOR
from origin.tests.test_base import BaseAPITestCase

User = get_user_model()
URL = "/api/v2/team/agent-use/"

SECRET_TARGET = "987654321"
SECRET_TOOL = "search_workspace"
SECRET_ERROR = "secret_error_word"
SECRET_RUN = "run-secret-0001"


class TeamAgentUseCase(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        self.oauth_id = str(uuid.uuid4())
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token(self.user)}")

    def call(self, user=None, team=None, **kw):
        fields = dict(
            user=user or self.user2,
            team=team or self.team,
            credential_kind=CREDENTIAL_OAUTH,
            credential_id=self.oauth_id,
            app_family=APP_CLAUDE,
            run_id=SECRET_RUN,
            tool=SECRET_TOOL,
            op="read",
            outcome="ok",
            error_code="",
            targets=[{"type": "task", "id": SECRET_TARGET}],
            count=1,
        )
        fields.update(kw)
        return AgentCall.objects.create(**fields)

    def get(self, **params):
        params.setdefault("team_id", str(self.team.team_id))
        return self.client.get(URL, params)

    def as_user(self, user):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {self.get_access_token(user)}")


class TestWhoMayRead(TeamAgentUseCase):
    def test_the_owner_gets_200(self):
        self.assertEqual(self.get().status_code, 200)

    def test_a_member_gets_404(self):
        self.call()
        self.as_user(self.user2)
        self.assertEqual(self.get().status_code, 404)

    def test_an_editor_gets_404(self):
        TeamMembers.objects.filter(team=self.team, attendee=self.user2).update(member_role=EDITOR)
        self.as_user(self.user2)
        self.assertEqual(self.get().status_code, 404)

    def test_an_outsider_gets_404(self):
        outsider = User.objects.create_user(
            username="o", email="o@example.com", password="x1y2z3w4"
        )
        self.as_user(outsider)
        self.assertEqual(self.get().status_code, 404)

    def test_anonymous_is_refused(self):
        self.client.credentials()
        self.assertIn(self.get().status_code, (401, 403))

    def test_a_missing_or_bad_team_id_is_404(self):
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.get(team_id="nope").status_code, 404)

    def test_an_api_key_cannot_read_it(self):
        raw = generate_key()
        ApiKey.objects.create(
            user=self.user, team=self.team, name="k", key_hash=hash_key(raw), prefix=raw[:11]
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"ApiKey {raw}")
        self.assertIn(self.get().status_code, (401, 403, 404))


class TestCounts(TeamAgentUseCase):
    def test_calls_changes_and_refused_per_member_per_app(self):
        self.call(count=3)  # a collapsed read of 3
        self.call(op="search", count=1)
        self.call(op="write", count=2)
        self.call(op="delete", count=1, outcome="refused_destructive")
        self.call(op="write", outcome="refused_scope")
        self.call(app_family=APP_CHATGPT, count=5)
        rows = self.get().json()["rows"]
        by_app = {r["app"]: r for r in rows if r["userId"] == str(self.user2.id)}
        self.assertEqual(set(by_app), {APP_CLAUDE, APP_CHATGPT})
        claude = by_app[APP_CLAUDE]
        self.assertEqual(claude["calls"], 3 + 1 + 2 + 1 + 1)
        self.assertEqual(claude["changes"], 2)  # only the ok write
        self.assertEqual(claude["refused"], 2)
        self.assertEqual(claude["username"], "otheruser")
        self.assertEqual(by_app[APP_CHATGPT]["calls"], 5)

    def test_members_are_separate_rows(self):
        self.call(user=self.user)
        self.call(user=self.user2)
        self.call(user=self.user2)
        rows = self.get().json()["rows"]
        counts = {r["userId"]: r["calls"] for r in rows}
        self.assertEqual(counts, {str(self.user.id): 1, str(self.user2.id): 2})

    def test_weeks_split_and_old_rows_are_cut_off(self):
        now = timezone.now()
        self.call()
        old = self.call()
        AgentCall.objects.filter(pk=old.pk).update(ts=now - timedelta(weeks=2))
        ancient = self.call()
        AgentCall.objects.filter(pk=ancient.pk).update(ts=now - timedelta(weeks=8))
        rows = self.get(weeks=4).json()["rows"]
        self.assertEqual(len(rows), 2)
        self.assertGreater(rows[0]["weekStart"], rows[1]["weekStart"])
        self.assertEqual(len(self.get(weeks=12).json()["rows"]), 3)

    def test_weeks_is_clamped(self):
        self.assertEqual(self.get(weeks=999).json()["weeks"], 13)
        self.assertEqual(self.get(weeks=0).json()["weeks"], 1)
        self.assertEqual(self.get(weeks="x").json()["weeks"], 4)

    def test_other_teams_and_personal_rows_are_left_out(self):
        other = TeamMaster.objects.create(team_name="Other", team_email="o@x.com", owner=self.user2)
        self.call(team=other)
        AgentCall.objects.create(
            user=self.user2, team=None, credential_kind=CREDENTIAL_OAUTH, tool="x", op="read"
        )
        self.assertEqual(self.get().json()["rows"], [])

    def test_the_key_name_is_given_for_a_key_credential(self):
        raw = generate_key()
        key = ApiKey.objects.create(
            user=self.user2,
            team=self.team,
            name="Claude Code",
            key_hash=hash_key(raw),
            prefix=raw[:11],
        )
        self.call(
            credential_kind=CREDENTIAL_API_KEY, credential_id=str(key.id), app_family=APP_OTHER
        )
        self.call()  # an OAuth connection: no key name
        rows = {r["credentialKind"]: r for r in self.get().json()["rows"]}
        self.assertEqual(rows[CREDENTIAL_API_KEY]["keyName"], "Claude Code")
        self.assertIsNone(rows[CREDENTIAL_OAUTH]["keyName"])

    def test_a_revoked_or_unknown_key_has_no_name_and_does_not_break(self):
        self.call(credential_kind=CREDENTIAL_API_KEY, credential_id="not-a-uuid")
        self.assertIsNone(self.get().json()["rows"][0]["keyName"])


class TestNoDetailLeaks(TeamAgentUseCase):
    def test_the_answer_carries_no_target_tool_run_or_error(self):
        self.call(error_code=SECRET_ERROR, outcome="tool_error", count=4)
        self.call(op="search")
        resp = self.get()
        body = json.dumps(resp.json())
        for secret in (SECRET_TARGET, SECRET_TOOL, SECRET_ERROR, SECRET_RUN, "targets", "run_id"):
            self.assertNotIn(secret, body)
        allowed = {
            "weekStart",
            "userId",
            "username",
            "app",
            "credentialKind",
            "keyName",
            "calls",
            "changes",
            "refused",
        }
        for row in resp.json()["rows"]:
            self.assertEqual(set(row), allowed)
        self.assertEqual(set(resp.json()), {"weeks", "rows"})

    def test_no_per_call_row_is_returned(self):
        for _ in range(5):
            self.call(count=1)
        rows = self.get().json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 5)
