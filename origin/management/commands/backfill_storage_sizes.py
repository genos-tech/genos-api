"""Fill in the byte size of uploads stored before sizes were recorded (MIP-1962).

New uploads record their size at upload time. Older rows have 0; this reads each file's size from
storage (`storage.size()`, no file content is read) and writes it.

    python manage.py backfill_storage_sizes                # DRY RUN (default): counts and byte totals
    python manage.py backfill_storage_sizes --apply --yes  # write

DRY RUN IS THE DEFAULT. It prints per-kind counts and byte totals only, never a name, path or id.
Only rows whose size is 0 are touched, so a second run updates nothing and a size set at upload is
never overwritten. A file that is missing from storage is counted and left at 0. PRODUCTION: run
only with the owner's go.
"""

from __future__ import annotations

from django.core.exceptions import SuspiciousFileOperation
from django.core.management.base import BaseCommand, CommandError

from origin.services.storage_usage import KINDS, _with_file

CHUNK = 1000


class Command(BaseCommand):
    help = "Record the byte size of old uploads from storage (dry run unless --apply --yes)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the sizes.")
        parser.add_argument("--yes", action="store_true", help="Confirm --apply.")

    def handle(self, *args, apply, yes, **options):
        if apply and not yes:
            raise CommandError("--apply writes to the database: add --yes.")
        self.stdout.write("APPLY" if apply else "DRY RUN (nothing is written; add --apply --yes)")
        grand = {"checked": 0, "updated": 0, "missing": 0, "bytes": 0}
        for kind in KINDS:
            model = kind.model
            field = model._meta.get_field(kind.file_attname)
            rows = _with_file(kind).filter(**{kind.size_attname: 0})
            checked = updated = missing = total = 0
            batch = []
            for pk, name in rows.values_list("pk", kind.file_attname).iterator(chunk_size=CHUNK):
                checked += 1
                try:
                    size = field.storage.size(name)
                except (OSError, SuspiciousFileOperation):
                    missing += 1
                    continue
                if size <= 0:
                    continue
                updated += 1
                total += size
                batch.append((pk, size))
                if apply and len(batch) >= CHUNK:
                    self._write(kind, batch)
                    batch = []
            if apply and batch:
                self._write(kind, batch)
            if checked:
                verb = "updated" if apply else "would be updated"
                self.stdout.write(
                    f"{kind.label}: {checked} checked, {updated} {verb}, "
                    f"{missing} missing, {total} bytes"
                )
            for key, value in (
                ("checked", checked),
                ("updated", updated),
                ("missing", missing),
                ("bytes", total),
            ):
                grand[key] += value
        self.stdout.write(
            f"TOTAL: {grand['checked']} checked, {grand['updated']} updated, "
            f"{grand['missing']} missing, {grand['bytes']} bytes"
        )

    def _write(self, kind, batch):
        model = kind.model
        objs = [model(pk=pk, **{kind.size_attname: size}) for pk, size in batch]
        model._default_manager.bulk_update(objs, [kind.size_attname])
