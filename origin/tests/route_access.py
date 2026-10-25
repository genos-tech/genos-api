"""Access declaration for every routed DRF view (MIP-716).

`test_route_access` walks the URL resolver and fails on any routed view
missing from `REGISTRY`, so adding a route means adding a line here, in
the same PR, saying which of four kinds of check it has:

``public(reason)``
    Reachable without signing in. Must match the view's own
    `permission_classes` (an `AllowAny`, or none). The reason says what
    stops abuse — a signature, a single-use token, a throttle.

``signed_in(reason)``
    Any signed-in user, and nothing more is needed because the view
    touches no tenant data: a proxy to a third-party search, the build's
    feature flags.

``self_only(reason)``
    The caller's own rows only — their preferences, their to-dos, their
    keys. Every handler must refer to the caller (`request.user`), which
    is what scoping to them looks like; a handler that never does is a
    forgotten check.

``checked(*helpers)``
    Reads or changes something other people can see, so it goes through
    a membership, role or ownership check. Name the helpers. Every one
    named must be reachable from the view's handlers, and every handler
    must reach one of them (or, for a handler acting only on the caller's
    own rows beside the guarded ones, refer to the caller).

Name the *specific* helper (`_get_channel_for_user`, `can_access_task`)
rather than the most general one it calls, so that a reader of this file
can find the check.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Public:
    reason: str


@dataclass(frozen=True)
class SignedIn:
    reason: str


@dataclass(frozen=True)
class SelfOnly:
    reason: str


@dataclass(frozen=True)
class Checked:
    guards: tuple[str, ...]


def public(reason: str) -> Public:
    return Public(reason)


def signed_in(reason: str) -> SignedIn:
    return SignedIn(reason)


def self_only(reason: str) -> SelfOnly:
    return SelfOnly(reason)


def checked(*guards: str) -> Checked:
    return Checked(tuple(guards))


# Reasons shared by many rows.
_PREF = "the caller's own preference, read and written on request.user"
_OWN_ACTIVITY = "the caller's own activity feed (recipient=request.user)"
_OWN_TODO = "the caller's own to-do rows (user=request.user)"
_OWN_BILLING = "the caller's own billing account and Stripe session"
_OWN_AGENT = "the caller's own agent runs and sessions (user_id=request.user.id)"
_INBOX = "acts on an inbox item addressed to the caller (receiver=request.user)"


REGISTRY = {
    # search_engine.agent_views
    "origin.search_engine.agent_views.AgentAskView": checked("participates_in_team"),
    "origin.search_engine.agent_views.AgentDecideView": self_only(
        "approves a paused run; the run's user_id must equal request.user.id"
    ),
    "origin.search_engine.agent_views.AgentFeaturesView": self_only(
        "feature flags resolved for request.user"
    ),
    "origin.search_engine.agent_views.AgentModelsView": signed_in(
        "the model catalogue; the per-user choice comes from the caller's own preference"
    ),
    "origin.search_engine.agent_views.AgentRunFeedbackView": self_only(_OWN_AGENT),
    "origin.search_engine.agent_views.AgentSessionDetailView": self_only(_OWN_AGENT),
    "origin.search_engine.agent_views.AgentSessionPinView": self_only(_OWN_AGENT),
    "origin.search_engine.agent_views.AgentSessionsListView": self_only(_OWN_AGENT),
    "origin.search_engine.agent_views.AgentUsageView": self_only(
        "the caller's own usage and quota"
    ),
    "origin.search_engine.agent_views.NoteSummaryView": checked("fetch_note_for_agent"),
    "origin.search_engine.agent_views.ThreadSummaryView": checked(
        "fetch_thread_messages_for_agent"
    ),
    # search_engine.views
    "origin.search_engine.views.SearchView": checked("is_team_member"),
    # views.chat.activity_views_v3
    "origin.views.chat.activity_views_v3.ActivityCommentSurfaceView": checked("is_team_member"),
    "origin.views.chat.activity_views_v3.ActivityListView": self_only(_OWN_ACTIVITY),
    "origin.views.chat.activity_views_v3.ActivityReadAllView": self_only(_OWN_ACTIVITY),
    "origin.views.chat.activity_views_v3.ActivityReadBatchView": self_only(_OWN_ACTIVITY),
    "origin.views.chat.activity_views_v3.ActivityReadView": self_only(_OWN_ACTIVITY),
    "origin.views.chat.activity_views_v3.ActivitySurfaceView": checked("is_team_member"),
    # views.chat.channel_views
    "origin.views.chat.channel_views.ChannelDetailView": checked("_get_channel_for_user"),
    "origin.views.chat.channel_views.ChannelInlineUploadView": checked("_get_channel_for_user"),
    "origin.views.chat.channel_views.ChannelJoinView": checked("_verify_team_member"),
    "origin.views.chat.channel_views.ChannelListView": checked(
        "external_objects_for_member", "_manager_or_raise"
    ),
    "origin.views.chat.channel_views.ChannelMemberDetailView": checked("_get_channel_for_user"),
    "origin.views.chat.channel_views.ChannelMembersView": checked("_get_channel_for_user"),
    "origin.views.chat.channel_views.ChannelProfileImageView": checked("_get_channel_for_user"),
    "origin.views.chat.channel_views.ChannelSharesView": checked("_get_channel_for_user"),
    # views.chat.message_views
    "origin.views.chat.message_views.MessageAttachmentsView": checked("ChannelMember"),
    "origin.views.chat.message_views.MessageDetailView": checked("_fetch_for_user"),
    "origin.views.chat.message_views.MessagesDeltaView": checked("_verify_member_or_404"),
    "origin.views.chat.message_views.TaskCardMessageView": checked("ChannelMember"),
    "origin.views.chat.message_views.ThreadMessagesDeltaView": checked("_verify_member_or_404"),
    # views.chat.personal_tag_views
    "origin.views.chat.personal_tag_views.ChannelPersonalTagsView": checked(
        "_get_channel_for_user"
    ),
    "origin.views.chat.personal_tag_views.PersonalTagDetailView": checked("_get_owned"),
    "origin.views.chat.personal_tag_views.PersonalTagListView": self_only("the caller's own tags"),
    # views.chat.pin_flag_views
    "origin.views.chat.pin_flag_views.FlagListView": self_only("the caller's own flags"),
    "origin.views.chat.pin_flag_views.FlagView": checked("_verify_message_member"),
    "origin.views.chat.pin_flag_views.MessageReminderListView": self_only(
        "the caller's own reminders"
    ),
    "origin.views.chat.pin_flag_views.MessageReminderView": checked("_verify_message_member"),
    "origin.views.chat.pin_flag_views.PinListView": self_only(
        "pins in channels the caller is a member of"
    ),
    "origin.views.chat.pin_flag_views.PinView": checked("_verify_member_or_404"),
    # views.chat.reaction_views_v3
    "origin.views.chat.reaction_views_v3.MessageReactionsView": checked("_verify_message_member"),
    # views.chat.read_cursor_views
    "origin.views.chat.read_cursor_views.ReadCursorView": checked("_verify_member_or_404"),
    # views.chat.search_views_v3
    "origin.views.chat.search_views_v3.SearchTeamMembersAndGroupsView": checked(
        "_verify_team_member"
    ),
    # views.chat.todo_views
    "origin.views.chat.todo_views.ToDoCategoryDetailView": checked("_get_owned"),
    "origin.views.chat.todo_views.ToDoCategoryListView": self_only(_OWN_TODO),
    "origin.views.chat.todo_views.ToDoGroupListView": self_only(_OWN_TODO),
    "origin.views.chat.todo_views.ToDoItemDetailView": checked("_get_owned_item"),
    "origin.views.chat.todo_views.ToDoItemListView": self_only(_OWN_TODO),
    "origin.views.chat.todo_views.ToDoItemReminderListView": self_only(_OWN_TODO),
    "origin.views.chat.todo_views.ToDoItemReminderView": checked("_get_owned_item"),
    "origin.views.chat.todo_views.ToDoScheduleDetailView": checked("_get_owned"),
    "origin.views.chat.todo_views.ToDoScheduleListView": self_only(_OWN_TODO),
    # views.common.api_key_views
    "origin.views.common.api_key_views.ApiKeyDetailView": self_only(
        "ApiKey filtered on user=request.user"
    ),
    "origin.views.common.api_key_views.ApiKeyListCreateView": checked("is_team_member"),
    "origin.views.common.api_key_views.TeamApiKeyDetailView": checked("_owned_team"),
    "origin.views.common.api_key_views.TeamApiKeyListView": checked("_owned_team"),
    # views.common.auth_views
    "origin.views.common.auth_views.CookieTokenRefreshView": public(
        "exchanges the HttpOnly refresh cookie for a new access token; the cookie is the credential"
    ),
    "origin.views.common.auth_views.CustomTokenObtainPairView": public(
        "sign-in; SignInBurstThrottle + SignInSustainedThrottle per IP"
    ),
    "origin.views.common.auth_views.DemoSignInView": public(
        "creates a throwaway demo tenant; DemoSignInThrottle per IP"
    ),
    "origin.views.common.auth_views.LogoutView": public(
        "clears the caller's own auth cookies; must work with an expired access token"
    ),
    "origin.views.common.auth_views.PasswordResetConfirmView": public(
        "single-use 256-bit reset token, hashed and short-lived; PasswordResetConfirmThrottle"
    ),
    "origin.views.common.auth_views.PasswordResetRequestView": public(
        "answers 200 whether or not the email exists; PasswordResetRequestThrottle"
    ),
    "origin.views.common.auth_views.ResendVerificationView": public(
        "enumeration-safe 200; ResendVerificationThrottle"
    ),
    "origin.views.common.auth_views.UserInfoView": self_only("the caller's own profile"),
    "origin.views.common.auth_views.UserViewSet": public("signup, create-only; SignUpThrottle"),
    "origin.views.common.auth_views.VerifyEmailView": public(
        "single-use signed verification token"
    ),
    # views.common.billing_views
    "origin.views.common.billing_views.BillingCheckoutView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingConfigView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingCreditPackCheckoutView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingCreditPacksView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingPlansView": public(
        "the static price list shown on the marketing page"
    ),
    "origin.views.common.billing_views.BillingPortalView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingRefreshView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.BillingSubscriptionView": self_only(_OWN_BILLING),
    "origin.views.common.billing_views.StripeWebhookView": public(
        "Stripe signature verified against STRIPE_WEBHOOK_SECRET before anything is read"
    ),
    "origin.views.common.billing_views.TeamBillingCheckoutView": checked("_owned_team_or_error"),
    "origin.views.common.billing_views.TeamBillingConfigView": self_only(
        "teams filtered on owner=request.user"
    ),
    "origin.views.common.billing_views.TeamBillingPortalView": checked("_owned_team_or_error"),
    # views.common.calendar_views
    "origin.views.common.calendar_views.CalendarEventDetailView": self_only(
        "the caller's own calendars, through their own OAuth token"
    ),
    "origin.views.common.calendar_views.CalendarEventsAggregateView": self_only(
        "the caller's own calendars, through their own OAuth token"
    ),
    "origin.views.common.calendar_views.CalendarEventsView": self_only(
        "the caller's own calendars, through their own OAuth token"
    ),
    "origin.views.common.calendar_views.CalendarListView": self_only(
        "the caller's own calendars, through their own OAuth token"
    ),
    # views.common.gif_views
    "origin.views.common.gif_views.GifSearchView": signed_in(
        "proxies a GIPHY search; no workspace data"
    ),
    # views.common.github_views
    "origin.views.common.github_views.GithubAccessibleReposView": self_only(
        "repos visible to the caller's own GitHub token"
    ),
    "origin.views.common.github_views.GithubBranchesForTaskView": checked("can_access_task"),
    "origin.views.common.github_views.GithubMyPullsView": self_only(
        "pull requests visible to the caller's own GitHub token"
    ),
    "origin.views.common.github_views.GithubPullDetailView": self_only(
        "fetched with the caller's own GitHub token, so GitHub enforces repo access"
    ),
    "origin.views.common.github_views.GithubPullsForTaskView": checked("can_access_task"),
    "origin.views.common.github_views.GithubPullsForTasksView": checked("can_access_task"),
    "origin.views.common.github_views.GithubWebhookView": public(
        "HMAC signature verified against the repo's webhook secret"
    ),
    # views.common.health_views
    "origin.views.common.health_views.AlertingHealthView": public(
        "outside probe of the alert cron; 404 without the OPS_HEALTH_TOKEN bearer, "
        "and only a status, an age and a count with it"
    ),
    "origin.views.common.health_views.HealthView": public(
        "liveness probe for the platform; returns no data"
    ),
    # views.common.inbox_views
    "origin.views.common.inbox_views.GrantNoteAccessFromInboxView": checked("get_explicit_role"),
    "origin.views.common.inbox_views.InboxItemForJoinGMRequestView": checked("is_team_member"),
    "origin.views.common.inbox_views.InboxItemForJoinProjectRequestView": checked("is_team_member"),
    "origin.views.common.inbox_views.InboxItemForJoinTeamRequestView": self_only(
        "files a request from the caller; the team owner approves it"
    ),
    "origin.views.common.inbox_views.InboxItemForNoteAccessRequestView": checked(
        "get_effective_role"
    ),
    "origin.views.common.inbox_views.InboxItemView": checked("is_team_member"),
    "origin.views.common.inbox_views.JoinGMFromInboxView": self_only(_INBOX),
    # views.common.mention_group_views
    "origin.views.common.mention_group_views.MentionGroupMembersView": checked("is_team_member"),
    "origin.views.common.mention_group_views.MentionGroupResolveView": self_only(
        "groups narrowed to teams the caller belongs to (TeamMembers attendee=request.user)"
    ),
    "origin.views.common.mention_group_views.MentionGroupView": checked("is_team_member"),
    # views.common.notification_views
    "origin.views.common.notification_views.NotificationPreferenceView": self_only(_PREF),
    "origin.views.common.notification_views.PresenceAudienceView": checked(
        "host_team_ids_for_user"
    ),
    "origin.views.common.notification_views.PresenceHeartbeatView": self_only(
        "marks the caller's own presence"
    ),
    "origin.views.common.notification_views.PushSubscriptionView": self_only(
        "the caller's own Web Push subscriptions"
    ),
    # views.common.oauth_views
    "origin.views.common.oauth_views.IntegrationsDisconnectAccountView": self_only(
        "ConnectedAccount filtered on user=request.user"
    ),
    "origin.views.common.oauth_views.IntegrationsDisconnectView": self_only(
        "ConnectedAccount filtered on user=request.user"
    ),
    "origin.views.common.oauth_views.IntegrationsListView": self_only(
        "the caller's own connected accounts"
    ),
    "origin.views.common.oauth_views.OAuthCallbackView": public(
        "the provider redirects here; the signed state parameter binds it to the user who started the flow"
    ),
    "origin.views.common.oauth_views.OAuthInitiateView": public(
        "starts a provider sign-in; issues the signed state the callback checks"
    ),
    # views.common.ownership_claim_views
    "origin.views.common.ownership_claim_views.TeamOwnershipClaimFinalizeView": self_only(
        "the claim's own requester or approver, checked against request.user in ownership_claim"
    ),
    "origin.views.common.ownership_claim_views.TeamOwnershipClaimRequestView": self_only(
        "files a claim by the caller"
    ),
    "origin.views.common.ownership_claim_views.TeamOwnershipClaimRespondView": self_only(
        "only the claim's addressee may answer, checked against request.user in ownership_claim"
    ),
    "origin.views.common.ownership_claim_views.TeamOwnershipClaimStatusView": self_only(
        "claims the caller filed or was asked to answer"
    ),
    # views.common.runtime_config_views
    "origin.views.common.runtime_config_views.RuntimeConfigView": signed_in(
        "the build's feature flags, the same for every user"
    ),
    # views.common.security_event_views
    "origin.views.common.security_event_views.TeamSecurityEventListView": checked("_owned_team"),
    # views.common.team_connection_views
    "origin.views.common.team_connection_views.CrossTeamNoticeView": checked("is_team_member"),
    "origin.views.common.team_connection_views.ExternalShareObjectView": checked("is_team_member"),
    "origin.views.common.team_connection_views.ExternalShareParticipantsView": checked(
        "_is_manager", "is_team_member", "_manager_or_raise"
    ),
    "origin.views.common.team_connection_views.ExternalShareRespondView": checked(
        "_manager_or_raise"
    ),
    "origin.views.common.team_connection_views.ExternalShareRevokeView": checked("_is_manager"),
    "origin.views.common.team_connection_views.ExternalShareView": checked(
        "require_team_member_or_response", "_manager_or_raise"
    ),
    "origin.views.common.team_connection_views.TeamConnectionRespondView": checked(
        "_manager_or_raise"
    ),
    "origin.views.common.team_connection_views.TeamConnectionRevokeView": checked("_is_manager"),
    "origin.views.common.team_connection_views.TeamConnectionView": checked(
        "require_team_member_or_response", "_manager_or_raise"
    ),
    # views.common.team_emoji_views
    "origin.views.common.team_emoji_views.TeamEmojiView": checked("_verify_team_member"),
    # views.common.team_views
    "origin.views.common.team_views.CheckTeamExistsView": signed_in(
        "join-by-id looks a team up before you belong to it; a non-member gets only the join"
        " card (name, contact address, image) for a UUID they already hold"
    ),
    "origin.views.common.team_views.GetMyTeamsView": checked("is_team_member"),
    "origin.views.common.team_views.GetTeamMemberInfoView": checked(
        "require_team_member_or_response"
    ),
    "origin.views.common.team_views.GetTeamMembersView": checked("is_team_member"),
    "origin.views.common.team_views.InviteAcceptView": self_only(
        "adds the caller only; accept_invite requires their email to match the invite"
    ),
    "origin.views.common.team_views.InvitePreviewView": public(
        "bearer of a 256-bit invite token, looked up by hash; InvitePreviewThrottle per IP"
    ),
    "origin.views.common.team_views.InviteTeamMembersView": checked("can_manage"),
    "origin.views.common.team_views.JoinTeamFromInboxView": self_only(_INBOX),
    "origin.views.common.team_views.LeaveTeamView": self_only(
        "removes the caller's own membership"
    ),
    "origin.views.common.team_views.TeamMasterView": checked("can_manage"),
    "origin.views.common.team_views.TeamMemberRoleView": checked("can_manage"),
    "origin.views.common.team_views.TeamMembersView": checked("is_team_member"),
    "origin.views.common.team_views.TeamProfileImageView": checked("can_manage"),
    # views.common.user_views
    "origin.views.common.user_views.AccountDeletionView": self_only(
        "deletes the caller's own account"
    ),
    "origin.views.common.user_views.AccountExportView": self_only(
        "exports the caller's own account data"
    ),
    "origin.views.common.user_views.AutoCloseOnPrMergePreferenceView": self_only(_PREF),
    "origin.views.common.user_views.AutoSyncTasksToCalendarPreferenceView": self_only(_PREF),
    "origin.views.common.user_views.CalendarSyncBackfillView": self_only(
        "syncs the caller's own tasks to their own calendar"
    ),
    "origin.views.common.user_views.DigestPreferenceView": self_only(_PREF),
    "origin.views.common.user_views.LanguagePreferenceView": self_only(_PREF),
    "origin.views.common.user_views.LlmModelPreferenceView": self_only(_PREF),
    "origin.views.common.user_views.SpotlightWebSearchPreferenceView": self_only(_PREF),
    "origin.views.common.user_views.TimezonePreferenceView": self_only(_PREF),
    "origin.views.common.user_views.UiSettingsPreferenceView": self_only(_PREF),
    "origin.views.common.user_views.UserProfileImageView": self_only("the caller's own avatar"),
    "origin.views.common.user_views.UserProfileView": self_only("the caller's own profile fields"),
    # views.common.webhook_views
    "origin.views.common.webhook_views.WebhookDetailView": checked("is_team_member"),
    "origin.views.common.webhook_views.WebhookListCreateView": checked("is_team_member"),
    # views.note.chat_note_views
    "origin.views.note.chat_note_views.AllChatNoteMetaView": checked("_accessible_chat_note_ids"),
    "origin.views.note.chat_note_views.AllChatNotesView": checked("_accessible_chat_note_ids"),
    "origin.views.note.chat_note_views.ChatNoteAttachmentView": checked("require_write_role"),
    "origin.views.note.chat_note_views.ChatNoteMasterView": checked(
        "require_write_role", "validate_request_user"
    ),
    "origin.views.note.chat_note_views.ChatNoteMoveView": checked("require_write_role"),
    "origin.views.note.chat_note_views.ChatSubNotesView": checked("get_effective_role"),
    "origin.views.note.chat_note_views.SingleChatNoteView": checked("require_read_role"),
    # views.note.collab_access_views
    "origin.views.note.collab_access_views.CollabAccessCheckView": checked("can_access_task"),
    # views.note.favorite_note_views
    "origin.views.note.favorite_note_views.AllFavoriteNotesMetaView": checked("readable_note_ids"),
    "origin.views.note.favorite_note_views.CheckNoteFavoriteView": checked("validate_request_user"),
    "origin.views.note.favorite_note_views.NoteFavoriteView": checked(
        "validate_request_user", "get_effective_role"
    ),
    # views.note.note_folder_tag_views
    "origin.views.note.note_folder_tag_views.NoteFolderTagLinkView": checked("get_folder_role"),
    "origin.views.note.note_folder_tag_views.NoteFolderTagView": checked(
        "_can_edit_vocabulary", "is_team_member"
    ),
    # views.note.note_role_views
    "origin.views.note.note_role_views.NoteRoleCheckView": checked("get_effective_role"),
    "origin.views.note.note_role_views.NoteRoleListView": checked("get_effective_role"),
    "origin.views.note.note_role_views.NoteRoleView": checked("get_explicit_role"),
    "origin.views.note.note_role_views.SharedPersonalNoteMetaView": checked(
        "validate_request_user"
    ),
    # views.note.note_version_views
    "origin.views.note.note_version_views.NoteVersionDetailView": checked("require_read_role"),
    "origin.views.note.note_version_views.NoteVersionListView": checked("require_read_role"),
    "origin.views.note.note_version_views.NoteVersionRestoreView": checked("require_write_role"),
    # views.note.personal_note_folder_views
    "origin.views.note.personal_note_folder_views.PersonalNoteFolderView": checked(
        "_get_owned_folder", "validate_request_user"
    ),
    "origin.views.note.personal_note_folder_views.PersonalNoteMoveView": checked("get_folder_role"),
    # views.note.personal_note_views
    "origin.views.note.personal_note_views.AllPersonalNoteMetaView": checked(
        "validate_request_user"
    ),
    "origin.views.note.personal_note_views.AllPersonalNotesView": checked("validate_request_user"),
    "origin.views.note.personal_note_views.PersonalNoteAttachmentView": checked(
        "require_write_role"
    ),
    "origin.views.note.personal_note_views.PersonalNoteMasterView": checked(
        "require_write_role", "get_folder_role"
    ),
    "origin.views.note.personal_note_views.SinglePersonalNoteView": checked("require_read_role"),
    # views.note.recent_note_views
    "origin.views.note.recent_note_views.AllRecentNotesMetaView": checked("readable_note_ids"),
    "origin.views.note.recent_note_views.RecordNoteOpenView": checked("get_effective_role"),
    # views.note.task_note_views
    "origin.views.note.task_note_views.AllTaskNoteMetaView": checked("_accessible_task_note_ids"),
    "origin.views.note.task_note_views.AllTaskNotesView": checked("_accessible_task_note_ids"),
    "origin.views.note.task_note_views.SingleTaskNoteView": checked("require_read_role"),
    "origin.views.note.task_note_views.TaskNoteAttachmentView": checked("require_write_role"),
    "origin.views.note.task_note_views.TaskNoteMasterView": checked(
        "require_write_role", "can_access_task", "validate_request_user"
    ),
    "origin.views.note.task_note_views.TaskNoteMoveView": checked("require_write_role"),
    # views.note.team_note_folder_views
    "origin.views.note.team_note_folder_views.TeamNoteFolderMemberView": checked("get_folder_role"),
    "origin.views.note.team_note_folder_views.TeamNoteFolderView": checked(
        "get_folder_role", "readable_team_folder_roles"
    ),
    "origin.views.note.team_note_folder_views.TeamNoteMetaView": checked(
        "readable_team_folder_roles"
    ),
    # views.project.prj_views
    "origin.views.project.prj_views.CheckProjectExistsView": checked(
        "require_team_member_or_response"
    ),
    "origin.views.project.prj_views.JoinProjectFromInboxView": self_only(_INBOX),
    "origin.views.project.prj_views.JoinProjectView": checked("is_project_member"),
    "origin.views.project.prj_views.LeaveProjectView": self_only(
        "removes the caller's own membership; attendee_id must equal request.user.id"
    ),
    "origin.views.project.prj_views.ProjectCustomFieldsView": checked(
        "_load_project_for_mutation", "_is_member"
    ),
    "origin.views.project.prj_views.ProjectLabelAssignmentView": checked(
        "_require_project_manager"
    ),
    "origin.views.project.prj_views.ProjectLabelsView": checked(
        "_require_project_manager", "require_team_member_or_response"
    ),
    "origin.views.project.prj_views.ProjectMasterView": checked(
        "require_project_member_or_response",
        "require_team_member_or_response",
        "can_manage",
        "is_project_member",
    ),
    "origin.views.project.prj_views.ProjectMemberRoleView": checked("can_manage"),
    "origin.views.project.prj_views.ProjectMembersView": checked(
        "require_project_member_or_response"
    ),
    "origin.views.project.prj_views.ProjectProfileImageView": checked("can_manage"),
    "origin.views.project.prj_views.ProjectSavedTaskFilterView": checked("_is_member"),
    "origin.views.project.prj_views.ProjectTagsView": checked("require_project_member_or_response"),
    "origin.views.project.prj_views.ProjectTaskFieldRulesView": checked("_is_member", "can_manage"),
    "origin.views.project.prj_views.ProjectTaskTemplateView": checked("_is_member"),
    "origin.views.project.prj_views.ProjectTemplateDefaultsView": checked("_is_member"),
    "origin.views.project.prj_views.ProjectsView": checked("require_team_member_or_response"),
    # views.public.mcp.views
    "origin.views.public.mcp.views.McpView": checked("is_team_member"),
    # views.public.openapi
    "origin.views.public.openapi.OpenApiSchemaView": public(
        "the public API's schema document; no data"
    ),
    # views.public.v1_views
    "origin.views.public.v1_views.MeView": self_only(
        "the key owner's own identity and the key's own scope"
    ),
    "origin.views.public.v1_views.ProjectListView": checked("is_team_member"),
    "origin.views.public.v1_views.TaskDetailView": checked("can_access_task"),
    "origin.views.public.v1_views.TaskListCreateView": checked(
        "is_team_member", "is_project_member"
    ),
    # views.task.milestone_views
    "origin.views.task.milestone_views.MilestoneAssigneesView": checked("is_project_member"),
    "origin.views.task.milestone_views.MilestoneView": checked("is_project_member"),
    "origin.views.task.milestone_views.ProjectMilestonesView": checked("is_project_member"),
    # views.task.search_views
    "origin.views.task.search_views.GetSearchTeamTasksView": checked("is_project_member"),
    # views.task.sprint_views
    "origin.views.task.sprint_views.ProjectSprintsView": checked("is_project_member"),
    "origin.views.task.sprint_views.SprintConfigView": checked("is_project_member"),
    "origin.views.task.sprint_views.SprintView": checked("is_project_member"),
    # views.task.task_activity_views
    "origin.views.task.task_activity_views.MilestoneBurndownView": checked("can_access_task"),
    "origin.views.task.task_activity_views.TaskActivityListView": checked("can_access_task"),
    "origin.views.task.task_activity_views.TaskVelocityView": checked("can_access_task"),
    # views.task.task_delivery_views
    "origin.views.task.task_delivery_views.TaskDeliveryListView": checked("can_access_task"),
    # views.task.task_views
    "origin.views.task.task_views.ChildTaskView": checked("is_project_member"),
    "origin.views.task.task_views.GetMyAssignedTasksView": checked("is_team_member"),
    "origin.views.task.task_views.GetProjectTasksView": checked("is_project_member"),
    "origin.views.task.task_views.GetTaskByThreadIdView": checked("can_access_task"),
    "origin.views.task.task_views.GetTaskView": checked("can_access_task"),
    "origin.views.task.task_views.GetTeamTasksPageView": checked("require_team_member_or_response"),
    "origin.views.task.task_views.GetTeamTasksView": checked("require_team_member_or_response"),
    "origin.views.task.task_views.TaskAttachmentsView": checked("can_access_task"),
    "origin.views.task.task_views.TaskBodyAttachmentView": checked("can_access_task"),
    "origin.views.task.task_views.TaskCommentMentionView": checked("can_access_task"),
    "origin.views.task.task_views.TaskCommentReactionView": checked("can_access_task"),
    "origin.views.task.task_views.TaskCommentsView": checked("can_access_task"),
    "origin.views.task.task_views.TaskDependencyBatchListView": checked("_openable_task_q"),
    "origin.views.task.task_views.TaskDependencyView": checked("can_access_task"),
    "origin.views.task.task_views.TaskMasterView": checked("can_access_task"),
    "origin.views.task.task_views.TaskMetaView": checked("reachable_project_ids"),
    # views.utils.extract_page_title_view
    "origin.views.utils.extract_page_title_view.PageTitleView": signed_in(
        "fetches a public page title through the SSRF-guarded fetcher (MIP-717); PageTitleThrottle"
    ),
}
