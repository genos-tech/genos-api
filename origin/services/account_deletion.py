"""Account deletion — soft-delete + anonymize.

GDPR/CCPA make erasure an obligation, not a feature. What that requires
is that the PERSON becomes unidentifiable; it does not require shredding
the workspace their teammates still depend on.

**Why soft-delete + anonymize rather than a row delete.** `CustomUser`
is referenced by ~66 foreign keys: 48 `SET_NULL` and 18 `CASCADE`, and
zero `PROTECT`. A real `.delete()` would therefore succeed silently
while doing two bad things at once — orphaning every message, task,
comment and note the person authored inside teams that are still live
(their teammates lose attribution and, for `TeamMembers.attendee`, gain
ghost membership rows that still count), and cascading away rows nobody
asked to lose. Anonymizing the row instead keeps referential integrity
intact and erases the identity, which is what the law actually asks for:
past work stays attributed to "Deleted user", and nothing dangles.

**What erasure means here, concretely:**
  * identity fields overwritten (email, username, phone, job title,
    status, avatar) — the email is replaced with an unroutable
    `deleted-<uuid>@deleted.invalid` so the unique constraint still
    holds and nothing can ever mail them again;
  * password made unusable and OAuth grants deleted (those rows hold
    live third-party refresh tokens — credentials, not just PII);
  * every Stripe subscription of the person, and of the teams that die with them, cancelled at
    once BEFORE anything is erased, so nothing bills a deleted person (MIP-2254). The Stripe
    customer record is kept (MIP-874). If Stripe cannot be reached the deletion stops with
    `BillingCancelFailed` and can be repeated;
  * `is_active=False`, which SimpleJWT re-checks against the DB on
    EVERY request (`CHECK_USER_IS_ACTIVE`), so all sessions on all
    devices die at once, with no token blacklist to maintain;
  * personal-only data deleted outright: notification preferences and
    push subscriptions, the first-month trial row, todos, personal tags,
    My Notes notes and folders, and the AI/search rows keyed by the bare
    `user_id` string (those carry no FK, so nothing else would ever clean
    them up);
  * what the TEAM reads is kept: a Team Notes note, and the team folder
    it sits in, stays when anyone else can still read it. The note reads
    "Deleted user" like every other line of the person's; the folder gets
    a live owner (a co-owner, else the team owner). See `note_spaces`.

**Ownership is the one hard blocker.** A team whose owner vanishes is
unadministrable, so deletion refuses while the user solely owns a team
that still has other active members — they must transfer ownership
first (the UI links to it). A team where they are the last member is
soft-deleted along with them.
"""

from __future__ import annotations

import logging
import uuid

from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from oauth2_provider.models import (
    get_access_token_model,
    get_grant_model,
    get_refresh_token_model,
)

from origin.models.chat.personal_tag_models import PersonalChannelTag
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup
from origin.models.chat.unified_models import ReadCursor
from origin.models.common.api_key_models import ApiKey
from origin.models.common.briefing_models import MemberBriefingState
from origin.models.common.erasure_models import ErasureObjectType
from origin.models.common.invite_models import TeamInvite
from origin.models.common.item_activity_models import purge_for_user as purge_item_activity
from origin.models.common.mcp_connection_models import McpConnection
from origin.models.common.member_importance_models import (
    purge_for_user as purge_importance,
)
from origin.models.common.mfa_models import MFARecoveryCode, UserMFA
from origin.models.common.notification_models import NotificationPreference, PushSubscription
from origin.models.common.schedule_models import MemberSchedule, MemberScheduleSeed
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.trial_models import CoreTrial
from origin.models.common.user_models import ConnectedAccount
from origin.services import blind_index, change_feed, erasure_log
from origin.services.deleted_account_activity import delete_for_recipient as delete_activity_for
from origin.services.deleted_account_leftovers import blank_network_for
from origin.services.deleted_account_leftovers import delete_for_user as delete_leftovers_for
from origin.services.deleted_account_memberships import end_group_memberships
from origin.services.oauth import revoke as oauth_revoke
from origin.services.security_events import record_security_event
from origin.services.skills import service as skills_service
from origin.views.utils.note_acl_resync import purge_notes_from_index

