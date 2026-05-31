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

from .settings import *  # noqa: F401,F403,E402

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

# Speed up user-creation-heavy tests.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
