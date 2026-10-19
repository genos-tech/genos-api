# Web Push subscription keys are encrypted at rest from here on
# (MIP-618), and the legacy `CustomUser.token` fields leave the model.
#
# Push keys: `p256dh` and `auth` together let anyone read every push that
# browser receives, and a database dump alone used to hand them over. The
# columns become `EncryptedTextField` and the existing rows are encrypted
# here. A row the previous release writes while this one deploys is
# plaintext; the field reads it as-is, and `rotate_field_keys` encrypts it
# (so does the browser's own re-registration, sent on every app load).
#
# Encrypting needs FIELD_ENCRYPTION_KEYS. With rows to encrypt and no key,
# this stops rather than leaving them readable; an empty table (CI, a
# fresh dev database) needs no key. The ALTER from varchar to text is a
# catalog change on Postgres, not a table rewrite.
#
# `token` / `token_expiration`: nothing reads or writes them any more
# (account deletion only cleared them), but the user serializer still
# returned them. They leave Django's state here and their values are
# cleared. The columns stay until MIP-757 drops them, because Railway runs
# this migration while the previous release is still serving, and that
# release selects every column of the user table.

from django.db import migrations

import origin.services.crypto
from origin.services import crypto


def _encrypt(value):
    if not value or crypto.looks_encrypted(value):
        return value
    return crypto.encrypt(value)


def _decrypt(value):
    if not value or not crypto.looks_encrypted(value):
        return value
    return crypto.decrypt(value)


def _map_push_keys(connection, convert):
    # Raw SQL both ways: through the ORM, the field would decrypt on read
    # and encrypt again on write. The ALTER above holds the table lock
    # until this transaction commits, so no write can land in between.
    with connection.cursor() as cursor:
        cursor.execute("SELECT id, p256dh, auth FROM origin_pushsubscription")
        for pk, p256dh, auth in cursor.fetchall():
            new = (convert(p256dh), convert(auth))
            if new != (p256dh, auth):
                cursor.execute(
                    "UPDATE origin_pushsubscription SET p256dh = %s, auth = %s WHERE id = %s",
                    [*new, pk],
                )


def encrypt_push_keys(apps, schema_editor):
    _map_push_keys(schema_editor.connection, _encrypt)


def decrypt_push_keys(apps, schema_editor):
    # So a rollback hands the previous release the plaintext it expects.
    _map_push_keys(schema_editor.connection, _decrypt)


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0198_security_event"),
    ]

    operations = [
        migrations.AlterField(
            model_name="pushsubscription",
            name="auth",
            field=origin.services.crypto.EncryptedTextField(),
        ),
        migrations.AlterField(
            model_name="pushsubscription",
            name="p256dh",
            field=origin.services.crypto.EncryptedTextField(),
        ),
        migrations.RunPython(encrypt_push_keys, decrypt_push_keys),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "UPDATE origin_customuser SET token = NULL, token_expiration = NULL"
                    " WHERE token IS NOT NULL OR token_expiration IS NOT NULL",
                    migrations.RunSQL.noop,
                ),
            ],
            state_operations=[
                migrations.RemoveField(model_name="customuser", name="token"),
                migrations.RemoveField(model_name="customuser", name="token_expiration"),
            ],
        ),
    ]
