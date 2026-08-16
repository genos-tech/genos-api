"""Tests for the CronCommand base (origin/management/cron_command.py).

Two jobs, both invisible when they break:

1. **Fail loud.** A scheduled command must exit non-zero when something
   fails, so the cron runner marks the run failed instead of green.
   CronCommand enforces that by tripping on any ERROR logged under the
   `origin` logger during the run.
2. **Drain.** Railway will not schedule a job more often than every 5
   minutes, so commands that want minutely resolution get it by making
   several passes inside one run. Untested, this fails the same way the
   thing it fixes did — silently, at the schedule level, where nothing
   red appears.
"""

import logging
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from origin.management.cron_command import CronCommand

# BaseCommand.execute() reads these option keys directly.
_EXEC_OPTS = {"force_color": False, "no_color": False, "skip_checks": True}


class FakeDrainClock:
    """A monotonic clock that only moves when something sleeps in it.

    The drain loop's entire contract is about elapsed time, so testing it
    against the real clock would mean either a four-minute test or a race.
    Here `sleep` advances the clock instead of waiting, which is also how
    the assertions want to read: the recorded sleeps *are* the schedule.

    Shared with the command-level drain test in `test_todo_reminders`.
    """

    def __init__(self, start: float = 1000.0) -> None:
        # Nonzero and arbitrary: `monotonic()` has no epoch, and a clock
        # starting at 0 would let an absolute/relative mix-up pass.
        self.start = start
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def elapsed(self) -> float:
        """Seconds into the run — for tests that also need a wall clock."""
        return self.now - self.start

    def patch(self):
        """Swap this in for the `time` module the drain loop reaches for."""
        return mock.patch("origin.management.cron_command.time", self)


