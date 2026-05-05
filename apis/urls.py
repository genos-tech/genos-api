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

from django.conf import settings
from django.conf.urls.static import static
from origin.urls.common import urls as common_urls
from origin.urls.chat import urls as chat_urls
from origin.urls.project import urls as prj_urls
from origin.urls.task import urls as task_urls
from origin.urls.note import urls as note_urls

urlpatterns = [
    # path("admin/", admin.site.urls),
]

urlpatterns.extend(common_urls.urlpatterns)
urlpatterns.extend(chat_urls.urlpatterns)
urlpatterns.extend(prj_urls.urlpatterns)
urlpatterns.extend(task_urls.urlpatterns)
urlpatterns.extend(note_urls.urlpatterns)

# Serve user-uploaded media in *both* dev and prod. Until the app
# moves to django-storages + S3/R2, a Railway Volume mounted at
# `MEDIA_ROOT` is what makes this durable; this URL pattern is what
# makes the files reachable. Django's `static()` helper is documented
# as "development-only" because it's not optimized for high traffic,
# but for an MVP behind Railway's edge it is fine. When you migrate
# uploads to object storage, delete this line — the bucket's own
# domain (or a CDN in front of it) will serve `/media/` instead.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
