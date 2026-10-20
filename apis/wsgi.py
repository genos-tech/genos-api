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
