"""Local development settings: the compose `api` service runs `manage.py runserver`.

Why this module exists (MIP-1873). Django's threaded dev server opens one Postgres
connection per request thread. With `CONN_MAX_AGE=600` (the production value, set in
`apis/settings.py`) every connection is kept open for ten minutes, and a request thread that
exits without closing its connection leaves it open until the garbage collector runs. On the
local stack that meant ~100 idle connections from the api container, enough to refuse every
other client of `genos-pg` ("sorry, too many clients already").

Production never loads this module: gunicorn and the deploy set `DJANGO_SETTINGS_MODULE` to
`apis.settings`, and the guard below refuses to import this module without DEBUG.
"""

import sys

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403
from .settings import CORS_ALLOWED_ORIGINS, DATABASES, DEBUG

if not DEBUG:
    raise ImproperlyConfigured(
        "apis.settings_dev is for local development only (DEBUG must be on)."
    )

# Never run the test suite on these settings (MIP-1881). `manage.py` lets a set
# DJANGO_SETTINGS_MODULE win over its test default, so with the compose `api` service selecting this
# module, `manage.py test` would skip the MIP-1466 live-credential guard in `apis.settings_test`
# (the compose env carries real keys) and run on the Redis cache and the dev database. The argv
# test is the one `manage.py` and `settings_test` use, so all three agree on what a test run is.
if sys.argv[1:2] == ["test"]:
    raise ImproperlyConfigured(
        "apis.settings_dev is not for tests: run genos-platform/docker/dt.sh, "
        "or pass --settings=apis.settings_test."
    )

# Close each connection when its request ends, instead of keeping it for CONN_MAX_AGE seconds.
# Costs one reconnect per request on the local stack; nothing else changes.
DATABASES["default"]["CONN_MAX_AGE"] = 0

# Dev CORS (MIP-1871). `apis/settings.py` allows every origin with credentials when DEBUG is on. On
# the local stack a page on any other localhost port is same-site with the api, so the Lax refresh
# cookie is sent to it and it could read the refresh response, which carries the access token.
# Keep CORS to the listed origins: the inherited, env-driven list plus 127.0.0.1:3000.
CORS_ALLOW_ALL_ORIGINS = False
CORS_ALLOWED_ORIGINS = list(dict.fromkeys([*CORS_ALLOWED_ORIGINS, "http://127.0.0.1:3000"]))
