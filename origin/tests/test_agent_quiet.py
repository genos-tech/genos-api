"""MIP-2022: `GET /api/v2/mcp/connections/quiet/`, "your AI app has recorded nothing in N days".

Pins: no connection is never quiet; a fresh connection is not quiet yet; an agent write (task
change, comment or note version via a connected app or an API key) inside the window clears it;
a person's own write, an old agent write and someone else's write do not; a revoked connection
does not count; a paused plan is not "broken"; the answer is the caller's own; the query count is
constant.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

from django.core.cache import cache
from django.db import connection as db
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from oauth2_provider.models import Application

from origin.models.common.mcp_connection_models import McpConnection
from origin.models.note.personal_note_models import PersonalNoteMaster
from origin.models.note.version_note_models import NoteVersionMaster
from origin.models.project.prj_models import ProjectMaster
from origin.models.task.task_activity_models import TaskActivity
from origin.models.task.task_models import TaskMaster
from origin.services import agent_quiet
from origin.tests.test_base import BaseAPITestCase
from origin.tests.test_mcp_oauth_foundation import oauth_on

URL = "/api/v2/mcp/connections/quiet/"
CLAUDE = {"kind": "oauth", "app": "claude", "connection_id": "c-1"}
KEY = {"kind": "api_key", "key_id": "k-1"}


@oauth_on()
class QuietTests(BaseAPITestCase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.app = Application.objects.create(
            client_id="claude",
            client_type=Application.CLIENT_PUBLIC,
            authorization_grant_type=Application.GRANT_AUTHORIZATION_CODE,
            redirect_uris="https://claude.ai/cb",
            name="Claude",
        )
        self.project = ProjectMaster.objects.create(
            team=self.team,
            project_name="Launch",
            code="LAU",
            owner=self.user,
            project_system_user=self.user,
        )
        self.task = TaskMaster.objects.create(
            team=self.team, project=self.project, reporter=self.user, title="Spec", status="Open"
        )
        TaskActivity.objects.all().delete()
        self.authenticate()
        patcher = mock.patch("origin.services.agent_quiet.get_mcp_enabled", return_value=True)
        self.plan = patcher.start()
        self.addCleanup(patcher.stop)

    def connect(self, user=None, age_days=10, **kw):
        conn = McpConnection.objects.create(
            user=user or self.user,
            application=self.app,
            default_team=self.team,
            team_ids=[str(self.team.team_id)],
            scope="write",
            **kw,
        )
        McpConnection.objects.filter(pk=conn.pk).update(
            ts_created_at=timezone.now() - timedelta(days=age_days)
        )
        return conn

    def act(self, actor, via, days_ago=0):
        row = TaskActivity.objects.create(
            team=self.team,
            project=self.project,
            task=self.task,
            actor=actor,
            action_type="status_changed",
            metadata={"via": via} if via else {},
        )
        TaskActivity.objects.filter(pk=row.pk).update(
            ts_created_at=timezone.now() - timedelta(days=days_ago)
        )

    def get(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200, res.content)
        return res.json()

    def test_no_connection_is_not_connected_and_not_quiet(self):
        self.assertEqual(self.get(), {"connected": False, "quiet": False, "days": 3})

    def test_connected_and_no_agent_writes_is_quiet(self):
        self.connect()
        self.assertEqual(self.get(), {"connected": True, "quiet": True, "days": 3})

    def test_a_fresh_connection_is_not_quiet_yet(self):
        self.connect(age_days=1)
        self.assertEqual(self.get(), {"connected": True, "quiet": False, "days": 3})

    def test_a_recent_connected_app_write_clears_it(self):
        self.connect()
        self.act(self.user, CLAUDE, days_ago=1)
        self.assertFalse(self.get()["quiet"])

    def test_a_recent_api_key_write_clears_it(self):
        self.connect()
        self.act(self.user, KEY)
        self.assertFalse(self.get()["quiet"])

    def test_a_recent_note_version_by_an_agent_clears_it(self):
        self.connect()
        note = PersonalNoteMaster.objects.create(
            team=self.team, owner=self.user, title="N", body=None
        )
        NoteVersionMaster.objects.create(
            team=self.team,
            note_type=1,
            note_id=note.note_id,
            version_no=1,
            editor=self.user,
            title="N",
            body=None,
            via=CLAUDE,
        )
        self.assertFalse(self.get()["quiet"])

    def test_old_agent_writes_a_persons_writes_and_others_writes_do_not_count(self):
        self.connect()
        self.act(self.user, CLAUDE, days_ago=5)  # too old
        self.act(self.user, None)  # a person
        self.act(self.user, {"kind": "agent"})  # the in-app agent
        self.act(self.user2, CLAUDE)  # someone else's app
        self.assertTrue(self.get()["quiet"])

    def test_a_revoked_connection_is_not_connected(self):
        self.connect(revoked_at=timezone.now())
        self.assertFalse(self.get()["connected"])

    def test_a_paused_plan_is_not_quiet(self):
        self.connect()
        self.plan.return_value = False
        self.assertEqual(self.get(), {"connected": True, "quiet": False, "days": 3})

    def test_it_is_the_callers_own(self):
        self.connect(user=self.user2)
        self.assertFalse(self.get()["connected"])

    def test_the_window_is_the_constant(self):
        self.assertEqual(agent_quiet.QUIET_DAYS, 3)

    def test_the_query_count_is_constant(self):
        self.connect()
        for _ in range(20):
            self.act(self.user, None)
        with CaptureQueriesContext(db) as ctx:
            self.get()
        self.assertLessEqual(len(ctx), 6, [q["sql"][:80] for q in ctx.captured_queries])

    def test_anonymous_is_refused(self):
        self.client.credentials()
        self.assertIn(self.client.get(URL).status_code, (401, 403))
