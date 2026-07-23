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
    POST   /api/v3/channels/{id}/join/                    self-join a public GM

  Messages
    GET    /api/v3/channels/{id}/messages/?since=ISO      delta sync
    POST   /api/v3/channels/{id}/messages/                send a message
    GET    /api/v3/channels/{id}/threads/?since=ISO       thread-reply delta
    GET    /api/v3/messages/{id}/                         single message detail
    PATCH  /api/v3/messages/{id}/                         edit
    DELETE /api/v3/messages/{id}/                         soft-delete

  Attachments
    POST   /api/v3/messages/{id}/attachments/             upload a file (multipart)

  Reactions
    POST   /api/v3/messages/{id}/reactions/               add reaction (body: {emoji})
    DELETE /api/v3/messages/{id}/reactions/               remove reaction

  Read cursor
    PUT    /api/v3/channels/{id}/read_cursor/             advance cursor

  Pin / Flag
    POST   /api/v3/channels/{id}/pin/                     pin channel
    DELETE /api/v3/channels/{id}/pin/                     unpin
    GET    /api/v3/pins/                                  list pinned channels
    POST   /api/v3/messages/{id}/flag/                    flag message
    DELETE /api/v3/messages/{id}/flag/                    unflag

  Personal tags (per-user PRIVATE labels on GM channels)
    GET    /api/v3/personal-tags/                         tags + assignments + default chips
    POST   /api/v3/personal-tags/                         create a tag
    PATCH  /api/v3/personal-tags/{tag_id}/                rename / recolor / pin
    DELETE /api/v3/personal-tags/{tag_id}/                delete (assignments cascade)
    PUT    /api/v3/channels/{id}/personal-tags/           replace the channel's tag set
"""

from django.urls import path

from origin.views.chat.activity_views_v3 import (
    ActivityListView,
    ActivityReadAllView,
    ActivityReadBatchView,
    ActivityReadView,
    ActivitySurfaceView,
)
from origin.views.chat.channel_views import (
    ChannelDetailView,
    ChannelInlineUploadView,
    ChannelJoinView,
    ChannelListView,
    ChannelMemberDetailView,
    ChannelMembersView,
    ChannelProfileImageView,
)
from origin.views.chat.message_views import (
    MessageAttachmentsView,
    MessageDetailView,
    MessagesDeltaView,
    TaskCardMessageView,
    ThreadMessagesDeltaView,
)
from origin.views.chat.personal_tag_views import (
    ChannelPersonalTagsView,
    PersonalTagDetailView,
    PersonalTagListView,
)
from origin.views.chat.pin_flag_views import (
    FlagListView,
    FlagView,
    PinListView,
    PinView,
)
from origin.views.chat.reaction_views_v3 import MessageReactionsView
from origin.views.chat.read_cursor_views import ReadCursorView
from origin.views.chat.search_views_v3 import SearchTeamMembersAndGroupsView

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
    path(
        "api/v3/channels/<uuid:channel_id>/join/",
        ChannelJoinView.as_view(),
        name="v3_channel_join",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/profile/image/",
        ChannelProfileImageView.as_view(),
        name="v3_channel_profile_image",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/uploads/",
        ChannelInlineUploadView.as_view(),
        name="v3_channel_inline_upload",
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
    # Task-card header message (rewrite the PM card body after a task edit)
    path(
        "api/v3/tasks/<int:task_id>/card-message/",
        TaskCardMessageView.as_view(),
        name="v3_task_card_message",
    ),
    # Attachments
    path(
        "api/v3/messages/<uuid:message_id>/attachments/",
        MessageAttachmentsView.as_view(),
        name="v3_message_attachments",
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
    path(
        "api/v3/flags/",
        FlagListView.as_view(),
        name="v3_flag_list",
    ),
    path(
        "api/v3/pins/",
        PinListView.as_view(),
        name="v3_pin_list",
    ),
    # Personal tags (per-user PRIVATE labels on GM channels)
    path(
        "api/v3/personal-tags/",
        PersonalTagListView.as_view(),
        name="v3_personal_tag_list",
    ),
    path(
        "api/v3/personal-tags/<int:tag_id>/",
        PersonalTagDetailView.as_view(),
        name="v3_personal_tag_detail",
    ),
    path(
        "api/v3/channels/<uuid:channel_id>/personal-tags/",
        ChannelPersonalTagsView.as_view(),
        name="v3_channel_personal_tags",
    ),
    # Activity feed
    path(
        "api/v3/activities/",
        ActivityListView.as_view(),
        name="v3_activity_list",
    ),
    path(
        "api/v3/activities/<uuid:activity_id>/read/",
        ActivityReadView.as_view(),
        name="v3_activity_read",
    ),
    path(
        "api/v3/activities/read-all/",
        ActivityReadAllView.as_view(),
        name="v3_activity_read_all",
    ),
    path(
        "api/v3/activities/read-batch/",
        ActivityReadBatchView.as_view(),
        name="v3_activity_read_batch",
    ),
    path(
        "api/v3/activities/surface/",
        ActivitySurfaceView.as_view(),
        name="v3_activity_surface",
    ),
    # Chat search (replaces legacy /api/v2/search/teamMembersAndGroups/)
    path(
        "api/v3/search/teamMembersAndGroups/",
        SearchTeamMembersAndGroupsView.as_view(),
        name="v3_search_team_members_and_groups",
    ),
]
