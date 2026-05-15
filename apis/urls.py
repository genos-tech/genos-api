"""
URL configuration for apis project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import re

from django.conf import settings
from django.urls import re_path
from django.views.static import serve
from origin.urls.common import urls as common_urls
from origin.urls.chat import urls as chat_urls
from origin.urls.project import urls as prj_urls
from origin.urls.task import urls as task_urls
from origin.urls.note import urls as note_urls
from origin.search_engine import urls as search_engine_urls

urlpatterns = [
    # path("admin/", admin.site.urls),
]

urlpatterns.extend(common_urls.urlpatterns)
urlpatterns.extend(chat_urls.urlpatterns)
urlpatterns.extend(prj_urls.urlpatterns)
urlpatterns.extend(task_urls.urlpatterns)
urlpatterns.extend(note_urls.urlpatterns)
urlpatterns.extend(search_engine_urls.urlpatterns)

# Serve user-uploaded media in *both* dev and prod.
#
# We register the route directly via `re_path` + `serve` instead of
# `django.conf.urls.static.static(...)` because that helper short-
# circuits to `return []` when `DEBUG=False` (see Django source). In
# production every `/media/...` request would otherwise miss the URL
# resolver entirely and 404, leaving the avatar / attachment images
# perma-broken even when the file is sitting right there on the
# Railway Volume mounted at `MEDIA_ROOT`.
#
# `django.views.static.serve` is the same view `static()` would have
# wired up; it's marked "not optimized for high traffic" in the docs,
# which is fine for our MVP volume. When the app migrates uploads to
# django-storages + S3 / R2, delete this block — the bucket's own
# domain (or a CDN in front of it) will serve `/media/` instead.
urlpatterns += [
    re_path(
        r"^%s(?P<path>.*)$" % re.escape(settings.MEDIA_URL.lstrip("/")),
        serve,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
