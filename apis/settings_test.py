"""Settings for running the test suite (used by CI and local test runs).

Production/dev point CACHES at Redis via django-redis with
IGNORE_EXCEPTIONS=True, which *silently no-ops* when Redis is unreachable.
That makes tests asserting caching behavior fail in a Redis-less environment
(CI, or a host that can't resolve the `redis` service hostname). Using an
in-process LocMemCache makes caching real and deterministic, so the only
external service the test suite needs is Postgres.

Run with: manage.py test --settings=apis.settings_test
"""

import os
import sys

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403,E402

# MIP-1466: `manage.py test` must never run with live model/billing credentials in the process.
# A test that forgets to stub a model call would make a BILLED call (the dev stack's compose env
# carries the real keys; a test container built from it inherits them). The evals are not tests:
# they run `manage.py agent_eval*` with real credentials on purpose and are not affected. Opt out,
# knowingly, with ALLOW_LIVE_LLM_IN_TESTS=1. The key-free runner is `docker/dt.sh` in genos-platform.
_LIVE_CREDENTIALS = (
    "CLAUDE_API_KEY",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "COHERE_API_KEY",
    "TAVILY_API_KEY",
)
_REAL_KEY_PLACEHOLDERS = ("", "test", "dummy", "none", "no-key", "fake")
_live = [
    name
    for name in _LIVE_CREDENTIALS
    if (os.environ.get(name) or "").strip().lower() not in _REAL_KEY_PLACEHOLDERS
    and not (os.environ.get(name) or "").strip().lower().startswith(("no-key", "test-", "fake-"))
]
# A Stripe key is billing only when it is a LIVE-mode key (a test-mode key moves no money).
if (os.environ.get("STRIPE_SECRET_KEY") or "").strip().startswith(("sk_live_", "rk_live_")):
    _live.append("STRIPE_SECRET_KEY")
if _live and sys.argv[1:2] == ["test"] and os.environ.get("ALLOW_LIVE_LLM_IN_TESTS") != "1":
    raise ImproperlyConfigured(
        "The test suite refuses to run with live credentials in the environment ("
        + ", ".join(_live)
        + "): a test that forgets to stub a model call would be billed. Run it without them "
        "(genos-platform/docker/dt.sh does), or set ALLOW_LIVE_LLM_IN_TESTS=1 if you mean to."
    )

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

# Opt-in isolated test database, so several test runners can execute in
# parallel against the same Postgres server without colliding on the shared
# `test_origin` database. Inert in CI (the var is unset there → default
# `test_origin`). Set DJANGO_TEST_DB_NAME=test_origin_<suffix> per runner.
_test_db_name = os.environ.get("DJANGO_TEST_DB_NAME")
if _test_db_name:
    DATABASES["default"].setdefault("TEST", {})  # noqa: F405
    DATABASES["default"]["TEST"]["NAME"] = _test_db_name  # noqa: F405

# Scheduled reports are Core and up (MIP-1213) and every test user is Free by default. The suites
# that are about schedules, not about the plan, run with the capability on for every tier, as the
# MCP suites do for `mcp_enabled`. The unchanged table stays available for the gate's own tests
# (`origin/tests/test_schedule_plan_gate.py`).
PRODUCTION_TIER_QUOTAS = SEARCH_ENGINE["TIER_QUOTAS"]  # noqa: F405
# MIP-1089 turned ten agent flags ON by default in production. The suite keeps them OFF here:
# every flag's tests (and the "flag off is byte-identical to main" pins) turn the flag on or off
# EXPLICITLY, and the many tests that are about something else assume the unflagged behaviour.
# `apis.settings.SEARCH_ENGINE` (not this copy) keeps the production defaults, and
# `origin/tests/test_agent_all_flags_on.py` pins them.
AGENT_FLAGS_ON_IN_PRODUCTION = (
    "AGENT_RESUME_KEEPS_BLOCKS",
    "AGENT_RESUME_KEEPS_TURNS",
    "AGENT_CLOSING_LINE_IN_ORIGIN_THREAD",
    "AGENT_REJECT_REASON",
    "AGENT_TELL_MODEL_UNDO",
    "AGENT_FETCH_TASK_ANCHORS",
    "AGENT_INBOX_REPORT_LABEL",
    "AGENT_TEAM_IN_CURRENT_USER",
    "AGENT_NOTE_THREADS_IN_GET_NOTE",
    "AGENT_CLOSED_ANCESTOR_RULE",
    "AGENT_INAPP_TIMELINE_TOOL",
)
SEARCH_ENGINE = {  # noqa: F405
    **SEARCH_ENGINE,  # noqa: F405
    "TIER_QUOTAS": {
        tier: {**cfg, "schedules_enabled": True} for tier, cfg in PRODUCTION_TIER_QUOTAS.items()
    },
    **{flag: False for flag in AGENT_FLAGS_ON_IN_PRODUCTION},
}

# Speed up user-creation-heavy tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# A fixed Fernet key so anything that encrypts at rest works in tests.
# `FIELD_ENCRYPTION_KEYS` is optional in dev and REQUIRED the moment
# something actually encrypts — `crypto._get_fernet` raises rather than
# silently storing plaintext — so a suite that depends on the ambient
# environment passes on a developer machine that has it and fails in CI,
# which does not. That is the same failure this module already exists to
# prevent for Redis: the only external service the tests should need is
# Postgres.
#
# NOT a production key and never used as one: settings_test is only
# loaded by `--settings=apis.settings_test`. Tests that need a DIFFERENT
# key, or need it absent, still override it per-case — see
# `test_services_extra.TestCrypto`, which also clears the `lru_cache` on
# `_get_fernet` so a key cached by an earlier test cannot leak.
FIELD_ENCRYPTION_KEYS = ["zH8Zq3nQ1vJ5xK7bR2wT9yU4pS6dF0gA8cE1hL3mN5o="]

# Same reasoning, for the blind index every email lookup goes through
# (MIP-619): without a key nobody could sign up or sign in. Not a
# production key either.
BLIND_INDEX_KEYS = ["k1mN3bV5cX7zL9aS2dF4gH6jQ8wE0rT1yU3iO5pA7sD="]
