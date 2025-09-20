from django.urls import path

from origin.views.note.note_views import *


urlpatterns = [
    path("api/v2/note/personal/", PersonalNoteMasterView.as_view(), name="personal_note"),
    path("api/v2/note/", SingleNoteView.as_view(), name="my_note_meta"),
    path("api/v2/note/all/", AllNotesView.as_view(), name="my_note"),
    path("api/v2/note/meta/", AllNoteMetaView.as_view(), name="my_note_meta"),
]
