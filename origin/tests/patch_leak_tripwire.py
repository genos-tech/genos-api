"""Fail the test that leaves a module attribute patched, by name, at its own tearDown (MIP-1765).

Why: MIP-1760. A test entered `mock.patch.object(search, "_run_keyword")` from a pool thread and
from the test thread at once; the two restores interleaved and the module kept a fake
`_run_keyword` for the rest of the shard. The failures showed up 11 tests later, in
`test_search_lexical_tiers`, and only on some runs, so it read as an unexplained flake.

What it does: before each test (before Django's pre-setup, so after `setUpClass`) it records the
identity of every CALLABLE attribute of the modules in `WATCHED_MODULES`; after the test, its
`tearDown`, every `addCleanup` and Django's post-teardown have run, it compares. An attribute that
is a different object, or gone, makes THAT test fail with the test id, the attribute and what it
holds now. The attribute is put back, so the leak is reported once, at its source, and does not
turn the next hundred tests red.

Scope (what it does NOT see):
  * only callables (functions, classes, mocks): a patched constant or a rebound module-level
    cache is not compared, because a legitimate run changes those and a false positive would
    train everyone to ignore the failure;
  * only the modules listed below: add the module (one line) when a test patches a new one;
  * only Django test cases (`SimpleTestCase` and everything under it): the hook is
    `_setup_and_call`, which wraps the whole test. Plain `unittest.TestCase` classes (a handful of
    pure-logic modules, none of them patches module attributes) are not covered;
  * a patch started in `setUpClass` and stopped in `tearDownClass` is left alone (it is the same
    before and after each test), and one that leaks out of `tearDownClass` is not seen.

The failure is added to the result after the test's own outcome, so a leaking test that passed
reads `ok` and then `FAIL: <test id>` in a verbose run, and the summary and the exit status name
it. A test that replaces a watched callable on purpose (a reload, a rebuilt cache) must put the
original back; the tripwire puts back what it finds different.

Cost: no database, one pass over about a thousand dict entries per test (about 0.15 ms).

How it is wired: `origin/tests/__init__.py` calls `install()` when the package is imported, so
every `manage.py test` that names a module of this package gets it, without a change to
`manage.py`, `apis/test_runner.py` or `apis/settings_test.py`.

If it fails a test of yours: the patch was started and not stopped. Use `with mock.patch...`
or `self.enterContext(...)`/`addCleanup(patcher.stop)`, and never enter a patch inside a function
that runs on another thread: two threads restoring one attribute can put back each other's mock.
"""

from __future__ import annotations

import importlib
import sys
from unittest import mock

from django.test import SimpleTestCase

# Modules whose attributes tests patch the most (`grep -rhoE 'patch\(.origin[a-z_.]+' origin/tests`),
# led by the search engine, whose lanes run on threads. A name that does not import fails
# `test_patch_leak_tripwire`, so a typo cannot quietly watch nothing.
WATCHED_MODULES = (
    "origin.search_engine.search",
    "origin.search_engine.agent_views",
    "origin.search_engine.views",
    "origin.search_engine.ingestion",
    "origin.search_engine.purge",
    "origin.search_engine.reranker",
    "origin.search_engine.opensearch_client",
    "origin.search_engine.friendly_titles",
    "origin.search_engine.query_rewriter",
    "origin.search_engine.note_context",
    "origin.search_engine.shared_answers",
    "origin.search_engine.llm",
    "origin.search_engine.embeddings",
    "origin.services.realtime_push",
    "origin.services.collab_push",
    "origin.services.webpush_dispatch",
    "origin.services.github_webhooks",
    "origin.services.starter_seeder",
    "origin.services.oauth.tokens",
    "origin.services.ops_alerts",
    "origin.views.common.github_views",
    "origin.views.common.calendar_views",
    "origin.views.common.auth_views",
    "origin.views.common.team_views",
    "django.utils.timezone",
    "time",
    "requests",
    "httpx",
)

_MISSING = object()
_modules = None  # the imported modules, resolved on first use (apps must be loaded)


def _watched():
    global _modules
    if _modules is None:
        resolved = []
        for name in WATCHED_MODULES:
            try:
                resolved.append(importlib.import_module(name))
            except Exception:  # noqa: BLE001 - any import-time error, not only ImportError
                pass  # `test_patch_leak_tripwire` fails on it; do not break every test
        _modules = tuple(resolved)
    return _modules


def snapshot():
    """{module: [(name, object), ...]} for every callable attribute of the watched modules."""
    return {
        module: [(n, v) for n, v in list(vars(module).items()) if callable(v)]
        for module in _watched()
    }


def leaks(before):
    """[(module, name, original, now)] for every attribute of `before` that is not the same object."""
    found = []
    for module, items in before.items():
        current = vars(module)
        for name, original in items:
            now = current.get(name, _MISSING)
            if now is not original:
                found.append((module, name, original, now))
    return found


def restore(found):
    for module, name, original, _now in found:
        setattr(module, name, original)


def describe(test, found):
    lines = [
        f"{test.id()} left module attributes patched after its tearDown and cleanups "
        "(MIP-1765; the attributes were put back so later tests are not affected):"
    ]
    for module, name, original, now in found:
        shown = "removed" if now is _MISSING else mock_name(now)
        lines.append(f"  {module.__name__}.{name}: now {shown}, was {mock_name(original)}")
    lines.append(
        "Stop every patch the test starts: `with mock.patch...`, `self.enterContext(...)` or "
        "`addCleanup(patcher.stop)`. Do not enter a patch on another thread: two threads "
        "restoring one attribute can put back each other's mock (MIP-1760)."
    )
    return "\n".join(lines)


def mock_name(obj):
    kind = "mock" if isinstance(obj, mock.NonCallableMock) else "object"
    return f"{kind} {obj!r}"[:160]


class PatchLeakError(AssertionError):
    pass


def install():
    """Hook every Django test case once. Safe to call again.

    The hook is `SimpleTestCase._setup_and_call`, the one place Django runs a test's pre-setup,
    `setUp`, the test, `tearDown`, the cleanups and the post-teardown in order, so the comparison
    happens after every one of them. (`_pre_setup` is a classmethod and Django's subclasses call
    it through `super()`, so it is not a safe place to hang an instance snapshot.)
    """
    if getattr(SimpleTestCase, "_patch_leak_tripwire", False):
        return
    setup_and_call = SimpleTestCase._setup_and_call

    def _setup_and_call(self, result, debug=False):
        before = snapshot()
        try:
            setup_and_call(self, result, debug)
        finally:
            found = leaks(before)
            if found:
                restore(found)  # always, also when `debug` is re-raising another error
                error = PatchLeakError(describe(self, found))
                if debug:
                    if sys.exc_info()[0] is None:
                        raise error
                else:
                    try:
                        raise error
                    except PatchLeakError:
                        result.addFailure(self, sys.exc_info())

    SimpleTestCase._setup_and_call = _setup_and_call
    SimpleTestCase._patch_leak_tripwire = True