log = logging.getLogger(__name__)

DELETED_EMAIL_DOMAIN = "deleted.invalid"
DELETED_USERNAME = "Deleted user"


class OwnershipTransferRequired(Exception):
    """Raised when the user still solely owns shared teams."""

    def __init__(self, teams):
        self.teams = teams
        super().__init__("Ownership must be transferred before deleting the account.")


class BillingCancelFailed(Exception):
    """Raised, before anything is erased, when a subscription could not be cancelled at Stripe.

    Erasing the account first would leave a paid plan billing a person nobody can reach: the
    retry is cheap and the person is at the screen, so the deletion stops and can be repeated."""


def _teams_closing_with(user) -> list[TeamMaster]:
    """The teams that die with this person: the ones they own (those with other members already
    blocked the deletion) and the ones where they are the last live member."""
    teams = {t.team_id: t for t in TeamMaster.objects.filter(owner=user, is_deleted=False)}
    for membership in TeamMembers.objects.filter(attendee=user, is_deleted=False).select_related(
        "team"
    ):
        team = membership.team
        if team.team_id in teams or team.is_deleted or team.is_demo:
            continue
        others = (
            TeamMembers.objects.filter(
                team=team,
                is_deleted=False,
                attendee__is_deleted=False,
                attendee__is_system_user=False,
            )
            .exclude(attendee=user)
            .exists()
        )
        if not others:
            teams[team.team_id] = team
    return list(teams.values())


def _cancel_billing(user) -> set:
    """Cancel the person's own subscription and those of the teams that die with them (MIP-2254).

    Runs BEFORE the erasure and fails closed (`BillingCancelFailed`). The Stripe customer record
    is kept (Ken, 2026-11-02, MIP-874): only the charging stops. Returns the ids of the teams
    whose subscription was cancelled, so the erasure can drop their plan to free.

    No network call for a person who never paid: a customer id is the only thing that asks Stripe.

    Two accepted limits. (1) The cancel is not locked against the erasure: if someone joins a team
    the person was the last live member of in those few milliseconds, the team stays open with its
    plan cancelled (the webhook then drops it to free). (2) If the erasure itself fails after the
    cancel, the subscriptions stay cancelled and the account stays; repeating the deletion finishes it.
    """
    from origin.services import stripe_billing

    customers = {}  # customer id -> team (None for the person's own)
    if user.stripe_customer_id:
        customers[user.stripe_customer_id] = None
    for team in _teams_closing_with(user):
        if team.stripe_customer_id:
            customers[team.stripe_customer_id] = team
    if not customers:
        return set()
    if not stripe_billing.billing_enabled():
        # Nothing can be cancelled without a key (a dev stack). Production has one.
        log.warning("[account] user=%s has Stripe customers but billing is off", user.pk)
        return set()

    cancelled_teams = set()
    for customer_id, team in customers.items():
        try:
            count = stripe_billing.cancel_subscriptions_now(customer_id)
        except stripe_billing.BillingError as exc:
            # The customer id is not logged: the id of the person is enough to find it.
            log.warning("[account] subscription not cancelled user=%s", user.pk, exc_info=True)
            raise BillingCancelFailed from exc
        if count:
            log.warning(
                "[account] cancelled %d subscription(s) user=%s team=%s",
                count,
                user.pk,
                team.team_id if team else "-",
            )
        if team is not None:
            cancelled_teams.add(team.team_id)
    return cancelled_teams


def blocking_owned_teams(user) -> list[TeamMaster]:
    """Teams this user owns that OTHER active members still depend on.

    A team where they are the last one standing is not blocking — it
    goes away with them.
    """
    blocking = []
    for team in TeamMaster.objects.filter(owner=user, is_deleted=False):
        others = (
            TeamMembers.objects.filter(team=team, is_deleted=False).exclude(attendee=user).exists()
        )
        if others:
            blocking.append(team)
    return blocking


