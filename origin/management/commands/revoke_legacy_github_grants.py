"""Revoke the legacy GitHub OAuth grants that carry the `repo` scope (MIP-2091, phase 4 of the
migration in the design MIP-2057, section 6; the runbook is
`operations/GITHUB_APP_MIGRATION_RUNBOOK.md` in genos-docs).

    python manage.py revoke_legacy_github_grants                       # report only (the default)
    python manage.py revoke_legacy_github_grants --limit 5 --apply     # revoke a first few
    python manage.py revoke_legacy_github_grants --apply               # revoke every eligible one

A legacy grant is the never-expiring `repo` token Connect GitHub used to store on the person's
`github` row (the sign-in row, which is the same row). It is eligible once the person has moved to
the App (they hold a `github_app` connection). By default the command leaves alone:

  * people with no App connection (`--include-unmoved` includes them: they lose Genos' GitHub
    access until they connect the App);
  * people who still administer a legacy hook (`registered_by` of a registration): the reconcile
    cron and `rekey_github_webhooks` use that token for the hook. Deal with the hooks first
    (`migrate_github_hooks_to_app`), or pass `--include-registrars`.

`--apply` asks GitHub to delete the OAuth App's grant for each person (which also removes the app
from their "Authorized OAuth Apps": at their next sign-in with GitHub they see the consent screen
again, for `read:user user:email` only) and then clears the stored token and the `repo` scope. The
row, which is the sign-in identity, stays. A grant GitHub no longer knows counts as done, so a repeat
is safe. A failure leaves that row untouched, is counted, and makes the run exit non-zero.

Output is counts only: no user id, no name, no email, no token. Production: run it only with the
maintainer's go.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from origin.services import crypto
from origin.services.github_app_migration import (
    plan_grant_revocation,
    revoke_legacy_grant,
    summarize,
)

log = logging.getLogger(__name__)

_DONE = ("revoked", "already_invalid")


class Command(BaseCommand):
    help = "Report (default) or revoke the legacy GitHub OAuth grants with the `repo` scope."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Revoke. Without it nothing is changed."
        )
        parser.add_argument(
            "--limit",
            type=int,
            help="Revoke at most this many in one run (a first few, then more).",
        )
        parser.add_argument(
            "--include-unmoved",
            action="store_true",
            help="Also people with no GitHub App connection.",
        )
        parser.add_argument(
            "--include-registrars",
            action="store_true",
            help="Also people who still administer a legacy hook.",
        )

    def handle(self, *args, **options):
        apply: bool = options["apply"]
        limit = options["limit"]
        if limit is not None and limit < 1:
            raise CommandError("--limit must be at least 1.")

        plan = plan_grant_revocation(
            include_unmoved=options["include_unmoved"],
            include_registrars=options["include_registrars"],
        )
        batch = plan.eligible[:limit] if limit else plan.eligible
        self.stdout.write(
            f"{len(plan.eligible)} legacy grant(s) eligible"
            + (f", {len(batch)} in this run (--limit)" if len(batch) != len(plan.eligible) else "")
            + f"; {len(plan.registrars)} held back (still a hook's administrator); "
            f"{plan.unmoved} with no GitHub App connection"
            + (" (included)" if options["include_unmoved"] else " (not touched)")
        )
        log.info(
            "[github-grant-revoke] report eligible=%d registrars=%d unmoved=%d",
            len(plan.eligible),
            len(plan.registrars),
            plan.unmoved,
        )
        if not apply:
            self.stdout.write("Dry run: nothing was changed. Run with --apply to revoke.")
            return
        if not (settings.GITHUB_OAUTH_CLIENT_ID and settings.GITHUB_OAUTH_CLIENT_SECRET):
            raise CommandError(
                "The GitHub OAuth App's client id and secret are not set: GitHub cannot be asked "
                "to revoke a grant without them."
            )
        crypto.require_keyring()

        outcomes = summarize(revoke_legacy_grant(account) for account in batch)
        revoked = sum(outcomes.get(o, 0) for o in _DONE)
        self.stdout.write(
            f"revoked {outcomes.get('revoked', 0)}, already invalid "
            f"{outcomes.get('already_invalid', 0)}, not done {len(batch) - revoked} of {len(batch)}"
        )
        log.info(
            "[github-grant-revoke] apply attempted=%d revoked=%d not_done=%d",
            len(batch),
            revoked,
            len(batch) - revoked,
        )
        if revoked != len(batch):
            detail = ", ".join(
                f"{n} {outcome}" for outcome, n in sorted(outcomes.items()) if outcome not in _DONE
            )
            raise CommandError(
                f"{len(batch) - revoked} grant(s) were not revoked ({detail}). Their rows are "
                "unchanged; fix the cause and run again."
            )
