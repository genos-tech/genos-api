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

from django.core.exceptions import ImproperlyConfigured

from .settings import *  # noqa: F401,F403
from .settings import DATABASES, DEBUG

if not DEBUG:
    raise ImproperlyConfigured(
        "apis.settings_dev is for local development only (DEBUG must be on)."
    )

# Close each connection when its request ends, instead of keeping it for CONN_MAX_AGE seconds.
# Costs one reconnect per request on the local stack; nothing else changes.
DATABASES["default"]["CONN_MAX_AGE"] = 0

# The refresh cookie must survive a page on another origin (MIP-1871). The api is on
# localhost:8890; a page opened at http://127.0.0.1:3000 is cross-site to it, and Chrome sends a
# cross-site cookie only when it is SameSite=None; Secure. Chrome treats http://localhost as a
# secure origin, so the Secure attribute is accepted there. With SameSite=Lax the sign-in worked
# but no refresh cookie was kept, so the next page load's refresh got 403 and the person was
# signed out.
AUTH_COOKIE_SECURE = True
AUTH_COOKIE_SAMESITE = "None"
