from django.urls import path
from origin.views.chat.dm_views import *
from origin.views.chat.gm_views import *
from origin.views.chat.pm_views import *
from origin.views.chat.search_views import GetTeamMembersAndGroupsView

urlpatterns = [
    # DM urls
    path("api/v2/dm/create/", DMMasterView.as_view(), name="create_dm"),
    path("api/v2/dm/checkExistence/", CheckDMExistsView.as_view(), name="check_dm_existence"),
    path("api/v2/dm/id/", DMIdView.as_view(), name="get_dm_id"),
    path("api/v2/dm/ids/", AllDMIdsView.as_view(), name="get_all_my_dm_ids"),
    path("api/v2/dm/history/", DMHistoryView.as_view(), name="get_all_my_dm_messages"),
    path("api/v2/dm/message/", DMSingleMessageView.as_view(), name="insert_dm_message"),
    path(
        "api/v2/dm/messagesById/",
        DMMessagesByIdView.as_view(),
        name="get_dm_messages_by_id",
    ),
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
    # GM urls
    path("api/v2/gm/create/", GMMasterView.as_view(), name="create_gm"),
    path("api/v2/gm/checkExistence/", CheckGMExistsView.as_view(), name="check_gm_existence"),
    path("api/v2/gm/id/", GMIdView.as_view(), name="get_gm_id"),
    path("api/v2/gm/ids/", AllGMIdsView.as_view(), name="get_all_my_gm_ids"),
    path("api/v2/gm/join/", GMMembersView.as_view(), name="join_gm"),
    path("api/v2/gm/history/", GMHistoryView.as_view(), name="get_all_my_gm_messages"),
    path("api/v2/gm/message/", GMSingleMessageView.as_view(), name="insert_gm_message"),
    path(
        "api/v2/gm/messagesById/",
        GMMessagesByIdView.as_view(),
        name="get_gm_messages_by_id",
    ),
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
    # PM urls
    path("api/v2/pm/history/", PMHistoryView.as_view(), name="pm_history"),
    path("api/v2/pm/singleMessage/", PMSingleMessageView.as_view(), name="pm_message"),
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
    # Search
    path(
        "api/v2/search/teamMembersAndGroups/",
        GetTeamMembersAndGroupsView.as_view(),
        name="search_team_members_and_groups",
    ),
]
