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
  * `is_active=False`, which SimpleJWT re-checks against the DB on
    EVERY request (`CHECK_USER_IS_ACTIVE`), so all sessions on all
    devices die at once, with no token blacklist to maintain;
  * personal-only data deleted outright: notification preferences and
    push subscriptions, todos, personal tags, personal notes, and the
    AI/search rows keyed by the bare `user_id` string (those carry no
    FK, so nothing else would ever clean them up).

**Ownership is the one hard blocker.** A team whose owner vanishes is
unadministrable, so deletion refuses while the user solely owns a team
that still has other active members — they must transfer ownership
first (the UI links to it). A team where they are the last member is
soft-deleted along with them.
"""

from __future__ import annotations

import logging
import uuid

from django.db import transaction
from django.utils import timezone

from origin.models.chat.personal_tag_models import PersonalChannelTag
from origin.models.chat.todo_models import ToDoCategory, ToDoGroup
from origin.models.common.api_key_models import ApiKey
from origin.models.common.notification_models import NotificationPreference, PushSubscription
from origin.models.common.security_event_models import SecurityAction
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount
from origin.services.security_events import record_security_event

log = logging.getLogger(__name__)

DELETED_EMAIL_DOMAIN = "deleted.invalid"
DELETED_USERNAME = "Deleted user"


class OwnershipTransferRequired(Exception):
    """Raised when the user still solely owns shared teams."""

    def __init__(self, teams):
        self.teams = teams
        super().__init__("Ownership must be transferred before deleting the account.")


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


def _delete_personal_data(user) -> None:
    """Data that exists only for this person and has no team value."""
    NotificationPreference.objects.filter(user=user).delete()
    PushSubscription.objects.filter(user=user).delete()
    # OAuth grants hold live third-party refresh tokens.
    ConnectedAccount.objects.filter(user=user).delete()
    ToDoGroup.objects.filter(user=user).delete()  # items cascade
    ToDoCategory.objects.filter(user=user).delete()
    # Assignments carry no user column by design — they cascade from the
    # tag, whose owner IS the user.
    PersonalChannelTag.objects.filter(user=user).delete()

    # Personal notes are private by definition, so they are erasable
    # content rather than team content. Folders cascade nothing (the
    # tree is plain integer columns), so both go explicitly.
    from origin.models.note.common_note_models import NotePermissionMaster
    from origin.models.note.personal_note_models import (
        PersonalNoteFolder,
        PersonalNoteMaster,
    )

    note_ids = list(PersonalNoteMaster.objects.filter(owner=user).values_list("note_id", flat=True))
    if note_ids:
        NotePermissionMaster.objects.filter(note_id__in=note_ids, note_type=1).delete()
        PersonalNoteMaster.objects.filter(note_id__in=note_ids).delete()
    PersonalNoteFolder.objects.filter(owner=user).delete()


def delete_account(user) -> dict:
    """Erase the person, keep the workspace consistent.

    Raises `OwnershipTransferRequired` when shared teams would be left
    without an owner. Returns a small summary for the audit log.
    """
    blocking = blocking_owned_teams(user)
    if blocking:
        raise OwnershipTransferRequired(blocking)

    user_id = str(user.id)
    original_email = user.email

    with transaction.atomic():
        # Teams where this user was the last member go with them. Soft,
        # not a purge: support can still answer "what happened to this
        # team", and nothing cascades into rows shared with anyone else.
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
        TeamMembers.objects.filter(attendee=user, is_deleted=False).update(
            is_deleted=True, ts_updated_at=timezone.now()
        )

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

        _delete_personal_data(user)
        _purge_search_engine_rows(user_id)

        # Identity erasure. The email must stay unique and unroutable.
        user.email = f"deleted-{uuid.uuid4().hex[:16]}@{DELETED_EMAIL_DOMAIN}"
        user.username = DELETED_USERNAME
        user.phone_number = None
        user.custom_status = None
        user.role = None
        user.base_country = None
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
        user.token = None
        user.token_expiration = None
        user.password_reset_token_hash = None
        user.password_reset_token_expires_at = None
        user.digest_enabled = False
        user.email_digest_enabled = False
        user.save()

    log.warning(
        "[account] deleted user=%s (was %s) solo_teams=%d",
        user_id,
        original_email,
        len(solo_team_ids),
    )
    return {"user_id": user_id, "solo_teams_closed": len(solo_team_ids)}
