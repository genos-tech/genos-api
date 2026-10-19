"""Which uploads the database points at but storage doesn't have (MIP-620).

Postgres and the media volume are backed up separately, so a restore can
bring back rows whose files were never in the volume backup (or a volume
from before those rows existed). Nothing errors until someone opens the
file and gets a 404. Run this after restoring either one:

    python manage.py media_check          # per-field counts
    python manage.py media_check --list   # also every missing path

It reads every FileField on every model, so a new upload field is covered
without touching this command. Inline chat images (`chats/<id>/inline/`)
have no row, only a URL inside the message body, so they aren't checked.

Exits 1 when anything is missing, so a restore script can stop on it.
"""

from __future__ import annotations

from django.apps import apps
from django.core.exceptions import SuspiciousFileOperation
from django.core.management.base import BaseCommand, CommandError
from django.db import models


class Command(BaseCommand):
    help = "List rows whose uploaded file is missing from media storage."

    def add_arguments(self, parser):
        parser.add_argument(
            "--list", action="store_true", dest="show_paths", help="Print every missing path."
        )

    def handle(self, *args, show_paths, **options):
        total_missing = 0
        for model in apps.get_models():
            for field in model._meta.get_fields():
                if not isinstance(field, models.FileField):
                    continue
                rows = (
                    model._default_manager.exclude(**{field.attname: ""})
                    .exclude(**{f"{field.attname}__isnull": True})
                    .values_list("pk", field.attname)
                )
                checked = 0
                missing = []
                for pk, name in rows.iterator(chunk_size=2000):
                    checked += 1
                    try:
                        present = field.storage.exists(name)
                    except SuspiciousFileOperation:
                        # A path outside MEDIA_ROOT can't be served either.
                        present = False
                    if not present:
                        missing.append((pk, name))
                if not checked:
                    continue
                label = f"{model._meta.label}.{field.name}"
                self.stdout.write(f"{label}: {checked} checked, {len(missing)} missing")
                if show_paths:
                    for pk, name in missing:
                        self.stdout.write(f"  {pk}  {name}")
                total_missing += len(missing)
        if total_missing:
            raise CommandError(f"{total_missing} file(s) missing from media storage.")
        self.stdout.write("Every referenced file is present.")
