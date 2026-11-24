"""`python manage.py operator_mfa_reset` — turn a person's two-factor off, with an audit row (MIP-2239).

The support path for someone who has lost both their phone and their recovery codes
(AUTH_SECURITY_PLAN §4.5). Resetting two-factor hands an account to whoever asks, so the
policy is a person's decision, not this command's: it only does the reset and records who
did it and why. Proof of identity, per the runbook, comes BEFORE running it.

Usage:
    python manage.py operator_mfa_reset <user_id> --reason "<why, and how identity was checked>" [--dry-run]

It deletes the secret and every recovery code, so the person signs in with the password alone
and can set two-factor up again from Settings > Security. Sessions are left alone (this is
not a cut-off: use `operator_cutoff` for that). Idempotent: an account without two-factor
writes nothing.

**No audit row, no reset.** The change and its `SecurityEvent` commit together.
Prints ids and counts only.
"""

from __future__ import annotations

import getpass
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from origin.models.common.mfa_models import MFARecoveryCode, UserMFA
from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.services.pii_mask import mask_email
from origin.services.security_events import record_security_event

User = get_user_model()
_REASON_MAX = 1000


class Command(BaseCommand):
    help = "Support: turn a user's two-factor sign-in off and write an operator SecurityEvent."

    def add_arguments(self, parser):
        parser.add_argument("user_id", help="The user's id (UUID).")
        parser.add_argument("--reason", required=True, help="Why. Recorded in the audit row.")
        parser.add_argument("--dry-run", action="store_true", help="Change and record nothing.")

    def handle(self, *args, **opts):
        reason = mask_email((opts["reason"] or "").strip())[:_REASON_MAX]
        if not reason:
            raise CommandError("--reason must not be blank.")
        try:
            user_id = uuid.UUID(str(opts["user_id"]).strip())
        except ValueError:
            raise CommandError("user_id must be a UUID.") from None
        user = User._default_manager.filter(pk=user_id).first()
        if user is None:
            raise CommandError(f"No user with id {user_id}.")
        mfa = UserMFA.objects.filter(user_id=user_id).first()
        if mfa is None:
            self.stdout.write(f"user {user_id}: two-factor is not set up; nothing to do.")
            return
        codes = MFARecoveryCode.objects.filter(user_id=user_id).count()
        prefix = "[dry-run] " if opts["dry_run"] else ""
        self.stdout.write(
            f"{prefix}user {user_id}: delete the secret and {codes} recovery code(s)."
        )
        if opts["dry_run"]:
            return
        operator = getpass.getuser()
        with transaction.atomic():
            MFARecoveryCode.objects.filter(user_id=user_id).delete()
            UserMFA.objects.filter(user_id=user_id).delete()
            event = record_security_event(
                SecurityAction.OPERATOR_MFA_RESET,
                actor_label=f"operator:{operator}",
                force_source=SecuritySource.OPERATOR,
                target_type="user",
                target_id=user_id,
                metadata={
                    "reason": reason,
                    "operator": operator,
                    "was_enabled": mfa.is_enabled,
                    "recovery_codes_deleted": codes,
                },
            )
            if event is None:
                raise CommandError(
                    "The audit row could not be written, so nothing was changed. "
                    "Check the logs for 'security.event write failed' and run it again."
                )
        self.stdout.write(f"  recorded as security event {event.event_id}.")
