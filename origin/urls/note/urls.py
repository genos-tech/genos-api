from django.urls import path

from origin.views.note.note_views import *


urlpatterns = [
    path("api/v2/note/personal/", PersonalNoteMasterView.as_view(), name="personal_note"),
    path(
        "api/v2/note/personal/single/",
        SinglePersonalNoteView.as_view(),
        name="personal_single_note",
    ),
    path("api/v2/note/task/", TaskNoteMasterView.as_view(), name="task_note"),
    path("api/v2/note/chat/", ChatNoteMasterView.as_view(), name="chat_note"),
    path("api/v2/note/all/", AllNotesView.as_view(), name="all_notes"),
    path("api/v2/note/meta/", AllNoteMetaView.as_view(), name="note_meta"),
    path(
        "api/v2/note/personal/attachment/",
        PersonalNoteAttachmentView.as_view(),
        name="personal_attachment",
    ),
]
