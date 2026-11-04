"""Pin the clock a fixed-window counter reads, for tests that count a window.

A helper module, not a test module, so more than one test file can use it without one
importing another (the same shape as `notice_watch.py`).
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest import mock

from origin.services import security_events


class MidMinute:
    """Freeze the clock a counter reads, at the middle of a minute (MIP-993, MIP-995, MIP-999).

    `note_forbidden` counts in fixed one-minute windows keyed on `int(time.time() // 60)`.
    A test that makes exactly the 3 refusals the threshold needs, then expects one alert,
    fails whenever the real clock crosses a minute between its first refusal and its last:
    one lands in the old window, two in the new one, and nothing reaches 3 (`0 != 1`). A
    loaded run, which stretches a test from milliseconds to seconds, makes that likely.
    A test that makes many refusals and expects exactly one alert fails the other way: a
    split that leaves 3 or more on each side alerts twice (`2 != 1`). The API-key counters
    (`note_api_key_used`, `note_api_key_rejected`) count in fixed hours keyed on
    `int(time.time() // 3600)`, and a test that sends two requests and expects one row fails
    the same way across an hour boundary; a frozen clock holds the hour too.

    The same goes for the other fixed-window counters, each reading `time` in its own module:
    `throttles` (`WindowRateThrottle`, a minute or an hour) and `ops_alerts` (the 5xx burst).
    `clock_modules` names the modules to pin; it defaults to `security_events`, and a test
    class that counts through another says so: `clock_modules = (throttles,)`.

    The fake goes on those modules only: patching `time.time` itself would freeze the stdlib
    for everyone, the cache's expiry included (the same shape as `test_throttles`, MIP-858).
    It is derived from the real clock, so it still names the minute the test started in, and
    it keeps the real `monotonic`, which the throttles' cache breaker reads.

    Put it first among the bases: `BaseAPITestCase.setUp` does not call `super().setUp()`,
    so a mixin listed after it would never run. The same goes for a class that defines its own
    `setUp`: it must call `super().setUp()`, or this one is shadowed.
    """

    clock_modules = (security_events,)

    def setUp(self):
        super().setUp()
        now = 60.0 * int(time.time() // 60) + 30.0
        fake = SimpleNamespace(time=lambda: now, monotonic=time.monotonic)
        for module in self.clock_modules:
            patcher = mock.patch.object(module, "time", fake)
            patcher.start()
            self.addCleanup(patcher.stop)