def _purge_search_engine_rows(user_id: str) -> None:
    """Delete the AI/search rows keyed by a BARE `user_id` string.

    These models deliberately carry no FK to `CustomUser`, so neither
    CASCADE nor SET_NULL touches them — without this they would outlive
    the account forever. Imported locally: `search_engine.models` pulls
    the agent stack, which account deletion has no other reason to load.
    """
    from origin.search_engine.models import (
        AgentRun,
        AgentRunFeedback,
        AgentSession,
        AiCreditEntry,
        AiRequestCost,
        AiSpendEvent,
    )

    # `AgentRun` MUST be swept explicitly, and it must come before
    # `AgentSession`. `AgentRun.session` is `on_delete=SET_NULL`
    # (search_engine/models.py), so deleting the session does NOT cascade
    # to its runs — it NULLs their `session_id` and leaves them alive.
    # That matters more here than anywhere else: `AgentRun.query` holds
    # the user's question as raw text and `final_answer_text` the answer,
    # so a missed sweep leaves the most directly personal content in this
    # whole module behind. `AgentStep` / `AgentLlmCall` /
    # `AgentRunJudgement` CASCADE from the RUN (not from the session), so
    # they survive an orphaned run too, and only the run delete sweeps
    # them.
    #
    # `AgentRunFeedback` also cascades from `AgentRun`, so its entry below
    # looks redundant — it is not. It carries its own `user_id`, which is
    # the *rater*, who need not be the run's owner, so filtering it
    # directly erases ratings this person left on anyone's run.
    for model in (
        AgentRunFeedback,
        AgentRun,
        AgentSession,
        AiCreditEntry,
        AiRequestCost,
        AiSpendEvent,
    ):
        model.objects.filter(user_id=user_id).delete()

    # NOTE: `RagChunk` is keyed by (entity_type, entity_id, team_id) and
    # has no user column, so it cannot be swept per-user here. The
    # deleted personal notes are handled by the existing deleted-data
    # purge, which reconciles chunks against their vanished parents.
    # The same holds for the `conversation` / `spotlight_answer` chunks
    # built off the runs deleted above: `purge.py::_resolve_agent_runs`
    # reports a chunk as dead once its `AgentRun` row is gone, so the
    # answer text those chunks carry is collected by the next sweep
    # rather than needing a pass here.


