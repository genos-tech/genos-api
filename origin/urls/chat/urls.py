from django.urls import path
from origin.views.chat.dm_views import *
from origin.views.chat.gm_views import *
from origin.views.chat.search_views import GetTeamMembersAndGroupsView

urlpatterns = [
    # DM urls
    path("api/v2/dm/create/", DMMasterView.as_view(), name="create_dm"),
    path("api/v2/dm/checkExistence/", CheckDMExistsView.as_view(), name="check_dm_existence"),
    path("api/v2/dm/getDMId/", GetDMIdView.as_view(), name="get_dm_id"),
    path("api/v2/dm/getAllMyDMIds/", GetAllMyDMIdsView.as_view(), name="get_all_my_dm_ids"),
    path("api/v2/dm/getHistory/", DMAllMyMessagesView.as_view(), name="get_all_my_dm_messages"),
    path("api/v2/dm/addMessage/", DMSingleMessageView.as_view(), name="insert_dm_message"),
    path(
        "api/v2/dm/getMessagesById/",
        DMMessagesByIdView.as_view(),
        name="get_dm_messages_by_id",
    ),
    path(
        "api/v2/dm/checkThreadExistence/",
        CheckDMThreadExistsView.as_view(),
        name="check_dm_thread_existence",
    ),
    path(
        "api/v2/dm/addThreadMessage/",
        DMSingleThreadMessageView.as_view(),
        name="insert_dm_thread_message",
    ),
    path(
        "api/v2/dm/getThreadMessagesById/",
        DMThreadMessagesByIdView.as_view(),
        name="get_dm_thread_messages_by_id",
    ),
    # GM urls
    path("api/v2/gm/create/", GMMasterView.as_view(), name="create_gm"),
    path("api/v2/gm/checkExistence/", CheckGMExistsView.as_view(), name="check_gm_existence"),
    path("api/v2/gm/getGMId/", GetGMIdView.as_view(), name="get_gm_id"),
    path("api/v2/gm/join/", GMMembersView.as_view(), name="join_gm"),
    path("api/v2/gm/getAllMyGMIds/", GetAllMyGMIdsView.as_view(), name="get_all_my_gm_ids"),
    path("api/v2/gm/getHistory/", GMAllMyMessagesView.as_view(), name="get_all_my_gm_messages"),
    path("api/v2/gm/addMessage/", GMSingleMessageView.as_view(), name="insert_gm_message"),
    path(
        "api/v2/gm/getMessagesById/",
        GMMessagesByIdView.as_view(),
        name="get_gm_messages_by_id",
    ),
    path(
        "api/v2/gm/checkThreadExistence/",
        CheckGMThreadExistsView.as_view(),
        name="check_gm_thread_existence",
    ),
    path(
        "api/v2/gm/addThreadMessage/",
        GMSingleThreadMessageView.as_view(),
        name="insert_gm_thread_message",
    ),
    path(
        "api/v2/gm/getThreadMessagesById/",
        GMThreadMessagesByIdView.as_view(),
        name="get_gm_thread_messages_by_id",
    ),
    # Search
    path(
        "api/v2/search/teamMembersAndGroups/",
        GetTeamMembersAndGroupsView.as_view(),
        name="search_team_members_and_groups",
    ),
]
