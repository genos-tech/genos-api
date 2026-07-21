"""Completion push for a backgrounded agent run.

All three Ask surfaces (Spotlight overlay, thread modal, note modal)
deliberately keep an in-flight stream alive after their window is
dismissed, so an answer can land long after the user has moved on. This
covers the away-from-app half of telling them: the duration floor, the
deep-link derivation, and the preference gating for the new
`agent_run_done` category.

`schedule_push_to_user` is mocked in the firing tests — the delivery path
it wraps is already covered by `test_webpush.py`; what matters here is
whether the run-close path decides to call it at all.
"""

from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from django.utils import timezone

from origin.models.common.notification_models import NotificationPreference
from origin.search_engine import agent_views
from origin.search_engine.models import AgentRun
from origin.services.webpush_gating import should_push
from origin.tests.test_base import BaseAPITestCase

PUSH_TARGET = "origin.search_engine.agent_views.schedule_push_to_user"


class AgentRunDoneGatingTests(BaseAPITestCase):
    """The new category rides the existing `inbox` coarse column."""

    def test_defaults_on_without_a_prefs_row(self):
        self.assertTrue(should_push(self.user2.id, "agent_run_done"))

    def test_inbox_coarse_toggle_blocks_it(self):
        # Grouped under `inbox` on both sides (frontend `categories.ts`
        # and `_COARSE_FIELD`), so turning the group off must mute it.
        NotificationPreference.objects.create(user=self.user2, enable_inbox=False)
        self.assertFalse(should_push(self.user2.id, "agent_run_done"))

    def test_per_category_override_blocks_it(self):
        NotificationPreference.objects.create(
            user=self.user2, category_settings={"agent_run_done": False}
        )
        self.assertFalse(should_push(self.user2.id, "agent_run_done"))

    def test_push_master_blocks_it(self):
        NotificationPreference.objects.create(user=self.user2, push_enabled=False)
        self.assertFalse(should_push(self.user2.id, "agent_run_done"))


class RunCompleteUrlTests(BaseAPITestCase):
    def test_thread_scoped_session_links_to_the_channel(self):
        session = SimpleNamespace(
            chat_type=3, chat_id="0f8f-channel", note_type=None, note_id=None
        )
        self.assertEqual(
            agent_views._run_complete_url(session), "/workspace/chat/pm/0f8f-channel"
        )

    def test_personal_note_session_links_to_the_note(self):
        session = SimpleNamespace(chat_type=None, chat_id=None, note_type=1, note_id=42)
        self.assertEqual(agent_views._run_complete_url(session), "/workspace/notes/my/42")

    def test_plain_spotlight_run_falls_back_to_the_app_root(self):
        # The overlay is a Cmd-K layer, not a route — there is nothing
        # more specific to link to. It restores its own turns on reopen.
        self.assertEqual(agent_views._run_complete_url(None), "/workspace/chat")

    def test_unknown_chat_type_falls_back_rather_than_building_a_broken_link(self):
        session = SimpleNamespace(chat_type=99, chat_id="x", note_type=None, note_id=None)
        self.assertEqual(agent_views._run_complete_url(session), "/workspace/chat")


class PushRunCompleteTests(BaseAPITestCase):
    def _run(self, *, age_seconds: int, query: str = "why did the deploy fail?") -> AgentRun:
        run = AgentRun.objects.create(
            team_id=str(self.team.team_id),
            user_id=str(self.user.id),
            query=query,
            status="done",
        )
        # started_at is auto_now_add — rewrite it to age the run.
        AgentRun.objects.filter(run_id=run.run_id).update(
            started_at=timezone.now() - timedelta(seconds=age_seconds)
        )
        run.refresh_from_db()
        return run

    def test_fires_for_a_slow_run(self):
        run = self._run(age_seconds=120)
        with mock.patch(PUSH_TARGET) as push:
            agent_views._push_run_complete(run, failed=False)
        push.assert_called_once()
        kwargs = push.call_args.kwargs
        self.assertEqual(kwargs["category"], "agent_run_done")
        self.assertEqual(kwargs["recipient_id"], str(self.user.id))
        self.assertEqual(kwargs["title"], "Your AI answer is ready")
        self.assertEqual(kwargs["tag"], f"agent_run_done:{run.run_id}")

    def test_skips_a_run_that_finished_immediately(self):
        # Answered before the user could plausibly have gone anywhere —
        # an OS card for that reads as noise, not as a completion.
        run = self._run(age_seconds=2)
        with mock.patch(PUSH_TARGET) as push:
            agent_views._push_run_complete(run, failed=False)
        push.assert_not_called()

    def test_failed_run_gets_its_own_title(self):
        run = self._run(age_seconds=120)
        with mock.patch(PUSH_TARGET) as push:
            agent_views._push_run_complete(run, failed=True)
        self.assertEqual(push.call_args.kwargs["title"], "Your AI answer didn't finish")

    def test_a_push_failure_never_propagates_to_the_run(self):
        # The run is already saved by this point; a notification problem
        # must not turn a successful answer into a 500.
        run = self._run(age_seconds=120)
        with mock.patch(PUSH_TARGET, side_effect=RuntimeError("vapid exploded")):
            agent_views._push_run_complete(run, failed=False)  # must not raise