def _delete_personal_data(user, closing_team_ids=()) -> None:
    """Data that exists only for this person and has no team value.

    `closing_team_ids` are the teams that die with the person: nothing of theirs is kept there."""
    NotificationPreference.objects.filter(user=user).delete()
    PushSubscription.objects.filter(user=user).delete()
    # The two-factor secret and recovery-code hashes (MIP-2239): no cascade fires on a soft delete.
    MFARecoveryCode.objects.filter(user=user).delete()
    UserMFA.objects.filter(user=user).delete()
    # OAuth grants hold live third-party refresh tokens. With OAUTH_REVOKE_ON_DISCONNECT on they are
    # also revoked at the provider, once the erasure commits (MIP-1790); off, this is the plain delete.
    grants = oauth_revoke.capture(ConnectedAccount.objects.filter(user=user))
    ConnectedAccount.objects.filter(user=user).delete()
    oauth_revoke.revoke_after_commit(grants)
    ToDoGroup.objects.filter(user=user).delete()  # items cascade
    ToDoCategory.objects.filter(user=user).delete()
    # What the person has read, and when (MIP-1550): theirs alone, nothing shows it to anyone
    # else. The FK cascade never fires for a soft delete, so the rows have to go by hand.
    ReadCursor.objects.filter(user=user).delete()
    MemberBriefingState.objects.filter(user=user).delete()
    # The member's schedules (MIP-1147) are theirs alone; the account is soft-deleted, so no
    # FK cascade fires and the tick would keep running them.
    MemberSchedule.objects.filter(user=user).delete()
    MemberScheduleSeed.objects.filter(user=user).delete()  # the marker (MIP-1178)
    # Their Genos Skills, stars and installs (MIP-2040), and the body notes of the skills: the
    # account is soft-deleted, so no FK cascade fires.
    skills_service.erase_for_user(user)
    # The rest of what no cascade reached (MIP-1783): the email outbox, the whole inbox (the
    # reports those schedules filed (MIP-1149), notices, digests, reminders, requests), flags, pins,
    # reminders, reactions, recents, to-do schedules, favorites, quota counters, and the
    # "generated by" id on shared summaries. What stays, and why: `deleted_account_leftovers`.
    delete_leftovers_for(user.pk, name=user.username)
    # Connected apps (MIP-1077): the grant AND every token, refresh included, that an AI
    # client holds for this person. The account is soft-deleted, so no FK cascade fires:
    # without this the tokens would outlive the person.
    McpConnection.objects.filter(user=user).delete()
    # The member's whole activity feed (MIP-1541, MIP-1542): every `Activity` row addressed to
    # them, whatever the kind (mention, reply, reaction, message, comment notification, AI-app
    # notice). `meta` can carry previews of other people's messages. `recipient` is a CASCADE
    # FK, but the account is soft-deleted so nothing fires. Rows where they are only the
    # ACTOR stay: they are someone else's feed and read "Deleted user".
    delete_activity_for(user.pk)
    # The first month of Core (MIP-1737, MIP-1779): the row is the person's, and nothing about it
    # may outlive the account, so a deleted person's trial is not kept to refuse a second month.
    # The FK cascade never fires for a soft delete, so it goes by hand.
    CoreTrial.objects.filter(user=user).delete()
    get_grant_model().objects.filter(user=user).delete()
    get_refresh_token_model().objects.filter(user=user).delete()
    get_access_token_model().objects.filter(user=user).delete()
    # Assignments carry no user column by design — they cascade from the
    # tag, whose owner IS the user.
    PersonalChannelTag.objects.filter(user=user).delete()
    user.member_events.all().delete()  # what their AI apps opened (MIP-1143)
    user.agent_calls.all().delete()  # what their AI apps called (MIP-2006)
    purge_item_activity(user.pk)  # the per-day table (MIP-1084)
    purge_importance(user.pk)  # what they marked and tuned (MIP-1087)

    # My Notes are private by definition, so they are erasable content. TEAM NOTES are not:
    # a note in a team folder (and the folder, the team's ACL carrier) belongs to the team, and
    # teammates read it (MIP-1782). `PersonalNoteMaster` / `PersonalNoteFolder` hold both
    # spaces in one table, so what goes and what stays is decided by `note_spaces.plan_erasure`,
    # the rule the data export labels notes with too. The folder tree is plain integer columns
    # (no cascade), so notes and folders go explicitly.
    from origin.models.note.common_note_models import NoteFolderPermission, NotePermissionMaster
    from origin.models.note.favorite_note_models import NoteFavoriteMaster
    from origin.models.note.personal_note_models import (
        PersonalNoteFolder,
        PersonalNoteMaster,
    )
    from origin.models.note.recent_note_models import NoteRecentMaster
    from origin.models.note.version_note_models import NoteVersionMaster
    from origin.services.note_delete import forget_deleted_notes, forget_note_attachments
    from origin.services.note_satellites import batched_note_cleanup
    from origin.services.note_spaces import hand_over_folder, plan_erasure

    plan = plan_erasure(user, closing_team_ids)
    note_ids = sorted(plan.delete_note_ids)
    if note_ids:
        forget_note_attachments(1, note_ids)  # their uploaded files, before the notes go (MIP-1720)
        NotePermissionMaster.objects.filter(note_id__in=note_ids, note_type=1).delete()
        # The note rows' versions and summaries (which hold their text) go with them in one
        # pass, through the post_delete receiver (MIP-1517).
        with batched_note_cleanup():
            PersonalNoteMaster.objects.filter(note_id__in=note_ids).delete()
        # What other people hold about those notes: recents, favorites, mention rows (MIP-1514).
        forget_deleted_notes(1, note_ids)
    # The team folders that stay get a live owner BEFORE the person's own grants go, so a
    # folder is never left with nobody who can manage it when somebody could have.
    for folder_id in plan.handover_folder_ids:
        hand_over_folder(folder_id, user.pk)
    PersonalNoteFolder.objects.filter(folder_id__in=plan.delete_folder_ids).delete()
    # The person's grants on every team folder and note: the folder FK would cascade them, but
    # the account is soft-deleted. Other people's grants on a note or folder that stays are theirs.
    NoteFolderPermission.objects.filter(user=user).delete()
    NotePermissionMaster.objects.filter(user=user, note_type=1).delete()
    # What the person wrote into a team's history stays (the editor reads "Deleted user", like
    # every other line of theirs); the name of the API key or connected app they wrote through is
    # their own label, so it does not.
    NoteVersionMaster.objects.filter(editor=user).update(via=None)
    _drop_from_search_after_commit(plan.delete_note_ids, plan.delete_folder_ids)
    # The person's own lists of notes they opened or starred: the account is soft-deleted, so
    # the user FK (SET_NULL) never fires and the rows would stay, pointing at their id.
    NoteRecentMaster.objects.filter(user=user).delete()
    NoteFavoriteMaster.objects.filter(user=user).delete()


