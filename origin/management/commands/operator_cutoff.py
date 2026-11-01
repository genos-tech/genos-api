"""`python manage.py operator_cutoff` — cut a user or API keys off, with an audit row (MIP-865).

The first hour of a breach is spent at a shell, and until this existed
the actions taken there (`update(is_active=False)`, `ApiKey.revoke()`)
left no `SecurityEvent`: the trail covered everything except what the
operator did about the incident. Each subcommand here acts AND records,
with `SecuritySource.OPERATOR`, the operator's OS user, and the reason.

Usage:
    python manage.py operator_cutoff user <user_id> --reason "<why>" [--dry-run]
    python manage.py operator_cutoff api-key <key_id_or_prefix> --reason "<why>" [--dry-run]
    python manage.py operator_cutoff team-keys <team_id> --reason "<why>" [--dry-run]

`user`       sets `is_active=False` (simplejwt re-reads it on every
             request, so an access token stops working at once),
             blacklists the user's outstanding refresh tokens so the
             session cannot be renewed, and revokes every live API key
             the user holds (personal and team).
`api-key`    revokes one key, named by its id or by the display prefix
             shown in the key list (`gnos_ab12cd`). NEVER pass the key
             itself: it is not looked up that way and the argument is
             never echoed, but it would still land in shell history.
`team-keys`  revokes every live API key bound to the team, whoever holds
             it. Personal tokens are untouched, as they are when a member
             leaves (`revoke_team_api_keys`).

Every subcommand needs `--reason`. `--dry-run` prints what would change and
writes nothing at all, no audit row included.

**Idempotent.** A target that is already cut off is reported as such and
writes no second event. A user who is inactive but still has a live key is
not "already cut off": the rest is done and recorded.

**No audit row, no cut-off.** The action and its event commit together;
if the event cannot be written the action is rolled back and the command
fails, so a re-run can be tried. (An operator who cannot wait can still
use the shell, and knows they are off the record.)

**Exit codes.** Non-zero (1) for an unknown target, a missing/blank
`--reason`, or a failure to write the audit row; argparse's own 2 for a
usage error. Zero for success, for a dry-run, and for "already done".

Prints ids and counts only: no emails, no key material, no tokens.
"""

from __future__ import annotations

import argparse
import getpass
import uuid

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from rest_framework_simplejwt.token_blacklist.models import BlacklistedToken, OutstandingToken

from origin.models.common.api_key_models import ApiKey
from origin.models.common.security_event_models import SecurityAction, SecuritySource
from origin.models.common.team_models import TeamMaster
from origin.services.pii_mask import mask_email
from origin.services.security_events import record_security_event
from origin.services.team_membership import revoke_team_api_keys

User = get_user_model()

_REASON_MAX = 1000
# A team can hold many keys; the row lists the first of them and the total.
_KEY_IDS_MAX = 200


def _operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _uuid(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw).strip())
    except (ValueError, AttributeError):
        raise CommandError(f"{what} must be a UUID.") from None


def _clean_reason(raw: str) -> str:
    reason = (raw or "").strip()
    if not reason:
        raise CommandError("--reason must not be blank.")
    # Free text typed at a shell: an address in it would sit in the audit
    # row in plain (MIP-765).
    return mask_email(reason)[:_REASON_MAX]


