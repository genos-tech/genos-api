"""Let a test write the repeated reactions the old route used to leave (MIP-1595).

The table refuses a person's same emoji twice on one comment now
(`unique_task_comment_reaction_per_sender_emoji`). The cleanup command and the DELETE that must
still take back rows which doubled up BEFORE the guard need exactly such rows, so a test that
builds them drops the constraint first. The DDL is part of the test's own transaction on Postgres,
so the rollback at the end of the test puts the constraint back for every other test.
"""

from django.db import connection

CONSTRAINT = "unique_task_comment_reaction_per_sender_emoji"


def allow_repeated_reactions() -> None:
    """Drop the repeat constraint inside the running test's transaction (idempotent).

    A conditional unique constraint is a partial unique INDEX on Postgres, so that is what goes.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1 FROM pg_indexes WHERE indexname = %s", [CONSTRAINT])
        if cursor.fetchone():
            cursor.execute(f'DROP INDEX "{CONSTRAINT}"')
