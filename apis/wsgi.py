"""
WSGI config for apis project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/5.1/howto/deployment/wsgi/
"""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "apis.settings")

application = get_wsgi_application()


# The web server's own boot check (MIP-719): browser-facing settings the
# crons don't set, so it can't live in settings.py. See its docstring.
from django.conf import settings  # noqa: E402

from apis.settings import validate_web_config  # noqa: E402

validate_web_config(
    debug=settings.DEBUG,
    allowed_hosts=settings.ALLOWED_HOSTS,
    cors_allowed_origins=settings.CORS_ALLOWED_ORIGINS,
    field_encryption_keys=settings.FIELD_ENCRYPTION_KEYS,
    blind_index_keys=settings.BLIND_INDEX_KEYS,
    search_engine=settings.SEARCH_ENGINE,
)

# Import the URLconf, and with it every view module, now rather than on the first
# request (MIP-944). Under `gunicorn --preload` this runs once, in the master, so a
# worker recycled by `--max-requests` is a fork that already has everything loaded
# instead of a first request that spends a second or more importing views.
from django.urls import Resolver404, resolve  # noqa: E402

try:
    resolve("/")
except Resolver404:
    pass
