"""MIP-1996: trigram GIN index for the task title search (`icontains` on TaskMaster.title).

GET /api/v2/search/tasks/ matches titles with `icontains`, which Django runs as
UPPER(title) LIKE UPPER('%q%'); a btree cannot serve a leading wildcard, a trigram GIN on the same
expression can.

Raw SQL on purpose: Django renders `OpClass(Upper("title"), ...)` as `((UPPER("title") gin_trgm_ops)`,
which Postgres rejects. The index is therefore not declared in the model's Meta (makemigrations has
nothing to detect). Non-atomic so it is built CONCURRENTLY (no write lock on the task table).
Reversible: the reverse drops the index concurrently and leaves the extension in place.
"""

from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    atomic = False

    dependencies = [
        ("origin", "0279_skill_grants"),
    ]

    operations = [
        TrigramExtension(),
        migrations.RunSQL(
            sql=(
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS taskmaster_title_trgm_idx "
                "ON origin_taskmaster USING gin (UPPER(title) gin_trgm_ops)"
            ),
            reverse_sql="DROP INDEX CONCURRENTLY IF EXISTS taskmaster_title_trgm_idx",
        ),
    ]
