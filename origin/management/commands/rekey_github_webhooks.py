"""Give every GitHub webhook still on the shared secret its own (MIP-618).

Hooks registered before per-repo secrets sign with GITHUB_WEBHOOK_SECRET,
so that one value leaking still forges events for every such repo. Each
is re-keyed on the next task save that links its repo anyway; this does
all of them now, through the GitHub connection of whoever registered the
hook.

    python manage.py rekey_github_webhooks --dry-run    # list, no changes
    python manage.py rekey_github_webhooks

Safe to repeat: a re-keyed hook is skipped. A hook it can't re-key (the
registrar disconnected GitHub, lost admin, or the hook was deleted on
GitHub) is listed and the run fails, because unsetting the shared secret
then would stop that repo's deliveries. Linking a PR of the repo as a
repo admin re-keys it too.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from origin.models.common.user_models import GithubWebhookRegistration
from origin.services import crypto
from origin.services.github_webhooks import hook_admin_headers, rekey_hook


class Command(BaseCommand):
    help = "Give every GitHub webhook still signing with GITHUB_WEBHOOK_SECRET its own secret."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the hooks still on the shared secret without changing them.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        legacy = list(
            GithubWebhookRegistration.objects.filter(accepts_shared_secret=True).order_by(
                "owner", "repo"
            )
        )
        if dry_run:
            for registration in legacy:
                self.stdout.write(f"would re-key {registration.owner}/{registration.repo}")
            self.stdout.write(f"{len(legacy)} hook(s) still on the shared secret")
            return

        # The new secret is stored encrypted.
        crypto.require_keyring()
        failed = []
        for registration in legacy:
            name = f"{registration.owner}/{registration.repo}"
            headers = hook_admin_headers(None, registration)
            if headers is None:
                failed.append(f"{name} (the registrar has no usable GitHub connection)")
            elif not rekey_hook(registration, headers):
                failed.append(f"{name} (GitHub refused or was unreachable; see the log)")
            else:
                self.stdout.write(f"re-keyed {name}")
        self.stdout.write(f"re-keyed {len(legacy) - len(failed)} of {len(legacy)}")

        if failed:
            for entry in failed:
                self.stderr.write(f"still on the shared secret: {entry}")
            raise CommandError(
                f"{len(failed)} hook(s) still verify with GITHUB_WEBHOOK_SECRET. "
                "Keep it set until they are re-keyed."
            )
