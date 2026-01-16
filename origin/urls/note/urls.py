from django.urls import path

from origin.views.note.personal_note_views import *
from origin.views.note.chat_note_views import *
from origin.views.note.task_note_views import *
from origin.views.note.favorite_note_views import *


urlpatterns = [
    path("api/v2/note/personal/", PersonalNoteMasterView.as_view(), name="personal_note"),
    path(
        "api/v2/note/personal/single/",
        SinglePersonalNoteView.as_view(),
        name="personal_single_note",
    ),
    path("api/v2/note/personal/all/", AllPersonalNotesView.as_view(), name="all_personal_notes"),
    path(
        "api/v2/note/personal/meta/", AllPersonalNoteMetaView.as_view(), name="personal_note_meta"
    ),
    path(
        "api/v2/note/personal/attachment/",
        PersonalNoteAttachmentView.as_view(),
        name="personal_attachment",
    ),
    path("api/v2/note/task/", TaskNoteMasterView.as_view(), name="task_note"),
    path(
        "api/v2/note/task/single/",
        SingleTaskNoteView.as_view(),
        name="task_single_note",
    ),
    path("api/v2/note/task/all/", AllTaskNotesView.as_view(), name="all_task_notes"),
    path("api/v2/note/task/meta/", AllTaskNoteMetaView.as_view(), name="task_note_meta"),
    path(
        "api/v2/note/task/attachment/",
        TaskNoteAttachmentView.as_view(),
        name="task_attachment",
    ),
    path("api/v2/note/chat/", ChatNoteMasterView.as_view(), name="chat_note"),
    path(
        "api/v2/note/chat/single/",
        SingleChatNoteView.as_view(),
        name="chat_single_note",
    ),
    path("api/v2/note/chat/all/", AllChatNotesView.as_view(), name="all_chat_notes"),
    path("api/v2/note/chat/meta/", AllChatNoteMetaView.as_view(), name="task_note_meta"),
    path(
        "api/v2/note/chat/attachment/",
        ChatNoteAttachmentView.as_view(),
        name="chat_attachment",
    ),
    path(
        "api/v2/note/chat/subs/",
        ChatSubNotesView.as_view(),
        name="chat_sub_notes",
    ),
    # Favorite note endpoints
    path(
        "api/v2/note/favorite/",
        NoteFavoriteView.as_view(),
        name="note_favorite",
    ),
    path(
        "api/v2/note/favorite/meta/",
        AllFavoriteNotesMetaView.as_view(),
        name="all_favorite_notes_meta",
    ),
    path(
        "api/v2/note/favorite/check/",
        CheckNoteFavoriteView.as_view(),
        name="check_note_favorite",
    ),
]
