"""Guards for fixture code that deletes data in bulk (MIP-1469)."""

from __future__ import annotations

from django.db import connection


class NotATestDatabase(RuntimeError):
    """A bulk delete was asked for on a database that is not the test database."""


def require_test_database(what: str) -> None:
    """Refuse unless the connected database is the one the Django test runner created.

    The runner switches the default connection to `test_<name>`; a management command, a shell or
    a stray import runs against the real database, whose name has no `test_` prefix. One wrong
    import of a test base class from a command must not be able to wipe a database.
    """
    name = str(connection.settings_dict.get("NAME") or "")
    if not name.startswith("test_"):
        raise NotATestDatabase(
            f"{what} deletes rows in bulk and only runs against the test database "
            f"(connected to {name!r}); it is not a tool, never call it from a command"
        )
