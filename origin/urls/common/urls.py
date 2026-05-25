from django.urls import path

from origin.views.common.auth_views import *
from origin.views.common.user_views import *
from origin.views.common.team_views import *
from origin.views.common.inbox_views import *
from origin.views.common.mention_group_views import (
    MentionGroupView,
    MentionGroupMembersView,
    MentionGroupResolveView,
)
from origin.views.common.notification_views import NotificationPreferenceView
from origin.views.common.oauth_views import (
    IntegrationsDisconnectView,
    IntegrationsListView,
    OAuthCallbackView,
    OAuthInitiateView,
)
from origin.views.common.calendar_views import (
    CalendarEventDetailView,
    CalendarEventsView,
    CalendarListView,
)
from origin.views.common.github_views import (
    GithubBranchesForTaskView,
    GithubMyPullsView,
    GithubPullDetailView,
    GithubPullsForTaskView,
    GithubWebhookView,
)
from origin.views.chat.reaction_views import *
from origin.views.utils.extract_page_title_view import get_page_title

user_list = UserViewSet.as_view({"post": "create"})

urlpatterns = [
    # User
    path("api/v2/user/signup/", user_list, name="signup"),
    path("api/v2/user/signin/", CustomTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("api/v2/user/signin/refresh/", CookieTokenRefreshView.as_view(), name="token_refresh"),
    path("api/v2/user/demo/", DemoSignInView.as_view(), name="demo_signin"),
    path("api/v2/user/signout/", LogoutView.as_view(), name="signout"),
    path(
        "api/v2/user/password-reset/request/",
        PasswordResetRequestView.as_view(),
        name="password_reset_request",
    ),
    path(
        "api/v2/user/password-reset/confirm/",
        PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "api/v2/user/verify-email/",
        VerifyEmailView.as_view(),
        name="verify_email",
    ),
    path(
        "api/v2/user/verify-email/resend/",
        ResendVerificationView.as_view(),
        name="resend_verification",
    ),
    # OAuth + Integrations
    path(
        "api/v2/oauth/<str:provider_name>/initiate/",
        OAuthInitiateView.as_view(),
        name="oauth_initiate",
    ),
    path(
        "api/v2/oauth/<str:provider_name>/callback/",
        OAuthCallbackView.as_view(),
        name="oauth_callback",
    ),
    path(
        "api/v2/integrations/me/",
        IntegrationsListView.as_view(),
        name="integrations_list",
    ),
    path(
        "api/v2/integrations/<str:provider_name>/",
        IntegrationsDisconnectView.as_view(),
        name="integrations_disconnect",
    ),
    # Google Calendar
    path("api/v2/calendar/list/", CalendarListView.as_view(), name="calendar_list"),
    path(
        "api/v2/calendar/events/",
        CalendarEventsView.as_view(),
        name="calendar_events",
    ),
    path(
        "api/v2/calendar/events/<str:event_id>/",
        CalendarEventDetailView.as_view(),
        name="calendar_event_detail",
    ),
    # GitHub PRs (read-only)
    path("api/v2/github/pulls/", GithubMyPullsView.as_view(), name="github_pulls"),
    path(
        "api/v2/github/pulls/<str:owner>/<str:repo>/<str:number>/",
        GithubPullDetailView.as_view(),
        name="github_pull_detail",
    ),
    # Inbound webhook receiver — auto-status sync on PR merge.
    path(
        "api/v2/github/webhook/",
        GithubWebhookView.as_view(),
        name="github_webhook",
    ),
    # Branches matching a task's display ID (e.g. branches containing "GEN-42").
    path(
        "api/v2/github/branches/for-task/",
        GithubBranchesForTaskView.as_view(),
        name="github_branches_for_task",
    ),
    # PRs whose head branches match a task's display ID — drives the
    # task table's PR column.
    path(
        "api/v2/github/pulls/for-task/",
        GithubPullsForTaskView.as_view(),
        name="github_pulls_for_task",
    ),
    path("api/v2/user/profile/", UserProfileView.as_view(), name="update_status"),
    path("api/v2/user/me/", UserInfoView.as_view(), name="user_me"),
    path(
        "api/v2/user/profile/image/",
        UserProfileImageView.as_view(),
        name="update_user_profile_image",
    ),
    path(
        "api/v2/user/notification-preferences/",
        NotificationPreferenceView.as_view(),
        name="user_notification_preferences",
    ),
    path(
        "api/v2/user/preferences/auto-close-on-pr-merge/",
        AutoCloseOnPrMergePreferenceView.as_view(),
        name="auto_close_on_pr_merge_preference",
    ),
    path(
        "api/v2/user/preferences/auto-sync-tasks-to-calendar/",
        AutoSyncTasksToCalendarPreferenceView.as_view(),
        name="auto_sync_tasks_to_calendar_preference",
    ),
    path(
        "api/v2/user/preferences/llm-model/",
        LlmModelPreferenceView.as_view(),
        name="llm_model_preference",
    ),
    path(
        "api/v2/user/calendar-sync/backfill/",
        CalendarSyncBackfillView.as_view(),
        name="calendar_sync_backfill",
    ),
    # Team
    path("api/v2/team/create/", TeamMasterView.as_view(), name="join_team"),
    path("api/v2/team/exist/", CheckTeamExistsView.as_view(), name="exist_team"),
    path("api/v2/team/join/", TeamMembersView.as_view(), name="exist_team"),
    path(
        "api/v2/team/join/fromInbox/",
        JoinTeamFromInboxView.as_view(),
        name="join_team_from_inbox",
    ),
    path(
        "api/v2/team/profile/image/",
        TeamProfileImageView.as_view(),
        name="update_team_profile_image",
    ),
    path("api/v2/team/getMyTeams/", GetMyTeamsView.as_view(), name="get_my_team"),
    path("api/v2/team/getTeamMembers/", GetTeamMembersView.as_view(), name="get_team_members"),
    path(
        "api/v2/team/getTeamMemberInfo/",
        GetTeamMemberInfoView.as_view(),
        name="get_team_member_info",
    ),
    # Mention groups (Slack-style @group). team-scoped CRUD + bulk
    # group→user resolver used by the Flask mention pipeline.
    path("api/v2/mention-group/", MentionGroupView.as_view(), name="mention_group"),
    path(
        "api/v2/mention-group/members/",
        MentionGroupMembersView.as_view(),
        name="mention_group_members",
    ),
    path(
        "api/v2/mention-group/resolve/",
        MentionGroupResolveView.as_view(),
        name="mention_group_resolve",
    ),
    # Inbox
    path("api/v2/inbox/", InboxItemView.as_view(), name="inbox_item"),
    path(
        "api/v2/inbox/joinTeamRequest/",
        InboxItemForJoinTeamRequestView.as_view(),
        name="inbox_join_team_request_item",
    ),
    path(
        "api/v2/inbox/joinProjectRequest/",
        InboxItemForJoinProjectRequestView.as_view(),
        name="inbox_join_project_request_item",
    ),
    path(
        "api/v2/inbox/joinGMRequest/",
        InboxItemForJoinGMRequestView.as_view(),
        name="inbox_join_gm_request_item",
    ),
    path("api/v2/getPageTitle/", get_page_title, name="get_page_title"),
]
