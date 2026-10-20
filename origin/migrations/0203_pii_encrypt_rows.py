# Encrypts the PII already stored (MIP-619): the rows 0202's columns held
# as plaintext.
#
# Not atomic: each batch commits on its own, so the user table, read by
# every request, is never locked for the length of the run. Each row is
# rewritten with a compare-and-swap on the value read, so a write the
# previous release makes meanwhile wins; that write is plaintext, which
# the field reads as-is and `rotate_field_keys` encrypts. Safe to rerun
# after a failure: a value already encrypted is skipped.
#
# Encrypting needs FIELD_ENCRYPTION_KEYS. With rows to encrypt and no key,
# this stops before writing anything rather than leaving them readable; an
# empty table (CI, a fresh dev database) needs no key.
#
# The reverse decrypts, so a rollback hands the previous release the
# plaintext it can also read. It needs every key the rows were encrypted
# with. A value that looks like a token but that no key opens was stored
# as plaintext, so it is left as it is in both directions.

from cryptography.fernet import InvalidToken
from django.db import migrations, transaction

from origin.services import crypto

# (table, primary key, column)
_COLUMNS = [
    ("origin_customuser", "id", "email"),
    ("origin_customuser", "id", "phone_number"),
    ("origin_customuser", "id", "base_country"),
    ("origin_customuser", "id", "current_location"),
    ("origin_customuser", "id", "about_me"),
    ("origin_connectedaccount", "id", "provider_email"),
    ("origin_teammaster", "team_id", "team_email"),
    ("origin_teaminvite", "id", "invited_email"),
    ("origin_emailsuppression", "id", "address"),
]

_BATCH = 500


def _encrypt(value):
    if crypto.looks_encrypted(value):
        return value
    return crypto.encrypt(value)


def _decrypt(value):
    if not crypto.looks_encrypted(value):
        return value
    try:
        return crypto.decrypt(value)
    except InvalidToken:
        return value


def _convert(connection, convert):
    # Raw SQL: through the ORM, the field would decrypt on read and
    # encrypt again on write.
    for table, pk, column in _COLUMNS:
        last = None
        while True:
            with transaction.atomic(using=connection.alias), connection.cursor() as cursor:
                after = "" if last is None else f"AND {pk} > %s"
                cursor.execute(
                    f"SELECT {pk}, {column} FROM {table} "
                    f"WHERE {column} IS NOT NULL AND {column} <> '' {after} "
                    f"ORDER BY {pk} LIMIT {_BATCH}",
                    [] if last is None else [last],
                )
                rows = cursor.fetchall()
                for row_id, value in rows:
                    new = convert(value)
                    if new != value:
                        cursor.execute(
                            f"UPDATE {table} SET {column} = %s WHERE {pk} = %s AND {column} = %s",
                            [new, row_id, value],
                        )
            if len(rows) < _BATCH:
                break
            last = rows[-1][0]


def encrypt_rows(apps, schema_editor):
    _convert(schema_editor.connection, _encrypt)


def decrypt_rows(apps, schema_editor):
    _convert(schema_editor.connection, _decrypt)


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("origin", "0202_pii_encrypt_columns"),
    ]

    operations = [
        migrations.RunPython(encrypt_rows, decrypt_rows),
    ]
