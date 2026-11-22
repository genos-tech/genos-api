"""Test helper: the AI-app notice defaults as they were before MIP-2009.

Since MIP-2009 only the risky actions (deletes, 10+ items at once) start on. Tests of the notice
itself (what it says, who gets it, how it is pushed) are about the notice, not its default, so they
run with every switch on (except `ai_app_note_edited`, always off by default). The defaults
themselves are pinned in `test_mcp_notice_risky_defaults`.
"""

from __future__ import annotations

from unittest import mock

from origin.services import ai_app_switches, webpush_gating


def all_notices_on_by_default(test_case) -> None:
    on = {k: k != "ai_app_note_edited" for k in ai_app_switches.KEYS}
    test_case.notice_default_patchers = []
    for target in (ai_app_switches.DEFAULTS, webpush_gating._PUSH_DEFAULTS):
        patcher = mock.patch.dict(target, on)
        patcher.start()
        test_case.addCleanup(patcher.stop)
        test_case.notice_default_patchers.append(patcher)


def restore_real_defaults(test_case) -> None:
    """For the tests of the defaults themselves: undo `all_notices_on_by_default`."""
    for patcher in test_case.notice_default_patchers:
        patcher.stop()