class _LogsAppError(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.cron").error("something failed")


class _Clean(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        logging.getLogger("origin.tests.cron").info("all good")


class _LogsForeignError(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        # Outside the `origin` namespace — must NOT trip the wire.
        logging.getLogger("somethirdparty").error("not our problem")


class _Raises(CronCommand):
    requires_system_checks = []

    def handle(self, *args, **options):
        raise CommandError("explicit failure")


class TestCronCommand(SimpleTestCase):
    def test_error_log_fails_the_run(self):
        with self.assertRaises(CommandError):
            _LogsAppError().execute(**_EXEC_OPTS)

    def test_clean_run_succeeds(self):
        # No ERROR logged → no raise.
        _Clean().execute(**_EXEC_OPTS)

    def test_foreign_logger_error_is_ignored(self):
        # ERROR outside `origin.*` doesn't red the run.
        _LogsForeignError().execute(**_EXEC_OPTS)

    def test_explicit_raise_propagates(self):
        with self.assertRaises(CommandError):
            _Raises().execute(**_EXEC_OPTS)

    def test_tripwire_detached_after_run(self):
        # The handler must be removed so it doesn't leak across runs.
        before = list(logging.getLogger("origin").handlers)
        _Clean().execute(**_EXEC_OPTS)
        after = list(logging.getLogger("origin").handlers)
        self.assertEqual(before, after)


class _Drains(CronCommand):
    """Records the clock reading at the start of every pass it is given."""

    requires_system_checks = []

    def __init__(self, clock: FakeDrainClock, work_seconds: float = 0.0) -> None:
        super().__init__()
        self.clock = clock
        self.work_seconds = work_seconds
        self.pass_starts: list[float] = []
        self.numbers: list[int] = []

    def handle(self, *args, **options):
        for number in self.drain_passes(**options):
            self.pass_starts.append(self.clock.elapsed())
            self.numbers.append(number)
            self.clock.now += self.work_seconds


class _DrainsThenErrors(CronCommand):
    requires_system_checks = []

    def __init__(self) -> None:
        super().__init__()
        self.count = 0

    def handle(self, *args, **options):
        for number in self.drain_passes(**options):
            self.count += 1
            if number == 2:
                logging.getLogger("origin.tests.cron").error("pass 2 failed")


class TestDrainPasses(SimpleTestCase):
    """The passes a run makes, and when it stops making them.

    Every number here is load-bearing against Railway's behaviour: it will
    not schedule below 5 minutes, and it SKIPS a scheduled run whose
    predecessor is still alive. So a drain that overruns its window does
    not merely finish late — it cancels the next tick.
    """

    def _run(self, clock, work_seconds=0.0, **drain_opts):
        command = _Drains(clock, work_seconds=work_seconds)
        with clock.patch(), mock.patch(
            "origin.management.cron_command.close_old_connections"
        ) as closer:
            command.execute(**_EXEC_OPTS, **drain_opts)
        return command, closer

    def test_no_drain_seconds_makes_exactly_one_pass(self):
        # The default, and what a human running this by hand gets: one pass,
        # no sleeping, identical to the pre-drain behaviour.
        clock = FakeDrainClock()
        command, closer = self._run(clock, drain_seconds=0, drain_interval=60)
        self.assertEqual(command.numbers, [1])
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(closer.call_count, 0)

    def test_missing_options_make_exactly_one_pass(self):
        # `drain_passes(**options)` is also reached from code paths that
        # never went through the parser (a direct `handle()` call in a
        # test). Absent keys must mean "one pass", not a crash.
        clock = FakeDrainClock()
        command, _ = self._run(clock)
        self.assertEqual(command.numbers, [1])

    def test_the_shipped_budget_makes_four_passes_a_minute_apart(self):
        # 240 seconds at 60-second cadence — what railway-reminder-tick.toml
        # actually passes. Four passes covers the five-minute cron gap.
        clock = FakeDrainClock()
        command, closer = self._run(clock, drain_seconds=240, drain_interval=60)
        self.assertEqual(command.numbers, [1, 2, 3, 4])
        self.assertEqual(clock.sleeps, [60, 60, 60])
        # One reconnect check per sleep, and none before the first pass:
        # the connection was only idle across a sleep.
        self.assertEqual(closer.call_count, 3)

    def test_the_run_finishes_inside_the_cron_interval(self):
        # The whole point of 240 rather than 300. If a drain outlived its
        # window Railway would skip the next tick and halve the rate —
        # the exact failure this mechanism exists to fix.
        clock = FakeDrainClock()
        self._run(clock, drain_seconds=240, drain_interval=60)
        self.assertLess(clock.elapsed(), 300)

    def test_work_is_subtracted_from_the_sleep_not_added_to_the_cadence(self):
        # Passes must land a minute apart, not a-minute-plus-however-long-
        # the-last-one-took apart; otherwise the cadence drifts and the
        # budget buys fewer passes than it was sized for.
        clock = FakeDrainClock()
        command, _ = self._run(clock, work_seconds=20, drain_seconds=240, drain_interval=60)
        self.assertEqual(clock.sleeps, [40, 40, 40])
        self.assertEqual(command.pass_starts, [0, 60, 120, 180])

    def test_a_pass_that_outran_the_window_does_not_start_another(self):
        # A pass cannot be interrupted mid-flight, so the guarantee is
        # narrower than "never overrun": never START a pass past the
        # deadline, even when the cadence arithmetic alone would allow it.
        clock = FakeDrainClock()
        command, _ = self._run(clock, work_seconds=250, drain_seconds=240, drain_interval=60)
        self.assertEqual(command.numbers, [1])
        self.assertEqual(clock.sleeps, [])

    def test_a_pass_slower_than_the_interval_runs_back_to_back(self):
        # Cadence already blown, budget not. The sleep clamps to zero rather
        # than going negative, so passes run back to back — and the run can
        # end up past its own budget (270s here), which is why the budget is
        # sized with a minute of slack under the cron interval.
        clock = FakeDrainClock()
        command, _ = self._run(clock, work_seconds=90, drain_seconds=240, drain_interval=60)
        self.assertEqual(command.numbers, [1, 2, 3])
        self.assertEqual(clock.sleeps, [0, 0])
        self.assertLess(clock.elapsed(), 300)

    def test_interval_longer_than_budget_still_makes_one_pass(self):
        # A misconfigured pair must degrade to the old behaviour, not to
        # zero passes — a cron that silently does nothing is this whole
        # bug's signature.
        clock = FakeDrainClock()
        command, _ = self._run(clock, drain_seconds=60, drain_interval=600)
        self.assertEqual(command.numbers, [1])

    def test_flags_are_registered_on_every_cron_command(self):
        # Through the parser, not the kwargs: this is the wiring that lets
        # railway-*.toml pass `--drain-seconds` to a command that never
        # declared it. `create_parser` is the only thing holding it up.
        clock = FakeDrainClock()
        command = _Drains(clock)
        with clock.patch(), mock.patch("origin.management.cron_command.close_old_connections"):
            call_command(command, "--drain-seconds", "120", "--drain-interval", "60")
        self.assertEqual(command.numbers, [1, 2])

    def test_a_failing_pass_reds_the_run_without_stopping_the_drain(self):
        # Both halves matter. The remaining passes still run, because one
        # kind of reminder failing must not strand the other; and the run
        # still exits non-zero, because that is the only signal Railway reads.
        clock = FakeDrainClock()
        command = _DrainsThenErrors()
        with clock.patch(), mock.patch("origin.management.cron_command.close_old_connections"):
            with self.assertRaises(CommandError):
                command.execute(**_EXEC_OPTS, drain_seconds=180, drain_interval=60)
        self.assertEqual(command.count, 3)
