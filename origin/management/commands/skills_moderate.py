"""The operator queue of Genos Skills, and the takedown (MIP-2044).

The Django admin is not installed in this project (`apis/settings.py`, `apis/urls.py`), so the queue
is this command, run where the other operator commands run. It lists what waits for an operator and
makes the two decisions, through `services/skills/moderation.py` (the same functions as the staff API
`POST` / `DELETE /api/v2/skills/<id>/takedown/`):

    python manage.py skills_moderate                                 # the queue (reads only)
    python manage.py skills_moderate --show <skill-id>               # the text readers get, the scan, the reports
    python manage.py skills_moderate --takedown <skill-id> --reason "..." --operator <staff username>
    python manage.py skills_moderate --restore  <skill-id> [--reason "..."] --operator <staff username>

Dry-run by default: a decision is written only with `--apply`. A takedown stops installs, team runs
and the picker at once (every reader and every run checks the status on each request). The reason is
shown to the author on their skill. Never prints an account's email.
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from origin.models.skills.skill_models import (
    REPORT_OPEN,
    STATUS_IN_REVIEW,
    STATUS_REPORTED_HIDDEN,
    STATUS_TAKEN_DOWN,
    Skill,
    SkillReport,
)
from origin.services.skills import moderation, service

QUEUE_STATUSES = (STATUS_IN_REVIEW, STATUS_REPORTED_HIDDEN, STATUS_TAKEN_DOWN)


class Command(BaseCommand):
    help = "List the Genos Skills waiting for an operator; take a skill down or put it back."

    def add_arguments(self, parser):
        parser.add_argument("--show", metavar="SKILL_ID", help="Show one skill in full.")
        parser.add_argument("--takedown", metavar="SKILL_ID", help="Stop this skill for everybody.")
        parser.add_argument("--restore", metavar="SKILL_ID", help="Put a held skill back.")
        parser.add_argument("--reason", default="", help="The sentence the author is shown.")
        parser.add_argument("--operator", default="", help="The staff account taking the decision.")
        parser.add_argument("--apply", action="store_true", help="Write the decision.")

    def handle(self, *args, **options):
        if options["show"]:
            return self._show(options["show"])
        if options["takedown"] or options["restore"]:
            return self._decide(options)
        return self._queue()

    # -- reads -----------------------------------------------------------------------------

    def _queue(self):
        rows = Skill.objects.filter(status__in=QUEUE_STATUSES).order_by("-updated_at")
        if not rows:
            self.stdout.write("Nothing waits for an operator.")
            return
        for skill in rows:
            reports = skill.reports.filter(status=REPORT_OPEN)
            reasons = ", ".join(sorted({r.reason for r in reports})) or "-"
            self.stdout.write(
                f"{skill.id}  /{skill.slug}  {skill.visibility}  {skill.status}  "
                f"open reports: {reports.count()} ({reasons})"
            )

    def _show(self, raw_id):
        skill = Skill.objects.filter(pk=moderation.pk_of(raw_id)).first()
        if skill is None:
            raise CommandError("No such skill.")
        self.stdout.write(f"/{skill.slug}  {skill.title}  [{skill.visibility}, {skill.status}]")
        if skill.moderation_reason:
            self.stdout.write(f"Last decision: {skill.moderation_reason}")
        version = skill.versions.order_by("-version_no").first()
        if version is not None:
            self.stdout.write(f"Newest version {version.version_no}, scan: {version.scan_result}")
        published = skill.versions.filter(version_no=skill.published_version_no).first()
        self.stdout.write("--- the text readers get ---")
        self.stdout.write(published.body_markdown if published else "(none published)")
        self.stdout.write("--- open reports ---")
        for report in SkillReport.objects.filter(skill=skill, status=REPORT_OPEN):
            self.stdout.write(f"{report.created_at:%Y-%m-%d}  {report.reason}  {report.text}")

    # -- decisions -------------------------------------------------------------------------

    def _decide(self, options):
        takedown = bool(options["takedown"])
        raw_id = options["takedown"] or options["restore"]
        verb = "take down" if takedown else "restore"
        if not options["apply"]:
            self.stdout.write(f"Dry run: would {verb} {raw_id}. Pass --apply and --operator.")
            return
        operator = (
            get_user_model().objects.filter(username=options["operator"], is_staff=True).first()
        )
        if operator is None:
            raise CommandError("--operator must be the username of a staff account.")
        try:
            skill = (
                moderation.take_down(operator, raw_id, options["reason"])
                if takedown
                else moderation.restore(operator, raw_id, options["reason"])
            )
        except service.SkillError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(f"{skill.id} is now {skill.status}.")
