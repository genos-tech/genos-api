"""Re-encrypt every encrypted column with the newest key (MIP-618).

Step 2 of a key rotation (the steps are in `origin/services/crypto.py`):
put the new key first in `FIELD_ENCRYPTION_KEYS`, redeploy every service
that holds it, run this, then drop the old key.

    python manage.py rotate_field_keys --dry-run    # count only, no writes
    python manage.py rotate_field_keys

Also encrypts any plaintext left in an `EncryptedTextField` column, which
is what a container still running the previous release writes during a
deploy. So it's safe, and useful, to run after any deploy that encrypts a
new column.

Idempotent: a second run finds nothing to do. Each row is rewritten with
a compare-and-swap on the value it read, so a write that lands meanwhile
wins and the row is counted as skipped; run it again to pick it up.

A value that no key in the ring can decrypt is reported and left as it
is, and the run fails, because dropping the old key now would lose
whatever that key protects. Values are never printed, only row ids.
"""

from __future__ import annotations

from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import connection

from origin.services import crypto

# Ciphertext held in a plain TextField, encrypted by hand at each write
# (`crypto.encrypt(...)`) rather than by the field. Every
# `EncryptedTextField` column is found on its own; a new hand-encrypted
# column has to be listed here, which `test_rotate_field_keys` checks.
MANUAL_COLUMNS = [
    ("origin.ConnectedAccount", "access_token_encrypted"),
    ("origin.ConnectedAccount", "refresh_token_encrypted"),
    ("origin.WebhookEndpoint", "secret_encrypted"),
]


def encrypted_columns() -> list[tuple[type, str, bool]]:
    """(model, field name, plaintext allowed) for every encrypted column.

    A plaintext value is only legitimate in an `EncryptedTextField`
    column, which reads it as-is. In a hand-encrypted one it means a bug
    that skipped `encrypt`, so it's reported rather than silently fixed.
    """
    columns = [
        (model, field.name, True)
        for model in apps.get_models()
        for field in model._meta.concrete_fields
        if isinstance(field, crypto.EncryptedTextField)
    ]
    for label, name in MANUAL_COLUMNS:
        columns.append((apps.get_model(label), name, False))
    return columns


class Command(BaseCommand):
    help = "Re-encrypt every encrypted column with the newest FIELD_ENCRYPTION_KEYS key."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Count what would change without writing.",
        )

    def handle(self, *args, **options):
        dry_run: bool = options["dry_run"]
        crypto.require_keyring()

        unreadable = []
        for model, name, plaintext_ok in encrypted_columns():
            label = f"{model._meta.label}.{name}"
            table = connection.ops.quote_name(model._meta.db_table)
            column = connection.ops.quote_name(model._meta.get_field(name).column)
            pk = connection.ops.quote_name(model._meta.pk.column)
            rotated = encrypted = skipped = 0

            # Raw SQL, not the ORM: through an EncryptedTextField the ORM
            # would hand back plaintext and encrypt again on save, so it
            # could never tell which key a value used.
            with connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {pk}, {column} FROM {table} WHERE {column} IS NOT NULL AND {column} <> ''"
                )
                rows = cursor.fetchall()

            for row_id, value in rows:
                if crypto.looks_encrypted(value):
                    try:
                        if not crypto.needs_rotation(value):
                            continue
                    except InvalidToken:
                        unreadable.append(f"{label} id={row_id}")
                        continue
                    new_value = crypto.rotate(value)
                    kind = "rotated"
                elif plaintext_ok:
                    new_value = crypto.encrypt(value)
                    kind = "encrypted"
                else:
                    unreadable.append(f"{label} id={row_id} (plaintext)")
                    continue

                if not dry_run:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE {table} SET {column} = %s WHERE {pk} = %s AND {column} = %s",
                            [new_value, row_id, value],
                        )
                        if cursor.rowcount == 0:
                            skipped += 1
                            continue
                if kind == "rotated":
                    rotated += 1
                else:
                    encrypted += 1

            verb = "would change" if dry_run else "changed"
            self.stdout.write(
                f"{label}: {verb} {rotated + encrypted} of {len(rows)} "
                f"(rotated {rotated}, encrypted {encrypted} plaintext, skipped {skipped})"
            )

        if unreadable:
            for entry in unreadable:
                self.stderr.write(f"cannot rotate: {entry}")
            raise CommandError(
                f"{len(unreadable)} value(s) could not be rotated. Keep every key in "
                "FIELD_ENCRYPTION_KEYS until they are dealt with."
            )