def _drop_from_search_after_commit(note_ids, folder_ids) -> None:
    """Take the erased notes and folders out of search, once the erasure has committed.

    Best-effort and after the commit, like the delete paths in `note_delete`: a failed erasure
    must not have emptied the index, and the orphan sweep retries what this misses. The notes
    that stay are not touched here: their text and access did not change, and the owner change
    on a team folder bumps the folder's `ts_updated_at`, which the incremental reindex follows.
    """
    notes, folders = sorted(note_ids), sorted(folder_ids)
    if not notes and not folders:
        return

    def _purge() -> None:
        from origin.search_engine.purge import purge_note_folders

        purge_notes_from_index(notes)
        purge_note_folders(folders)

    transaction.on_commit(_purge, robust=True)


def _delete_avatar_file(path: str, user_id: str) -> None:
    """Remove the person's photo from storage.

    Clearing `profile_image_url` on the row only forgets the path; the file
    stays on the media volume. The avatar endpoint deletes the previous file
    when a photo is replaced (`previous_file.delete(save=False)`), and
    deletion is the one other path that has to.

    Best-effort: a file that is already gone, or a storage error, must not
    block the erasure itself. The row is anonymised either way, so the worst
    case is a file left behind, which is where this started. The failure is
    logged by user id, never by path or address.
    """
    if not path:
        return
    try:
        # The same storage the upload wrote through, so this works on the
        # media volume and on a bucket alike.
        default_storage.delete(path)
    except Exception:  # noqa: BLE001 - best-effort cleanup, see above
        log.warning("[account] avatar file not removed user=%s", user_id, exc_info=True)


def _delete_invites_to(email: str) -> int:
    """Remove the invitations addressed to `email`; returns how many.

    An invite stores the address it was sent to (encrypted, plus a blind
    index), so it keeps the person's address after the account row has
    forgotten it. Only the invites TO this address go: the ones this person
    SENT are team history, and keep pointing at the anonymised row, which
    reads "Deleted user" like the rest of their past work.

    Every status, not just pending. An accepted or revoked invite still
    carries the address, and the audit trail the team owner reads is
    `SecurityEvent`, which has its own (masked) record of the departure.
    """
    deleted, _ = TeamInvite.objects.filter(blind_index.INVITE_EMAIL.matches(email)).delete()
    return deleted


def _cut_off_open_sockets(user_id, left_team_ids) -> None:
    """Take the person's open sockets out of every room they were in, after commit.

    One push per team: a team they were a member of leaves the team room as well; a team where
    they only held a project or a chat (a guest, an external chat) has just those rooms listed.
    Best-effort and a no-op where genos-sockets is not configured (`push_membership_removed`).
    """
    from origin.models.chat.unified_models import ChannelMember
    from origin.models.project.prj_models import ProjectMembers
    from origin.services.member_removal import memberships_in_team
    from origin.services.realtime_push import push_membership_removed

    left = {str(t) for t in left_team_ids}
    guest_teams = {
        str(t)
        for t in (
            *ProjectMembers.objects.filter(attendee_id=user_id).values_list("team_id", flat=True),
            *ChannelMember.objects.filter(user_id=user_id, is_deleted=False).values_list(
                "channel__team_id", flat=True
            ),
        )
        if t is not None
    } - left
    for team_id, scope in [(t, "team") for t in sorted(left)] + [
        (t, "channel") for t in sorted(guest_teams)
    ]:
        projects, channels = memberships_in_team(team_id, user_id)
        push_membership_removed(
            user_id=user_id, team_id=team_id, scope=scope, project_ids=projects, channels=channels
        )