class Command(BaseCommand):
    help = "Incident response: cut off a user or API keys and write an operator SecurityEvent."

    def add_arguments(self, parser):
        common = self._common()
        sub = parser.add_subparsers(dest="target", metavar="{user,api-key,team-keys}")
        sub.required = True
        p = sub.add_parser("user", parents=[common], help="Deactivate a user and end their access.")
        p.add_argument("user_id", help="The user's id (UUID).")
        p = sub.add_parser("api-key", parents=[common], help="Revoke one API key.")
        p.add_argument("key", help="The key's id (UUID) or its display prefix (gnos_xxxxxx).")
        p = sub.add_parser("team-keys", parents=[common], help="Revoke every live key of a team.")
        p.add_argument("team_id", help="The team's id (UUID).")

    @staticmethod
    def _common():
        common = argparse.ArgumentParser(add_help=False)
        common.add_argument("--reason", required=True, help="Why. Recorded in the audit row.")
        common.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change; change and record nothing.",
        )
        return common

    def handle(self, *args, **opts):
        reason = _clean_reason(opts["reason"])
        self.operator = _operator()
        self.reason = reason
        self.dry_run = bool(opts["dry_run"])
        target = opts["target"]
        if target == "user":
            self._user(_uuid(opts["user_id"], "user_id"))
        elif target == "api-key":
            self._api_key(opts["key"])
        else:
            self._team_keys(_uuid(opts["team_id"], "team_id"))

    # -- helpers -----------------------------------------------------------

    def _say(self, line: str) -> None:
        self.stdout.write(("[dry-run] " if self.dry_run else "") + line)

    def _metadata(self, **extra) -> dict:
        return {"reason": self.reason, "operator": self.operator, **extra}

    def _record(self, action, **kwargs):
        event = record_security_event(
            action,
            actor_label=f"operator:{self.operator}",
            force_source=SecuritySource.OPERATOR,
            **kwargs,
        )
        if event is None:
            # Inside the caller's `atomic`, so raising undoes the action.
            raise CommandError(
                "The audit row could not be written, so nothing was changed. "
                "Check the logs for 'security.event write failed' and run it again."
            )
        return event

    # -- user --------------------------------------------------------------

    def _user(self, user_id: uuid.UUID) -> None:
        user = User._default_manager.filter(pk=user_id).first()
        if user is None:
            raise CommandError(f"No user with id {user_id}.")
        now = timezone.now()
        keys = list(ApiKey.objects.filter(user_id=user_id, revoked_at__isnull=True))
        tokens = list(
            OutstandingToken.objects.filter(
                user_id=user_id, expires_at__gt=now, blacklistedtoken__isnull=True
            )
        )
        if not user.is_active and not keys and not tokens:
            self._say(f"user {user_id}: already cut off (inactive, no live keys or sessions).")
            return
        was_active = bool(user.is_active)
        self._say(
            f"user {user_id}: "
            f"{'deactivate' if was_active else 'already inactive'}; "
            f"revoke {len(keys)} API key(s); blacklist {len(tokens)} refresh token(s)."
        )
        if self.dry_run:
            return
        with transaction.atomic():
            if was_active:
                User._default_manager.filter(pk=user_id).update(is_active=False)
            for key in keys:
                key.revoke()
            BlacklistedToken.objects.bulk_create(
                [BlacklistedToken(token=t) for t in tokens], ignore_conflicts=True
            )
            event = self._record(
                SecurityAction.OPERATOR_USER_DEACTIVATED,
                target_type="user",
                target_id=user_id,
                metadata=self._metadata(
                    user_id=str(user_id),
                    was_active=was_active,
                    api_keys_revoked=len(keys),
                    api_key_ids=[str(k.id) for k in keys[:_KEY_IDS_MAX]],
                    refresh_tokens_blacklisted=len(tokens),
                ),
            )
        self.stdout.write(f"  recorded as security event {event.event_id}.")

    # -- one key -----------------------------------------------------------

    def _find_key(self, raw: str) -> ApiKey:
        raw = (raw or "").strip()
        try:
            key = ApiKey.objects.filter(pk=uuid.UUID(raw)).first()
        except ValueError:
            matches = list(ApiKey.objects.filter(prefix=raw)[:3]) if raw else []
            if len(matches) > 1:
                ids = ", ".join(str(k.id) for k in matches)
                raise CommandError(
                    f"That prefix matches several keys; use an id. Candidates: {ids}."
                )
            key = matches[0] if matches else None
        if key is None:
            # The argument is not echoed: it may be a key pasted by mistake.
            raise CommandError("No API key matches that id or prefix.")
        return key

    def _api_key(self, raw: str) -> None:
        key = self._find_key(raw)
        if key.revoked_at is not None:
            self._say(
                f"api key {key.id}: already revoked (at {key.revoked_at:%Y-%m-%d %H:%M} UTC)."
            )
            return
        self._say(f"api key {key.id} (owner {key.user_id}, team {key.team_id or '-'}): revoke.")
        if self.dry_run:
            return
        with transaction.atomic():
            key.revoke()
            event = self._record(
                SecurityAction.OPERATOR_API_KEY_REVOKED,
                team_id=key.team_id,
                target_type="api_key",
                target_id=key.id,
                target_label=f"{key.name} ({key.prefix})",
                metadata=self._metadata(
                    api_key_id=str(key.id),
                    owner_id=str(key.user_id),
                    team_id=str(key.team_id) if key.team_id else None,
                ),
            )
        self.stdout.write(f"  recorded as security event {event.event_id}.")

    # -- a team's keys -----------------------------------------------------

    def _team_keys(self, team_id: uuid.UUID) -> None:
        if not TeamMaster.objects.filter(team_id=team_id).exists():
            raise CommandError(f"No team with id {team_id}.")
        keys = list(ApiKey.objects.filter(team_id=team_id, revoked_at__isnull=True))
        if not keys:
            self._say(f"team {team_id}: already clear (no live team API keys).")
            return
        holders = sorted({k.user_id for k in keys}, key=str)
        self._say(f"team {team_id}: revoke {len(keys)} API key(s) held by {len(holders)} user(s).")
        if self.dry_run:
            return
        with transaction.atomic():
            # The same call a member leaving the team makes.
            revoked = sum(revoke_team_api_keys(team_id, uid) for uid in holders)
            event = self._record(
                SecurityAction.OPERATOR_TEAM_KEYS_REVOKED,
                team_id=team_id,
                target_type="team",
                target_id=team_id,
                metadata=self._metadata(
                    team_id=str(team_id),
                    api_keys_revoked=revoked,
                    api_key_ids=[str(k.id) for k in keys[:_KEY_IDS_MAX]],
                    holder_ids=[str(u) for u in holders[:_KEY_IDS_MAX]],
                ),
            )
        self.stdout.write(f"  revoked {revoked}; recorded as security event {event.event_id}.")
