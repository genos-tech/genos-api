"""The sequence scenario as a TestCase base (MIP-1269).

The story, the readers and `ground_truth` live in `origin/services/dev_fixtures/sequence_scenario.py`
(production code, so the dev tool `agent_eval_setup --scenario sequence` can build the same story
without importing the tests: MIP-1469). This module is the test host: it supplies the test
client, the authentication and the assertions, and re-exports the names the tests import. Read the
service module for the story (steps, options, the observer effect).

    class MyCase(SequenceScenario):
        def test_x(self):
            seq = self.build_sequence(client_on_project=False, chat_shape="flat")
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.contrib.auth import get_user_model
from django.utils import timezone

from origin.models.common.team_models import TeamMembers
from origin.models.task.task_models import TaskMaster
from origin.services.dev_fixtures.guards import require_test_database
from origin.services.dev_fixtures.sequence_scenario import (  # noqa: F401  (re-exported)
    ACTOR,
    JST,
    NOTE_V1,
    NOTE_V2,
    ORDER,
    SCHEDULE,
    TEXT,
    USERNAMES,
    Sequence,
    SequenceStory,
    ground_truth,
    locate_steps,
    mention_chip,
    paragraph,
    who_of,
)
from origin.tests.test_mcp_endpoint import McpBase

User = get_user_model()


class SequenceScenario(SequenceStory, McpBase):
    """A TestCase base: `build_sequence` and the readers. Subclass it, do not run it."""

    def setUp(self):
        # Before ANY row is written: this base clears tasks, so it must never run anywhere but
        # the test database (one wrong import from a command would otherwise wipe a database).
        require_test_database("SequenceScenario.setUp")
        super().setUp()
        # McpBase's own project and task are noise for this story. Scoped to this test's own team.
        TaskMaster.objects.filter(team=self.team).delete()
        self.b = self.user  # the PM: the team's owner, so on the project's own record
        self.a = User.objects.create_user(username="client_a", email="a@example.com", password="x")
        self.c = User.objects.create_user(username="dev_c", email="c@example.com", password="x")
        for user in (self.a, self.b, self.c):
            user.timezone = "Asia/Tokyo"
            user.save(update_fields=["timezone"])
        for user in (self.a, self.c):
            TeamMembers.objects.create(team=self.team, attendee=user)
        # Local midnight three days ago: far from "now" in both directions.
        local = timezone.now().astimezone(JST).date() - timedelta(days=3)
        self.base = datetime.combine(local, datetime.min.time(), tzinfo=JST)
        self._raw_keys: dict[str, str] = {}

    def _rest(self, user):
        self.authenticate(user)

    def _expect(self, ok, detail=""):
        self.assertTrue(ok, detail)
