from django.urls import path
from origin.views.chat.dm_views import *
from origin.views.chat.dm_delta_views import (
    DMChatsListView,
    DMMessagesDeltaView,
    DMThreadMessagesDeltaView,
)
from origin.views.chat.gm_views import *
from origin.views.chat.gm_delta_views import (
    GMChatsListView,
    GMMessagesDeltaView,
    GMThreadMessagesDeltaView,
)
from origin.views.chat.mdm_views import *
from origin.views.chat.mdm_delta_views import (
    MDMChatsListView,
    MDMMessagesDeltaView,
    MDMThreadMessagesDeltaView,
)
from origin.views.chat.pm_views import *
from origin.views.chat.pm_delta_views import (
    PMChatsListView,
    PMMessagesDeltaView,
    PMThreadMessagesDeltaView,
)

# DMHistoryView / GMHistoryView / PMHistoryView were removed by the
# Phase 5 cleanup — the frontend's bulk history loader uses the Phase 2
# {chats, messagesDelta, threadMessagesDelta} split now. MDMHistoryView
# stays because the frontend still uses its single-MDM mode (?mdm_id=X)
# from useChatRouting / useChatListItem / moveToChat / createMDMChatGroup
# / addMembersToChat / ModalChatView.
from origin.views.chat.search_views import *
from origin.views.chat.reaction_views import *
from origin.views.chat.activity_views import *
from origin.views.chat.mention_views import *
from origin.views.chat.read_status_views import *
from origin.views.chat.chat_attachment_views import *
from origin.views.chat.todo_views import *
from origin.views.chat.chat_master_views import *

