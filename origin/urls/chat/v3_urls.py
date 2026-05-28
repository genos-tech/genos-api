"""
URL routing for the unified `/api/v3/` chat surface.

Replaces the per-chat-type `/api/v2/{dm,gm,pm,mdm}/...` routes (in
sibling file `urls.py`). The legacy routes stay live during the rewrite
so the existing frontend keeps working; once the new FE ships, the
legacy `urls.py` block will be deleted.

URL shape (see plan §2):

  Channels
    GET    /api/v3/channels/                              list user's channels
    POST   /api/v3/channels/                              create DM/GM/MDM
    GET    /api/v3/channels/{id}/                         single channel detail + members
    GET    /api/v3/channels/{id}/members/                 member roster
    POST   /api/v3/channels/{id}/members/                 add member(s) (GM/MDM only)
    DELETE /api/v3/channels/{id}/members/{user_id}/       remove a member

  Messages
    GET    /api/v3/channels/{id}/messages/?since=ISO      delta sync
    POST   /api/v3/channels/{id}/messages/                send a message
    GET    /api/v3/channels/{id}/threads/?since=ISO       thread-reply delta
    GET    /api/v3/messages/{id}/                         single message detail
    PATCH  /api/v3/messages/{id}/                         edit
    DELETE /api/v3/messages/{id}/                         soft-delete

  Reactions
    POST   /api/v3/messages/{id}/reactions/               add reaction (body: {emoji})
    DELETE /api/v3/messages/{id}/reactions/               remove reaction

  Read cursor
    PUT    /api/v3/channels/{id}/read_cursor/             advance cursor

  Pin / Flag
    POST   /api/v3/channels/{id}/pin/                     pin channel
    DELETE /api/v3/channels/{id}/pin/                     unpin
    POST   /api/v3/messages/{id}/flag/                    flag message
    DELETE /api/v3/messages/{id}/flag/                    unflag
"""

from django.urls import path

from origin.views.chat.channel_views import (
    ChannelDetailView,
    ChannelListView,
    ChannelMemberDetailView,
    ChannelMembersView,
)
from origin.views.chat.message_views import (
    MessageDetailView,
    MessagesDeltaView,
    ThreadMessagesDeltaView,
)
from origin.views.chat.pin_flag_views import FlagView, PinView
from origin.views.chat.reaction_views_v3 import MessageReactionsView
from origin.views.chat.read_cursor_views import ReadCursorView

urlpatterns = [
    # Channels
    path(
        "api/v3/channels/",
        ChannelListView.as_view(),
        name="v3_channel_list",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/",
        ChannelDetailView.as_view(),
        name="v3_channel_detail",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/members/",
        ChannelMembersView.as_view(),
        name="v3_channel_members",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/members/<uuid:user_id>/",
        ChannelMemberDetailView.as_view(),
        name="v3_channel_member_detail",
    ),
    # Messages
    path(
        "api/v3/channels/<uuid:channel_id>/messages/",
        MessagesDeltaView.as_view(),
        name="v3_messages_delta",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/threads/",
        ThreadMessagesDeltaView.as_view(),
        name="v3_thread_messages_delta",
    ),
    path(
        "api/v3/messages/<uuid:message_id>/",
        MessageDetailView.as_view(),
        name="v3_message_detail",
    ),
    # Reactions
    path(
        "api/v3/messages/<uuid:message_id>/reactions/",
        MessageReactionsView.as_view(),
        name="v3_message_reactions",
    ),
    # Read cursor
    path(
        "api/v3/channels/<uuid:channel_id>/read_cursor/",
        ReadCursorView.as_view(),
        name="v3_read_cursor",
    ),
    # Pin / Flag
    path(
        "api/v3/channels/<uuid:channel_id>/pin/",
        PinView.as_view(),
        name="v3_channel_pin",
    ),
    path(
        "api/v3/messages/<uuid:message_id>/flag/",
        FlagView.as_view(),
        name="v3_message_flag",
    ),
]
