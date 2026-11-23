"""`python manage.py sync_official_skills`: publish the official Genos skills (MIP-2048).

    python manage.py sync_official_skills                  # dry run (the default): says what it would do
    python manage.py sync_official_skills --apply          # writes
    python manage.py sync_official_skills --only triage    # one skill (repeatable)

**Dry run is the default.** Without `--apply` nothing is written: not the official account, not a skill.

What it does: makes one public skill per translated runbook of the Skills v2 pack
(`origin/services/skills/official_catalog/`), owned by a Genos system account that it creates on the
first `--apply`. A second run changes nothing; a catalog from a newer plugin release cuts a new
version of each changed skill and keeps its stars and installs. It touches only skills owned by that
account, never a member's, and leaves a skill an operator held, hid or took down as it is. Full rules:
`origin/services/skills/official.py`.

Visibility is NOT this command's: official skills are public skills, so nobody sees one until
`GENOS_SKILLS_PUBLIC` is on (and `GENOS_SKILLS` for the in-app run). Running it with the switches off
is safe.

Output is counts and the catalog's own slugs: no account, team or person is named. Refresh the
catalog (not this command) with `scripts/build_official_skills.py` when the plugin has a new release.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from origin.services.skills import official


class Command(BaseCommand):
    help = "Create or update the official Genos skills. Dry run unless --apply is given."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true", help="Write. Without this flag nothing is changed."
        )
        parser.add_argument(
            "--only",
            action="append",
            default=[],
            metavar="SLUG",
            help="Only this official skill (repeatable).",
        )

    def handle(self, *args, **options):
        try:
            report = official.sync(apply=options["apply"], only=set(options["only"]) or None)
        except official.CatalogError as exc:
            raise CommandError(str(exc)) from exc
        mode = "applied" if report.applied else "dry run, nothing written"
        self.stdout.write(f"official skills ({mode})")
        for action in report.actions:
            flagged = f"  [scan: {', '.join(action.scan_codes)}]" if action.scan_codes else ""
            self.stdout.write(f"  {action.kind:<9} {action.slug}  {action.detail}{flagged}")
        for slug in report.orphans:
            self.stdout.write(f"  orphan    {slug}  not in the catalog any more, left as it is")
        self.stdout.write(
            f"{report.count(official.CREATE)} to create, {report.count(official.UPDATE)} to update, "
            f"{report.count(official.UNCHANGED)} unchanged, {report.count(official.SKIPPED)} skipped"
            if not report.applied
            else f"{report.count(official.CREATE)} created, {report.count(official.UPDATE)} updated, "
            f"{report.count(official.UNCHANGED)} unchanged, {report.count(official.SKIPPED)} skipped"
        )
        if not report.applied:
            self.stdout.write("Run again with --apply to write.")
