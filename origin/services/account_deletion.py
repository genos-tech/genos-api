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
from origin.models.common.notification_models import NotificationPreference, PushSubscription
from origin.models.common.team_models import TeamMaster, TeamMembers
from origin.models.common.user_models import ConnectedAccount

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
        AgentRunFeedback,
        AgentSession,
        AiCreditEntry,
        AiRequestCost,
        AiSpendEvent,
    )

    for model in (
        AgentRunFeedback,
        AgentSession,  # AgentRun / AgentStep / AgentLlmCall cascade from it
        AiCreditEntry,
        AiRequestCost,
        AiSpendEvent,
    ):
        model.objects.filter(user_id=user_id).delete()

    # NOTE: `RagChunk` is keyed by (entity_type, entity_id, team_id) and
    # has no user column, so it cannot be swept per-user here. The
    # deleted personal notes are handled by the existing deleted-data
    # purge, which reconciles chunks against their vanished parents.


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
        TeamMembers.objects.filter(attendee=user, is_deleted=False).update(
            is_deleted=True, ts_updated_at=timezone.now()
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
