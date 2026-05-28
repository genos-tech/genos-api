"""
URL routing for the unified `/api/v3/` chat surface.

Replaces the per-chat-type `/api/v2/{dm,gm,pm,mdm}/...` routes (in
sibling file `urls.py`). The legacy routes stay live during the rewrite
so the existing frontend keeps working; once the new FE ships, the
legacy `urls.py` block will be deleted.

URL shape (see plan §2):
    GET    /api/v3/channels/                              list user's channels
    GET    /api/v3/channels/{channel_id}/                 single channel detail + members
    GET    /api/v3/channels/{channel_id}/members/         member roster
    GET    /api/v3/channels/{channel_id}/messages/?since=ISO     delta sync
    GET    /api/v3/channels/{channel_id}/threads/?since=ISO      thread-reply delta
    GET    /api/v3/messages/{message_id}/                 single message detail

Mutating endpoints (POST/PATCH/DELETE for messages, reactions, read
cursors, pins, flags, channel create, member add/remove) ship in a
follow-up commit because they need the unified SocketIO handler rewrite
to ship in tandem (the WS layer proxies through the REST layer).
"""

from django.urls import path

from origin.views.chat.channel_views import (
    ChannelDetailView,
    ChannelListView,
    ChannelMembersView,
)
from origin.views.chat.message_views import (
    MessageDetailView,
    MessagesDeltaView,
    ThreadMessagesDeltaView,
)

urlpatterns = [
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
]