urlpatterns = [
    # DM urls
    path("api/v2/dm/create/", DMMasterView.as_view(), name="create_dm"),
    path("api/v2/dm/checkExistence/", CheckDMExistsView.as_view(), name="check_dm_existence"),
    path("api/v2/dm/id/", DMIdView.as_view(), name="get_dm_id"),
    path("api/v2/dm/ids/", AllDMIdsView.as_view(), name="get_all_my_dm_ids"),
    # Phase 2 incremental-sync DM endpoints — replaced the bulk
    # /dm/history/ endpoint that lived here previously.
    path("api/v2/dm/chats/", DMChatsListView.as_view(), name="dm_chats_list"),
    path(
        "api/v2/dm/messagesDelta/",
        DMMessagesDeltaView.as_view(),
        name="dm_messages_delta",
    ),
    path(
        "api/v2/dm/threadMessagesDelta/",
        DMThreadMessagesDeltaView.as_view(),
        name="dm_thread_messages_delta",
    ),
    path("api/v2/dm/message/", DMSingleMessageView.as_view(), name="insert_dm_message"),
    path(
        "api/v2/dm/checkThreadExistence/",
        CheckDMThreadExistsView.as_view(),
        name="check_dm_thread_existence",
    ),
    path(
        "api/v2/dm/threadMessage/",
        DMSingleThreadMessageView.as_view(),
        name="insert_dm_thread_message",
    ),
    path(
        "api/v2/dm/threadMessagesById/",
        DMThreadMessagesByIdView.as_view(),
        name="get_dm_thread_messages_by_id",
    ),
    path(
        "api/v2/dm/threadMessagesByTaskId/",
        DMThreadMessagesByTaskIdView.as_view(),
        name="get_dm_thread_messages_by_task_id",
    ),
    # MDM urls (Multi-user Direct Message)
    path("api/v2/mdm/create/", MDMMasterView.as_view(), name="create_mdm"),
    path("api/v2/mdm/profile/", MDMMasterView.as_view(), name="get_mdm_profile"),
    path("api/v2/mdm/checkExistence/", CheckMDMExistsView.as_view(), name="check_mdm_existence"),
    path("api/v2/mdm/ids/", AllMDMIdsView.as_view(), name="get_all_my_mdm_ids"),
    path("api/v2/mdm/join/", MDMMembersView.as_view(), name="join_mdm"),
    path("api/v2/mdm/members/", MDMMembersView.as_view(), name="get_mdm_members"),
    path("api/v2/mdm/history/", MDMHistoryView.as_view(), name="get_all_my_mdm_messages"),
    # Phase 2 incremental-sync MDM endpoints.
    path("api/v2/mdm/chats/", MDMChatsListView.as_view(), name="mdm_chats_list"),
    path(
        "api/v2/mdm/messagesDelta/",
        MDMMessagesDeltaView.as_view(),
        name="mdm_messages_delta",
    ),
    path(
        "api/v2/mdm/threadMessagesDelta/",
        MDMThreadMessagesDeltaView.as_view(),
        name="mdm_thread_messages_delta",
    ),
    path("api/v2/mdm/message/", MDMSingleMessageView.as_view(), name="insert_mdm_message"),
    path(
        "api/v2/mdm/checkThreadExistence/",
        CheckMDMThreadExistsView.as_view(),
        name="check_mdm_thread_existence",
    ),
    path(
        "api/v2/mdm/threadMessage/",
        MDMSingleThreadMessageView.as_view(),
        name="insert_mdm_thread_message",
    ),
    path(
        "api/v2/mdm/threadMessagesById/",
        MDMThreadMessagesByIdView.as_view(),
        name="get_mdm_thread_messages_by_id",
    ),
    # GM urls
    path("api/v2/gm/create/", GMMasterView.as_view(), name="create_gm"),
    path("api/v2/gm/profile/", GMMasterView.as_view(), name="get_gm_profile"),
    path("api/v2/gm/profile/image/", GMProfileImageView.as_view(), name="update_gm_profile_image"),
    path("api/v2/gm/checkExistence/", CheckGMExistsView.as_view(), name="check_gm_existence"),
    path("api/v2/gm/id/", GMIdView.as_view(), name="get_gm_id"),
    path("api/v2/gm/ids/", AllGMIdsView.as_view(), name="get_all_my_gm_ids"),
    path("api/v2/gm/join/", GMMembersView.as_view(), name="join_gm"),
    path("api/v2/gm/join/fromInbox/", JoinGMFromInboxView.as_view(), name="join_gm_from_inbox"),
    # Phase 2 incremental-sync GM endpoints — replaced the bulk
    # /gm/history/ endpoint.
    path("api/v2/gm/chats/", GMChatsListView.as_view(), name="gm_chats_list"),
    path(
        "api/v2/gm/messagesDelta/",
        GMMessagesDeltaView.as_view(),
        name="gm_messages_delta",
    ),
    path(
        "api/v2/gm/threadMessagesDelta/",
        GMThreadMessagesDeltaView.as_view(),
        name="gm_thread_messages_delta",
    ),
    path("api/v2/gm/message/", GMSingleMessageView.as_view(), name="insert_gm_message"),
    path(
        "api/v2/gm/checkThreadExistence/",
        CheckGMThreadExistsView.as_view(),
        name="check_gm_thread_existence",
    ),
    path(
        "api/v2/gm/threadMessage/",
        GMSingleThreadMessageView.as_view(),
        name="insert_gm_thread_message",
    ),
    path(
        "api/v2/gm/threadMessagesById/",
        GMThreadMessagesByIdView.as_view(),
        name="get_gm_thread_messages_by_id",
    ),
    path(
        "api/v2/gm/threadMessagesByTaskId/",
        GMThreadMessagesByTaskIdView.as_view(),
        name="get_gm_thread_messages_by_task_id",
    ),
    # PM urls
    # Phase 2 incremental-sync PM endpoints — replaced the bulk
    # /pm/history/ endpoint.
    path("api/v2/pm/chats/", PMChatsListView.as_view(), name="pm_chats_list"),
    path(
        "api/v2/pm/messagesDelta/",
        PMMessagesDeltaView.as_view(),
        name="pm_messages_delta",
    ),
    path(
        "api/v2/pm/threadMessagesDelta/",
        PMThreadMessagesDeltaView.as_view(),
        name="pm_thread_messages_delta",
    ),
    path("api/v2/pm/message/", PMSingleMessageView.as_view(), name="pm_message"),
    path(
        "api/v2/pm/threadMessage/",
        PMSingleThreadMessageView.as_view(),
        name="pm_single_thread_message",
    ),
    path(
        "api/v2/pm/checkThreadExistence/",
        CheckPMThreadExistsView.as_view(),
        name="check_pm_thread_existence",
    ),
    path(
        "api/v2/pm/threadMessagesById/",
        PMThreadMessagesByIdView.as_view(),
        name="get_pm_thread_messages_by_id",
    ),
    path(
        "api/v2/pm/threadMessagesByTaskId/",
        PMThreadMessagesByTaskIdView.as_view(),
        name="get_pm_thread_messages_by_task_id",
    ),
    # Search
    path(
        "api/v2/search/teamMembersAndGroups/",
        GetTeamMembersAndGroupsView.as_view(),
        name="search_team_members_and_groups",
    ),
    # Reaction
    path("api/v2/chat/reaction/", ChatReactionView.as_view(), name="chat_reaction"),
    # Activity
    path("api/v2/chat/activity/history/", ActivityHistoryView.as_view(), name="chat_activity"),
    path("api/v2/chat/activity/", ActivityView.as_view(), name="chat_activity"),
    # Mention
    path("api/v2/chat/mention/", ChatMentionView.as_view(), name="chat_mention"),
    # Read status
    path("api/v2/chat/read/", ReadStatusView.as_view(), name="read_status"),
    path(
        "api/v2/chat/activity/read/", ActivityReadStatusView.as_view(), name="activity_read_status"
    ),
    path(
        "api/v2/chat/activity/read/all/",
        MarkAllActivityAsReadView.as_view(),
        name="mark_all_activity_as_read",
    ),
    # Chat attachment
    path("api/v2/chat/attachment/", ChatAttachmentView.as_view(), name="chat_attachment"),
    # To-Do
    path("api/v2/todo/", ToDoFactView.as_view(), name="chat_todo"),
    # Chat master
    path("api/v2/chat/master/", UserChatMasterView.as_view(), name="chat_master"),
    # Flag message
    path("api/v2/chat/flaggedMessages/", FlagMessageView.as_view(), name="flagged_messages"),
]
