# Email lookups move onto blind indexes (MIP-619), the first of the two
# releases that encrypt PII at rest.
#
# This release: each searchable address gets a sibling column holding an
# HMAC of its canonical form (`origin/services/blind_index.py`), filled in
# here, and every lookup and uniqueness check goes through it. The PII
# columns become `Decrypting*` fields: they still store plaintext, but can
# read ciphertext. The next release encrypts them, and Railway runs its
# migration while this release is still serving, so this one has to be
# able to read the result.
#
# Uniqueness: the user, suppression and invite hashes fold case, as every
# lookup on those addresses already did (`iexact`, or a value stored
# lowercased). The old unique constraint on `CustomUser.email` was
# case-sensitive, so it may hold two accounts whose addresses differ only
# in case. This migration stops if it finds any: merging two people's
# accounts is not something a migration should decide. Find them with
#
#   SELECT lower(trim(email)), array_agg(id) FROM origin_customuser
#   GROUP BY 1 HAVING count(*) > 1;
#
# `team_email` keeps its case-sensitive rule: it's built from the team
# name, which is unique case-sensitively.
#
# Hashing needs BLIND_INDEX_KEYS. With rows to hash and no key, this stops
# rather than leaving every account unfindable; an empty table (CI, a
# fresh dev database) needs no key. A row the previous release inserts
# during the deploy has no hash: lookups fall back to its plaintext, and
# `rotate_field_keys` fills it in.
#
# The column types don't change here. The encrypting release widens them
# to text, since ciphertext is far longer than the plaintext limits.

from django.db import migrations, models

import origin.services.crypto
from origin.services import blind_index

# (table, primary key, source column, hash column, index)
_INDEXES = [
    ("origin_customuser", "id", "email", "email_hash", blind_index.USER_EMAIL),
    ("origin_teammaster", "team_id", "team_email", "team_email_hash", blind_index.TEAM_EMAIL),
    ("origin_teaminvite", "id", "invited_email", "invited_email_hash", blind_index.INVITE_EMAIL),
    (
        "origin_emailsuppression",
        "id",
        "address",
        "address_hash",
        blind_index.SUPPRESSED_ADDRESS,
    ),
]

_UNIQUE = {"email_hash", "team_email_hash", "address_hash"}


def fill_hashes(apps, schema_editor):
    # Raw SQL: the historical models carry no `save()` logic, and a column
    # this migration adds isn't there to read through them anyway.
    connection = schema_editor.connection
    with connection.cursor() as cursor:
        for table, pk, source, column, index in _INDEXES:
            cursor.execute(f"SELECT {pk}, {source} FROM {table} WHERE {source} <> ''")
            rows = cursor.fetchall()
            hashes = {row_id: index.compute(value) for row_id, value in rows}
            if column in _UNIQUE:
                seen = {}
                clashes = set()
                for row_id, digest in hashes.items():
                    if digest is None:  # only spaces: stored without a hash
                        continue
                    if digest in seen:
                        clashes.update({seen[digest], row_id})
                    seen[digest] = row_id
                if clashes:
                    rule = "ignoring case and spaces" if index.fold_case else "ignoring spaces"
                    raise RuntimeError(
                        f"{table}.{source}: {len(clashes)} rows share an address, {rule} "
                        f"(ids {sorted(map(str, clashes))}). Resolve them by hand, then "
                        "deploy again; see this migration's header."
                    )
            for row_id, digest in hashes.items():
                cursor.execute(
                    f"UPDATE {table} SET {column} = %s WHERE {pk} = %s",
                    [digest, row_id],
                )


class Migration(migrations.Migration):
    dependencies = [
        ("origin", "0200_github_webhook_secret_per_repo"),
    ]

    operations = [
        # Added without their unique constraints, so a clash is reported by
        # `fill_hashes` with the row ids instead of by Postgres.
        migrations.AddField(
            model_name="customuser",
            name="email_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="emailsuppression",
            name="address_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="teaminvite",
            name="invited_email_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.AddField(
            model_name="teammaster",
            name="team_email_hash",
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(fill_hashes, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="customuser",
            name="email_hash",
            field=models.CharField(editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="emailsuppression",
            name="address_hash",
            field=models.CharField(editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.AlterField(
            model_name="teammaster",
            name="team_email_hash",
            field=models.CharField(editable=False, max_length=64, null=True, unique=True),
        ),
        migrations.AddIndex(
            model_name="teaminvite",
            index=models.Index(
                fields=["team", "invited_email_hash"], name="teaminvite_team_email_hash"
            ),
        ),
        migrations.AlterField(
            model_name="connectedaccount",
            name="provider_email",
            field=origin.services.crypto.DecryptingEmailField(
                blank=True, max_length=254, null=True
            ),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="about_me",
            field=origin.services.crypto.DecryptingTextField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="base_country",
            field=origin.services.crypto.DecryptingCharField(blank=True, max_length=50, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="current_location",
            field=origin.services.crypto.DecryptingCharField(blank=True, max_length=64, null=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="email",
            field=origin.services.crypto.DecryptingEmailField(max_length=254, unique=True),
        ),
        migrations.AlterField(
            model_name="customuser",
            name="phone_number",
            field=origin.services.crypto.DecryptingCharField(blank=True, max_length=20, null=True),
        ),
        migrations.AlterField(
            model_name="emailsuppression",
            name="address",
            field=origin.services.crypto.DecryptingEmailField(max_length=254, unique=True),
        ),
        migrations.AlterField(
            model_name="teaminvite",
            name="invited_email",
            field=origin.services.crypto.DecryptingEmailField(max_length=254),
        ),
        migrations.AlterField(
            model_name="teammaster",
            name="team_email",
            field=origin.services.crypto.DecryptingEmailField(max_length=254, unique=True),
        ),
    ]
