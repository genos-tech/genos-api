from django.urls import path

from origin.views.common.auth_views import *
from origin.views.common.billing_views import (
    BillingCheckoutView,
    BillingConfigView,
    BillingPlansView,
    BillingPortalView,
    BillingRefreshView,
    BillingSubscriptionView,
    StripeWebhookView,
    TeamBillingCheckoutView,
    TeamBillingConfigView,
    TeamBillingPortalView,
)
from origin.views.common.calendar_views import (
    CalendarEventDetailView,
    CalendarEventsView,
    CalendarListView,
)
from origin.views.common.gif_views import GifSearchView
from origin.views.common.github_views import (
    GithubBranchesForTaskView,
    GithubMyPullsView,
    GithubPullDetailView,
    GithubPullsForTasksView,
    GithubPullsForTaskView,
    GithubWebhookView,
)
from origin.views.common.health_views import HealthView
from origin.views.common.inbox_views import *
from origin.views.common.mention_group_views import (
    MentionGroupMembersView,
    MentionGroupResolveView,
    MentionGroupView,
)
from origin.views.common.notification_views import (
    NotificationPreferenceView,
    PresenceHeartbeatView,
    PushSubscriptionView,
)
from origin.views.common.oauth_views import (
    IntegrationsDisconnectView,
    IntegrationsListView,
    OAuthCallbackView,
    OAuthInitiateView,
)
from origin.views.common.runtime_config_views import RuntimeConfigView
from origin.views.common.team_emoji_views import TeamEmojiView
from origin.views.common.team_views import *
from origin.views.common.user_views import *
from origin.views.utils.extract_page_title_view import get_page_title

user_list = UserViewSet.as_view({"post": "create"})

urlpatterns = [
    # Deploy-readiness probe (Railway healthcheckPath). Unauthenticated.
    path("api/v2/health/", HealthView.as_view(), name="health"),
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
    # Billing — Stripe self-serve subscriptions (tier system).
    path("api/v2/billing/config/", BillingConfigView.as_view(), name="billing_config"),
    path("api/v2/billing/plans/", BillingPlansView.as_view(), name="billing_plans"),
    path("api/v2/billing/checkout/", BillingCheckoutView.as_view(), name="billing_checkout"),
    path("api/v2/billing/portal/", BillingPortalView.as_view(), name="billing_portal"),
    path("api/v2/billing/refresh/", BillingRefreshView.as_view(), name="billing_refresh"),
    path(
        "api/v2/billing/subscription/",
        BillingSubscriptionView.as_view(),
        name="billing_subscription",
    ),
    path(
        "api/v2/billing/team/config/",
        TeamBillingConfigView.as_view(),
        name="team_billing_config",
    ),
    path(
        "api/v2/billing/team/checkout/",
        TeamBillingCheckoutView.as_view(),
        name="team_billing_checkout",
    ),
    path(
        "api/v2/billing/team/portal/",
        TeamBillingPortalView.as_view(),
        name="team_billing_portal",
    ),
    path(
        "api/v2/billing/stripe/webhook/",
        StripeWebhookView.as_view(),
        name="stripe_webhook",
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
    # Batched variant: one request resolves the PR column for a whole
    # table paint (?task_ids=1,2,3) instead of one request per row.
    path(
        "api/v2/github/pulls/for-tasks/",
        GithubPullsForTasksView.as_view(),
        name="github_pulls_for_tasks",
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
        "api/v2/user/push-subscriptions/",
        PushSubscriptionView.as_view(),
        name="user_push_subscriptions",
    ),
    path(
        "api/v2/user/presence/heartbeat/",
        PresenceHeartbeatView.as_view(),
        name="user_presence_heartbeat",
    ),
    # Runtime config — per-chat-type rollout flags + panic switch.
    # Polled by the client every 60s. Source of truth for whether a
    # given user falls into the v3-chat canary bucket.
    path(
        "api/v2/runtime-config/",
        RuntimeConfigView.as_view(),
        name="runtime_config",
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
        "api/v2/user/preferences/spotlight-web-search/",
        SpotlightWebSearchPreferenceView.as_view(),
        name="spotlight_web_search_preference",
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
    path("api/v2/team/profile/", TeamMasterView.as_view(), name="team_profile"),
    path("api/v2/team/exist/", CheckTeamExistsView.as_view(), name="exist_team"),
    path("api/v2/team/join/", TeamMembersView.as_view(), name="exist_team"),
    path("api/v2/team/leave/", LeaveTeamView.as_view(), name="leave_team"),
    path(
        "api/v2/team/join/fromInbox/",
        JoinTeamFromInboxView.as_view(),
        name="join_team_from_inbox",
    ),
    # Invite by email (owner-only send; public preview; authed accept).
    path("api/v2/team/invite/", InviteTeamMembersView.as_view(), name="team_invite"),
    path(
        "api/v2/team/invite/preview/",
        InvitePreviewView.as_view(),
        name="team_invite_preview",
    ),
    path(
        "api/v2/team/invite/accept/",
        InviteAcceptView.as_view(),
        name="team_invite_accept",
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
    # GIF search proxy (GIPHY key stays server-side).
    path("api/v2/gif/search/", GifSearchView.as_view(), name="gif_search"),
    # Team custom emoji (Slack-style :name: images, incl. animated GIFs).
    path("api/v2/team-emoji/", TeamEmojiView.as_view(), name="team_emoji"),
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
    path(
        "api/v2/gm/join/fromInbox/",
        JoinGMFromInboxView.as_view(),
        name="join_gm_from_inbox",
    ),
    path(
        "api/v2/inbox/noteAccessRequest/",
        InboxItemForNoteAccessRequestView.as_view(),
        name="inbox_note_access_request_item",
    ),
    path(
        "api/v2/note/role/fromInbox/",
        GrantNoteAccessFromInboxView.as_view(),
        name="grant_note_access_from_inbox",
    ),
    path("api/v2/getPageTitle/", get_page_title, name="get_page_title"),
]
