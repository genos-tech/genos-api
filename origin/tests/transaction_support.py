"""A cheaper flush for the test classes that really need real commits (MIP-1643).

Django's `TransactionTestCase` empties the database after every test with `flush`: one
`TRUNCATE` of every table of every installed app (~129 here), then `post_migrate`, which
re-creates every content type and permission. On a disk-backed Postgres that is about 34 MB of
block writes PER TEST (a `TRUNCATE` gives every table and index a new file, and each new file is
fsynced; measured in MIP-1639/MIP-1643), and the 581 transactional tests of the api suite
wrote 22.3 GB between them (VM disk counter) on a disk-backed Postgres.

`CheapFlushTransactionTestCase` leaves the database in the same state, for less:

  * Only the tables that have rows are cleared, with `DELETE` (no new files, no per-table
    fsync). A test touches a couple of dozen tables, not 129. The statements run in one
    transaction with every constraint deferred, so the order does not matter, exactly like the
    single multi-table `TRUNCATE` it replaces. They take `LOCK TABLE ... ACCESS EXCLUSIVE` on
    every table first, as `TRUNCATE` does: a worker thread of the test that still has a
    transaction open makes the flush wait for it, instead of `DELETE` (which only sees committed
    rows) skipping its rows and letting them leak into the next test.
  * `django_content_type` and `auth_permission` are left alone while nothing changed them, which
    is what `flush` + `post_migrate` ends up with anyway (the same rows, new ids). If a test did
    change either (their rows differ in any column from when the class started), or anything in
    the cheap path fails, the teardown is Django's own full flush (with a warning for the failure
    case), so the next test never sees a difference.

It keeps Django's `_fixture_teardown` contract: `reset_sequences` stays off in both (ids keep
counting up), and nothing here touches `available_apps` or `serialized_rollback` (the classes
that use it keep working, and are given the plain Django behaviour, see `_cheap_flush_allowed`).

Why not `available_apps`: Django then flushes only the listed apps' tables, with `CASCADE`, and
skips `post_migrate`. 103 of the schema's 129 tables are reached by following foreign keys from
the user table (recursive query on the dev database), which every one of these tests writes, so
the cascade truncates most of the schema again anyway, and a test that reads content types or
permissions loses the rows `post_migrate` made.

New transactional tests use this class (MIP-1649)
-------------------------------------------------
Every class under `origin/tests/` that is transactional (Django's `TransactionTestCase`,
`LiveServerTestCase`, `StaticLiveServerTestCase`, DRF's `APILiveServerTestCase`), directly, under
an import alias, or through a base class of any module, must derive from
`CheapFlushTransactionTestCase`. `origin.tests.test_transactional_test_guard` checks it with a
static scan (no database) in the normal suite and fails naming the class, the file and this
module. Django's `TestCase` is not affected: it rolls back and never flushes.

A class that really needs Django's own full flush (for example, it asserts on rows that only
`post_migrate` makes) opts out with a class attribute carrying the reason, and subclasses inherit it:

    class NeedsDjangosFlush(TransactionTestCase):
        full_flush_reason = "asserts the content types are rebuilt by post_migrate after a test"

The reason must be a non-empty string literal; an empty or non-string value fails the guard.
The attribute is greppable (`grep -rn full_flush_reason origin/tests`) and has no runtime effect.
"""

from __future__ import annotations

import warnings

from django.core.management import call_command
from django.db import connections, transaction
from django.test import TransactionTestCase

# Rows `post_migrate` (re)creates after Django's flush: left alone while their number is unchanged.
_RECREATED_BY_POST_MIGRATE = ("django_content_type", "auth_permission")


def _quote(connection, name):
    return connection.ops.quote_name(name)


def _nonempty_tables(connection, tables):
    """The subset of `tables` that has at least one row, in one round trip."""
    if not tables:
        return []
    union = " UNION ALL ".join(
        f"SELECT {i} WHERE EXISTS (SELECT 1 FROM {_quote(connection, t)})"
        for i, t in enumerate(tables)
    )
    with connection.cursor() as cursor:
        cursor.execute(union)
        return [tables[i] for (i,) in cursor.fetchall()]


def _fingerprints(connection, tables):
    """(rows, md5 of every row's text in id order) per table: any insert, delete or edit changes it."""
    with connection.cursor() as cursor:
        prints = {}
        for table in tables:
            quoted = _quote(connection, table)
            cursor.execute(
                f"SELECT count(*), md5(coalesce(string_agg(t::text, ',' ORDER BY id), '')) "
                f"FROM {quoted} AS t"
            )
            prints[table] = cursor.fetchone()
        return prints


def delete_rows_of_nonempty_tables(connection, tables):
    """Empty every table of `tables` that has rows. Returns the names it emptied."""
    if not tables:
        return []
    with transaction.atomic(using=connection.alias):
        with connection.cursor() as cursor:
            # Waits for a straggler's open transaction, like TRUNCATE; only then is "has rows" true.
            cursor.execute(
                "LOCK TABLE "
                + ", ".join(_quote(connection, t) for t in tables)
                + " IN ACCESS EXCLUSIVE MODE"
            )
            dirty = _nonempty_tables(connection, tables)
            if dirty:
                cursor.execute("SET CONSTRAINTS ALL DEFERRED")
            for table in dirty:
                cursor.execute(f"DELETE FROM {_quote(connection, table)}")
    return dirty


class CheapFlushTransactionTestCase(TransactionTestCase):
    """`TransactionTestCase` whose teardown empties the database for far fewer bytes."""

    @classmethod
    def _cheap_flush_allowed(cls):
        # Subclasses that set `available_apps` or `serialized_rollback` get Django's behaviour.
        return cls.available_apps is None and not cls.serialized_rollback

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # What `post_migrate` leaves: taken before any test of this class ran.
        cls._cheap_flush_baseline = {}
        if not cls._cheap_flush_allowed():
            return
        for db_name in cls._databases_names(include_mirrors=False):
            connection = connections[db_name]
            if connection.vendor != "postgresql":
                continue
            existing = set(connection.introspection.table_names())
            tables = [t for t in _RECREATED_BY_POST_MIGRATE if t in existing]
            cls._cheap_flush_baseline[db_name] = _fingerprints(connection, tables)

    def _fixture_teardown(self):
        if not self._cheap_flush_allowed():
            return super()._fixture_teardown()
        for db_name in self._databases_names(include_mirrors=False):
            if not self._cheap_flush(db_name):
                self._full_flush(db_name)

    def _full_flush(self, db_name):
        call_command(
            "flush",
            verbosity=0,
            interactive=False,
            database=db_name,
            reset_sequences=False,
            allow_cascade=False,
            inhibit_post_migrate=False,
        )

    def _cheap_flush(self, db_name):
        """True when the database was emptied the cheap way, False to ask for the full flush."""
        connection = connections[db_name]
        baseline = self._cheap_flush_baseline.get(db_name)
        if baseline is None:
            return False
        try:
            tables = connection.introspection.django_table_names(
                only_existing=True, include_views=False
            )
            if _fingerprints(connection, list(baseline)) != baseline:
                return False
            keep = set(baseline)
            delete_rows_of_nonempty_tables(connection, [t for t in tables if t not in keep])
        except Exception as exc:  # the full flush below is always correct, just dearer
            warnings.warn(
                f"{type(self).__name__}: cheap flush failed ({exc!r}); using Django's full flush",
                stacklevel=2,
            )
            return False
        return True
