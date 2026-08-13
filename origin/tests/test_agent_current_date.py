"""The agent is told what day it is (SPOTLIGHT_TEMPORAL_REASONING.md gap 1).

Without an anchored "today", the model computes every relative date
("this week", "overdue", "in three days") against its training cutoff —
silently and confidently. `_drive_loop` appends the asking user's real
calendar date to the system instruction so that reasoning is grounded.

Three things this suite pins:
  * the date is the ASKING USER's local day, not the server's UTC day
    (the same personal-scope rule the date-taking tools already follow —
    see `services/user_time.py` and `test_agent_tools_user_timezone.py`);
  * only the DATE is injected, never the wall-clock time — a per-second
    string would re-key the Gemini / OpenAI model prefix caches on every
    loop step, turning a guaranteed cache hit into a guaranteed miss;
  * the injection actually reaches the model, and the kill-switch removes
    it byte-for-byte.
"""

from datetime import datetime
from datetime import timezone as dt_timezone
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from origin.search_engine.agent.controller import _current_date_line, _drive_loop
from origin.search_engine.agent.tools import ToolContext
from origin.search_engine.llm.types import AgentMessage

User = get_user_model()

# 2026-07-30 22:30 UTC is already 2026-07-31 07:30 in Tokyo — the instant
# where a UTC "today" is a full day behind a Tokyo user's. (2026-07-31 is a
# Friday; 2026-07-30 is a Thursday — hardcoded below so the expected string
# is fully literal and a bad %A format can't pass by matching itself.)
LATE_UTC = datetime(2026, 7, 30, 22, 30, tzinfo=dt_timezone.utc)


def _se(**overrides):
    from django.conf import settings as dj_settings

    cfg = dict(dj_settings.SEARCH_ENGINE)
    cfg.update(overrides)
    return cfg


class _CaptureClient:
    """Ends the turn on the first call, recording the system instruction.

    A text-only response with no `[type:id]` citation tokens is emitted by
    `_drive_loop` as the final answer, so the loop returns after exactly
    one model call without any tool execution or citation lookup.
    """

    def __init__(self):
        self.captured: list[str] = []

    def generate_step(self, messages, tools, system_instruction, **kw):
        self.captured.append(system_instruction)
        yield ("All set.", None)


class CurrentDateLineTests(TestCase):
    """`_current_date_line` — the helper that builds the anchor string."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="tz-date", email="tzdate@test.com", password="testpass123"
        )

    def test_uses_the_users_local_day_not_server_utc(self):
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            line = _current_date_line(self.user.pk)
        self.assertEqual(line, "Today's date is 2026-07-31 (Friday) in the Asia/Tokyo timezone.")
        self.assertIn("2026-07-31", line)  # Tokyo's day...
        self.assertNotIn("2026-07-30", line)  # ...not the server's UTC day

    def test_falls_back_to_utc_when_user_has_no_timezone(self):
        # Every row is NULL until the client reports one, so this is the
        # common path: the anchor is the server's UTC day, named as such.
        self.assertIsNone(self.user.timezone)
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            line = _current_date_line(self.user.pk)
        self.assertEqual(line, "Today's date is 2026-07-30 (Thursday) in the UTC timezone.")

    def test_no_wall_clock_time_is_injected(self):
        # Cache safety: a HH:MM in the prefix would bust the Gemini /
        # OpenAI prefix caches every step. The line must carry a date and
        # nothing that looks like a clock time.
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])
        with patch("django.utils.timezone.now", return_value=LATE_UTC):
            line = _current_date_line(self.user.pk)
        self.assertNotRegex(line, r"\d{1,2}:\d{2}")


class CurrentDateInjectionTests(TestCase):
    """End to end: the date reaches (or, flag-off, does not reach) the model."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="tz-inject", email="tzinject@test.com", password="testpass123"
        )
        self.user.timezone = "Asia/Tokyo"
        self.user.save(update_fields=["timezone"])

    def _drive(self, **se_overrides):
        client = _CaptureClient()
        # Effort levels off so the loop params come from the plain env
        # values; step-cap wrap-up off so a single model call is all there
        # is. Neither interacts with the date injection — they just keep
        # the harness minimal (mirrors test_agent_dynamic_steps).
        base = {"AGENT_EFFORT_LEVELS": False, "AGENT_STEP_CAP_WRAPUP": False}
        base.update(se_overrides)
        with (
            patch("django.utils.timezone.now", return_value=LATE_UTC),
            override_settings(SEARCH_ENGINE=_se(**base)),
            patch(
                "origin.search_engine.agent.controller.get_model_client",
                return_value=client,
            ),
        ):
            _drive_loop(
                messages=[AgentMessage(role="user", text="what is due this week?")],
                ctx=ToolContext(team_id="team", user_id=str(self.user.pk)),
                emit=lambda e: None,
                run_id=None,
                starting_step=0,
                seen_sources_by_id={},
            )
        return client.captured[0]

    def test_flag_on_appends_the_users_today(self):
        sys_instr = self._drive(AGENT_INJECT_CURRENT_DATE=True)
        expected = "Today's date is 2026-07-31 (Friday) in the Asia/Tokyo timezone."
        # Appended LAST so the OpenAI prefix cache keeps a shared head.
        self.assertTrue(sys_instr.rstrip().endswith(expected), sys_instr[-200:])

    def test_flag_off_leaves_the_prompt_untouched(self):
        sys_instr = self._drive(AGENT_INJECT_CURRENT_DATE=False)
        self.assertNotIn("Today's date is", sys_instr)
