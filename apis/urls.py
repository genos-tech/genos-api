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
from urllib.parse import quote

from django.conf import settings
from django.urls import re_path
from django.views.static import serve as _serve_static

from origin.search_engine import urls as search_engine_urls
from origin.urls.chat import urls as chat_urls
from origin.urls.chat import v3_urls as chat_v3_urls
from origin.urls.common import urls as common_urls
from origin.urls.note import urls as note_urls
from origin.urls.project import urls as prj_urls
from origin.urls.task import urls as task_urls

urlpatterns = [
    # path("admin/", admin.site.urls),
]

urlpatterns.extend(common_urls.urlpatterns)
urlpatterns.extend(chat_urls.urlpatterns)
urlpatterns.extend(chat_v3_urls.urlpatterns)
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
def _serve_media_as_attachment(request, path, document_root=None):
    # Force `Content-Disposition: attachment` on every media response.
    # BlockNote's toolbar FileDownloadButton is hardcoded to
    # `window.open(url)`, which makes the browser pick rendering by
    # MIME — `.py` (text/x-python) opens in a tab while `.md` falls
    # back to "Save As" since no native viewer exists. Forcing the
    # attachment disposition gives uniform "download the file"
    # behavior across types, and also fixes any future bare anchor /
    # `target=_blank` clicks on attachment URLs.
    #
    # `<img>` / `<video>` / `<audio>` subresource loads ignore
    # Content-Disposition, so inline image previews in chat / note
    # bodies still render normally — the header only affects
    # top-level navigation and `fetch`/XHR consumers (which we
    # already wrap with `URL.createObjectURL` in `downloadFile`).
    response = _serve_static(request, path, document_root=document_root)
    filename = path.rsplit("/", 1)[-1] or "download"
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "download"
    response["Content-Disposition"] = (
        f'attachment; filename="{ascii_fallback}"; ' f"filename*=UTF-8''{quote(filename)}"
    )
    return response


urlpatterns += [
    re_path(
        r"^%s(?P<path>.*)$" % re.escape(settings.MEDIA_URL.lstrip("/")),
        _serve_media_as_attachment,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
