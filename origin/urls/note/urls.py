from django.urls import path

from origin.views.note.note_views import *


urlpatterns = [
    path("api/v2/note/personal/", PersonalNoteMasterView.as_view(), name="personal_note"),
]
