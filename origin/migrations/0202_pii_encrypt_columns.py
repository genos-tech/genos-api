# PII is encrypted at rest from here on (MIP-619), the second of the two
# releases. The first (0201) moved every lookup and uniqueness check onto
# blind indexes; this one makes the columns `Encrypted*` fields, and 0203
# encrypts the rows already there.
#
# Schema: each column becomes `text`, since ciphertext is several times
# longer than the plaintext limits (the fields still enforce those). The
# unique constraints on `email`, `team_email` and `address` are dropped,
# along with the (team, invited_email) index: a value encrypted with a
# random IV can't be compared, so the hash columns 0201 added carry them
# now. Each ALTER from varchar to text is a catalog change on Postgres,
# not a table rewrite, and no index is left on these columns to rebuild.
#
# Hashes: a row the release before 0201 wrote during that deploy has no
# hash, and was found by its plaintext. Lookups no longer fall back to
# it, so those hashes are filled in here first. Like 0201, this stops
# with the row ids if one would share an address with another row, and
# needs BLIND_INDEX_KEYS only when there is something to hash.

from django.db import migrations

import origin.services.crypto
from origin.services import blind_index

# (table, primary key, source column, hash column, index, unique)
_INDEXES = [
    ("origin_customuser", "id", "email", "email_hash", blind_index.USER_EMAIL, True),
    (
        "origin_teammaster",
        "team_id",
        "team_email",
        "team_email_hash",
        blind_index.TEAM_EMAIL,
        True,
    ),
    (
        "origin_teaminvite",
        "id",
        "invited_email",
        "invited_email_hash",
        blind_index.INVITE_EMAIL,
        False,
    ),
    (
        "origin_emailsuppression",
        "id",
        "address",
        "address_hash",
        blind_index.SUPPRESSED_ADDRESS,
        True,
    ),
]


def fill_missing_hashes(apps, schema_editor):
    # Raw SQL, as in 0201. The columns still hold plaintext here: only
    # 0203 encrypts them.
    with schema_editor.connection.cursor() as cursor:
        for table, pk, source, column, index, unique in _INDEXES:
            cursor.execute(
                f"SELECT {pk}, {source} FROM {table} WHERE {column} IS NULL AND {source} <> ''"
            )
            hashes = {
                row_id: digest
                for row_id, value in cursor.fetchall()
                # None: only spaces, which is stored without a hash.
                if (digest := index.compute(value)) is not None
            }
            if not hashes:
                continue
            if unique:
                cursor.execute(
                    f"SELECT {pk}, {column} FROM {table} WHERE {column} = ANY(%s)",
                    [list(hashes.values())],
                )
                seen = {digest: row_id for row_id, digest in cursor.fetchall()}
                clashes = set()
                for row_id, digest in hashes.items():
                    if digest in seen:
                        clashes.update({seen[digest], row_id})
                    seen[digest] = row_id
                if clashes:
                    rule = "ignoring case and spaces" if index.fold_case else "ignoring spaces"
                    raise RuntimeError(
                        f"{table}.{source}: {len(clashes)} rows share an address, {rule} "
                        f"(ids {sorted(map(str, clashes))}). Resolve them by hand, then "
                        "deploy again; see migration 0201's header."
                    )
            for row_id, digest in hashes.items():
                cursor.execute(
                    f"UPDATE {table} SET {column} = %s WHERE {pk} = %s",
                    [digest, row_id],
                )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0201_pii_blind_index"),
    ]

    operations = [
        # First, so it runs before the ALTERs below take their table locks.
        migrations.RunPython(fill_missing_hashes, migrations.RunPython.noop),
        migrations.RemoveIndex(
            model_name="teaminvite",
            name="origin_team_team_id_21892b_idx",
        ),
        migrations.AlterField(
            model_name="connectedaccount",
            name="provider_email",
            field=origin.services.crypto.EncryptedEmailField(blank=True, max_length=254, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="about_me",
            field=origin.services.crypto.EncryptedPIITextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="base_country",
            field=origin.services.crypto.EncryptedCharField(blank=True, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="current_location",
            field=origin.services.crypto.EncryptedCharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="email",
            field=origin.services.crypto.EncryptedEmailField(max_length=254),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="phone_number",
            field=origin.services.crypto.EncryptedCharField(blank=True, max_length=20, null=True),
        ),
        migrations.AlterField(
            model_name="emailsuppression",
            name="address",
            field=origin.services.crypto.EncryptedEmailField(max_length=254),
        ),
        migrations.AlterField(
            model_name="teaminvite",
            name="invited_email",
            field=origin.services.crypto.EncryptedEmailField(max_length=254),
        ),
        migrations.AlterField(
            model_name="teammaster",
            name="team_email",
            field=origin.services.crypto.EncryptedEmailField(max_length=254),
        ),
    ]