def _disconnect_open_sockets(user_id) -> None:
    """Close every open socket of the deleted person (MIP-2241): leaving the rooms is not
    enough, the socket itself stays connected and in their own user rooms."""
    from origin.services.realtime_push import push_user_disconnect

    push_user_disconnect(user_id)


def delete_account(user) -> dict:
    """Erase the person, keep the workspace consistent.

    Raises `OwnershipTransferRequired` when shared teams would be left
    without an owner. Returns a small summary for the audit log.
    """
    blocking = blocking_owned_teams(user)
    if blocking:
        raise OwnershipTransferRequired(blocking)

    # Before anything is erased, and fail closed: a person who pays must not keep being charged
    # after they are gone (MIP-2254). Raises `BillingCancelFailed`, and the erasure has not begun.
    cancelled_team_ids = _cancel_billing(user)

    user_id = str(user.id)
    original_email = user.email
    # The file is held now: the identity erasure below blanks the field. It is
    # removed only after the commit, because a file delete cannot be rolled back
    # and a failed erasure must not cost the person their photo.
    avatar_path = user.profile_image_url.name if user.profile_image_url else ""

    with transaction.atomic():
        # Teams where this user was the last member go with them: closed here, in this
        # transaction, and their data is purged by the `closed-team-purge` cron right after
        # (MIP-1958, `origin/services/team_purge.py`), never inside the request.
        solo_team_ids = list(
            TeamMaster.objects.filter(owner=user, is_deleted=False).values_list(
                "team_id", flat=True
            )
        )
        if solo_team_ids:
            TeamMaster.objects.filter(team_id__in=solo_team_ids).update(
                is_deleted=True, ts_updated_at=timezone.now()
            )

        # Memberships end, so the person stops appearing in member
        # lists, mention pickers and capacity maths.
        left_team_ids = list(
            TeamMembers.objects.filter(attendee=user, is_deleted=False).values_list(
                "team_id", flat=True
            )
        )
        # What their open browser is still in, read BEFORE the rows go, and cut off once the
        # erasure commits (MIP-1550): ending a membership does not end a socket that already
        # joined a room, so the deleted person's tab kept receiving the team's chats and kept
        # broadcasting "online" to its roster until it reconnected (it cannot: the token dies).
        _cut_off_open_sockets(user.pk, left_team_ids)
        _disconnect_open_sockets(user.pk)

        TeamMembers.objects.filter(attendee=user, is_deleted=False).update(
            is_deleted=True, ts_updated_at=timezone.now()
        )
        # A team nobody else is live in goes with the person (MIP-1958): the owned one above, and
        # also a team they were a plain member of whose owner is long gone. Closing is the
        # "enqueue"; the cron `closed-team-purge` deletes its data, outside this request.
        from origin.services.team_purge import close_team_if_empty

        for team_id in left_team_ids:
            if close_team_if_empty(team_id):
                solo_team_ids.append(team_id)
        # A closed team whose subscription was just cancelled goes to `free` here: the webhook
        # that follows ignores a closed team, and `team_purge` holds a paid one for billing
        # (MIP-2254).
        if cancelled_team_ids:
            from origin.services import stripe_billing

            for closed in TeamMaster.objects.filter(
                team_id__in=[t for t in solo_team_ids if t in cancelled_team_ids]
            ):
                stripe_billing.end_team_plan_after_cancel(closed)
        # The person is no longer a participant of a group or a project chat (MIP-1546). A DM
        # or multi-person DM row stays: the others keep the conversation.
        end_group_memberships(user.pk)

        # Two queryset updates no receiver hears (MIP-970): the person's teams
        # (and any team that died with them) change what members see.
        for team_id in {*solo_team_ids, *left_team_ids}:
            change_feed.bump_team(team_id)

        # Every key, not just team-bound ones: the person is gone, so
        # nothing may act as them. `is_active=False` below already makes
        # the authenticator refuse them; revoking records it on the key
        # itself, so a team owner's key list shows it as revoked instead
        # of live (`TeamApiKeyListView`).
        revoked_keys = ApiKey.objects.filter(user=user, revoked_at__isnull=True).update(
            revoked_at=timezone.now(), ts_updated_at=timezone.now()
        )

        # Written here, before the identity below is erased: the trail
        # keeps the address the account had, because it holds no FK to
        # join back through. Each team the person left gets its own row
        # so its owner sees the departure.
        record_security_event(
            SecurityAction.ACCOUNT_DELETED,
            actor=user,
            target_type="user",
            target_id=user_id,
            target_label=original_email,
            metadata={"solo_teams_closed": len(solo_team_ids), "api_keys_revoked": revoked_keys},
        )
        for team_id in left_team_ids:
            record_security_event(
                SecurityAction.MEMBER_REMOVED,
                actor=user,
                team_id=team_id,
                target_type="user",
                target_id=user_id,
                target_label=original_email,
                metadata={"via": "account_deleted"},
            )
        # The rows above were just written with this person as the actor, and carry the IP and
        # user agent of the request that deleted them. Blanked here, after they exist (MIP-1789).
        blank_network_for(user.pk)

        # What the guest teams' rosters wrote for this person on OTHER teams' objects (a shared
        # project, chat or note folder, MIP-1550). Leaving a team through `remove_team_member`
        # withdraws these; deleting the account is the other way out of a team and used to leave
        # them. Nothing re-checks a grant at read time, so a row left here is access nobody sees.
        from origin.services.external_grants import drop_team_member_participation

        withdrawn = sum(
            drop_team_member_participation(team_id, user.pk)
            for team_id in {*solo_team_ids, *left_team_ids}
        )

        _delete_personal_data(user, solo_team_ids)
        _purge_search_engine_rows(user_id)
        # Both read the identity that is overwritten just below, so they run first.
        invites_removed = _delete_invites_to(original_email)
        # A group or project chat the person owned gets a live owner (MIP-1550). The row below
        # is still not marked deleted, so the helper is told which account to leave out.
        from origin.services.deleted_account_channel_owners import reassign_owned_channels

        reassign_owned_channels(user.pk)

        # Identity erasure. The email must stay unique and unroutable.
        user.email = f"deleted-{uuid.uuid4().hex[:16]}@{DELETED_EMAIL_DOMAIN}"
        user.username = DELETED_USERNAME
        user.phone_number = None
        user.custom_status = None
        user.role = None
        user.current_location = None
        # Free text the user wrote about themselves — the most identifying
        # field on this row after the ones above, and the only one where
        # they chose the words.
        user.about_me = None
        user.timezone = None
        user.language = None
        user.profile_image_url = ""
        user.profile_image_file_name = None
        user.set_unusable_password()
        user.is_deleted = True
        # The session kill-switch: SimpleJWT re-reads this row on every
        # request, so every device is signed out immediately.
        user.is_active = False
        user.is_email_verified = False
        user.password_reset_token_hash = None
        user.password_reset_token_expires_at = None
        user.save()
        erasure_log.record(ErasureObjectType.ACCOUNT, user_id)  # MIP-2249

    _delete_avatar_file(avatar_path, user_id)

    # The id only: logging `original_email` here kept the address in Railway
    # after the row forgot it (MIP-622).
    log.warning(
        "[account] deleted user=%s solo_teams=%d invites_removed=%d",
        user_id,
        len(solo_team_ids),
        invites_removed,
    )
    return {
        "user_id": user_id,
        "solo_teams_closed": len(solo_team_ids),
        "invites_removed": invites_removed,
        "external_participation_withdrawn": withdrawn,
    }
